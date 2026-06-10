"""Public-safe aggregate report for operator-approved private validation runs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from .evidence_bundle import _privacy_failures


PRIVATE_VALIDATION_AGGREGATE_JSON = "private_validation_aggregate_summary.json"
SCHEMA_VERSION = "scan-qc.private-validation-aggregate.v1"

_SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_IMAGE_SUFFIX_RE = re.compile(r"\.(?:png|jpe?g|tiff?|bmp|gif|jp2|pdf|csv)$", re.IGNORECASE)
_PASS_STATUSES = {"pass", "passed", "ok", "success"}
_FAIL_STATUSES = {"fail", "failed", "error", "blocked"}
_QUALITY_SIGNAL_STATUSES = {
    "measured_with_changes",
    "measured_no_quality_operations",
    "not_applicable",
    "unknown",
}
_COUNT_FIELDS = (
    ("total_files", "total_items"),
    ("processed_files", "processed_items"),
    ("failed_files", "failed_items"),
    ("guardrail_failed_files", "guardrail_failed_items"),
    ("warning_files", "warning_items"),
    ("changed_files", "changed_items"),
    ("quality_gain_files", "quality_gain_items"),
    ("overprocessing_risk_files", "overprocessing_risk_items"),
)
_METRIC_IDS = (
    "background_uniformity_delta",
    "text_contrast_delta",
    "scanline_residual_delta",
    "bleed_through_delta",
    "color_mean_abs_delta",
    "changed_pixel_ratio",
    "brightness_delta",
    "contrast_delta",
    "tone_contrast_delta",
    "scanlines_delta",
    "faded_text_delta",
    "text_edges_delta",
    "processed_output_dark_pixel_loss_ratio",
    "processed_output_highlight_clip_delta",
    "source_cer",
    "processed_cer",
    "cer_relative_reduction",
    "source_wer",
    "processed_wer",
    "wer_relative_reduction",
    "ocr_foreground_retention_ratio",
    "ocr_background_delta",
)


def write_private_validation_aggregate(
    *,
    input_dir: Path | None = None,
    files: list[Path] | None = None,
    output_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    summary = build_private_validation_aggregate(input_dir=input_dir, files=files)
    path = output_path or ((input_dir or Path.cwd()) / PRIVATE_VALIDATION_AGGREGATE_JSON)
    if path.suffix == "":
        path = path / PRIVATE_VALIDATION_AGGREGATE_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, summary


def build_private_validation_aggregate(
    *,
    input_dir: Path | None = None,
    files: list[Path] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    paths = _input_paths(input_dir=input_dir, files=files or [])
    group_accumulators: dict[str, dict[str, Any]] = {}
    risk_code_counts: dict[str, int] = {}
    quality_signal_status_counts: dict[str, int] = {}
    blocking_codes: list[str] = []
    raw_sensitive_count = 0
    invalid_payload_count = 0

    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            invalid_payload_count += 1
            _increment(risk_code_counts, "invalid_validation_payload")
            continue
        if not isinstance(payload, dict):
            invalid_payload_count += 1
            _increment(risk_code_counts, "invalid_validation_payload")
            continue
        if _privacy_failures(payload, raw):
            raw_sensitive_count += 1
        group_id, group_risk = _group_id(payload)
        if group_risk:
            _increment(risk_code_counts, group_risk)
        group = group_accumulators.setdefault(group_id, _new_group(group_id))
        group["validation_input_count"] += 1
        status = _status(payload.get("status"))
        if status == "fail":
            group["failed_validation_inputs"] += 1
            _increment(risk_code_counts, "validation_group_failed")
        elif status == "pass":
            group["passed_validation_inputs"] += 1
        else:
            group["unknown_validation_inputs"] += 1
            _increment(risk_code_counts, "validation_status_unknown")
        _merge_counts(group["counts"], payload.get("counts"))
        _merge_metrics(group["metric_accumulators"], payload.get("quality_metrics"))
        signal_status = _quality_signal_status(payload)
        _increment(group["quality_signal_status_counts"], signal_status)
        _increment(quality_signal_status_counts, signal_status)
        _merge_risk_codes(risk_code_counts, payload.get("risk_codes"))

    groups = [_public_group(group) for group in sorted(group_accumulators.values(), key=lambda item: item["group_id"])]
    if not paths:
        blocking_codes.append("no_validation_inputs")
    if invalid_payload_count:
        blocking_codes.append("invalid_validation_payload")
    if any(group["failed_validation_inputs"] for group in group_accumulators.values()):
        blocking_codes.append("validation_group_failed")
    status = "pass" if not blocking_codes else "fail"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "blocking_codes": sorted(set(blocking_codes)),
        "validation_inputs": {
            "provided_count": len(paths),
            "invalid_payload_count": invalid_payload_count,
            "raw_sensitive_payload_count": raw_sensitive_count,
        },
        "group_count": len(groups),
        "group_summaries": groups,
        "quality_signal_status_counts": _status_count_summary(quality_signal_status_counts),
        "risk_code_counts": _risk_code_summary(risk_code_counts),
        "quality_measurement": {
            "method": "private_validation_aggregate_only",
            "before_after_evidence": "group_metrics_only",
            "row_level_evidence_included": False,
            "binary_payload_included": False,
        },
        "privacy": _privacy_payload(),
    }
    privacy_failures = _privacy_failures(summary, json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if privacy_failures:
        summary["status"] = "fail"
        summary["blocking_codes"] = sorted(set(summary["blocking_codes"] + ["privacy_output_not_public_safe"]))
        summary["privacy"]["self_check_status"] = "fail"
        summary["privacy"]["self_check_failure_count"] = len(privacy_failures)
    return summary


def _input_paths(*, input_dir: Path | None, files: list[Path]) -> list[Path]:
    if input_dir is None and not files:
        raise ValueError("provide --input-dir or at least one --file private validation JSON input.")
    if input_dir is not None and files:
        raise ValueError("use either --input-dir or --file inputs, not both.")
    if input_dir is not None:
        return sorted(path for path in input_dir.glob("*.json") if path.name != PRIVATE_VALIDATION_AGGREGATE_JSON)
    return [Path(path) for path in files]


def _group_id(payload: dict[str, Any]) -> tuple[str, str | None]:
    value = payload.get("public_group_id", payload.get("validation_group"))
    if isinstance(value, str) and _safe_code(value):
        return value, None
    return "unclassified", "unsafe_group_label_omitted"


def _safe_code(value: str) -> bool:
    return (
        bool(_SAFE_CODE_RE.match(value))
        and "/" not in value
        and "\\" not in value
        and not _IMAGE_SUFFIX_RE.search(value)
    )


def _status(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    if normalized in _PASS_STATUSES:
        return "pass"
    if normalized in _FAIL_STATUSES:
        return "fail"
    return "unknown"


def _new_group(group_id: str) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "validation_input_count": 0,
        "passed_validation_inputs": 0,
        "failed_validation_inputs": 0,
        "unknown_validation_inputs": 0,
        "counts": {public_field: 0 for _input_field, public_field in _COUNT_FIELDS},
        "metric_accumulators": {},
        "quality_signal_status_counts": {},
    }


def _merge_counts(target: dict[str, int], value: Any) -> None:
    value = value if isinstance(value, dict) else {}
    for input_field, public_field in _COUNT_FIELDS:
        target[public_field] += _safe_int(value.get(input_field))


def _merge_metrics(target: dict[str, dict[str, float]], value: Any) -> None:
    value = value if isinstance(value, dict) else {}
    for metric_id in _METRIC_IDS:
        metric = value.get(metric_id)
        if not isinstance(metric, dict):
            continue
        count = _safe_int(metric.get("count")) or 1
        average = _safe_float(metric.get("average", metric.get("value")))
        maximum = _safe_float(metric.get("max", metric.get("maximum", average)))
        accumulator = target.setdefault(metric_id, {"count": 0.0, "weighted_sum": 0.0, "max": 0.0})
        accumulator["count"] += count
        accumulator["weighted_sum"] += average * count
        accumulator["max"] = max(accumulator["max"], maximum)


def _merge_risk_codes(target: dict[str, int], value: Any) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, str) and _safe_code(item):
            _increment(target, item)
        else:
            _increment(target, "unsafe_risk_code_omitted")


def _quality_signal_status(payload: dict[str, Any]) -> str:
    signal = payload.get("quality_signal")
    value = signal.get("status") if isinstance(signal, dict) else payload.get("quality_signal_status")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _QUALITY_SIGNAL_STATUSES:
            return normalized
    return "unknown"


def _public_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": group["group_id"],
        "validation_input_count": group["validation_input_count"],
        "passed_validation_inputs": group["passed_validation_inputs"],
        "failed_validation_inputs": group["failed_validation_inputs"],
        "unknown_validation_inputs": group["unknown_validation_inputs"],
        "counts": dict(group["counts"]),
        "quality_signal_status_counts": _status_count_summary(group["quality_signal_status_counts"]),
        "metric_summary": _metric_summary(group["metric_accumulators"]),
    }


def _metric_summary(accumulators: dict[str, dict[str, float]]) -> list[dict[str, float | int | str]]:
    summary: list[dict[str, float | int | str]] = []
    for metric_id, accumulator in sorted(accumulators.items()):
        count = int(accumulator["count"])
        if count <= 0:
            continue
        summary.append({
            "metric_id": metric_id,
            "count": count,
            "average": round(accumulator["weighted_sum"] / count, 6),
            "max": round(accumulator["max"], 6),
        })
    return summary


def _risk_code_summary(risk_code_counts: dict[str, int]) -> list[dict[str, int | str]]:
    return [
        {"risk_code": code, "count": count}
        for code, count in sorted(risk_code_counts.items())
    ]


def _status_count_summary(counts: dict[str, int]) -> list[dict[str, int | str]]:
    return [
        {"status": status, "count": count}
        for status, count in sorted(counts.items())
    ]


def _privacy_payload() -> dict[str, Any]:
    return {
        "aggregate_only": True,
        "public_safe": True,
        "contains_paths": False,
        "contains_filenames": False,
        "contains_hashes": False,
        "contains_ocr_text": False,
        "contains_thumbnails": False,
        "contains_image_content": False,
        "contains_row_level_findings": False,
        "contains_row_level_evidence": False,
        "self_check_status": "pass",
        "self_check_failure_count": 0,
    }


def _increment(target: dict[str, int], key: str) -> None:
    target[key] = target.get(key, 0) + 1


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
