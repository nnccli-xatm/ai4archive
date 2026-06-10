"""Dry-run processing plans for scanned-image batches."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .processing import ProcessingOptions, _process_image


PROCESSING_PLAN_JSON = "processing_plan.json"
PROCESSING_PLAN_CSV = "processing_plan.csv"


def write_processing_plan(
    report_path: Path,
    input_dir: Path,
    out_dir: Path,
    options: ProcessingOptions | None = None,
    process_dir: Path | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = _load_report(report_path)
    plan = build_processing_plan(report, input_dir, options, process_dir=process_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / PROCESSING_PLAN_JSON
    csv_path = out_dir / PROCESSING_PLAN_CSV
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_plan_csv(plan, csv_path)
    return json_path, csv_path, plan


def build_processing_plan(
    report: dict[str, Any],
    input_dir: Path,
    options: ProcessingOptions | None = None,
    process_dir: Path | None = None,
) -> dict[str, Any]:
    options = options or ProcessingOptions()
    input_dir = input_dir.resolve()
    previous_records = _load_previous_records(process_dir) if (process_dir and options.resume_processing) else {}
    
    records = [
        _plan_record(
            item,
            input_dir,
            options,
            process_dir=process_dir,
            previous_record=previous_records.get(item.get("relative_path")),
        )
        for item in report.get("files", [])
    ]
    planned_files = sum(1 for record in records if record["status"] == "planned")
    skipped_files = sum(1 for record in records if record["status"] == "skipped")
    unopenable_files = sum(1 for record in records if record["status"] == "unopenable")
    warning_files = sum(1 for record in records if record.get("processing_warnings"))
    counts = {
        "total_files": len(records),
        "planned_files": planned_files,
        "skipped_files": skipped_files,
        "unopenable_files": unopenable_files,
        "processing_warning_files": warning_files,
        "exif_transpose_candidates": _operation_count(records, "exif_transpose"),
        "deskew_candidates": sum(1 for record in records if record.get("deskew_candidate")),
        "dark_border_trim_candidates": sum(1 for record in records if record.get("dark_border_trim_candidate")),
        "scanner_gutter_trim_candidates": sum(1 for record in records if record.get("scanner_gutter_trim_candidate")),
        "auto_crop_candidates": sum(1 for record in records if record.get("auto_crop_candidate")),
        "despeckle_candidates": sum(1 for record in records if record.get("despeckle_candidate")),
        "tone_normalization_candidates": sum(1 for record in records if record.get("tone_normalization_candidate")),
        "paper_color_cast_normalization_candidates": sum(
            1 for record in records if record.get("paper_color_cast_normalization_candidate")
        ),
        "edge_shadow_lightening_candidates": sum(1 for record in records if record.get("edge_shadow_lightening_candidate")),
        "corner_shadow_cleanup_candidates": sum(1 for record in records if record.get("corner_shadow_cleanup_candidate")),
        "background_stain_lightening_candidates": sum(
            1 for record in records if record.get("background_stain_lightening_candidate")
        ),
        "scanline_lightening_candidates": sum(1 for record in records if record.get("scanline_lightening_candidate")),
        "illumination_gradient_leveling_candidates": sum(
            1 for record in records if record.get("illumination_gradient_leveling_candidate")
        ),
        "faded_text_enhancement_candidates": sum(
            1 for record in records if record.get("faded_text_enhancement_candidate")
        ),
        "text_edge_sharpening_candidates": sum(
            1 for record in records if record.get("text_edge_sharpening_candidate")
        ),
    }
    return {
        "schema_version": "scan-qc.processing-plan.v1",
        "generated_from_report_at": report.get("generated_at"),
        "source_report_schema_version": report.get("schema_version"),
        "project": report.get("project", {}),
        "operations": {
            "auto_crop": options.auto_crop,
            "deskew": options.deskew,
            "trim_dark_border": options.trim_dark_border,
            "scanner_gutter_trim": options.scanner_gutter_trim,
            "despeckle": options.despeckle,
            "normalize_tones": options.normalize_tones,
            "normalize_paper_color_cast": options.normalize_paper_color_cast,
            "lighten_edge_shadow": options.lighten_edge_shadow,
            "lighten_corner_shadows": options.lighten_corner_shadows,
            "lighten_background_stains": options.lighten_background_stains,
            "lighten_fold_shadows": options.lighten_fold_shadows,
            "level_illumination_gradient": options.level_illumination_gradient,
            "clean_bleed_through": options.clean_bleed_through,
            "lighten_scanlines": options.lighten_scanlines,
            "enhance_faded_text": options.enhance_faded_text,
            "sharpen_text_edges": options.sharpen_text_edges,
            "processing_profile": options.processing_profile,
            "resume_processing": options.resume_processing,
            "reuse_scan_measurements": options.reuse_scan_measurements,
        },
        "summary": counts,
        "privacy": {
            "sensitivity": "sensitive_local_evidence",
            "contains_file_list": True,
            "contains_paths": True,
            "contains_hashes": True,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "embeds_images": False,
            "public_safe": False,
        },
        "review": {
            "purpose": "Operator review before enabling --process-out.",
            "writes_derivative_images": False,
            "may_feed_process_out_review": True,
        },
        "files": records,
    }


def _load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError("scan report must be a JSON object with a files list")
    return payload


def _load_previous_records(process_dir: Path | None) -> dict[str, dict[str, Any]]:
    if process_dir is None:
        return {}
    manifest_path = process_dir / "processing_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if payload.get("schema_version") != "scan-qc.processing.v1" or not isinstance(payload.get("files"), list):
        return {}
    records: dict[str, dict[str, Any]] = {}
    for record in payload["files"]:
        if isinstance(record, dict):
            source_relative_path = record.get("source_relative_path")
            if isinstance(source_relative_path, str) and source_relative_path:
                records[source_relative_path] = record
    return records


def _plan_record(
    item: dict[str, Any],
    input_dir: Path,
    options: ProcessingOptions,
    process_dir: Path | None = None,
    previous_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    relative_path = item.get("relative_path")
    record = {
        "source_relative_path": relative_path,
        "source_sha256": item.get("sha256"),
        "status": "skipped",
        "proposed_operations": [],
        "original_size": None,
        "planned_output_size": None,
        "pre_deskew_size": None,
        "post_deskew_size": None,
        "skew_angle_degrees": None,
        "skew_confidence": 0.0,
        "deskew_candidate": False,
        "deskew_reason": None,
        "dark_border_trim_candidate": False,
        "dark_border_bbox": None,
        "dark_border_reason": None,
        "scanner_gutter_trim_candidate": False,
        "scanner_gutter_bbox": None,
        "scanner_gutter_reason": None,
        "scanner_gutter_trim_margins": {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0},
        "auto_crop_candidate": False,
        "crop_bbox": None,
        "despeckle_candidate": False,
        "despeckle_pixels_changed": 0,
        "despeckle_reason": None,
        "tone_normalization_candidate": False,
        "tone_reason": None,
        "paper_color_cast_normalization_candidate": False,
        "paper_color_cast_reason": None,
        "paper_color_cast_reason_code": None,
        "paper_color_cast_delta": 0.0,
        "paper_color_cast_brightness_delta": 0.0,
        "paper_color_cast_changed_pixel_ratio": 0.0,
        "paper_color_cast_candidate_pixel_ratio": 0.0,
        "edge_shadow_lightening_candidate": False,
        "edge_shadow_reason": None,
        "edge_shadow_edges": [],
        "corner_shadow_cleanup_candidate": False,
        "corner_shadows_reason": None,
        "corner_shadows_reason_code": None,
        "corner_shadows_corners": [],
        "background_stain_lightening_candidate": False,
        "background_stains_reason": None,
        "illumination_gradient_leveling_candidate": False,
        "illumination_gradient_reason": None,
        "illumination_gradient_reason_code": None,
        "illumination_gradient_orientation": None,
        "illumination_gradient_changed_pixel_ratio": 0.0,
        "illumination_gradient_candidate_pixel_ratio": 0.0,
        "scanline_lightening_candidate": False,
        "scanlines_reason": None,
        "scanlines_orientation": None,
        "faded_text_enhancement_candidate": False,
        "faded_text_reason": None,
        "faded_text_delta": 0.0,
        "faded_text_changed_pixel_ratio": 0.0,
        "faded_text_candidate_pixel_ratio": 0.0,
        "text_edge_sharpening_candidate": False,
        "text_edges_reason": None,
        "text_edges_delta": 0.0,
        "text_edges_changed_pixel_ratio": 0.0,
        "text_edges_candidate_pixel_ratio": 0.0,
        "processing_audit": None,
        "processing_warnings": [],
        "failure_reason": None,
        "scan_measurements_reused": False,
        "scan_measurement_reuse_reason": None,
        "existing_derivative_reused": False,
    }
    if not isinstance(relative_path, str) or not relative_path:
        record["failure_reason"] = "missing relative_path"
        return record
    if not item.get("openable"):
        record["status"] = "unopenable"
        record["failure_reason"] = "source image is not openable"
        return record

    source = input_dir / relative_path
    
    # Check if existing derivative can be reused when resume_processing is enabled
    if options.resume_processing and previous_record:
        if _can_reuse_derivative(source, previous_record, options, process_dir=process_dir):
            record["status"] = "planned"
            record["existing_derivative_reused"] = True
            record["proposed_operations"] = previous_record.get("proposed_operations", [])
            # Reuse metadata from previous successful processing
            for key in [
                "original_size", "planned_output_size", "deskew_candidate", "deskew_reason",
                "dark_border_trim_candidate", "auto_crop_candidate", "despeckle_candidate",
                "tone_normalization_candidate", "paper_color_cast_normalization_candidate",
                "edge_shadow_lightening_candidate", "corner_shadow_cleanup_candidate",
                "background_stain_lightening_candidate", "illumination_gradient_leveling_candidate",
                "scanline_lightening_candidate", "faded_text_enhancement_candidate",
                "text_edge_sharpening_candidate"
            ]:
                if key in previous_record:
                    record[key] = previous_record[key]
            return record

    # Always call _process_image for compatibility with existing tests
    # The reuse_scan_measurements optimization happens inside _process_image
    try:
        with Image.open(source) as image:
            _processed, operations, process_info = _process_image(image, options, scan_record=item)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        record["status"] = "unopenable"
        record["failure_reason"] = str(exc)
        return record
    
    # Track scan measurement reuse for audit purposes
    if options.reuse_scan_measurements:
        # Check if the operations indicate scan measurements were reused
        scan_measurements_reused = any(
            op in operations 
            for op in [
                "skew_detect_reused_scan_measurement",
                "dark_border_detect_reused_scan_measurement"
            ]
        )
        record["scan_measurements_reused"] = scan_measurements_reused
        if scan_measurements_reused:
            record["scan_measurement_reuse_reason"] = "scan_measurements_available"
    
    record.update(
        {
            "status": "planned",
            "proposed_operations": operations,
            "original_size": process_info["original_size"],
            "planned_output_size": process_info["output_size"],
            "pre_deskew_size": process_info["pre_deskew_size"],
            "post_deskew_size": process_info["post_deskew_size"],
            "skew_angle_degrees": process_info["skew_angle_degrees"],
            "skew_confidence": process_info["skew_confidence"],
            "deskew_candidate": process_info["deskewed"],
            "deskew_reason": process_info["deskew_reason"],
            "dark_border_trim_candidate": process_info["dark_border_trimmed"],
            "dark_border_bbox": process_info["dark_border_bbox"],
            "dark_border_reason": process_info["dark_border_reason"],
            "scanner_gutter_trim_candidate": process_info["scanner_gutter_trimmed"],
            "scanner_gutter_bbox": process_info["scanner_gutter_bbox"],
            "scanner_gutter_reason": process_info["scanner_gutter_reason"],
            "scanner_gutter_trim_margins": process_info["scanner_gutter_trim_margins"],
            "auto_crop_candidate": process_info["cropped"],
            "crop_bbox": process_info["crop_bbox"],
            "despeckle_candidate": process_info["despeckled"],
            "despeckle_pixels_changed": process_info["despeckle_pixels_changed"],
            "despeckle_reason": process_info["despeckle_reason"],
            "tone_normalization_candidate": process_info["tone_normalized"],
            "tone_reason": process_info["tone_reason"],
            "paper_color_cast_normalization_candidate": process_info["paper_color_cast_normalized"],
            "paper_color_cast_reason": process_info["paper_color_cast_reason"],
            "paper_color_cast_reason_code": process_info["paper_color_cast_reason_code"],
            "paper_color_cast_delta": process_info["paper_color_cast_delta"],
            "paper_color_cast_brightness_delta": process_info["paper_color_cast_brightness_delta"],
            "paper_color_cast_changed_pixel_ratio": process_info["paper_color_cast_changed_pixel_ratio"],
            "paper_color_cast_candidate_pixel_ratio": process_info["paper_color_cast_candidate_pixel_ratio"],
            "edge_shadow_lightening_candidate": process_info["edge_shadow_lightened"],
            "edge_shadow_reason": process_info["edge_shadow_reason"],
            "edge_shadow_edges": process_info["edge_shadow_edges"],
            "corner_shadow_cleanup_candidate": process_info["corner_shadows_lightened"],
            "corner_shadows_reason": process_info["corner_shadows_reason"],
            "corner_shadows_reason_code": process_info["corner_shadows_reason_code"],
            "corner_shadows_corners": process_info["corner_shadows_corners"],
            "background_stain_lightening_candidate": process_info["background_stains_lightened"],
            "background_stains_reason": process_info["background_stains_reason"],
            "illumination_gradient_leveling_candidate": process_info["illumination_gradient_levelled"],
            "illumination_gradient_reason": process_info["illumination_gradient_reason"],
            "illumination_gradient_reason_code": process_info["illumination_gradient_reason_code"],
            "illumination_gradient_orientation": process_info["illumination_gradient_orientation"],
            "illumination_gradient_changed_pixel_ratio": process_info["illumination_gradient_changed_pixel_ratio"],
            "illumination_gradient_candidate_pixel_ratio": process_info[
                "illumination_gradient_candidate_pixel_ratio"
            ],
            "scanline_lightening_candidate": process_info["scanlines_lightened"],
            "scanlines_reason": process_info["scanlines_reason"],
            "scanlines_orientation": process_info["scanlines_orientation"],
            "faded_text_enhancement_candidate": process_info["faded_text_enhanced"],
            "faded_text_reason": process_info["faded_text_reason"],
            "faded_text_delta": process_info["faded_text_delta"],
            "faded_text_changed_pixel_ratio": process_info["faded_text_changed_pixel_ratio"],
            "faded_text_candidate_pixel_ratio": process_info["faded_text_candidate_pixel_ratio"],
            "text_edge_sharpening_candidate": process_info["text_edges_sharpened"],
            "text_edges_reason": process_info["text_edges_reason"],
            "text_edges_delta": process_info["text_edges_delta"],
            "text_edges_changed_pixel_ratio": process_info["text_edges_changed_pixel_ratio"],
            "text_edges_candidate_pixel_ratio": process_info["text_edges_candidate_pixel_ratio"],
            "processing_audit": process_info["processing_audit"],
            "processing_warnings": process_info["processing_warnings"],
        }
    )
    return record


def _can_reuse_derivative(
    source: Path,
    previous_record: dict[str, Any],
    options: ProcessingOptions,
    process_dir: Path | None = None,
) -> bool:
    if previous_record.get("status") not in {"processed", "resumed"}:
        return False
    if previous_record.get("source_sha256") != _compute_sha256_if_exists(source):
        return False
    if previous_record.get("processing_options_fingerprint") != _processing_options_fingerprint(options):
        return False
    output_relative_path = previous_record.get("output_relative_path")
    output_sha256 = previous_record.get("output_sha256")
    if not isinstance(output_relative_path, str) or not isinstance(output_sha256, str) or not output_sha256:
        return False
    if process_dir is None:
        return False

    process_root = process_dir.resolve()
    output_path = (process_root / output_relative_path).resolve()
    try:
        output_path.relative_to(process_root)
    except ValueError:
        return False

    return _compute_sha256_if_exists(output_path) == output_sha256


def _compute_sha256_if_exists(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    import hashlib
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _processing_options_fingerprint(options: ProcessingOptions) -> str:
    import hashlib
    key_data = json.dumps({
        "auto_crop": options.auto_crop,
        "deskew": options.deskew,
        "trim_dark_border": options.trim_dark_border,
        "scanner_gutter_trim": options.scanner_gutter_trim,
        "despeckle": options.despeckle,
        "normalize_tones": options.normalize_tones,
        "normalize_paper_color_cast": options.normalize_paper_color_cast,
        "lighten_edge_shadow": options.lighten_edge_shadow,
        "lighten_corner_shadows": options.lighten_corner_shadows,
        "lighten_background_stains": options.lighten_background_stains,
        "lighten_fold_shadows": options.lighten_fold_shadows,
        "level_illumination_gradient": options.level_illumination_gradient,
        "clean_bleed_through": options.clean_bleed_through,
        "lighten_scanlines": options.lighten_scanlines,
        "enhance_faded_text": options.enhance_faded_text,
        "sharpen_text_edges": options.sharpen_text_edges,
        "processing_profile": options.processing_profile,
        "deskew_max_degrees": options.deskew_max_degrees,
        "deskew_min_confidence": options.deskew_min_confidence,
    }, sort_keys=True)
    return hashlib.sha256(key_data.encode()).hexdigest()[:16]


def _operation_count(records: list[dict[str, Any]], operation: str) -> int:
    return sum(1 for record in records if operation in record.get("proposed_operations", []))


def _write_plan_csv(plan: dict[str, Any], path: Path) -> None:
    fieldnames = [
        "source_relative_path",
        "source_sha256",
        "status",
        "proposed_operations",
        "deskew_candidate",
        "skew_angle_degrees",
        "skew_confidence",
        "dark_border_trim_candidate",
        "dark_border_bbox",
        "scanner_gutter_trim_candidate",
        "scanner_gutter_bbox",
        "scanner_gutter_reason",
        "auto_crop_candidate",
        "crop_bbox",
        "despeckle_candidate",
        "despeckle_pixels_changed",
        "tone_normalization_candidate",
        "tone_reason",
        "paper_color_cast_normalization_candidate",
        "paper_color_cast_reason",
        "paper_color_cast_reason_code",
        "paper_color_cast_delta",
        "paper_color_cast_brightness_delta",
        "paper_color_cast_changed_pixel_ratio",
        "paper_color_cast_candidate_pixel_ratio",
        "edge_shadow_lightening_candidate",
        "edge_shadow_reason",
        "edge_shadow_edges",
        "corner_shadow_cleanup_candidate",
        "corner_shadows_reason",
        "corner_shadows_reason_code",
        "corner_shadows_corners",
        "background_stain_lightening_candidate",
        "background_stains_reason",
        "illumination_gradient_leveling_candidate",
        "illumination_gradient_reason",
        "illumination_gradient_reason_code",
        "illumination_gradient_orientation",
        "illumination_gradient_changed_pixel_ratio",
        "illumination_gradient_candidate_pixel_ratio",
        "scanline_lightening_candidate",
        "scanlines_reason",
        "scanlines_orientation",
        "faded_text_enhancement_candidate",
        "faded_text_reason",
        "faded_text_delta",
        "faded_text_changed_pixel_ratio",
        "faded_text_candidate_pixel_ratio",
        "text_edge_sharpening_candidate",
        "text_edges_reason",
        "text_edges_delta",
        "text_edges_changed_pixel_ratio",
        "text_edges_candidate_pixel_ratio",
        "processing_warnings",
        "failure_reason",
        "scan_measurements_reused",
        "scan_measurement_reuse_reason",
        "existing_derivative_reused",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in plan["files"]:
            writer.writerow({field: _csv_value(record.get(field)) for field in fieldnames})


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
