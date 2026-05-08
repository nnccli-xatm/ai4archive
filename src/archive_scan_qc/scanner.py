"""Phase-one archive scan QC scanner.

This module intentionally performs read-only checks against source images.
It writes no derivative image files and never modifies originals.
"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Any

from PIL import Image, ImageStat, UnidentifiedImageError

from .concurrency import resolve_worker_count, worker_metadata
from .rules import RulesProfile, default_rules_profile

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

PROTECTED_P0_RULES = {
    "openability",
    "dpi_minimum",
    "dimensions",
    "duplicate_name",
    "duplicate_file",
    "manifest_missing_file",
    "manifest_unexpected_file",
    "manifest_duplicate_entry",
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
    rules_profile: RulesProfile | None = None
    workers: int | None = None


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

    profile = _effective_profile(config)
    candidate_files, skip_stats = _iter_candidate_files(input_dir, config.output_dir, config.manifest_csv)
    scan_workers = resolve_worker_count(config.workers, len(candidate_files))
    files = _inspect_files(candidate_files, input_dir, scan_workers)
    manifest_paths = _read_manifest_paths(config.manifest_csv) if config.manifest_csv else None
    findings = _build_findings(files, profile, manifest_paths)
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
        workers=worker_metadata(config.workers, scan_workers),
    )
    summary["performance"] = performance
    project = {
        "project_id": config.project_id,
        "batch_id": config.batch_id,
        "input_dir": str(input_dir),
        "output_dir": str(config.output_dir.resolve()),
        "min_dpi": profile.min_dpi,
        "name_pattern": profile.name_pattern,
        "manifest_csv": str(config.manifest_csv.resolve()) if config.manifest_csv else None,
        "rules_profile": profile.metadata(),
        "workers": scan_workers,
        "worker_mode": performance["mode"],
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
        "blank_page_findings": summary["blank_page_findings"],
        "manifest_used": summary["manifest_used"],
        "manifest_csv": project["manifest_csv"],
        "rules_profile": profile.metadata(),
        "manifest_entry_count": summary["manifest_entry_count"],
        "manifest_unique_entry_count": summary["manifest_unique_entry_count"],
        "manifest_missing_count": summary["manifest_missing_count"],
        "manifest_unexpected_count": summary["manifest_unexpected_count"],
        "manifest_duplicate_count": summary["manifest_duplicate_count"],
        "skipped_total_count": summary["skipped_total_count"],
        "skipped_file_count": summary["skipped_file_count"],
        "skipped_directory_count": summary["skipped_directory_count"],
        "performance": performance,
        "workers": scan_workers,
        "worker_mode": performance["mode"],
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


def _inspect_files(candidate_files: list[Path], input_dir: Path, workers: int) -> list[dict[str, Any]]:
    if workers == 1:
        return [_inspect_file(path, input_dir) for path in candidate_files]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(lambda path: _inspect_file(path, input_dir), candidate_files))


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


def _effective_profile(config: ScanConfig) -> RulesProfile:
    profile = config.rules_profile or default_rules_profile()
    return RulesProfile(
        name=profile.name,
        version=profile.version,
        source=profile.source,
        min_dpi=config.min_dpi if config.rules_profile is None else profile.min_dpi,
        name_pattern=config.name_pattern if config.rules_profile is None else profile.name_pattern,
        dark_mean_threshold=config.dark_mean_threshold if config.rules_profile is None else profile.dark_mean_threshold,
        bright_mean_threshold=config.bright_mean_threshold if config.rules_profile is None else profile.bright_mean_threshold,
        low_contrast_stddev_threshold=(
            config.low_contrast_stddev_threshold if config.rules_profile is None else profile.low_contrast_stddev_threshold
        ),
        blur_laplacian_variance_threshold=(
            config.blur_laplacian_variance_threshold
            if config.rules_profile is None
            else profile.blur_laplacian_variance_threshold
        ),
        blur_min_contrast_stddev=config.blur_min_contrast_stddev if config.rules_profile is None else profile.blur_min_contrast_stddev,
        blank_brightness_min=profile.blank_brightness_min,
        blank_contrast_max=profile.blank_contrast_max,
        blank_foreground_coverage_max=profile.blank_foreground_coverage_max,
        blank_edge_coverage_max=profile.blank_edge_coverage_max,
        blank_dark_pixel_ratio_max=profile.blank_dark_pixel_ratio_max,
        rules=profile.rules,
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
        "orientation_class": None,
        "aspect_ratio": None,
        "exif_orientation": None,
        "exif_orientation_requires_transpose": False,
        "quality_brightness_mean": None,
        "quality_contrast_stddev": None,
        "quality_sharpness_laplacian_var": None,
        "quality_dark_pixel_ratio": None,
        "quality_foreground_coverage": None,
        "quality_edge_coverage": None,
        "error": None,
    }

    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            dpi_x, dpi_y = _extract_dpi(image.info.get("dpi"))
            orientation_metrics = _measure_orientation(image)
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
                    **orientation_metrics,
                    **quality_metrics,
                    "error": None,
                }
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        base["error"] = str(exc)

    return base


def _measure_orientation(image: Image.Image) -> dict[str, Any]:
    width, height = image.width, image.height
    aspect_ratio = round(width / height, 4) if height else None
    exif_orientation = _extract_exif_orientation(image)
    return {
        "orientation_class": _orientation_class(width, height),
        "aspect_ratio": aspect_ratio,
        "exif_orientation": exif_orientation,
        "exif_orientation_requires_transpose": exif_orientation in {2, 3, 4, 5, 6, 7, 8},
    }


def _orientation_class(width: int, height: int) -> str | None:
    if not width or not height:
        return None
    ratio = width / height
    if 0.95 <= ratio <= 1.05:
        return "square"
    if ratio > 1:
        return "landscape"
    return "portrait"


def _extract_exif_orientation(image: Image.Image) -> int | None:
    try:
        value = image.getexif().get(274)
    except (AttributeError, OSError, ValueError, TypeError):
        return None
    if isinstance(value, int) and 1 <= value <= 8:
        return value
    return None


def _measure_quality(image: Image.Image) -> dict[str, float]:
    grayscale = image.convert("L")
    sample = grayscale.copy()
    sample.thumbnail((900, 900), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(sample)
    brightness_mean = float(stat.mean[0])
    contrast_stddev = float(stat.stddev[0])
    laplacian_variance, edge_coverage = _laplacian_metrics(sample)
    return {
        "quality_brightness_mean": round(brightness_mean, 2),
        "quality_contrast_stddev": round(contrast_stddev, 2),
        "quality_sharpness_laplacian_var": round(laplacian_variance, 2),
        "quality_dark_pixel_ratio": round(_pixel_ratio_at_or_below(sample, 64), 6),
        "quality_foreground_coverage": round(_pixel_ratio_at_or_below(sample, 230), 6),
        "quality_edge_coverage": round(edge_coverage, 6),
    }


def _pixel_ratio_at_or_below(image: Image.Image, threshold: int) -> float:
    width, height = image.size
    total = width * height
    if not total:
        return 0.0
    histogram = image.histogram()
    return sum(histogram[: threshold + 1]) / total


def _laplacian_metrics(image: Image.Image) -> tuple[float, float]:
    width, height = image.size
    if width < 3 or height < 3:
        return 0.0, 0.0

    pixels = image.load()
    count = 0
    total = 0.0
    total_sq = 0.0
    edge_count = 0
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            center = pixels[x, y] * 4
            value = center - pixels[x - 1, y] - pixels[x + 1, y] - pixels[x, y - 1] - pixels[x, y + 1]
            total += value
            total_sq += value * value
            if abs(value) >= 18:
                edge_count += 1
            count += 1
    if not count:
        return 0.0, 0.0
    mean = total / count
    return max(0.0, (total_sq / count) - (mean * mean)), edge_count / count


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
    profile: RulesProfile,
    manifest_paths: list[str] | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    _add_per_file_findings(files, findings, profile)
    _add_duplicate_name_findings(files, findings, profile)
    _add_duplicate_hash_findings(files, findings, profile)
    _add_batch_consistency_findings(files, findings, profile)
    if manifest_paths is not None:
        _add_manifest_findings(files, findings, manifest_paths, profile)
    return findings


def _add_manifest_findings(
    files: list[dict[str, Any]],
    findings: list[dict[str, str]],
    manifest_paths: list[str],
    profile: RulesProfile,
) -> None:
    file_paths = {item["relative_path"] for item in files}
    manifest_set = set(manifest_paths)

    for path in sorted(manifest_set - file_paths):
        _append_path_finding(
            findings,
            path,
            "manifest_missing_file",
            "P0",
            "Manifest expects this file, but it was not found in the scanned directory.",
            profile,
        )

    for path in sorted(file_paths - manifest_set):
        _append_path_finding(
            findings,
            path,
            "manifest_unexpected_file",
            "P0",
            "File exists in the scanned directory, but it is not listed in the manifest.",
            profile,
        )

    duplicate_paths = sorted(path for path, count in _path_counts(manifest_paths).items() if count > 1)
    for path in duplicate_paths:
        _append_path_finding(
            findings,
            path,
            "manifest_duplicate_entry",
            "P0",
            "Manifest contains duplicate relative_path entries.",
            profile,
        )


def _add_per_file_findings(
    files: list[dict[str, Any]],
    findings: list[dict[str, str]],
    profile: RulesProfile,
) -> None:
    name_regex = re.compile(profile.name_pattern) if profile.name_pattern else None
    for item in files:
        if item["extension"] not in SUPPORTED_EXTENSIONS:
            _append_finding(
                item,
                findings,
                "unsupported_format",
                "P1",
                "File extension is not in the phase-one supported image list.",
                profile,
            )
        if not item["openable"]:
            _append_finding(item, findings, "openability", "P0", f"Image could not be opened: {item['error']}", profile)
            continue
        if item["dpi_x"] is None or item["dpi_y"] is None:
            _append_finding(
                item,
                findings,
                "dpi_missing",
                "P1",
                "Image does not expose horizontal and vertical DPI metadata.",
                profile,
            )
        elif item["dpi_x"] < profile.min_dpi or item["dpi_y"] < profile.min_dpi:
            _append_finding(item, findings, "dpi_minimum", "P0", f"Image DPI is below minimum {profile.min_dpi}.", profile)
        if not item["width"] or not item["height"]:
            _append_finding(item, findings, "dimensions", "P0", "Image width or height is missing.", profile)
        if name_regex and not name_regex.fullmatch(Path(item["filename"]).stem):
            _append_finding(
                item,
                findings,
                "name_pattern",
                "P1",
                "Filename stem does not match configured naming pattern.",
                profile,
            )
        _add_quality_findings(item, findings, profile)


def _add_quality_findings(
    item: dict[str, Any],
    findings: list[dict[str, str]],
    profile: RulesProfile,
) -> None:
    brightness = item.get("quality_brightness_mean")
    contrast = item.get("quality_contrast_stddev")
    sharpness = item.get("quality_sharpness_laplacian_var")
    dark_pixel_ratio = item.get("quality_dark_pixel_ratio")
    foreground_coverage = item.get("quality_foreground_coverage")
    edge_coverage = item.get("quality_edge_coverage")
    if (
        brightness is None
        or contrast is None
        or sharpness is None
        or dark_pixel_ratio is None
        or foreground_coverage is None
        or edge_coverage is None
    ):
        return

    if (
        brightness >= profile.blank_brightness_min
        and contrast <= profile.blank_contrast_max
        and foreground_coverage <= profile.blank_foreground_coverage_max
        and edge_coverage <= profile.blank_edge_coverage_max
        and dark_pixel_ratio <= profile.blank_dark_pixel_ratio_max
    ):
        _append_finding(
            item,
            findings,
            "quality_near_blank_page",
            "P2",
            "Page has very bright, very low-content thumbnail metrics; review as possible blank page or missed scan.",
            profile,
        )
    if brightness < profile.dark_mean_threshold:
        _append_finding(
            item,
            findings,
            "quality_too_dark",
            "P1",
            f"Mean grayscale brightness {brightness} is below conservative threshold {profile.dark_mean_threshold}.",
            profile,
        )
    if brightness > profile.bright_mean_threshold and contrast < profile.low_contrast_stddev_threshold:
        _append_finding(
            item,
            findings,
            "quality_too_bright",
            "P1",
            f"Mean grayscale brightness {brightness} is above conservative threshold {profile.bright_mean_threshold} with very low contrast.",
            profile,
        )
    if contrast < profile.low_contrast_stddev_threshold:
        _append_finding(
            item,
            findings,
            "quality_low_contrast",
            "P2",
            f"Grayscale standard deviation {contrast} is below conservative threshold {profile.low_contrast_stddev_threshold}.",
            profile,
        )
    if contrast >= profile.blur_min_contrast_stddev and sharpness < profile.blur_laplacian_variance_threshold:
        _append_finding(
            item,
            findings,
            "quality_suspected_blur",
            "P2",
            f"Laplacian variance sharpness {sharpness} is below conservative threshold {profile.blur_laplacian_variance_threshold}.",
            profile,
        )


def _add_duplicate_name_findings(
    files: list[dict[str, Any]],
    findings: list[dict[str, str]],
    profile: RulesProfile,
) -> None:
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in files:
        parent = Path(item["relative_path"]).parent.as_posix()
        by_name.setdefault((parent, item["filename"].casefold()), []).append(item)
    for matches in by_name.values():
        if len(matches) > 1:
            paths = ", ".join(match["relative_path"] for match in matches)
            for item in matches:
                _append_finding(
                    item,
                    findings,
                    "duplicate_name",
                    "P0",
                    f"Filename is not unique within its directory: {paths}",
                    profile,
                )


def _add_duplicate_hash_findings(
    files: list[dict[str, Any]],
    findings: list[dict[str, str]],
    profile: RulesProfile,
) -> None:
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        by_hash.setdefault(item["sha256"], []).append(item)
    for matches in by_hash.values():
        if len(matches) > 1:
            paths = ", ".join(match["relative_path"] for match in matches)
            for item in matches:
                _append_finding(item, findings, "duplicate_file", "P0", f"SHA-256 duplicate detected: {paths}", profile)


def _add_batch_consistency_findings(
    files: list[dict[str, Any]],
    findings: list[dict[str, str]],
    profile: RulesProfile,
) -> None:
    open_files = [item for item in files if item["openable"]]
    for key, rule in (("format", "batch_format_consistency"), ("color_mode", "batch_color_mode_consistency")):
        values = sorted({str(item[key]) for item in open_files if item[key] is not None})
        if len(values) > 1:
            message = f"Openable files in batch use multiple {key} values: {', '.join(values)}"
            for item in open_files:
                _append_finding(item, findings, rule, "P2", message, profile)

    dpi_values = sorted({(item["dpi_x"], item["dpi_y"]) for item in open_files if item["dpi_x"] and item["dpi_y"]})
    if len(dpi_values) > 1:
        values = ", ".join(f"{x}x{y}" for x, y in dpi_values)
        for item in open_files:
            _append_finding(
                item,
                findings,
                "batch_dpi_consistency",
                "P2",
                f"Openable files in batch use multiple DPI values: {values}",
                profile,
            )

    orientation_values = sorted(
        {item["orientation_class"] for item in open_files if item["orientation_class"] in {"portrait", "landscape"}}
    )
    if len(orientation_values) > 1:
        oriented_files = [item for item in open_files if item["orientation_class"] in {"portrait", "landscape"}]
        portrait_count = sum(1 for item in oriented_files if item["orientation_class"] == "portrait")
        landscape_count = sum(1 for item in oriented_files if item["orientation_class"] == "landscape")
        minority_count = min(portrait_count, landscape_count)
        oriented_count = len(oriented_files)
        if portrait_count >= 2 and landscape_count >= 2 and minority_count / oriented_count >= 0.2:
            message = (
                "Openable non-square files mix portrait and landscape orientation classes: "
                f"portrait={portrait_count}, landscape={landscape_count}. Review for rotated pages or mixed attachments."
            )
            for item in oriented_files:
                _append_finding(item, findings, "batch_orientation_consistency", "P2", message, profile)


def _append_finding(
    item: dict[str, Any],
    findings: list[dict[str, str]],
    rule: str,
    severity: str,
    message: str,
    profile: RulesProfile,
) -> None:
    if not _rule_enabled(profile, rule):
        return
    findings.append(_finding(item, rule, _effective_severity(profile, rule, severity), message))


def _append_path_finding(
    findings: list[dict[str, str]],
    relative_path: str,
    rule: str,
    severity: str,
    message: str,
    profile: RulesProfile,
) -> None:
    if not _rule_enabled(profile, rule):
        return
    findings.append(_path_finding(relative_path, rule, _effective_severity(profile, rule, severity), message))


def _rule_enabled(profile: RulesProfile, rule: str) -> bool:
    if rule in PROTECTED_P0_RULES:
        return True
    return profile.is_rule_enabled(rule)


def _effective_severity(profile: RulesProfile, rule: str, default: str) -> str:
    severity = profile.severity_for(rule, default)
    if rule in PROTECTED_P0_RULES and default == "P0":
        return "P0"
    return severity


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
        "blank_page_findings": sum(1 for finding in findings if finding["rule"] == "quality_near_blank_page"),
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
    workers: dict[str, Any],
) -> dict[str, Any]:
    elapsed_seconds = max(0.0, round(elapsed_seconds, 6))
    return {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "total_files": total_files,
        "openable_files": openable_files,
        **workers,
        "files_per_minute": _files_per_minute(total_files, elapsed_seconds),
        "openable_files_per_minute": _files_per_minute(openable_files, elapsed_seconds),
    }


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)
