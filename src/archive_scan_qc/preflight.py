"""Privacy-safe preflight checks for production batch runs."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .concurrency import resolve_worker_count, worker_metadata
from .rules import RulesProfile, default_rules_profile
from .scanner import _iter_candidate_files


PROCESSING_FLAGS = ("auto_crop", "deskew", "trim_dark_border", "despeckle", "resume_processing")


@dataclass(frozen=True)
class PreflightConfig:
    project_id: str
    batch_id: str
    input_dir: Path
    output_dir: Path
    process_out: Path | None = None
    manifest_csv: Path | None = None
    rules_profile: RulesProfile | None = None
    rules_profile_error: str | None = None
    rules_profile_provided: bool = False
    workers: int | None = None
    auto_crop: bool = False
    deskew: bool = False
    trim_dark_border: bool = False
    despeckle: bool = False
    resume_processing: bool = False


@dataclass(frozen=True)
class _ManifestValidation:
    used: bool
    readable: bool
    has_relative_path_column: bool
    entry_count: int
    unique_entry_count: int
    empty_path_count: int
    absolute_path_count: int
    parent_escape_count: int
    duplicate_count: int
    missing_count: int
    unexpected_count: int


def run_preflight(config: PreflightConfig) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    input_exists = config.input_dir.exists()
    input_is_dir = config.input_dir.is_dir() if input_exists else False
    output_exists = config.output_dir.exists()
    output_is_dir = config.output_dir.is_dir() if output_exists else None
    process_exists = config.process_out.exists() if config.process_out else False
    process_is_dir = config.process_out.is_dir() if config.process_out and process_exists else None
    processing_enabled = any(getattr(config, flag) for flag in PROCESSING_FLAGS)

    if not input_exists:
        _add(errors, "input_missing", "Input directory does not exist.")
    elif not input_is_dir:
        _add(errors, "input_not_directory", "Input path is not a directory.")

    if output_exists and not output_is_dir:
        _add(errors, "output_not_directory", "Output path exists but is not a directory.")
    if input_exists and input_is_dir:
        input_resolved = config.input_dir.resolve()
        output_resolved = config.output_dir.resolve()
        if output_resolved == input_resolved:
            _add(errors, "output_same_as_input", "Output directory must not be the input directory.")
        elif _is_relative_to(output_resolved, input_resolved):
            _add(warnings, "output_inside_input", "Output directory is inside input; scan will skip that subtree.")
        if config.process_out:
            process_resolved = config.process_out.resolve()
            if process_resolved == input_resolved:
                _add(errors, "process_output_same_as_input", "Processing output must not be the input directory.")
            elif _is_relative_to(process_resolved, input_resolved):
                _add(
                    warnings,
                    "process_output_inside_input",
                    "Processing output is inside input; keep outputs separate for production.",
                )

    if config.process_out and process_exists and not process_is_dir:
        _add(errors, "process_output_not_directory", "Processing output path exists but is not a directory.")
    if processing_enabled and not config.process_out:
        _add(errors, "process_output_required", "Processing flags require --process-out.")

    candidate_count = 0
    skipped = {"hidden_directories": 0, "output_directories": 0, "hidden_files": 0, "manifest_files": 0}
    if input_exists and input_is_dir:
        candidate_files, skip_stats = _iter_candidate_files(config.input_dir.resolve(), config.output_dir, config.manifest_csv)
        candidate_count = len(candidate_files)
        skipped = {
            "hidden_directories": skip_stats.hidden_directories,
            "output_directories": skip_stats.output_directories,
            "hidden_files": skip_stats.hidden_files,
            "manifest_files": skip_stats.manifest_files,
        }
    else:
        candidate_files = []

    worker_error = None
    effective_workers = None
    worker_info: dict[str, Any] | None = None
    try:
        effective_workers = resolve_worker_count(config.workers, candidate_count)
        worker_info = worker_metadata(config.workers, effective_workers)
    except ValueError as exc:
        worker_error = str(exc)
        _add(errors, "workers_invalid", str(exc))

    if config.rules_profile_error:
        _add(errors, "rules_profile_invalid", config.rules_profile_error)

    profile = config.rules_profile or default_rules_profile()
    input_paths = {path.relative_to(config.input_dir.resolve()).as_posix() for path in candidate_files}
    manifest = _validate_manifest(config.manifest_csv, input_paths)
    if manifest.used:
        if not manifest.readable:
            _add(errors, "manifest_unreadable", "Manifest CSV could not be read.")
        if not manifest.has_relative_path_column:
            _add(errors, "manifest_missing_relative_path", "Manifest CSV must include a relative_path column.")
        if manifest.empty_path_count:
            _add(errors, "manifest_empty_paths", "Manifest CSV contains empty relative_path values.")
        if manifest.absolute_path_count:
            _add(errors, "manifest_absolute_paths", "Manifest CSV contains absolute paths.")
        if manifest.parent_escape_count:
            _add(errors, "manifest_parent_escape_paths", "Manifest CSV contains paths that escape the input root.")
        if manifest.duplicate_count:
            _add(errors, "manifest_duplicate_paths", "Manifest CSV contains duplicate relative_path values.")
        if manifest.missing_count:
            _add(errors, "manifest_missing_files", "Manifest references files that are missing from input.")
        if manifest.unexpected_count:
            _add(errors, "manifest_unexpected_files", "Input contains files that are not listed in the manifest.")

    status = "pass" if not errors else "fail"
    return {
        "schema_version": "scan-qc.preflight.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "project": {
            "project_id": config.project_id,
            "batch_id": config.batch_id,
        },
        "configuration": {
            "input": {"exists": input_exists, "is_directory": input_is_dir},
            "output": {"exists": output_exists, "is_directory": output_is_dir},
            "processing_output": {
                "provided": config.process_out is not None,
                "exists": process_exists if config.process_out else None,
                "is_directory": process_is_dir,
            },
            "processing_flags": {flag: getattr(config, flag) for flag in PROCESSING_FLAGS},
            "rules_profile": {
                "provided": config.rules_profile_provided or config.rules_profile is not None,
                "name": profile.name,
                "version": profile.version,
                "loaded": config.rules_profile_error is None,
            },
            "workers": {
                "valid": worker_error is None,
                "requested_workers": config.workers,
                "effective_workers": effective_workers,
                "metadata": worker_info,
            },
        },
        "input_summary": {
            "candidate_file_count": candidate_count,
            "skipped": skipped,
        },
        "manifest": {
            "used": manifest.used,
            "readable": manifest.readable,
            "has_relative_path_column": manifest.has_relative_path_column,
            "entry_count": manifest.entry_count,
            "unique_entry_count": manifest.unique_entry_count,
            "empty_path_count": manifest.empty_path_count,
            "absolute_path_count": manifest.absolute_path_count,
            "parent_escape_count": manifest.parent_escape_count,
            "duplicate_count": manifest.duplicate_count,
            "missing_count": manifest.missing_count,
            "unexpected_count": manifest.unexpected_count,
        },
        "errors": errors,
        "warnings": warnings,
        "privacy": {
            "contains_file_list": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "path_detail": "aggregate_counts_only",
        },
    }


def write_preflight_report(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "preflight_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _validate_manifest(manifest_csv: Path | None, input_paths: set[str]) -> _ManifestValidation:
    if manifest_csv is None:
        return _ManifestValidation(False, False, False, 0, 0, 0, 0, 0, 0, 0, 0)
    try:
        with manifest_csv.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "relative_path" not in reader.fieldnames:
                return _ManifestValidation(True, True, False, 0, 0, 0, 0, 0, 0, 0, len(input_paths))
            paths: list[str] = []
            empty = 0
            absolute = 0
            parent_escape = 0
            for row in reader:
                raw = row.get("relative_path", "")
                normalized = raw.strip().replace("\\", "/")
                if not normalized:
                    empty += 1
                    continue
                if _is_absolute_manifest_path(raw, normalized):
                    absolute += 1
                    continue
                parts = PurePosixPath(normalized).parts
                if ".." in parts:
                    parent_escape += 1
                    continue
                paths.append(normalized)
    except OSError:
        return _ManifestValidation(True, False, False, 0, 0, 0, 0, 0, 0, 0, len(input_paths))

    counts: dict[str, int] = {}
    for path in paths:
        counts[path] = counts.get(path, 0) + 1
    manifest_set = set(paths)
    return _ManifestValidation(
        used=True,
        readable=True,
        has_relative_path_column=True,
        entry_count=len(paths) + empty + absolute + parent_escape,
        unique_entry_count=len(manifest_set),
        empty_path_count=empty,
        absolute_path_count=absolute,
        parent_escape_count=parent_escape,
        duplicate_count=sum(1 for count in counts.values() if count > 1),
        missing_count=len(manifest_set - input_paths),
        unexpected_count=len(input_paths - manifest_set),
    )


def _is_absolute_manifest_path(raw: str, normalized: str) -> bool:
    stripped = raw.strip()
    return normalized.startswith("/") or Path(stripped).is_absolute() or PureWindowsPath(stripped).is_absolute()


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _add(items: list[dict[str, str]], code: str, message: str) -> None:
    items.append({"code": code, "message": message})
