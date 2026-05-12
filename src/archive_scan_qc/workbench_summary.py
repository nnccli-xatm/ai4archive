"""Public-safe aggregate summary bundle for the static workbench."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .evidence_bundle import _privacy_failures


WORKBENCH_SUMMARY_JSON = "workbench_public_summary.json"
SCHEMA_VERSION = "scan-qc.workbench-public-summary.v1"

_PASS_STATUSES = {"pass", "passed", "ok", "success", "no_candidates", "no_inputs"}
_FAIL_STATUSES = {"fail", "failed", "error", "blocked"}
_UNSUPPORTED_INPUT_ARTIFACT = "unsupported_input"
_PROCESSING_REUSE_COUNTER_KEYS = (
    "processing_resumed_files",
    "processing_duplicate_reused_files",
    "processing_existing_derivative_reused_files",
)
_PROCESSING_OPERATION_NAMES = ("auto_crop", "deskew", "trim_dark_border", "despeckle")
_PROCESSING_OPERATION_FIELDS = (
    "enabled",
    "file_count",
    "elapsed_seconds",
    "files_per_minute",
    "average_seconds_per_file",
)
_DESPECKLE_BACKEND_FIELDS = ("backend_mode", "numpy_available", "backend_counts")


@dataclass(frozen=True)
class WorkbenchArtifact:
    name: str
    category: str
    label: str
    schema_prefix: str | None = None


KNOWN_WORKBENCH_ARTIFACTS = (
    WorkbenchArtifact(WORKBENCH_SUMMARY_JSON, "workbench_public_summary", "Workbench public summary", "scan-qc.workbench-public-summary."),
    WorkbenchArtifact("run_plan_summary.json", "run_plan", "Run-plan summary", "scan-qc.run-plan-summary."),
    WorkbenchArtifact(
        "aggregate_baseline_summary.json",
        "aggregate_baseline",
        "Aggregate baseline summary",
        "scan-qc.aggregate-baseline.",
    ),
    WorkbenchArtifact(
        "aggregate_evidence_bundle_summary.json",
        "aggregate_evidence_bundle",
        "Aggregate evidence bundle summary",
        "scan-qc.aggregate-evidence-bundle.",
    ),
    WorkbenchArtifact(
        "final_production_handoff_summary.json",
        "final_production_handoff",
        "Final production handoff summary",
        "scan-qc.final-production-handoff-summary.",
    ),
    WorkbenchArtifact(
        "release_candidate_summary.json",
        "release_candidate",
        "Release candidate summary",
        "scan-qc.release-candidate-summary.",
    ),
    WorkbenchArtifact("release_readiness_summary.json", "release_readiness", "Release readiness summary", "scan-qc.release-readiness."),
    WorkbenchArtifact("acceptance_summary.json", "acceptance", "Acceptance summary", "scan-qc.acceptance-summary."),
    WorkbenchArtifact("review_summary.json", "review", "Review summary", "scan-qc.review-summary."),
    WorkbenchArtifact(
        "review_decision_verification_summary.json",
        "review_decision_verification",
        "Review-decision verification summary",
        "scan-qc.review-decision-verification-summary.",
    ),
    WorkbenchArtifact(
        "deep_inspection_candidate_summary.json",
        "deep_inspection_candidates",
        "Deep-inspection candidate summary",
        "scan-qc.deep-inspection-candidates.",
    ),
    WorkbenchArtifact(
        "deep_inspection_provider_probe.json",
        "deep_inspection_provider_probe",
        "Deep-inspection provider probe",
        "scan-qc.deep-inspection-provider.",
    ),
    WorkbenchArtifact(
        "frontend_workbench_validation.json",
        "frontend_workbench_validation",
        "Frontend workbench validation summary",
        "scan-qc.frontend-workbench-validation.",
    ),
    WorkbenchArtifact("capability_probe.json", "provider_capability_probe", "Provider capability probe", "scan-qc.capability-probe."),
    WorkbenchArtifact(
        "public_safe_validation_index.json",
        "public_safe_validation_index",
        "Public-safe validation index",
        "scan-qc.public-safe-validation-index.",
    ),
    WorkbenchArtifact(
        "artifact_readiness_checklist.json",
        "artifact_readiness_checklist",
        "Public-safe artifact readiness checklist",
        "scan-qc-artifact-readiness-checklist.",
    ),
    WorkbenchArtifact(
        "public_safe_artifact_readiness.json",
        "artifact_readiness_checklist",
        "Public-safe artifact readiness checklist",
        "scan-qc-artifact-readiness-checklist.",
    ),
)

_KNOWN_BY_NAME = {artifact.name: artifact for artifact in KNOWN_WORKBENCH_ARTIFACTS}

_DISALLOWED_INPUT_NAMES = {
    "scan_qc_report.json",
    "scan_qc_report.csv",
    "scan_qc_report.html",
    "scan_qc_files.csv",
    "scan_qc_findings.csv",
    "processing_manifest.json",
    "processing_retry_manifest.json",
    "processing_review_package.json",
    "review_template.csv",
    "review_template.json",
    "acceptance_sampling_review.json",
    "acceptance_sampling_review.csv",
}

_PRIVATE_SUFFIXES = {
    ".bmp",
    ".csv",
    ".gif",
    ".jpeg",
    ".jpg",
    ".jp2",
    ".jsonl",
    ".log",
    ".pdf",
    ".png",
    ".tif",
    ".tiff",
    ".txt",
}


def write_workbench_public_summary(
    *,
    evidence_dir: Path | None = None,
    files: list[Path] | None = None,
    output_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    summary = build_workbench_public_summary(evidence_dir=evidence_dir, files=files)
    path = output_path or ((evidence_dir or Path.cwd()) / WORKBENCH_SUMMARY_JSON)
    if path.suffix == "":
        path = path / WORKBENCH_SUMMARY_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, summary


def build_workbench_public_summary(
    *,
    evidence_dir: Path | None = None,
    files: list[Path] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if evidence_dir is None and not files:
        raise ValueError("provide --evidence-dir or at least one --file aggregate JSON input.")
    if evidence_dir is not None and files:
        raise ValueError("use either --evidence-dir or --file inputs, not both.")

    explicit_mode = bool(files)
    paths_by_name, rejected_inputs = _paths_by_name(evidence_dir=evidence_dir, files=files or [])
    artifacts: dict[str, Any] = {}
    blocking_items: list[dict[str, str]] = []
    warning_items: list[dict[str, str]] = []
    checks_passed = 0
    checks_failed = 0
    privacy_failure_count = 0
    present_count = 0
    passed_count = 0
    failed_count = 0
    missing_count = 0
    processing_reuse_counts: dict[str, int] = {}
    processing_operation_timings: dict[str, Any] = {}
    ready_signals: list[bool] = []

    for expected in KNOWN_WORKBENCH_ARTIFACTS:
        path = paths_by_name.get(expected.name)
        record, blockers, warnings = _artifact_record(path, expected, explicit_mode=explicit_mode)
        artifacts[expected.name] = record
        blocking_items.extend(blockers)
        warning_items.extend(warnings)
        checks_passed += record["checks_passed"]
        checks_failed += record["checks_failed"]
        privacy_failure_count += record["privacy"]["failure_count"]
        present_count += int(record["present"])
        passed_count += int(record["status"] == "pass")
        failed_count += int(record["status"] == "fail")
        missing_count += int(record["status"] in {"missing", "not_provided"})
        _merge_processing_reuse_counts(processing_reuse_counts, record["metrics"])
        _merge_processing_operation_timings(processing_operation_timings, record["metrics"])
        if record["ready"] is not None:
            ready_signals.append(bool(record["ready"]))

    for _name, code in rejected_inputs:
        blocking_items.append({"artifact": _UNSUPPORTED_INPUT_ARTIFACT, "category": "unsupported", "code": code})
        checks_failed += 1

    blocking_counts_by_code = _counts_by_code(blocking_items)
    warning_counts_by_code = _counts_by_code(warning_items)
    status = "pass" if checks_failed == 0 and not blocking_items else "fail"
    ready = (status == "pass" and all(ready_signals)) if ready_signals else None

    summary_metrics = {
        "known_artifacts": len(KNOWN_WORKBENCH_ARTIFACTS),
        "artifacts_present": present_count,
        "artifacts_passed": passed_count,
        "artifacts_failed": failed_count,
        "artifacts_missing": missing_count,
        "unsupported_inputs": len(rejected_inputs),
        **processing_reuse_counts,
    }
    if processing_operation_timings:
        summary_metrics["processing_operation_timings"] = processing_operation_timings
    closure_metrics = _human_review_closure_metrics(artifacts)
    if closure_metrics:
        summary_metrics["human_review_closure"] = closure_metrics
    readiness_metrics = _deep_inspection_readiness_metrics(artifacts)
    summary_metrics.update(readiness_metrics)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "ready": ready,
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "blocking_item_count": len(blocking_items),
        "warning_item_count": len(warning_items),
        "blocking_counts_by_code": blocking_counts_by_code,
        "warning_counts_by_code": warning_counts_by_code,
        "blocking_items": blocking_items,
        "warning_items": warning_items,
        "summary": summary_metrics,
        "workflow_state": _workflow_state(artifacts),
        "artifact_presence": {
            name: {
                "present": record["present"],
                "category": record["category"],
                "label": record["label"],
                "status": record["status"],
                "reported_status": record["reported_status"],
                "ready": record["ready"],
            }
            for name, record in artifacts.items()
        },
        "artifacts": artifacts,
        "privacy": {
            "status": "pass" if privacy_failure_count == 0 and not rejected_inputs else "fail",
            "aggregate_only": privacy_failure_count == 0 and not rejected_inputs,
            "private_indicators_found": privacy_failure_count > 0,
            "private_indicator_count": privacy_failure_count,
            "unsupported_private_input_count": len(rejected_inputs),
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
            "contains_manifests": False,
            "contains_derivative_references": False,
            "contains_provider_command_strings": False,
            "contains_environment_values": False,
            "redacts_private_values": True,
        },
        "sensitive_values_omitted": True,
    }


def _paths_by_name(*, evidence_dir: Path | None, files: list[Path]) -> tuple[dict[str, Path], list[tuple[str, str]]]:
    if evidence_dir is not None:
        return ({artifact.name: evidence_dir / artifact.name for artifact in KNOWN_WORKBENCH_ARTIFACTS}, [])

    result: dict[str, Path] = {}
    rejected: list[tuple[str, str]] = []
    for path in files:
        name = path.name
        if name in result:
            raise ValueError(f"duplicate aggregate JSON input: {name}")
        if name in _KNOWN_BY_NAME:
            result[name] = path
        else:
            rejected.append((name, _unsupported_code(name)))
    return result, rejected


def _artifact_record(
    path: Path | None,
    expected: WorkbenchArtifact,
    *,
    explicit_mode: bool,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    base = {
        "category": expected.category,
        "label": expected.label,
        "present": False,
        "status": "not_provided" if explicit_mode else "missing",
        "reported_status": None,
        "schema_version": None,
        "ready": None,
        "checks_passed": 0,
        "checks_failed": 0,
        "blocking_item_count": 0,
        "warning_item_count": 0,
        "blocking_counts_by_code": {},
        "warning_counts_by_code": {},
        "metrics": {},
        "privacy": {"status": "not_checked", "failure_count": 0, "failure_codes": []},
    }
    if path is None:
        return base, [], []
    if not path.exists():
        return base, [], []
    if not path.is_file():
        base.update({"present": True, "status": "fail", "checks_failed": 1})
        blocker = _blocker(expected, "aggregate_input_not_file")
        return base, [blocker], []

    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        base.update({"present": True, "status": "fail", "checks_failed": 1})
        return base, [_blocker(expected, "malformed_json")], []
    if not isinstance(payload, dict):
        base.update({"present": True, "status": "fail", "checks_failed": 1})
        return base, [_blocker(expected, "json_root_not_object")], []

    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    schema = payload.get("schema_version")
    if isinstance(schema, str):
        base["schema_version"] = schema
    if expected.schema_prefix and (not isinstance(schema, str) or not schema.startswith(expected.schema_prefix)):
        if not (expected.name == "frontend_workbench_validation.json" and _is_frontend_workbench_validation_payload(payload)):
            blockers.append(_blocker(expected, "schema_version_unexpected"))

    reported_status = _normalized_status(payload)
    if reported_status is None:
        if expected.name == "aggregate_baseline_summary.json" and _aggregate_baseline_infers_pass(payload):
            reported_status = "pass"
        elif expected.name == "run_plan_summary.json":
            reported_status = _run_plan_inferred_status(payload)
    base["reported_status"] = reported_status
    if reported_status in _FAIL_STATUSES:
        blockers.append(_blocker(expected, "artifact_status_failed"))
    elif reported_status not in _PASS_STATUSES:
        blockers.append(_blocker(expected, "artifact_status_unknown"))

    privacy_codes = _privacy_failures(payload, raw)
    blockers.extend(_blocker(expected, code) for code in privacy_codes)
    blockers.extend(_blocker(expected, code) for code in _blocking_codes(payload))
    warnings.extend(_warning(expected, code) for code in _warning_codes(payload))

    nested_failed = _extract_int(payload, "checks_failed")
    checks_failed = len(blockers) + nested_failed
    checks_passed = _extract_int(payload, "checks_passed")
    if checks_passed == 0 and checks_failed == 0:
        checks_passed = 1

    base.update(
        {
            "present": True,
            "status": "pass" if checks_failed == 0 else "fail",
            "ready": _ready_signal(payload),
            "checks_passed": checks_passed,
            "checks_failed": checks_failed,
            "blocking_item_count": len(blockers),
            "warning_item_count": len(warnings),
            "blocking_counts_by_code": _counts_by_code(blockers),
            "warning_counts_by_code": _counts_by_code(warnings),
            "metrics": _metrics(payload, expected),
            "privacy": {
                "status": "pass" if not privacy_codes else "fail",
                "failure_count": len(privacy_codes),
                "failure_codes": privacy_codes,
            },
        }
    )
    return base, blockers, warnings


def _metrics(payload: dict[str, Any], expected: WorkbenchArtifact) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "blocking_item_count": _count_from_payload(payload, "blocking_item_count", "blocking_items"),
        "warning_item_count": _count_from_payload(payload, "warning_item_count", "warnings"),
    }
    metrics.update(_processing_reuse_counts(payload))
    metrics.update(_processing_operation_timings(payload))
    for key in ("checks_passed", "checks_failed"):
        value = _extract_int(payload, key)
        if value:
            metrics[key] = value
    if expected.name == "public_safe_validation_index.json":
        summary = payload.get("summary")
        if isinstance(summary, dict):
            for key in ("artifacts_present", "artifacts_passed", "artifacts_failed", "artifacts_missing"):
                metrics[key] = _safe_int(summary.get(key))
    if expected.name == "frontend_workbench_validation.json":
        for key in ("error_count",):
            metrics[key] = _safe_int(payload.get(key))
        counts = payload.get("counts")
        if isinstance(counts, dict):
            metrics["validator_counts"] = _safe_count_map(counts)
        fixture_groups = payload.get("fixture_groups")
        if isinstance(fixture_groups, dict):
            metrics["fixture_groups"] = _safe_count_map(fixture_groups)
    if expected.name == WORKBENCH_SUMMARY_JSON:
        summary = payload.get("summary")
        if isinstance(summary, dict):
            for key in (
                "known_artifacts",
                "artifacts_present",
                "artifacts_passed",
                "artifacts_failed",
                "artifacts_missing",
                "unsupported_inputs",
            ):
                metrics[key] = _safe_int(summary.get(key))
    if expected.name == "deep_inspection_candidate_summary.json":
        for key in ("candidate_total", "provider_count"):
            metrics[key] = _safe_int(payload.get(key))
        metrics["candidates_by_reason"] = _safe_count_map(payload.get("candidates_by_reason"))
        metrics["candidates_by_severity"] = _safe_count_map(payload.get("candidates_by_severity"))
        if isinstance(payload.get("provider_configured"), bool):
            metrics["provider_configured"] = payload["provider_configured"]
        if isinstance(payload.get("privacy_status"), str) and _safe_code(payload["privacy_status"]):
            metrics["privacy_status"] = payload["privacy_status"]
        if isinstance(payload.get("no_inference_run"), bool):
            metrics["no_inference_run"] = payload["no_inference_run"]
    if expected.name in {"capability_probe.json", "deep_inspection_provider_probe.json"}:
        readiness = payload.get("readiness")
        visibility = payload.get("gpu_provider_visibility")
        privacy = payload.get("privacy")
        configuration = payload.get("configuration")
        optional_packages = payload.get("optional_packages")
        metrics["provider_count"] = _safe_int(payload.get("provider_count"))
        if isinstance(payload.get("configured"), bool):
            metrics["provider_configured"] = payload["configured"]
            metrics["configured_provider_count"] = metrics["provider_count"] if payload["configured"] else 0
            metrics["disabled_provider_count"] = 0 if payload["configured"] else metrics["provider_count"]
            metrics["configuration_status"] = "configured" if payload["configured"] else "not_configured"
        if isinstance(payload.get("no_inference_run"), bool):
            metrics["no_inference_run"] = payload["no_inference_run"]
        if isinstance(readiness, dict):
            providers = readiness.get("provider_packages_found")
            metrics["provider_packages_found_count"] = len(providers) if isinstance(providers, list) else 0
            for key in ("gpu_acceleration_configured", "model_acceleration_configured"):
                if isinstance(readiness.get(key), bool):
                    metrics[key] = readiness[key]
        if isinstance(visibility, dict):
            metrics["gpu_visible_count"] = _safe_int(visibility.get("gpu_visible_count"))
            torch_cuda = visibility.get("torch_cuda")
            if isinstance(torch_cuda, dict):
                metrics["visible_model_count"] = _safe_int(torch_cuda.get("visible_count"))
        if isinstance(configuration, dict):
            if isinstance(configuration.get("any_provider_configured"), bool):
                metrics["providers_configured"] = configuration["any_provider_configured"]
            configured_parts = [
                key
                for key in ("analysis_provider_configured", "gpu_acceleration_configured", "model_acceleration_configured")
                if configuration.get(key) is True
            ]
            metrics["configuration_status"] = "configured" if configured_parts else "not_configured"
        if isinstance(optional_packages, dict):
            visible_count = sum(1 for item in optional_packages.values() if isinstance(item, dict) and item.get("available") is True)
            missing_count = sum(1 for item in optional_packages.values() if isinstance(item, dict) and item.get("available") is False)
            metrics["optional_package_visible_count"] = visible_count
            metrics["optional_package_missing_count"] = missing_count
        if isinstance(privacy, dict) and privacy.get("aggregate_only") is True:
            metrics["privacy_status"] = "aggregate_public_safe"
    if expected.name in {"artifact_readiness_checklist.json", "public_safe_artifact_readiness.json"}:
        checklist = payload.get("artifact_readiness_checklist") or payload.get("public_safe_artifact_readiness")
        if isinstance(checklist, dict):
            for key in ("ready", "missing_count", "blocking_count", "warning_count", "stale_count"):
                value = checklist.get(key)
                if isinstance(value, bool) or isinstance(value, int):
                    metrics[key] = value
        elif isinstance(checklist, list):
            metrics["checklist_item_count"] = len(checklist)
            metrics["missing_count"] = sum(1 for item in checklist if isinstance(item, dict) and item.get("status") == "missing")
    if expected.name == "review_summary.json":
        metrics.update(_review_closure_metrics(payload))
    if expected.name == "acceptance_summary.json":
        metrics.update(_acceptance_closure_metrics(payload))
    return {key: value for key, value in metrics.items() if value is not None}


def _human_review_closure_metrics(artifacts: dict[str, Any]) -> dict[str, Any]:
    review = _artifact_metrics(artifacts, "review_summary.json")
    acceptance = _artifact_metrics(artifacts, "acceptance_summary.json")
    result = _clean_optional_metrics(
        {
            "review_present": artifacts["review_summary.json"].get("present"),
            "review_status": artifacts["review_summary.json"].get("reported_status"),
            "acceptance_present": artifacts["acceptance_summary.json"].get("present"),
            "acceptance_status": artifacts["acceptance_summary.json"].get("reported_status"),
            "total_findings": _first_present(review.get("total_findings"), acceptance.get("human_review_total_findings")),
            "remaining_p0": _first_present(review.get("remaining_p0"), acceptance.get("human_review_remaining_p0")),
            "remaining_p1": _first_present(review.get("remaining_p1"), acceptance.get("human_review_remaining_p1")),
            "status_counts": _first_present(review.get("status_counts"), acceptance.get("human_review_status_counts")),
            "severity_counts": review.get("severity_counts"),
            "severity_status_counts": review.get("severity_status_counts"),
            "rule_status_counts": review.get("rule_status_counts"),
            "acceptance_passed": _first_present(review.get("acceptance_passed"), acceptance.get("acceptance_passed")),
            "acceptance_pass": acceptance.get("pass"),
            "blocking_item_count": acceptance.get("blocking_item_count"),
            "warning_item_count": acceptance.get("warning_item_count"),
            "privacy_status": _first_present(review.get("privacy_status"), acceptance.get("privacy_status")),
        }
    )
    if not artifacts["review_summary.json"].get("present") and not artifacts["acceptance_summary.json"].get("present"):
        return {}
    return result


def _review_closure_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    return _clean_optional_metrics(
        {
            "total_findings": _safe_int(payload.get("total_findings")),
            "remaining_p0": _safe_int(payload.get("remaining_p0")),
            "remaining_p1": _safe_int(payload.get("remaining_p1")),
            "status_counts": _safe_count_map(payload.get("status_counts")),
            "severity_counts": _safe_count_map(payload.get("severity_counts")),
            "severity_status_counts": _safe_nested_count_map(payload.get("severity_status_counts")),
            "rule_status_counts": _safe_nested_count_map(payload.get("rule_status_counts")),
            "acceptance_passed": payload.get("acceptance_passed") if isinstance(payload.get("acceptance_passed"), bool) else None,
            "privacy_status": _privacy_status_code(payload),
        }
    )


def _acceptance_closure_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    human_review = payload.get("human_review")
    review_metrics = human_review if isinstance(human_review, dict) else {}
    return _clean_optional_metrics(
        {
            "pass": payload.get("pass") if isinstance(payload.get("pass"), bool) else None,
            "acceptance_passed": payload.get("acceptance_passed") if isinstance(payload.get("acceptance_passed"), bool) else payload.get("pass") if isinstance(payload.get("pass"), bool) else None,
            "blocking_item_count": _safe_int(payload.get("blocking_item_count")),
            "warning_item_count": _safe_int(payload.get("warning_item_count")),
            "human_review_total_findings": _safe_int(review_metrics.get("total_findings")),
            "human_review_remaining_p0": _safe_int(review_metrics.get("remaining_p0")),
            "human_review_remaining_p1": _safe_int(review_metrics.get("remaining_p1")),
            "human_review_status_counts": _safe_count_map(review_metrics.get("status_counts")),
            "privacy_status": _privacy_status_code(payload),
        }
    )


def _privacy_status_code(payload: dict[str, Any]) -> str | None:
    privacy = payload.get("privacy")
    if isinstance(privacy, dict):
        status = privacy.get("status")
        if isinstance(status, str) and _safe_metric_key(status):
            return status
        if privacy.get("aggregate_only") is True:
            return "aggregate_public_safe"
    status = payload.get("privacy_status")
    if isinstance(status, str) and _safe_metric_key(status):
        return status
    return None


def _processing_reuse_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {
        key: value
        for key in _PROCESSING_REUSE_COUNTER_KEYS
        if (value := _extract_optional_int(payload, key)) is not None
    }


def _merge_processing_reuse_counts(target: dict[str, int], metrics: dict[str, Any]) -> None:
    for key in _PROCESSING_REUSE_COUNTER_KEYS:
        value = _safe_int(metrics.get(key))
        if value is None:
            continue
        if key not in target or value > target[key]:
            target[key] = value


def _processing_operation_timings(payload: dict[str, Any]) -> dict[str, Any]:
    timings = _extract_processing_operation_timings(payload)
    if not timings:
        return {}
    return {"processing_operation_timings": timings}


def _extract_processing_operation_timings(payload: dict[str, Any]) -> dict[str, Any]:
    for timings in _processing_operation_timing_candidates(payload):
        sanitized = {}
        for operation in _PROCESSING_OPERATION_NAMES:
            timing = _sanitize_processing_operation_timing(operation, timings.get(operation))
            if timing:
                sanitized[operation] = timing
        if sanitized:
            return sanitized
    return {}


def _processing_operation_timing_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for container_key in ("summary", "throughput", "timing"):
        container = payload.get(container_key)
        if not isinstance(container, dict):
            continue
        timings = container.get("processing_operation_timings") or container.get("operation_timings")
        if isinstance(timings, dict):
            candidates.append(timings)
    timings = payload.get("processing_operation_timings") or payload.get("operation_timings")
    if isinstance(timings, dict):
        candidates.append(timings)
    return candidates


def _sanitize_processing_operation_timing(operation: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    if isinstance(value.get("enabled"), bool):
        result["enabled"] = value["enabled"]
    file_count = _safe_int(value.get("file_count"))
    if file_count is not None:
        result["file_count"] = file_count
    for key in ("elapsed_seconds", "files_per_minute", "average_seconds_per_file"):
        parsed = _safe_float(value.get(key))
        if parsed is not None:
            result[key] = parsed
    if operation == "despeckle":
        result.update(_sanitize_despeckle_backend_timing(value))
    allowed_fields = _PROCESSING_OPERATION_FIELDS + (_DESPECKLE_BACKEND_FIELDS if operation == "despeckle" else ())
    return {key: result[key] for key in allowed_fields if key in result}


def _sanitize_despeckle_backend_timing(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    backend_mode = value.get("backend_mode")
    if isinstance(backend_mode, str) and _safe_code(backend_mode):
        result["backend_mode"] = backend_mode
    if isinstance(value.get("numpy_available"), bool):
        result["numpy_available"] = value["numpy_available"]
    backend_counts = {key: count for key, count in _safe_count_map(value.get("backend_counts")).items() if _safe_code(key)}
    if backend_counts:
        result["backend_counts"] = backend_counts
    return {key: result[key] for key in _DESPECKLE_BACKEND_FIELDS if key in result}


def _merge_processing_operation_timings(target: dict[str, Any], metrics: dict[str, Any]) -> None:
    timings = metrics.get("processing_operation_timings")
    if not isinstance(timings, dict):
        return
    for operation in _PROCESSING_OPERATION_NAMES:
        timing = timings.get(operation)
        if isinstance(timing, dict):
            target[operation] = dict(timing)


def _deep_inspection_readiness_metrics(artifacts: dict[str, Any]) -> dict[str, Any]:
    candidate = _artifact_metrics(artifacts, "deep_inspection_candidate_summary.json")
    provider_probe = _artifact_metrics(artifacts, "deep_inspection_provider_probe.json")
    capability_probe = _artifact_metrics(artifacts, "capability_probe.json")
    result: dict[str, Any] = {}

    deep_inspection = _clean_optional_metrics(
        {
            "status": artifacts["deep_inspection_candidate_summary.json"].get("reported_status"),
            "candidate_total": candidate.get("candidate_total"),
            "candidates_by_reason": candidate.get("candidates_by_reason"),
            "candidates_by_severity": candidate.get("candidates_by_severity"),
            "provider_configured": _first_present(candidate.get("provider_configured"), provider_probe.get("provider_configured")),
            "provider_count": _first_present(candidate.get("provider_count"), provider_probe.get("provider_count")),
            "checks_passed": candidate.get("checks_passed"),
            "checks_failed": candidate.get("checks_failed"),
            "privacy_status": candidate.get("privacy_status"),
            "no_inference_run": _first_present(candidate.get("no_inference_run"), provider_probe.get("no_inference_run")),
            "configuration_status": provider_probe.get("configuration_status"),
        }
    )
    if deep_inspection:
        result["deep_inspection_readiness"] = deep_inspection

    provider_capability = _clean_optional_metrics(
        {
            "provider_count": _first_present(provider_probe.get("provider_count"), capability_probe.get("provider_count")),
            "configured_provider_count": provider_probe.get("configured_provider_count"),
            "disabled_provider_count": provider_probe.get("disabled_provider_count"),
            "providers_configured": _first_present(provider_probe.get("provider_configured"), capability_probe.get("providers_configured")),
            "provider_packages_found_count": capability_probe.get("provider_packages_found_count"),
            "optional_package_visible_count": capability_probe.get("optional_package_visible_count"),
            "optional_package_missing_count": capability_probe.get("optional_package_missing_count"),
            "visible_gpu_count": capability_probe.get("gpu_visible_count"),
            "visible_model_count": capability_probe.get("visible_model_count"),
            "gpu_acceleration_configured": capability_probe.get("gpu_acceleration_configured"),
            "model_acceleration_configured": capability_probe.get("model_acceleration_configured"),
            "privacy_status": _first_present(capability_probe.get("privacy_status"), provider_probe.get("privacy_status")),
            "no_inference_run": _first_present(capability_probe.get("no_inference_run"), provider_probe.get("no_inference_run")),
            "configuration_status": capability_probe.get("configuration_status"),
        }
    )
    if provider_capability:
        result["provider_capability_readiness"] = provider_capability

    return result


def _artifact_metrics(artifacts: dict[str, Any], name: str) -> dict[str, Any]:
    record = artifacts.get(name)
    metrics = record.get("metrics") if isinstance(record, dict) else None
    return metrics if isinstance(metrics, dict) else {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _clean_optional_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if value is not None and value != {}}


def _aggregate_baseline_infers_pass(payload: dict[str, Any]) -> bool:
    if not isinstance(payload.get("aggregate_counts"), dict):
        return False
    privacy = payload.get("privacy")
    if isinstance(privacy, dict) and privacy.get("aggregate_only") is False:
        return False
    return True


def _run_plan_inferred_status(payload: dict[str, Any]) -> str | None:
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return None
    failed_batches = _safe_int(summary.get("failed_batches"))
    if failed_batches is None:
        return "pass"
    return "fail" if failed_batches > 0 else "pass"


def _workflow_state(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "acceptance_status": artifacts["acceptance_summary.json"]["reported_status"],
        "release_readiness_status": artifacts["release_readiness_summary.json"]["reported_status"],
        "release_candidate_status": artifacts["release_candidate_summary.json"]["reported_status"],
        "handoff_status": artifacts["final_production_handoff_summary.json"]["reported_status"],
        "validation_index_status": artifacts["public_safe_validation_index.json"]["reported_status"],
        "deep_inspection_status": artifacts["deep_inspection_candidate_summary.json"]["reported_status"],
    }


def _blocking_codes(payload: dict[str, Any]) -> list[str]:
    return _coded_items(payload.get("blocking_items")) + _coded_items(payload.get("checks_failed"))


def _warning_codes(payload: dict[str, Any]) -> list[str]:
    return _coded_items(payload.get("warning_items")) + _coded_items(payload.get("warnings"))


def _coded_items(value: Any) -> list[str]:
    if isinstance(value, int):
        return ["aggregate_item_present"] if value > 0 else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("code"), str):
            result.append(item["code"])
        elif isinstance(item, str):
            result.append(item if _safe_code(item) else "aggregate_item_present")
        else:
            result.append("aggregate_item_present")
    return result


def _safe_code(value: str) -> bool:
    return bool(value) and all(char.islower() or char.isdigit() or char == "_" for char in value)


def _normalized_status(payload: dict[str, Any]) -> str | None:
    status = payload.get("status")
    if isinstance(status, str):
        return status.lower()
    if payload.get("pass") is True or payload.get("passed") is True:
        return "pass"
    if payload.get("pass") is False or payload.get("passed") is False:
        return "fail"
    return None


def _is_frontend_workbench_validation_payload(payload: dict[str, Any]) -> bool:
    return (
        isinstance(payload.get("status"), str)
        and isinstance(payload.get("error_count"), int)
        and isinstance(payload.get("errors"), list)
        and isinstance(payload.get("counts"), dict)
        and isinstance(payload.get("privacy"), dict)
    )


def _ready_signal(payload: dict[str, Any]) -> bool | None:
    for key in ("ready", "ready_for_handoff", "ready_for_release_candidate", "pass", "acceptance_passed"):
        if isinstance(payload.get(key), bool):
            return payload[key]
    checklist = payload.get("artifact_readiness_checklist") or payload.get("public_safe_artifact_readiness")
    if isinstance(checklist, dict) and isinstance(checklist.get("ready"), bool):
        return checklist["ready"]
    return None


def _count_from_payload(payload: dict[str, Any], count_key: str, list_key: str) -> int:
    count = _extract_int(payload, count_key)
    if count:
        return count
    value = payload.get(list_key)
    return len(value) if isinstance(value, list) else 0


def _counts_by_code(items: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        code = item["code"]
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def _extract_int(payload: Any, key: str) -> int:
    value = _extract_optional_int(payload, key)
    return value if value is not None else 0


def _extract_optional_int(payload: Any, key: str) -> int | None:
    if isinstance(payload, dict):
        value = payload.get(key)
        parsed = _safe_int(value)
        if parsed is not None:
            return parsed
        for child in payload.values():
            found = _extract_optional_int(child, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = _extract_optional_int(child, key)
            if found is not None:
                return found
    return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value >= 0:
        return float(value)
    return None


def _safe_count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, count in value.items():
        parsed = _safe_int(count)
        if isinstance(key, str) and _safe_metric_key(key) and parsed is not None:
            result[key] = parsed
    return dict(sorted(result.items()))


def _safe_nested_count_map(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, int]] = {}
    for key, counts in value.items():
        nested = _safe_count_map(counts)
        if isinstance(key, str) and _safe_metric_key(key) and nested:
            result[key] = nested
    return dict(sorted(result.items()))


def _safe_metric_key(value: str) -> bool:
    if not value or len(value) > 80:
        return False
    if len(value) >= 32 and all(char.lower() in "0123456789abcdef" for char in value):
        return False
    return all(char.isalnum() or char in {"_", "-"} for char in value)


def _unsupported_code(name: str) -> str:
    lowered = name.lower()
    if lowered in _DISALLOWED_INPUT_NAMES:
        return "unsupported_private_input_rejected"
    if any(token in lowered for token in ("manifest", "report", "row", "ocr", "hash", "thumbnail", "image", "provider_log", "command", "environment")):
        return "unsupported_private_looking_input_rejected"
    if Path(lowered).suffix in _PRIVATE_SUFFIXES:
        return "unsupported_private_looking_input_rejected"
    return "unsupported_aggregate_input_rejected"


def _blocker(expected: WorkbenchArtifact, code: str) -> dict[str, str]:
    return {"artifact": expected.name, "category": expected.category, "code": code}


def _warning(expected: WorkbenchArtifact, code: str) -> dict[str, str]:
    return {"artifact": expected.name, "category": expected.category, "code": code}
