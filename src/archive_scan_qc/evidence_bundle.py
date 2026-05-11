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
    "contains_file_list",
    "contains_filenames",
    "contains_hashes",
    "contains_image_content",
    "contains_ocr_text",
    "contains_paths",
    "contains_row_level_evidence",
    "contains_row_level_findings",
    "contains_secrets",
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
    "total_files",
    "total_findings",
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
    if normalized_status in _FAIL_STATUSES:
        failed += 1
        blockers.append(_blocker(expected.name, "artifact_status_failed"))
    elif normalized_status not in _PASS_STATUSES:
        failed += 1
        blockers.append(_blocker(expected.name, "artifact_status_unknown"))

    privacy_failures = _privacy_failures(payload, raw)
    if privacy_failures:
        failed += len(privacy_failures)
        blockers.extend(_blocker(expected.name, code) for code in privacy_failures)

    count_failures = _count_failures(payload)
    if count_failures:
        failed += len(count_failures)
        blockers.extend(_blocker(expected.name, code) for code in count_failures)

    base["status"] = "pass" if failed == 0 else "fail"
    base["checks"] = sorted({item["code"] for item in blockers if item["artifact"] == expected.name}) or ["json_parseable", "schema_status_counts_privacy_ok"]
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


def _count_failures(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for key in ("checks_passed", "checks_failed", "blocking_item_count", "blocking_items"):
        value = _find_first_key(payload, key)
        if key == "blocking_items" and value is not None and not isinstance(value, (int, list)):
            failures.append("blocking_items_not_list")
        elif key != "blocking_items" and value is not None and not isinstance(value, int):
            failures.append(f"{key}_not_integer")
    return failures


def _private_key(path: tuple[str, ...], key: str, value: Any) -> bool:
    if _aggregate_counter_key(path, value) or _aggregate_benchmark_source_key(path, value):
        return False
    normalized = key.lower()
    if normalized in _ALLOWED_KEY_EXCEPTIONS:
        return False
    return any(token in normalized for token in _PRIVATE_KEYS)


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
