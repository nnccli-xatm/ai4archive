"""Public-safe validation index for aggregate QA outputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .evidence_bundle import _privacy_failures


VALIDATION_INDEX_JSON = "public_safe_validation_index.json"
SCHEMA_VERSION = "scan-qc.public-safe-validation-index.v1"

_PASS_STATUSES = {"pass", "passed", "ok", "success"}
_FAIL_STATUSES = {"fail", "failed", "error", "blocked"}
_PROCESSING_REUSE_COUNTER_KEYS = (
    "processing_resumed_files",
    "processing_duplicate_reused_files",
    "processing_existing_derivative_reused_files",
)


@dataclass(frozen=True)
class KnownAggregateArtifact:
    name: str
    category: str
    schema_prefix: str | None = None


KNOWN_PUBLIC_SAFE_ARTIFACTS = (
    KnownAggregateArtifact(
        "image_processing_capability_smoke.json",
        "image_processing_capability_smoke",
        "scan-qc.image-processing-capability-smoke.",
    ),
    KnownAggregateArtifact(
        "processing_quality_summary.json",
        "processing_quality_summary",
        "scan-qc.processing-quality-summary.",
    ),
    KnownAggregateArtifact("frontend_workbench_validation.json", "frontend_workbench_validation"),
    KnownAggregateArtifact("release_readiness_summary.json", "release_readiness", "scan-qc.release-readiness."),
    KnownAggregateArtifact("release_candidate_summary.json", "release_candidate", "scan-qc.release-candidate-summary."),
    KnownAggregateArtifact(
        "aggregate_evidence_bundle_summary.json",
        "aggregate_evidence_bundle",
        "scan-qc.aggregate-evidence-bundle.",
    ),
    KnownAggregateArtifact(
        "review_decision_verification_summary.json",
        "review_decision_verification",
        "scan-qc.review-decision-verification-summary.",
    ),
    KnownAggregateArtifact(
        "private_validation_aggregate_summary.json",
        "private_validation_aggregate",
        "scan-qc.private-validation-aggregate.",
    ),
    KnownAggregateArtifact(
        "final_production_handoff_summary.json",
        "final_production_handoff",
        "scan-qc.final-production-handoff-summary.",
    ),
)


def write_public_safe_validation_index(
    *,
    input_dir: Path | None = None,
    files: list[Path] | None = None,
    output_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    summary = build_public_safe_validation_index(input_dir=input_dir, files=files)
    path = output_path or ((input_dir or Path.cwd()) / VALIDATION_INDEX_JSON)
    if path.suffix == "":
        path = path / VALIDATION_INDEX_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, summary


def build_public_safe_validation_index(
    *,
    input_dir: Path | None = None,
    files: list[Path] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if input_dir is None and not files:
        raise ValueError("provide --input-dir or at least one --file aggregate JSON input.")
    if input_dir is not None and files:
        raise ValueError("use either --input-dir or --file inputs, not both.")

    explicit_mode = bool(files)
    paths_by_name = _paths_by_name(input_dir=input_dir, files=files or [])
    artifacts: dict[str, Any] = {}
    blocking_items: list[dict[str, str]] = []
    checks_passed = 0
    checks_failed = 0
    privacy_failure_count = 0
    present_count = 0
    pass_count = 0
    fail_count = 0
    missing_count = 0
    processing_reuse_counts: dict[str, int] = {}

    for expected in KNOWN_PUBLIC_SAFE_ARTIFACTS:
        path = paths_by_name.get(expected.name)
        record, blockers = _artifact_record(path, expected, explicit_mode=explicit_mode)
        artifacts[expected.name] = record
        blocking_items.extend(blockers)
        checks_passed += record["checks_passed"]
        checks_failed += record["checks_failed"]
        privacy_failure_count += record["privacy"]["failure_count"]
        present_count += int(record["present"])
        pass_count += int(record["status"] == "pass")
        fail_count += int(record["status"] == "fail")
        missing_count += int(record["status"] in {"missing", "not_provided"})
        _merge_processing_reuse_counts(processing_reuse_counts, record.get("counts", {}))

    unknown_inputs = sorted(name for name in paths_by_name if name not in {item.name for item in KNOWN_PUBLIC_SAFE_ARTIFACTS})
    for name in unknown_inputs:
        blocking_items.append({"artifact": name, "category": "unknown", "code": "unknown_public_safe_artifact"})
        checks_failed += 1

    status = "pass" if checks_failed == 0 and not blocking_items else "fail"
    summary = {
        "known_artifacts": len(KNOWN_PUBLIC_SAFE_ARTIFACTS),
        "artifacts_present": present_count,
        "artifacts_passed": pass_count,
        "artifacts_failed": fail_count,
        "artifacts_missing": missing_count,
        "unknown_inputs": len(unknown_inputs),
        "blocking_item_count": len(blocking_items),
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
    }
    summary.update(processing_reuse_counts)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "summary": summary,
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "artifact_presence": {
            name: {
                "present": record["present"],
                "category": record["category"],
                "status": record["status"],
                "reported_status": record["reported_status"],
            }
            for name, record in artifacts.items()
        },
        "artifacts": artifacts,
        "blocking_items": blocking_items,
        "privacy": {
            "aggregate_only": True,
            "private_indicators_found": privacy_failure_count > 0,
            "private_indicator_count": privacy_failure_count,
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
            "contains_local_preview_object_urls": False,
            "contains_provider_command_strings": False,
            "contains_prompts": False,
            "contains_raw_model_output": False,
            "redacts_private_values": True,
        },
        "sensitive_values_omitted": True,
    }


def _paths_by_name(*, input_dir: Path | None, files: list[Path]) -> dict[str, Path]:
    if input_dir is not None:
        return {item.name: input_dir / item.name for item in KNOWN_PUBLIC_SAFE_ARTIFACTS}
    result: dict[str, Path] = {}
    for path in files:
        if path.name in result:
            raise ValueError(f"duplicate aggregate JSON input: {path.name}")
        result[path.name] = path
    return result


def _artifact_record(
    path: Path | None,
    expected: KnownAggregateArtifact,
    *,
    explicit_mode: bool,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    base = {
        "category": expected.category,
        "present": False,
        "status": "not_provided" if explicit_mode else "missing",
        "reported_status": None,
        "schema_version": None,
        "checks_passed": 0,
        "checks_failed": 0 if explicit_mode else 1,
        "privacy": {"status": "not_checked", "failure_count": 0, "failure_codes": []},
    }
    if path is None:
        return base, []
    if not path.exists():
        base["checks_failed"] = 1
        return base, [_blocker(expected, "aggregate_artifact_missing")]
    if not path.is_file():
        base["present"] = True
        base["status"] = "fail"
        base["checks_failed"] = 1
        return base, [_blocker(expected, "aggregate_artifact_not_file")]

    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        base["present"] = True
        base["status"] = "fail"
        base["checks_failed"] = 1
        return base, [_blocker(expected, "malformed_json")]
    if not isinstance(payload, dict):
        base["present"] = True
        base["status"] = "fail"
        base["checks_failed"] = 1
        return base, [_blocker(expected, "json_root_not_object")]

    blockers: list[dict[str, str]] = []
    checks_failed = 0
    schema = payload.get("schema_version")
    if isinstance(schema, str):
        base["schema_version"] = schema
    if expected.schema_prefix and (not isinstance(schema, str) or not schema.startswith(expected.schema_prefix)):
        checks_failed += 1
        blockers.append(_blocker(expected, "schema_version_unexpected"))

    reported_status = _normalized_status(payload)
    base["reported_status"] = reported_status
    if reported_status in _FAIL_STATUSES:
        checks_failed += 1
        blockers.append(_blocker(expected, "artifact_status_failed"))
    elif reported_status not in _PASS_STATUSES:
        checks_failed += 1
        blockers.append(_blocker(expected, "artifact_status_unknown"))

    privacy_codes = _privacy_codes(payload, raw)
    for code in privacy_codes:
        blockers.append(_blocker(expected, code))
    checks_failed += len(privacy_codes)
    public_blocking_codes = _public_blocking_codes(payload.get("blocking_codes"))
    for code in public_blocking_codes:
        blockers.append(_blocker(expected, code))
    checks_failed += len(public_blocking_codes)
    checks_passed = _extract_int(payload, "checks_passed")
    nested_failed = _extract_int(payload, "checks_failed")
    if checks_passed == 0 and nested_failed == 0 and checks_failed == 0:
        checks_passed = 1
    checks_failed += nested_failed
    counts = _safe_artifact_counts(payload)

    base.update(
        {
            "present": True,
            "status": "pass" if checks_failed == 0 else "fail",
            "checks_passed": checks_passed,
            "checks_failed": checks_failed,
            "counts": counts,
            "privacy": {
                "status": "pass" if not privacy_codes else "fail",
                "failure_count": len(privacy_codes),
                "failure_codes": privacy_codes,
            },
        }
    )
    nested_review_decision = _nested_artifact_status(payload, "review_decision_verification_summary.json")
    if nested_review_decision:
        base["review_decision_verification"] = nested_review_decision
    return base, blockers


def _privacy_codes(payload: dict[str, Any], raw: str) -> list[str]:
    codes = set(_privacy_failures(payload, raw))
    raw_lower = raw.lower()
    if "derivative/" in raw_lower:
        codes.add("private_derivative_reference_present")
    if any(needle in raw_lower for needle in ("blob:", "preview_object_url", "url.createobjecturl")):
        codes.add("private_local_preview_object_url_present")
    if any(needle in raw_lower for needle in ('"provider_command"', '"analysis_provider_command"')):
        codes.add("private_provider_command_string_present")
    if '"prompt"' in raw_lower:
        codes.add("private_prompt_present")
    if any(needle in raw_lower for needle in ('"raw_model_output"', "raw_model_output:", '"model_output"')):
        codes.add("private_raw_model_output_present")
    if any(needle in raw_lower for needle in ('"processing_manifest"', "manifest.csv", "row_report.csv")):
        codes.add("private_manifest_reference_present")
    if any(needle in raw_lower for needle in ("source_type", "scan-qc-review-decisions.local.v1")):
        codes.add("private_source_metadata_present")
    return sorted(codes)


def _normalized_status(payload: dict[str, Any]) -> str | None:
    status = payload.get("status")
    return str(status).lower() if status is not None else None


def _extract_int(payload: Any, key: str) -> int:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        for child in payload.values():
            found = _extract_int(child, key)
            if found:
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = _extract_int(child, key)
            if found:
                return found
    return 0


def _safe_artifact_counts(payload: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    counts.update(_processing_reuse_counts(payload))
    for key in ("blocking_count", "warning_count", "blocking_item_count"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            counts[key] = max(0, value)
    for key in ("blocking_counts_by_code", "warning_counts_by_code"):
        value = payload.get(key)
        if isinstance(value, dict):
            counts[key] = {
                str(code): max(0, count)
                for code, count in sorted(value.items())
                if isinstance(count, int) and not isinstance(count, bool)
            }
    public_blocking_codes = _public_blocking_codes(payload.get("blocking_codes"))
    if public_blocking_codes:
        counts["blocking_counts_by_code"] = _counts_by_code(public_blocking_codes)
    public_warning_codes = _public_blocking_codes(payload.get("warning_codes"))
    if public_warning_codes:
        counts["warning_counts_by_code"] = _counts_by_code(public_warning_codes)
    return counts


def _public_blocking_codes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    codes: list[str] = []
    for item in value:
        if isinstance(item, str):
            codes.append(item if _safe_public_code(item) else "aggregate_item_present")
        else:
            codes.append("aggregate_item_present")
    return codes


def _safe_public_code(value: str) -> bool:
    return bool(value) and all(char.islower() or char.isdigit() or char == "_" for char in value)


def _counts_by_code(codes: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for code in codes:
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def _processing_reuse_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {
        key: value
        for key in _PROCESSING_REUSE_COUNTER_KEYS
        if (value := _extract_optional_int(payload, key)) is not None
    }


def _merge_processing_reuse_counts(target: dict[str, int], counts: dict[str, Any]) -> None:
    for key in _PROCESSING_REUSE_COUNTER_KEYS:
        value = _safe_int(counts.get(key))
        if value is None:
            continue
        if key not in target or value > target[key]:
            target[key] = value


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


def _nested_artifact_status(payload: dict[str, Any], artifact_name: str) -> dict[str, Any]:
    for container_name in ("artifact_status_summary", "artifacts", "artifact_presence"):
        container = payload.get(container_name)
        if not isinstance(container, dict):
            continue
        item = container.get(artifact_name)
        if not isinstance(item, dict):
            continue
        summary: dict[str, Any] = {}
        for key in ("present", "required"):
            value = item.get(key)
            if isinstance(value, bool):
                summary[key] = value
        for key in ("status", "reported_status", "privacy_status"):
            value = item.get(key)
            if isinstance(value, str):
                summary[key] = value
        for key in ("checks_passed", "checks_failed", "blocking_count", "warning_count"):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                summary[key] = max(0, value)
        for key in ("blocking_counts_by_code", "warning_counts_by_code"):
            value = item.get(key)
            if isinstance(value, dict):
                summary[key] = {
                    str(code): max(0, count)
                    for code, count in sorted(value.items())
                    if isinstance(count, int) and not isinstance(count, bool)
                }
        if summary:
            return summary
    return {}


def _blocker(expected: KnownAggregateArtifact, code: str) -> dict[str, str]:
    return {"artifact": expected.name, "category": expected.category, "code": code}
