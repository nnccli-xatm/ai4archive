"""Local derivative-image processing for scanned-image batches.

The processing layer never modifies source images. It writes derivative files
and a manifest that links each output back to the original scan record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass(frozen=True)
class ProcessingOptions:
    auto_crop: bool = False


def process_images(
    report: dict[str, Any],
    input_dir: Path,
    process_dir: Path,
    options: ProcessingOptions | None = None,
) -> dict[str, Any]:
    options = options or ProcessingOptions()
    input_dir = input_dir.resolve()
    process_dir = process_dir.resolve()
    image_root = process_dir / "images"
    records = [_process_record(item, input_dir, image_root, options) for item in report["files"]]
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
            "auto_crop_conservative" if options.auto_crop else "auto_crop_disabled",
            "autocontrast_cutoff_0_5",
            "preserve_source_relative_path",
        ],
        "files": records,
    }
    process_dir.mkdir(parents=True, exist_ok=True)
    (process_dir / "processing_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _process_record(
    item: dict[str, Any],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
) -> dict[str, Any]:
    relative_path = item["relative_path"]
    base = {
        "source_relative_path": relative_path,
        "source_sha256": item.get("sha256"),
        "output_relative_path": None,
        "output_sha256": None,
        "original_size": None,
        "output_size": None,
        "crop_bbox": None,
        "cropped": False,
        "status": "skipped",
        "operations": [],
        "error": None,
        "failure_reason": None,
    }
    if not item.get("openable"):
        base["failure_reason"] = "source image is not openable"
        base["error"] = base["failure_reason"]
        return base

    source = input_dir / relative_path
    target = image_root / relative_path
    try:
        with Image.open(source) as image:
            processed, operations, crop_info = _process_image(image, options)
            target.parent.mkdir(parents=True, exist_ok=True)
            _save_image(processed, target, image)
        base.update(
            {
                "output_relative_path": target.relative_to(image_root.parent).as_posix(),
                "output_sha256": _sha256(target),
                "original_size": crop_info["original_size"],
                "output_size": crop_info["output_size"],
                "crop_bbox": crop_info["crop_bbox"],
                "cropped": crop_info["cropped"],
                "status": "processed",
                "operations": operations,
            }
        )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        base["status"] = "failed"
        base["error"] = str(exc)
        base["failure_reason"] = str(exc)
    return base


def _process_image(image: Image.Image, options: ProcessingOptions) -> tuple[Image.Image, list[str], dict[str, Any]]:
    operations: list[str] = []
    processed = ImageOps.exif_transpose(image)
    operations.append("exif_transpose")
    original_size = list(processed.size)

    if processed.mode not in {"L", "RGB"}:
        processed = processed.convert("RGB")
        operations.append("convert_to_rgb")

    crop_bbox: tuple[int, int, int, int] | None = None
    if options.auto_crop:
        crop_bbox = _detect_conservative_crop_bbox(processed)
        if crop_bbox:
            processed = processed.crop(crop_bbox)
            operations.append("auto_crop_conservative")
        else:
            operations.append("auto_crop_noop")
    else:
        operations.append("auto_crop_disabled")

    processed = ImageOps.autocontrast(processed, cutoff=0.5)
    operations.append("autocontrast_cutoff_0_5")
    crop_info = {
        "original_size": original_size,
        "output_size": list(processed.size),
        "crop_bbox": list(crop_bbox) if crop_bbox else None,
        "cropped": crop_bbox is not None,
    }
    return processed, operations, crop_info


def _detect_conservative_crop_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    width, height = image.size
    if width < 20 or height < 20:
        return None

    grayscale = image.convert("L")
    background = _corner_background_value(grayscale)
    diff = grayscale.point(lambda value: 255 if abs(value - background) >= 18 else 0)
    bbox = diff.getbbox()
    if not bbox:
        return None

    left, top, right, bottom = bbox
    if min(left, top, width - right, height - bottom) < 2:
        return None

    crop_width = right - left
    crop_height = bottom - top
    crop_area_ratio = (crop_width * crop_height) / (width * height)
    if crop_area_ratio < 0.25 or crop_area_ratio > 0.98:
        return None

    return bbox


def _corner_background_value(image: Image.Image) -> int:
    width, height = image.size
    sample = max(3, min(width, height) // 20)
    corners = [
        image.crop((0, 0, sample, sample)),
        image.crop((width - sample, 0, width, sample)),
        image.crop((0, height - sample, sample, height)),
        image.crop((width - sample, height - sample, width, height)),
    ]
    values = []
    for corner in corners:
        histogram = corner.histogram()
        total = sum(value * count for value, count in enumerate(histogram))
        values.append(int(round(total / (sample * sample))))
    return sorted(values)[len(values) // 2]


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
