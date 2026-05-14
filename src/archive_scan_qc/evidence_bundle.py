"""Aggregate evidence bundle verifier for production handoff."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


EVIDENCE_BUNDLE_JSON = "aggregate_evidence_bundle_summary.json"
SCHEMA_VERSION = "scan-qc.aggregate-evidence-bundle.v1"

_PASS_STATUSES = {"pass", "passed", "ok", "success"}
_FAIL_STATUSES = {"fail", "failed", "error", "blocked"}
_PRIVATE_KEYS = (
    "absolute_path",
    "content",
    "file",
    "filename",
    "files",
    "finding",
    "findings",
    "hash",
    "image",
    "manifest",
    "ocr",
    "path",
    "relative_path",
    "root",
    "sample",
    "secret",
    "sha",
    "source",
    "text",
    "thumbnail",
    "token",
)
_ALLOWED_KEY_EXCEPTIONS = {
    "aggregate_only",
    "contains_environment_values",
    "contains_derivative_image_references",
    "contains_file_list",
    "contains_filenames",
    "contains_hashes",
    "contains_image_content",
    "contains_manifests",
    "contains_ocr_text",
    "contains_paths",
    "contains_reviewer_notes",
    "contains_row_level_evidence",
    "contains_row_level_findings",
    "contains_secrets",
    "contains_source_roots",
    "contains_thumbnails",
    "configuration_sources",
    "model_inference_run",
    "no_inference_run",
    "omits",
    "openable_files",
    "optional_packages",
    "paddleocr",
    "private_image_paths",
    "provider_packages_found",
    "provider_packages_found_count",
    "python_version_family",
    "retained_public_summary",
    "scan_processing_semantics",
    "source_values_omitted",
    "total_files",
    "total_findings",
}
_ALLOWED_NUMERIC_KEY_SUFFIXES = (
    "files_per_minute",
    "openable_files_per_minute",
    "processed_files_per_minute",
    "benchmark_files_per_minute",
    "benchmark_processed_files_per_minute",
    "average_seconds_per_file",
)
_ALLOWED_NUMERIC_KEYS = {
    "min_scan_files_per_minute",
    "min_processing_files_per_minute",
    "file_count",
    "processing_failed_files",
    "processing_failed_files_max",
    "processing_processed_files",
    "processing_resumed_files",
    "processing_duplicate_reused_files",
    "processing_existing_derivative_reused_files",
    "processing_scan_measurement_reused_files",
    "reused_scan_measurement_files",
    "safe_skip_files",
    "projection_detection_files",
    "fallback_detection_files",
    "deskew_safe_skip_files",
    "deskew_projection_detection_files",
    "deskew_fallback_detection_files",
}
_COUNT_SUMMARY_KEYS = {
    "average",
    "best_observed",
    "count",
    "file_count",
    "lowest_observed",
    "max",
    "mean",
    "median",
    "min",
    "provided",
    "status",
    "threshold",
    "total",
    "value",
}
_PRIVATE_VALUE_PATTERNS = (
    ("absolute_path", re.compile(r"(^|[\s\"'])((/[A-Za-z0-9_.~ -]+)+|[A-Za-z]:\\[^\"'\s]+)")),
    ("filename", re.compile(r"\b[^/\\\s\"']+\.(?:png|jpe?g|tiff?|bmp|gif|jp2|pdf|csv)\b", re.IGNORECASE)),
    ("hash", re.compile(r"\b[a-f0-9]{32,128}\b", re.IGNORECASE)),
    ("data_uri", re.compile(r"data:image/", re.IGNORECASE)),
    ("secret", re.compile(r"\b(?:api[_-]?key|secret|token|password|bearer)\b", re.IGNORECASE)),
)
_AGGREGATE_BASELINE_COUNTER_PATHS = {
    ("aggregate_counts", "p0_findings"),
    ("aggregate_counts", "p1_findings"),
    ("aggregate_counts", "p2_findings"),
    ("aggregate_counts", "processing_failed_files"),
    ("benchmark", "worker_sweep", "workers", "[]", "processing", "failed_files"),
}
_AGGREGATE_BENCHMARK_SOURCES = {
    "aggregate_baseline",
    "aggregate baseline",
    "benchmark repeated worker runs",
    "run_aggregate_baseline",
    "archive_scan_qc.acceptance.build_acceptance_summary",
}
_REVIEW_DECISION_SOURCE_SCHEMA = "scan-qc-review-decisions.local.v1"
_REVIEW_DECISION_SOURCE_TYPE = "aggregate_handoff"


@dataclass(frozen=True)
class ExpectedArtifact:
    name: str
    required: bool
    schema_prefix: str
    status_path: tuple[str, ...] = ("status",)


EXPECTED_ARTIFACTS = (
    ExpectedArtifact("release_candidate_summary.json", True, "scan-qc.release-candidate-summary."),
    ExpectedArtifact("release_readiness_summary.json", False, "scan-qc.release-readiness."),
    ExpectedArtifact("acceptance_summary.json", False, "scan-qc.acceptance-summary."),
    ExpectedArtifact("aggregate_baseline_summary.json", False, "scan-qc.aggregate-baseline."),
    ExpectedArtifact("capability_probe.json", False, "scan-qc.capability-probe."),
    ExpectedArtifact("deep_inspection_provider_probe.json", False, "scan-qc.deep-inspection-provider."),
    ExpectedArtifact("deep_inspection_candidate_summary.json", False, "scan-qc.deep-inspection-candidates."),
    ExpectedArtifact("review_decision_verification_summary.json", False, "scan-qc.review-decision-verification-summary."),
)


def write_evidence_bundle_summary(evidence_dir: Path, output_path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    summary = build_evidence_bundle_summary(evidence_dir)
    path = output_path or evidence_dir / EVIDENCE_BUNDLE_JSON
    if path.suffix == "":
        path = path / EVIDENCE_BUNDLE_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, summary


def build_evidence_bundle_summary(evidence_dir: Path, *, generated_at: str | None = None) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    blocking_items: list[dict[str, Any]] = []
    checks_passed = 0
    checks_failed = 0
    privacy_violation_count = 0

    for expected in EXPECTED_ARTIFACTS:
        path = evidence_dir / expected.name
        record, passed, failed, blockers, privacy_failures = _verify_artifact(path, expected)
        artifacts[expected.name] = record
        checks_passed += passed
        checks_failed += failed
        blocking_items.extend(blockers)
        privacy_violation_count += privacy_failures

    status = "pass" if checks_failed == 0 and not blocking_items else "fail"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "blocking_items": blocking_items,
        "artifact_presence": {
            name: {"present": item["present"], "required": item["required"], "status": item["status"]}
            for name, item in artifacts.items()
        },
        "artifacts": artifacts,
        "privacy": {
            "aggregate_only": status == "pass",
            "private_indicators_found": privacy_violation_count > 0,
            "private_indicator_count": privacy_violation_count,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
            "redacts_private_values": True,
        },
        "sensitive_values_omitted": True,
    }


def _verify_artifact(path: Path, expected: ExpectedArtifact) -> tuple[dict[str, Any], int, int, list[dict[str, Any]], int]:
    base = {
        "name": expected.name,
        "required": expected.required,
        "present": path.exists(),
        "status": "missing",
        "schema_version": None,
        "reported_status": None,
        "checks": [],
    }
    if not path.exists():
        if expected.required:
            return base, 0, 1, [_blocker(expected.name, "required_artifact_missing")], 0
        base["status"] = "optional_missing"
        return base, 1, 0, [], 0
    if not path.is_file():
        base["status"] = "invalid"
        return base, 0, 1, [_blocker(expected.name, "artifact_not_file")], 0

    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        base["status"] = "invalid_json"
        return base, 0, 1, [_blocker(expected.name, "malformed_json")], 0
    if not isinstance(payload, dict):
        base["status"] = "invalid_schema"
        return base, 0, 1, [_blocker(expected.name, "json_root_not_object")], 0

    passed = 1
    failed = 0
    blockers: list[dict[str, Any]] = []
    schema = payload.get("schema_version")
    base["schema_version"] = schema if isinstance(schema, str) else None
    if not isinstance(schema, str) or not schema.startswith(expected.schema_prefix):
        failed += 1
        blockers.append(_blocker(expected.name, "schema_version_unexpected"))

    reported_status = _path_value(payload, expected.status_path)
    if reported_status is None and expected.name == "aggregate_baseline_summary.json" and _aggregate_baseline_infers_pass(payload):
        reported_status = "pass"
    normalized_status = str(reported_status).lower() if reported_status is not None else None
    base["reported_status"] = normalized_status
    if expected.name == "deep_inspection_candidate_summary.json" and normalized_status in {"no_candidates", "no_inputs"}:
        pass
    elif normalized_status in _FAIL_STATUSES:
        failed += 1
        blockers.append(_blocker(expected.name, "artifact_status_failed"))
    elif normalized_status not in _PASS_STATUSES:
        failed += 1
        blockers.append(_blocker(expected.name, "artifact_status_unknown"))

    privacy_failures = _privacy_failures(payload, raw)
    if privacy_failures:
        failed += len(privacy_failures)
        blockers.extend(_blocker(expected.name, code) for code in privacy_failures)

    count_failures = _count_failures(payload, artifact_name=expected.name)
    if count_failures:
        failed += len(count_failures)
        blockers.extend(_blocker(expected.name, code) for code in count_failures)

    candidate_failures = _deep_inspection_candidate_failures(payload) if expected.name == "deep_inspection_candidate_summary.json" else []
    if candidate_failures:
        failed += len(candidate_failures)
        blockers.extend(_blocker(expected.name, code) for code in candidate_failures)

    review_decision_failures = (
        _review_decision_verification_failures(payload) if expected.name == "review_decision_verification_summary.json" else []
    )
    if review_decision_failures:
        failed += len(review_decision_failures)
        blockers.extend(_blocker(expected.name, code) for code in review_decision_failures)

    base["status"] = "pass" if failed == 0 else "fail"
    base["checks"] = sorted({item["code"] for item in blockers if item["artifact"] == expected.name}) or ["json_parseable", "schema_status_counts_privacy_ok"]
    if expected.name == "deep_inspection_candidate_summary.json":
        base["candidate_total"] = _coerce_int(payload.get("candidate_total")) or 0
        base["provider_configured"] = payload.get("provider_configured") if isinstance(payload.get("provider_configured"), bool) else None
        base["provider_count"] = _coerce_int(payload.get("provider_count")) or 0
        base["checks_passed_count"] = _candidate_check_count(payload.get("checks_passed"))
        base["checks_failed_count"] = _candidate_check_count(payload.get("checks_failed"))
    if expected.name == "review_decision_verification_summary.json":
        base["checks_passed_count"] = _coerce_int(payload.get("checks_passed")) or 0
        base["checks_failed_count"] = _coerce_int(payload.get("checks_failed")) or 0
        base["blocking_count"] = _coerce_int(payload.get("blocking_count")) or 0
        base["warning_count"] = _coerce_int(payload.get("warning_count")) or 0
        base["decision_summary"] = _review_decision_summary(payload.get("decision_summary"))
        base["blocking_counts_by_code"] = _safe_count_map(payload.get("blocking_counts_by_code"))
        base["warning_counts_by_code"] = _safe_count_map(payload.get("warning_counts_by_code"))
        privacy = payload.get("privacy")
        base["privacy_status"] = privacy.get("status") if isinstance(privacy, dict) and isinstance(privacy.get("status"), str) else None
    return base, passed, failed, blockers, len(privacy_failures)


def _privacy_failures(payload: dict[str, Any], raw: str) -> list[str]:
    failures: list[str] = []
    privacy = payload.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("aggregate_only") is not True:
        failures.append("privacy_not_aggregate_only")
    elif any(value is True for key, value in privacy.items() if key.startswith("contains_")):
        failures.append("privacy_flag_contains_private_evidence")

    for path, key, value in _walk(payload):
        if _private_key(path, key, value):
            failures.append("private_key_present")
            break
        if isinstance(value, str) and _private_value(value):
            failures.append("private_value_present")
            break
        if isinstance(value, list) and key in {"files", "findings", "manifest"} and value:
            failures.append("row_level_collection_present")
            break

    for code, pattern in _PRIVATE_VALUE_PATTERNS:
        if pattern.search(raw):
            failures.append(f"private_{code}_pattern_present")
            break

    return sorted(set(failures))


def _count_failures(payload: dict[str, Any], *, artifact_name: str | None = None) -> list[str]:
    failures: list[str] = []
    for key in ("checks_passed", "checks_failed", "blocking_item_count", "blocking_items"):
        value = _find_first_key(payload, key)
        if artifact_name == "deep_inspection_candidate_summary.json" and key in {"checks_passed", "checks_failed"}:
            if value is not None and not isinstance(value, list):
                failures.append(f"{key}_not_list")
            continue
        if key == "blocking_items" and value is not None and not isinstance(value, (int, list)):
            failures.append("blocking_items_not_list")
        elif key != "blocking_items" and value is not None and not isinstance(value, int):
            failures.append(f"{key}_not_integer")
    return failures


def _deep_inspection_candidate_failures(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not _non_negative_int(payload.get("candidate_total")):
        failures.append("candidate_total_not_non_negative_integer")
    if not _count_map(payload.get("candidates_by_reason")):
        failures.append("candidates_by_reason_invalid")
    if not _count_map(payload.get("candidates_by_severity")):
        failures.append("candidates_by_severity_invalid")
    if not isinstance(payload.get("provider_configured"), bool):
        failures.append("provider_configured_not_boolean")
    if not _non_negative_int(payload.get("provider_count")):
        failures.append("provider_count_not_non_negative_integer")
    if not isinstance(payload.get("checks_passed"), list):
        failures.append("checks_passed_not_list")
    checks_failed = payload.get("checks_failed")
    if not isinstance(checks_failed, list):
        failures.append("checks_failed_not_list")
    elif checks_failed:
        failures.append("checks_failed_present")
    if payload.get("privacy_status") != "aggregate_public_safe":
        failures.append("privacy_status_not_aggregate_public_safe")
    if payload.get("no_inference_run") is not True:
        failures.append("inference_run_not_allowed")
    return sorted(set(failures))


def _review_decision_verification_failures(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not _non_negative_int(payload.get("checks_passed")):
        failures.append("checks_passed_not_non_negative_integer")
    checks_failed = payload.get("checks_failed")
    if not _non_negative_int(checks_failed):
        failures.append("checks_failed_not_non_negative_integer")
    elif checks_failed > 0:
        failures.append("checks_failed_present")
    blocking_count = payload.get("blocking_count")
    if not _non_negative_int(blocking_count):
        failures.append("blocking_count_not_non_negative_integer")
    elif blocking_count > 0:
        failures.append("review_decision_blocking_count_present")
    if not _count_map(payload.get("blocking_counts_by_code")):
        failures.append("blocking_counts_by_code_invalid")
    if not _count_map(payload.get("warning_counts_by_code")):
        failures.append("warning_counts_by_code_invalid")
    if not _review_decision_summary(payload.get("decision_summary")):
        failures.append("decision_summary_invalid")
    privacy = payload.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("status") != "pass" or privacy.get("aggregate_only") is not True:
        failures.append("review_decision_privacy_not_public_safe")
    return sorted(set(failures))


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count_map(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(key, str) and _non_negative_int(count) for key, count in value.items())


def _safe_count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: count
        for key, count in sorted(value.items())
        if isinstance(key, str) and isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }


def _review_decision_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    decision_counts = _safe_count_map(value.get("decision_counts"))
    summary = {
        "total_decisions": _coerce_int(value.get("total_decisions")) or 0,
        "pending": _coerce_int(value.get("pending")) or 0,
        "accepted": _coerce_int(value.get("accepted")) or 0,
        "rejected": _coerce_int(value.get("rejected")) or 0,
        "rework": _coerce_int(value.get("rework")) or 0,
        "completion_status": value.get("completion_status") if isinstance(value.get("completion_status"), str) else None,
        "decision_counts": decision_counts,
    }
    if summary["completion_status"] is None:
        return {}
    return summary


def _candidate_check_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _private_key(path: tuple[str, ...], key: str, value: Any) -> bool:
    if _aggregate_counter_key(path, value) or _aggregate_benchmark_source_key(path, value):
        return False
    normalized = key.lower()
    if normalized in _ALLOWED_KEY_EXCEPTIONS:
        return False
    if _aggregate_key_exception(path, normalized, value):
        return False
    return any(token in normalized for token in _PRIVATE_KEYS)


def _aggregate_key_exception(path: tuple[str, ...], normalized: str, value: Any) -> bool:
    if path == ("source",) and _review_decision_source_metadata(value):
        return True
    if path in {("source", "schema"), ("source", "source_type")} and isinstance(value, str):
        return True
    if normalized in _ALLOWED_NUMERIC_KEYS and _is_number(value):
        return True
    if normalized.endswith(_ALLOWED_NUMERIC_KEY_SUFFIXES) and (_is_number(value) or _aggregate_count_structure(value)):
        return True
    if path == ("sensitive_artifacts", "paths_embedded") and isinstance(value, bool):
        return True
    if "finding_rule_counts_repeated_runs" in path and _aggregate_count_structure(value):
        return True
    if path == ("benchmark", "finding_rule_counts_repeated_runs") and _aggregate_count_structure(value):
        return True
    return False


def _aggregate_count_structure(value: Any) -> bool:
    if _is_number(value) or isinstance(value, bool):
        return True
    if isinstance(value, dict):
        return all(str(key).lower() in _COUNT_SUMMARY_KEYS or _aggregate_count_structure(child) for key, child in value.items())
    if isinstance(value, list):
        return all(_aggregate_count_structure(child) for child in value)
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _private_value(value: str) -> bool:
    return any(pattern.search(value) for _, pattern in _PRIVATE_VALUE_PATTERNS)


def _aggregate_counter_key(path: tuple[str, ...], value: Any) -> bool:
    return path in _AGGREGATE_BASELINE_COUNTER_PATHS and _numeric_count_value(value)


def _numeric_count_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, dict):
        return bool(value) and all(_numeric_count_value(child) for child in value.values())
    return False


def _aggregate_benchmark_source_key(path: tuple[str, ...], value: Any) -> bool:
    return path == ("benchmark", "source") and isinstance(value, str) and value in _AGGREGATE_BENCHMARK_SOURCES


def _review_decision_source_metadata(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"schema", "source_type"}
        and value.get("schema") == _REVIEW_DECISION_SOURCE_SCHEMA
        and value.get("source_type") == _REVIEW_DECISION_SOURCE_TYPE
    )


def _aggregate_baseline_infers_pass(payload: dict[str, Any]) -> bool:
    privacy = payload.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("aggregate_only") is not True:
        return False
    if any(value is True for key, value in privacy.items() if key.startswith("contains_")):
        return False

    self_check = payload.get("privacy_self_check")
    self_check_passed = False
    if isinstance(self_check, dict):
        status = str(self_check.get("status", "")).lower()
        self_check_passed = self_check.get("passed") is True or status in _PASS_STATUSES

    return self_check_passed and _coerce_int(_path_value(payload, ("aggregate_counts", "processing_failed_files"))) == 0


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _walk(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str, Any]]:
    rows: list[tuple[tuple[str, ...], str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            rows.append((path + (key_text,), key_text, child))
            rows.extend(_walk(child, path + (key_text,)))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_walk(child, path + ("[]",)))
    return rows


def _path_value(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for item in path:
        if not isinstance(current, dict):
            return None
        current = current.get(item)
    return current


def _find_first_key(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _find_first_key(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_first_key(value, key)
            if found is not None:
                return found
    return None


def _blocker(artifact: str, code: str) -> dict[str, str]:
    return {"artifact": artifact, "code": code}
