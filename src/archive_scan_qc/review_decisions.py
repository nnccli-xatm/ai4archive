"""Privacy-safe review decision summary verifier."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "scan-qc.review-decision-verification-summary.v1"
INPUT_SCHEMA = "scan-qc-review-decisions.local.v1"
REVIEW_DECISION_VERIFICATION_JSON = "review_decision_verification_summary.json"
ALLOWED_DECISIONS = ("pending", "accepted_issue", "false_positive", "fixed_externally", "needs_rescan", "blocked")
PRIVATE_FIELD_NAMES = {
    "absolute_path",
    "derivative_image",
    "filename",
    "hash",
    "image_bytes",
    "image_content",
    "manifest",
    "ocr_text",
    "path",
    "preview_filename",
    "preview_object_url",
    "provider_command",
    "prompt",
    "raw_model_output",
    "relative_path",
    "reviewer_notes",
    "sha256",
    "source_image",
    "thumbnail",
}
ALLOWED_TOP_LEVEL_FIELDS = {
    "schema",
    "source_type",
    "source_target_count",
    "generated_in_browser",
    "privacy",
    "aggregate_counts",
    "review_counts",
    "reviewed_targets",
    "decisions",
}
ALLOWED_DECISION_FIELDS = {"scope", "local_id", "decision"}


def write_review_decision_verification_summary(summary_path: Path, output_path: Path) -> tuple[Path, dict[str, Any]]:
    summary = _load_json_object(summary_path)
    payload = build_review_decision_verification_summary(summary)
    path = _resolve_output_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, payload


def build_review_decision_verification_summary(summary: dict[str, Any]) -> dict[str, Any]:
    blocking: dict[str, int] = {}
    warnings: dict[str, int] = {}

    if summary.get("schema") != INPUT_SCHEMA:
        _add(blocking, "invalid_schema")

    for key in summary:
        if key not in ALLOWED_TOP_LEVEL_FIELDS:
            _add(warnings, "ignored_extra_top_level_field")

    private_count = _private_field_count(summary)
    if private_count:
        blocking["privacy_sensitive_field"] = private_count

    decisions_raw = summary.get("decisions")
    decisions = decisions_raw if isinstance(decisions_raw, list) else []
    if not isinstance(decisions_raw, list):
        _add(blocking, "malformed_decision_list")

    decision_counts = {decision: 0 for decision in ALLOWED_DECISIONS}
    seen_keys: set[tuple[str, str]] = set()
    valid_decisions = 0
    malformed_entries = 0
    unknown_decisions = 0
    duplicate_decisions = 0
    extra_decision_fields = 0

    for item in decisions:
        if not isinstance(item, dict) or isinstance(item, list):
            malformed_entries += 1
            continue
        if any(key not in ALLOWED_DECISION_FIELDS for key in item):
            extra_decision_fields += 1
        scope = item.get("scope")
        local_id = item.get("local_id")
        decision = item.get("decision")
        if not isinstance(scope, str) or not scope or not isinstance(local_id, str) or not local_id:
            malformed_entries += 1
            continue
        if not isinstance(decision, str) or decision not in ALLOWED_DECISIONS:
            unknown_decisions += 1
            continue
        key = (scope, local_id)
        if key in seen_keys:
            duplicate_decisions += 1
            continue
        seen_keys.add(key)
        decision_counts[decision] += 1
        valid_decisions += 1

    if malformed_entries:
        blocking["malformed_decision_entry"] = malformed_entries
    if unknown_decisions:
        blocking["unknown_decision_value"] = unknown_decisions
    if duplicate_decisions:
        blocking["duplicate_decision"] = duplicate_decisions
    if extra_decision_fields:
        warnings["ignored_extra_decision_field"] = extra_decision_fields

    source_target_count = _safe_int(summary.get("source_target_count"))
    if source_target_count is None or source_target_count < 0:
        _add(blocking, "invalid_source_target_count")
    elif source_target_count != valid_decisions:
        _add(blocking, "source_target_count_mismatch")

    reported_review_counts = summary.get("review_counts")
    if not isinstance(reported_review_counts, dict):
        _add(blocking, "missing_review_counts")
    else:
        for decision, count in decision_counts.items():
            if _safe_int(reported_review_counts.get(decision)) != count:
                _add(blocking, "review_count_mismatch")

    aggregate_counts = summary.get("aggregate_counts")
    completion = aggregate_counts.get("review_completion") if isinstance(aggregate_counts, dict) else None
    if not isinstance(aggregate_counts, dict):
        _add(blocking, "missing_aggregate_counts")
    if not isinstance(completion, dict):
        _add(blocking, "missing_review_completion")
    else:
        reviewed = valid_decisions - decision_counts["pending"]
        completion_expectations = {
            "total": valid_decisions,
            "pending": decision_counts["pending"],
            "reviewed": reviewed,
        }
        for field, expected in completion_expectations.items():
            if _safe_int(completion.get(field)) != expected:
                _add(blocking, "review_completion_count_mismatch")
        if not isinstance(completion.get("complete"), bool) or completion.get("complete") != (valid_decisions > 0 and decision_counts["pending"] == 0):
            _add(blocking, "review_completion_status_mismatch")

    total_decisions = valid_decisions
    status = "pass" if not blocking else "blocked"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": status,
        "checks_passed": 0 if blocking else 1,
        "checks_failed": sum(blocking.values()),
        "source": {
            "schema": summary.get("schema") if isinstance(summary.get("schema"), str) else None,
            "source_type": summary.get("source_type") if isinstance(summary.get("source_type"), str) else None,
        },
        "decision_summary": {
            "total_decisions": total_decisions,
            "pending": decision_counts["pending"],
            "accepted": decision_counts["accepted_issue"],
            "rejected": decision_counts["false_positive"],
            "rework": decision_counts["fixed_externally"] + decision_counts["needs_rescan"] + decision_counts["blocked"],
            "completion_status": "complete" if total_decisions > 0 and decision_counts["pending"] == 0 else "incomplete",
            "decision_counts": decision_counts,
        },
        "blocking_counts_by_code": dict(sorted(blocking.items())),
        "warning_counts_by_code": dict(sorted(warnings.items())),
        "blocking_count": sum(blocking.values()),
        "warning_count": sum(warnings.values()),
        "privacy": {
            "status": "pass" if not private_count else "blocked",
            "aggregate_only": private_count == 0,
            "sensitive_field_count": private_count,
            "source_values_omitted": True,
        },
        "sensitivity": (
            "Aggregate-only verifier output. Decision IDs, paths, filenames, hashes, OCR text, thumbnails, "
            "image references, prompts, provider commands, and raw model output are not emitted."
        ),
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Review-decision summary JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Review-decision summary JSON must be an object.")
    return payload


def _resolve_output_path(path: Path) -> Path:
    return path / REVIEW_DECISION_VERIFICATION_JSON if path.suffix == "" else path


def _private_field_count(value: Any) -> int:
    if isinstance(value, dict):
        count = 0
        for key, child in value.items():
            if isinstance(key, str) and key in PRIVATE_FIELD_NAMES:
                count += 1
            count += _private_field_count(child)
        return count
    if isinstance(value, list):
        return sum(_private_field_count(item) for item in value)
    return 0


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _add(counts: dict[str, int], code: str) -> None:
    counts[code] = counts.get(code, 0) + 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
