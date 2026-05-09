"""Local derivative-image processing for scanned-image batches.

The processing layer never modifies source images. It writes derivative files
and a manifest that links each output back to the original scan record.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from PIL import Image, ImageChops, ImageOps, ImageStat, UnidentifiedImageError

from .concurrency import resolve_worker_count, worker_metadata


@dataclass(frozen=True)
class ProcessingOptions:
    auto_crop: bool = False
    deskew: bool = False
    trim_dark_border: bool = False
    despeckle: bool = False
    resume_processing: bool = False
    deskew_max_degrees: float = 5.0
    deskew_min_confidence: float = 0.08
    audit_max_size_change_ratio: float = 0.55
    audit_max_pixel_change_ratio: float = 0.60
    audit_max_brightness_delta: float = 80.0
    audit_max_contrast_delta: float = 80.0
    audit_max_crop_ratio: float = 0.55
    audit_max_trim_margin_ratio: float = 0.12
    audit_max_despeckle_pixel_ratio: float = 0.01
    workers: int | None = None


@dataclass(frozen=True)
class SkewDetection:
    angle_degrees: float | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class DarkBorderDetection:
    bbox: tuple[int, int, int, int] | None
    reason: str


def detect_skew(image: Image.Image) -> SkewDetection:
    return _detect_skew(image)


def detect_dark_border_bbox(image: Image.Image) -> DarkBorderDetection:
    return _detect_dark_border_bbox(image)


def process_images(
    report: dict[str, Any],
    input_dir: Path,
    process_dir: Path,
    options: ProcessingOptions | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    start_seconds = time.perf_counter()
    options = options or ProcessingOptions()
    input_dir = input_dir.resolve()
    process_dir = process_dir.resolve()
    image_root = process_dir / "images"
    process_workers = resolve_worker_count(options.workers, len(report["files"]))
    previous_records = _load_previous_records(process_dir) if options.resume_processing else {}
    records = _process_records(report["files"], input_dir, image_root, options, process_workers, previous_records)
    processed_files = sum(1 for item in records if item["status"] == "processed")
    resumed_files = sum(1 for item in records if item["status"] == "resumed")
    skipped_files = sum(1 for item in records if item["status"] == "skipped")
    failed_files = sum(1 for item in records if item["status"] == "failed")
    reprocessed_files = sum(1 for item in records if item.get("reprocessed"))
    finished_at = datetime.now(timezone.utc)
    performance = _performance_summary(
        started_at,
        finished_at,
        time.perf_counter() - start_seconds,
        total_files=len(records),
        processed_files=processed_files,
        skipped_files=skipped_files,
        failed_files=failed_files,
        workers=worker_metadata(options.workers, process_workers),
    )
    performance["operation_timings"] = _aggregate_operation_timings(records, options)
    for record in records:
        record.pop("operation_timings", None)
    manifest = {
        "schema_version": "scan-qc.processing.v1",
        "generated_at": finished_at.isoformat(),
        "project": report.get("project", {}),
        "source_report_schema_version": report.get("schema_version"),
        "process_dir": str(process_dir),
        "image_root": str(image_root),
        "summary": {
            "total_files": len(records),
            "processed_files": processed_files,
            "resumed_files": resumed_files,
            "skipped_due_to_resume": resumed_files,
            "reprocessed_files": reprocessed_files,
            "skipped_files": skipped_files,
            "failed_files": failed_files,
            "retry_list_files": failed_files,
            "performance": performance,
            "workers": process_workers,
            "worker_mode": performance["mode"],
        },
        "performance": performance,
        "operations": [
            "exif_transpose",
            "convert_non_l_or_rgb_to_rgb",
            "skew_detect_projection",
            "deskew_conservative" if options.deskew else "deskew_disabled",
            "dark_border_trim_conservative" if options.trim_dark_border else "dark_border_trim_disabled",
            "auto_crop_conservative" if options.auto_crop else "auto_crop_disabled",
            "despeckle_isolated_pixels" if options.despeckle else "despeckle_disabled",
            "autocontrast_cutoff_0_5",
            "preserve_source_relative_path",
        ],
        "resume": {
            "enabled": options.resume_processing,
            "previous_manifest_found": (process_dir / "processing_manifest.json").exists(),
            "skipped_due_to_resume": resumed_files,
            "reprocessed_files": reprocessed_files,
        },
        "files": records,
    }
    process_dir.mkdir(parents=True, exist_ok=True)
    (process_dir / "processing_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    retry_manifest = _retry_manifest(manifest)
    (process_dir / "processing_retry_manifest.json").write_text(
        json.dumps(retry_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    audit_summary = _audit_summary(manifest, options)
    (process_dir / "processing_audit_summary.json").write_text(
        json.dumps(audit_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _process_records(
    files: list[dict[str, Any]],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
    workers: int,
    previous_records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if workers == 1:
        return [_process_record(item, input_dir, image_root, options, previous_records.get(item["relative_path"])) for item in files]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(
            executor.map(
                lambda item: _process_record(item, input_dir, image_root, options, previous_records.get(item["relative_path"])),
                files,
            )
        )


def _performance_summary(
    started_at: datetime,
    finished_at: datetime,
    elapsed_seconds: float,
    *,
    total_files: int,
    processed_files: int,
    skipped_files: int,
    failed_files: int,
    workers: dict[str, Any],
) -> dict[str, Any]:
    elapsed_seconds = max(0.0, round(elapsed_seconds, 6))
    return {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "total_files": total_files,
        "processed_files": processed_files,
        "skipped_files": skipped_files,
        "failed_files": failed_files,
        **workers,
        "processed_files_per_minute": _files_per_minute(processed_files, elapsed_seconds),
        "total_files_per_minute": _files_per_minute(total_files, elapsed_seconds),
    }


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)


def _process_record(
    item: dict[str, Any],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
    previous_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    relative_path = item["relative_path"]
    base = {
        "source_relative_path": relative_path,
        "source_sha256": item.get("sha256"),
        "output_relative_path": None,
        "output_sha256": None,
        "original_size": None,
        "output_size": None,
        "pre_deskew_size": None,
        "post_deskew_size": None,
        "skew_angle_degrees": None,
        "skew_confidence": 0.0,
        "deskewed": False,
        "deskew_reason": None,
        "dark_border_trimmed": False,
        "dark_border_bbox": None,
        "dark_border_reason": None,
        "crop_bbox": None,
        "cropped": False,
        "despeckled": False,
        "despeckle_pixels_changed": 0,
        "despeckle_reason": None,
        "processing_audit": None,
        "processing_warnings": [],
        "operation_timings": {},
        "status": "skipped",
        "resumed": False,
        "reprocessed": False,
        "operations": [],
        "error": None,
        "failure_reason": None,
    }
    if previous_record and previous_record.get("status") in {"processed", "resumed"} and _previous_output_exists(previous_record, image_root):
        resumed = dict(previous_record)
        resumed["status"] = "resumed"
        resumed["resumed"] = True
        resumed["reprocessed"] = False
        resumed["error"] = None
        resumed["failure_reason"] = None
        resumed["operations"] = list(previous_record.get("operations", [])) + ["resume_skip_existing_derivative"]
        return resumed

    if options.resume_processing:
        base["reprocessed"] = previous_record is not None

    if not item.get("openable"):
        base["failure_reason"] = "source image is not openable"
        base["error"] = base["failure_reason"]
        return base

    source = input_dir / relative_path
    target = image_root / relative_path
    try:
        if source.resolve() == target.resolve():
            raise ValueError("derivative target would overwrite the source image")
        with Image.open(source) as image:
            processed, operations, process_info = _process_image(image, options)
            guardrail_failures = process_info["processing_audit"].get("guardrail_failures", [])
            if guardrail_failures:
                raise ValueError("processing guardrail exceeded: " + "; ".join(guardrail_failures))
            target.parent.mkdir(parents=True, exist_ok=True)
            _save_image(processed, target, image)
        base.update(
            {
                "output_relative_path": target.relative_to(image_root.parent).as_posix(),
                "output_sha256": _sha256(target),
                "original_size": process_info["original_size"],
                "output_size": process_info["output_size"],
                "pre_deskew_size": process_info["pre_deskew_size"],
                "post_deskew_size": process_info["post_deskew_size"],
                "skew_angle_degrees": process_info["skew_angle_degrees"],
                "skew_confidence": process_info["skew_confidence"],
                "deskewed": process_info["deskewed"],
                "deskew_reason": process_info["deskew_reason"],
                "dark_border_trimmed": process_info["dark_border_trimmed"],
                "dark_border_bbox": process_info["dark_border_bbox"],
                "dark_border_reason": process_info["dark_border_reason"],
                "crop_bbox": process_info["crop_bbox"],
                "cropped": process_info["cropped"],
                "despeckled": process_info["despeckled"],
                "despeckle_pixels_changed": process_info["despeckle_pixels_changed"],
                "despeckle_reason": process_info["despeckle_reason"],
                "processing_audit": process_info["processing_audit"],
                "processing_warnings": process_info["processing_warnings"],
                "operation_timings": process_info["operation_timings"],
                "status": "processed",
                "operations": operations,
            }
        )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        base["status"] = "failed"
        base["error"] = str(exc)
        base["failure_reason"] = str(exc)
        if "process_info" in locals():
            base.update(
                {
                    "original_size": process_info["original_size"],
                    "output_size": process_info["output_size"],
                    "pre_deskew_size": process_info["pre_deskew_size"],
                    "post_deskew_size": process_info["post_deskew_size"],
                    "skew_angle_degrees": process_info["skew_angle_degrees"],
                    "skew_confidence": process_info["skew_confidence"],
                    "deskewed": process_info["deskewed"],
                    "deskew_reason": process_info["deskew_reason"],
                    "dark_border_trimmed": process_info["dark_border_trimmed"],
                    "dark_border_bbox": process_info["dark_border_bbox"],
                    "dark_border_reason": process_info["dark_border_reason"],
                    "crop_bbox": process_info["crop_bbox"],
                    "cropped": process_info["cropped"],
                    "despeckled": process_info["despeckled"],
                    "despeckle_pixels_changed": process_info["despeckle_pixels_changed"],
                    "despeckle_reason": process_info["despeckle_reason"],
                    "processing_audit": process_info["processing_audit"],
                    "processing_warnings": process_info["processing_warnings"],
                    "operation_timings": process_info["operation_timings"],
                    "operations": operations,
                }
            )
    return base


def _load_previous_records(process_dir: Path) -> dict[str, dict[str, Any]]:
    manifest_path = process_dir / "processing_manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    previous: dict[str, dict[str, Any]] = {}
    for record in payload.get("files", []):
        source_relative_path = record.get("source_relative_path")
        if not isinstance(source_relative_path, str):
            continue
        previous[source_relative_path] = record
    return previous


def _previous_output_exists(record: dict[str, Any], image_root: Path) -> bool:
    output_relative_path = record.get("output_relative_path")
    if not isinstance(output_relative_path, str) or not output_relative_path:
        return False
    output_path = image_root.parent / output_relative_path
    try:
        output_path.resolve().relative_to(image_root.parent.resolve())
    except ValueError:
        return False
    return output_path.exists() and output_path.is_file()


def _retry_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    failed_records = [record for record in manifest["files"] if record.get("status") == "failed"]
    return {
        "schema_version": "scan-qc.processing.retry.v1",
        "generated_at": manifest["generated_at"],
        "source_processing_manifest": "processing_manifest.json",
        "summary": {
            "failed_files": len(failed_records),
            "retry_list_files": len(failed_records),
        },
        "files": failed_records,
    }


def _audit_summary(manifest: dict[str, Any], options: ProcessingOptions) -> dict[str, Any]:
    summary = manifest["summary"]
    performance = summary["performance"]
    audit_records = [record["processing_audit"] for record in manifest["files"] if isinstance(record.get("processing_audit"), dict)]
    warning_records = [record for record in manifest["files"] if record.get("processing_warnings")]
    guardrail_failures = [
        failure
        for audit in audit_records
        for failure in audit.get("guardrail_failures", [])
        if isinstance(failure, str)
    ]
    guardrail_failed_files = sum(1 for audit in audit_records if audit.get("guardrail_failures"))
    operation_timings = performance.get("operation_timings", {})
    return {
        "schema_version": "scan-qc.processing.audit.v1",
        "generated_at": manifest["generated_at"],
        "operations": {
            "auto_crop": options.auto_crop,
            "deskew": options.deskew,
            "trim_dark_border": options.trim_dark_border,
            "despeckle": options.despeckle,
            "resume_processing": options.resume_processing,
        },
        "workers": {
            "requested_workers": performance["requested_workers"],
            "effective_workers": performance["effective_workers"],
            "worker_cap": performance["worker_cap"],
            "mode": performance["mode"],
        },
        "timing": {
            "started_at": performance["started_at"],
            "finished_at": performance["finished_at"],
            "elapsed_seconds": performance["elapsed_seconds"],
            "operation_timings": operation_timings,
        },
        "counts": {
            "total_files": summary["total_files"],
            "processed_files": summary["processed_files"],
            "resumed_files": summary["resumed_files"],
            "skipped_due_to_resume": summary["skipped_due_to_resume"],
            "reprocessed_files": summary["reprocessed_files"],
            "skipped_files": summary["skipped_files"],
            "failed_files": summary["failed_files"],
            "retry_list_files": summary["retry_list_files"],
            "processing_warning_files": len(warning_records),
            "guardrail_failed_files": guardrail_failed_files,
        },
        "thresholds": _audit_thresholds(options),
        "metrics": {
            "size_change_ratio": _aggregate_metric(audit_records, "size_change_ratio"),
            "pixel_change_ratio": _aggregate_metric(audit_records, "pixel_change_ratio"),
            "brightness_delta": _aggregate_metric(audit_records, "brightness_delta"),
            "contrast_delta": _aggregate_metric(audit_records, "contrast_delta"),
            "crop_ratio": _aggregate_metric(audit_records, "crop_ratio"),
            "max_trim_margin_ratio": _aggregate_metric(audit_records, "max_trim_margin_ratio"),
            "deskew_abs_angle_degrees": _aggregate_metric(audit_records, "deskew_abs_angle_degrees"),
            "despeckle_pixel_ratio": _aggregate_metric(audit_records, "despeckle_pixel_ratio"),
        },
        "distributions": {
            "pixel_change_ratio": _ratio_distribution(audit_records, "pixel_change_ratio"),
            "crop_ratio": _ratio_distribution(audit_records, "crop_ratio"),
            "max_trim_margin_ratio": _ratio_distribution(audit_records, "max_trim_margin_ratio"),
            "despeckle_pixel_ratio": _ratio_distribution(audit_records, "despeckle_pixel_ratio"),
        },
        "guardrails": {
            "enabled": True,
            "warning_files": len(warning_records),
            "failed_files": guardrail_failed_files,
            "failure_reasons": _reason_counts(guardrail_failures),
        },
        "throughput": {
            "processed_files_per_minute": performance["processed_files_per_minute"],
            "total_files_per_minute": performance["total_files_per_minute"],
        },
        "privacy": {
            "aggregate_only": True,
            "contains_file_list": False,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
        },
    }


def _aggregate_operation_timings(records: list[dict[str, Any]], options: ProcessingOptions) -> dict[str, dict[str, Any]]:
    enabled = {
        "auto_crop": options.auto_crop,
        "deskew": options.deskew,
        "trim_dark_border": options.trim_dark_border,
        "despeckle": options.despeckle,
    }
    timings: dict[str, dict[str, Any]] = {}
    for operation, is_enabled in enabled.items():
        values = [
            float(record["operation_timings"][operation]["elapsed_seconds"])
            for record in records
            if isinstance(record.get("operation_timings"), dict)
            and isinstance(record["operation_timings"].get(operation), dict)
            and isinstance(record["operation_timings"][operation].get("elapsed_seconds"), int | float)
        ]
        elapsed_seconds = round(sum(values), 6)
        timings[operation] = {
            "enabled": is_enabled,
            "file_count": len(values),
            "elapsed_seconds": elapsed_seconds,
            "files_per_minute": _files_per_minute(len(values), elapsed_seconds),
            "average_seconds_per_file": round(elapsed_seconds / len(values), 6) if values else None,
        }
    return timings


def _audit_thresholds(options: ProcessingOptions) -> dict[str, float]:
    return {
        "max_size_change_ratio": options.audit_max_size_change_ratio,
        "max_pixel_change_ratio": options.audit_max_pixel_change_ratio,
        "max_brightness_delta": options.audit_max_brightness_delta,
        "max_contrast_delta": options.audit_max_contrast_delta,
        "max_crop_ratio": options.audit_max_crop_ratio,
        "max_trim_margin_ratio": options.audit_max_trim_margin_ratio,
        "max_despeckle_pixel_ratio": options.audit_max_despeckle_pixel_ratio,
        "max_deskew_degrees": options.deskew_max_degrees,
    }


def _aggregate_metric(records: list[dict[str, Any]], key: str) -> dict[str, float | int | None]:
    values = [float(record[key]) for record in records if isinstance(record.get(key), int | float)]
    if not values:
        return {"count": 0, "average": None, "max": None}
    return {"count": len(values), "average": round(sum(values) / len(values), 6), "max": round(max(values), 6)}


def _ratio_distribution(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    buckets = {"0": 0, "0-0.01": 0, "0.01-0.05": 0, "0.05-0.10": 0, "0.10-0.25": 0, "0.25+": 0}
    for record in records:
        value = record.get(key)
        if not isinstance(value, int | float):
            continue
        if value == 0:
            buckets["0"] += 1
        elif value <= 0.01:
            buckets["0-0.01"] += 1
        elif value <= 0.05:
            buckets["0.01-0.05"] += 1
        elif value <= 0.10:
            buckets["0.05-0.10"] += 1
        elif value <= 0.25:
            buckets["0.10-0.25"] += 1
        else:
            buckets["0.25+"] += 1
    return buckets


def _reason_counts(reasons: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _process_image(image: Image.Image, options: ProcessingOptions) -> tuple[Image.Image, list[str], dict[str, Any]]:
    operations: list[str] = []
    operation_timings: dict[str, dict[str, float]] = {}
    processed = ImageOps.exif_transpose(image)
    operations.append("exif_transpose")
    original_size = list(processed.size)

    if processed.mode not in {"L", "RGB"}:
        processed = processed.convert("RGB")
        operations.append("convert_to_rgb")
    audit_source = processed.copy()

    pre_deskew_size = list(processed.size)
    post_deskew_size = list(processed.size)
    with _operation_timer(operation_timings, "deskew", enabled=options.deskew):
        skew = _detect_skew(processed)
        operations.append("skew_detect_projection")
        deskewed = False
        deskew_reason = skew.reason
        if not options.deskew:
            operations.append("deskew_disabled")
            deskew_reason = "deskew disabled"
        elif skew.angle_degrees is None:
            operations.append("deskew_noop")
        elif skew.confidence < options.deskew_min_confidence:
            operations.append("deskew_noop")
            deskew_reason = "low confidence"
        elif abs(skew.angle_degrees) > options.deskew_max_degrees:
            operations.append("deskew_noop")
            deskew_reason = "angle exceeds conservative threshold"
        elif abs(skew.angle_degrees) < 0.2:
            operations.append("deskew_noop")
            deskew_reason = "angle below correction threshold"
        else:
            processed = _rotate_for_deskew(processed, -skew.angle_degrees)
            operations.append("deskew_conservative")
            post_deskew_size = list(processed.size)
            deskewed = True
            deskew_reason = "deskew applied"

    dark_border = DarkBorderDetection(None, "dark border trim disabled")
    dark_border_trimmed = False
    with _operation_timer(operation_timings, "trim_dark_border", enabled=options.trim_dark_border):
        if options.trim_dark_border:
            dark_border = _detect_dark_border_bbox(processed)
            if dark_border.bbox:
                processed = processed.crop(dark_border.bbox)
                operations.append("dark_border_trim_conservative")
                dark_border_trimmed = True
            else:
                operations.append("dark_border_trim_noop")
        else:
            operations.append("dark_border_trim_disabled")

    crop_bbox: tuple[int, int, int, int] | None = None
    with _operation_timer(operation_timings, "auto_crop", enabled=options.auto_crop):
        if options.auto_crop:
            crop_bbox = _detect_conservative_crop_bbox(processed)
            if crop_bbox:
                processed = processed.crop(crop_bbox)
                operations.append("auto_crop_conservative")
            else:
                operations.append("auto_crop_noop")
        else:
            operations.append("auto_crop_disabled")

    despeckled = False
    despeckle_pixels_changed = 0
    despeckle_reason = "despeckle disabled"
    with _operation_timer(operation_timings, "despeckle", enabled=options.despeckle):
        if options.despeckle:
            processed, despeckle_pixels_changed = _despeckle_isolated_pixels(processed)
            if despeckle_pixels_changed:
                operations.append("despeckle_isolated_pixels")
                despeckled = True
                despeckle_reason = "isolated dark pixels replaced"
            else:
                operations.append("despeckle_noop")
                despeckle_reason = "no isolated dark pixels found"
        else:
            operations.append("despeckle_disabled")

    processed = ImageOps.autocontrast(processed, cutoff=0.5)
    operations.append("autocontrast_cutoff_0_5")
    processing_audit = _processing_audit(audit_source, processed, options, crop_bbox, dark_border.bbox, skew.angle_degrees, despeckle_pixels_changed)
    processing_warnings = list(processing_audit["guardrail_failures"])
    crop_info = {
        "original_size": original_size,
        "output_size": list(processed.size),
        "pre_deskew_size": pre_deskew_size,
        "post_deskew_size": post_deskew_size,
        "skew_angle_degrees": skew.angle_degrees,
        "skew_confidence": skew.confidence,
        "deskewed": deskewed,
        "deskew_reason": deskew_reason,
        "dark_border_trimmed": dark_border_trimmed,
        "dark_border_bbox": list(dark_border.bbox) if dark_border.bbox else None,
        "dark_border_reason": dark_border.reason,
        "crop_bbox": list(crop_bbox) if crop_bbox else None,
        "cropped": crop_bbox is not None,
        "despeckled": despeckled,
        "despeckle_pixels_changed": despeckle_pixels_changed,
        "despeckle_reason": despeckle_reason,
        "processing_audit": processing_audit,
        "processing_warnings": processing_warnings,
        "operation_timings": operation_timings,
    }
    return processed, operations, crop_info


class _operation_timer:
    def __init__(self, timings: dict[str, dict[str, float]], operation: str, *, enabled: bool) -> None:
        self.timings = timings
        self.operation = operation
        self.enabled = enabled
        self.started_at = 0.0

    def __enter__(self) -> None:
        self.started_at = time.perf_counter()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.enabled:
            self.timings[self.operation] = {
                "elapsed_seconds": max(0.0, round(time.perf_counter() - self.started_at, 6)),
            }


def _processing_audit(
    source: Image.Image,
    processed: Image.Image,
    options: ProcessingOptions,
    crop_bbox: tuple[int, int, int, int] | None,
    dark_border_bbox: tuple[int, int, int, int] | None,
    skew_angle_degrees: float | None,
    despeckle_pixels_changed: int,
) -> dict[str, Any]:
    source_width, source_height = source.size
    output_width, output_height = processed.size
    source_area = max(1, source_width * source_height)
    output_area = max(1, output_width * output_height)
    size_change_ratio = abs(output_area - source_area) / source_area
    crop_ratio = 0.0
    if crop_bbox:
        crop_ratio = 1.0 - (((crop_bbox[2] - crop_bbox[0]) * (crop_bbox[3] - crop_bbox[1])) / source_area)
    trim_margins = _trim_margins(source.size, dark_border_bbox)
    max_trim_margin_ratio = max(trim_margins.values()) if trim_margins else 0.0
    source_l = source.convert("L")
    processed_l = processed.convert("L")
    brightness_delta, contrast_delta = _tonal_deltas(source_l, processed_l)
    pixel_change_ratio = _pixel_change_ratio(source_l, processed_l)
    deskew_abs_angle = round(abs(skew_angle_degrees or 0.0), 6)
    despeckle_pixel_ratio = despeckle_pixels_changed / source_area
    metrics = {
        "size_change_ratio": round(size_change_ratio, 6),
        "pixel_change_ratio": round(pixel_change_ratio, 6),
        "brightness_delta": round(brightness_delta, 6),
        "contrast_delta": round(contrast_delta, 6),
        "crop_ratio": round(max(0.0, crop_ratio), 6),
        "trim_margins": trim_margins,
        "max_trim_margin_ratio": round(max_trim_margin_ratio, 6),
        "deskew_abs_angle_degrees": deskew_abs_angle,
        "despeckle_pixel_ratio": round(despeckle_pixel_ratio, 6),
    }
    failures = _audit_guardrail_failures(metrics, options)
    return {**metrics, "guardrail_failures": failures}


def _trim_margins(size: tuple[int, int], bbox: tuple[int, int, int, int] | None) -> dict[str, float]:
    width, height = size
    if not bbox:
        return {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}
    left, top, right, bottom = bbox
    return {
        "left": round(left / width, 6) if width else 0.0,
        "top": round(top / height, 6) if height else 0.0,
        "right": round((width - right) / width, 6) if width else 0.0,
        "bottom": round((height - bottom) / height, 6) if height else 0.0,
    }


def _tonal_deltas(source: Image.Image, processed: Image.Image) -> tuple[float, float]:
    comparable = processed.resize(source.size, Image.Resampling.BILINEAR) if processed.size != source.size else processed
    source_stat = ImageStat.Stat(source)
    processed_stat = ImageStat.Stat(comparable)
    return abs(source_stat.mean[0] - processed_stat.mean[0]), abs(source_stat.stddev[0] - processed_stat.stddev[0])


def _pixel_change_ratio(source: Image.Image, processed: Image.Image) -> float:
    comparable = processed.resize(source.size, Image.Resampling.BILINEAR) if processed.size != source.size else processed
    diff = ImageChops.difference(source, comparable)
    histogram = diff.point(lambda value: 255 if value > 8 else 0).histogram()
    changed = sum(histogram[1:])
    return changed / max(1, source.size[0] * source.size[1])


def _audit_guardrail_failures(metrics: dict[str, Any], options: ProcessingOptions) -> list[str]:
    checks = [
        ("size_change_ratio", options.audit_max_size_change_ratio),
        ("pixel_change_ratio", options.audit_max_pixel_change_ratio),
        ("brightness_delta", options.audit_max_brightness_delta),
        ("contrast_delta", options.audit_max_contrast_delta),
        ("crop_ratio", options.audit_max_crop_ratio),
        ("max_trim_margin_ratio", options.audit_max_trim_margin_ratio),
        ("despeckle_pixel_ratio", options.audit_max_despeckle_pixel_ratio),
    ]
    failures = []
    for key, threshold in checks:
        value = metrics.get(key)
        if isinstance(value, int | float) and value > threshold:
            failures.append(key)
    return failures


def _detect_skew(image: Image.Image) -> SkewDetection:
    width, height = image.size
    if width < 30 or height < 30:
        return SkewDetection(None, 0.0, "image too small")

    grayscale = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    histogram = grayscale.histogram()
    total_pixels = width * height
    low = _histogram_percentile(histogram, total_pixels, 0.05)
    high = _histogram_percentile(histogram, total_pixels, 0.95)
    if high - low < 35:
        return SkewDetection(None, 0.0, "low contrast")

    threshold = max(0, min(255, low + int((high - low) * 0.35)))
    ink = grayscale.point(lambda value: 255 if value <= threshold else 0, mode="L")
    bbox = ink.getbbox()
    if not bbox:
        return SkewDetection(None, 0.0, "blank page")

    ink_ratio = _nonzero_ratio(ink, bbox)
    if ink_ratio < 0.002:
        return SkewDetection(None, 0.0, "insufficient foreground")
    if ink_ratio > 0.65:
        return SkewDetection(None, 0.0, "foreground too dense")

    sample = ink.crop(bbox)
    sample.thumbnail((700, 700), Image.Resampling.BILINEAR)
    background = 0
    scores: list[tuple[float, float]] = []
    for correction_angle in _frange(-7.0, 7.0, 0.25):
        rotated = sample.rotate(correction_angle, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=background)
        scores.append((correction_angle, _horizontal_projection_variance(rotated)))

    scores.sort(key=lambda item: item[1], reverse=True)
    best_angle, best_score = scores[0]
    runner_up = max(score for angle, score in scores if abs(angle - best_angle) >= 1.0)
    confidence = 0.0 if best_score <= 0 else max(0.0, min(1.0, (best_score - runner_up) / best_score))
    skew_angle = round(-best_angle, 2)
    return SkewDetection(skew_angle, round(confidence, 3), "skew detected")


def _histogram_percentile(histogram: list[int], total: int, percentile: float) -> int:
    target = total * percentile
    running = 0
    for value, count in enumerate(histogram):
        running += count
        if running >= target:
            return value
    return 255


def _nonzero_ratio(image: Image.Image, bbox: tuple[int, int, int, int]) -> float:
    histogram = image.crop(bbox).histogram()
    foreground = sum(histogram[1:])
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    return foreground / area if area else 0.0


def _frange(start: float, stop: float, step: float) -> list[float]:
    count = int(round((stop - start) / step))
    return [start + index * step for index in range(count + 1)]


def _horizontal_projection_variance(image: Image.Image) -> float:
    width, height = image.size
    pixels = image.load()
    row_counts = []
    for y in range(height):
        row_counts.append(sum(1 for x in range(width) if pixels[x, y] > 0))
    mean = sum(row_counts) / len(row_counts)
    return sum((count - mean) ** 2 for count in row_counts) / len(row_counts)


def _rotate_for_deskew(image: Image.Image, correction_angle: float) -> Image.Image:
    fill = _corner_background_value(image.convert("L"))
    fillcolor: int | tuple[int, int, int]
    if image.mode == "RGB":
        fillcolor = (fill, fill, fill)
    else:
        fillcolor = fill
    return image.rotate(correction_angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=fillcolor)


def _detect_conservative_crop_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    width, height = image.size
    if width < 20 or height < 20:
        return None

    grayscale = image.convert("L")
    background = _corner_background_value(grayscale)
    diff = grayscale.point(lambda value: 255 if abs(value - background) >= 18 else 0)
    bbox = diff.getbbox()
    if not bbox:
        return None

    left, top, right, bottom = bbox
    if min(left, top, width - right, height - bottom) < 2:
        return None

    crop_width = right - left
    crop_height = bottom - top
    crop_area_ratio = (crop_width * crop_height) / (width * height)
    if crop_area_ratio < 0.25 or crop_area_ratio > 0.98:
        return None

    return bbox


def _detect_dark_border_bbox(image: Image.Image) -> DarkBorderDetection:
    width, height = image.size
    if width < 40 or height < 40:
        return DarkBorderDetection(None, "image too small")

    grayscale = image.convert("L")
    max_x = max(2, int(width * 0.08))
    max_y = max(2, int(height * 0.08))
    min_retain_width = int(width * 0.88)
    min_retain_height = int(height * 0.88)
    left = _dark_edge_run(grayscale, "left", max_x)
    right = _dark_edge_run(grayscale, "right", max_x)
    top = _dark_edge_run(grayscale, "top", max_y)
    bottom = _dark_edge_run(grayscale, "bottom", max_y)

    if max(left, right, top, bottom) < 2:
        return DarkBorderDetection(None, "no confident dark edge border")

    bbox = (left, top, width - right, height - bottom)
    retained_width = bbox[2] - bbox[0]
    retained_height = bbox[3] - bbox[1]
    if retained_width < min_retain_width or retained_height < min_retain_height:
        return DarkBorderDetection(None, "candidate trim exceeds conservative retain ratio")
    if retained_width <= 0 or retained_height <= 0:
        return DarkBorderDetection(None, "invalid trim candidate")

    return DarkBorderDetection(bbox, "dark edge border trimmed")


def _dark_edge_run(image: Image.Image, side: str, max_pixels: int) -> int:
    width, height = image.size
    pixels = image.load()
    run = 0
    for offset in range(max_pixels):
        if side == "left":
            values = [pixels[offset, y] for y in range(height)]
        elif side == "right":
            values = [pixels[width - 1 - offset, y] for y in range(height)]
        elif side == "top":
            values = [pixels[x, offset] for x in range(width)]
        else:
            values = [pixels[x, height - 1 - offset] for x in range(width)]
        dark_ratio = sum(1 for value in values if value <= 45) / len(values)
        mean = sum(values) / len(values)
        if dark_ratio >= 0.72 and mean <= 80:
            run = offset + 1
        else:
            break
    return run


def _despeckle_isolated_pixels(image: Image.Image) -> tuple[Image.Image, int]:
    grayscale = image.convert("L")
    width, height = grayscale.size
    if width < 3 or height < 3:
        return image.copy(), 0

    candidate_bbox = grayscale.point(lambda value: 255 if value <= 60 else 0, mode="L").getbbox()
    if not candidate_bbox:
        return image.copy(), 0

    left, top, right, bottom = candidate_bbox
    x_start = max(1, left)
    y_start = max(1, top)
    x_stop = min(width - 1, right)
    y_stop = min(height - 1, bottom)
    if x_start >= x_stop or y_start >= y_stop:
        return image.copy(), 0

    gray_pixels = grayscale.load()
    source = image.convert("RGB") if image.mode != "RGB" else image.copy()
    output = source.copy()
    source_pixels = source.load()
    output_pixels = output.load()
    changed = 0
    for y in range(y_start, y_stop):
        for x in range(x_start, x_stop):
            if gray_pixels[x, y] > 60:
                continue
            dark_neighbors = 0
            neighbor_values: list[int] = []
            neighbor_rgb: list[tuple[int, int, int]] = []
            for ny in range(y - 1, y + 2):
                for nx in range(x - 1, x + 2):
                    if nx == x and ny == y:
                        continue
                    value = gray_pixels[nx, ny]
                    neighbor_values.append(value)
                    neighbor_rgb.append(source_pixels[nx, ny])
                    if value <= 90:
                        dark_neighbors += 1
            if dark_neighbors > 1:
                continue
            wider_dark = 0
            for ny in range(max(0, y - 2), min(height, y + 3)):
                for nx in range(max(0, x - 2), min(width, x + 3)):
                    if nx == x and ny == y:
                        continue
                    if gray_pixels[nx, ny] <= 90:
                        wider_dark += 1
            if wider_dark > 2:
                continue
            median_gray = sorted(neighbor_values)[len(neighbor_values) // 2]
            if median_gray < 120:
                continue
            replacement = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*neighbor_rgb))
            output_pixels[x, y] = replacement
            changed += 1

    if image.mode == "L":
        return output.convert("L"), changed
    if image.mode == "RGB":
        return output, changed
    return output.convert(image.mode), changed


def _corner_background_value(image: Image.Image) -> int:
    width, height = image.size
    sample = max(3, min(width, height) // 20)
    corners = [
        image.crop((0, 0, sample, sample)),
        image.crop((width - sample, 0, width, sample)),
        image.crop((0, height - sample, sample, height)),
        image.crop((width - sample, height - sample, width, height)),
    ]
    values = []
    for corner in corners:
        histogram = corner.histogram()
        total = sum(value * count for value, count in enumerate(histogram))
        values.append(int(round(total / (sample * sample))))
    return sorted(values)[len(values) // 2]


def _save_image(image: Image.Image, target: Path, source_image: Image.Image) -> None:
    save_kwargs: dict[str, Any] = {}
    dpi = source_image.info.get("dpi")
    if dpi:
        save_kwargs["dpi"] = dpi

    suffix = target.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".jpe", ".jfif"}:
        if image.mode != "RGB":
            image = image.convert("RGB")
        save_kwargs.update({"quality": 95})
    image.save(target, **save_kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
