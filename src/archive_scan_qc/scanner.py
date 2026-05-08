"""Phase-one archive scan QC scanner.

This module intentionally performs read-only checks against source images.
It writes no derivative image files and never modifies originals.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Any

from PIL import Image, ImageStat, UnidentifiedImageError

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
    dark_mean_threshold: float = 45.0
    bright_mean_threshold: float = 250.0
    low_contrast_stddev_threshold: float = 10.0
    blur_laplacian_variance_threshold: float = 20.0
    blur_min_contrast_stddev: float = 12.0


@dataclass(frozen=True)
class ManifestCheck:
    used: bool
    path: str | None
    entry_count: int
    unique_entry_count: int
    missing_count: int
    unexpected_count: int
    duplicate_count: int


@dataclass(frozen=True)
class SkipStats:
    hidden_directories: int = 0
    output_directories: int = 0
    hidden_files: int = 0
    manifest_files: int = 0

    @property
    def directories(self) -> int:
        return self.hidden_directories + self.output_directories

    @property
    def files(self) -> int:
        return self.hidden_files + self.manifest_files

    @property
    def total(self) -> int:
        return self.directories + self.files


def scan_batch(config: ScanConfig) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    start_seconds = time.perf_counter()
    input_dir = config.input_dir.resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {input_dir}")

    candidate_files, skip_stats = _iter_candidate_files(input_dir, config.output_dir, config.manifest_csv)
    files = [_inspect_file(path, input_dir) for path in candidate_files]
    manifest_paths = _read_manifest_paths(config.manifest_csv) if config.manifest_csv else None
    findings = _build_findings(files, config, manifest_paths)
    manifest_check = _summarize_manifest_check(files, manifest_paths, config.manifest_csv)
    summary = _summarize(files, findings, manifest_check, skip_stats)
    finished_at = datetime.now(timezone.utc)
    generated_at = finished_at.isoformat()
    performance = _performance_summary(
        started_at,
        finished_at,
        time.perf_counter() - start_seconds,
        total_files=summary["total_files"],
        openable_files=summary["openable_files"],
    )
    summary["performance"] = performance
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
        "skipped_total_count": summary["skipped_total_count"],
        "skipped_file_count": summary["skipped_file_count"],
        "skipped_directory_count": summary["skipped_directory_count"],
        "performance": performance,
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


def _iter_candidate_files(input_dir: Path, output_dir: Path, manifest_csv: Path | None) -> tuple[list[Path], SkipStats]:
    output_dir = output_dir.resolve()
    manifest_csv = manifest_csv.resolve() if manifest_csv else None
    files: list[Path] = []
    hidden_directories = 0
    output_directories = 0
    hidden_files = 0
    manifest_files = 0

    for directory_name, dirnames, filenames in os.walk(input_dir):
        directory = Path(directory_name)
        kept_dirnames = []
        for dirname in dirnames:
            path = directory / dirname
            if dirname.startswith("."):
                hidden_directories += 1
            elif _is_relative_to(path.resolve(), output_dir):
                output_directories += 1
            else:
                kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for filename in filenames:
            path = directory / filename
            if filename.startswith("."):
                hidden_files += 1
            elif manifest_csv and path.resolve() == manifest_csv:
                manifest_files += 1
            else:
                files.append(path)

    return (
        sorted(files),
        SkipStats(
            hidden_directories=hidden_directories,
            output_directories=output_directories,
            hidden_files=hidden_files,
            manifest_files=manifest_files,
        ),
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


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
        "quality_brightness_mean": None,
        "quality_contrast_stddev": None,
        "quality_sharpness_laplacian_var": None,
        "error": None,
    }

    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dpi_x, dpi_y = _extract_dpi(image.info.get("dpi"))
            quality_metrics = _measure_quality(image)
            base.update(
                {
                    "openable": True,
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "dpi_x": dpi_x,
                    "dpi_y": dpi_y,
                    "color_mode": image.mode,
                    **quality_metrics,
                    "error": None,
                }
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        base["error"] = str(exc)

    return base


def _measure_quality(image: Image.Image) -> dict[str, float]:
    grayscale = image.convert("L")
    sample = grayscale.copy()
    sample.thumbnail((900, 900), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(sample)
    brightness_mean = float(stat.mean[0])
    contrast_stddev = float(stat.stddev[0])
    return {
        "quality_brightness_mean": round(brightness_mean, 2),
        "quality_contrast_stddev": round(contrast_stddev, 2),
        "quality_sharpness_laplacian_var": round(_laplacian_variance(sample), 2),
    }


def _laplacian_variance(image: Image.Image) -> float:
    width, height = image.size
    if width < 3 or height < 3:
        return 0.0

    pixels = image.load()
    count = 0
    total = 0.0
    total_sq = 0.0
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            center = pixels[x, y] * 4
            value = center - pixels[x - 1, y] - pixels[x + 1, y] - pixels[x, y - 1] - pixels[x, y + 1]
            total += value
            total_sq += value * value
            count += 1
    if not count:
        return 0.0
    mean = total / count
    return max(0.0, (total_sq / count) - (mean * mean))


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
                "P0",
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
        _add_quality_findings(item, findings, config)


def _add_quality_findings(
    item: dict[str, Any],
    findings: list[dict[str, str]],
    config: ScanConfig,
) -> None:
    brightness = item.get("quality_brightness_mean")
    contrast = item.get("quality_contrast_stddev")
    sharpness = item.get("quality_sharpness_laplacian_var")
    if brightness is None or contrast is None or sharpness is None:
        return

    if brightness < config.dark_mean_threshold:
        findings.append(
            _finding(
                item,
                "quality_too_dark",
                "P1",
                f"Mean grayscale brightness {brightness} is below conservative threshold {config.dark_mean_threshold}.",
            )
        )
    if brightness > config.bright_mean_threshold and contrast < config.low_contrast_stddev_threshold:
        findings.append(
            _finding(
                item,
                "quality_too_bright",
                "P1",
                f"Mean grayscale brightness {brightness} is above conservative threshold {config.bright_mean_threshold} with very low contrast.",
            )
        )
    if contrast < config.low_contrast_stddev_threshold:
        findings.append(
            _finding(
                item,
                "quality_low_contrast",
                "P2",
                f"Grayscale standard deviation {contrast} is below conservative threshold {config.low_contrast_stddev_threshold}.",
            )
        )
    if contrast >= config.blur_min_contrast_stddev and sharpness < config.blur_laplacian_variance_threshold:
        findings.append(
            _finding(
                item,
                "quality_suspected_blur",
                "P2",
                f"Laplacian variance sharpness {sharpness} is below conservative threshold {config.blur_laplacian_variance_threshold}.",
            )
        )


def _add_duplicate_name_findings(files: list[dict[str, Any]], findings: list[dict[str, str]]) -> None:
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in files:
        parent = Path(item["relative_path"]).parent.as_posix()
        by_name.setdefault((parent, item["filename"].casefold()), []).append(item)
    for matches in by_name.values():
        if len(matches) > 1:
            paths = ", ".join(match["relative_path"] for match in matches)
            for item in matches:
                findings.append(_finding(item, "duplicate_name", "P0", f"Filename is not unique within its directory: {paths}"))


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
    skip_stats: SkipStats,
) -> dict[str, Any]:
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
        "skipped_total_count": skip_stats.total,
        "skipped_file_count": skip_stats.files,
        "skipped_directory_count": skip_stats.directories,
        "skipped_hidden_directory_count": skip_stats.hidden_directories,
        "skipped_output_directory_count": skip_stats.output_directories,
        "skipped_hidden_file_count": skip_stats.hidden_files,
        "skipped_manifest_file_count": skip_stats.manifest_files,
    }


def _performance_summary(
    started_at: datetime,
    finished_at: datetime,
    elapsed_seconds: float,
    *,
    total_files: int,
    openable_files: int,
) -> dict[str, Any]:
    elapsed_seconds = max(0.0, round(elapsed_seconds, 6))
    return {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "total_files": total_files,
        "openable_files": openable_files,
        "files_per_minute": _files_per_minute(total_files, elapsed_seconds),
        "openable_files_per_minute": _files_per_minute(openable_files, elapsed_seconds),
    }


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)
