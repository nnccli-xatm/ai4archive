"""Endpoint-shaped service API core for service job orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rule_templates import (
    CATALOG_SCHEMA_VERSION,
    DRY_RUN_SCHEMA_VERSION,
    build_rule_template_catalog,
    build_rule_template_dry_run,
)
from .service_jobs import (
    SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON,
    SERVICE_JOB_MAX_ACTIVE_JOBS,
    SERVICE_JOB_MAX_ACTIVE_WORKERS,
    SERVICE_JOB_MAX_TMP_BYTES,
    SERVICE_JOB_MAX_WORKERS,
    SERVICE_JOB_MIN_FREE_SPACE_BYTES,
    ServiceJobConfig,
    cancel_service_job,
    create_service_job,
    recover_service_job,
    recover_service_jobs,
    retry_service_job,
    run_service_job,
    start_service_job_async,
)


SERVICE_API_SCHEMA_VERSION = "scan-qc.service-api.v1"


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
            {"method": "POST", "path": "/api/rule-templates", "implemented_by_core": False},
            {"method": "PUT", "path": "/api/rule-templates/{template_id}", "implemented_by_core": False},
            {"method": "POST", "path": "/api/jobs", "implemented_by_core": True},
            {"method": "GET", "path": "/api/jobs", "implemented_by_core": True},
            {"method": "GET", "path": "/api/jobs/{job_id}", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs/{job_id}/run", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs/{job_id}/start", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs/{job_id}/retry", "implemented_by_core": True},
            {"method": "POST", "path": "/api/jobs/{job_id}/cancel", "implemented_by_core": True},
            {"method": "GET", "path": "/api/production/session", "implemented_by_core": False},
            {"method": "POST", "path": "/api/production/setup", "implemented_by_core": False},
            {"method": "POST", "path": "/api/production/start", "implemented_by_core": False},
            {"method": "GET", "path": "/api/production/progress", "implemented_by_core": False},
            {"method": "GET", "path": "/api/production/review-queue", "implemented_by_core": False},
            {"method": "POST", "path": "/api/production/review-actions", "implemented_by_core": False},
            {"method": "POST", "path": "/api/production/finish-export", "implemented_by_core": False},
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
            "rule_template_catalog": CATALOG_SCHEMA_VERSION,
            "rule_template_dry_run": DRY_RUN_SCHEMA_VERSION,
        },
        "privacy": service_api_privacy(),
    }


def create_job_response(request: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    config = _service_job_config_from_request(request)
    return create_service_job(config, job_id=job_id)


def list_rule_templates_response() -> dict[str, Any]:
    return build_rule_template_catalog()


def get_rule_template_response(*, template_id: str) -> dict[str, Any]:
    return build_rule_template_dry_run(rule_template=template_id)


def get_job_response(*, service_root: Path, job_id: str) -> dict[str, Any]:
    return recover_service_job(service_root, job_id)


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


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


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
