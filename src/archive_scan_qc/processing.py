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
import shutil
import time
from typing import Any

from PIL import Image, ImageChops, ImageOps, ImageStat, UnidentifiedImageError

from .concurrency import resolve_worker_count, worker_metadata


def _load_numpy() -> Any | None:
    try:
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        return None
    return np


@dataclass(frozen=True)
class ProcessingOptions:
    auto_crop: bool = False
    deskew: bool = False
    trim_dark_border: bool = False
    despeckle: bool = False
    despeckle_backend: str = "fallback"
    resume_processing: bool = False
    reuse_scan_measurements: bool = False
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
    duplicate_reused_files = sum(1 for item in records if item.get("duplicate_derivative_reused"))
    existing_derivative_reused_files = sum(1 for item in records if item.get("_existing_derivative_reused"))
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
    performance["scan_measurement_reuse"] = _aggregate_scan_measurement_reuse(records)
    for record in records:
        record.pop("operation_timings", None)
        record.pop("_existing_derivative_reused", None)
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
            "duplicate_reused_files": duplicate_reused_files,
            "existing_derivative_reused_files": existing_derivative_reused_files,
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
            "reuse_scan_measurements" if options.reuse_scan_measurements else "reuse_scan_measurements_disabled",
            "autocontrast_cutoff_0_5",
            "preserve_source_relative_path",
        ],
        "resume": {
            "enabled": options.resume_processing,
            "previous_manifest_found": (process_dir / "processing_manifest.json").exists(),
            "skipped_due_to_resume": resumed_files,
            "reprocessed_files": reprocessed_files,
            "duplicate_reused_files": duplicate_reused_files,
            "existing_derivative_reused_files": existing_derivative_reused_files,
            "scan_measurement_reuse": performance["scan_measurement_reuse"],
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
    if not options.resume_processing:
        return _process_records_reusing_duplicates(files, input_dir, image_root, options, workers)
    if workers == 1:
        return [_process_record(item, input_dir, image_root, options, previous_records.get(item["relative_path"])) for item in files]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(
            executor.map(
                lambda item: _process_record(item, input_dir, image_root, options, previous_records.get(item["relative_path"])),
                files,
            )
        )


def _process_records_reusing_duplicates(
    files: list[dict[str, Any]],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
    workers: int,
) -> list[dict[str, Any]]:
    first_by_sha: dict[str, int] = {}
    unique_items: list[dict[str, Any]] = []
    unique_positions: list[int] = []
    duplicate_sources: dict[int, int] = {}

    for position, item in enumerate(files):
        source_sha = item.get("sha256")
        if not item.get("openable") or not isinstance(source_sha, str) or not source_sha:
            unique_positions.append(position)
            unique_items.append(item)
            continue
        first_position = first_by_sha.get(source_sha)
        if first_position is None:
            first_by_sha[source_sha] = position
            unique_positions.append(position)
            unique_items.append(item)
        else:
            duplicate_sources[position] = first_position

    if workers == 1:
        unique_records = [_process_record(item, input_dir, image_root, options) for item in unique_items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            unique_records = list(executor.map(lambda item: _process_record(item, input_dir, image_root, options), unique_items))

    records: list[dict[str, Any] | None] = [None] * len(files)
    for position, record in zip(unique_positions, unique_records):
        records[position] = record
    for position, source_position in duplicate_sources.items():
        source_record = records[source_position]
        if source_record is None or source_record.get("status") != "processed":
            records[position] = _process_record(files[position], input_dir, image_root, options)
        else:
            records[position] = _reuse_duplicate_record(files[position], input_dir, image_root, options, source_record)
    return [record for record in records if record is not None]


def _reuse_duplicate_record(
    item: dict[str, Any],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
    source_record: dict[str, Any],
) -> dict[str, Any]:
    relative_path = item["relative_path"]
    source = input_dir / relative_path
    if not source.is_file() or _sha256(source) != item.get("sha256"):
        return _process_record(item, input_dir, image_root, options)

    target = image_root / relative_path
    source_output_relative = source_record.get("output_relative_path")
    if not isinstance(source_output_relative, str) or not source_output_relative:
        record = dict(source_record)
        record.update(
            {
                "source_relative_path": relative_path,
                "source_sha256": item.get("sha256"),
                "output_relative_path": None,
                "output_sha256": None,
                "status": "failed",
                "resumed": False,
                "reprocessed": False,
                "operation_timings": {},
                "error": "duplicate derivative source output is missing",
                "failure_reason": "duplicate derivative source output is missing",
            }
        )
        return record
    source_output = image_root.parent / source_output_relative
    try:
        target.resolve().relative_to(image_root.parent.resolve())
        if source_output.resolve() == target.resolve():
            raise ValueError("duplicate derivative target matches source derivative")
        existing_derivative_reused = False
        if target.exists() and target.is_file() and _sha256(target) == source_record.get("output_sha256"):
            existing_derivative_reused = True
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_output, target)
    except (OSError, ValueError) as exc:
        record = dict(source_record)
        record.update(
            {
                "source_relative_path": relative_path,
                "source_sha256": item.get("sha256"),
                "output_relative_path": None,
                "output_sha256": None,
                "status": "failed",
                "resumed": False,
                "reprocessed": False,
                "operation_timings": {},
                "error": str(exc),
                "failure_reason": str(exc),
            }
        )
        return record

    record = dict(source_record)
    record.update(
        {
            "source_relative_path": relative_path,
            "source_sha256": item.get("sha256"),
            "output_relative_path": target.relative_to(image_root.parent).as_posix(),
            "output_sha256": _sha256(target),
            "status": "processed",
            "resumed": False,
            "reprocessed": False,
            "duplicate_derivative_reused": True,
            "_existing_derivative_reused": existing_derivative_reused,
            "operation_timings": {},
            "operations": list(source_record.get("operations", [])) + ["reuse_duplicate_derivative"],
            "error": None,
            "failure_reason": None,
        }
    )
    return record


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
        "despeckle_backend_mode": None,
        "processing_audit": None,
        "processing_warnings": [],
        "operation_timings": {},
        "status": "skipped",
        "resumed": False,
        "reprocessed": False,
        "duplicate_derivative_reused": False,
        "_existing_derivative_reused": False,
        "scan_measurements_reused": False,
        "processing_options_fingerprint": _processing_options_fingerprint(options),
        "operations": [],
        "error": None,
        "failure_reason": None,
    }
    if previous_record and _previous_record_is_current(previous_record, item, input_dir, image_root, options):
        resumed = dict(previous_record)
        resumed["status"] = "resumed"
        resumed["resumed"] = True
        resumed["reprocessed"] = False
        resumed["duplicate_derivative_reused"] = bool(previous_record.get("duplicate_derivative_reused"))
        resumed["_existing_derivative_reused"] = True
        resumed["processing_options_fingerprint"] = _processing_options_fingerprint(options)
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
            processed, operations, process_info = _process_image(image, options, scan_record=item)
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
                "despeckle_backend_mode": process_info["despeckle_backend_mode"],
                "processing_audit": process_info["processing_audit"],
                "processing_warnings": process_info["processing_warnings"],
                "operation_timings": process_info["operation_timings"],
                "scan_measurements_reused": process_info["scan_measurements_reused"],
                "status": "processed",
                "processing_options_fingerprint": _processing_options_fingerprint(options),
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
                    "despeckle_backend_mode": process_info["despeckle_backend_mode"],
                    "processing_audit": process_info["processing_audit"],
                    "processing_warnings": process_info["processing_warnings"],
                    "operation_timings": process_info["operation_timings"],
                    "scan_measurements_reused": process_info["scan_measurements_reused"],
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


def _previous_record_is_current(
    record: dict[str, Any],
    item: dict[str, Any],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
) -> bool:
    if record.get("status") not in {"processed", "resumed"}:
        return False
    if record.get("source_sha256") != item.get("sha256"):
        return False
    if record.get("processing_options_fingerprint") != _processing_options_fingerprint(options):
        return False
    output_relative_path = record.get("output_relative_path")
    output_sha256 = record.get("output_sha256")
    if not isinstance(output_relative_path, str) or not isinstance(output_sha256, str) or not output_sha256:
        return False
    output_path = image_root.parent / output_relative_path
    try:
        output_path.resolve().relative_to(image_root.parent.resolve())
    except ValueError:
        return False
    if not output_path.exists() or not output_path.is_file() or _sha256(output_path) != output_sha256:
        return False
    source_relative_path = item.get("relative_path")
    if not isinstance(source_relative_path, str) or not source_relative_path:
        return False
    source_path = input_dir / source_relative_path
    return source_path.exists() and source_path.is_file() and _sha256(source_path) == item.get("sha256")


def _processing_options_fingerprint(options: ProcessingOptions) -> str:
    identity = {
        "auto_crop": options.auto_crop,
        "deskew": options.deskew,
        "trim_dark_border": options.trim_dark_border,
        "despeckle": options.despeckle,
        "despeckle_backend": options.despeckle_backend,
        "reuse_scan_measurements": options.reuse_scan_measurements,
        "deskew_max_degrees": options.deskew_max_degrees,
        "deskew_min_confidence": options.deskew_min_confidence,
        "audit_max_size_change_ratio": options.audit_max_size_change_ratio,
        "audit_max_pixel_change_ratio": options.audit_max_pixel_change_ratio,
        "audit_max_brightness_delta": options.audit_max_brightness_delta,
        "audit_max_contrast_delta": options.audit_max_contrast_delta,
        "audit_max_crop_ratio": options.audit_max_crop_ratio,
        "audit_max_trim_margin_ratio": options.audit_max_trim_margin_ratio,
        "audit_max_despeckle_pixel_ratio": options.audit_max_despeckle_pixel_ratio,
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
            "reuse_scan_measurements": options.reuse_scan_measurements,
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
            "scan_measurement_reuse": performance.get("scan_measurement_reuse", _empty_scan_measurement_reuse()),
        },
        "counts": {
            "total_files": summary["total_files"],
            "processed_files": summary["processed_files"],
            "resumed_files": summary["resumed_files"],
            "skipped_due_to_resume": summary["skipped_due_to_resume"],
            "reprocessed_files": summary["reprocessed_files"],
            "duplicate_reused_files": summary["duplicate_reused_files"],
            "existing_derivative_reused_files": summary["existing_derivative_reused_files"],
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
            "reused_scan_measurement_files": _operation_reuse_count(records, operation),
        }
        if operation == "despeckle":
            timings[operation].update(_aggregate_despeckle_backend(records, is_enabled))
    return timings


def _operation_reuse_count(records: list[dict[str, Any]], operation: str) -> int:
    return sum(
        1
        for record in records
        if isinstance(record.get("operation_timings"), dict)
        and isinstance(record["operation_timings"].get(operation), dict)
        and record["operation_timings"][operation].get("reused_scan_measurement") is True
    )


def _aggregate_scan_measurement_reuse(records: list[dict[str, Any]]) -> dict[str, Any]:
    operations = ("deskew", "trim_dark_border")
    reused = {operation: _operation_reuse_count(records, operation) for operation in operations}
    fallback = {
        operation: sum(
            1
            for record in records
            if isinstance(record.get("operation_timings"), dict)
            and isinstance(record["operation_timings"].get(operation), dict)
            and record["operation_timings"][operation].get("fallback_reason")
        )
        for operation in operations
    }
    return {
        "enabled": any(record.get("scan_measurements_reused") for record in records if isinstance(record, dict)),
        "files_with_any_reuse": sum(1 for record in records if record.get("scan_measurements_reused")),
        "operations_skipped": {operation: count for operation, count in reused.items() if count},
        "fallback_operations": {operation: count for operation, count in fallback.items() if count},
    }


def _empty_scan_measurement_reuse() -> dict[str, Any]:
    return {
        "enabled": False,
        "files_with_any_reuse": 0,
        "operations_skipped": {},
        "fallback_operations": {},
    }


def _aggregate_despeckle_backend(records: list[dict[str, Any]], enabled: bool) -> dict[str, Any]:
    backend_counts = {"numpy": 0, "fallback": 0, "not_applicable": 0, "unknown": 0}
    for record in records:
        timing = record.get("operation_timings")
        timing_mode = timing.get("despeckle", {}).get("backend_mode") if isinstance(timing, dict) else None
        mode = timing_mode if isinstance(timing_mode, str) else record.get("despeckle_backend_mode")
        if mode in backend_counts:
            backend_counts[mode] += 1
        elif record.get("status") in {"processed", "failed"} and enabled:
            backend_counts["unknown"] += 1

    active_modes = [mode for mode in ("numpy", "fallback", "not_applicable", "unknown") if backend_counts[mode]]
    if not enabled:
        backend_mode = "disabled"
    elif len(active_modes) == 1:
        backend_mode = active_modes[0]
    elif active_modes:
        backend_mode = "mixed"
    else:
        backend_mode = "unknown"
    return {
        "backend_mode": backend_mode,
        "numpy_available": backend_counts["numpy"] > 0,
        "backend_counts": backend_counts,
    }


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


def _process_image(
    image: Image.Image,
    options: ProcessingOptions,
    *,
    scan_record: dict[str, Any] | None = None,
) -> tuple[Image.Image, list[str], dict[str, Any]]:
    operations: list[str] = []
    operation_timings: dict[str, dict[str, Any]] = {}
    processed = ImageOps.exif_transpose(image)
    operations.append("exif_transpose")
    original_size = list(processed.size)

    if processed.mode not in {"L", "RGB"}:
        processed = processed.convert("RGB")
        operations.append("convert_to_rgb")
    audit_source = processed.copy()
    reusable = _scan_measurements_for_processing(scan_record, processed) if options.reuse_scan_measurements else {}

    pre_deskew_size = list(processed.size)
    post_deskew_size = list(processed.size)
    with _operation_timer(operation_timings, "deskew", enabled=options.deskew):
        skew = reusable.get("skew")
        if isinstance(skew, SkewDetection):
            operations.append("skew_detect_reused_scan_measurement")
            operation_timings.setdefault("deskew", {})["reused_scan_measurement"] = True
        else:
            skew = _detect_skew(processed)
            operations.append("skew_detect_projection")
            if options.reuse_scan_measurements and options.deskew:
                operation_timings.setdefault("deskew", {})["fallback_reason"] = reusable.get(
                    "fallback_reason", "scan measurements unavailable"
                )
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
            reused_dark_border = reusable.get("dark_border")
            if isinstance(reused_dark_border, DarkBorderDetection) and not deskewed:
                dark_border = reused_dark_border
                operations.append("dark_border_detect_reused_scan_measurement")
                operation_timings.setdefault("trim_dark_border", {})["reused_scan_measurement"] = True
            else:
                dark_border = _detect_dark_border_bbox(processed)
                if options.reuse_scan_measurements:
                    fallback_reason = (
                        "deskew changed coordinate space"
                        if isinstance(reused_dark_border, DarkBorderDetection) and deskewed
                        else reusable.get("fallback_reason", "scan measurements unavailable")
                    )
                    operation_timings.setdefault("trim_dark_border", {})["fallback_reason"] = fallback_reason
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
    despeckle_backend_mode = "disabled"
    with _operation_timer(operation_timings, "despeckle", enabled=options.despeckle):
        if options.despeckle:
            processed, despeckle_pixels_changed, despeckle_backend_mode = _despeckle_isolated_pixels(
                processed,
                backend=options.despeckle_backend,
            )
            if despeckle_pixels_changed:
                operations.append("despeckle_isolated_pixels")
                despeckled = True
                despeckle_reason = "isolated dark pixels replaced"
            else:
                operations.append("despeckle_noop")
                despeckle_reason = "no isolated dark pixels found"
        else:
            operations.append("despeckle_disabled")
    if options.despeckle and "despeckle" in operation_timings:
        operation_timings["despeckle"]["backend_mode"] = despeckle_backend_mode
        operation_timings["despeckle"]["numpy_available"] = despeckle_backend_mode == "numpy"

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
        "despeckle_backend_mode": despeckle_backend_mode,
        "processing_audit": processing_audit,
        "processing_warnings": processing_warnings,
        "operation_timings": operation_timings,
        "scan_measurements_reused": any(
            timing.get("reused_scan_measurement") is True for timing in operation_timings.values()
        ),
    }
    return processed, operations, crop_info


class _operation_timer:
    def __init__(self, timings: dict[str, dict[str, Any]], operation: str, *, enabled: bool) -> None:
        self.timings = timings
        self.operation = operation
        self.enabled = enabled
        self.started_at = 0.0

    def __enter__(self) -> None:
        self.started_at = time.perf_counter()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.enabled:
            timing = self.timings.setdefault(self.operation, {})
            timing["elapsed_seconds"] = max(0.0, round(time.perf_counter() - self.started_at, 6))


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


def _scan_measurements_for_processing(scan_record: dict[str, Any] | None, image: Image.Image) -> dict[str, Any]:
    if not isinstance(scan_record, dict):
        return {"fallback_reason": "scan record unavailable"}
    if scan_record.get("openable") is not True:
        return {"fallback_reason": "scan record is not openable"}
    if scan_record.get("exif_orientation_requires_transpose") is True:
        return {"fallback_reason": "scan measurements predate EXIF transpose"}
    if scan_record.get("width") != image.width or scan_record.get("height") != image.height:
        return {"fallback_reason": "scan measurement dimensions do not match processing image"}

    skew = _scan_record_skew(scan_record)
    dark_border = _scan_record_dark_border(scan_record, image.size)
    if not skew and not dark_border:
        return {"fallback_reason": "complete reusable scan measurements unavailable"}

    reusable: dict[str, Any] = {}
    if skew:
        reusable["skew"] = skew
    if dark_border:
        reusable["dark_border"] = dark_border
    return reusable


def _scan_record_skew(scan_record: dict[str, Any]) -> SkewDetection | None:
    angle = scan_record.get("quality_skew_angle_degrees")
    confidence = scan_record.get("quality_skew_confidence")
    reason = scan_record.get("quality_skew_reason")
    if angle is not None and not isinstance(angle, int | float):
        return None
    if not isinstance(confidence, int | float) or not isinstance(reason, str):
        return None
    return SkewDetection(float(angle) if angle is not None else None, float(confidence), reason)


def _scan_record_dark_border(scan_record: dict[str, Any], size: tuple[int, int]) -> DarkBorderDetection | None:
    bbox_value = scan_record.get("quality_dark_border_bbox")
    reason = scan_record.get("quality_dark_border_reason")
    if not isinstance(reason, str):
        return None
    if bbox_value is None:
        return DarkBorderDetection(None, reason)
    if (
        not isinstance(bbox_value, list | tuple)
        or len(bbox_value) != 4
        or not all(isinstance(value, int) for value in bbox_value)
    ):
        return None
    left, top, right, bottom = bbox_value
    width, height = size
    if left < 0 or top < 0 or right > width or bottom > height or left >= right or top >= bottom:
        return None
    return DarkBorderDetection((left, top, right, bottom), reason)


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
    scores = _deskew_candidate_scores(sample)
    best_angle, best_score = max(scores.items(), key=lambda item: item[1])
    runner_up = max(score for angle, score in scores.items() if abs(angle - best_angle) >= 1.0)
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


def _deskew_candidate_scores(sample: Image.Image) -> dict[float, float]:
    background = 0
    scores: dict[float, float] = {}

    def score(correction_angle: float) -> float:
        normalized_angle = round(correction_angle, 2)
        if normalized_angle not in scores:
            rotated = sample.rotate(
                normalized_angle,
                resample=Image.Resampling.BILINEAR,
                expand=True,
                fillcolor=background,
            )
            scores[normalized_angle] = _horizontal_projection_variance(rotated)
        return scores[normalized_angle]

    zero_score = score(0.0)
    near_scores = {angle: score(angle) for angle in (-1.0, -0.5, 0.5, 1.0)}
    best_near_score = max(near_scores.values())
    zero_confidence = 0.0 if zero_score <= 0 else max(0.0, min(1.0, (zero_score - best_near_score) / zero_score))
    if zero_score >= best_near_score and zero_confidence >= 0.08:
        return scores

    coarse_angles = _frange(-7.0, 7.0, 1.0)
    for correction_angle in coarse_angles:
        score(correction_angle)

    best_coarse_angle = max(coarse_angles, key=lambda angle: scores[round(angle, 2)])
    refine_start = max(-7.0, best_coarse_angle - 1.0)
    refine_stop = min(7.0, best_coarse_angle + 1.0)
    for correction_angle in _frange(refine_start, refine_stop, 0.25):
        score(correction_angle)
    return scores


def _horizontal_projection_variance(image: Image.Image) -> float:
    width, height = image.size
    projection = image.resize((1, height), Image.Resampling.BOX)
    row_counts = [value * width / 255 for value in projection.tobytes()]
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


def _despeckle_isolated_pixels(image: Image.Image, *, backend: str = "fallback") -> tuple[Image.Image, int, str]:
    grayscale = image.convert("L")
    width, height = grayscale.size
    if width < 3 or height < 3:
        return image.copy(), 0, "not_applicable"

    dark_mask = grayscale.point(lambda value: 255 if value <= 60 else 0, mode="L")
    candidates, backend_mode = _despeckle_candidate_points_with_backend(dark_mask, backend=backend)
    if not candidates:
        return image.copy(), 0, backend_mode

    gray_pixels = grayscale.load()
    source: Image.Image | None = None
    output: Image.Image | None = None
    source_pixels: Any = None
    output_pixels: Any = None
    changed = 0
    for x, y in candidates:
        dark_neighbors = 0
        neighbor_values: list[int] = []
        for ny in range(y - 1, y + 2):
            for nx in range(x - 1, x + 2):
                if nx == x and ny == y:
                    continue
                value = gray_pixels[nx, ny]
                neighbor_values.append(value)
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

        if output is None:
            source = image if image.mode == "RGB" else image.convert("RGB")
            output = source.copy()
            source_pixels = source.load()
            output_pixels = output.load()
        neighbor_rgb = [
            source_pixels[nx, ny]
            for ny in range(y - 1, y + 2)
            for nx in range(x - 1, x + 2)
            if nx != x or ny != y
        ]
        replacement = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*neighbor_rgb))
        output_pixels[x, y] = replacement
        changed += 1

    if output is None:
        return image.copy(), 0, backend_mode
    if image.mode == "L":
        return output.convert("L"), changed, backend_mode
    if image.mode == "RGB":
        return output, changed, backend_mode
    return output.convert(image.mode), changed, backend_mode


def _despeckle_candidate_points(dark_mask: Image.Image, *, backend: str = "fallback") -> list[tuple[int, int]]:
    candidates, _backend_mode = _despeckle_candidate_points_with_backend(dark_mask, backend=backend)
    return candidates


def _despeckle_candidate_points_with_backend(dark_mask: Image.Image, *, backend: str = "fallback") -> tuple[list[tuple[int, int]], str]:
    if backend not in {"fallback", "numpy"}:
        raise ValueError("despeckle backend must be fallback or numpy")
    if backend == "numpy":
        numpy_candidates = _despeckle_candidate_points_numpy(dark_mask)
        if numpy_candidates is not None:
            return numpy_candidates, "numpy"
    return _despeckle_candidate_points_fallback(dark_mask), "fallback"


def _despeckle_candidate_points_numpy(dark_mask: Image.Image) -> list[tuple[int, int]] | None:
    np = _load_numpy()
    if np is None:
        return None

    width, height = dark_mask.size
    if width < 3 or height < 3:
        return []

    candidate_bbox = dark_mask.getbbox()
    if not candidate_bbox:
        return []

    left, top, right, bottom = candidate_bbox
    if left >= width - 1 or top >= height - 1 or right <= 1 or bottom <= 1:
        return []

    try:
        crop = np.asarray(dark_mask.crop(candidate_bbox), dtype=np.uint8) > 0
        padded = np.pad(crop.astype(np.uint8), 1, mode="constant", constant_values=0)
    except (TypeError, ValueError):
        return None

    neighbor_counts = (
        padded[:-2, :-2]
        + padded[:-2, 1:-1]
        + padded[:-2, 2:]
        + padded[1:-1, :-2]
        + padded[1:-1, 2:]
        + padded[2:, :-2]
        + padded[2:, 1:-1]
        + padded[2:, 2:]
    )
    candidate_mask = crop & (neighbor_counts <= 1)

    crop_height, crop_width = crop.shape
    if left == 0:
        candidate_mask[:, 0] = False
    if top == 0:
        candidate_mask[0, :] = False
    if right == width:
        candidate_mask[:, crop_width - 1] = False
    if bottom == height:
        candidate_mask[crop_height - 1, :] = False

    local_y_values, local_x_values = np.nonzero(candidate_mask)
    return [
        (left + int(local_x), top + int(local_y))
        for local_y, local_x in zip(local_y_values, local_x_values)
    ]


def _despeckle_candidate_points_fallback(dark_mask: Image.Image) -> list[tuple[int, int]]:
    width, height = dark_mask.size
    if width < 3 or height < 3:
        return []

    candidate_bbox = dark_mask.getbbox()
    if not candidate_bbox:
        return []

    left, top, right, bottom = candidate_bbox
    if left >= width - 1 or top >= height - 1 or right <= 1 or bottom <= 1:
        return []

    crop_width = right - left
    crop_values = dark_mask.crop(candidate_bbox).tobytes()
    candidates: list[tuple[int, int]] = []
    for index, mask_value in enumerate(crop_values):
        if not mask_value:
            continue
        local_x = index % crop_width
        local_y = index // crop_width
        x = left + local_x
        y = top + local_y
        if x == 0 or y == 0 or x == width - 1 or y == height - 1:
            continue

        dark_neighbors = 0
        for neighbor_y in range(max(0, local_y - 1), min(bottom - top, local_y + 2)):
            row_offset = neighbor_y * crop_width
            for neighbor_x in range(max(0, local_x - 1), min(crop_width, local_x + 2)):
                if neighbor_x == local_x and neighbor_y == local_y:
                    continue
                if crop_values[row_offset + neighbor_x]:
                    dark_neighbors += 1
                    if dark_neighbors > 1:
                        break
            if dark_neighbors > 1:
                break
        if dark_neighbors <= 1:
            candidates.append((x, y))
    return candidates


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
