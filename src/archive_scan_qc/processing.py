"""Local derivative-image processing for scanned-image batches.

The processing layer never modifies source images. It writes derivative files
and a manifest that links each output back to the original scan record.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


def process_images(report: dict[str, Any], input_dir: Path, process_dir: Path) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    process_dir = process_dir.resolve()
    image_root = process_dir / "images"
    records = [_process_record(item, input_dir, image_root) for item in report["files"]]
    manifest = {
        "schema_version": "scan-qc.processing.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": report.get("project", {}),
        "source_report_schema_version": report.get("schema_version"),
        "process_dir": str(process_dir),
        "image_root": str(image_root),
        "summary": {
            "total_files": len(records),
            "processed_files": sum(1 for item in records if item["status"] == "processed"),
            "skipped_files": sum(1 for item in records if item["status"] == "skipped"),
            "failed_files": sum(1 for item in records if item["status"] == "failed"),
        },
        "operations": [
            "exif_transpose",
            "convert_non_l_or_rgb_to_rgb",
            "autocontrast_cutoff_0_5",
            "preserve_source_relative_path",
        ],
        "files": records,
    }
    process_dir.mkdir(parents=True, exist_ok=True)
    (process_dir / "processing_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _process_record(item: dict[str, Any], input_dir: Path, image_root: Path) -> dict[str, Any]:
    relative_path = item["relative_path"]
    base = {
        "source_relative_path": relative_path,
        "source_sha256": item.get("sha256"),
        "output_relative_path": None,
        "output_sha256": None,
        "status": "skipped",
        "operations": [],
        "error": None,
    }
    if not item.get("openable"):
        base["error"] = "source image is not openable"
        return base

    source = input_dir / relative_path
    target = image_root / relative_path
    try:
        with Image.open(source) as image:
            processed, operations = _process_image(image)
            target.parent.mkdir(parents=True, exist_ok=True)
            _save_image(processed, target, image)
        base.update(
            {
                "output_relative_path": target.relative_to(image_root.parent).as_posix(),
                "output_sha256": _sha256(target),
                "status": "processed",
                "operations": operations,
            }
        )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        base["status"] = "failed"
        base["error"] = str(exc)
    return base


def _process_image(image: Image.Image) -> tuple[Image.Image, list[str]]:
    operations: list[str] = []
    processed = ImageOps.exif_transpose(image)
    operations.append("exif_transpose")

    if processed.mode not in {"L", "RGB"}:
        processed = processed.convert("RGB")
        operations.append("convert_to_rgb")

    processed = ImageOps.autocontrast(processed, cutoff=0.5)
    operations.append("autocontrast_cutoff_0_5")
    return processed, operations


def _save_image(image: Image.Image, target: Path, source_image: Image.Image) -> None:
    save_kwargs: dict[str, Any] = {}
    dpi = source_image.info.get("dpi")
    if dpi:
        save_kwargs["dpi"] = dpi

    suffix = target.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".jpe", ".jfif"}:
        if image.mode != "RGB":
            image = image.convert("RGB")
        save_kwargs.update({"quality": 95})
    image.save(target, **save_kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
