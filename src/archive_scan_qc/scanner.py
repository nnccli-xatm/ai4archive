"""Phase-one archive scan QC scanner.

This module intentionally performs read-only checks against source images.
It writes no derivative image files and never modifies originals.
"""

from __future__ import annotations

import csv
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
    manifest_csv: Path | None = None


@dataclass(frozen=True)
class ManifestCheck:
    used: bool
    path: str | None
    entry_count: int
    unique_entry_count: int
    missing_count: int
    unexpected_count: int
    duplicate_count: int


def scan_batch(config: ScanConfig) -> dict[str, Any]:
    input_dir = config.input_dir.resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {input_dir}")

    files = [_inspect_file(path, input_dir) for path in _iter_candidate_files(input_dir)]
    manifest_paths = _read_manifest_paths(config.manifest_csv) if config.manifest_csv else None
    findings = _build_findings(files, config, manifest_paths)
    manifest_check = _summarize_manifest_check(files, manifest_paths, config.manifest_csv)
    summary = _summarize(files, findings, manifest_check)
    generated_at = datetime.now(timezone.utc).isoformat()
    project = {
        "project_id": config.project_id,
        "batch_id": config.batch_id,
        "input_dir": str(input_dir),
        "output_dir": str(config.output_dir.resolve()),
        "min_dpi": config.min_dpi,
        "name_pattern": config.name_pattern,
        "manifest_csv": str(config.manifest_csv.resolve()) if config.manifest_csv else None,
    }
    manifest = {
        "project_id": project["project_id"],
        "batch_id": project["batch_id"],
        "input_dir": project["input_dir"],
        "output_dir": project["output_dir"],
        "rule_version": "scan-qc.phase1.v1",
        "generated_at": generated_at,
        "total_files": summary["total_files"],
        "p0_findings": summary["p0_findings"],
        "p1_findings": summary["p1_findings"],
        "p2_findings": summary["p2_findings"],
        "manifest_used": summary["manifest_used"],
        "manifest_csv": project["manifest_csv"],
        "manifest_entry_count": summary["manifest_entry_count"],
        "manifest_unique_entry_count": summary["manifest_unique_entry_count"],
        "manifest_missing_count": summary["manifest_missing_count"],
        "manifest_unexpected_count": summary["manifest_unexpected_count"],
        "manifest_duplicate_count": summary["manifest_duplicate_count"],
    }

    return {
        "schema_version": "scan-qc.phase1.v1",
        "generated_at": generated_at,
        "project": project,
        "manifest": manifest,
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


def _read_manifest_paths(manifest_csv: Path) -> list[str]:
    with manifest_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "relative_path" not in reader.fieldnames:
            raise ValueError("Manifest CSV must include a relative_path column.")
        paths = []
        for row in reader:
            relative_path = _normalize_manifest_path(row.get("relative_path", ""))
            if relative_path:
                paths.append(relative_path)
        return paths


def _normalize_manifest_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


def _build_findings(
    files: list[dict[str, Any]],
    config: ScanConfig,
    manifest_paths: list[str] | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    _add_per_file_findings(files, findings, config)
    _add_duplicate_name_findings(files, findings)
    _add_duplicate_hash_findings(files, findings)
    _add_batch_consistency_findings(files, findings)
    if manifest_paths is not None:
        _add_manifest_findings(files, findings, manifest_paths)
    return findings


def _add_manifest_findings(
    files: list[dict[str, Any]],
    findings: list[dict[str, str]],
    manifest_paths: list[str],
) -> None:
    file_paths = {item["relative_path"] for item in files}
    manifest_set = set(manifest_paths)

    for path in sorted(manifest_set - file_paths):
        findings.append(
            _path_finding(
                path,
                "manifest_missing_file",
                "P0",
                "Manifest expects this file, but it was not found in the scanned directory.",
            )
        )

    for path in sorted(file_paths - manifest_set):
        findings.append(
            _path_finding(
                path,
                "manifest_unexpected_file",
                "P1",
                "File exists in the scanned directory, but it is not listed in the manifest.",
            )
        )

    duplicate_paths = sorted(path for path, count in _path_counts(manifest_paths).items() if count > 1)
    for path in duplicate_paths:
        findings.append(
            _path_finding(
                path,
                "manifest_duplicate_entry",
                "P0",
                "Manifest contains duplicate relative_path entries.",
            )
        )


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


def _path_finding(relative_path: str, rule: str, severity: str, message: str) -> dict[str, str]:
    return {
        "relative_path": relative_path,
        "rule": rule,
        "severity": severity,
        "message": message,
    }


def _path_counts(paths: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in paths:
        counts[path] = counts.get(path, 0) + 1
    return counts


def _summarize_manifest_check(
    files: list[dict[str, Any]],
    manifest_paths: list[str] | None,
    manifest_csv: Path | None,
) -> ManifestCheck:
    if manifest_paths is None:
        return ManifestCheck(False, None, 0, 0, 0, 0, 0)

    file_paths = {item["relative_path"] for item in files}
    manifest_set = set(manifest_paths)
    duplicate_count = sum(1 for count in _path_counts(manifest_paths).values() if count > 1)
    return ManifestCheck(
        used=True,
        path=str(manifest_csv.resolve()) if manifest_csv else None,
        entry_count=len(manifest_paths),
        unique_entry_count=len(manifest_set),
        missing_count=len(manifest_set - file_paths),
        unexpected_count=len(file_paths - manifest_set),
        duplicate_count=duplicate_count,
    )


def _summarize(
    files: list[dict[str, Any]],
    findings: list[dict[str, str]],
    manifest_check: ManifestCheck,
) -> dict[str, int | bool | str | None]:
    return {
        "total_files": len(files),
        "openable_files": sum(1 for item in files if item["openable"]),
        "total_findings": len(findings),
        "p0_findings": sum(1 for finding in findings if finding["severity"] == "P0"),
        "p1_findings": sum(1 for finding in findings if finding["severity"] == "P1"),
        "p2_findings": sum(1 for finding in findings if finding["severity"] == "P2"),
        "manifest_used": manifest_check.used,
        "manifest_csv": manifest_check.path,
        "manifest_entry_count": manifest_check.entry_count,
        "manifest_unique_entry_count": manifest_check.unique_entry_count,
        "manifest_missing_count": manifest_check.missing_count,
        "manifest_unexpected_count": manifest_check.unexpected_count,
        "manifest_duplicate_count": manifest_check.duplicate_count,
    }
