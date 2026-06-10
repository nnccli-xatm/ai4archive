"""Service-style production job boundaries.

This module is the small backend core for future HTTP/API surfaces. It keeps
private path-bearing state in a per-job record and exposes a separate
public-safe aggregate summary for status polling or handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
from typing import Any
import uuid

from .production_runner import (
    PRODUCTION_RUN_PROGRESS_JSON,
    PRODUCTION_RUN_SUMMARY_JSON,
    ProductionRunConfig,
    run_production_folder,
)
from .processing_review import write_processing_review_package
from .processing_quality_summary import PROCESSING_QUALITY_SUMMARY_JSON
from .production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON, write_production_review_queue
from .review_decisions import REVIEW_DECISION_VERIFICATION_JSON, build_review_decision_verification_summary
from .scanner import SUPPORTED_EXTENSIONS
from .rules import (
    BUILTIN_RULE_TEMPLATE_IDS,
    CUSTOM_RULE_TEMPLATE_ID,
    RulesProfileError,
    attach_rule_template,
    builtin_rules_profile,
    processing_defaults_for_rule_template,
    rules_profile_from_mapping,
)
from .rule_templates import load_service_rule_template


SERVICE_JOB_SCHEMA_VERSION = "scan-qc.service-job.v1"
SERVICE_JOB_PUBLIC_SUMMARY_SCHEMA_VERSION = "scan-qc.service-job-public-summary.v1"
SERVICE_JOB_INDEX_PUBLIC_SUMMARY_SCHEMA_VERSION = "scan-qc.service-job-index-public-summary.v1"
SERVICE_JOB_INDEX_RECOVERY_ISSUES_SCHEMA_VERSION = "scan-qc.service-job-index-recovery-issues.v1"
SERVICE_JOB_PUBLIC_TIMINGS_SCHEMA_VERSION = "scan-qc.service-job-public-timings.v1"
SERVICE_JOB_SOURCE_INTEGRITY_SCHEMA_VERSION = "scan-qc.service-job-source-integrity.v1"
LOCAL_REVIEW_ARTIFACT_SCHEMA_VERSION = "scan-qc.service-job-local-review-artifact.v1"
LOCAL_PREVIEW_SCHEMA_VERSION = "scan-qc.service-job-local-preview.v1"
SERVICE_JOB_REVIEW_ACTIONS_SCHEMA_VERSION = "scan-qc.service-job-review-actions.v1"
SERVICE_JOB_REVIEW_HISTORY_SCHEMA_VERSION = "scan-qc.service-job-review-history.v1"
SERVICE_JOB_EVENT_LOG_SCHEMA_VERSION = "scan-qc.service-job-event-log.v1"
SERVICE_JOB_RECORD_JSON = "service_job.json"
SERVICE_JOB_PUBLIC_SUMMARY_JSON = "service_job_public_summary.json"
SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON = "service_job_index_public_summary.json"
SERVICE_JOB_REVIEW_DECISIONS_JSON = "scan-qc-review-decisions.summary.json"
SERVICE_JOB_REVIEW_HISTORY_JSON = "service_job_review_history.json"
SERVICE_JOB_EVENT_LOG_JSON = "service_job_event_log.json"
SERVICE_JOBS_DIRNAME = "jobs"
JOB_ID_PATTERN = re.compile(r"^job-[A-Za-z0-9][A-Za-z0-9_-]{2,80}$")
TERMINAL_STATES = {"finished", "needs_review", "failed", "interrupted", "cancelled"}
RETRYABLE_STATES = {"failed", "interrupted", "needs_recovery"}
SERVICE_JOB_MAX_WORKERS = 8
SERVICE_JOB_MAX_ACTIVE_JOBS = 2
SERVICE_JOB_MAX_ACTIVE_WORKERS = 8
SERVICE_JOB_MIN_FREE_SPACE_BYTES = 64 * 1024 * 1024
SERVICE_JOB_MAX_TMP_BYTES = 1024 * 1024 * 1024
LOCAL_REVIEW_ARTIFACT_IDS = {
    "processing-review-package": "processing_review_package",
    "production-review-queue": "production_review_queue",
}
PUBLIC_STAGE_TIMING_IDS = ("scan", "process", "summarize")
PUBLIC_AGGREGATE_PROCESSING_UNAVAILABLE_REASONS = (
    "missing_total_images",
    "missing_processed_images",
    "no_total_images",
    "no_processed_images",
    "no_elapsed_seconds",
)
PUBLIC_SOURCE_INTEGRITY_REASON_CODES = (
    "not_checked",
    "pre_run_snapshot_failed",
    "pre_run_snapshot_unavailable",
    "post_run_snapshot_failed",
)
PUBLIC_OPERATION_TIMING_IDS = (
    "auto_crop",
    "deskew",
    "trim_dark_border",
    "scanner_gutter_trim",
    "despeckle",
    "normalize_tones",
    "normalize_paper_color_cast",
    "lighten_edge_shadow",
    "lighten_corner_shadows",
    "lighten_background_stains",
    "lighten_fold_shadows",
    "level_illumination_gradient",
    "clean_bleed_through",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
)
PUBLIC_QUALITY_METRIC_IDS = (
    "brightness_delta",
    "contrast_delta",
    "crop_ratio",
    "deskew_abs_angle_degrees",
    "max_trim_margin_ratio",
    "scanner_gutter_max_trim_margin_ratio",
    "despeckle_pixel_ratio",
    "tone_background_delta",
    "tone_contrast_delta",
    "tone_changed_pixel_ratio",
    "paper_color_cast_delta",
    "paper_color_cast_brightness_delta",
    "paper_color_cast_changed_pixel_ratio",
    "edge_shadow_delta",
    "edge_shadow_changed_pixel_ratio",
    "corner_shadows_delta",
    "corner_shadows_changed_pixel_ratio",
    "background_stains_delta",
    "background_stains_changed_pixel_ratio",
    "fold_shadows_delta",
    "fold_shadows_changed_pixel_ratio",
    "illumination_gradient_correction_delta",
    "illumination_gradient_changed_pixel_ratio",
    "bleed_through_delta",
    "bleed_through_changed_pixel_ratio",
    "scanlines_delta",
    "scanlines_changed_pixel_ratio",
    "faded_text_delta",
    "faded_text_changed_pixel_ratio",
    "text_edges_delta",
    "text_edges_changed_pixel_ratio",
    "text_edges_edge_energy_before",
    "text_edges_edge_energy_after",
    "processed_output_brightness_increase",
    "processed_output_near_white_delta",
    "processed_output_highlight_clip_delta",
    "processed_output_dark_pixel_loss_ratio",
    "processed_output_dark_pixel_lift_ratio",
)
LOCAL_PREVIEW_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")
_ASYNC_JOB_LOCK = threading.Lock()
_ASYNC_JOB_KEYS: set[str] = set()
_ASYNC_JOB_WORKERS: dict[str, int] = {}


@dataclass(frozen=True)
class ServiceJobConfig:
    input_dir: Path
    service_root: Path
    project_id: str = "default-project"
    batch_id: str = "default-batch"
    rule_template: str = "dat-31-2017-standard"
    processing_mode: str = "standard"
    workers: int | None = 1


class ServiceJobNotFoundError(FileNotFoundError):
    """Raised when a requested service job checkpoint does not exist."""


def create_service_job(config: ServiceJobConfig, *, job_id: str | None = None) -> dict[str, Any]:
    """Create an isolated service job record and public-safe summary."""

    input_dir = config.input_dir.resolve()
    service_root = config.service_root.resolve()
    _validate_service_paths(input_dir, service_root)
    _validate_service_root_capacity(service_root)
    workers = _validate_worker_limit(config.workers)
    job_id = _validate_job_id(job_id or _new_job_id())
    job_root = (service_root / SERVICE_JOBS_DIRNAME / job_id).resolve()
    _require_within(job_root, service_root)
    if job_root.exists():
        raise FileExistsError(f"Service job already exists: {job_id}")

    stored_template = _stored_service_template(service_root, config.rule_template)
    profile = _rules_profile_for_template(config.rule_template, service_root, stored_template=stored_template)
    processing_defaults = _processing_options_for_job(
        config.rule_template,
        config.processing_mode,
        service_root,
        stored_template=stored_template,
    )
    template_metadata = profile.metadata().get("template")
    if stored_template is not None and isinstance(template_metadata, dict):
        template_metadata = {
            **template_metadata,
            "service_template_id": config.rule_template,
            "source": "service-managed-custom-template",
        }
    directories = _job_directories(job_root)
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=False)
    now = _utc_now()
    record = {
        "schema_version": SERVICE_JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "state": "created",
        "created_at": now,
        "updated_at": now,
        "privacy": _private_record_privacy(),
        "project": {
            "project_id": config.project_id,
            "batch_id": config.batch_id,
        },
        "paths": {
            "input_dir": str(input_dir),
            "service_root": str(service_root),
            "job_root": str(job_root),
            "metadata_dir": str(directories["metadata"]),
            "derivatives_dir": str(directories["derivatives"]),
            "tmp_dir": str(directories["tmp"]),
            "checkpoint_dir": str(directories["checkpoints"]),
            "review_dir": str(directories["review"]),
            "log_dir": str(directories["logs"]),
        },
        "isolation": _isolation_payload(service_root, job_root, directories),
        "template_snapshot": {
            "schema_version": "scan-qc.service-template-snapshot.v1",
            "service_template_id": config.rule_template,
            "rule_template": template_metadata,
            "processing_mode": config.processing_mode,
            "processing_defaults": processing_defaults,
            "custom_template_draft": stored_template.get("template") if isinstance(stored_template, dict) else None,
            "workers": workers,
        },
        "resource_limits": {
            "max_workers_per_job": SERVICE_JOB_MAX_WORKERS,
            "max_active_workers": SERVICE_JOB_MAX_ACTIVE_WORKERS,
            "min_free_space_bytes": SERVICE_JOB_MIN_FREE_SPACE_BYTES,
            "max_tmp_bytes_per_job": SERVICE_JOB_MAX_TMP_BYTES,
            "workers_requested": workers,
        },
        "production_artifacts": _production_artifact_paths(directories["metadata"], directories["derivatives"]),
        "recovery": {
            "status": "created",
            "resume_supported": True,
            "checkpoint_files": [SERVICE_JOB_RECORD_JSON, SERVICE_JOB_PUBLIC_SUMMARY_JSON],
        },
    }
    _append_service_job_event(record, "job_created", state="created", recovery_status="created")
    _write_job_record(job_root, record)
    return _write_public_summary(job_root, _public_summary_from_record(record))


def run_service_job(service_root: Path, job_id: str) -> dict[str, Any]:
    """Run a created service job synchronously and refresh its public summary."""

    _mark_service_job_running(service_root, job_id, recovery_status="running")
    return _execute_running_service_job(service_root, job_id, raise_errors=True)


def retry_service_job(service_root: Path, job_id: str) -> dict[str, Any]:
    """Explicitly retry a failed, interrupted, or recoverable service job."""

    _mark_service_job_retrying(service_root, job_id)
    return _execute_running_service_job(service_root, job_id, raise_errors=True)


def start_service_job_async(service_root: Path, job_id: str) -> dict[str, Any]:
    """Start a service job in a local background thread and return running state."""

    record = load_service_job_record(service_root, job_id)
    key = _reserve_async_job(record)
    try:
        summary = _mark_service_job_running(service_root, job_id, recovery_status="async_running")
        worker = threading.Thread(
            target=_async_service_job_worker,
            args=(service_root.resolve(), job_id),
            name=f"archive-scan-qc-{job_id}",
            daemon=True,
        )
        worker.start()
    except Exception:
        _unregister_async_job_key(key)
        raise
    return summary


def _mark_service_job_running(service_root: Path, job_id: str, *, recovery_status: str) -> dict[str, Any]:
    record = load_service_job_record(service_root, job_id)
    if record.get("state") == "running":
        raise RuntimeError("Service job is already running.")
    if str(record.get("state") or "") in TERMINAL_STATES:
        raise RuntimeError("Service job is already terminal.")
    _validate_job_tmp_quota(record)
    _update_record_state(record, "running", recovery_status=recovery_status)
    _write_job_record(_job_root_from_record(record), record)
    return _write_public_summary(_job_root_from_record(record), _public_summary_from_record(record))


def _mark_service_job_retrying(service_root: Path, job_id: str) -> dict[str, Any]:
    record = load_service_job_record(service_root, job_id)
    state = str(record.get("state") or "")
    if state not in RETRYABLE_STATES:
        raise RuntimeError("Service job is not in a retryable state.")
    _validate_job_tmp_quota(record)
    retry_count = (_safe_int(record.get("retry_count")) or 0) + 1
    record["retry_count"] = retry_count
    record["retry"] = {
        "status": "retrying",
        "attempt": retry_count,
        "resume_processing": True,
        "reuse_existing_derivatives": True,
        "started_at": _utc_now(),
    }
    _update_record_state(record, "running", recovery_status="retrying")
    _write_job_record(_job_root_from_record(record), record)
    return _write_public_summary(_job_root_from_record(record), _public_summary_from_record(record))


def _execute_running_service_job(service_root: Path, job_id: str, *, raise_errors: bool) -> dict[str, Any] | None:
    record = load_service_job_record(service_root, job_id)
    if record.get("state") != "running":
        return recover_service_job(service_root, job_id)
    source_snapshot = _capture_source_integrity_start(record)
    try:
        production_summary = run_production_folder(_production_config_from_record(record))
        latest = load_service_job_record(service_root, job_id)
        _refresh_source_integrity(latest, source_snapshot)
        _refresh_service_job_review_artifacts(latest, production_summary)
    except BaseException:
        latest = load_service_job_record(service_root, job_id)
        _refresh_source_integrity(latest, source_snapshot)
        if latest.get("state") == "cancelled":
            _write_public_summary(_job_root_from_record(latest), _public_summary_from_record(latest))
        else:
            refreshed = recover_service_job(service_root, job_id)
            if refreshed.get("state") not in {"failed", "interrupted"}:
                _update_record_state(latest, "failed", recovery_status="failed_without_terminal_summary")
                _write_job_record(_job_root_from_record(latest), latest)
                _write_public_summary(_job_root_from_record(latest), _public_summary_from_record(latest))
        if raise_errors:
            raise
        return None
    latest = load_service_job_record(service_root, job_id)
    if latest.get("state") == "cancelled":
        return _write_public_summary(_job_root_from_record(latest), _public_summary_from_record(latest))
    return recover_service_job(service_root, job_id)


def _async_service_job_worker(service_root: Path, job_id: str) -> None:
    try:
        _execute_running_service_job(service_root, job_id, raise_errors=False)
    finally:
        _unregister_async_job_key(_async_job_key_from_parts(service_root, job_id))


def cancel_service_job(service_root: Path, job_id: str) -> dict[str, Any]:
    """Mark a non-terminal service job as cancelled and refresh its public summary."""

    record = load_service_job_record(service_root, job_id)
    if str(record.get("state") or "") not in TERMINAL_STATES:
        _update_record_state(record, "cancelled", recovery_status="cancelled_by_service_request")
        _write_job_record(_job_root_from_record(record), record)
    return _write_public_summary(_job_root_from_record(record), _public_summary_from_record(record))


def recover_service_job(service_root: Path, job_id: str) -> dict[str, Any]:
    """Refresh a service job from its checkpoint/progress/summary files."""

    record = load_service_job_record(service_root, job_id)
    metadata_dir = Path(record["paths"]["metadata_dir"])
    production_summary = _read_json(metadata_dir / PRODUCTION_RUN_SUMMARY_JSON)
    production_progress = _read_json(metadata_dir / PRODUCTION_RUN_PROGRESS_JSON)
    state, recovery_status = _derive_recovered_state(record, production_summary, production_progress)
    if state in TERMINAL_STATES and isinstance(production_summary, dict) and not _local_review_is_available(record):
        _refresh_service_job_review_artifacts(record, production_summary)
    _update_record_state(record, state, recovery_status=recovery_status)
    _refresh_retry_status(record, state)
    _write_job_record(_job_root_from_record(record), record)
    return _write_public_summary(
        _job_root_from_record(record),
        _public_summary_from_record(record, production_summary=production_summary, production_progress=production_progress),
    )


def recover_service_jobs(service_root: Path) -> dict[str, Any]:
    """Recover every job under a service root and return an aggregate public summary."""

    root = service_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    jobs_dir = root / SERVICE_JOBS_DIRNAME
    summaries: list[dict[str, Any]] = []
    recovery_issue_codes: list[str] = []
    if jobs_dir.is_dir():
        for record_path in sorted(jobs_dir.glob(f"*/{SERVICE_JOB_RECORD_JSON}"), key=lambda path: path.parent.name):
            try:
                summaries.append(recover_service_job(root, record_path.parent.name))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                recovery_issue_codes.append(_recovery_issue_code(exc))
                continue
    state_counts: dict[str, int] = {}
    for summary in summaries:
        state = str(summary.get("state") or "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    index = {
        "schema_version": SERVICE_JOB_INDEX_PUBLIC_SUMMARY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "aggregate_only": True,
        "public_safe": True,
        "job_count": len(summaries),
        "skipped_job_count": len(recovery_issue_codes),
        "state_counts": state_counts,
        "jobs": summaries,
        "recovery_issues": _public_index_recovery_issues_payload(recovery_issue_codes),
        "privacy": _public_summary_privacy(),
    }
    (root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


def load_service_job_record(service_root: Path, job_id: str) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    root = service_root.resolve()
    job_root = (root / SERVICE_JOBS_DIRNAME / job_id).resolve()
    _require_within(job_root, root)
    record_path = job_root / SERVICE_JOB_RECORD_JSON
    if not record_path.is_file():
        raise ServiceJobNotFoundError("Service job does not exist.")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("schema_version") != SERVICE_JOB_SCHEMA_VERSION:
        raise ValueError("Unsupported service job schema.")
    if record.get("job_id") != job_id:
        raise ValueError("Service job id mismatch.")
    _validate_loaded_record_paths(record, root, job_root)
    return record


def read_service_job_local_review_artifact(service_root: Path, job_id: str, artifact_id: str) -> dict[str, Any]:
    """Read a path-bearing local review artifact through a fixed local-only allowlist."""

    artifact_key = LOCAL_REVIEW_ARTIFACT_IDS.get(artifact_id)
    if artifact_key is None:
        raise ValueError("Unsupported local review artifact.")

    recover_service_job(service_root, job_id)
    record = load_service_job_record(service_root, job_id)
    local_review = record.get("local_review") if isinstance(record.get("local_review"), dict) else {}
    artifacts = local_review.get("artifacts") if isinstance(local_review.get("artifacts"), dict) else {}
    artifact_value = artifacts.get(artifact_key) if isinstance(artifacts, dict) else None
    if local_review.get("provided") is not True or not artifact_value:
        raise RuntimeError("Local review artifact is not available.")

    artifact_path = Path(str(artifact_value)).resolve()
    job_root = _job_root_from_record(record)
    review_dir = _service_job_review_dir(record)
    _require_within(artifact_path, job_root)
    _require_within(artifact_path, review_dir)
    payload = _read_json(artifact_path)
    if not isinstance(payload, dict):
        raise RuntimeError("Local review artifact is not readable.")
    return {
        "schema_version": LOCAL_REVIEW_ARTIFACT_SCHEMA_VERSION,
        "job_id": str(record.get("job_id") or job_id),
        "artifact_id": artifact_id,
        "artifact_schema_version": payload.get("schema_version"),
        "local_only": True,
        "sensitive": True,
        "public_safe": False,
        "payload": payload,
        "privacy": {
            "local_only": True,
            "public_safe": False,
            "contains_paths": True,
            "contains_filenames": True,
            "contains_hashes": True,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
        },
    }


def resolve_service_job_local_preview(
    service_root: Path,
    job_id: str,
    local_id: str,
    requested_source: str | None = None,
) -> dict[str, Any]:
    """Resolve a local-only preview image path through a queue local_id allowlist."""

    safe_id = str(local_id or "").strip()
    if not safe_id:
        raise ValueError("Local preview request requires a local_id.")
    source_filter = str(requested_source or "").strip()
    if source_filter and source_filter not in {"original", "processed"}:
        raise ValueError("Local preview source must be original or processed.")

    recover_service_job(service_root, job_id)
    record = load_service_job_record(service_root, job_id)
    local_review = record.get("local_review") if isinstance(record.get("local_review"), dict) else {}
    artifacts = local_review.get("artifacts") if isinstance(local_review.get("artifacts"), dict) else {}
    queue_value = artifacts.get("production_review_queue") if isinstance(artifacts, dict) else None
    if local_review.get("provided") is not True or not queue_value:
        raise ValueError("Local preview queue is not available.")
    queue_path = Path(str(queue_value)).resolve()
    job_root = _job_root_from_record(record)
    review_dir = _service_job_review_dir(record)
    _require_within(queue_path, job_root)
    _require_within(queue_path, review_dir)
    queue = _read_json(queue_path)
    items = queue.get("items") if isinstance(queue, dict) else None
    if not isinstance(items, list):
        raise ValueError("Local preview queue has no items.")
    item = next((entry for entry in items if isinstance(entry, dict) and entry.get("local_id") == safe_id), None)
    if not isinstance(item, dict):
        raise ValueError("Local preview item was not found.")

    relative_path = _safe_preview_relative_path(str(item.get("relative_path") or ""))
    input_dir = Path(str(record["paths"]["input_dir"])).resolve()
    derivatives_dir = Path(str(record["paths"]["derivatives_dir"])).resolve()
    for candidate, source in _preview_candidates(input_dir, derivatives_dir, relative_path):
        if source_filter and source != source_filter:
            continue
        resolved = candidate.resolve()
        if _valid_preview_path(resolved, input_dir, derivatives_dir):
            return {
                "schema_version": LOCAL_PREVIEW_SCHEMA_VERSION,
                "job_id": str(record.get("job_id") or job_id),
                "local_id": safe_id,
                "source": "original_fallback" if not source_filter and source == "original" else source,
                "path": resolved,
                "local_only": True,
                "sensitive": True,
                "public_safe": False,
                "privacy": {
                    "local_only": True,
                    "public_safe": False,
                    "contains_image_content": True,
                    "contains_paths": False,
                    "contains_filenames": False,
                    "path_returned_to_http_client": False,
                },
            }
    raise ValueError("Local preview image is not available.")


def write_service_job_review_actions(
    service_root: Path,
    job_id: str,
    review_decisions: dict[str, Any],
) -> dict[str, Any]:
    """Persist local review decisions and return only aggregate verification status."""

    if not isinstance(review_decisions, dict):
        raise ValueError("Review actions require a review_decisions object.")
    recover_service_job(service_root, job_id)
    record = load_service_job_record(service_root, job_id)
    verification = build_review_decision_verification_summary(review_decisions)
    if verification.get("status") != "pass":
        raise ValueError("Review decisions failed public-safe verification.")

    job_root = _job_root_from_record(record)
    review_dir = _service_job_review_dir(record)
    _require_within(review_dir, job_root)
    review_dir.mkdir(parents=True, exist_ok=True)
    updated_at = _utc_now()
    decisions_path = review_dir / SERVICE_JOB_REVIEW_DECISIONS_JSON
    verification_path = review_dir / REVIEW_DECISION_VERIFICATION_JSON
    decisions_path.write_text(
        json.dumps(review_decisions, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verification_path.write_text(
        json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    history_path, history_summary = _append_service_job_review_history(
        review_dir=review_dir,
        job_root=job_root,
        record=record,
        review_decisions=review_decisions,
        verification=verification,
        recorded_at=updated_at,
    )
    decision_summary = verification.get("decision_summary") if isinstance(verification.get("decision_summary"), dict) else {}
    record["review_actions"] = {
        "schema_version": SERVICE_JOB_REVIEW_ACTIONS_SCHEMA_VERSION,
        "provided": True,
        "updated_at": updated_at,
        "artifacts": {
            "review_decisions": str(decisions_path),
            "review_decision_verification": str(verification_path),
            "review_history": str(history_path),
        },
        "verification": _public_review_action_verification(verification),
        "history": history_summary,
    }
    _append_service_job_event(
        record,
        "review_actions_recorded",
        state=str(record.get("state") or "unknown"),
        recovery_status=str((record.get("recovery") or {}).get("status") or "unknown"),
    )
    _write_job_record(job_root, record)
    return {
        "schema_version": SERVICE_JOB_REVIEW_ACTIONS_SCHEMA_VERSION,
        "generated_at": updated_at,
        "job_id": str(record.get("job_id") or job_id),
        "saved": True,
        "review_decisions_written": True,
        "verification_summary_written": True,
        "review_history_written": True,
        "decision_summary": _public_decision_summary(decision_summary),
        "verification": _public_review_action_verification(verification),
        "history": history_summary,
        "storage": {
            "managed_by_service": True,
            "local_only_payload_written": True,
            "path_returned": False,
        },
        "privacy": _public_summary_privacy(),
    }


def read_service_job_review_history_summary(service_root: Path, job_id: str) -> dict[str, Any]:
    """Return only the public-safe aggregate review history for a service job."""

    summary = recover_service_job(service_root, job_id)
    record = load_service_job_record(service_root, job_id)
    job_root = _job_root_from_record(record)
    review_dir = _service_job_review_dir(record)
    _require_within(review_dir, job_root)
    history_path = (review_dir / SERVICE_JOB_REVIEW_HISTORY_JSON).resolve()
    _require_within(history_path, job_root)
    history = None
    if history_path.is_file():
        history = _read_json(history_path)
    if history is None:
        review_actions = record.get("review_actions") if isinstance(record.get("review_actions"), dict) else {}
        history = review_actions.get("history")
    public_history = _public_review_history_payload(history)
    return {
        "schema_version": SERVICE_JOB_REVIEW_HISTORY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "job_id": str(record.get("job_id") or job_id),
        "job_state": str(summary.get("state") or record.get("state") or "unknown"),
        "provided": bool(public_history.get("provided")),
        "status": str(public_history.get("status") or "unknown"),
        "review_history": public_history,
        "storage": {
            "managed_by_service": True,
            "local_only_payload_written": bool(public_history.get("local_only_payload_written")),
            "path_returned": False,
        },
        "privacy": _public_summary_privacy(),
    }


def _validate_service_paths(input_dir: Path, service_root: Path) -> None:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Service job input directory does not exist: {input_dir}")
    if _paths_overlap(input_dir, service_root):
        raise ValueError("Service root and input directory must not overlap.")
    service_root.mkdir(parents=True, exist_ok=True)
    jobs_dir = service_root / SERVICE_JOBS_DIRNAME
    jobs_dir.mkdir(parents=True, exist_ok=True)


def _validate_service_root_capacity(service_root: Path) -> None:
    try:
        free_bytes = shutil.disk_usage(service_root).free
    except OSError as exc:
        raise RuntimeError("Service root free space could not be checked.") from exc
    if free_bytes < SERVICE_JOB_MIN_FREE_SPACE_BYTES:
        raise RuntimeError("Service root free space is below the configured minimum.")


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _validate_job_id(job_id: str) -> str:
    if not JOB_ID_PATTERN.match(job_id):
        raise ValueError("Invalid service job id.")
    return job_id


def _validate_worker_limit(workers: int | None) -> int | None:
    if workers is None:
        return None
    if workers < 1:
        raise ValueError("Service job workers must be a positive integer.")
    if workers > SERVICE_JOB_MAX_WORKERS:
        raise ValueError(f"Service job workers exceed the per-job limit of {SERVICE_JOB_MAX_WORKERS}.")
    return workers


def _new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"job-{stamp}-{uuid.uuid4().hex[:10]}"


def _job_directories(job_root: Path) -> dict[str, Path]:
    return {
        "root": job_root,
        "metadata": job_root / "metadata",
        "derivatives": job_root / "derivatives",
        "tmp": job_root / "tmp",
        "checkpoints": job_root / "checkpoints",
        "review": job_root / "review",
        "logs": job_root / "logs",
    }


def _safe_preview_relative_path(value: str) -> Path:
    text = value.replace("\\", "/").strip()
    relative = Path(text)
    if not text or relative.is_absolute() or relative.drive:
        raise ValueError("Local preview relative path is invalid.")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Local preview relative path is invalid.")
    return relative


def _preview_candidates(input_dir: Path, derivatives_dir: Path, relative_path: Path) -> list[tuple[Path, str]]:
    return [
        (derivatives_dir / "images" / relative_path, "processed"),
        (derivatives_dir / relative_path, "processed"),
        (input_dir / relative_path, "original"),
    ]


def _valid_preview_path(candidate: Path, input_dir: Path, derivatives_dir: Path) -> bool:
    if candidate.suffix.lower() not in LOCAL_PREVIEW_IMAGE_SUFFIXES or not candidate.is_file():
        return False
    return _is_within(candidate, input_dir) or _is_within(candidate, derivatives_dir)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        _require_within(child, parent)
    except ValueError:
        return False
    return True


def _require_within(child: Path, parent: Path) -> None:
    child = child.resolve()
    parent = parent.resolve()
    if child != parent and parent not in child.parents:
        raise ValueError("Service job path escapes the service root.")


def _validate_loaded_record_paths(record: dict[str, Any], service_root: Path, job_root: Path) -> None:
    paths = record.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("Service job record paths are missing.")
    service_root = service_root.resolve()
    job_root = job_root.resolve()
    input_dir = Path(str(paths.get("input_dir", ""))).resolve()
    if not input_dir.is_dir():
        raise ValueError("Service job input directory is missing.")
    if _paths_overlap(input_dir, service_root):
        raise ValueError("Service job input directory overlaps the service root.")
    if Path(str(paths.get("service_root", ""))).resolve() != service_root:
        raise ValueError("Service job service root mismatch.")
    if Path(str(paths.get("job_root", ""))).resolve() != job_root:
        raise ValueError("Service job root mismatch.")
    for key in ("metadata_dir", "derivatives_dir", "tmp_dir", "checkpoint_dir", "log_dir"):
        _require_within(Path(str(paths.get(key, ""))).resolve(), job_root)
    if "review_dir" in paths:
        _require_within(Path(str(paths.get("review_dir", ""))).resolve(), job_root)


def _stored_service_template(service_root: Path, rule_template: str) -> dict[str, Any] | None:
    if rule_template in BUILTIN_RULE_TEMPLATE_IDS:
        return None
    return load_service_rule_template(service_root, rule_template)


def _rules_profile_for_template(
    rule_template: str,
    service_root: Path,
    *,
    stored_template: dict[str, Any] | None = None,
):
    if rule_template not in BUILTIN_RULE_TEMPLATE_IDS:
        stored = stored_template if isinstance(stored_template, dict) else load_service_rule_template(service_root, rule_template)
        template_draft = stored.get("template") if isinstance(stored.get("template"), dict) else {}
        return attach_rule_template(
            rules_profile_from_mapping(template_draft, source=f"service-managed-template:{rule_template}"),
            CUSTOM_RULE_TEMPLATE_ID,
        )
    return builtin_rules_profile(rule_template)


def _processing_options_for_job(
    rule_template: str,
    processing_mode: str,
    service_root: Path,
    *,
    stored_template: dict[str, Any] | None = None,
) -> dict[str, bool]:
    if rule_template in BUILTIN_RULE_TEMPLATE_IDS:
        defaults = processing_defaults_for_rule_template(rule_template)
    else:
        stored = stored_template if isinstance(stored_template, dict) else load_service_rule_template(service_root, rule_template)
        raw_defaults = stored.get("processing_defaults")
        defaults = {str(key): bool(value) for key, value in raw_defaults.items()} if isinstance(raw_defaults, dict) else {}
    if processing_mode == "standard":
        return defaults
    if processing_mode == "qc_only":
        return {key: False for key in defaults}
    if processing_mode == "light":
        return {
            key: bool(defaults.get(key) and key in {"auto_crop", "reuse_scan_measurements"})
            for key in defaults
        }
    raise ValueError("Unsupported service job processing mode.")


def _production_config_from_record(record: dict[str, Any]) -> ProductionRunConfig:
    template_snapshot = record["template_snapshot"]
    template = template_snapshot["rule_template"]["id"]
    defaults = dict(record["template_snapshot"]["processing_defaults"])
    if template == CUSTOM_RULE_TEMPLATE_ID:
        template_draft = template_snapshot.get("custom_template_draft")
        if not isinstance(template_draft, dict):
            raise RulesProfileError("Custom service job template snapshot is missing.")
        rules_profile = attach_rule_template(
            rules_profile_from_mapping(
                template_draft,
                source=f"service-job-template-snapshot:{template_snapshot.get('service_template_id') or 'custom'}",
            ),
            CUSTOM_RULE_TEMPLATE_ID,
        )
    else:
        rules_profile = builtin_rules_profile(template)
    return ProductionRunConfig(
        input_dir=Path(record["paths"]["input_dir"]),
        derivative_output_dir=Path(record["paths"]["derivatives_dir"]),
        metadata_output_dir=Path(record["paths"]["metadata_dir"]),
        project_id=record["project"]["project_id"],
        batch_id=record["project"]["batch_id"],
        rules_profile=rules_profile,
        processing_mode=record["template_snapshot"]["processing_mode"],
        workers=record["template_snapshot"].get("workers"),
        auto_crop=bool(defaults.get("auto_crop")),
        deskew=bool(defaults.get("deskew")),
        trim_dark_border=bool(defaults.get("trim_dark_border")),
        scanner_gutter_trim=bool(defaults.get("scanner_gutter_trim")),
        despeckle=bool(defaults.get("despeckle")),
        normalize_tones=bool(defaults.get("normalize_tones")),
        normalize_paper_color_cast=bool(defaults.get("normalize_paper_color_cast")),
        lighten_edge_shadow=bool(defaults.get("lighten_edge_shadow")),
        lighten_corner_shadows=bool(defaults.get("lighten_corner_shadows")),
        lighten_background_stains=bool(defaults.get("lighten_background_stains")),
        lighten_fold_shadows=bool(defaults.get("lighten_fold_shadows")),
        level_illumination_gradient=bool(defaults.get("level_illumination_gradient")),
        clean_bleed_through=bool(defaults.get("clean_bleed_through")),
        lighten_scanlines=bool(defaults.get("lighten_scanlines")),
        enhance_faded_text=bool(defaults.get("enhance_faded_text")),
        sharpen_text_edges=bool(defaults.get("sharpen_text_edges")),
        despeckle_content_type_check=bool(defaults.get("despeckle_content_type_check", True)),
        reuse_scan_measurements=bool(defaults.get("reuse_scan_measurements")),
    )


def _production_artifact_paths(metadata_dir: Path, derivatives_dir: Path) -> dict[str, str]:
    return {
        "summary": str(metadata_dir / PRODUCTION_RUN_SUMMARY_JSON),
        "progress": str(metadata_dir / PRODUCTION_RUN_PROGRESS_JSON),
        "processing_manifest": str(derivatives_dir / "processing_manifest.json"),
        "processing_retry_manifest": str(derivatives_dir / "processing_retry_manifest.json"),
        "processing_audit_summary": str(derivatives_dir / "processing_audit_summary.json"),
        "processing_quality_summary": str(derivatives_dir / PROCESSING_QUALITY_SUMMARY_JSON),
    }


def _derive_recovered_state(
    record: dict[str, Any],
    production_summary: dict[str, Any] | None,
    production_progress: dict[str, Any] | None,
) -> tuple[str, str]:
    if str(record.get("state") or "") == "cancelled":
        return "cancelled", "cancelled_by_service_request"
    if isinstance(production_summary, dict) and isinstance(production_summary.get("status"), str):
        status = str(production_summary["status"])
        if status in TERMINAL_STATES:
            return status, "terminal_summary_recovered"
    if isinstance(production_progress, dict) and isinstance(production_progress.get("state"), str):
        state = str(production_progress["state"])
        if state in TERMINAL_STATES:
            return state, "terminal_progress_recovered"
        if state == "running":
            if str(record.get("state") or "") == "running" and _async_job_is_active(record):
                return "running", "async_running"
            return "needs_recovery", "running_progress_requires_resume_after_service_restart"
    if str(record.get("state") or "") == "running":
        if _async_job_is_active(record):
            return "running", "async_running"
        return "needs_recovery", "running_record_requires_resume_after_service_restart"
    return str(record.get("state") or "created"), "job_record_recovered"


def _public_summary_from_record(
    record: dict[str, Any],
    *,
    production_summary: dict[str, Any] | None = None,
    production_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    counts = _public_counts(production_summary, production_progress)
    source_integrity = _public_source_integrity_payload(record.get("source_integrity"))
    return {
        "schema_version": SERVICE_JOB_PUBLIC_SUMMARY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "job_id": record["job_id"],
        "state": record["state"],
        "state_label_zh": _state_label_zh(str(record["state"])),
        "aggregate_only": True,
        "public_safe": True,
        "counts": counts,
        "template": {
            "rule_template_id": record["template_snapshot"].get("service_template_id")
            or record["template_snapshot"]["rule_template"]["id"],
            "base_rule_template_id": record["template_snapshot"]["rule_template"]["id"],
            "processing_mode": record["template_snapshot"]["processing_mode"],
        },
        "resource_limits": _public_resource_limits(record.get("resource_limits")),
        "isolation": {
            "job_root_isolated": bool(record["isolation"]["job_root_isolated"]),
            "metadata_isolated": bool(record["isolation"]["metadata_isolated"]),
            "derivatives_isolated": bool(record["isolation"]["derivatives_isolated"]),
            "tmp_isolated": bool(record["isolation"]["tmp_isolated"]),
            "checkpoint_isolated": bool(record["isolation"]["checkpoint_isolated"]),
            "review_isolated": bool(record["isolation"].get("review_isolated")),
            "log_isolated": bool(record["isolation"].get("log_isolated")),
        },
        "recovery": _public_recovery_payload(record.get("recovery")),
        "retry": _public_retry_payload(record),
        "events": _public_event_log_payload(record.get("event_log")),
        "quality": _public_quality_payload(production_summary),
        "timings": _public_timings_payload(production_summary, production_progress),
        "source_integrity": source_integrity,
        "local_review": _public_local_review_payload(record.get("local_review")),
        "review_actions": _public_review_actions_payload(record.get("review_actions")),
        "source_images_modified": bool(source_integrity.get("source_images_modified")),
        "network_services_called": False,
        "private_paths_exposed": False,
        "privacy": _public_summary_privacy(),
    }


def _public_counts(
    production_summary: dict[str, Any] | None,
    production_progress: dict[str, Any] | None,
) -> dict[str, int | None]:
    summary_counts = production_summary.get("counts", {}) if isinstance(production_summary, dict) else {}
    progress = production_progress.get("aggregate_processing", {}) if isinstance(production_progress, dict) else {}
    return {
        "total_files": _safe_int(summary_counts.get("total_files", progress.get("total_images"))),
        "processed_files": _safe_int(summary_counts.get("processed_files", progress.get("processed_images"))),
        "resumed_files": _safe_int(summary_counts.get("resumed_files")),
        "reused_files": _safe_int(summary_counts.get("reused_files")),
        "reprocessed_files": _safe_int(summary_counts.get("reprocessed_files")),
        "failed_files": _safe_int(summary_counts.get("failed_files")),
        "retry_list_files": _safe_int(summary_counts.get("retry_list_files")),
        "remaining_files": _safe_int(summary_counts.get("remaining_files", progress.get("remaining_images"))),
        "p0_findings": _safe_int(summary_counts.get("p0_findings")),
        "p1_findings": _safe_int(summary_counts.get("p1_findings")),
        "p2_findings": _safe_int(summary_counts.get("p2_findings")),
    }


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, round(float(value), 6))
    except (TypeError, ValueError):
        return None


def _public_decision_summary(decision_summary: Any) -> dict[str, Any]:
    summary = decision_summary if isinstance(decision_summary, dict) else {}
    closure = summary.get("closure_gate_summary") if isinstance(summary.get("closure_gate_summary"), dict) else {}
    return {
        "total_decisions": _safe_int(summary.get("total_decisions")) or 0,
        "pending": _safe_int(summary.get("pending")) or 0,
        "accepted": _safe_int(summary.get("accepted")) or 0,
        "rejected": _safe_int(summary.get("rejected")) or 0,
        "rework": _safe_int(summary.get("rework")) or 0,
        "completion_status": str(summary.get("completion_status") or "unknown"),
        "decision_counts": _int_dict(summary.get("decision_counts")),
        "closure_gate_summary": {
            "open_p0_count": _safe_int(closure.get("open_p0_count")) or 0,
            "open_p1_count": _safe_int(closure.get("open_p1_count")) or 0,
            "manually_handled_count": _safe_int(closure.get("manually_handled_count")) or 0,
            "can_complete_delivery": bool(closure.get("can_complete_delivery")),
        },
    }


def _public_review_action_verification(verification: Any) -> dict[str, Any]:
    payload = verification if isinstance(verification, dict) else {}
    privacy = payload.get("privacy") if isinstance(payload.get("privacy"), dict) else {}
    return {
        "status": str(payload.get("status") or "unknown"),
        "blocking_count": _safe_int(payload.get("blocking_count")) or 0,
        "warning_count": _safe_int(payload.get("warning_count")) or 0,
        "blocking_counts_by_code": _int_dict(payload.get("blocking_counts_by_code")),
        "warning_counts_by_code": _int_dict(payload.get("warning_counts_by_code")),
        "privacy": {
            "status": str(privacy.get("status") or "unknown"),
            "aggregate_only": bool(privacy.get("aggregate_only")),
            "sensitive_field_count": _safe_int(privacy.get("sensitive_field_count")) or 0,
            "source_values_omitted": True,
        },
    }


def _append_service_job_review_history(
    *,
    review_dir: Path,
    job_root: Path,
    record: dict[str, Any],
    review_decisions: dict[str, Any],
    verification: dict[str, Any],
    recorded_at: str,
) -> tuple[Path, dict[str, Any]]:
    history_path = (review_dir / SERVICE_JOB_REVIEW_HISTORY_JSON).resolve()
    _require_within(history_path, job_root)
    previous = _read_json(history_path) if history_path.is_file() else {}
    previous_entries = previous.get("entries") if isinstance(previous, dict) else None
    entries = list(previous_entries) if isinstance(previous_entries, list) else []
    entry = {
        "entry_id": f"review-action-{len(entries) + 1:06d}",
        "recorded_at": recorded_at,
        "job_id": str(record.get("job_id") or ""),
        "review_decisions": review_decisions,
        "verification": verification,
    }
    entries.append(entry)
    history = {
        "schema_version": SERVICE_JOB_REVIEW_HISTORY_SCHEMA_VERSION,
        "job_id": str(record.get("job_id") or ""),
        "updated_at": recorded_at,
        "entry_count": len(entries),
        "local_only": True,
        "sensitive": True,
        "public_safe": False,
        "entries": entries,
        "privacy": {
            "local_only": True,
            "public_safe": False,
            "contains_review_rows": True,
            "contains_local_ids": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "path_returned_to_http_client": False,
        },
    }
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return history_path, _public_review_history_payload(history)


def _public_review_actions_payload(review_actions: Any) -> dict[str, Any]:
    payload = review_actions if isinstance(review_actions, dict) else {}
    provided = payload.get("provided") is True
    return {
        "schema_version": SERVICE_JOB_REVIEW_ACTIONS_SCHEMA_VERSION,
        "provided": provided,
        "status": "available" if provided else "not_available",
        "updated": bool(payload.get("updated_at")) if provided else False,
        "verification": _public_review_action_verification(payload.get("verification")),
        "history": _public_review_history_payload(payload.get("history")),
        "storage": {
            "managed_by_service": provided,
            "local_only_payload_written": provided,
            "path_returned": False,
        },
        "privacy": _public_summary_privacy(),
    }


def _public_review_history_payload(history: Any) -> dict[str, Any]:
    payload = history if isinstance(history, dict) else {}
    if "latest_verification_status" in payload:
        entry_count = _safe_int(payload.get("entry_count")) or 0
        return {
            "schema_version": SERVICE_JOB_REVIEW_HISTORY_SCHEMA_VERSION,
            "provided": entry_count > 0,
            "status": "available" if entry_count > 0 else "not_available",
            "entry_count": entry_count,
            "latest_verification_status": str(payload.get("latest_verification_status") or "unknown"),
            "latest_completion_status": str(payload.get("latest_completion_status") or "unknown"),
            "latest_decision_summary": _public_decision_summary(payload.get("latest_decision_summary")),
            "local_only_payload_written": bool(payload.get("local_only_payload_written")),
            "path_returned": False,
            "privacy": {
                "public_safe": True,
                "aggregate_only": True,
                "contains_review_rows": False,
                "contains_local_ids": False,
                "contains_paths": False,
                "contains_filenames": False,
                "contains_hashes": False,
                "contains_thumbnails": False,
                "contains_ocr_text": False,
                "contains_image_content": False,
            },
        }
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    latest = entries[-1] if entries and isinstance(entries[-1], dict) else {}
    latest_verification = latest.get("verification") if isinstance(latest.get("verification"), dict) else {}
    latest_summary = (
        latest_verification.get("decision_summary")
        if isinstance(latest_verification.get("decision_summary"), dict)
        else {}
    )
    entry_count = _safe_int(payload.get("entry_count")) or len(entries)
    return {
        "schema_version": SERVICE_JOB_REVIEW_HISTORY_SCHEMA_VERSION,
        "provided": entry_count > 0,
        "status": "available" if entry_count > 0 else "not_available",
        "entry_count": entry_count,
        "latest_verification_status": str(latest_verification.get("status") or "unknown"),
        "latest_completion_status": str(latest_summary.get("completion_status") or "unknown"),
        "latest_decision_summary": _public_decision_summary(latest_summary),
        "local_only_payload_written": entry_count > 0,
        "path_returned": False,
        "privacy": {
            "public_safe": True,
            "aggregate_only": True,
            "contains_review_rows": False,
            "contains_local_ids": False,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
        },
    }


def _append_service_job_event(
    record: dict[str, Any],
    event_type: str,
    *,
    state: str,
    recovery_status: str,
) -> dict[str, Any]:
    job_root = _job_root_from_record(record)
    log_dir = Path(str(record["paths"]["log_dir"])).resolve()
    _require_within(log_dir, job_root)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / SERVICE_JOB_EVENT_LOG_JSON).resolve()
    _require_within(log_path, job_root)
    previous = _read_json(log_path) if log_path.is_file() else {}
    previous_events = previous.get("events") if isinstance(previous, dict) else None
    events = list(previous_events) if isinstance(previous_events, list) else []
    recorded_at = _utc_now()
    event = {
        "event_id": f"service-event-{len(events) + 1:06d}",
        "recorded_at": recorded_at,
        "event_type": str(event_type),
        "state": str(state),
        "recovery_status": str(recovery_status),
    }
    events.append(event)
    event_log = {
        "schema_version": SERVICE_JOB_EVENT_LOG_SCHEMA_VERSION,
        "job_id": str(record.get("job_id") or ""),
        "updated_at": recorded_at,
        "event_count": len(events),
        "local_only": True,
        "sensitive": True,
        "public_safe": False,
        "events": events,
        "privacy": {
            "local_only": True,
            "public_safe": False,
            "contains_event_rows": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "path_returned_to_http_client": False,
        },
    }
    log_path.write_text(
        json.dumps(event_log, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    record["event_log"] = {
        "schema_version": SERVICE_JOB_EVENT_LOG_SCHEMA_VERSION,
        "provided": True,
        "updated_at": recorded_at,
        "event_count": len(events),
        "latest_event_type": str(event_type),
        "latest_state": str(state),
        "latest_recovery_status": str(recovery_status),
        "artifacts": {"event_log": str(log_path)},
    }
    return record["event_log"]


def _public_event_log_payload(event_log: Any) -> dict[str, Any]:
    payload = event_log if isinstance(event_log, dict) else {}
    provided = payload.get("provided") is True
    return {
        "schema_version": SERVICE_JOB_EVENT_LOG_SCHEMA_VERSION,
        "provided": provided,
        "status": "available" if provided else "not_available",
        "event_count": _safe_int(payload.get("event_count")) or 0,
        "latest_event_type": str(payload.get("latest_event_type") or "unknown"),
        "latest_state": str(payload.get("latest_state") or "unknown"),
        "latest_recovery_status": str(payload.get("latest_recovery_status") or "unknown"),
        "local_only_payload_written": provided,
        "path_returned": False,
        "privacy": {
            "public_safe": True,
            "aggregate_only": True,
            "contains_event_rows": False,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
        },
    }


def _recovery_issue_code(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_checkpoint_json"
    if isinstance(exc, OSError):
        return "checkpoint_unreadable"
    return "invalid_checkpoint"


def _public_index_recovery_issues_payload(issue_codes: list[str]) -> dict[str, Any]:
    by_code: dict[str, int] = {}
    for code in issue_codes:
        safe_code = str(code or "unknown_recovery_issue")
        by_code[safe_code] = by_code.get(safe_code, 0) + 1
    issue_count = len(issue_codes)
    return {
        "schema_version": SERVICE_JOB_INDEX_RECOVERY_ISSUES_SCHEMA_VERSION,
        "provided": True,
        "status": "issues_found" if issue_count else "clear",
        "issue_count": issue_count,
        "skipped_job_count": issue_count,
        "by_code": by_code,
        "privacy": {
            "public_safe": True,
            "aggregate_only": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_job_ids": False,
            "contains_exception_messages": False,
            "contains_checkpoint_rows": False,
        },
    }


def _public_recovery_payload(recovery: Any) -> dict[str, Any]:
    recovery = recovery if isinstance(recovery, dict) else {}
    return {
        "status": str(recovery.get("status") or "unknown"),
        "resume_supported": bool(recovery.get("resume_supported")),
    }


def _public_retry_payload(record: dict[str, Any]) -> dict[str, Any]:
    retry = record.get("retry") if isinstance(record.get("retry"), dict) else {}
    attempt = _safe_int(retry.get("attempt", record.get("retry_count"))) if isinstance(retry, dict) else None
    provided = bool(attempt)
    return {
        "provided": provided,
        "status": str(retry.get("status") or ("not_started" if not provided else "unknown")),
        "attempt": attempt or 0,
        "resume_processing": bool(retry.get("resume_processing")) if provided else False,
        "reuse_existing_derivatives": bool(retry.get("reuse_existing_derivatives")) if provided else False,
        "privacy": {
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_manifest_rows": False,
            "contains_retry_file_list": False,
        },
    }


def _public_resource_limits(resource_limits: Any) -> dict[str, int | None]:
    resource_limits = resource_limits if isinstance(resource_limits, dict) else {}
    return {
        "max_workers_per_job": _safe_int(resource_limits.get("max_workers_per_job")),
        "max_active_workers": (
            _safe_int(resource_limits.get("max_active_workers")) or SERVICE_JOB_MAX_ACTIVE_WORKERS
        ),
        "min_free_space_bytes": (
            _safe_int(resource_limits.get("min_free_space_bytes")) or SERVICE_JOB_MIN_FREE_SPACE_BYTES
        ),
        "max_tmp_bytes_per_job": (
            _safe_int(resource_limits.get("max_tmp_bytes_per_job")) or SERVICE_JOB_MAX_TMP_BYTES
        ),
        "workers_requested": _safe_int(resource_limits.get("workers_requested")),
    }


def _public_quality_payload(production_summary: dict[str, Any] | None) -> dict[str, Any]:
    quality = production_summary.get("processing_quality_summary") if isinstance(production_summary, dict) else None
    if not isinstance(quality, dict) or quality.get("provided") is not True:
        return {
            "provided": False,
            "status": "not_available",
            "public_safe": True,
            "blocking_codes": [],
            "quality_signal_status": "not_available",
            "quality_operations_applied": {},
            "quality_metrics": {},
            "guardrails": {
                "enabled": True,
                "warning_files": None,
                "failed_files": None,
                "failure_reasons": {},
            },
        }
    counts = quality.get("counts") if isinstance(quality.get("counts"), dict) else {}
    signal = quality.get("quality_signal") if isinstance(quality.get("quality_signal"), dict) else {}
    metrics = quality.get("quality_metrics") if isinstance(quality.get("quality_metrics"), dict) else {}
    guardrails = quality.get("guardrails") if isinstance(quality.get("guardrails"), dict) else {}
    return {
        "provided": True,
        "schema_version": quality.get("schema_version"),
        "status": quality.get("status"),
        "blocking_codes": _string_list(quality.get("blocking_codes")),
        "public_safe": bool(quality.get("public_safe", True)),
        "processed_files": _safe_int(counts.get("processed_files")),
        "failed_files": _safe_int(counts.get("failed_files")),
        "processing_warning_files": _safe_int(counts.get("processing_warning_files")),
        "retry_list_files": _safe_int(counts.get("retry_list_files")),
        "guardrail_failed_files": _safe_int(guardrails.get("failed_files")),
        "quality_signal_status": str(signal.get("status") or "unknown"),
        "any_quality_operation_changed_files": _safe_int(signal.get("any_quality_operation_changed_files")),
        "geometry_changed_files": _safe_int(signal.get("geometry_changed_files")),
        "background_cleanup_changed_files": _safe_int(signal.get("background_cleanup_changed_files")),
        "text_enhancement_changed_files": _safe_int(signal.get("text_enhancement_changed_files")),
        "defect_cleanup_changed_files": _safe_int(signal.get("defect_cleanup_changed_files")),
        "quality_operations_applied": _bool_dict(signal.get("quality_operations_applied")),
        "quality_metrics": _public_quality_metrics(metrics),
        "guardrails": {
            "enabled": bool(guardrails.get("enabled", True)),
            "warning_files": _safe_int(guardrails.get("warning_files")),
            "failed_files": _safe_int(guardrails.get("failed_files")),
            "failure_reasons": _int_dict(guardrails.get("failure_reasons")),
        },
    }


def _public_quality_metrics(metrics: Any) -> dict[str, dict[str, float | int | None]]:
    metrics = metrics if isinstance(metrics, dict) else {}
    public_metrics: dict[str, dict[str, float | int | None]] = {}
    for metric_id in PUBLIC_QUALITY_METRIC_IDS:
        payload = metrics.get(metric_id)
        if not isinstance(payload, dict):
            continue
        public_metrics[metric_id] = {
            "count": _safe_int(payload.get("count")) or 0,
            "average": _safe_float(payload.get("average")),
            "max": _safe_float(payload.get("max")),
        }
    return public_metrics


def _public_timings_payload(
    production_summary: dict[str, Any] | None,
    production_progress: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = production_summary if isinstance(production_summary, dict) else {}
    progress = production_progress if isinstance(production_progress, dict) else {}
    stage_source = summary.get("stage_timings") if isinstance(summary.get("stage_timings"), dict) else None
    if stage_source is None:
        stage_source = progress.get("stage_timings") if isinstance(progress.get("stage_timings"), dict) else None
    aggregate_source = (
        summary.get("aggregate_processing")
        if isinstance(summary.get("aggregate_processing"), dict)
        else progress.get("aggregate_processing")
    )
    operation_timings = _public_operation_timings(_operation_timings_from_summary(summary))
    provided = bool(stage_source or aggregate_source or operation_timings)
    return {
        "schema_version": SERVICE_JOB_PUBLIC_TIMINGS_SCHEMA_VERSION,
        "provided": provided,
        "status": "available" if provided else "not_available",
        "aggregate_only": True,
        "public_safe": True,
        "stage_timings": _public_stage_timings(stage_source),
        "aggregate_processing": _public_aggregate_processing(aggregate_source),
        "operation_timings": operation_timings,
        "operation_count": len(operation_timings),
        "privacy": {
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
        },
    }


def _operation_timings_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    performance = summary.get("performance") if isinstance(summary.get("performance"), dict) else {}
    processing = performance.get("processing") if isinstance(performance.get("processing"), dict) else {}
    timings = processing.get("operation_timings") if isinstance(processing.get("operation_timings"), dict) else {}
    return timings


def _public_stage_timings(stage_timings: Any) -> dict[str, Any]:
    stage_timings = stage_timings if isinstance(stage_timings, dict) else {}
    raw_stages = stage_timings.get("stages") if isinstance(stage_timings.get("stages"), list) else []
    raw_by_id = {stage.get("id"): stage for stage in raw_stages if isinstance(stage, dict)}
    stages: list[dict[str, Any]] = []
    for stage_id in PUBLIC_STAGE_TIMING_IDS:
        raw = raw_by_id.get(stage_id)
        if not isinstance(raw, dict):
            continue
        stages.append(
            {
                "id": stage_id,
                "elapsed_seconds": _safe_float(raw.get("elapsed_seconds")),
                "status": _public_stage_status(raw.get("status")),
            }
        )
    return {
        "schema_version": "scan-qc.production-stage-timings.v1",
        "aggregate_only": True,
        "stages": stages,
    }


def _public_stage_status(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    return value if value in {"pending", "running", "completed", "failed", "interrupted"} else "unknown"


def _public_aggregate_processing(aggregate_processing: Any) -> dict[str, Any]:
    aggregate_processing = aggregate_processing if isinstance(aggregate_processing, dict) else {}
    return {
        "schema_version": "scan-qc.aggregate-processing-rate.v1",
        "aggregate_only": True,
        "total_images": _safe_int(aggregate_processing.get("total_images")),
        "processed_images": _safe_int(aggregate_processing.get("processed_images")),
        "remaining_images": _safe_int(aggregate_processing.get("remaining_images")),
        "elapsed_seconds": _safe_float(aggregate_processing.get("elapsed_seconds")),
        "images_per_minute": _safe_float(aggregate_processing.get("images_per_minute")),
        "estimated_remaining_seconds": _safe_float(aggregate_processing.get("estimated_remaining_seconds")),
        "unavailable_reason": _public_unavailable_reason(aggregate_processing.get("unavailable_reason")),
    }


def _public_unavailable_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value in PUBLIC_AGGREGATE_PROCESSING_UNAVAILABLE_REASONS else "unknown"


def _public_operation_timings(operation_timings: Any) -> dict[str, dict[str, Any]]:
    operation_timings = operation_timings if isinstance(operation_timings, dict) else {}
    public_timings: dict[str, dict[str, Any]] = {}
    for operation_id in PUBLIC_OPERATION_TIMING_IDS:
        raw = operation_timings.get(operation_id)
        if not isinstance(raw, dict):
            continue
        public_timings[operation_id] = {
            "enabled": bool(raw.get("enabled")),
            "file_count": _safe_int(raw.get("file_count")),
            "elapsed_seconds": _safe_float(raw.get("elapsed_seconds")),
            "average_seconds_per_file": _safe_float(raw.get("average_seconds_per_file")),
            "files_per_minute": _safe_float(raw.get("files_per_minute")),
            "reused_scan_measurement_files": _safe_int(raw.get("reused_scan_measurement_files")),
        }
    return public_timings


def _capture_source_integrity_start(record: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    try:
        return _source_integrity_snapshot(Path(record["paths"]["input_dir"]))
    except Exception:
        record["source_integrity"] = _source_integrity_unavailable("pre_run_snapshot_failed")
        _write_job_record(_job_root_from_record(record), record)
        return None


def _refresh_source_integrity(
    record: dict[str, Any],
    before_snapshot: dict[str, dict[str, Any]] | None,
) -> None:
    if before_snapshot is None:
        if not isinstance(record.get("source_integrity"), dict):
            record["source_integrity"] = _source_integrity_unavailable("pre_run_snapshot_unavailable")
        _write_job_record(_job_root_from_record(record), record)
        return
    try:
        after_snapshot = _source_integrity_snapshot(Path(record["paths"]["input_dir"]))
        record["source_integrity"] = _source_integrity_result(before_snapshot, after_snapshot)
    except Exception:
        record["source_integrity"] = _source_integrity_unavailable("post_run_snapshot_failed")
    _write_job_record(_job_root_from_record(record), record)


def _source_integrity_snapshot(input_dir: Path) -> dict[str, dict[str, Any]]:
    root = input_dir.resolve()
    snapshot: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        relative_path = path.relative_to(root).as_posix()
        snapshot[relative_path] = {
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return snapshot


def _source_integrity_result(
    before_snapshot: dict[str, dict[str, Any]],
    after_snapshot: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    before_paths = set(before_snapshot)
    after_paths = set(after_snapshot)
    shared_paths = before_paths & after_paths
    modified_files = sum(1 for path in shared_paths if before_snapshot[path] != after_snapshot[path])
    missing_files = len(before_paths - after_paths)
    added_files = len(after_paths - before_paths)
    source_images_modified = modified_files > 0 or missing_files > 0
    source_tree_changed = source_images_modified or added_files > 0
    return {
        "schema_version": SERVICE_JOB_SOURCE_INTEGRITY_SCHEMA_VERSION,
        "provided": True,
        "status": "pass" if not source_tree_changed else "fail",
        "aggregate_only": True,
        "public_safe": True,
        "checked_files": len(before_paths),
        "unchanged_files": max(0, len(shared_paths) - modified_files),
        "modified_files": modified_files,
        "missing_files": missing_files,
        "added_files": added_files,
        "source_images_modified": source_images_modified,
        "source_tree_changed": source_tree_changed,
        "hashes_recorded_in_public_summary": False,
        "privacy": _source_integrity_privacy(),
    }


def _source_integrity_unavailable(reason_code: str) -> dict[str, Any]:
    return {
        "schema_version": SERVICE_JOB_SOURCE_INTEGRITY_SCHEMA_VERSION,
        "provided": False,
        "status": "not_available",
        "reason_code": reason_code,
        "aggregate_only": True,
        "public_safe": True,
        "checked_files": None,
        "unchanged_files": None,
        "modified_files": None,
        "missing_files": None,
        "added_files": None,
        "source_images_modified": False,
        "source_tree_changed": False,
        "hashes_recorded_in_public_summary": False,
        "privacy": _source_integrity_privacy(),
    }


def _public_source_integrity_payload(source_integrity: Any) -> dict[str, Any]:
    source_integrity = source_integrity if isinstance(source_integrity, dict) else {}
    if source_integrity.get("provided") is not True:
        return _source_integrity_unavailable(_public_source_integrity_reason(source_integrity.get("reason_code")))
    return {
        "schema_version": SERVICE_JOB_SOURCE_INTEGRITY_SCHEMA_VERSION,
        "provided": True,
        "status": "pass" if source_integrity.get("status") == "pass" else "fail",
        "aggregate_only": True,
        "public_safe": True,
        "checked_files": _safe_int(source_integrity.get("checked_files")),
        "unchanged_files": _safe_int(source_integrity.get("unchanged_files")),
        "modified_files": _safe_int(source_integrity.get("modified_files")),
        "missing_files": _safe_int(source_integrity.get("missing_files")),
        "added_files": _safe_int(source_integrity.get("added_files")),
        "source_images_modified": bool(source_integrity.get("source_images_modified")),
        "source_tree_changed": bool(source_integrity.get("source_tree_changed")),
        "hashes_recorded_in_public_summary": False,
        "privacy": _source_integrity_privacy(),
    }


def _source_integrity_privacy() -> dict[str, bool]:
    return {
        "contains_paths": False,
        "contains_filenames": False,
        "contains_hashes": False,
        "contains_thumbnails": False,
        "contains_ocr_text": False,
        "contains_image_content": False,
    }


def _public_source_integrity_reason(value: Any) -> str:
    if not isinstance(value, str):
        return "not_checked"
    return value if value in PUBLIC_SOURCE_INTEGRITY_REASON_CODES else "not_checked"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _bool_dict(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(key): bool(flag) for key, flag in sorted(value.items()) if isinstance(key, str)}


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _safe_int(count) or 0 for key, count in sorted(value.items()) if isinstance(key, str)}


def _refresh_service_job_review_artifacts(record: dict[str, Any], production_summary: dict[str, Any]) -> None:
    try:
        review_dir = _service_job_review_dir(record)
        review_dir.mkdir(parents=True, exist_ok=True)
        artifacts = production_summary.get("artifacts") if isinstance(production_summary, dict) else {}
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        manifest_path = Path(str(artifacts.get("processing_manifest", "")))
        scan_report_path = Path(str(artifacts.get("admin_scan_report", "")))
        if not manifest_path.is_file():
            record["local_review"] = _local_review_unavailable("missing_processing_manifest")
            _write_job_record(_job_root_from_record(record), record)
            return

        review_json_path, review_html_path = write_processing_review_package(manifest_path, review_dir)
        review_package = _read_json(review_json_path) or {}
        queue_path = review_dir / PRODUCTION_REVIEW_QUEUE_JSON
        queue_inputs: dict[str, Path] = {"processing_review_package_path": review_json_path}
        if scan_report_path.is_file():
            queue_inputs["scan_qc_report_path"] = scan_report_path
        _queue_path, queue = write_production_review_queue(queue_path, **queue_inputs)
        record["local_review"] = {
            "schema_version": "scan-qc.service-job-local-review.v1",
            "provided": True,
            "status": "available",
            "local_only": True,
            "review_dir": str(review_dir),
            "artifacts": {
                "processing_review_package": str(review_json_path),
                "processing_review_package_html": str(review_html_path),
                "production_review_queue": str(queue_path),
            },
            "summary": _local_review_summary(queue, review_package),
        }
    except Exception:
        record["local_review"] = _local_review_unavailable("review_artifact_generation_failed")
    _write_job_record(_job_root_from_record(record), record)


def _local_review_is_available(record: dict[str, Any]) -> bool:
    local_review = record.get("local_review")
    return isinstance(local_review, dict) and local_review.get("provided") is True


def _service_job_review_dir(record: dict[str, Any]) -> Path:
    job_root = _job_root_from_record(record)
    paths = record.get("paths") if isinstance(record.get("paths"), dict) else {}
    configured = paths.get("review_dir") if isinstance(paths, dict) else None
    review_dir = Path(str(configured)) if configured else job_root / "review"
    review_dir = review_dir.resolve()
    _require_within(review_dir, job_root)
    return review_dir


def _local_review_unavailable(reason_code: str) -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.service-job-local-review.v1",
        "provided": False,
        "status": "not_available",
        "reason_code": reason_code,
        "local_only": True,
        "artifacts": {},
        "summary": {},
    }


def _local_review_summary(queue: dict[str, Any], processing_review_package: dict[str, Any]) -> dict[str, Any]:
    summary = queue.get("summary") if isinstance(queue, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    return {
        "review_item_count": _safe_int(summary.get("total_items")) or 0,
        "ready_for_operator_review": bool(summary.get("ready_for_operator_review")),
        "items_by_source_category": _int_dict(summary.get("items_by_source_category")),
        "items_by_suggested_action": _int_dict(summary.get("items_by_suggested_action")),
        "processing_review_group_counts": _processing_review_group_counts(processing_review_package),
    }


def _processing_review_group_counts(package: dict[str, Any]) -> dict[str, int]:
    groups = package.get("groups") if isinstance(package, dict) else {}
    if not isinstance(groups, dict):
        return {}
    counts: dict[str, int] = {}
    for group_id, payload in groups.items():
        if isinstance(group_id, str) and isinstance(payload, dict):
            counts[group_id] = _safe_int(payload.get("count")) or 0
    return dict(sorted(counts.items()))


def _public_local_review_payload(local_review: Any) -> dict[str, Any]:
    local_review = local_review if isinstance(local_review, dict) else {}
    summary = local_review.get("summary") if isinstance(local_review.get("summary"), dict) else {}
    return {
        "provided": bool(local_review.get("provided")),
        "status": str(local_review.get("status") or "not_available"),
        "reason_code": str(local_review.get("reason_code") or "") or None,
        "local_only": True,
        "processing_review_package_written": bool(
            isinstance(local_review.get("artifacts"), dict)
            and local_review["artifacts"].get("processing_review_package")
        ),
        "processing_review_package_html_written": bool(
            isinstance(local_review.get("artifacts"), dict)
            and local_review["artifacts"].get("processing_review_package_html")
        ),
        "production_review_queue_written": bool(
            isinstance(local_review.get("artifacts"), dict)
            and local_review["artifacts"].get("production_review_queue")
        ),
        "review_item_count": _safe_int(summary.get("review_item_count")),
        "ready_for_operator_review": bool(summary.get("ready_for_operator_review")),
        "items_by_source_category": _int_dict(summary.get("items_by_source_category")),
        "items_by_suggested_action": _int_dict(summary.get("items_by_suggested_action")),
        "processing_review_group_counts": _int_dict(summary.get("processing_review_group_counts")),
        "privacy": {
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
        },
    }


def _reserve_async_job(record: dict[str, Any]) -> str:
    key = _async_job_key(record)
    requested_workers = _async_worker_units(record)
    with _ASYNC_JOB_LOCK:
        if key in _ASYNC_JOB_KEYS:
            raise RuntimeError("Service job is already running.")
        if len(_ASYNC_JOB_KEYS) >= SERVICE_JOB_MAX_ACTIVE_JOBS:
            raise RuntimeError("Service active job limit reached.")
        if sum(_ASYNC_JOB_WORKERS.values()) + requested_workers > SERVICE_JOB_MAX_ACTIVE_WORKERS:
            raise RuntimeError("Service active worker limit reached.")
        _ASYNC_JOB_KEYS.add(key)
        _ASYNC_JOB_WORKERS[key] = requested_workers
    return key


def _unregister_async_job_key(key: str) -> None:
    with _ASYNC_JOB_LOCK:
        _ASYNC_JOB_KEYS.discard(key)
        _ASYNC_JOB_WORKERS.pop(key, None)


def _async_job_is_active(record: dict[str, Any]) -> bool:
    with _ASYNC_JOB_LOCK:
        return _async_job_key(record) in _ASYNC_JOB_KEYS


def _async_job_key(record: dict[str, Any]) -> str:
    return _async_job_key_from_parts(Path(str(record["paths"]["service_root"])), str(record["job_id"]))


def _async_job_key_from_parts(service_root: Path, job_id: str) -> str:
    return f"{service_root.resolve()}::{job_id}"


def _async_worker_units(record: dict[str, Any]) -> int:
    limits = record.get("resource_limits") if isinstance(record.get("resource_limits"), dict) else {}
    workers = _safe_int(limits.get("workers_requested")) if isinstance(limits, dict) else None
    if workers is None:
        return SERVICE_JOB_MAX_WORKERS
    return min(max(1, workers), SERVICE_JOB_MAX_WORKERS)


def _refresh_retry_status(record: dict[str, Any], state: str) -> None:
    retry = record.get("retry")
    if not isinstance(retry, dict) or retry.get("status") != "retrying" or state == "running":
        return
    retry["status"] = "completed" if state in {"finished", "needs_review"} else state
    retry["finished_at"] = _utc_now()


def _validate_job_tmp_quota(record: dict[str, Any]) -> None:
    paths = record.get("paths") if isinstance(record.get("paths"), dict) else {}
    tmp_dir = Path(str(paths.get("tmp_dir", ""))).resolve() if isinstance(paths, dict) else Path()
    _require_within(tmp_dir, _job_root_from_record(record))
    limit = _job_tmp_quota(record)
    used = _directory_size(tmp_dir)
    if used > limit:
        raise RuntimeError("Service job temporary directory quota exceeded.")


def _job_tmp_quota(record: dict[str, Any]) -> int:
    limits = record.get("resource_limits") if isinstance(record.get("resource_limits"), dict) else {}
    configured = _safe_int(limits.get("max_tmp_bytes_per_job")) if isinstance(limits, dict) else None
    return configured if configured is not None else SERVICE_JOB_MAX_TMP_BYTES


def _directory_size(root: Path) -> int:
    total = 0
    if not root.exists():
        return total
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def _isolation_payload(service_root: Path, job_root: Path, directories: dict[str, Path]) -> dict[str, Any]:
    for path in directories.values():
        _require_within(path, job_root)
    return {
        "service_root": str(service_root),
        "job_root": str(job_root),
        "job_root_isolated": True,
        "metadata_isolated": directories["metadata"].parent == job_root,
        "derivatives_isolated": directories["derivatives"].parent == job_root,
        "tmp_isolated": directories["tmp"].parent == job_root,
        "checkpoint_isolated": directories["checkpoints"].parent == job_root,
        "review_isolated": directories["review"].parent == job_root,
        "log_isolated": directories["logs"].parent == job_root,
    }


def _update_record_state(record: dict[str, Any], state: str, *, recovery_status: str) -> None:
    previous_state = str(record.get("state") or "")
    previous_recovery = record.get("recovery") if isinstance(record.get("recovery"), dict) else {}
    previous_recovery_status = str(previous_recovery.get("status") or "")
    record["state"] = state
    record["updated_at"] = _utc_now()
    record["recovery"] = {
        **record.get("recovery", {}),
        "status": recovery_status,
        "resume_supported": True,
    }
    if previous_state != state or previous_recovery_status != recovery_status:
        _append_service_job_event(record, "state_changed", state=state, recovery_status=recovery_status)


def _write_job_record(job_root: Path, record: dict[str, Any]) -> Path:
    path = job_root / SERVICE_JOB_RECORD_JSON
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_public_summary(job_root: Path, summary: dict[str, Any]) -> dict[str, Any]:
    path = job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _job_root_from_record(record: dict[str, Any]) -> Path:
    return Path(record["paths"]["job_root"]).resolve()


def _private_record_privacy() -> dict[str, bool]:
    return {
        "aggregate_only": False,
        "contains_private_paths": True,
        "contains_file_list": False,
        "contains_hashes": False,
        "contains_thumbnails": False,
        "contains_ocr_text": False,
        "contains_image_content": False,
    }


def _public_summary_privacy() -> dict[str, bool]:
    return {
        "contains_paths": False,
        "contains_filenames": False,
        "contains_hashes": False,
        "contains_thumbnails": False,
        "contains_ocr_text": False,
        "contains_image_content": False,
        "contains_environment_values": False,
    }


def _state_label_zh(state: str) -> str:
    return {
        "created": "已创建",
        "running": "处理中",
        "finished": "已完成",
        "needs_review": "等待复核",
        "failed": "失败",
        "interrupted": "已中断",
        "cancelled": "已取消",
        "needs_recovery": "需要恢复",
    }.get(state, state)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
