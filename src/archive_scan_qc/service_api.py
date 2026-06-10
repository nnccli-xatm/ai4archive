"""Endpoint-shaped service API core for service job orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rule_templates import (
    CATALOG_SCHEMA_VERSION,
    CUSTOM_TEMPLATE_VALIDATION_SCHEMA_VERSION,
    DRY_RUN_SCHEMA_VERSION,
    SERVICE_TEMPLATE_DETAIL_SCHEMA_VERSION,
    SERVICE_TEMPLATE_WRITE_SCHEMA_VERSION,
    build_rule_template_catalog,
    build_custom_rule_template_validation,
    build_rule_template_detail,
    save_service_rule_template,
)
from .service_jobs import (
    SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON,
    LOCAL_REVIEW_ARTIFACT_SCHEMA_VERSION,
    SERVICE_JOB_MAX_ACTIVE_JOBS,
    SERVICE_JOB_MAX_ACTIVE_WORKERS,
    SERVICE_JOB_MAX_TMP_BYTES,
    SERVICE_JOB_MAX_WORKERS,
    SERVICE_JOB_EVENT_LOG_SCHEMA_VERSION,
    SERVICE_JOB_MIN_FREE_SPACE_BYTES,
    SERVICE_JOB_REVIEW_HISTORY_SCHEMA_VERSION,
    ServiceJobConfig,
    cancel_service_job,
    create_service_job,
    recover_service_job,
    recover_service_jobs,
    retry_service_job,
    resolve_service_job_local_preview,
    run_service_job,
    read_service_job_local_review_artifact,
    read_service_job_review_history_summary,
    start_service_job_async,
    write_service_job_review_actions,
)


SERVICE_API_SCHEMA_VERSION = "scan-qc.service-api.v1"
PRODUCTION_SESSION_SCHEMA_VERSION = "scan-qc.service-production-session.v1"


def service_health(*, service_root: Path | None = None) -> dict[str, Any]:
    root = service_root.resolve() if service_root is not None else None
    return {
        "schema_version": SERVICE_API_SCHEMA_VERSION,
        "status": "pass",
        "generated_at": _utc_now(),
        "service_root_configured": root is not None,
        "job_index_available": bool(root and (root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).is_file()),
        "privacy": _public_privacy(),
    }


def service_capabilities() -> dict[str, Any]:
    return {
        "schema_version": SERVICE_API_SCHEMA_VERSION,
        "status": "pass",
        "generated_at": _utc_now(),
        "endpoints": [
            {"method": "GET", "path": "/api/health", "implemented_by_core": True},
            {"method": "GET", "path": "/api/capabilities", "implemented_by_core": True},
            {"method": "GET", "path": "/api/rule-templates", "implemented_by_core": True},
            {"method": "GET", "path": "/api/rule-templates/{template_id}", "implemented_by_core": True},
            {"method": "POST", "path": "/api/rule-templates/validate", "implemented_by_core": True},
            {"method": "POST", "path": "/api/rule-templates", "implemented_by_core": True},
            {"method": "PUT", "path": "/api/rule-templates/{template_id}", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs", "implemented_by_core": True},
            {"method": "GET", "path": "/api/jobs", "implemented_by_core": True},
            {"method": "GET", "path": "/api/jobs/{job_id}", "implemented_by_core": True},
            {"method": "GET", "path": "/api/jobs/{job_id}/local-review/{artifact_id}", "implemented_by_core": True},
            {"method": "GET", "path": "/api/jobs/{job_id}/local-preview/{local_id}", "implemented_by_core": True},
            {"method": "GET", "path": "/api/jobs/{job_id}/review-history", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs/{job_id}/run", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs/{job_id}/start", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs/{job_id}/retry", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs/{job_id}/cancel", "implemented_by_core": True},
            {"method": "GET", "path": "/api/production/session", "implemented_by_core": True},
            {"method": "POST", "path": "/api/production/setup", "implemented_by_core": True},
            {"method": "POST", "path": "/api/production/start", "implemented_by_core": True},
            {"method": "GET", "path": "/api/production/progress", "implemented_by_core": True},
            {"method": "GET", "path": "/api/production/review-queue", "implemented_by_core": True},
            {"method": "GET", "path": "/api/production/review-history", "implemented_by_core": True},
            {"method": "GET", "path": "/api/production/preview", "implemented_by_core": True},
            {"method": "POST", "path": "/api/production/review-actions", "implemented_by_core": True},
            {"method": "POST", "path": "/api/production/finish-export", "implemented_by_core": True},
        ],
        "resource_limits": {
            "max_workers_per_job": SERVICE_JOB_MAX_WORKERS,
            "max_active_async_jobs": SERVICE_JOB_MAX_ACTIVE_JOBS,
            "max_active_workers": SERVICE_JOB_MAX_ACTIVE_WORKERS,
            "min_free_space_bytes": SERVICE_JOB_MIN_FREE_SPACE_BYTES,
            "max_tmp_bytes_per_job": SERVICE_JOB_MAX_TMP_BYTES,
        },
        "schemas": {
            "service_api": SERVICE_API_SCHEMA_VERSION,
            "service_job_public_summary": "scan-qc.service-job-public-summary.v1",
            "service_job_index_public_summary": "scan-qc.service-job-index-public-summary.v1",
            "service_job_local_review_artifact": LOCAL_REVIEW_ARTIFACT_SCHEMA_VERSION,
            "service_job_local_preview": "scan-qc.service-job-local-preview.v1",
            "rule_template_catalog": CATALOG_SCHEMA_VERSION,
            "rule_template_dry_run": DRY_RUN_SCHEMA_VERSION,
            "rule_template_custom_validation": CUSTOM_TEMPLATE_VALIDATION_SCHEMA_VERSION,
            "service_rule_template_detail": SERVICE_TEMPLATE_DETAIL_SCHEMA_VERSION,
            "service_rule_template_write": SERVICE_TEMPLATE_WRITE_SCHEMA_VERSION,
            "production_session": PRODUCTION_SESSION_SCHEMA_VERSION,
            "service_job_review_actions": "scan-qc.service-job-review-actions.v1",
            "service_job_review_history": SERVICE_JOB_REVIEW_HISTORY_SCHEMA_VERSION,
            "service_job_event_log": SERVICE_JOB_EVENT_LOG_SCHEMA_VERSION,
        },
        "privacy": service_api_privacy(),
    }


def create_job_response(request: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    config = _service_job_config_from_request(request)
    return create_service_job(config, job_id=job_id)


def production_session_response(*, service_root: Path) -> dict[str, Any]:
    index = recover_jobs_response(service_root=service_root)
    return _production_response(
        view="session",
        session={
            "job_count": _safe_int(index.get("job_count")),
            "state_counts": _int_dict(index.get("state_counts")),
            "jobs": index.get("jobs") if isinstance(index.get("jobs"), list) else [],
        },
    )


def production_setup_response(request: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    summary = create_job_response(request, job_id=job_id)
    return _production_job_response("setup", summary)


def production_start_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return _production_job_response("start", start_job_response(service_root=service_root, job_id=job_id))


def production_progress_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return _production_job_response("progress", get_job_response(service_root=service_root, job_id=job_id))


def production_review_queue_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    summary = get_job_response(service_root=service_root, job_id=job_id)
    local_review = summary.get("local_review") if isinstance(summary.get("local_review"), dict) else {}
    return _production_response(
        view="review_queue",
        job=summary,
        review_queue={
            "available": bool(local_review.get("production_review_queue_written")),
            "review_item_count": _safe_int(local_review.get("review_item_count")),
            "by_source": _int_dict(local_review.get("review_queue_by_source")),
            "by_recommended_action": _int_dict(local_review.get("review_queue_by_recommended_action")),
            "processing_review_group_counts": _int_dict(local_review.get("processing_review_group_counts")),
            "local_review_artifact_id": (
                "production-review-queue" if local_review.get("production_review_queue_written") else None
            ),
            "local_only_artifact": bool(local_review.get("production_review_queue_written")),
        },
    )


def production_review_actions_response(request: dict[str, Any], *, service_root: Path) -> dict[str, Any]:
    job_id = _required_string(request, "job_id")
    review_decisions = request.get("review_decisions")
    if not isinstance(review_decisions, dict):
        raise ValueError("Production review actions require a review_decisions object.")
    return _production_response(
        view="review_actions",
        review_actions=write_service_job_review_actions(service_root, job_id, review_decisions),
    )


def production_review_history_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return _production_response(
        view="review_history",
        review_history=read_service_job_review_history_summary(service_root, job_id),
    )


def production_finish_export_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    summary = get_job_response(service_root=service_root, job_id=job_id)
    state = str(summary.get("state") or "")
    quality = summary.get("quality") if isinstance(summary.get("quality"), dict) else {}
    blocking_codes = quality.get("blocking_codes") if isinstance(quality.get("blocking_codes"), list) else []
    return _production_response(
        view="finish_export",
        job=summary,
        finish_export={
            "terminal": state in {"finished", "needs_review", "failed", "interrupted", "cancelled"},
            "ready_for_export": state == "finished" and not blocking_codes and not summary.get("source_images_modified"),
            "requires_review": state == "needs_review" or bool(blocking_codes),
            "state": state,
            "blocking_codes": [str(code) for code in blocking_codes if isinstance(code, str)],
            "source_images_modified": bool(summary.get("source_images_modified")),
        },
    )


def list_rule_templates_response(*, service_root: Path | None = None) -> dict[str, Any]:
    return build_rule_template_catalog(service_root=service_root)


def get_rule_template_response(*, template_id: str, service_root: Path | None = None) -> dict[str, Any]:
    return build_rule_template_detail(template_id=template_id, service_root=service_root)


def validate_rule_template_response(request: dict[str, Any]) -> dict[str, Any]:
    template_draft = request.get("template")
    if not isinstance(template_draft, dict):
        raise ValueError("Rule template validation requires a template object.")
    return build_custom_rule_template_validation(template_draft=template_draft)


def save_rule_template_response(
    request: dict[str, Any],
    *,
    service_root: Path,
    template_id: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    service_template_id = template_id or request.get("template_id")
    if not isinstance(service_template_id, str):
        raise ValueError("Rule template save requires a template_id string.")
    template_draft = request.get("template")
    if not isinstance(template_draft, dict):
        raise ValueError("Rule template save requires a template object.")
    return save_service_rule_template(
        service_root=service_root,
        template_id=service_template_id,
        template_draft=template_draft,
        replace_existing=replace_existing,
    )


def get_job_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return recover_service_job(service_root, job_id)


def get_job_local_review_artifact_response(*, service_root: Path, job_id: str, artifact_id: str) -> dict[str, Any]:
    return read_service_job_local_review_artifact(service_root, job_id, artifact_id)


def get_job_review_history_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return read_service_job_review_history_summary(service_root, job_id)


def get_job_local_preview_response(
    *,
    service_root: Path,
    job_id: str,
    local_id: str,
    source: str | None = None,
) -> dict[str, Any]:
    return resolve_service_job_local_preview(service_root, job_id, local_id, source)


def cancel_job_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return cancel_service_job(service_root, job_id)


def run_job_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return run_service_job(service_root, job_id)


def start_job_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return start_service_job_async(service_root, job_id)


def retry_job_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return retry_service_job(service_root, job_id)


def recover_jobs_response(*, service_root: Path) -> dict[str, Any]:
    return recover_service_jobs(service_root)


def service_api_privacy() -> dict[str, bool]:
    return _public_privacy()


def _production_job_response(view: str, summary: dict[str, Any]) -> dict[str, Any]:
    return _production_response(view=view, job=summary)


def _production_response(
    *,
    view: str,
    job: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    review_queue: dict[str, Any] | None = None,
    review_history: dict[str, Any] | None = None,
    review_actions: dict[str, Any] | None = None,
    finish_export: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": PRODUCTION_SESSION_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": "pass",
        "view": view,
        "aggregate_only": True,
        "public_safe": True,
        "workflow": {
            "setup_supported": True,
            "async_start_supported": True,
            "progress_polling_supported": True,
            "review_queue_public_summary_supported": True,
            "finish_export_summary_supported": True,
            "review_actions_persisted": True,
            "review_history_public_summary_supported": True,
        },
        "resource_limits": {
            "max_workers_per_job": SERVICE_JOB_MAX_WORKERS,
            "max_active_async_jobs": SERVICE_JOB_MAX_ACTIVE_JOBS,
            "max_active_workers": SERVICE_JOB_MAX_ACTIVE_WORKERS,
            "min_free_space_bytes": SERVICE_JOB_MIN_FREE_SPACE_BYTES,
            "max_tmp_bytes_per_job": SERVICE_JOB_MAX_TMP_BYTES,
        },
        "privacy": _public_privacy(),
    }
    if job is not None:
        payload["job"] = job
    if session is not None:
        payload["session"] = session
    if review_queue is not None:
        payload["review_queue"] = review_queue
    if review_history is not None:
        payload["review_history"] = review_history
    if review_actions is not None:
        payload["review_actions"] = review_actions
    if finish_export is not None:
        payload["finish_export"] = finish_export
    return payload


def _service_job_config_from_request(request: dict[str, Any]) -> ServiceJobConfig:
    return ServiceJobConfig(
        input_dir=Path(str(_required(request, "input_dir"))),
        service_root=Path(str(_required(request, "service_root"))),
        project_id=str(request.get("project_id") or "default-project"),
        batch_id=str(request.get("batch_id") or "default-batch"),
        rule_template=str(request.get("rule_template") or "dat-31-2017-standard"),
        processing_mode=str(request.get("processing_mode") or "standard"),
        workers=_optional_int(request.get("workers")),
    )


def _required(request: dict[str, Any], key: str) -> Any:
    value = request.get(key)
    if value in {None, ""}:
        raise ValueError(f"Missing service API request field: {key}.")
    return value


def _required_string(request: dict[str, Any], key: str) -> str:
    value = _required(request, key)
    if not isinstance(value, str):
        raise ValueError(f"Service API request field must be a string: {key}.")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _safe_int(raw) for key, raw in sorted(value.items()) if isinstance(key, str)}


def _public_privacy() -> dict[str, bool]:
    return {
        "public_safe": True,
        "aggregate_only": True,
        "contains_paths": False,
        "contains_filenames": False,
        "contains_hashes": False,
        "contains_thumbnails": False,
        "contains_ocr_text": False,
        "contains_image_content": False,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
