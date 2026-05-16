"""Privacy-safe preflight checks for production batch runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .concurrency import resolve_worker_count, worker_metadata
from .manifest import manifest_summary, read_manifest
from .rules import RulesProfile, default_rules_profile
from .scanner import _iter_candidate_files


PROCESSING_FLAGS = (
    "auto_crop",
    "deskew",
    "trim_dark_border",
    "scanner_gutter_trim",
    "despeckle",
    "normalize_tones",
    "lighten_edge_shadow",
    "lighten_corner_shadows",
    "lighten_background_stains",
    "lighten_fold_shadows",
    "clean_bleed_through",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
    "resume_processing",
)


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
    scanner_gutter_trim: bool = False
    despeckle: bool = False
    normalize_tones: bool = False
    lighten_edge_shadow: bool = False
    lighten_corner_shadows: bool = False
    lighten_background_stains: bool = False
    lighten_fold_shadows: bool = False
    clean_bleed_through: bool = False
    lighten_scanlines: bool = False
    enhance_faded_text: bool = False
    sharpen_text_edges: bool = False
    resume_processing: bool = False


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
    file_order = [path.relative_to(config.input_dir.resolve()).as_posix() for path in candidate_files]
    manifest = read_manifest(config.manifest_csv, input_paths, file_order)
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
        if manifest.sequence_invalid_count:
            _add(errors, "manifest_invalid_sequence_values", "Manifest contains non-numeric or invalid sequence values.")
        if manifest.sequence_duplicate_count:
            _add(errors, "manifest_duplicate_sequence_values", "Manifest contains duplicate sequence values.")
        if manifest.sequence_gap_count:
            _add(errors, "manifest_sequence_gaps", "Manifest strict sequence mode has missing sequence values.")

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
        "manifest": manifest_summary(manifest),
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


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _add(items: list[dict[str, str]], code: str, message: str) -> None:
    items.append({"code": code, "message": message})
