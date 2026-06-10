"""Public-safe synthetic smoke run for image-processing capability."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

from .processing import ProcessingOptions, process_images
from .processing_quality_summary import (
    PROCESSING_QUALITY_SUMMARY_JSON,
    build_processing_quality_summary,
    write_processing_quality_summary,
)
from .scanner import ScanConfig, scan_batch


IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON = "image_processing_capability_smoke.json"
SCHEMA_VERSION = "scan-qc.image-processing-capability-smoke.v1"

_STABLE_OPERATION_FIELDS = (
    "auto_crop",
    "deskew",
    "trim_dark_border",
    "scanner_gutter_trim",
    "despeckle",
    "normalize_tones",
    "normalize_paper_color_cast",
    "lighten_edge_shadow",
    "lighten_corner_shadows",
    "lighten_background_stains",
    "lighten_fold_shadows",
    "level_illumination_gradient",
    "clean_bleed_through",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
)

_SYNTHETIC_FIXTURE_GROUPS = (
    "clean_text_page",
    "skewed_text_page",
    "dark_border_page",
    "scanner_gutter_page",
    "speckled_text_page",
    "faded_shadow_page",
    "color_cast_page",
    "low_contrast_text_page",
    "illumination_gradient_page",
    "scanline_page",
    "bleed_through_page",
    "broad_thin_paper_bleed_through_page",
    "corner_shadow_page",
    "background_stain_page",
    "fold_shadow_page",
    "blurred_text_edges_page",
    "ultra_pale_typed_text_page",
    "low_saturation_carbon_text_page",
    "mixed_photo_stamp_table_page",
)

_PUBLIC_OPERATION_REASON_CODE_KEYS = {
    "faded_text": (
        "applied_stable_low_saturation_text",
        "applied_print_clean_stable_low_saturation_text",
    ),
}

_REQUIRED_OPERATION_REASON_CODE_BLOCKERS = (
    (
        "faded_text",
        "applied_stable_low_saturation_text",
        "low_saturation_faded_text_reason_not_observed",
    ),
)

_REQUIRED_OPERATION_COUNT_BLOCKERS = {
    "deskewed_files": "deskew_not_applied",
    "dark_border_trimmed_files": "dark_border_trim_not_applied",
    "scanner_gutter_trimmed_files": "scanner_gutter_trim_not_applied",
    "despeckled_files": "despeckle_not_applied",
    "tone_normalized_files": "tone_normalization_not_applied",
    "paper_color_cast_normalized_files": "paper_color_cast_not_normalized",
    "edge_shadow_lightened_files": "edge_shadow_not_lightened",
    "corner_shadows_lightened_files": "corner_shadows_not_lightened",
    "background_stains_lightened_files": "background_stains_not_lightened",
    "fold_shadows_lightened_files": "fold_shadows_not_lightened",
    "illumination_gradient_levelled_files": "illumination_gradient_not_levelled",
    "bleed_through_cleaned_files": "bleed_through_not_cleaned",
    "scanlines_lightened_files": "scanlines_not_lightened",
    "faded_text_enhanced_files": "faded_text_not_enhanced",
    "text_edges_sharpened_files": "text_edges_not_sharpened",
}

_REQUIRED_QUALITY_METRIC_MINIMA = (
    ("deskew_abs_angle_degrees", "max", 0.3, "deskew_angle_evidence_below_min"),
    ("max_trim_margin_ratio", "max", 0.04, "trim_margin_evidence_below_min"),
    ("scanner_gutter_max_trim_margin_ratio", "max", 0.04, "scanner_gutter_evidence_below_min"),
    ("tone_background_delta", "max", 6.0, "tone_background_delta_below_min"),
    ("tone_contrast_delta", "max", 40.0, "tone_contrast_delta_below_min"),
    ("tone_changed_pixel_ratio", "max", 0.05, "tone_changed_pixel_ratio_below_min"),
    ("paper_color_cast_delta", "max", 4.0, "paper_color_cast_delta_below_min"),
    ("paper_color_cast_changed_pixel_ratio", "max", 0.5, "paper_color_cast_changed_ratio_below_min"),
    ("edge_shadow_delta", "max", 8.0, "edge_shadow_delta_below_min"),
    ("edge_shadow_changed_pixel_ratio", "max", 0.02, "edge_shadow_changed_ratio_below_min"),
    ("corner_shadows_delta", "max", 2.5, "corner_shadows_delta_below_min"),
    ("corner_shadows_changed_pixel_ratio", "max", 0.02, "corner_shadows_changed_ratio_below_min"),
    ("background_stains_delta", "max", 6.0, "background_stains_delta_below_min"),
    ("background_stains_changed_pixel_ratio", "max", 0.01, "background_stains_changed_ratio_below_min"),
    ("fold_shadows_delta", "max", 4.0, "fold_shadows_delta_below_min"),
    ("illumination_gradient_correction_delta", "max", 8.0, "illumination_gradient_delta_below_min"),
    (
        "illumination_gradient_changed_pixel_ratio",
        "max",
        0.7,
        "illumination_gradient_changed_ratio_below_min",
    ),
    ("bleed_through_delta", "max", 3.0, "bleed_through_delta_below_min"),
    ("scanlines_delta", "max", 4.0, "scanlines_delta_below_min"),
    ("faded_text_delta", "max", 3.0, "faded_text_delta_below_min"),
    ("text_edges_delta", "max", 3.0, "text_edges_delta_below_min"),
)

_REQUIRED_QUALITY_METRIC_COMPARISONS = (
    (
        "text_edges_edge_energy_after",
        "max",
        "text_edges_edge_energy_before",
        "max",
        "text_edge_energy_not_improved",
    ),
)

_MIXED_CONTENT_MAX_CHANGED_PIXEL_RATIO = 0.01
_MIXED_CONTENT_MAX_COLOR_MEAN_ABS_DELTA = 1.0
_MIXED_CONTENT_MAX_EDGE_ENERGY_DELTA_RATIO = 0.02


def run_image_processing_capability_smoke(
    *,
    output_path: Path | None = None,
    despeckle_backend: str = "fallback",
    workers: int = 1,
    generated_at: str | None = None,
) -> tuple[Path | None, dict[str, Any], Path | None]:
    """Run scan/process over generated fixtures and optionally write a summary."""

    if despeckle_backend not in {"fallback", "numpy"}:
        raise ValueError("despeckle_backend must be 'fallback' or 'numpy'.")
    if workers < 1:
        raise ValueError("workers must be a positive integer.")

    with tempfile.TemporaryDirectory(prefix="ai4-image-processing-capability-") as temp_dir:
        root = Path(temp_dir)
        input_dir = root / "input"
        scan_dir = root / "scan"
        process_dir = root / "processed"
        fixture_count = _write_synthetic_fixtures(input_dir)
        source_bytes_before = _source_bytes(input_dir)
        report = scan_batch(
            ScanConfig(
                project_id="synthetic-capability",
                batch_id="image-processing-smoke",
                input_dir=input_dir,
                output_dir=scan_dir,
                min_dpi=200,
                workers=workers,
            )
        )
        options = ProcessingOptions(
            auto_crop=True,
            deskew=True,
            trim_dark_border=True,
            scanner_gutter_trim=True,
            despeckle=True,
            normalize_tones=True,
            normalize_paper_color_cast=True,
            lighten_edge_shadow=True,
            lighten_corner_shadows=True,
            lighten_background_stains=True,
            lighten_fold_shadows=True,
            level_illumination_gradient=True,
            clean_bleed_through=True,
            lighten_scanlines=True,
            enhance_faded_text=True,
            sharpen_text_edges=True,
            despeckle_content_type_check=False,
            despeckle_backend=despeckle_backend,
            workers=workers,
        )
        manifest = process_images(report, input_dir, process_dir, options)
        source_images_modified = source_bytes_before != _source_bytes(input_dir)
        audit_summary = _load_json(process_dir / "processing_audit_summary.json")
        protected_content_checks = [
            _mixed_content_protection_check(
                input_dir=input_dir,
                process_dir=process_dir,
                fixture_index=fixture_count,
            )
        ]
        quality_summary = build_processing_quality_summary(
            manifest=manifest,
            audit_summary=audit_summary,
            fixture_context={
                "source": "generated_at_runtime",
                "synthetic_inputs_only": True,
                "fixture_count": fixture_count,
                "fixture_groups": list(_SYNTHETIC_FIXTURE_GROUPS),
                "protected_content_checks": protected_content_checks,
            },
            generated_at=generated_at,
        )
        payload = _build_summary(
            fixture_count=fixture_count,
            report=report,
            manifest=manifest,
            audit_summary=audit_summary,
            quality_summary=quality_summary,
            protected_content_checks=protected_content_checks,
            source_images_modified=source_images_modified,
            despeckle_backend=despeckle_backend,
            workers=workers,
            generated_at=generated_at,
        )

    path = write_image_processing_capability_smoke(payload, output_path) if output_path else None
    quality_path = write_processing_quality_summary(quality_summary, output_path) if output_path else None
    return path, payload, quality_path


def write_image_processing_capability_smoke(payload: dict[str, Any], output_path: Path) -> Path:
    path = output_path / IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON if output_path.suffix == "" else output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _build_summary(
    *,
    fixture_count: int,
    report: dict[str, Any],
    manifest: dict[str, Any],
    audit_summary: dict[str, Any],
    quality_summary: dict[str, Any],
    protected_content_checks: list[dict[str, Any]],
    source_images_modified: bool,
    despeckle_backend: str,
    workers: int,
    generated_at: str | None,
) -> dict[str, Any]:
    scan_summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    processing_summary = manifest.get("summary", {}) if isinstance(manifest.get("summary"), dict) else {}
    audit_counts = audit_summary.get("counts", {}) if isinstance(audit_summary.get("counts"), dict) else {}
    audit_privacy = audit_summary.get("privacy", {}) if isinstance(audit_summary.get("privacy"), dict) else {}
    operation_timings = _public_operation_timings(audit_summary)
    operation_reason_code_counts = _public_operation_reason_code_counts(audit_summary)
    blockers = _blocking_codes(
        fixture_count=fixture_count,
        scan_summary=scan_summary,
        processing_summary=processing_summary,
        audit_counts=audit_counts,
        audit_privacy=audit_privacy,
        quality_summary=quality_summary,
        protected_content_checks=protected_content_checks,
        operation_reason_code_counts=operation_reason_code_counts,
        source_images_modified=source_images_modified,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": "pass" if not blockers else "fail",
        "blocking_codes": blockers,
        "privacy": {
            "aggregate_only": True,
            "public_safe": True,
            "synthetic_inputs_only": True,
            "private_inputs_read": False,
            "contains_file_list": False,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "contains_environment_values": False,
        },
        "processing_run": {
            "scan_run": True,
            "image_processing_run": True,
            "provider_commands_run": False,
            "source_images_modified": source_images_modified,
            "derivative_images_written": _safe_int(processing_summary.get("processed_files")) > 0,
            "temp_work_paths_published": False,
        },
        "synthetic_fixture_summary": {
            "fixture_count": fixture_count,
            "fixture_source": "generated_at_runtime",
            "fixture_groups": list(_SYNTHETIC_FIXTURE_GROUPS),
            "private_source_images_required": False,
        },
        "quality_baseline": quality_summary,
        "protected_content_checks": protected_content_checks,
        "related_public_safe_artifacts": {
            "image_processing_capability_smoke": IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON,
            "processing_quality_summary": PROCESSING_QUALITY_SUMMARY_JSON,
        },
        "stable_processing_contract": {
            "despeckle_backend_requested": despeckle_backend,
            "workers_requested": workers,
            "operations_enabled": list(_STABLE_OPERATION_FIELDS),
        },
        "counts": {
            "synthetic_fixture_count": fixture_count,
            "total_files": _safe_int(scan_summary.get("total_files")),
            "openable_files": _safe_int(scan_summary.get("openable_files")),
            "p0_findings": _safe_int(scan_summary.get("p0_findings")),
            "p1_findings": _safe_int(scan_summary.get("p1_findings")),
            "p2_findings": _safe_int(scan_summary.get("p2_findings")),
            "processed_files": _safe_int(processing_summary.get("processed_files")),
            "failed_files": _safe_int(processing_summary.get("failed_files")),
            "retry_list_files": _safe_int(processing_summary.get("retry_list_files")),
            "processing_warning_files": _safe_int(audit_counts.get("processing_warning_files")),
            "guardrail_failed_files": _safe_int(audit_counts.get("guardrail_failed_files")),
        },
        "operation_counts": _operation_counts(audit_counts),
        "operation_reason_code_counts": operation_reason_code_counts,
        "backend_summary": {
            "despeckle_backend_requested": despeckle_backend,
            "despeckle_backend_modes": _despeckle_backend_counts(operation_timings),
        },
        "operation_timings": operation_timings,
        "source_semantics": {
            "source_images_modified": source_images_modified,
            "originals_read_only": not source_images_modified,
            "derivatives_only": True,
        },
    }


def _blocking_codes(
    *,
    fixture_count: int,
    scan_summary: dict[str, Any],
    processing_summary: dict[str, Any],
    audit_counts: dict[str, Any],
    audit_privacy: dict[str, Any],
    quality_summary: dict[str, Any] | None = None,
    protected_content_checks: list[dict[str, Any]],
    operation_reason_code_counts: dict[str, dict[str, int]],
    source_images_modified: bool,
) -> list[str]:
    blockers: list[str] = []
    if _safe_int(scan_summary.get("total_files")) != fixture_count:
        blockers.append("synthetic_fixture_count_mismatch")
    if _safe_int(scan_summary.get("openable_files")) != fixture_count:
        blockers.append("synthetic_openability_mismatch")
    if _safe_int(processing_summary.get("processed_files")) <= 0:
        blockers.append("no_derivative_images_processed")
    if _safe_int(processing_summary.get("failed_files")) != 0:
        blockers.append("processing_failed_files")
    if _safe_int(processing_summary.get("retry_list_files")) != 0:
        blockers.append("processing_retry_list_not_empty")
    for field, blocker in _REQUIRED_OPERATION_COUNT_BLOCKERS.items():
        if _safe_int(audit_counts.get(field)) <= 0:
            blockers.append(blocker)
    reason_counts = operation_reason_code_counts if isinstance(operation_reason_code_counts, dict) else {}
    for operation, reason_code, blocker in _REQUIRED_OPERATION_REASON_CODE_BLOCKERS:
        operation_counts = reason_counts.get(operation)
        if not isinstance(operation_counts, dict) or _safe_int(operation_counts.get(reason_code)) <= 0:
            blockers.append(blocker)
    blockers.extend(_quality_metric_blockers(quality_summary))
    if _safe_int(audit_counts.get("guardrail_failed_files")) != 0:
        blockers.append("processing_guardrail_failed_files")
    if source_images_modified:
        blockers.append("source_images_modified")
    if audit_privacy.get("aggregate_only") is not True:
        blockers.append("audit_not_aggregate_only")
    for field in ("contains_paths", "contains_hashes", "contains_thumbnails", "contains_ocr_text"):
        if audit_privacy.get(field) is True:
            blockers.append(f"audit_privacy_{field}")
    if any(check.get("status") != "pass" for check in protected_content_checks):
        blockers.append("protected_content_check_failed")
    return blockers


def _quality_metric_blockers(quality_summary: dict[str, Any] | None) -> list[str]:
    if not isinstance(quality_summary, dict):
        return ["quality_metric_summary_missing"]
    metrics = quality_summary.get("quality_metrics")
    if not isinstance(metrics, dict):
        return ["quality_metric_summary_missing"]
    blockers: list[str] = []
    for metric_name, statistic_name, minimum, blocker in _REQUIRED_QUALITY_METRIC_MINIMA:
        metric = metrics.get(metric_name)
        if not isinstance(metric, dict) or _safe_float(metric.get(statistic_name)) < minimum:
            blockers.append(blocker)
    for after_metric_name, after_statistic, before_metric_name, before_statistic, blocker in (
        _REQUIRED_QUALITY_METRIC_COMPARISONS
    ):
        after_metric = metrics.get(after_metric_name)
        before_metric = metrics.get(before_metric_name)
        after_value = _safe_float(after_metric.get(after_statistic)) if isinstance(after_metric, dict) else 0.0
        before_value = _safe_float(before_metric.get(before_statistic)) if isinstance(before_metric, dict) else 0.0
        if after_value <= before_value:
            blockers.append(blocker)
    return blockers


def _public_operation_timings(audit_summary: dict[str, Any]) -> dict[str, Any]:
    timing = audit_summary.get("timing")
    if not isinstance(timing, dict):
        return {}
    operation_timings = timing.get("operation_timings")
    if not isinstance(operation_timings, dict):
        return {}
    public_timings: dict[str, Any] = {}
    for operation in _STABLE_OPERATION_FIELDS:
        raw = operation_timings.get(operation)
        if not isinstance(raw, dict):
            continue
        public_timings[operation] = {
            key: raw[key]
            for key in (
                "enabled",
                "file_count",
                "elapsed_seconds",
                "files_per_minute",
                "average_seconds_per_file",
                "backend_mode",
                "backend_counts",
                "numpy_available",
            )
            if key in raw
        }
    return public_timings


def _operation_counts(audit_counts: dict[str, Any]) -> dict[str, int]:
    fields = (
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
    )
    return {field: _safe_int(audit_counts.get(field)) for field in fields}


def _public_operation_reason_code_counts(audit_summary: dict[str, Any]) -> dict[str, dict[str, int]]:
    guardrails = audit_summary.get("guardrails")
    guardrails = guardrails if isinstance(guardrails, dict) else {}
    payload: dict[str, dict[str, int]] = {}
    for operation, reason_codes in _PUBLIC_OPERATION_REASON_CODE_KEYS.items():
        operation_summary = guardrails.get(operation)
        operation_summary = operation_summary if isinstance(operation_summary, dict) else {}
        reason_distribution = operation_summary.get("reason_code_distribution")
        reason_distribution = reason_distribution if isinstance(reason_distribution, dict) else {}
        payload[operation] = {
            reason_code: _safe_int(reason_distribution.get(reason_code))
            for reason_code in reason_codes
        }
    return payload


def _despeckle_backend_counts(operation_timings: dict[str, Any]) -> dict[str, int]:
    despeckle = operation_timings.get("despeckle")
    if not isinstance(despeckle, dict):
        return {}
    counts = despeckle.get("backend_counts")
    if not isinstance(counts, dict):
        return {}
    return {str(key): _safe_int(value) for key, value in counts.items()}


def _mixed_content_protection_check(*, input_dir: Path, process_dir: Path, fixture_index: int) -> dict[str, Any]:
    source_path = input_dir / f"synthetic_fixture_{fixture_index:03d}.png"
    processed_path = process_dir / "images" / source_path.name
    limits = {
        "max_changed_pixel_ratio": _MIXED_CONTENT_MAX_CHANGED_PIXEL_RATIO,
        "max_color_mean_abs_delta": _MIXED_CONTENT_MAX_COLOR_MEAN_ABS_DELTA,
        "max_edge_energy_delta_ratio": _MIXED_CONTENT_MAX_EDGE_ENERGY_DELTA_RATIO,
    }
    if not source_path.exists() or not processed_path.exists():
        return {
            "fixture_group": "mixed_photo_stamp_table_page",
            "checked": False,
            "status": "fail",
            "fail_codes": ["protected_fixture_missing"],
            "changed_pixel_ratio": 1.0,
            "color_mean_abs_delta": 255.0,
            "edge_energy_before": 0.0,
            "edge_energy_after": 0.0,
            "edge_energy_delta_ratio": 1.0,
            **limits,
        }

    source = Image.open(source_path).convert("RGB")
    processed = Image.open(processed_path).convert("RGB")
    comparable = processed.resize(source.size, Image.Resampling.BILINEAR) if processed.size != source.size else processed
    changed_pixel_ratio = _changed_pixel_ratio(source, comparable)
    color_mean_abs_delta = _color_mean_abs_delta(source, comparable)
    edge_energy_before = _edge_energy(source)
    edge_energy_after = _edge_energy(comparable)
    edge_energy_delta_ratio = abs(edge_energy_after - edge_energy_before) / max(edge_energy_before, 1.0)

    fail_codes: list[str] = []
    if changed_pixel_ratio > _MIXED_CONTENT_MAX_CHANGED_PIXEL_RATIO:
        fail_codes.append("protected_changed_pixel_ratio_exceeded")
    if color_mean_abs_delta > _MIXED_CONTENT_MAX_COLOR_MEAN_ABS_DELTA:
        fail_codes.append("protected_color_delta_exceeded")
    if edge_energy_delta_ratio > _MIXED_CONTENT_MAX_EDGE_ENERGY_DELTA_RATIO:
        fail_codes.append("protected_edge_energy_delta_exceeded")

    return {
        "fixture_group": "mixed_photo_stamp_table_page",
        "checked": True,
        "status": "pass" if not fail_codes else "fail",
        "fail_codes": fail_codes,
        "changed_pixel_ratio": round(changed_pixel_ratio, 6),
        "color_mean_abs_delta": round(color_mean_abs_delta, 6),
        "edge_energy_before": round(edge_energy_before, 6),
        "edge_energy_after": round(edge_energy_after, 6),
        "edge_energy_delta_ratio": round(edge_energy_delta_ratio, 6),
        **limits,
    }


def _changed_pixel_ratio(source: Image.Image, processed: Image.Image) -> float:
    diff = ImageChops.difference(source, processed).convert("L")
    changed = diff.point(lambda value: 255 if value > 8 else 0).histogram()[255]
    return changed / max(1, source.width * source.height)


def _color_mean_abs_delta(source: Image.Image, processed: Image.Image) -> float:
    diff = ImageChops.difference(source, processed)
    mean = ImageStat.Stat(diff).mean
    return sum(mean[:3]) / 3.0


def _edge_energy(image: Image.Image) -> float:
    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    return ImageStat.Stat(edges).mean[0]


def _write_synthetic_fixtures(input_dir: Path) -> int:
    input_dir.mkdir(parents=True, exist_ok=True)
    fixtures = (
        _clean_text_page(),
        _skewed_text_page(),
        _dark_border_page(),
        _scanner_gutter_page(),
        _speckled_text_page(),
        _faded_shadow_page(),
        _color_cast_page(),
        _low_contrast_text_page(),
        _illumination_gradient_page(),
        _scanline_page(),
        _bleed_through_page(),
        _broad_thin_paper_bleed_through_page(),
        _corner_shadow_page(),
        _background_stain_page(),
        _fold_shadow_page(),
        _blurred_text_edges_page(),
        _ultra_pale_typed_text_page(),
        _low_saturation_carbon_text_page(),
        _mixed_photo_stamp_table_page(),
    )
    for index, image in enumerate(fixtures, start=1):
        image.save(input_dir / f"synthetic_fixture_{index:03d}.png", dpi=(300, 300))
    return len(fixtures)


def _clean_text_page() -> Image.Image:
    image = Image.new("RGB", (280, 220), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((36, 28, 244, 190), outline=(35, 35, 35), width=2)
    for y in range(54, 164, 22):
        draw.rectangle((58, y, 220, y + 5), fill=(24, 24, 24))
    return image


def _skewed_text_page() -> Image.Image:
    image = Image.new("RGB", (420, 560), (246, 246, 246))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    lines = (
        "ARCHIVE REGISTER PAGE",
        "STABLE BODY TEXT ROW",
        "SMALL ANGLE DESKEW",
        "PUBLIC SAFE SYNTHETIC",
        "NEUTRAL PAPER SAMPLE",
        "ROW EVIDENCE ONLY",
        "FINAL TEXT LINE",
    )
    for index, line in enumerate(lines):
        draw.text((74, 112 + index * 38), line, fill=(78, 78, 78), font=font)
    return image.rotate(
        -0.65,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(246, 246, 246),
    )


def _dark_border_page() -> Image.Image:
    image = _clean_text_page()
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 16, 219), fill=(2, 2, 2))
    draw.rectangle((264, 0, 279, 219), fill=(2, 2, 2))
    return image


def _scanner_gutter_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 0, 239, 179), fill=(248, 248, 248))
    draw.rectangle((36, 32, 204, 148), outline=(60, 60, 60), width=2)
    return image


def _speckled_text_page() -> Image.Image:
    image = _clean_text_page()
    draw = ImageDraw.Draw(image)
    for x, y in ((30, 35), (48, 170), (132, 42), (230, 174), (250, 96), (74, 118)):
        draw.point((x, y), fill=(0, 0, 0))
    return image


def _faded_shadow_page() -> Image.Image:
    image = Image.new("RGB", (280, 220), (232, 232, 228))
    draw = ImageDraw.Draw(image)
    for y in range(56, 164, 22):
        draw.rectangle((58, y, 220, y + 4), fill=(168, 168, 165))
    for x in range(0, 38):
        shade = 170 + int(x * 1.5)
        draw.line((x, 0, x, 219), fill=(shade, shade, shade))
    return image


def _color_cast_page() -> Image.Image:
    image = Image.new("RGB", (280, 220), (236, 230, 214))
    draw = ImageDraw.Draw(image)
    draw.rectangle((34, 30, 246, 190), outline=(80, 76, 68), width=1)
    for y in range(58, 164, 24):
        draw.rectangle((58, y, 218, y + 4), fill=(70, 68, 62))
    return image


def _low_contrast_text_page() -> Image.Image:
    image = Image.new("RGB", (280, 220), (226, 226, 222))
    draw = ImageDraw.Draw(image)
    for y in range(58, 164, 24):
        draw.rectangle((58, y, 218, y + 4), fill=(164, 164, 160))
    return image


def _illumination_gradient_page() -> Image.Image:
    width, height = 320, 240
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            value = int(226 + (246 - 226) * x / max(1, width - 1))
            pixels[x, y] = (value, value, value)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for index, line in enumerate(("ARCHIVE", "REGISTER", "PAGE")):
        draw.text((72, 70 + index * 28), line, fill=(90, 90, 90), font=font)
    return image


def _scanline_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (42, 64, 86):
        draw.rectangle((42, y, 158, y + 5), fill=(36, 36, 36))
    for x0, x1 in ((16, 48), (96, 128), (196, 228)):
        draw.rectangle((x0, 132, x1, 133), fill=(226, 226, 222))
    return image


def _bleed_through_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (244, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.text((34, 36), "REAL", fill=(70, 70, 70))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((124, 82), "321", fill=255)
    mask_draw.text((124, 104), "654", fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(2.4))
    ghost = Image.new("RGB", image.size, (232, 232, 228))
    image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.55)))
    return image


def _broad_thin_paper_bleed_through_page() -> Image.Image:
    image = Image.new("RGB", (300, 210), (245, 242, 238))
    draw = ImageDraw.Draw(image)
    for y in (30, 56, 82, 108, 134):
        draw.line((30, y, 140, y), fill=(76, 76, 76), width=1)

    ghost_mask = Image.new("L", image.size, 0)
    ghost_draw = ImageDraw.Draw(ghost_mask)
    ghost_draw.rectangle((106, 54, 264, 146), fill=118)
    ghost_draw.text((148, 70), "741", fill=186)
    ghost_draw.text((166, 98), "852", fill=186)
    ghost_draw.text((154, 126), "963", fill=186)
    ghost_mask = ghost_mask.filter(ImageFilter.GaussianBlur(6.2))
    ghost_overlay = Image.new("RGB", image.size, (236, 233, 226))
    image.paste(ghost_overlay, (0, 0), ghost_mask)

    draw.rectangle((40, 38, 128, 42), fill=(66, 66, 66))
    draw.rectangle((40, 92, 128, 96), fill=(66, 66, 66))
    return image


def _corner_shadow_page() -> Image.Image:
    image = _clean_text_page()
    draw = ImageDraw.Draw(image)
    for radius in range(62, 0, -4):
        shade = 160 + radius
        draw.pieslice((-radius, -radius, radius, radius), 0, 360, fill=(shade, shade, shade))
    return image


def _background_stain_page() -> Image.Image:
    image = Image.new("RGB", (240, 170), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (42, 66, 90):
        draw.rectangle((48, y, 178, y + 5), fill=(38, 38, 38))
    draw.ellipse((58, 116, 80, 134), fill=(216, 216, 211))
    draw.ellipse((188, 18, 210, 34), fill=(218, 218, 214))
    draw.rectangle((196, 112, 208, 124), fill=(214, 214, 210))
    return image


def _fold_shadow_page() -> Image.Image:
    image = Image.new("RGB", (220, 160), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    draw.rectangle((109, 14, 110, 146), fill=(234, 234, 230))
    return image


def _blurred_text_edges_page() -> Image.Image:
    image = Image.new("RGB", (420, 560), (245, 245, 242))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    lines = (
        "ARCHIVE QUALITY CONTROL PAGE",
        "TYPED TEXT EDGES ARE SOFT",
        "REVIEW SHOULD STAY SAFE",
        "PRINTED STROKES ONLY",
        "LOCAL BATCH SAMPLE",
        "NEUTRAL LIGHT PAPER",
        "MILD BLUR CASE",
        "STABLE ROW STRUCTURE",
        "FINAL TEXT LINE",
    )
    for index, line in enumerate(lines):
        draw.text((64, 100 + index * 34), line, fill=(72, 72, 72), font=font)
    return image.filter(ImageFilter.GaussianBlur(radius=0.75))


def _ultra_pale_typed_text_page() -> Image.Image:
    image = Image.new("RGB", (420, 560), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    lines = (
        "ARCHIVE REGISTER 1948",
        "TYPED PAGE SAMPLE",
        "PAGE HAS VERY PALE PRINT",
        "LOW CONTRAST TEXT",
        "SMALL STABLE GLYPHS",
        "SYNTHETIC VALIDATION",
        "PUBLIC SAFE FIXTURE",
    )
    for index, line in enumerate(lines):
        draw.text((64, 104 + index * 34), line, fill=(232, 232, 232), font=font)
    return image


def _low_saturation_carbon_text_page() -> Image.Image:
    image = Image.new("RGB", (360, 240), (245, 247, 250))
    draw = ImageDraw.Draw(image)
    ink = (162, 184, 206)
    for y in (56, 86, 116):
        draw.rectangle((48, y, 124, y + 4), fill=ink)
        draw.rectangle((144, y, 224, y + 4), fill=ink)
    return image


def _mixed_photo_stamp_table_page() -> Image.Image:
    image = Image.new("RGB", (280, 220), (244, 242, 234))
    draw = ImageDraw.Draw(image)
    draw.rectangle((34, 30, 150, 114), fill=(180, 194, 198), outline=(80, 92, 96))
    for x in range(34, 151, 8):
        shade = 120 + (x % 40)
        draw.line((x, 31, x, 113), fill=(shade, shade + 12, shade + 20))
    draw.ellipse((184, 40, 238, 94), outline=(170, 34, 34), width=4)
    for y in range(132, 190, 18):
        draw.line((38, y, 240, y), fill=(40, 40, 38), width=1)
    for x in range(38, 241, 42):
        draw.line((x, 132, x, 186), fill=(40, 40, 38), width=1)
    for y in (142, 160, 178):
        draw.rectangle((52, y, 132, y + 3), fill=(52, 52, 48))
    return image


def _source_bytes(input_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(input_dir.glob("*")) if path.is_file()}


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return payload


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
