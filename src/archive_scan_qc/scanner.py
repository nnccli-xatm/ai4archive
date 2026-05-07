"""Phase-one archive scan QC scanner.

This module intentionally performs read-only checks against source images.
It writes no derivative image files and never modifies originals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any

from PIL import Image, UnidentifiedImageError

SUPPORTED_EXTENSIONS = {
    ".bmp",
    ".dib",
    ".jfif",
    ".jp2",
    ".jpe",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
}


@dataclass(frozen=True)
class ScanConfig:
    project_id: str
    batch_id: str
    input_dir: Path
    output_dir: Path
    min_dpi: int = 200
    name_pattern: str | None = None


def scan_batch(config: ScanConfig) -> dict[str, Any]:
    input_dir = config.input_dir.resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {input_dir}")

    files = [_inspect_file(path, input_dir) for path in _iter_candidate_files(input_dir)]
    findings = _build_findings(files, config)
    summary = _summarize(files, findings)

    return {
        "schema_version": "scan-qc.phase1.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": {
            "project_id": config.project_id,
            "batch_id": config.batch_id,
            "input_dir": str(input_dir),
            "output_dir": str(config.output_dir.resolve()),
            "min_dpi": config.min_dpi,
            "name_pattern": config.name_pattern,
        },
        "dependency_notes": [
            "Pillow is used for local image openability and metadata collection; it is open source, lightweight, cross-platform, and does not require cloud services.",
            "Only Python standard-library modules are used for hashing, CSV, JSON, paths, and rule checks.",
        ],
        "summary": summary,
        "files": files,
        "findings": findings,
    }


def _iter_candidate_files(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.rglob("*") if path.is_file() and not path.name.startswith("."))


def _inspect_file(path: Path, root: Path) -> dict[str, Any]:
    relative_path = path.relative_to(root).as_posix()
    file_size = path.stat().st_size
    sha256 = _sha256(path)
    base = {
        "relative_path": relative_path,
        "filename": path.name,
        "extension": path.suffix.lower(),
        "file_size": file_size,
        "sha256": sha256,
        "openable": False,
        "format": None,
        "width": None,
        "height": None,
        "dpi_x": None,
        "dpi_y": None,
        "color_mode": None,
        "error": None,
    }

    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dpi_x, dpi_y = _extract_dpi(image.info.get("dpi"))
            base.update(
                {
                    "openable": True,
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "dpi_x": dpi_x,
                    "dpi_y": dpi_y,
                    "color_mode": image.mode,
                    "error": None,
                }
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        base["error"] = str(exc)

    return base


def _extract_dpi(raw_dpi: Any) -> tuple[int | None, int | None]:
    if not raw_dpi or not isinstance(raw_dpi, tuple) or len(raw_dpi) < 2:
        return None, None
    try:
        dpi_x = round(float(raw_dpi[0]))
        dpi_y = round(float(raw_dpi[1]))
    except (TypeError, ValueError):
        return None, None
    return dpi_x, dpi_y


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_findings(files: list[dict[str, Any]], config: ScanConfig) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    _add_per_file_findings(files, findings, config)
    _add_duplicate_name_findings(files, findings)
    _add_duplicate_hash_findings(files, findings)
    _add_batch_consistency_findings(files, findings)
    return findings


def _add_per_file_findings(
    files: list[dict[str, Any]],
    findings: list[dict[str, str]],
    config: ScanConfig,
) -> None:
    name_regex = re.compile(config.name_pattern) if config.name_pattern else None
    for item in files:
        if item["extension"] not in SUPPORTED_EXTENSIONS:
            findings.append(_finding(item, "unsupported_format", "P1", "File extension is not in the phase-one supported image list."))
        if not item["openable"]:
            findings.append(_finding(item, "openability", "P0", f"Image could not be opened: {item['error']}"))
            continue
        if item["dpi_x"] is None or item["dpi_y"] is None:
            findings.append(_finding(item, "dpi_missing", "P1", "Image does not expose horizontal and vertical DPI metadata."))
        elif item["dpi_x"] < config.min_dpi or item["dpi_y"] < config.min_dpi:
            findings.append(_finding(item, "dpi_minimum", "P0", f"Image DPI is below minimum {config.min_dpi}."))
        if not item["width"] or not item["height"]:
            findings.append(_finding(item, "dimensions", "P0", "Image width or height is missing."))
        if name_regex and not name_regex.fullmatch(Path(item["filename"]).stem):
            findings.append(_finding(item, "name_pattern", "P1", "Filename stem does not match configured naming pattern."))


def _add_duplicate_name_findings(files: list[dict[str, Any]], findings: list[dict[str, str]]) -> None:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        by_name.setdefault(item["filename"].casefold(), []).append(item)
    for matches in by_name.values():
        if len(matches) > 1:
            paths = ", ".join(match["relative_path"] for match in matches)
            for item in matches:
                findings.append(_finding(item, "duplicate_name", "P0", f"Filename is not unique in batch: {paths}"))


def _add_duplicate_hash_findings(files: list[dict[str, Any]], findings: list[dict[str, str]]) -> None:
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        by_hash.setdefault(item["sha256"], []).append(item)
    for matches in by_hash.values():
        if len(matches) > 1:
            paths = ", ".join(match["relative_path"] for match in matches)
            for item in matches:
                findings.append(_finding(item, "duplicate_file", "P0", f"SHA-256 duplicate detected: {paths}"))


def _add_batch_consistency_findings(files: list[dict[str, Any]], findings: list[dict[str, str]]) -> None:
    open_files = [item for item in files if item["openable"]]
    for key, rule in (("format", "batch_format_consistency"), ("color_mode", "batch_color_mode_consistency")):
        values = sorted({str(item[key]) for item in open_files if item[key] is not None})
        if len(values) > 1:
            message = f"Openable files in batch use multiple {key} values: {', '.join(values)}"
            for item in open_files:
                findings.append(_finding(item, rule, "P2", message))

    dpi_values = sorted({(item["dpi_x"], item["dpi_y"]) for item in open_files if item["dpi_x"] and item["dpi_y"]})
    if len(dpi_values) > 1:
        values = ", ".join(f"{x}x{y}" for x, y in dpi_values)
        for item in open_files:
            findings.append(_finding(item, "batch_dpi_consistency", "P2", f"Openable files in batch use multiple DPI values: {values}"))


def _finding(item: dict[str, Any], rule: str, severity: str, message: str) -> dict[str, str]:
    return {
        "relative_path": item["relative_path"],
        "rule": rule,
        "severity": severity,
        "message": message,
    }


def _summarize(files: list[dict[str, Any]], findings: list[dict[str, str]]) -> dict[str, int]:
    return {
        "total_files": len(files),
        "openable_files": sum(1 for item in files if item["openable"]),
        "total_findings": len(findings),
        "p0_findings": sum(1 for finding in findings if finding["severity"] == "P0"),
        "p1_findings": sum(1 for finding in findings if finding["severity"] == "P1"),
        "p2_findings": sum(1 for finding in findings if finding["severity"] == "P2"),
    }
