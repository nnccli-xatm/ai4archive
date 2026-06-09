"""Service-style production job boundaries.

This module is the small backend core for future HTTP/API surfaces. It keeps
private path-bearing state in a per-job record and exposes a separate
public-safe aggregate summary for status polling or handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
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
from .rules import (
    BUILTIN_RULE_TEMPLATE_IDS,
    RulesProfileError,
    builtin_rules_profile,
    processing_defaults_for_rule_template,
)


SERVICE_JOB_SCHEMA_VERSION = "scan-qc.service-job.v1"
SERVICE_JOB_PUBLIC_SUMMARY_SCHEMA_VERSION = "scan-qc.service-job-public-summary.v1"
SERVICE_JOB_INDEX_PUBLIC_SUMMARY_SCHEMA_VERSION = "scan-qc.service-job-index-public-summary.v1"
SERVICE_JOB_RECORD_JSON = "service_job.json"
SERVICE_JOB_PUBLIC_SUMMARY_JSON = "service_job_public_summary.json"
SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON = "service_job_index_public_summary.json"
SERVICE_JOBS_DIRNAME = "jobs"
JOB_ID_PATTERN = re.compile(r"^job-[A-Za-z0-9][A-Za-z0-9_-]{2,80}$")
TERMINAL_STATES = {"finished", "needs_review", "failed", "interrupted", "cancelled"}
SERVICE_JOB_MAX_WORKERS = 8
SERVICE_JOB_MAX_ACTIVE_JOBS = 2
_ASYNC_JOB_LOCK = threading.Lock()
_ASYNC_JOB_KEYS: set[str] = set()


@dataclass(frozen=True)
class ServiceJobConfig:
    input_dir: Path
    service_root: Path
    project_id: str = "default-project"
    batch_id: str = "default-batch"
    rule_template: str = "dat-31-2017-standard"
    processing_mode: str = "standard"
    workers: int | None = 1


def create_service_job(config: ServiceJobConfig, *, job_id: str | None = None) -> dict[str, Any]:
    """Create an isolated service job record and public-safe summary."""

    input_dir = config.input_dir.resolve()
    service_root = config.service_root.resolve()
    _validate_service_paths(input_dir, service_root)
    workers = _validate_worker_limit(config.workers)
    job_id = _validate_job_id(job_id or _new_job_id())
    job_root = (service_root / SERVICE_JOBS_DIRNAME / job_id).resolve()
    _require_within(job_root, service_root)
    if job_root.exists():
        raise FileExistsError(f"Service job already exists: {job_id}")

    directories = _job_directories(job_root)
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=False)

    profile = _rules_profile_for_template(config.rule_template)
    processing_defaults = _processing_options_for_job(config.rule_template, config.processing_mode)
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
            "rule_template": profile.metadata().get("template"),
            "processing_mode": config.processing_mode,
            "processing_defaults": processing_defaults,
            "workers": workers,
        },
        "resource_limits": {
            "max_workers_per_job": SERVICE_JOB_MAX_WORKERS,
            "workers_requested": workers,
        },
        "production_artifacts": _production_artifact_paths(directories["metadata"], directories["derivatives"]),
        "recovery": {
            "status": "created",
            "resume_supported": True,
            "checkpoint_files": [SERVICE_JOB_RECORD_JSON, SERVICE_JOB_PUBLIC_SUMMARY_JSON],
        },
    }
    _write_job_record(job_root, record)
    return _write_public_summary(job_root, _public_summary_from_record(record))


def run_service_job(service_root: Path, job_id: str) -> dict[str, Any]:
    """Run a created service job synchronously and refresh its public summary."""

    _mark_service_job_running(service_root, job_id, recovery_status="running")
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
    _update_record_state(record, "running", recovery_status=recovery_status)
    _write_job_record(_job_root_from_record(record), record)
    return _write_public_summary(_job_root_from_record(record), _public_summary_from_record(record))


def _execute_running_service_job(service_root: Path, job_id: str, *, raise_errors: bool) -> dict[str, Any] | None:
    record = load_service_job_record(service_root, job_id)
    if record.get("state") != "running":
        return recover_service_job(service_root, job_id)
    try:
        production_summary = run_production_folder(_production_config_from_record(record))
        latest = load_service_job_record(service_root, job_id)
        _refresh_service_job_review_artifacts(latest, production_summary)
    except BaseException:
        latest = load_service_job_record(service_root, job_id)
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
    _update_record_state(record, state, recovery_status=recovery_status)
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
    if jobs_dir.is_dir():
        for record_path in sorted(jobs_dir.glob(f"*/{SERVICE_JOB_RECORD_JSON}"), key=lambda path: path.parent.name):
            try:
                summaries.append(recover_service_job(root, record_path.parent.name))
            except (OSError, ValueError, json.JSONDecodeError):
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
        "state_counts": state_counts,
        "jobs": summaries,
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
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("schema_version") != SERVICE_JOB_SCHEMA_VERSION:
        raise ValueError("Unsupported service job schema.")
    if record.get("job_id") != job_id:
        raise ValueError("Service job id mismatch.")
    _validate_loaded_record_paths(record, root, job_root)
    return record


def _validate_service_paths(input_dir: Path, service_root: Path) -> None:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Service job input directory does not exist: {input_dir}")
    if _paths_overlap(input_dir, service_root):
        raise ValueError("Service root and input directory must not overlap.")
    service_root.mkdir(parents=True, exist_ok=True)
    jobs_dir = service_root / SERVICE_JOBS_DIRNAME
    jobs_dir.mkdir(parents=True, exist_ok=True)


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


def _rules_profile_for_template(rule_template: str):
    if rule_template not in BUILTIN_RULE_TEMPLATE_IDS:
        raise RulesProfileError("Service jobs currently require a built-in rule template.")
    return builtin_rules_profile(rule_template)


def _processing_options_for_job(rule_template: str, processing_mode: str) -> dict[str, bool]:
    defaults = processing_defaults_for_rule_template(rule_template)
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
    template = record["template_snapshot"]["rule_template"]["id"]
    defaults = dict(record["template_snapshot"]["processing_defaults"])
    return ProductionRunConfig(
        input_dir=Path(record["paths"]["input_dir"]),
        derivative_output_dir=Path(record["paths"]["derivatives_dir"]),
        metadata_output_dir=Path(record["paths"]["metadata_dir"]),
        project_id=record["project"]["project_id"],
        batch_id=record["project"]["batch_id"],
        rules_profile=builtin_rules_profile(template),
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
            "rule_template_id": record["template_snapshot"]["rule_template"]["id"],
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
        },
        "recovery": _public_recovery_payload(record.get("recovery")),
        "quality": _public_quality_payload(production_summary),
        "local_review": _public_local_review_payload(record.get("local_review")),
        "source_images_modified": False,
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
        "failed_files": _safe_int(summary_counts.get("failed_files")),
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


def _public_recovery_payload(recovery: Any) -> dict[str, Any]:
    recovery = recovery if isinstance(recovery, dict) else {}
    return {
        "status": str(recovery.get("status") or "unknown"),
        "resume_supported": bool(recovery.get("resume_supported")),
    }


def _public_resource_limits(resource_limits: Any) -> dict[str, int | None]:
    resource_limits = resource_limits if isinstance(resource_limits, dict) else {}
    return {
        "max_workers_per_job": _safe_int(resource_limits.get("max_workers_per_job")),
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
            "quality_operations_applied": {},
            "guardrails": {
                "enabled": True,
                "warning_files": None,
                "failed_files": None,
                "failure_reasons": {},
            },
        }
    counts = quality.get("counts") if isinstance(quality.get("counts"), dict) else {}
    signal = quality.get("quality_signal") if isinstance(quality.get("quality_signal"), dict) else {}
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
        "any_quality_operation_changed_files": _safe_int(signal.get("any_quality_operation_changed_files")),
        "geometry_changed_files": _safe_int(signal.get("geometry_changed_files")),
        "background_cleanup_changed_files": _safe_int(signal.get("background_cleanup_changed_files")),
        "text_enhancement_changed_files": _safe_int(signal.get("text_enhancement_changed_files")),
        "defect_cleanup_changed_files": _safe_int(signal.get("defect_cleanup_changed_files")),
        "quality_operations_applied": _bool_dict(signal.get("quality_operations_applied")),
        "guardrails": {
            "enabled": bool(guardrails.get("enabled", True)),
            "warning_files": _safe_int(guardrails.get("warning_files")),
            "failed_files": _safe_int(guardrails.get("failed_files")),
            "failure_reasons": _int_dict(guardrails.get("failure_reasons")),
        },
    }


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
            "summary": _local_review_summary(queue),
        }
    except Exception:
        record["local_review"] = _local_review_unavailable("review_artifact_generation_failed")
    _write_job_record(_job_root_from_record(record), record)


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


def _local_review_summary(queue: dict[str, Any]) -> dict[str, Any]:
    summary = queue.get("summary") if isinstance(queue, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    return {
        "review_item_count": _safe_int(summary.get("total_items")) or 0,
        "ready_for_operator_review": bool(summary.get("ready_for_operator_review")),
        "items_by_source_category": _int_dict(summary.get("items_by_source_category")),
        "items_by_suggested_action": _int_dict(summary.get("items_by_suggested_action")),
    }


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
    with _ASYNC_JOB_LOCK:
        if key in _ASYNC_JOB_KEYS:
            raise RuntimeError("Service job is already running.")
        if len(_ASYNC_JOB_KEYS) >= SERVICE_JOB_MAX_ACTIVE_JOBS:
            raise RuntimeError("Service active job limit reached.")
        _ASYNC_JOB_KEYS.add(key)
    return key


def _unregister_async_job_key(key: str) -> None:
    with _ASYNC_JOB_LOCK:
        _ASYNC_JOB_KEYS.discard(key)


def _async_job_is_active(record: dict[str, Any]) -> bool:
    with _ASYNC_JOB_LOCK:
        return _async_job_key(record) in _ASYNC_JOB_KEYS


def _async_job_key(record: dict[str, Any]) -> str:
    return _async_job_key_from_parts(Path(str(record["paths"]["service_root"])), str(record["job_id"]))


def _async_job_key_from_parts(service_root: Path, job_id: str) -> str:
    return f"{service_root.resolve()}::{job_id}"


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
    }


def _update_record_state(record: dict[str, Any], state: str, *, recovery_status: str) -> None:
    record["state"] = state
    record["updated_at"] = _utc_now()
    record["recovery"] = {
        **record.get("recovery", {}),
        "status": recovery_status,
        "resume_supported": True,
    }


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
