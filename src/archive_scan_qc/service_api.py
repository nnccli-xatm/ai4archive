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
    SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION,
    SERVICE_JOB_INDEX_RECOVERY_ISSUES_SCHEMA_VERSION,
    SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
    LOCAL_REVIEW_ARTIFACT_SCHEMA_VERSION,
    LOCAL_REVIEW_ITEM_SCHEMA_VERSION,
    SERVICE_JOB_MAX_ACTIVE_JOBS,
    SERVICE_JOB_MAX_ACTIVE_WORKERS,
    SERVICE_JOB_MAX_TMP_BYTES,
    SERVICE_JOB_MAX_WORKERS,
    SERVICE_JOB_EVENT_LOG_SCHEMA_VERSION,
    SERVICE_JOB_MIN_FREE_SPACE_BYTES,
    SERVICE_JOB_REVIEW_HISTORY_SCHEMA_VERSION,
    RETRYABLE_STATES,
    TERMINAL_STATES,
    ServiceJobConfig,
    cancel_service_job,
    create_service_job,
    recover_service_job,
    recover_service_jobs,
    retry_service_job,
    resolve_service_job_local_preview,
    run_service_job,
    read_service_job_local_review_artifact,
    read_service_job_local_review_item,
    read_service_job_review_history_summary,
    start_service_job_async,
    write_service_job_review_actions,
)


SERVICE_API_SCHEMA_VERSION = "scan-qc.service-api.v1"
PRODUCTION_SESSION_SCHEMA_VERSION = "scan-qc.service-production-session.v1"
_FINISH_EXPORT_STATE_BLOCKING_CODES = {
    "needs_review": "job_requires_review",
    "failed": "job_failed",
    "interrupted": "job_interrupted",
    "cancelled": "job_cancelled",
    "needs_recovery": "job_needs_recovery",
}


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
            {"method": "GET", "path": "/api/jobs/{job_id}/local-review-item/{local_id}", "implemented_by_core": True},
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
            {"method": "GET", "path": "/api/production/review-item", "implemented_by_core": True},
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
            "auto_worker_scheduling": True,
            "worker_scheduling_source": "service_job_heuristic_when_workers_omitted",
        },
        "schemas": {
            "service_api": SERVICE_API_SCHEMA_VERSION,
            "service_job_public_summary": "scan-qc.service-job-public-summary.v1",
            "service_job_index_public_summary": "scan-qc.service-job-index-public-summary.v1",
            "service_job_index_quality": SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION,
            "service_job_index_recovery_issues": SERVICE_JOB_INDEX_RECOVERY_ISSUES_SCHEMA_VERSION,
            "service_job_index_source_integrity": SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
            "service_job_local_review_artifact": LOCAL_REVIEW_ARTIFACT_SCHEMA_VERSION,
            "service_job_local_review_item": LOCAL_REVIEW_ITEM_SCHEMA_VERSION,
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
        "public_boundaries": {
            "service_quality": _service_quality_public_boundary(),
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
            "quality": index.get("quality") if isinstance(index.get("quality"), dict) else {},
            "source_integrity": (
                index.get("source_integrity") if isinstance(index.get("source_integrity"), dict) else {}
            ),
            "recovery_issues": index.get("recovery_issues") if isinstance(index.get("recovery_issues"), dict) else {},
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


def production_review_item_response(*, service_root: Path, job_id: str, local_id: str) -> dict[str, Any]:
    return read_service_job_local_review_item(service_root, job_id, local_id)


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
    quality_blocking_code_values = quality.get("blocking_codes") if isinstance(quality.get("blocking_codes"), list) else []
    quality_blocking_codes = _public_code_list(quality_blocking_code_values)
    source_images_modified = bool(summary.get("source_images_modified"))
    review_gate = _finish_export_review_gate(summary)
    blocking_codes = _finish_export_blocking_codes(
        state=state,
        quality_blocking_codes=quality_blocking_codes,
        source_images_modified=source_images_modified,
        review_blocking_codes=review_gate["blocking_codes"],
    )
    return _production_response(
        view="finish_export",
        job=summary,
        finish_export={
            "terminal": state in TERMINAL_STATES,
            "retryable": state in RETRYABLE_STATES,
            "ready_for_export": state == "finished" and not blocking_codes,
            "requires_review": (
                state == "needs_review"
                or bool(quality_blocking_codes)
                or source_images_modified
                or review_gate["requires_operator_review"]
            ),
            "state": state,
            "blocking_codes": blocking_codes,
            "source_images_modified": source_images_modified,
            "review_gate": review_gate,
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


def get_job_local_review_item_response(*, service_root: Path, job_id: str, local_id: str) -> dict[str, Any]:
    return read_service_job_local_review_item(service_root, job_id, local_id)


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


def _service_quality_public_boundary() -> dict[str, Any]:
    forbidden_private_content = [
        "local_paths",
        "file_lists",
        "filenames",
        "hashes",
        "thumbnails",
        "ocr_text",
        "image_content",
        "quality_rows",
    ]
    return {
        "schema_version": "scan-qc.service-quality-public-boundary.v1",
        "public_safe": True,
        "job_summary": {
            "scope": "single job public summary",
            "may_include_job_id": True,
            "may_include_processing_profile": True,
            "may_include_quality_metrics": True,
            "allowed_processing_profiles": [
                "standard",
                "print_clean",
                "photo_mixed_safe",
                "ocr_preprocess_light",
                "ocr_preprocess_leptonica",
                "ocr_preprocess_opencv_local",
                "ocr_preprocess_sauvola_wolf",
                "ocr_preprocess_stroke_bg",
                "ocr_preprocess_structure",
                "ocr_preprocess_deskew_clarity",
                "ocr_preprocess",
            ],
            "allowed_quality_context": [
                "blocking_codes",
                "quality_signal_status",
                "quality_category_counts",
                "quality_operation_category_booleans",
                "whitelisted_quality_metrics",
                "guardrail_status",
            ],
            "forbidden_content": forbidden_private_content,
        },
        "session_and_index_quality": {
            "scope": "production session and root index nested quality aggregates",
            "may_include_job_id": False,
            "may_include_processing_profile": False,
            "may_include_quality_metrics": False,
            "allowed_quality_context": [
                "quality_availability",
                "job_status_counts",
                "quality_signal_status_counts",
                "aggregate_file_counts",
                "blocking_code_counts",
            ],
            "forbidden_content": forbidden_private_content
            + ["job_ids", "processing_profiles", "quality_metrics"],
        },
    }


def _finish_export_blocking_codes(
    *,
    state: str,
    quality_blocking_codes: list[str],
    source_images_modified: bool,
    review_blocking_codes: list[str] | None = None,
) -> list[str]:
    blocking_codes = list(quality_blocking_codes)
    state_blocking_code = _FINISH_EXPORT_STATE_BLOCKING_CODES.get(state)
    if state_blocking_code:
        _append_unique(blocking_codes, state_blocking_code)
    elif state and state not in TERMINAL_STATES and state != "finished":
        _append_unique(blocking_codes, "job_not_terminal")
    elif state != "finished":
        _append_unique(blocking_codes, "job_not_exportable")
    if source_images_modified:
        _append_unique(blocking_codes, "source_images_modified")
    for code in review_blocking_codes or []:
        _append_unique(blocking_codes, code)
    return blocking_codes


def _finish_export_review_gate(summary: dict[str, Any]) -> dict[str, Any]:
    local_review = summary.get("local_review") if isinstance(summary.get("local_review"), dict) else {}
    review_actions = summary.get("review_actions") if isinstance(summary.get("review_actions"), dict) else {}
    history = review_actions.get("history") if isinstance(review_actions.get("history"), dict) else {}
    latest_summary = (
        history.get("latest_decision_summary")
        if isinstance(history.get("latest_decision_summary"), dict)
        else {}
    )
    closure = (
        latest_summary.get("closure_gate_summary")
        if isinstance(latest_summary.get("closure_gate_summary"), dict)
        else {}
    )
    review_item_count = _safe_int(local_review.get("review_item_count"))
    if review_item_count is None:
        review_item_count = 0
    reviewed_decision_count = _safe_int(latest_summary.get("total_decisions")) or 0
    requires_operator_review = bool(local_review.get("production_review_queue_written")) and review_item_count > 0
    review_actions_provided = bool(review_actions.get("provided"))
    latest_verification_status = str(history.get("latest_verification_status") or "not_available")
    latest_completion_status = str(history.get("latest_completion_status") or "not_available")
    can_complete_delivery = bool(closure.get("can_complete_delivery"))

    blocking_codes: list[str] = []
    if requires_operator_review:
        if not review_actions_provided:
            _append_unique(blocking_codes, "operator_review_required")
        elif latest_verification_status != "pass":
            _append_unique(blocking_codes, "operator_review_invalid")
        elif latest_completion_status != "complete" or reviewed_decision_count < review_item_count:
            _append_unique(blocking_codes, "operator_review_incomplete")
        elif not can_complete_delivery:
            _append_unique(blocking_codes, "operator_review_not_closed")

    return {
        "schema_version": "scan-qc.finish-export-review-gate.v1",
        "aggregate_only": True,
        "public_safe": True,
        "requires_operator_review": requires_operator_review,
        "review_item_count": review_item_count,
        "review_actions_provided": review_actions_provided,
        "reviewed_decision_count": reviewed_decision_count,
        "latest_verification_status": latest_verification_status,
        "latest_completion_status": latest_completion_status,
        "can_complete_delivery": can_complete_delivery,
        "status": "blocked" if blocking_codes else "pass",
        "blocking_codes": blocking_codes,
        "privacy": _public_privacy(),
    }


def _public_code_list(values: list[Any]) -> list[str]:
    codes: list[str] = []
    for value in values:
        if isinstance(value, str) and value:
            _append_unique(codes, value)
    return codes


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


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
            "auto_worker_scheduling": True,
            "worker_scheduling_source": "service_job_heuristic_when_workers_omitted",
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
