"""Public-safe aggregate quality summary for derivative processing."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


PROCESSING_QUALITY_SUMMARY_JSON = "processing_quality_summary.json"
SCHEMA_VERSION = "scan-qc.processing-quality-summary.v1"

_COUNT_FIELDS = (
    "total_files",
    "processed_files",
    "failed_files",
    "retry_list_files",
    "processing_warning_files",
    "guardrail_failed_files",
    "deskewed_files",
    "dark_border_trimmed_files",
    "scanner_gutter_trimmed_files",
    "auto_crop_applied_files",
    "despeckled_files",
    "tone_normalized_files",
    "paper_color_cast_normalized_files",
    "edge_shadow_lightened_files",
    "corner_shadows_lightened_files",
    "background_stains_lightened_files",
    "fold_shadows_lightened_files",
    "illumination_gradient_levelled_files",
    "bleed_through_cleaned_files",
    "scanlines_lightened_files",
    "faded_text_enhanced_files",
    "text_edges_sharpened_files",
    "ocr_preprocessed_files",
    "ocr_binary_created_files",
    "ocr_review_required_files",
)

_METRIC_FIELDS = (
    "brightness_delta",
    "contrast_delta",
    "crop_ratio",
    "max_trim_margin_ratio",
    "scanner_gutter_max_trim_margin_ratio",
    "deskew_abs_angle_degrees",
    "despeckle_pixel_ratio",
    "tone_background_delta",
    "tone_contrast_delta",
    "tone_changed_pixel_ratio",
    "paper_color_cast_delta",
    "paper_color_cast_brightness_delta",
    "paper_color_cast_changed_pixel_ratio",
    "edge_shadow_delta",
    "edge_shadow_changed_pixel_ratio",
    "corner_shadows_delta",
    "corner_shadows_changed_pixel_ratio",
    "background_stains_delta",
    "background_stains_changed_pixel_ratio",
    "fold_shadows_delta",
    "fold_shadows_changed_pixel_ratio",
    "illumination_gradient_correction_delta",
    "illumination_gradient_changed_pixel_ratio",
    "bleed_through_delta",
    "bleed_through_changed_pixel_ratio",
    "scanlines_delta",
    "scanlines_changed_pixel_ratio",
    "faded_text_delta",
    "faded_text_changed_pixel_ratio",
    "text_edges_delta",
    "text_edges_changed_pixel_ratio",
    "text_edges_edge_energy_before",
    "text_edges_edge_energy_after",
    "ocr_preprocess_changed_pixel_ratio",
    "ocr_background_delta",
    "ocr_background_candidate_pixel_ratio",
    "ocr_foreground_dark_loss_ratio",
    "ocr_foreground_dark_lift_ratio",
    "ocr_foreground_retention_ratio",
    "ocr_text_edge_energy_before",
    "ocr_text_edge_energy_after",
    "ocr_text_edge_energy_ratio",
    "ocr_text_soft_edge_ratio_before",
    "ocr_text_soft_edge_ratio_after",
    "ocr_text_soft_edge_ratio_delta",
    "ocr_deskew_clarity_candidate_count",
    "ocr_deskew_clarity_score",
    "ocr_deskew_clarity_edge_energy",
    "ocr_deskew_clarity_soft_edge_ratio",
    "ocr_deskew_clarity_table_line_score",
    "ocr_binary_foreground_ratio",
    "ocr_binary_foreground_retention_ratio",
    "processed_output_brightness_increase",
    "processed_output_near_white_delta",
    "processed_output_highlight_clip_delta",
    "processed_output_dark_pixel_loss_ratio",
    "processed_output_dark_pixel_lift_ratio",
)

_BACKGROUND_FIELDS = (
    "tone_normalized_files",
    "paper_color_cast_normalized_files",
    "edge_shadow_lightened_files",
    "corner_shadows_lightened_files",
    "background_stains_lightened_files",
    "fold_shadows_lightened_files",
    "illumination_gradient_levelled_files",
)

_GEOMETRY_FIELDS = (
    "deskewed_files",
    "dark_border_trimmed_files",
    "scanner_gutter_trimmed_files",
    "auto_crop_applied_files",
)

_TEXT_FIELDS = (
    "faded_text_enhanced_files",
    "text_edges_sharpened_files",
    "ocr_preprocessed_files",
    "ocr_binary_created_files",
)

_DEFECT_FIELDS = (
    "despeckled_files",
    "bleed_through_cleaned_files",
    "scanlines_lightened_files",
)


def build_processing_quality_summary(
    *,
    manifest: dict[str, Any] | None,
    audit_summary: dict[str, Any] | None,
    fixture_context: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a public-safe before/after quality signal summary.

    The input manifest can contain sensitive row-level evidence. This function
    only reads aggregate summary values and the already-public audit summary.
    """

    manifest_summary = manifest.get("summary", {}) if isinstance(manifest, dict) else {}
    audit_summary = audit_summary if isinstance(audit_summary, dict) else {}
    counts = _counts(manifest_summary, audit_summary.get("counts"))
    metrics = _quality_metrics(audit_summary.get("metrics"))
    guardrails = _guardrail_summary(audit_summary.get("guardrails"), counts)
    quality_signal = _quality_signal(counts)
    fixture_context_payload = _fixture_context(fixture_context)
    privacy = _privacy_payload()
    blockers = _blocking_codes(counts, guardrails, audit_summary.get("privacy"), fixture_context_payload)
    status = "pass" if not blockers else "fail"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "blocking_codes": blockers,
        "aggregate_only": True,
        "public_safe": True,
        "quality_measurement": {
            "method": "processing_manifest_and_audit_aggregate",
            "before_after_evidence": "aggregate_metrics_only",
            "row_level_evidence_included": False,
            "image_content_included": False,
        },
        "fixture_context": fixture_context_payload,
        "counts": counts,
        "quality_signal": quality_signal,
        "quality_metrics": metrics,
        "guardrails": guardrails,
        "privacy": privacy,
    }


def write_processing_quality_summary(payload: dict[str, Any], output_path: Path) -> Path:
    path = _quality_summary_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def quality_summary_path_for_output(output_path: Path) -> Path:
    return _quality_summary_path(output_path)


def _quality_summary_path(output_path: Path) -> Path:
    if output_path.suffix == "":
        return output_path / PROCESSING_QUALITY_SUMMARY_JSON
    return output_path.parent / PROCESSING_QUALITY_SUMMARY_JSON


def _counts(manifest_summary: Any, audit_counts: Any) -> dict[str, int]:
    manifest_summary = manifest_summary if isinstance(manifest_summary, dict) else {}
    audit_counts = audit_counts if isinstance(audit_counts, dict) else {}
    counts: dict[str, int] = {}
    for field in _COUNT_FIELDS:
        counts[field] = _safe_int(audit_counts.get(field, manifest_summary.get(field)))
    return counts


def _quality_signal(counts: dict[str, int]) -> dict[str, Any]:
    geometry_changed = _sum_fields(counts, _GEOMETRY_FIELDS)
    background_changed = _sum_fields(counts, _BACKGROUND_FIELDS)
    text_changed = _sum_fields(counts, _TEXT_FIELDS)
    defect_changed = _sum_fields(counts, _DEFECT_FIELDS)
    changed_total = geometry_changed + background_changed + text_changed + defect_changed
    processed = counts.get("processed_files", 0)
    status = "not_applicable"
    if processed:
        status = "measured_with_changes" if changed_total else "measured_no_quality_operations"
    return {
        "status": status,
        "processed_files": processed,
        "any_quality_operation_changed_files": min(processed, changed_total) if processed else 0,
        "geometry_changed_files": min(processed, geometry_changed) if processed else 0,
        "background_cleanup_changed_files": min(processed, background_changed) if processed else 0,
        "text_enhancement_changed_files": min(processed, text_changed) if processed else 0,
        "defect_cleanup_changed_files": min(processed, defect_changed) if processed else 0,
        "quality_operations_applied": {
            "geometry": geometry_changed > 0,
            "background_cleanup": background_changed > 0,
            "text_enhancement": text_changed > 0,
            "defect_cleanup": defect_changed > 0,
        },
    }


def _quality_metrics(raw_metrics: Any) -> dict[str, dict[str, float | int | None]]:
    raw_metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    metrics: dict[str, dict[str, float | int | None]] = {}
    for field in _METRIC_FIELDS:
        value = raw_metrics.get(field)
        if isinstance(value, dict):
            metrics[field] = _metric_payload(value)
    return metrics


def _metric_payload(value: dict[str, Any]) -> dict[str, float | int | None]:
    return {
        "count": _safe_int(value.get("count")),
        "average": _safe_float(value.get("average")),
        "max": _safe_float(value.get("max")),
    }


def _guardrail_summary(raw_guardrails: Any, counts: dict[str, int]) -> dict[str, Any]:
    raw_guardrails = raw_guardrails if isinstance(raw_guardrails, dict) else {}
    failure_reasons = raw_guardrails.get("failure_reasons")
    if not isinstance(failure_reasons, dict):
        failure_reasons = {}
    return {
        "enabled": bool(raw_guardrails.get("enabled", True)),
        "warning_files": _safe_int(raw_guardrails.get("warning_files", counts.get("processing_warning_files"))),
        "failed_files": _safe_int(raw_guardrails.get("failed_files", counts.get("guardrail_failed_files"))),
        "failure_reasons": {str(key): _safe_int(value) for key, value in sorted(failure_reasons.items())},
    }


def _fixture_context(raw_context: dict[str, Any] | None) -> dict[str, Any]:
    raw_context = raw_context if isinstance(raw_context, dict) else {}
    groups = raw_context.get("fixture_groups")
    if not isinstance(groups, list):
        groups = []
    protected_content_checks = raw_context.get("protected_content_checks")
    if not isinstance(protected_content_checks, list):
        protected_content_checks = []
    return {
        "source": str(raw_context.get("source") or "unspecified"),
        "synthetic_inputs_only": bool(raw_context.get("synthetic_inputs_only", False)),
        "fixture_count": _safe_int(raw_context.get("fixture_count")),
        "fixture_groups": [str(group) for group in groups if isinstance(group, str)],
        "protected_content_checks": [
            _protected_content_check_payload(check)
            for check in protected_content_checks
            if isinstance(check, dict)
        ],
    }


def _protected_content_check_payload(check: dict[str, Any]) -> dict[str, Any]:
    fail_codes = check.get("fail_codes")
    if not isinstance(fail_codes, list):
        fail_codes = []
    status = check.get("status")
    if status not in {"pass", "fail", "not_checked"}:
        status = "pass" if check.get("checked") is True and not fail_codes else "fail"
    return {
        "fixture_group": str(check.get("fixture_group") or "unspecified"),
        "checked": check.get("checked") is True,
        "status": status,
        "fail_codes": [str(code) for code in fail_codes if isinstance(code, str)],
        "changed_pixel_ratio": _safe_float_or_zero(check.get("changed_pixel_ratio")),
        "color_mean_abs_delta": _safe_float_or_zero(check.get("color_mean_abs_delta")),
        "edge_energy_before": _safe_float_or_zero(check.get("edge_energy_before")),
        "edge_energy_after": _safe_float_or_zero(check.get("edge_energy_after")),
        "edge_energy_delta_ratio": _safe_float_or_zero(check.get("edge_energy_delta_ratio")),
        "max_changed_pixel_ratio": _safe_float_or_zero(check.get("max_changed_pixel_ratio")),
        "max_color_mean_abs_delta": _safe_float_or_zero(check.get("max_color_mean_abs_delta")),
        "max_edge_energy_delta_ratio": _safe_float_or_zero(check.get("max_edge_energy_delta_ratio")),
    }


def _blocking_codes(
    counts: dict[str, int],
    guardrails: dict[str, Any],
    audit_privacy: Any,
    fixture_context: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if counts["processed_files"] <= 0:
        blockers.append("no_processed_files")
    if counts["failed_files"] > 0:
        blockers.append("processing_failed_files")
    if counts["retry_list_files"] > 0:
        blockers.append("processing_retry_list_not_empty")
    if _safe_int(guardrails.get("failed_files")) > 0:
        blockers.append("processing_guardrail_failed_files")
    if isinstance(audit_privacy, dict):
        if audit_privacy.get("aggregate_only") is not True:
            blockers.append("audit_not_aggregate_only")
        for field in ("contains_paths", "contains_hashes", "contains_thumbnails", "contains_ocr_text", "contains_image_content"):
            if audit_privacy.get(field) is True:
                blockers.append(f"audit_privacy_{field}")
    protected_content_checks = fixture_context.get("protected_content_checks")
    if isinstance(protected_content_checks, list) and any(
        isinstance(check, dict) and check.get("status") != "pass" for check in protected_content_checks
    ):
        blockers.append("protected_content_check_failed")
    return blockers


def _privacy_payload() -> dict[str, bool]:
    return {
        "aggregate_only": True,
        "public_safe": True,
        "contains_file_list": False,
        "contains_paths": False,
        "contains_filenames": False,
        "contains_hashes": False,
        "contains_thumbnails": False,
        "contains_ocr_text": False,
        "contains_image_content": False,
        "contains_environment_values": False,
        "contains_row_level_evidence": False,
    }


def _sum_fields(counts: dict[str, int], fields: tuple[str, ...]) -> int:
    return sum(counts.get(field, 0) for field in fields)


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float | None:
    if not isinstance(value, int | float):
        return None
    return round(float(value), 6)


def _safe_float_or_zero(value: Any) -> float:
    safe_value = _safe_float(value)
    return safe_value if safe_value is not None else 0.0
