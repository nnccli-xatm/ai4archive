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
) -> tuple[Path, Path, dict[str, Any]]:
    report = _load_report(report_path)
    plan = build_processing_plan(report, input_dir, options)
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
) -> dict[str, Any]:
    options = options or ProcessingOptions()
    input_dir = input_dir.resolve()
    records = [_plan_record(item, input_dir, options) for item in report.get("files", [])]
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
        "auto_crop_candidates": sum(1 for record in records if record.get("auto_crop_candidate")),
        "despeckle_candidates": sum(1 for record in records if record.get("despeckle_candidate")),
        "tone_normalization_candidates": sum(1 for record in records if record.get("tone_normalization_candidate")),
        "edge_shadow_lightening_candidates": sum(1 for record in records if record.get("edge_shadow_lightening_candidate")),
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
            "despeckle": options.despeckle,
            "normalize_tones": options.normalize_tones,
            "lighten_edge_shadow": options.lighten_edge_shadow,
            "resume_processing": False,
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


def _plan_record(item: dict[str, Any], input_dir: Path, options: ProcessingOptions) -> dict[str, Any]:
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
        "auto_crop_candidate": False,
        "crop_bbox": None,
        "despeckle_candidate": False,
        "despeckle_pixels_changed": 0,
        "despeckle_reason": None,
        "tone_normalization_candidate": False,
        "tone_reason": None,
        "edge_shadow_lightening_candidate": False,
        "edge_shadow_reason": None,
        "edge_shadow_edges": [],
        "processing_audit": None,
        "processing_warnings": [],
        "failure_reason": None,
    }
    if not isinstance(relative_path, str) or not relative_path:
        record["failure_reason"] = "missing relative_path"
        return record
    if not item.get("openable"):
        record["status"] = "unopenable"
        record["failure_reason"] = "source image is not openable"
        return record

    source = input_dir / relative_path
    try:
        with Image.open(source) as image:
            _processed, operations, process_info = _process_image(image, options, scan_record=item)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        record["status"] = "unopenable"
        record["failure_reason"] = str(exc)
        return record

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
            "auto_crop_candidate": process_info["cropped"],
            "crop_bbox": process_info["crop_bbox"],
            "despeckle_candidate": process_info["despeckled"],
            "despeckle_pixels_changed": process_info["despeckle_pixels_changed"],
            "despeckle_reason": process_info["despeckle_reason"],
            "tone_normalization_candidate": process_info["tone_normalized"],
            "tone_reason": process_info["tone_reason"],
            "edge_shadow_lightening_candidate": process_info["edge_shadow_lightened"],
            "edge_shadow_reason": process_info["edge_shadow_reason"],
            "edge_shadow_edges": process_info["edge_shadow_edges"],
            "processing_audit": process_info["processing_audit"],
            "processing_warnings": process_info["processing_warnings"],
        }
    )
    return record


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
        "auto_crop_candidate",
        "crop_bbox",
        "despeckle_candidate",
        "despeckle_pixels_changed",
        "tone_normalization_candidate",
        "tone_reason",
        "edge_shadow_lightening_candidate",
        "edge_shadow_reason",
        "edge_shadow_edges",
        "processing_warnings",
        "failure_reason",
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
