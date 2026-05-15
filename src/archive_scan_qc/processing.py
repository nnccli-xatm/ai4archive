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

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

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
    normalize_tones: bool = False
    lighten_edge_shadow: bool = False
    lighten_background_stains: bool = False
    lighten_scanlines: bool = False
    enhance_faded_text: bool = False
    sharpen_text_edges: bool = False
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
    audit_max_cumulative_change_score: float = 1.0
    audit_max_cumulative_pixel_change_ratio: float = 0.35
    audit_max_cumulative_brightness_delta: float = 50.0
    audit_max_cumulative_contrast_delta: float = 50.0
    audit_max_cumulative_crop_ratio: float = 0.55
    audit_max_cumulative_candidate_pixel_ratio: float = 1.0
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


@dataclass(frozen=True)
class CropDetection:
    bbox: tuple[int, int, int, int] | None
    reason: str


@dataclass(frozen=True)
class ToneNormalizationResult:
    image: Image.Image
    applied: bool
    reason: str
    background_before: float | None
    background_after: float | None
    contrast_before: float | None
    contrast_after: float | None
    changed_pixel_ratio: float = 0.0


@dataclass(frozen=True)
class EdgeShadowLighteningResult:
    image: Image.Image
    applied: bool
    reason: str
    edges: tuple[str, ...]
    edge_mean_before: float | None
    edge_mean_after: float | None
    edge_delta: float
    changed_pixel_ratio: float
    candidate_pixel_ratio: float


@dataclass(frozen=True)
class BackgroundStainLighteningResult:
    image: Image.Image
    applied: bool
    reason: str
    stain_mean_before: float | None
    stain_mean_after: float | None
    stain_delta: float
    changed_pixel_ratio: float
    candidate_pixel_ratio: float


@dataclass(frozen=True)
class ScanlineLighteningResult:
    image: Image.Image
    applied: bool
    reason: str
    orientation: str | None
    line_count: int
    line_mean_before: float | None
    line_mean_after: float | None
    line_delta: float
    changed_pixel_ratio: float
    candidate_pixel_ratio: float


@dataclass(frozen=True)
class FadedTextEnhancementResult:
    image: Image.Image
    applied: bool
    reason: str
    text_delta: float
    changed_pixel_ratio: float
    candidate_pixel_ratio: float


@dataclass(frozen=True)
class TextEdgeSharpeningResult:
    image: Image.Image
    applied: bool
    reason: str
    edge_delta: float
    changed_pixel_ratio: float
    candidate_pixel_ratio: float
    preflight_skipped: bool = False


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
            "normalize_tones_conservative" if options.normalize_tones else "normalize_tones_disabled",
            "lighten_edge_shadow_conservative" if options.lighten_edge_shadow else "lighten_edge_shadow_disabled",
            (
                "lighten_background_stains_conservative"
                if options.lighten_background_stains
                else "lighten_background_stains_disabled"
            ),
            "lighten_scanlines_conservative" if options.lighten_scanlines else "lighten_scanlines_disabled",
            "enhance_faded_text_conservative" if options.enhance_faded_text else "enhance_faded_text_disabled",
            "sharpen_text_edges_conservative" if options.sharpen_text_edges else "sharpen_text_edges_disabled",
            "reuse_scan_measurements" if options.reuse_scan_measurements else "reuse_scan_measurements_disabled",
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


def aggregate_processing_reuse_precheck(
    input_dir: Path,
    process_dir: Path,
    relative_paths: list[str],
    options: ProcessingOptions | None = None,
) -> dict[str, Any]:
    """Summarize existing derivative reuse before starting a processing run."""
    options = options or ProcessingOptions()
    input_dir = input_dir.resolve()
    process_dir = process_dir.resolve()
    image_root = process_dir / "images"
    safe_relative_paths = [path for path in relative_paths if isinstance(path, str) and path.strip()]
    total_files = len(safe_relative_paths)
    base = {
        "schema_version": "scan-qc.local-processing-precheck.v1",
        "aggregate_only": True,
        "total_files": total_files,
    }
    manifest_path = process_dir / "processing_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _processing_precheck_unknown(base, "missing_state_file", total_files)
    except (OSError, json.JSONDecodeError):
        return _processing_precheck_unknown(base, "unreadable_state_file", total_files)
    if payload.get("schema_version") != "scan-qc.processing.v1" or not isinstance(payload.get("files"), list):
        return _processing_precheck_unknown(base, "unsupported_state_file", total_files)

    previous_records: dict[str, dict[str, Any]] = {}
    for record in payload["files"]:
        if not isinstance(record, dict):
            return _processing_precheck_unknown(base, "incomplete_state_file", total_files)
        source_relative_path = record.get("source_relative_path")
        if not isinstance(source_relative_path, str) or not source_relative_path:
            return _processing_precheck_unknown(base, "incomplete_state_file", total_files)
        previous_records[source_relative_path] = record

    reusable_files = 0
    needs_processing_files = 0
    precheck_workers = resolve_worker_count(options.workers, total_files)

    def classify(relative_path: str) -> tuple[str, str | None]:
        source_path = input_dir / relative_path
        try:
            source_sha256 = _sha256(source_path)
        except OSError:
            return "unknown", "input_file_unreadable"
        item = {
            "relative_path": relative_path,
            "sha256": source_sha256,
            "openable": True,
        }
        previous_record = previous_records.get(relative_path)
        if not previous_record:
            return "needs_processing", None
        try:
            reusable = _previous_record_is_current(previous_record, item, input_dir, image_root, options)
        except OSError:
            reusable = False
        return ("reusable", None) if reusable else ("needs_processing", None)

    if precheck_workers == 1:
        decisions = [classify(relative_path) for relative_path in safe_relative_paths]
    else:
        with ThreadPoolExecutor(max_workers=precheck_workers) as executor:
            decisions = list(executor.map(classify, safe_relative_paths))

    for decision, reason in decisions:
        if decision == "unknown":
            return _processing_precheck_unknown(base, reason or "input_file_unreadable", total_files)
        if decision == "reusable":
            reusable_files += 1
        else:
            needs_processing_files += 1

    message_zh = (
        f"本批预检结果：共识别 {total_files} 张图片，"
        f"已有 {reusable_files} 张可复用处理后输出，"
        f"{needs_processing_files} 张需要新处理或补处理。"
    )
    next_steps_zh = [
        "确认当前选择的是同一批扫描原图和输出文件夹。",
        "开始处理后，系统会复用可安全确认的已有输出，并补齐需要处理的图片。",
        "预检只用于开始前判断，不代表本批已经完成交接。",
    ]
    return {
        **base,
        "reusable_files": reusable_files,
        "needs_processing_files": needs_processing_files,
        "unknown_scope_files": 0,
        "retry_scope_safe": True,
        "state": "ready",
        "message_zh": message_zh,
        "next_steps_zh": next_steps_zh,
    }


def _processing_precheck_unknown(base: dict[str, Any], reason: str, total_files: int) -> dict[str, Any]:
    return {
        **base,
        "reusable_files": None,
        "needs_processing_files": None,
        "unknown_scope_files": total_files,
        "retry_scope_safe": False,
        "state": "unknown",
        "unknown_reason": reason,
        "message_zh": "本批已有状态文件缺失或不完整，开始前不能安全判断哪些图片可复用；当前不会误报完成，也不会编造复用数量。",
        "next_steps_zh": [
            "确认扫描原图和输出文件夹选对。",
            "可以开始处理，系统会按保守方式重新核对已有输出。",
            "如果不确定是否同一批，请更换空输出文件夹或交管理员确认。",
        ],
    }


def _process_records(
    files: list[dict[str, Any]],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
    workers: int,
    previous_records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return _process_records_reusing_duplicates(files, input_dir, image_root, options, workers, previous_records)


def _process_records_reusing_duplicates(
    files: list[dict[str, Any]],
    input_dir: Path,
    image_root: Path,
    options: ProcessingOptions,
    workers: int,
    previous_records: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    previous_records = previous_records or {}
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

    def process_unique(item: dict[str, Any]) -> dict[str, Any]:
        return _process_record(item, input_dir, image_root, options, previous_records.get(item["relative_path"]))

    if workers == 1:
        unique_records = [process_unique(item) for item in unique_items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            unique_records = list(executor.map(process_unique, unique_items))

    records: list[dict[str, Any] | None] = [None] * len(files)
    for position, record in zip(unique_positions, unique_records):
        records[position] = record
    for position, source_position in duplicate_sources.items():
        item = files[position]
        previous_record = previous_records.get(item["relative_path"])
        if previous_record and _previous_record_is_current(previous_record, item, input_dir, image_root, options):
            records[position] = _resume_record(previous_record, options)
            continue
        source_record = records[source_position]
        if source_record is None or source_record.get("status") not in {"processed", "resumed"}:
            records[position] = _process_record(item, input_dir, image_root, options, previous_record)
        else:
            duplicate_record = _reuse_duplicate_record(item, input_dir, image_root, options, source_record)
            if options.resume_processing and previous_record is not None and duplicate_record.get("status") == "processed":
                duplicate_record["reprocessed"] = True
            records[position] = duplicate_record
    return [record for record in records if record is not None]


def _resume_record(previous_record: dict[str, Any], options: ProcessingOptions) -> dict[str, Any]:
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


def _int_count(value: Any) -> int:
    return int(value) if isinstance(value, int) else 0


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
        "crop_reason": None,
        "cropped": False,
        "despeckled": False,
        "despeckle_pixels_changed": 0,
        "despeckle_reason": None,
        "despeckle_backend_mode": None,
        "tone_normalized": False,
        "tone_reason": None,
        "tone_background_before": None,
        "tone_background_after": None,
        "tone_contrast_before": None,
        "tone_contrast_after": None,
        "tone_changed_pixel_ratio": 0.0,
        "edge_shadow_lightened": False,
        "edge_shadow_reason": None,
        "edge_shadow_edges": [],
        "edge_shadow_mean_before": None,
        "edge_shadow_mean_after": None,
        "edge_shadow_delta": 0.0,
        "edge_shadow_changed_pixel_ratio": 0.0,
        "edge_shadow_candidate_pixel_ratio": 0.0,
        "background_stains_lightened": False,
        "background_stains_reason": None,
        "background_stains_mean_before": None,
        "background_stains_mean_after": None,
        "background_stains_delta": 0.0,
        "background_stains_changed_pixel_ratio": 0.0,
        "background_stains_candidate_pixel_ratio": 0.0,
        "scanlines_lightened": False,
        "scanlines_reason": None,
        "scanlines_orientation": None,
        "scanlines_count": 0,
        "scanlines_mean_before": None,
        "scanlines_mean_after": None,
        "scanlines_delta": 0.0,
        "scanlines_changed_pixel_ratio": 0.0,
        "scanlines_candidate_pixel_ratio": 0.0,
        "faded_text_enhanced": False,
        "faded_text_reason": None,
        "faded_text_delta": 0.0,
        "faded_text_changed_pixel_ratio": 0.0,
        "faded_text_candidate_pixel_ratio": 0.0,
        "text_edges_sharpened": False,
        "text_edges_reason": None,
        "text_edges_delta": 0.0,
        "text_edges_changed_pixel_ratio": 0.0,
        "text_edges_candidate_pixel_ratio": 0.0,
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
        return _resume_record(previous_record, options)

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
                "crop_reason": process_info["crop_reason"],
                "cropped": process_info["cropped"],
                "despeckled": process_info["despeckled"],
                "despeckle_pixels_changed": process_info["despeckle_pixels_changed"],
                "despeckle_reason": process_info["despeckle_reason"],
                "despeckle_backend_mode": process_info["despeckle_backend_mode"],
                "tone_normalized": process_info["tone_normalized"],
                "tone_reason": process_info["tone_reason"],
                "tone_background_before": process_info["tone_background_before"],
                "tone_background_after": process_info["tone_background_after"],
                "tone_contrast_before": process_info["tone_contrast_before"],
                "tone_contrast_after": process_info["tone_contrast_after"],
                "tone_changed_pixel_ratio": process_info["tone_changed_pixel_ratio"],
                "edge_shadow_lightened": process_info["edge_shadow_lightened"],
                "edge_shadow_reason": process_info["edge_shadow_reason"],
                "edge_shadow_edges": process_info["edge_shadow_edges"],
                "edge_shadow_mean_before": process_info["edge_shadow_mean_before"],
                "edge_shadow_mean_after": process_info["edge_shadow_mean_after"],
                "edge_shadow_delta": process_info["edge_shadow_delta"],
                "edge_shadow_changed_pixel_ratio": process_info["edge_shadow_changed_pixel_ratio"],
                "edge_shadow_candidate_pixel_ratio": process_info["edge_shadow_candidate_pixel_ratio"],
                "background_stains_lightened": process_info["background_stains_lightened"],
                "background_stains_reason": process_info["background_stains_reason"],
                "background_stains_mean_before": process_info["background_stains_mean_before"],
                "background_stains_mean_after": process_info["background_stains_mean_after"],
                "background_stains_delta": process_info["background_stains_delta"],
                "background_stains_changed_pixel_ratio": process_info["background_stains_changed_pixel_ratio"],
                "background_stains_candidate_pixel_ratio": process_info["background_stains_candidate_pixel_ratio"],
                "scanlines_lightened": process_info["scanlines_lightened"],
                "scanlines_reason": process_info["scanlines_reason"],
                "scanlines_orientation": process_info["scanlines_orientation"],
                "scanlines_count": process_info["scanlines_count"],
                "scanlines_mean_before": process_info["scanlines_mean_before"],
                "scanlines_mean_after": process_info["scanlines_mean_after"],
                "scanlines_delta": process_info["scanlines_delta"],
                "scanlines_changed_pixel_ratio": process_info["scanlines_changed_pixel_ratio"],
                "scanlines_candidate_pixel_ratio": process_info["scanlines_candidate_pixel_ratio"],
                "faded_text_enhanced": process_info["faded_text_enhanced"],
                "faded_text_reason": process_info["faded_text_reason"],
                "faded_text_delta": process_info["faded_text_delta"],
                "faded_text_changed_pixel_ratio": process_info["faded_text_changed_pixel_ratio"],
                "faded_text_candidate_pixel_ratio": process_info["faded_text_candidate_pixel_ratio"],
                "text_edges_sharpened": process_info["text_edges_sharpened"],
                "text_edges_reason": process_info["text_edges_reason"],
                "text_edges_delta": process_info["text_edges_delta"],
                "text_edges_changed_pixel_ratio": process_info["text_edges_changed_pixel_ratio"],
                "text_edges_candidate_pixel_ratio": process_info["text_edges_candidate_pixel_ratio"],
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
                    "crop_reason": process_info["crop_reason"],
                    "cropped": process_info["cropped"],
                    "despeckled": process_info["despeckled"],
                    "despeckle_pixels_changed": process_info["despeckle_pixels_changed"],
                    "despeckle_reason": process_info["despeckle_reason"],
                    "despeckle_backend_mode": process_info["despeckle_backend_mode"],
                    "tone_normalized": process_info["tone_normalized"],
                    "tone_reason": process_info["tone_reason"],
                    "tone_background_before": process_info["tone_background_before"],
                    "tone_background_after": process_info["tone_background_after"],
                    "tone_contrast_before": process_info["tone_contrast_before"],
                    "tone_contrast_after": process_info["tone_contrast_after"],
                    "tone_changed_pixel_ratio": process_info["tone_changed_pixel_ratio"],
                    "edge_shadow_lightened": process_info["edge_shadow_lightened"],
                    "edge_shadow_reason": process_info["edge_shadow_reason"],
                    "edge_shadow_edges": process_info["edge_shadow_edges"],
                    "edge_shadow_mean_before": process_info["edge_shadow_mean_before"],
                    "edge_shadow_mean_after": process_info["edge_shadow_mean_after"],
                    "edge_shadow_delta": process_info["edge_shadow_delta"],
                    "edge_shadow_changed_pixel_ratio": process_info["edge_shadow_changed_pixel_ratio"],
                    "edge_shadow_candidate_pixel_ratio": process_info["edge_shadow_candidate_pixel_ratio"],
                    "background_stains_lightened": process_info["background_stains_lightened"],
                    "background_stains_reason": process_info["background_stains_reason"],
                    "background_stains_mean_before": process_info["background_stains_mean_before"],
                    "background_stains_mean_after": process_info["background_stains_mean_after"],
                    "background_stains_delta": process_info["background_stains_delta"],
                    "background_stains_changed_pixel_ratio": process_info["background_stains_changed_pixel_ratio"],
                    "background_stains_candidate_pixel_ratio": process_info["background_stains_candidate_pixel_ratio"],
                    "scanlines_lightened": process_info["scanlines_lightened"],
                    "scanlines_reason": process_info["scanlines_reason"],
                    "scanlines_orientation": process_info["scanlines_orientation"],
                    "scanlines_count": process_info["scanlines_count"],
                    "scanlines_mean_before": process_info["scanlines_mean_before"],
                    "scanlines_mean_after": process_info["scanlines_mean_after"],
                    "scanlines_delta": process_info["scanlines_delta"],
                    "scanlines_changed_pixel_ratio": process_info["scanlines_changed_pixel_ratio"],
                    "scanlines_candidate_pixel_ratio": process_info["scanlines_candidate_pixel_ratio"],
                    "faded_text_enhanced": process_info["faded_text_enhanced"],
                    "faded_text_reason": process_info["faded_text_reason"],
                    "faded_text_delta": process_info["faded_text_delta"],
                    "faded_text_changed_pixel_ratio": process_info["faded_text_changed_pixel_ratio"],
                    "faded_text_candidate_pixel_ratio": process_info["faded_text_candidate_pixel_ratio"],
                    "text_edges_sharpened": process_info["text_edges_sharpened"],
                    "text_edges_reason": process_info["text_edges_reason"],
                    "text_edges_delta": process_info["text_edges_delta"],
                    "text_edges_changed_pixel_ratio": process_info["text_edges_changed_pixel_ratio"],
                    "text_edges_candidate_pixel_ratio": process_info["text_edges_candidate_pixel_ratio"],
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
        "normalize_tones": options.normalize_tones,
        "lighten_edge_shadow": options.lighten_edge_shadow,
        "lighten_background_stains": options.lighten_background_stains,
        "lighten_scanlines": options.lighten_scanlines,
        "enhance_faded_text": options.enhance_faded_text,
        "sharpen_text_edges": options.sharpen_text_edges,
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
    deskew_timing = operation_timings.get("deskew") if isinstance(operation_timings, dict) else {}
    if not isinstance(deskew_timing, dict):
        deskew_timing = {}
    sharpen_text_edges_timing = operation_timings.get("sharpen_text_edges") if isinstance(operation_timings, dict) else {}
    if not isinstance(sharpen_text_edges_timing, dict):
        sharpen_text_edges_timing = {}
    processed_records = [record for record in manifest["files"] if record.get("status") in {"processed", "resumed"}]
    auto_crop_reasons = [
        record.get("crop_reason")
        for record in processed_records
        if isinstance(record.get("crop_reason"), str)
    ]
    auto_crop_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("crop_reason")]
        if record.get("cropped") is False
        and isinstance(reason, str)
        and reason != "auto crop disabled"
    ]
    dark_border_reasons = [
        record.get("dark_border_reason")
        for record in processed_records
        if isinstance(record.get("dark_border_reason"), str)
    ]
    deskew_reasons = [
        record.get("deskew_reason")
        for record in processed_records
        if isinstance(record.get("deskew_reason"), str)
    ]
    despeckle_reasons = [
        record.get("despeckle_reason")
        for record in processed_records
        if isinstance(record.get("despeckle_reason"), str)
    ]
    background_stains_reasons = [
        record.get("background_stains_reason")
        for record in processed_records
        if isinstance(record.get("background_stains_reason"), str)
    ]
    background_stains_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("background_stains_reason")]
        if record.get("background_stains_lightened") is False
        and isinstance(reason, str)
        and reason != "background stain lightening disabled"
    ]
    scanlines_reasons = [
        record.get("scanlines_reason")
        for record in processed_records
        if isinstance(record.get("scanlines_reason"), str)
    ]
    scanlines_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("scanlines_reason")]
        if record.get("scanlines_lightened") is False
        and isinstance(reason, str)
        and reason != "scanline lightening disabled"
    ]
    faded_text_reasons = [
        record.get("faded_text_reason")
        for record in processed_records
        if isinstance(record.get("faded_text_reason"), str)
    ]
    faded_text_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("faded_text_reason")]
        if record.get("faded_text_enhanced") is False
        and isinstance(reason, str)
        and reason != "faded text enhancement disabled"
    ]
    text_edges_reasons = [
        record.get("text_edges_reason")
        for record in processed_records
        if isinstance(record.get("text_edges_reason"), str)
    ]
    text_edges_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("text_edges_reason")]
        if record.get("text_edges_sharpened") is False
        and isinstance(reason, str)
        and reason != "text edge sharpening disabled"
    ]
    edge_shadow_reasons = [
        record.get("edge_shadow_reason")
        for record in processed_records
        if isinstance(record.get("edge_shadow_reason"), str)
    ]
    edge_shadow_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("edge_shadow_reason")]
        if record.get("edge_shadow_lightened") is False
        and isinstance(reason, str)
        and reason != "edge shadow lightening disabled"
    ]
    tone_reasons = [
        record.get("tone_reason")
        for record in processed_records
        if isinstance(record.get("tone_reason"), str)
    ]
    tone_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("tone_reason")]
        if record.get("tone_normalized") is False
        and isinstance(reason, str)
        and reason != "tone normalization disabled"
    ]
    return {
        "schema_version": "scan-qc.processing.audit.v1",
        "generated_at": manifest["generated_at"],
        "operations": {
            "auto_crop": options.auto_crop,
            "deskew": options.deskew,
            "trim_dark_border": options.trim_dark_border,
            "despeckle": options.despeckle,
            "normalize_tones": options.normalize_tones,
            "lighten_edge_shadow": options.lighten_edge_shadow,
            "lighten_background_stains": options.lighten_background_stains,
            "lighten_scanlines": options.lighten_scanlines,
            "enhance_faded_text": options.enhance_faded_text,
            "sharpen_text_edges": options.sharpen_text_edges,
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
            "pixel_change_guardrail_applied_files": sum(
                1 for audit in audit_records if audit.get("pixel_change_guardrail_applied") is True
            ),
            "pixel_change_guardrail_deferred_to_geometric_files": sum(
                1 for audit in audit_records if audit.get("pixel_change_guardrail_applied") is False
            ),
            "cumulative_change_guard_checked_files": sum(
                1 for audit in audit_records if audit.get("cumulative_change_guard_checked") is True
            ),
            "cumulative_change_guard_reverted_files": sum(
                1 for audit in audit_records if audit.get("cumulative_change_guard_reverted") is True
            ),
            "cumulative_change_guard_warning_files": sum(
                1
                for audit in audit_records
                if audit.get("cumulative_change_guard_action") in {"reverted_to_source", "warn_review"}
            ),
            "deskew_safe_skip_files": _int_count(deskew_timing.get("safe_skip_files")),
            "deskew_projection_detection_files": _int_count(deskew_timing.get("projection_detection_files")),
            "deskew_fallback_detection_files": _int_count(deskew_timing.get("fallback_detection_files")),
            "deskewed_files": sum(1 for record in processed_records if record.get("deskewed") is True),
            "deskew_skipped_files": sum(
                1
                for record in processed_records
                if record.get("deskewed") is False
                and record.get("deskew_reason") not in {None, "deskew disabled"}
            ),
            "dark_border_trimmed_files": sum(
                1 for record in processed_records if record.get("dark_border_trimmed") is True
            ),
            "auto_crop_applied_files": sum(1 for record in processed_records if record.get("cropped") is True),
            "auto_crop_skipped_files": sum(
                1
                for record in processed_records
                if record.get("cropped") is False
                and record.get("crop_reason") not in {None, "auto crop disabled"}
            ),
            "auto_crop_low_confidence_skip_files": _auto_crop_low_confidence_skip_count(
                auto_crop_skipped_reasons
            ),
            "dark_border_skipped_files": sum(
                1
                for record in processed_records
                if record.get("dark_border_trimmed") is False
                and record.get("dark_border_reason") not in {None, "dark border trim disabled"}
            ),
            "despeckled_files": sum(1 for record in processed_records if record.get("despeckled") is True),
            "despeckle_skipped_files": sum(
                1
                for record in processed_records
                if record.get("despeckled") is False
                and record.get("despeckle_reason") not in {None, "despeckle disabled"}
            ),
            "tone_normalized_files": sum(1 for audit in audit_records if audit.get("tone_normalized") is True),
            "tone_skipped_files": sum(
                1
                for record in processed_records
                if record.get("tone_normalized") is False
                and record.get("tone_reason") not in {None, "tone normalization disabled"}
            ),
            "edge_shadow_lightened_files": sum(
                1 for audit in audit_records if audit.get("edge_shadow_lightened") is True
            ),
            "edge_shadow_skipped_files": sum(
                1
                for record in processed_records
                if record.get("edge_shadow_lightened") is False
                and record.get("edge_shadow_reason") not in {None, "edge shadow lightening disabled"}
            ),
            "background_stains_lightened_files": sum(
                1 for audit in audit_records if audit.get("background_stains_lightened") is True
            ),
            "background_stains_skipped_files": sum(
                1
                for record in processed_records
                if record.get("background_stains_lightened") is False
                and record.get("background_stains_reason") not in {None, "background stain lightening disabled"}
            ),
            "scanlines_lightened_files": sum(1 for audit in audit_records if audit.get("scanlines_lightened") is True),
            "scanlines_skipped_files": sum(
                1
                for record in processed_records
                if record.get("scanlines_lightened") is False
                and record.get("scanlines_reason") not in {None, "scanline lightening disabled"}
            ),
            "faded_text_enhanced_files": sum(
                1 for audit in audit_records if audit.get("faded_text_enhanced") is True
            ),
            "faded_text_skipped_files": sum(
                1
                for record in processed_records
                if record.get("faded_text_enhanced") is False
                and record.get("faded_text_reason") not in {None, "faded text enhancement disabled"}
            ),
            "text_edges_sharpened_files": sum(
                1 for audit in audit_records if audit.get("text_edges_sharpened") is True
            ),
            "text_edges_skipped_files": sum(
                1
                for record in processed_records
                if record.get("text_edges_sharpened") is False
                and record.get("text_edges_reason") not in {None, "text edge sharpening disabled"}
            ),
            "text_edges_candidate_preflight_skipped_files": _int_count(
                sharpen_text_edges_timing.get("candidate_preflight_skipped_files")
            ),
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
            "tone_background_delta": _aggregate_metric(audit_records, "tone_background_delta"),
            "tone_contrast_delta": _aggregate_metric(audit_records, "tone_contrast_delta"),
            "tone_changed_pixel_ratio": _aggregate_metric(audit_records, "tone_changed_pixel_ratio"),
            "edge_shadow_delta": _aggregate_metric(audit_records, "edge_shadow_delta"),
            "edge_shadow_changed_pixel_ratio": _aggregate_metric(
                audit_records, "edge_shadow_changed_pixel_ratio"
            ),
            "edge_shadow_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "edge_shadow_candidate_pixel_ratio"
            ),
            "background_stains_delta": _aggregate_metric(audit_records, "background_stains_delta"),
            "background_stains_changed_pixel_ratio": _aggregate_metric(
                audit_records, "background_stains_changed_pixel_ratio"
            ),
            "background_stains_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "background_stains_candidate_pixel_ratio"
            ),
            "scanlines_delta": _aggregate_metric(audit_records, "scanlines_delta"),
            "scanlines_changed_pixel_ratio": _aggregate_metric(audit_records, "scanlines_changed_pixel_ratio"),
            "scanlines_candidate_pixel_ratio": _aggregate_metric(audit_records, "scanlines_candidate_pixel_ratio"),
            "faded_text_delta": _aggregate_metric(audit_records, "faded_text_delta"),
            "faded_text_changed_pixel_ratio": _aggregate_metric(
                audit_records, "faded_text_changed_pixel_ratio"
            ),
            "faded_text_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "faded_text_candidate_pixel_ratio"
            ),
            "text_edges_delta": _aggregate_metric(audit_records, "text_edges_delta"),
            "text_edges_changed_pixel_ratio": _aggregate_metric(
                audit_records, "text_edges_changed_pixel_ratio"
            ),
            "text_edges_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "text_edges_candidate_pixel_ratio"
            ),
            "cumulative_change_score": _aggregate_metric(audit_records, "cumulative_change_score"),
            "cumulative_change_pixel_ratio": _aggregate_metric(audit_records, "cumulative_change_pixel_ratio"),
            "cumulative_change_brightness_delta": _aggregate_metric(
                audit_records, "cumulative_change_brightness_delta"
            ),
            "cumulative_change_contrast_delta": _aggregate_metric(
                audit_records, "cumulative_change_contrast_delta"
            ),
            "cumulative_change_crop_ratio": _aggregate_metric(audit_records, "cumulative_change_crop_ratio"),
            "cumulative_change_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "cumulative_change_candidate_pixel_ratio"
            ),
        },
        "distributions": {
            "pixel_change_ratio": _ratio_distribution(audit_records, "pixel_change_ratio"),
            "crop_ratio": _ratio_distribution(audit_records, "crop_ratio"),
            "max_trim_margin_ratio": _ratio_distribution(audit_records, "max_trim_margin_ratio"),
            "despeckle_pixel_ratio": _ratio_distribution(audit_records, "despeckle_pixel_ratio"),
            "tone_changed_pixel_ratio": _ratio_distribution(audit_records, "tone_changed_pixel_ratio"),
        },
        "guardrails": {
            "enabled": True,
            "warning_files": len(warning_records),
            "failed_files": guardrail_failed_files,
            "failure_reasons": _reason_counts(guardrail_failures),
            "cumulative_change_guard": {
                "checked_files": sum(
                    1 for audit in audit_records if audit.get("cumulative_change_guard_checked") is True
                ),
                "reverted_files": sum(
                    1 for audit in audit_records if audit.get("cumulative_change_guard_reverted") is True
                ),
                "warning_files": sum(
                    1
                    for audit in audit_records
                    if audit.get("cumulative_change_guard_action") in {"reverted_to_source", "warn_review"}
                ),
                "max_score": _aggregate_metric(audit_records, "cumulative_change_score")["max"],
                "reason_distribution": _reason_counts(
                    reason
                    for audit in audit_records
                    for reason in audit.get("cumulative_change_guard_reasons", [])
                    if isinstance(reason, str)
                ),
            },
            "dark_border_trim": {
                "trimmed_files": sum(1 for record in processed_records if record.get("dark_border_trimmed") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("dark_border_trimmed") is False
                    and record.get("dark_border_reason") not in {None, "dark border trim disabled"}
                ),
                "reason_distribution": _reason_counts(reason for reason in dark_border_reasons if isinstance(reason, str)),
            },
            "auto_crop": _auto_crop_audit_summary(processed_records, audit_records, auto_crop_reasons),
            "deskew": {
                "corrected_files": sum(1 for record in processed_records if record.get("deskewed") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("deskewed") is False
                    and record.get("deskew_reason") not in {None, "deskew disabled"}
                ),
                "reason_distribution": _reason_counts(reason for reason in deskew_reasons if isinstance(reason, str)),
            },
            "despeckle": {
                "applied_files": sum(1 for record in processed_records if record.get("despeckled") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("despeckled") is False
                    and record.get("despeckle_reason") not in {None, "despeckle disabled"}
                ),
                "pixels_changed": sum(
                    _int_count(record.get("despeckle_pixels_changed")) for record in processed_records
                ),
                "backend_mode": _aggregate_despeckle_backend(processed_records, options.despeckle)["backend_mode"],
                "reason_distribution": _reason_counts(reason for reason in despeckle_reasons if isinstance(reason, str)),
            },
            "tone_normalization": {
                "applied_files": sum(1 for audit in audit_records if audit.get("tone_normalized") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("tone_normalized") is False
                    and record.get("tone_reason") not in {None, "tone normalization disabled"}
                ),
                "background_delta": _aggregate_metric(audit_records, "tone_background_delta"),
                "contrast_delta": _aggregate_metric(audit_records, "tone_contrast_delta"),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "tone_changed_pixel_ratio"),
                "reason_distribution": _reason_counts(reason for reason in tone_reasons if isinstance(reason, str)),
                "skip_reason_distribution": _reason_counts(tone_skipped_reasons),
                "protection_triggered_files": sum(
                    1
                    for reason in tone_skipped_reasons
                    if any(
                        marker in reason
                        for marker in (
                            "risk",
                            "noise",
                            "texture",
                            "high contrast",
                            "already normal",
                            "too dense",
                        )
                    )
                ),
                "conservative_scope_skip_files": sum(
                    1
                    for reason in tone_skipped_reasons
                    if any(marker in reason for marker in ("too dark", "overexposed", "outside conservative"))
                ),
                "low_confidence_skip_files": sum(
                    1
                    for reason in tone_skipped_reasons
                    if any(
                        marker in reason
                        for marker in (
                            "low-confidence",
                            "too sparse",
                            "tonal separation too small",
                            "improvement below",
                            "stretch range too narrow",
                        )
                    )
                ),
            },
            "edge_shadow": {
                "applied_files": sum(1 for audit in audit_records if audit.get("edge_shadow_lightened") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("edge_shadow_lightened") is False
                    and record.get("edge_shadow_reason") not in {None, "edge shadow lightening disabled"}
                ),
                "edge_distribution": _reason_counts(
                    edge
                    for record in processed_records
                    for edge in record.get("edge_shadow_edges", [])
                    if record.get("edge_shadow_lightened") is True and isinstance(edge, str)
                ),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "edge_shadow_changed_pixel_ratio"),
                "candidate_pixel_ratio": _aggregate_metric(audit_records, "edge_shadow_candidate_pixel_ratio"),
                "reason_distribution": _reason_counts(
                    reason for reason in edge_shadow_reasons if isinstance(reason, str)
                ),
                "skip_reason_distribution": _reason_counts(edge_shadow_skipped_reasons),
                "protection_triggered_files": sum(
                    1
                    for reason in edge_shadow_skipped_reasons
                    if any(
                        marker in reason
                        for marker in (
                            "risk",
                            "foreground too dense",
                            "texture",
                            "archival",
                            "正文",
                        )
                    )
                ),
                "conservative_scope_skip_files": sum(
                    1
                    for reason in edge_shadow_skipped_reasons
                    if "conservative" in reason or "broad uneven lighting" in reason
                ),
                "low_confidence_skip_files": sum(
                    1
                    for reason in edge_shadow_skipped_reasons
                    if "low-confidence" in reason or "no confident" in reason or "low tonal separation" in reason
                ),
            },
            "background_stains": {
                "applied_files": sum(
                    1 for audit in audit_records if audit.get("background_stains_lightened") is True
                ),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("background_stains_lightened") is False
                    and record.get("background_stains_reason") not in {None, "background stain lightening disabled"}
                ),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "background_stains_changed_pixel_ratio"),
                "candidate_pixel_ratio": _aggregate_metric(audit_records, "background_stains_candidate_pixel_ratio"),
                "reason_distribution": _reason_counts(
                    reason for reason in background_stains_reasons if isinstance(reason, str)
                ),
                "skip_reason_distribution": _reason_counts(background_stains_skipped_reasons),
                "protection_triggered_files": sum(1 for reason in background_stains_skipped_reasons if "risk" in reason),
                "conservative_scope_skip_files": sum(
                    1 for reason in background_stains_skipped_reasons if "conservative scope" in reason
                ),
                "low_confidence_skip_files": sum(
                    1 for reason in background_stains_skipped_reasons if "low-confidence" in reason
                ),
            },
            "scanlines": {
                "applied_files": sum(1 for audit in audit_records if audit.get("scanlines_lightened") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("scanlines_lightened") is False
                    and record.get("scanlines_reason") not in {None, "scanline lightening disabled"}
                ),
                "direction_distribution": _reason_counts(
                    record.get("scanlines_orientation")
                    for record in processed_records
                    if record.get("scanlines_lightened") is True
                    and isinstance(record.get("scanlines_orientation"), str)
                ),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "scanlines_changed_pixel_ratio"),
                "candidate_pixel_ratio": _aggregate_metric(audit_records, "scanlines_candidate_pixel_ratio"),
                "reason_distribution": _reason_counts(reason for reason in scanlines_reasons if isinstance(reason, str)),
                "skip_reason_distribution": _reason_counts(scanlines_skipped_reasons),
                "protection_triggered_files": sum(
                    1
                    for reason in scanlines_skipped_reasons
                    if any(marker in reason for marker in ("risk", "protected", "foreground too dense"))
                ),
                "conservative_scope_skip_files": sum(
                    1
                    for reason in scanlines_skipped_reasons
                    if "outside conservative scope" in reason
                    or "archival stripe risk" in reason
                    or "broad uneven lighting" in reason
                ),
                "low_confidence_skip_files": sum(
                    1
                    for reason in scanlines_skipped_reasons
                    if "low-confidence" in reason or "no confident" in reason
                ),
            },
            "faded_text": {
                "applied_files": sum(1 for audit in audit_records if audit.get("faded_text_enhanced") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("faded_text_enhanced") is False
                    and record.get("faded_text_reason") not in {None, "faded text enhancement disabled"}
                ),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "faded_text_changed_pixel_ratio"),
                "candidate_pixel_ratio": _aggregate_metric(audit_records, "faded_text_candidate_pixel_ratio"),
                "reason_distribution": _reason_counts(
                    reason for reason in faded_text_reasons if isinstance(reason, str)
                ),
                "skip_reason_distribution": _reason_counts(faded_text_skipped_reasons),
                "protection_triggered_files": sum(
                    1
                    for reason in faded_text_skipped_reasons
                    if any(marker in reason for marker in ("risk", "too dense", "dark foreground already present"))
                ),
                "conservative_scope_skip_files": sum(
                    1
                    for reason in faded_text_skipped_reasons
                    if "conservative" in reason or "outside" in reason
                ),
                "low_confidence_skip_files": sum(
                    1
                    for reason in faded_text_skipped_reasons
                    if any(marker in reason for marker in ("too weak", "too sparse", "insufficient", "no stable"))
                ),
            },
            "text_edges": {
                "applied_files": sum(1 for audit in audit_records if audit.get("text_edges_sharpened") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("text_edges_sharpened") is False
                    and record.get("text_edges_reason") not in {None, "text edge sharpening disabled"}
                ),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "text_edges_changed_pixel_ratio"),
                "candidate_pixel_ratio": _aggregate_metric(audit_records, "text_edges_candidate_pixel_ratio"),
                "reason_distribution": _reason_counts(
                    reason for reason in text_edges_reasons if isinstance(reason, str)
                ),
                "skip_reason_distribution": _reason_counts(text_edges_skipped_reasons),
                "protection_triggered_files": sum(
                    1
                    for reason in text_edges_skipped_reasons
                    if any(
                        marker in reason
                        for marker in (
                            "risk",
                            "too dense",
                            "photo-detail",
                            "table-region",
                            "edge mark",
                            "binding",
                        )
                    )
                ),
                "low_confidence_skip_files": sum(
                    1
                    for reason in text_edges_skipped_reasons
                    if any(
                        marker in reason
                        for marker in (
                            "too weak",
                            "too little",
                            "too sparse",
                            "insufficient",
                            "below conservative threshold",
                        )
                    )
                ),
                "candidate_preflight_skip_files": _int_count(
                    sharpen_text_edges_timing.get("candidate_preflight_skipped_files")
                ),
            },
        },
        "reuse_decisions": {
            "resume_skipped_existing_derivatives": {
                "count": summary["skipped_due_to_resume"],
                "reason": "prior successful derivative matched source hash, options fingerprint, and output hash",
            },
            "duplicate_derivative_reused": {
                "count": summary["duplicate_reused_files"],
                "reason": "duplicate source content reused an already processed derivative",
            },
            "existing_derivative_write_skipped": {
                "count": summary["existing_derivative_reused_files"],
                "reason": "existing derivative output already matched the expected derivative hash",
            },
            "scan_measurement_reuse": performance.get("scan_measurement_reuse", _empty_scan_measurement_reuse()),
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
        "normalize_tones": options.normalize_tones,
        "lighten_edge_shadow": options.lighten_edge_shadow,
        "lighten_background_stains": options.lighten_background_stains,
        "lighten_scanlines": options.lighten_scanlines,
        "enhance_faded_text": options.enhance_faded_text,
        "sharpen_text_edges": options.sharpen_text_edges,
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
        if operation == "deskew":
            timings[operation].update(_aggregate_deskew_detection_counts(records))
        if operation == "despeckle":
            timings[operation].update(_aggregate_despeckle_backend(records, is_enabled))
        if operation == "sharpen_text_edges":
            timings[operation]["candidate_preflight_skipped_files"] = _operation_flag_count(
                records,
                operation,
                "candidate_preflight_skip",
            )
    return timings


def _operation_reuse_count(records: list[dict[str, Any]], operation: str) -> int:
    return sum(
        1
        for record in records
        if isinstance(record.get("operation_timings"), dict)
        and isinstance(record["operation_timings"].get(operation), dict)
        and record["operation_timings"][operation].get("reused_scan_measurement") is True
    )


def _operation_flag_count(records: list[dict[str, Any]], operation: str, flag: str) -> int:
    return sum(
        1
        for record in records
        if isinstance(record.get("operation_timings"), dict)
        and isinstance(record["operation_timings"].get(operation), dict)
        and record["operation_timings"][operation].get(flag) is True
    )


def _aggregate_deskew_detection_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "safe_skip_files": _operation_flag_count(records, "deskew", "safe_skip"),
        "projection_detection_files": _operation_flag_count(records, "deskew", "projection_detection"),
        "fallback_detection_files": _operation_flag_count(records, "deskew", "fallback_detection"),
    }


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
        "deskew_safe_skip_files": _operation_flag_count(records, "deskew", "safe_skip"),
        "deskew_projection_detection_files": _operation_flag_count(records, "deskew", "projection_detection"),
        "deskew_fallback_detection_files": _operation_flag_count(records, "deskew", "fallback_detection"),
    }


def _empty_scan_measurement_reuse() -> dict[str, Any]:
    return {
        "enabled": False,
        "files_with_any_reuse": 0,
        "operations_skipped": {},
        "fallback_operations": {},
        "deskew_safe_skip_files": 0,
        "deskew_projection_detection_files": 0,
        "deskew_fallback_detection_files": 0,
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
        "max_cumulative_change_score": options.audit_max_cumulative_change_score,
        "max_cumulative_pixel_change_ratio": options.audit_max_cumulative_pixel_change_ratio,
        "max_cumulative_brightness_delta": options.audit_max_cumulative_brightness_delta,
        "max_cumulative_contrast_delta": options.audit_max_cumulative_contrast_delta,
        "max_cumulative_crop_ratio": options.audit_max_cumulative_crop_ratio,
        "max_cumulative_candidate_pixel_ratio": options.audit_max_cumulative_candidate_pixel_ratio,
        "max_tone_changed_pixel_ratio": 1.0,
        "max_faded_text_changed_pixel_ratio": 0.10,
        "max_faded_text_candidate_pixel_ratio": 0.18,
        "max_text_edges_changed_pixel_ratio": 0.08,
        "max_text_edges_candidate_pixel_ratio": 0.12,
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


def _auto_crop_audit_summary(
    processed_records: list[dict[str, Any]],
    audit_records: list[dict[str, Any]],
    auto_crop_reasons: list[str],
) -> dict[str, Any]:
    skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("crop_reason")]
        if record.get("cropped") is False
        and isinstance(reason, str)
        and reason != "auto crop disabled"
    ]
    return {
        "applied_files": sum(1 for record in processed_records if record.get("cropped") is True),
        "skipped_files": len(skipped_reasons),
        "cropped_side_distribution": _auto_crop_side_distribution(processed_records),
        "crop_ratio": _aggregate_metric(
            [audit for audit in audit_records if _float_metric(audit, "crop_ratio") > 0],
            "crop_ratio",
        ),
        "crop_ratio_distribution": _ratio_distribution(audit_records, "crop_ratio"),
        "reason_distribution": _reason_counts(reason for reason in auto_crop_reasons if isinstance(reason, str)),
        "skip_reason_distribution": _reason_counts(skipped_reasons),
        "post_deskew_safe_crop_files": sum(
            1
            for record in processed_records
            if record.get("crop_reason") == "post-deskew safe canvas crop applied"
        ),
        "edge_content_protection_skip_files": sum(
            1
            for reason in skipped_reasons
            if reason == "post-deskew crop skipped: edge content protection"
        ),
        "risk_skip_files": sum(1 for reason in skipped_reasons if "crop risk" in reason),
        "protection_triggered_files": _auto_crop_protection_skip_count(skipped_reasons),
        "low_confidence_skip_files": _auto_crop_low_confidence_skip_count(skipped_reasons),
        "cumulative_guard_reverted_files": sum(
            1
            for record in processed_records
            if record.get("crop_reason") == "reverted by cumulative change guard"
        ),
    }


def _auto_crop_side_distribution(processed_records: list[dict[str, Any]]) -> dict[str, int]:
    sides = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    for record in processed_records:
        bbox = record.get("crop_bbox")
        original_size = record.get("post_deskew_size") or record.get("original_size")
        if (
            record.get("cropped") is not True
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or not isinstance(original_size, list)
            or len(original_size) != 2
        ):
            continue
        width, height = original_size
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        left, top, right, bottom = bbox
        if all(isinstance(value, int) for value in (left, top, right, bottom)):
            if left > 0:
                sides["left"] += 1
            if top > 0:
                sides["top"] += 1
            if right < width:
                sides["right"] += 1
            if bottom < height:
                sides["bottom"] += 1
    return sides


def _auto_crop_protection_skip_count(reasons: list[str]) -> int:
    markers = (
        "foreground reaches crop safety margin",
        "inconsistent crop margin evidence",
        "crop boundary evidence is too sparse",
        "edge content",
        "edge content protection",
        "dark edge",
        "shadow",
        "binding",
        "exceeds conservative",
        "crop risk",
        "too small",
    )
    return sum(1 for reason in reasons if any(marker in reason for marker in markers))


def _auto_crop_low_confidence_skip_count(reasons: list[str]) -> int:
    markers = (
        "no confident foreground",
        "low-confidence",
        "low-confidence canvas edge",
        "low contrast",
        "subtle page edge",
        "weak crop",
        "image too small",
        "candidate crop change is too small",
    )
    return sum(1 for reason in reasons if any(marker in reason for marker in markers))


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
        safe_skip_skew = None if options.reuse_scan_measurements else _safe_deskew_skip_from_scan_record(
            scan_record,
            processed,
            options,
        )
        skew = reusable.get("skew") if safe_skip_skew is None else safe_skip_skew
        skew_from_projection = False
        if isinstance(skew, SkewDetection):
            operations.append("skew_detect_reused_scan_measurement")
            operation_timings.setdefault("deskew", {})["reused_scan_measurement"] = True
            if safe_skip_skew is not None:
                operations.append("deskew_safe_skip_scan_measurement")
                operation_timings.setdefault("deskew", {})["safe_skip_reason"] = "scan measurement proves no correction"
                operation_timings.setdefault("deskew", {})["safe_skip"] = True
        else:
            skew = _detect_skew(processed)
            skew_from_projection = True
            operations.append("skew_detect_projection")
            operation_timings.setdefault("deskew", {})["projection_detection"] = True
            if options.reuse_scan_measurements and options.deskew:
                operation_timings.setdefault("deskew", {})["fallback_reason"] = reusable.get(
                    "fallback_reason", "scan measurements unavailable"
                )
                operation_timings.setdefault("deskew", {})["fallback_detection"] = True
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
        elif skew_from_projection and _deskew_has_edge_content_risk(processed):
            operations.append("deskew_noop")
            deskew_reason = "edge content near rotation boundary"
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
    crop_reason = "auto crop disabled"
    with _operation_timer(operation_timings, "auto_crop", enabled=options.auto_crop):
        if options.auto_crop:
            crop_detection = (
                _detect_post_deskew_canvas_crop_bbox(processed)
                if deskewed
                else CropDetection(None, "post-deskew crop skipped: deskew not applied")
            )
            if crop_detection.bbox is None and crop_detection.reason != "post-deskew crop skipped: edge content protection":
                crop_detection = _detect_conservative_crop_bbox(processed)
            crop_bbox = crop_detection.bbox
            crop_reason = crop_detection.reason
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
            processed, despeckle_pixels_changed, despeckle_backend_mode, despeckle_reason = _despeckle_isolated_pixels_with_reason(
                processed,
                backend=options.despeckle_backend,
            )
            if despeckle_pixels_changed:
                operations.append("despeckle_isolated_pixels")
                despeckled = True
            else:
                operations.append("despeckle_noop")
        else:
            operations.append("despeckle_disabled")
    if options.despeckle and "despeckle" in operation_timings:
        operation_timings["despeckle"]["backend_mode"] = despeckle_backend_mode
        operation_timings["despeckle"]["numpy_available"] = despeckle_backend_mode == "numpy"

    tone = ToneNormalizationResult(processed, False, "tone normalization disabled", None, None, None, None)
    with _operation_timer(operation_timings, "normalize_tones", enabled=options.normalize_tones):
        if options.normalize_tones:
            tone = _normalize_tones_conservative(processed)
            processed = tone.image
            operations.append("normalize_tones_conservative" if tone.applied else "normalize_tones_noop")
        else:
            operations.append("normalize_tones_disabled")

    edge_shadow = EdgeShadowLighteningResult(
        processed, False, "edge shadow lightening disabled", (), None, None, 0.0, 0.0, 0.0
    )
    with _operation_timer(operation_timings, "lighten_edge_shadow", enabled=options.lighten_edge_shadow):
        if options.lighten_edge_shadow:
            edge_shadow = _lighten_edge_shadow_conservative(processed)
            processed = edge_shadow.image
            operations.append(
                "lighten_edge_shadow_conservative" if edge_shadow.applied else "lighten_edge_shadow_noop"
            )
        else:
            operations.append("lighten_edge_shadow_disabled")

    background_stains = BackgroundStainLighteningResult(
        processed, False, "background stain lightening disabled", None, None, 0.0, 0.0, 0.0
    )
    with _operation_timer(operation_timings, "lighten_background_stains", enabled=options.lighten_background_stains):
        if options.lighten_background_stains:
            background_stains = _lighten_background_stains_conservative(processed)
            processed = background_stains.image
            operations.append(
                "lighten_background_stains_conservative"
                if background_stains.applied
                else "lighten_background_stains_noop"
            )
        else:
            operations.append("lighten_background_stains_disabled")

    scanlines = ScanlineLighteningResult(
        processed, False, "scanline lightening disabled", None, 0, None, None, 0.0, 0.0, 0.0
    )
    with _operation_timer(operation_timings, "lighten_scanlines", enabled=options.lighten_scanlines):
        if options.lighten_scanlines:
            scanlines = _lighten_scanlines_conservative(processed)
            processed = scanlines.image
            operations.append("lighten_scanlines_conservative" if scanlines.applied else "lighten_scanlines_noop")
        else:
            operations.append("lighten_scanlines_disabled")

    faded_text = FadedTextEnhancementResult(
        processed, False, "faded text enhancement disabled", 0.0, 0.0, 0.0
    )
    with _operation_timer(operation_timings, "enhance_faded_text", enabled=options.enhance_faded_text):
        if options.enhance_faded_text:
            faded_text = _enhance_faded_text_conservative(processed)
            processed = faded_text.image
            operations.append(
                "enhance_faded_text_conservative" if faded_text.applied else "enhance_faded_text_noop"
            )
        else:
            operations.append("enhance_faded_text_disabled")

    text_edges = TextEdgeSharpeningResult(
        processed, False, "text edge sharpening disabled", 0.0, 0.0, 0.0
    )
    with _operation_timer(operation_timings, "sharpen_text_edges", enabled=options.sharpen_text_edges):
        if options.sharpen_text_edges:
            text_edges = _sharpen_text_edges_conservative(processed)
            if text_edges.preflight_skipped:
                operation_timings.setdefault("sharpen_text_edges", {})["candidate_preflight_skip"] = True
            processed = text_edges.image
            operations.append(
                "sharpen_text_edges_conservative" if text_edges.applied else "sharpen_text_edges_noop"
            )
        else:
            operations.append("sharpen_text_edges_disabled")

    attempted_audit = _processing_audit(
        audit_source,
        processed,
        options,
        crop_bbox,
        dark_border.bbox,
        skew.angle_degrees,
        despeckle_pixels_changed,
        tone.applied,
        tone.background_before,
        tone.background_after,
        tone.contrast_before,
        tone.contrast_after,
        tone.changed_pixel_ratio,
        edge_shadow.applied,
        edge_shadow.edge_delta,
        edge_shadow.changed_pixel_ratio,
        edge_shadow.candidate_pixel_ratio,
        background_stains.applied,
        background_stains.stain_delta,
        background_stains.changed_pixel_ratio,
        background_stains.candidate_pixel_ratio,
        scanlines.applied,
        scanlines.line_delta,
        scanlines.changed_pixel_ratio,
        scanlines.candidate_pixel_ratio,
        faded_text.applied,
        faded_text.text_delta,
        faded_text.changed_pixel_ratio,
        faded_text.candidate_pixel_ratio,
        text_edges.applied,
        text_edges.edge_delta,
        text_edges.changed_pixel_ratio,
        text_edges.candidate_pixel_ratio,
    )
    cumulative_guard = _cumulative_change_guard(attempted_audit, options)
    cumulative_guard_reverted = cumulative_guard["action"] == "reverted_to_source"
    if cumulative_guard_reverted:
        processed = audit_source.copy()
        operations.append("cumulative_change_guard_reverted_to_source")
        processing_audit = _processing_audit(
            audit_source,
            processed,
            options,
            None,
            None,
            None,
            0,
            cumulative_change_guard=cumulative_guard,
        )
    else:
        processing_audit = {**attempted_audit, **_cumulative_change_guard_audit_fields(cumulative_guard)}
    processing_warnings = list(processing_audit["guardrail_failures"])
    if cumulative_guard["action"] == "reverted_to_source":
        processing_warnings.append("cumulative_change_guard_reverted_to_source")
    crop_info = {
        "original_size": original_size,
        "output_size": list(processed.size),
        "pre_deskew_size": original_size if cumulative_guard_reverted else pre_deskew_size,
        "post_deskew_size": original_size if cumulative_guard_reverted else post_deskew_size,
        "skew_angle_degrees": None if cumulative_guard_reverted else skew.angle_degrees,
        "skew_confidence": skew.confidence,
        "deskewed": False if cumulative_guard_reverted else deskewed,
        "deskew_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else deskew_reason,
        "dark_border_trimmed": False if cumulative_guard_reverted else dark_border_trimmed,
        "dark_border_bbox": None if cumulative_guard_reverted else (list(dark_border.bbox) if dark_border.bbox else None),
        "dark_border_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else dark_border.reason,
        "crop_bbox": None if cumulative_guard_reverted else (list(crop_bbox) if crop_bbox else None),
        "crop_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else crop_reason,
        "cropped": False if cumulative_guard_reverted else crop_bbox is not None,
        "despeckled": False if cumulative_guard_reverted else despeckled,
        "despeckle_pixels_changed": 0 if cumulative_guard_reverted else despeckle_pixels_changed,
        "despeckle_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else despeckle_reason,
        "despeckle_backend_mode": despeckle_backend_mode,
        "tone_normalized": False if cumulative_guard_reverted else tone.applied,
        "tone_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else tone.reason,
        "tone_background_before": None if cumulative_guard_reverted else tone.background_before,
        "tone_background_after": None if cumulative_guard_reverted else tone.background_after,
        "tone_contrast_before": None if cumulative_guard_reverted else tone.contrast_before,
        "tone_contrast_after": None if cumulative_guard_reverted else tone.contrast_after,
        "tone_changed_pixel_ratio": 0.0 if cumulative_guard_reverted else tone.changed_pixel_ratio,
        "edge_shadow_lightened": False if cumulative_guard_reverted else edge_shadow.applied,
        "edge_shadow_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else edge_shadow.reason,
        "edge_shadow_edges": list(edge_shadow.edges),
        "edge_shadow_mean_before": None if cumulative_guard_reverted else edge_shadow.edge_mean_before,
        "edge_shadow_mean_after": None if cumulative_guard_reverted else edge_shadow.edge_mean_after,
        "edge_shadow_delta": 0.0 if cumulative_guard_reverted else edge_shadow.edge_delta,
        "edge_shadow_changed_pixel_ratio": 0.0 if cumulative_guard_reverted else edge_shadow.changed_pixel_ratio,
        "edge_shadow_candidate_pixel_ratio": 0.0
        if cumulative_guard_reverted
        else edge_shadow.candidate_pixel_ratio,
        "background_stains_lightened": False if cumulative_guard_reverted else background_stains.applied,
        "background_stains_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else background_stains.reason,
        "background_stains_mean_before": None if cumulative_guard_reverted else background_stains.stain_mean_before,
        "background_stains_mean_after": None if cumulative_guard_reverted else background_stains.stain_mean_after,
        "background_stains_delta": 0.0 if cumulative_guard_reverted else background_stains.stain_delta,
        "background_stains_changed_pixel_ratio": 0.0 if cumulative_guard_reverted else background_stains.changed_pixel_ratio,
        "background_stains_candidate_pixel_ratio": 0.0 if cumulative_guard_reverted else background_stains.candidate_pixel_ratio,
        "scanlines_lightened": False if cumulative_guard_reverted else scanlines.applied,
        "scanlines_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else scanlines.reason,
        "scanlines_orientation": scanlines.orientation,
        "scanlines_count": 0 if cumulative_guard_reverted else scanlines.line_count,
        "scanlines_mean_before": None if cumulative_guard_reverted else scanlines.line_mean_before,
        "scanlines_mean_after": None if cumulative_guard_reverted else scanlines.line_mean_after,
        "scanlines_delta": 0.0 if cumulative_guard_reverted else scanlines.line_delta,
        "scanlines_changed_pixel_ratio": 0.0 if cumulative_guard_reverted else scanlines.changed_pixel_ratio,
        "scanlines_candidate_pixel_ratio": 0.0 if cumulative_guard_reverted else scanlines.candidate_pixel_ratio,
        "faded_text_enhanced": False if cumulative_guard_reverted else faded_text.applied,
        "faded_text_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else faded_text.reason,
        "faded_text_delta": 0.0 if cumulative_guard_reverted else faded_text.text_delta,
        "faded_text_changed_pixel_ratio": 0.0 if cumulative_guard_reverted else faded_text.changed_pixel_ratio,
        "faded_text_candidate_pixel_ratio": 0.0 if cumulative_guard_reverted else faded_text.candidate_pixel_ratio,
        "text_edges_sharpened": False if cumulative_guard_reverted else text_edges.applied,
        "text_edges_reason": "reverted by cumulative change guard" if cumulative_guard_reverted else text_edges.reason,
        "text_edges_delta": 0.0 if cumulative_guard_reverted else text_edges.edge_delta,
        "text_edges_changed_pixel_ratio": 0.0 if cumulative_guard_reverted else text_edges.changed_pixel_ratio,
        "text_edges_candidate_pixel_ratio": 0.0 if cumulative_guard_reverted else text_edges.candidate_pixel_ratio,
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


def _normalize_tones_conservative(image: Image.Image) -> ToneNormalizationResult:
    if image.width < 30 or image.height < 30:
        return ToneNormalizationResult(image, False, "image too small for tone normalization", None, None, None, None)
    color_risk = _tone_color_risk_reason(image)
    if color_risk:
        return ToneNormalizationResult(image, False, color_risk, None, None, None, None)

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p01 = _histogram_percentile(histogram, total, 0.01)
    p05 = _histogram_percentile(histogram, total, 0.05)
    p50 = _histogram_percentile(histogram, total, 0.50)
    p95 = _histogram_percentile(histogram, total, 0.95)
    p99 = _histogram_percentile(histogram, total, 0.99)
    contrast_before = float(p95 - p05)
    background_before = float(p95)
    if p95 < 145:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: page is too dark for conservative automatic correction",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )
    if p05 > 205 and p95 >= 245:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: page appears overexposed",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )
    if p99 - p01 < 35:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: low-confidence tonal separation too small",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )
    if p95 >= 235 and contrast_before >= 65:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: exposure and contrast already normal",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )
    if contrast_before > 135:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: high contrast text already clear",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )
    if not (45 <= contrast_before <= 135 and 145 <= p95 <= 225):
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: outside conservative gray text range",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )

    foreground_threshold = max(0, min(p50 - 10, p95 - 35))
    foreground_pixels = sum(histogram[: foreground_threshold + 1])
    foreground_ratio = foreground_pixels / max(1, total)
    if foreground_ratio < 0.006:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: low-confidence foreground evidence too sparse",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )
    if foreground_ratio > 0.35:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: foreground too dense",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )

    local_noise_ratio = _tone_local_noise_ratio(grayscale)
    if local_noise_ratio > 0.008:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: high noise or fine texture risk",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )

    source_low = max(0, p05 - 6)
    source_high = min(255, p95 + 4)
    if source_high - source_low < 45:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: stretch range too narrow",
            background_before,
            background_before,
            contrast_before,
            contrast_before,
        )
    target_low = 70
    target_high = 224
    scale = (target_high - target_low) / (source_high - source_low)

    def map_value(value: int) -> int:
        return max(0, min(255, int(round((value - source_low) * scale + target_low))))

    normalized_l = grayscale.point(map_value, mode="L")
    normalized_histogram = normalized_l.histogram()
    background_after = float(_histogram_percentile(normalized_histogram, total, 0.95))
    contrast_after = float(
        _histogram_percentile(normalized_histogram, total, 0.95)
        - _histogram_percentile(normalized_histogram, total, 0.05)
    )
    changed_pixel_ratio = _tone_changed_pixel_ratio(grayscale, normalized_l)
    if background_after - background_before < 12 or contrast_after - contrast_before < 12:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: improvement below conservative threshold",
            background_before,
            background_after,
            contrast_before,
            contrast_after,
            changed_pixel_ratio,
        )
    if background_after - background_before > 75 or contrast_after - contrast_before > 75:
        return ToneNormalizationResult(
            image,
            False,
            "tone normalization skipped: brightness or contrast delta exceeds conservative threshold",
            background_before,
            background_after,
            contrast_before,
            contrast_after,
            changed_pixel_ratio,
        )

    normalized = _replace_luminance_preserving_chroma(image, normalized_l)
    return ToneNormalizationResult(
        normalized,
        True,
        "tone normalization applied: neutral gray low-contrast text page",
        background_before,
        background_after,
        contrast_before,
        contrast_after,
        changed_pixel_ratio,
    )


def _tone_local_noise_ratio(grayscale: Image.Image) -> float:
    median = grayscale.filter(ImageFilter.MedianFilter(size=3))
    diff = ImageChops.difference(grayscale, median)
    changed = sum(diff.point(lambda value: 255 if value > 18 else 0).histogram()[1:])
    return changed / max(1, grayscale.width * grayscale.height)


def _tone_changed_pixel_ratio(source_l: Image.Image, normalized_l: Image.Image) -> float:
    diff = ImageChops.difference(source_l, normalized_l)
    changed = sum(diff.point(lambda value: 255 if value > 20 else 0).histogram()[1:])
    return changed / max(1, source_l.width * source_l.height)


def _replace_luminance_preserving_chroma(image: Image.Image, normalized_l: Image.Image) -> Image.Image:
    if image.mode == "L":
        return normalized_l
    ycbcr = image.convert("YCbCr")
    _y, cb, cr = ycbcr.split()
    return Image.merge("YCbCr", (normalized_l, cb, cr)).convert("RGB")


def _lighten_edge_shadow_conservative(image: Image.Image) -> EdgeShadowLighteningResult:
    if image.width < 80 or image.height < 80:
        return _edge_shadow_noop(image, "edge shadow lightening skipped: image too small")
    color_risk = _tone_color_risk_reason(image)
    if color_risk:
        return _edge_shadow_noop(image, "edge shadow lightening skipped: color content or annotation risk")

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p05 = _histogram_percentile(histogram, total, 0.05)
    p95 = _histogram_percentile(histogram, total, 0.95)
    if p95 < 170:
        return _edge_shadow_noop(image, "edge shadow lightening skipped: page is too dark")
    if p95 - p05 < 28:
        return _edge_shadow_noop(image, "edge shadow lightening skipped: low tonal separation")

    strip = max(6, min(24, int(round(min(image.width, image.height) * 0.055))))
    edge_plans: list[tuple[str, tuple[int, int, int, int], tuple[int, int, int, int], float, int, float]] = []
    candidates = [
        ("left", (0, 0, strip, image.height), (strip, 0, strip * 3, image.height)),
        (
            "right",
            (image.width - strip, 0, image.width, image.height),
            (image.width - strip * 3, 0, image.width - strip, image.height),
        ),
        ("top", (0, 0, image.width, strip), (0, strip, image.width, strip * 3)),
        (
            "bottom",
            (0, image.height - strip, image.width, image.height),
            (0, image.height - strip * 3, image.width, image.height - strip),
        ),
    ]
    for side, edge_box, inner_box in candidates:
        edge = grayscale.crop(edge_box)
        inner = grayscale.crop(inner_box)
        edge_mean = ImageStat.Stat(edge).mean[0]
        inner_mean = ImageStat.Stat(inner).mean[0]
        edge_std = ImageStat.Stat(edge).stddev[0]
        inner_std = ImageStat.Stat(inner).stddev[0]
        edge_dark_ratio = _dark_pixel_ratio(edge, 125)
        inner_dark_ratio = _dark_pixel_ratio(inner, 125)
        edge_foreground_ratio = _dark_pixel_ratio(edge, 164)
        inner_foreground_ratio = _dark_pixel_ratio(inner, 164)
        delta = inner_mean - edge_mean
        if edge_dark_ratio > 0.006:
            return _edge_shadow_noop(image, f"edge shadow lightening skipped: archival mark or content risk at {side} edge")
        if inner_dark_ratio > 0.012:
            return _edge_shadow_noop(image, f"edge shadow lightening skipped:正文 or margin content risk near {side} edge")
        if edge_foreground_ratio > 0.035 or inner_foreground_ratio > 0.045:
            return _edge_shadow_noop(
                image,
                f"edge shadow lightening skipped: foreground too dense near {side} edge",
            )
        if edge_std > 24 or inner_std > 32:
            return _edge_shadow_noop(image, f"edge shadow lightening skipped: texture risk near {side} edge")
        if 10 <= delta <= 62 and edge_mean >= 132 and inner_mean >= 168:
            candidate_pixels = _edge_shadow_candidate_pixels(edge, inner_mean)
            candidate_ratio = candidate_pixels / max(1, total)
            if candidate_ratio < 0.008:
                return _edge_shadow_noop(
                    image,
                    f"edge shadow lightening skipped: low-confidence narrow shadow near {side} edge",
                )
            edge_plans.append((side, edge_box, inner_box, min(30.0, delta * 0.68), candidate_pixels, inner_mean))

    if not edge_plans:
        return _edge_shadow_noop(image, "edge shadow lightening skipped: no confident page-edge shadow")
    if len(edge_plans) > 2:
        return _edge_shadow_noop(image, "edge shadow lightening skipped: broad uneven lighting is outside conservative edge scope")

    working_l = grayscale.copy()
    before_values: list[float] = []
    after_values: list[float] = []
    changed_pixels = 0
    candidate_pixels_total = sum(
        candidate_pixels for _side, _edge_box, _inner_box, _delta, candidate_pixels, _inner_mean in edge_plans
    )
    for side, edge_box, _inner_box, max_delta, _candidate_pixels, inner_mean in edge_plans:
        edge = working_l.crop(edge_box)
        before_values.append(ImageStat.Stat(edge).mean[0])
        pixels = edge.load()
        width, height = edge.size
        for y in range(height):
            for x in range(width):
                value = pixels[x, y]
                if value < 132:
                    continue
                distance = (
                    x
                    if side == "left"
                    else width - 1 - x
                    if side == "right"
                    else y
                    if side == "top"
                    else height - 1 - y
                )
                factor = 1.0 - (distance / max(1, (width if side in {"left", "right"} else height)))
                if value > 248 or value > inner_mean - 4:
                    continue
                new_value = min(255, int(round(value + max_delta * max(0.0, factor))))
                if new_value - value > 2:
                    changed_pixels += 1
                pixels[x, y] = new_value
        working_l.paste(edge, edge_box)
        after_values.append(ImageStat.Stat(working_l.crop(edge_box)).mean[0])

    changed_ratio = changed_pixels / max(1, image.width * image.height)
    if changed_ratio > 0.18:
        return _edge_shadow_noop(image, "edge shadow lightening skipped: changed area exceeds conservative edge scope")

    result_image = working_l if image.mode == "L" else Image.merge("RGB", (working_l, working_l, working_l))
    before_mean = round(sum(before_values) / len(before_values), 6)
    after_mean = round(sum(after_values) / len(after_values), 6)
    candidate_ratio = candidate_pixels_total / max(1, total)
    return EdgeShadowLighteningResult(
        result_image,
        True,
        "edge shadow lightening applied: narrow neutral page-edge shadow",
        tuple(side for side, _edge_box, _inner_box, _delta, _candidate_pixels, _inner_mean in edge_plans),
        before_mean,
        after_mean,
        round(after_mean - before_mean, 6),
        round(changed_ratio, 6),
        round(candidate_ratio, 6),
    )


def _edge_shadow_noop(image: Image.Image, reason: str) -> EdgeShadowLighteningResult:
    return EdgeShadowLighteningResult(image, False, reason, (), None, None, 0.0, 0.0, 0.0)


def _edge_shadow_candidate_pixels(edge: Image.Image, inner_mean: float) -> int:
    upper = max(132, min(248, int(round(inner_mean - 4))))
    histogram = edge.histogram()
    return sum(histogram[132 : upper + 1])


def _lighten_background_stains_conservative(image: Image.Image) -> BackgroundStainLighteningResult:
    if image.width < 80 or image.height < 80:
        return _background_stains_noop(image, "background stain lightening skipped: image too small")
    color_risk = _background_stain_color_risk_reason(image)
    if color_risk:
        return _background_stains_noop(
            image,
            f"background stain lightening skipped: {color_risk}",
        )

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p05 = _histogram_percentile(histogram, total, 0.05)
    p50 = _histogram_percentile(histogram, total, 0.50)
    p90 = _histogram_percentile(histogram, total, 0.90)
    p95 = _histogram_percentile(histogram, total, 0.95)
    p99 = _histogram_percentile(histogram, total, 0.99)
    if p95 < 210 or p50 < 195:
        return _background_stains_noop(image, "background stain lightening skipped: page is too dark")
    if p99 - p05 < 14:
        return _background_stains_noop(image, "background stain lightening skipped: low-confidence tonal evidence")

    foreground_threshold = min(150, max(80, p50 - 46))
    foreground = grayscale.point(lambda value: 255 if value <= foreground_threshold else 0, mode="L")
    foreground_ratio = _mask_ratio(foreground)
    if foreground_ratio < 0.002:
        return _background_stains_noop(image, "background stain lightening skipped: foreground evidence too sparse")
    if foreground_ratio > 0.24:
        return _background_stains_noop(image, "background stain lightening skipped: foreground too dense")
    if _protected_edge_dark_ratio(foreground) > 0.0025:
        return _background_stains_noop(
            image,
            "background stain lightening skipped: binding, edge mark, or margin content risk",
        )

    background = max(p90, p95 - 2)
    min_stain = max(160, min(p50 + 4, background - 30))
    max_stain = max(min_stain, background - 8)
    pixel_candidate = grayscale.point(
        lambda value: 255 if min_stain <= value <= max_stain and 8 <= background - value <= 34 else 0,
        mode="L",
    )
    blur_radius = max(3, min(9, int(round(min(image.width, image.height) * 0.035))))
    low_frequency = grayscale.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    light_tonal_candidate = grayscale.point(
        lambda value: 255 if 165 <= value <= background - 4 and 4 <= background - value <= 35 else 0,
        mode="L",
    )
    low_frequency_candidate = low_frequency.point(
        lambda value: 255 if 170 <= value <= background - 5 and 5 <= background - value <= 28 else 0,
        mode="L",
    )
    low_frequency_candidate = ImageChops.multiply(low_frequency_candidate, light_tonal_candidate)
    candidate = ImageChops.lighter(pixel_candidate, low_frequency_candidate)
    edge_margin = max(3, int(round(min(image.width, image.height) * 0.025)))
    edge_cleared_candidate = _clear_mask_edges(candidate, edge_margin)
    edge_candidate_ratio = _mask_ratio(candidate) - _mask_ratio(edge_cleared_candidate)
    raw_candidate_ratio = _mask_ratio(edge_cleared_candidate)
    if raw_candidate_ratio > 0.09:
        return _background_stains_noop(
            image,
            "background stain lightening skipped: broad uneven lighting is outside conservative scope",
            raw_candidate_ratio,
        )
    if edge_candidate_ratio > 0.0004:
        return _background_stains_noop(
            image,
            "background stain lightening skipped: binding, edge mark, or margin content risk",
            edge_candidate_ratio,
        )
    candidate = edge_cleared_candidate
    protected = foreground.filter(ImageFilter.MaxFilter(13))
    protected_overlap_ratio = _mask_ratio(ImageChops.multiply(candidate, protected))
    if protected_overlap_ratio > 0.001 or protected_overlap_ratio / max(raw_candidate_ratio, 0.000001) > 0.02:
        return _background_stains_noop(
            image,
            "background stain lightening skipped: stain candidate near text, stamp, annotation, or original mark risk",
        )
    candidate = ImageChops.multiply(candidate, ImageChops.invert(protected))
    candidate_ratio = _mask_ratio(candidate)
    if candidate_ratio < 0.00008:
        return _background_stains_noop(image, "background stain lightening skipped: no confident light background stains")
    if candidate_ratio > 0.09:
        return _background_stains_noop(
            image,
            "background stain lightening skipped: broad uneven lighting is outside conservative scope",
            candidate_ratio,
        )

    components = [component for component in _mask_components(candidate) if len(component) >= 6]
    if not components:
        return _background_stains_noop(image, "background stain lightening skipped: no confident light background stains")
    if len(components) > 6:
        return _background_stains_noop(
            image,
            "background stain lightening skipped: too many stain candidates outside conservative scope",
            candidate_ratio,
        )
    selected: set[tuple[int, int]] = set()
    for component in components:
        area = len(component)
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        width = max(xs) - min(xs) + 1
        height = max(ys) - min(ys) + 1
        if area < 6:
            continue
        area_ratio = area / total
        low_frequency_shape = (
            area_ratio <= 0.08
            and width <= image.width * 0.62
            and height <= image.height * 0.62
            and width >= image.width * 0.24
            and height >= image.height * 0.22
            and area_ratio >= 0.004
        )
        small_speckle_shape = area_ratio <= 0.012 and width <= image.width * 0.18 and height <= image.height * 0.18
        if not (small_speckle_shape or low_frequency_shape):
            return _background_stains_noop(
                image,
                "background stain lightening skipped: large stain or historical damage risk",
                candidate_ratio,
            )
        selected.update(component)

    changed_ratio = len(selected) / max(1, total)
    if changed_ratio < 0.00008:
        return _background_stains_noop(image, "background stain lightening skipped: no confident light background stains")
    if changed_ratio > 0.08:
        return _background_stains_noop(
            image,
            "background stain lightening skipped: changed area exceeds conservative background scope",
            candidate_ratio,
        )

    before_values: list[int] = []
    after_values: list[int] = []
    if image.mode == "L":
        output = grayscale.copy()
        pixels = output.load()
        for x, y in selected:
            value = pixels[x, y]
            new_value = min(255, int(round(value + min(22, max(4, (background - value) * 0.78)))))
            pixels[x, y] = new_value
            before_values.append(value)
            after_values.append(new_value)
        result_image = output
    else:
        source = image.convert("RGB")
        output = source.copy()
        output_pixels = output.load()
        gray_pixels = grayscale.load()
        for x, y in selected:
            gray_value = gray_pixels[x, y]
            delta = min(22, max(4, int(round((background - gray_value) * 0.78))))
            red_value, green_value, blue_value = output_pixels[x, y]
            output_pixels[x, y] = (
                min(255, red_value + delta),
                min(255, green_value + delta),
                min(255, blue_value + delta),
            )
            before_values.append(gray_value)
            after_values.append(min(255, gray_value + delta))
        result_image = output

    before_mean = round(sum(before_values) / len(before_values), 6)
    after_mean = round(sum(after_values) / len(after_values), 6)
    if after_mean - before_mean < 4:
        return _background_stains_noop(
            image,
            "background stain lightening skipped: improvement below conservative threshold",
            candidate_ratio,
        )
    return BackgroundStainLighteningResult(
        result_image,
        True,
        "background stain lightening applied: conservative low-contrast stains on light background",
        before_mean,
        after_mean,
        round(after_mean - before_mean, 6),
        round(changed_ratio, 6),
        round(candidate_ratio, 6),
    )


def _background_stain_color_risk_reason(image: Image.Image) -> str | None:
    if image.mode == "L":
        return None
    sample = image.convert("RGB")
    sample.thumbnail((600, 600), Image.Resampling.BILINEAR)
    total = max(1, sample.width * sample.height)
    colored = 0
    red = 0
    weak_warm_background = 0
    pixel_data = sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata()
    for red_value, green_value, blue_value in pixel_data:
        high = max(red_value, green_value, blue_value)
        low = min(red_value, green_value, blue_value)
        spread = high - low
        brightness = (red_value + green_value + blue_value) / 3
        if red_value >= 110 and red_value - green_value >= 35 and red_value - blue_value >= 35:
            red += 1
        if spread > 18 and 30 < brightness < 250:
            colored += 1
        if (
            170 <= brightness <= 246
            and 10 <= spread <= 36
            and red_value >= green_value >= blue_value
            and red_value - blue_value <= 38
        ):
            weak_warm_background += 1
    red_ratio = red / total
    if red_ratio >= 0.0004:
        return "color content, stamp, or annotation risk"
    colored_ratio = colored / total
    weak_warm_ratio = weak_warm_background / total
    if 0.0008 <= colored_ratio < 0.01 and weak_warm_ratio / max(colored_ratio, 0.000001) < 0.85:
        return "color content, stamp, or annotation risk"
    if colored_ratio >= 0.003 and (weak_warm_ratio / max(colored_ratio, 0.000001) < 0.85 or colored_ratio > 0.16):
        return "color content, stamp, or annotation risk"
    return None


def _background_stains_noop(
    image: Image.Image,
    reason: str,
    candidate_pixel_ratio: float = 0.0,
) -> BackgroundStainLighteningResult:
    return BackgroundStainLighteningResult(
        image,
        False,
        reason,
        None,
        None,
        0.0,
        0.0,
        round(candidate_pixel_ratio, 6),
    )


def _lighten_scanlines_conservative(image: Image.Image) -> ScanlineLighteningResult:
    if image.width < 80 or image.height < 80:
        return _scanlines_noop(image, "scanline lightening skipped: image too small")
    color_risk = _tone_color_risk_reason(image)
    if color_risk:
        return _scanlines_noop(image, "scanline lightening skipped: color content, stamp, or annotation risk")

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p01 = _histogram_percentile(histogram, total, 0.01)
    p05 = _histogram_percentile(histogram, total, 0.05)
    p50 = _histogram_percentile(histogram, total, 0.50)
    p90 = _histogram_percentile(histogram, total, 0.90)
    p95 = _histogram_percentile(histogram, total, 0.95)
    p99 = _histogram_percentile(histogram, total, 0.99)
    if p95 < 210 or p50 < 190:
        return _scanlines_noop(image, "scanline lightening skipped: page is too dark")
    if p99 - p01 < 24:
        return _scanlines_noop(image, "scanline lightening skipped: low-confidence tonal evidence")

    foreground_threshold = min(155, max(78, p50 - 44))
    foreground = grayscale.point(lambda value: 255 if value <= foreground_threshold else 0, mode="L")
    foreground_ratio = _mask_ratio(foreground)
    if foreground_ratio < 0.0015:
        return _scanlines_noop(image, "scanline lightening skipped: foreground evidence too sparse")
    if foreground_ratio > 0.08 and p05 < 70:
        return _scanlines_noop(image, "scanline lightening skipped: foreground too dense")
    if foreground_ratio > 0.08 and p05 < 165 and p50 < 225:
        return _scanlines_noop(image, "scanline lightening skipped: photo, illustration, or dense texture risk")
    if foreground_ratio > 0.24:
        return _scanlines_noop(image, "scanline lightening skipped: foreground too dense")
    if _protected_edge_dark_ratio(foreground) > 0.0025:
        return _scanlines_noop(
            image,
            "scanline lightening skipped: binding, edge mark, or margin content risk",
        )

    protected = foreground.filter(ImageFilter.MaxFilter(13))
    horizontal = _scanline_axis_lightening_plan(grayscale, protected, horizontal=True, background=p90)
    vertical = _scanline_axis_lightening_plan(grayscale, protected, horizontal=False, background=p90)
    plan = horizontal if horizontal["score"] >= vertical["score"] else vertical
    if plan["reason"]:
        return _scanlines_noop(image, f"scanline lightening skipped: {plan['reason']}", plan["candidate_ratio"])

    selected = plan["selected"]
    selected_count = len(selected)
    changed_ratio = selected_count / max(1, total)
    candidate_ratio = round(plan["candidate_ratio"], 6)
    if changed_ratio < 0.0007:
        return _scanlines_noop(image, "scanline lightening skipped: no confident low-contrast scanlines", candidate_ratio)
    if changed_ratio > 0.035:
        return _scanlines_noop(
            image,
            "scanline lightening skipped: broad uneven lighting is outside conservative scope",
            candidate_ratio,
        )

    before_values: list[int] = []
    after_values: list[int] = []
    if image.mode == "L":
        output = grayscale.copy()
        pixels = output.load()
        for x, y in selected:
            value = pixels[x, y]
            delta = min(18, max(4, int(round((p95 - value) * 0.72))))
            new_value = min(255, value + delta)
            pixels[x, y] = new_value
            before_values.append(value)
            after_values.append(new_value)
        result_image = output
    else:
        source = image.convert("RGB")
        output = source.copy()
        output_pixels = output.load()
        gray_pixels = grayscale.load()
        for x, y in selected:
            gray_value = gray_pixels[x, y]
            delta = min(18, max(4, int(round((p95 - gray_value) * 0.72))))
            red_value, green_value, blue_value = output_pixels[x, y]
            output_pixels[x, y] = (
                min(255, red_value + delta),
                min(255, green_value + delta),
                min(255, blue_value + delta),
            )
            before_values.append(gray_value)
            after_values.append(min(255, gray_value + delta))
        result_image = output

    before_mean = round(sum(before_values) / len(before_values), 6)
    after_mean = round(sum(after_values) / len(after_values), 6)
    if after_mean - before_mean < 4:
        return _scanlines_noop(
            image,
            "scanline lightening skipped: improvement below conservative threshold",
            candidate_ratio,
        )
    return ScanlineLighteningResult(
        result_image,
        True,
        "scanline lightening applied: low-contrast neutral background scanlines",
        plan["orientation"],
        len(plan["lines"]),
        before_mean,
        after_mean,
        round(after_mean - before_mean, 6),
        round(changed_ratio, 6),
        candidate_ratio,
    )


def _scanline_axis_lightening_plan(
    grayscale: Image.Image,
    protected: Image.Image,
    *,
    horizontal: bool,
    background: int,
) -> dict[str, Any]:
    width, height = grayscale.size
    axis_length = height if horizontal else width
    cross_length = width if horizontal else height
    orientation = "horizontal" if horizontal else "vertical"
    margin = max(4, int(round(axis_length * 0.035)))
    if axis_length <= margin * 2 or cross_length < 40:
        return _empty_scanline_lightening_plan(orientation, "image too small")

    pixels = grayscale.load()
    protected_pixels = protected.load()
    line_stats: list[dict[str, Any]] = []
    all_candidates: set[tuple[int, int]] = set()
    max_dark_ratio = 0.0
    for index in range(axis_length):
        values: list[int] = []
        selected: list[tuple[int, int]] = []
        dark = 0
        protected_count = 0
        colored_axis = range(width) if horizontal else range(height)
        for cross in colored_axis:
            x, y = (cross, index) if horizontal else (index, cross)
            value = pixels[x, y]
            if protected_pixels[x, y]:
                protected_count += 1
                continue
            values.append(value)
            if value <= 125:
                dark += 1
            if 5 <= background - value <= 24 and value >= 170:
                selected.append((x, y))
        available_ratio = len(values) / max(1, cross_length)
        protected_ratio = protected_count / max(1, cross_length)
        candidate_ratio = len(selected) / max(1, cross_length)
        candidate_available_ratio = len(selected) / max(1, len(values))
        dark_ratio = dark / max(1, len(values)) if values else 1.0
        max_dark_ratio = max(max_dark_ratio, dark_ratio)
        mean = sum(values) / len(values) if values else 0.0
        line_stats.append(
            {
                "mean": mean,
                "available_ratio": available_ratio,
                "protected_ratio": protected_ratio,
                "candidate_ratio": candidate_ratio,
                "candidate_available_ratio": candidate_available_ratio,
                "dark_ratio": dark_ratio,
                "selected": selected,
            }
        )
        all_candidates.update(selected)

    candidate_total_ratio = len(all_candidates) / max(1, width * height)
    if max_dark_ratio > 0.025:
        return _empty_scanline_lightening_plan(orientation, "text, table line, stamp, annotation, or original mark risk", candidate_total_ratio)
    if candidate_total_ratio > 0.09:
        return _empty_scanline_lightening_plan(orientation, "broad uneven lighting is outside conservative scope", candidate_total_ratio)

    candidate_lines: list[int] = []
    selected: set[tuple[int, int]] = set()
    score = 0.0
    for index in range(margin, axis_length - margin):
        stat = line_stats[index]
        if stat["protected_ratio"] > 0.012:
            continue
        if stat["available_ratio"] < 0.72:
            continue
        if stat["candidate_ratio"] < 0.38 or stat["candidate_available_ratio"] < 0.54:
            continue
        neighbor_means = [
            line_stats[neighbor]["mean"]
            for offset in range(-8, 9)
            if abs(offset) >= 3
            for neighbor in [index + offset]
            if 0 <= neighbor < axis_length and line_stats[neighbor]["available_ratio"] >= 0.72
        ]
        if not neighbor_means:
            continue
        local_mean = sum(neighbor_means) / len(neighbor_means)
        local_delta = local_mean - stat["mean"]
        if not (4.5 <= local_delta <= 22.0):
            continue
        candidate_lines.append(index)
        selected.update(stat["selected"])
        score += min(1.0, stat["candidate_available_ratio"] / 0.8) * min(1.0, local_delta / 12.0)

    if not candidate_lines:
        return _empty_scanline_lightening_plan(orientation, "no confident low-contrast scanlines", candidate_total_ratio)
    groups = _contiguous_groups(candidate_lines)
    if len(groups) > 6 or any(len(group) > 4 for group in groups):
        return _empty_scanline_lightening_plan(
            orientation,
            "broad uneven lighting or archival stripe risk",
            candidate_total_ratio,
        )
    return {
        "orientation": orientation,
        "score": score,
        "lines": candidate_lines,
        "selected": selected,
        "candidate_ratio": candidate_total_ratio,
        "reason": None,
    }


def _empty_scanline_lightening_plan(
    orientation: str,
    reason: str,
    candidate_ratio: float = 0.0,
) -> dict[str, Any]:
    return {
        "orientation": orientation,
        "score": 0.0,
        "lines": [],
        "selected": set(),
        "candidate_ratio": round(candidate_ratio, 6),
        "reason": reason,
    }


def _contiguous_groups(values: list[int]) -> list[list[int]]:
    groups: list[list[int]] = []
    for value in sorted(values):
        if not groups or value != groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return groups


def _scanlines_noop(
    image: Image.Image,
    reason: str,
    candidate_pixel_ratio: float = 0.0,
) -> ScanlineLighteningResult:
    return ScanlineLighteningResult(
        image,
        False,
        reason,
        None,
        0,
        None,
        None,
        0.0,
        0.0,
        round(candidate_pixel_ratio, 6),
    )


def _enhance_faded_text_conservative(image: Image.Image) -> FadedTextEnhancementResult:
    if image.width < 80 or image.height < 80:
        return _faded_text_noop(image, "faded text enhancement skipped: image too small")
    color_risk = _tone_color_risk_reason(image)
    if color_risk:
        return _faded_text_noop(image, "faded text enhancement skipped: color content, stamp, or annotation risk")

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p01 = _histogram_percentile(histogram, total, 0.01)
    p05 = _histogram_percentile(histogram, total, 0.05)
    p50 = _histogram_percentile(histogram, total, 0.50)
    p95 = _histogram_percentile(histogram, total, 0.95)
    p99 = _histogram_percentile(histogram, total, 0.99)
    if p95 < 220 or p50 < 210:
        return _faded_text_noop(image, "faded text enhancement skipped: page is not a light paper background")
    if p05 < 105:
        return _faded_text_noop(image, "faded text enhancement skipped: dark foreground already present")
    if p99 - p01 < 18:
        return _faded_text_noop(image, "faded text enhancement skipped: text evidence too weak")
    if p95 - p05 > 92:
        return _faded_text_noop(image, "faded text enhancement skipped: contrast already normal or mixed content risk")

    threshold = min(214, p50 - 12, p95 - 14)
    if threshold < 125:
        return _faded_text_noop(image, "faded text enhancement skipped: outside conservative faded ink range")
    sampled_candidate_ratio = _faded_text_sample_candidate_ratio(grayscale, threshold, p95)
    if sampled_candidate_ratio < 0.0015:
        return _faded_text_noop(
            image,
            "faded text enhancement skipped: foreground evidence too sparse",
            sampled_candidate_ratio,
        )
    if sampled_candidate_ratio > 0.20:
        return _faded_text_noop(
            image,
            "faded text enhancement skipped: foreground too dense",
            sampled_candidate_ratio,
        )
    raw_candidate = grayscale.point(
        lambda value: 255 if 95 <= value <= threshold and 14 <= p95 - value <= 76 else 0,
        mode="L",
    )
    raw_candidate_ratio = _mask_ratio(raw_candidate)
    if _protected_edge_dark_ratio(raw_candidate) > 0.002:
        return _faded_text_noop(image, "faded text enhancement skipped: edge mark or binding risk", raw_candidate_ratio)

    candidate = _clear_mask_edges(raw_candidate, max(3, int(round(min(image.width, image.height) * 0.025))))
    candidate_ratio = _mask_ratio(candidate)
    if candidate_ratio < 0.0025:
        return _faded_text_noop(image, "faded text enhancement skipped: foreground evidence too sparse", candidate_ratio)
    if candidate_ratio > 0.16:
        return _faded_text_noop(image, "faded text enhancement skipped: foreground too dense", candidate_ratio)

    components = _mask_components(candidate)
    if not components:
        return _faded_text_noop(image, "faded text enhancement skipped: no stable text components", candidate_ratio)
    selected: set[tuple[int, int]] = set()
    text_like_components = 0
    rejected_large_components = 0
    for component in components:
        area = len(component)
        if area < 4:
            continue
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        width = max(xs) - min(xs) + 1
        height = max(ys) - min(ys) + 1
        line_like = height <= 8 and width <= image.width * 0.70
        if area / total > 0.018 or (not line_like and width > image.width * 0.42) or height > image.height * 0.22:
            rejected_large_components += 1
            continue
        fill_ratio = area / max(1, width * height)
        aspect = max(width / max(1, height), height / max(1, width))
        if fill_ratio > 0.82 and area > 24 and not line_like:
            rejected_large_components += 1
            continue
        if width >= 5 and height >= 1 and aspect <= 60:
            text_like_components += 1
            selected.update(component)
    if rejected_large_components:
        return _faded_text_noop(
            image,
            "faded text enhancement skipped: broad stain, texture, illustration, or table-region risk",
            candidate_ratio,
        )
    selected_ratio = len(selected) / max(1, total)
    if text_like_components < 3 or selected_ratio < 0.0025:
        return _faded_text_noop(image, "faded text enhancement skipped: stable text evidence insufficient", candidate_ratio)
    if selected_ratio > 0.09:
        return _faded_text_noop(
            image,
            "faded text enhancement skipped: changed area exceeds conservative text scope",
            candidate_ratio,
        )

    before_values: list[int] = []
    after_values: list[int] = []
    if image.mode == "L":
        output = grayscale.copy()
        pixels = output.load()
        for x, y in selected:
            value = pixels[x, y]
            delta = min(24, max(8, int(round((p95 - value) * 0.38))))
            new_value = max(0, value - delta)
            pixels[x, y] = new_value
            before_values.append(value)
            after_values.append(new_value)
        result_image = output
    else:
        source = image.convert("RGB")
        output = source.copy()
        output_pixels = output.load()
        gray_pixels = grayscale.load()
        for x, y in selected:
            gray_value = gray_pixels[x, y]
            delta = min(24, max(8, int(round((p95 - gray_value) * 0.38))))
            red_value, green_value, blue_value = output_pixels[x, y]
            output_pixels[x, y] = (
                max(0, red_value - delta),
                max(0, green_value - delta),
                max(0, blue_value - delta),
            )
            before_values.append(gray_value)
            after_values.append(max(0, gray_value - delta))
        result_image = output

    before_mean = sum(before_values) / len(before_values)
    after_mean = sum(after_values) / len(after_values)
    text_delta = before_mean - after_mean
    if text_delta < 8:
        return _faded_text_noop(
            image,
            "faded text enhancement skipped: readability delta below conservative threshold",
            candidate_ratio,
        )
    if text_delta > 26:
        return _faded_text_noop(
            image,
            "faded text enhancement skipped: readability delta exceeds conservative threshold",
            candidate_ratio,
        )
    return FadedTextEnhancementResult(
        result_image,
        True,
        "faded text enhancement applied: stable low-contrast neutral text on light paper",
        round(text_delta, 6),
        round(selected_ratio, 6),
        round(candidate_ratio, 6),
    )


def _faded_text_noop(
    image: Image.Image,
    reason: str,
    candidate_pixel_ratio: float = 0.0,
) -> FadedTextEnhancementResult:
    return FadedTextEnhancementResult(image, False, reason, 0.0, 0.0, round(candidate_pixel_ratio, 6))


def _faded_text_sample_candidate_ratio(grayscale: Image.Image, threshold: float, p95: int) -> float:
    sample = grayscale.copy()
    sample.thumbnail((96, 96), Image.Resampling.BILINEAR)
    candidate = sample.point(
        lambda value: 255 if 95 <= value <= threshold and 14 <= p95 - value <= 76 else 0,
        mode="L",
    )
    candidate = _clear_mask_edges(candidate, max(2, int(round(min(sample.width, sample.height) * 0.025))))
    return round(_mask_ratio(candidate), 6)


def _sharpen_text_edges_conservative(image: Image.Image) -> TextEdgeSharpeningResult:
    if image.width < 80 or image.height < 80:
        return _text_edges_noop(image, "text edge sharpening skipped: image too small")
    color_risk = _tone_color_risk_reason(image)
    if color_risk:
        return _text_edges_noop(image, "text edge sharpening skipped: color content, stamp, or annotation risk")

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p01 = _histogram_percentile(histogram, total, 0.01)
    p05 = _histogram_percentile(histogram, total, 0.05)
    p50 = _histogram_percentile(histogram, total, 0.50)
    p95 = _histogram_percentile(histogram, total, 0.95)
    p99 = _histogram_percentile(histogram, total, 0.99)
    if p95 < 220 or p50 < 205:
        return _text_edges_noop(image, "text edge sharpening skipped: page is not a light paper background")
    if p99 - p01 < 45:
        return _text_edges_noop(image, "text edge sharpening skipped: text edge evidence too weak")
    if p05 < 35 and _dark_pixel_ratio(grayscale, 64) > 0.09:
        return _text_edges_noop(image, "text edge sharpening skipped: dense dark foreground or illustration risk")
    if _source_protected_edge_dark_ratio(grayscale) > 0.002:
        return _text_edges_noop(image, "text edge sharpening skipped: edge mark or binding risk")

    sample_candidate_ratio = _text_edge_sample_candidate_ratio(grayscale)
    if sample_candidate_ratio < 0.02:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: cheap candidate preflight found too little blurred text edge evidence",
            sample_candidate_ratio,
            preflight_skipped=True,
        )

    candidate = _text_edge_candidate_mask(grayscale, p95)
    candidate = _clear_mask_edges(candidate, max(3, int(round(min(image.width, image.height) * 0.025))))
    candidate_ratio = _mask_ratio(candidate)
    if candidate_ratio < 0.0015:
        return _text_edges_noop(image, "text edge sharpening skipped: blurred text edge evidence too sparse", candidate_ratio)
    if candidate_ratio > 0.12:
        return _text_edges_noop(image, "text edge sharpening skipped: edge candidates too dense", candidate_ratio)
    if _protected_edge_dark_ratio(candidate) > 0.002:
        return _text_edges_noop(image, "text edge sharpening skipped: edge mark or binding risk", candidate_ratio)

    components = _mask_components(candidate)
    if len(components) > 120 and max((len(component) for component in components), default=0) < 24:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: fine texture or photo-detail risk",
            candidate_ratio,
        )
    selected: set[tuple[int, int]] = set()
    text_like_components = 0
    rejected_large_components = 0
    for component in components:
        area = len(component)
        if area < 2:
            continue
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        width = max(xs) - min(xs) + 1
        height = max(ys) - min(ys) + 1
        line_like = height <= 10 and width <= image.width * 0.72
        if area / total > 0.018 or height > image.height * 0.20 or (not line_like and width > image.width * 0.36):
            rejected_large_components += 1
            continue
        fill_ratio = area / max(1, width * height)
        aspect = max(width / max(1, height), height / max(1, width))
        if fill_ratio > 0.90 and area > 36 and not line_like:
            rejected_large_components += 1
            continue
        if width >= 2 and height >= 1 and aspect <= 80:
            text_like_components += 1
            selected.update(component)
    selected_ratio = len(selected) / max(1, total)
    if rejected_large_components:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: broad texture, illustration, or table-region risk",
            candidate_ratio,
        )
    if text_like_components < 3 or selected_ratio < 0.0015:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: stable text edge evidence insufficient",
            candidate_ratio,
        )
    if selected_ratio > 0.08:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: changed area exceeds conservative text edge scope",
            candidate_ratio,
        )

    sharpened = grayscale.filter(ImageFilter.UnsharpMask(radius=1.0, percent=90, threshold=2))
    source_pixels = grayscale.load()
    sharp_pixels = sharpened.load()
    changed = 0
    deltas: list[int] = []
    if image.mode == "L":
        output = grayscale.copy()
        output_pixels = output.load()
        for x, y in selected:
            delta = int(sharp_pixels[x, y]) - int(source_pixels[x, y])
            if 3 <= abs(delta) <= 42:
                output_pixels[x, y] = max(0, min(255, int(source_pixels[x, y]) + delta))
                changed += 1
                deltas.append(abs(delta))
        result_image = output
    else:
        source = image.convert("RGB")
        output = source.copy()
        output_pixels = output.load()
        for x, y in selected:
            delta = int(sharp_pixels[x, y]) - int(source_pixels[x, y])
            if 3 <= abs(delta) <= 42:
                red_value, green_value, blue_value = output_pixels[x, y]
                output_pixels[x, y] = (
                    max(0, min(255, red_value + delta)),
                    max(0, min(255, green_value + delta)),
                    max(0, min(255, blue_value + delta)),
                )
                changed += 1
                deltas.append(abs(delta))
        result_image = output

    changed_ratio = changed / max(1, total)
    if changed_ratio < 0.001:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: sharpening delta below conservative threshold",
            candidate_ratio,
        )
    if changed_ratio > 0.08:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: changed area exceeds conservative threshold",
            candidate_ratio,
        )
    edge_delta = sum(deltas) / max(1, len(deltas))
    if edge_delta < 3 or edge_delta > 24:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: edge delta outside conservative threshold",
            candidate_ratio,
        )
    brightness_delta, contrast_delta = _tonal_deltas(grayscale, result_image.convert("L"))
    if brightness_delta > 4.0 or contrast_delta > 8.0:
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: brightness or contrast delta exceeds conservative threshold",
            candidate_ratio,
        )

    return TextEdgeSharpeningResult(
        result_image,
        True,
        "text edge sharpening applied: stable neutral blurred text edges on light paper",
        round(edge_delta, 6),
        round(changed_ratio, 6),
        round(candidate_ratio, 6),
    )


def _text_edge_sample_candidate_ratio(grayscale: Image.Image) -> float:
    sample = grayscale.copy()
    sample.thumbnail((96, 96), Image.Resampling.BILINEAR)
    if sample.width < 30 or sample.height < 30:
        return 0.0
    histogram = sample.histogram()
    total = sample.width * sample.height
    p95 = _histogram_percentile(histogram, total, 0.95)
    candidate = _text_edge_candidate_mask(sample, p95)
    candidate = _clear_mask_edges(candidate, max(2, int(round(min(sample.width, sample.height) * 0.025))))
    return round(_mask_ratio(candidate), 6)


def _text_edge_candidate_mask(grayscale: Image.Image, p95: int) -> Image.Image:
    width, height = grayscale.size
    edges = grayscale.filter(ImageFilter.FIND_EDGES)
    source_pixels = grayscale.load()
    edge_pixels = edges.load()
    output = Image.new("L", grayscale.size, 0)
    output_pixels = output.load()
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            value = int(source_pixels[x, y])
            if not (45 <= value <= min(224, p95 - 8)):
                continue
            if int(edge_pixels[x, y]) < 14:
                continue
            local_min = 255
            local_max = 0
            for ny in range(y - 1, y + 2):
                for nx in range(x - 1, x + 2):
                    neighbor = int(source_pixels[nx, ny])
                    local_min = min(local_min, neighbor)
                    local_max = max(local_max, neighbor)
            if local_min <= 190 and local_max >= 210 and local_max - local_min >= 24:
                output_pixels[x, y] = 255
    return output


def _text_edges_noop(
    image: Image.Image,
    reason: str,
    candidate_pixel_ratio: float = 0.0,
    *,
    preflight_skipped: bool = False,
) -> TextEdgeSharpeningResult:
    return TextEdgeSharpeningResult(image, False, reason, 0.0, 0.0, round(candidate_pixel_ratio, 6), preflight_skipped)


def _mask_ratio(mask: Image.Image) -> float:
    histogram = mask.histogram()
    return sum(histogram[1:]) / max(1, mask.width * mask.height)


def _protected_edge_dark_ratio(mask: Image.Image) -> float:
    margin = max(5, int(round(min(mask.width, mask.height) * 0.06)))
    boxes = (
        (0, 0, margin, mask.height),
        (mask.width - margin, 0, mask.width, mask.height),
        (0, 0, mask.width, margin),
        (0, mask.height - margin, mask.width, mask.height),
    )
    edge_pixels = sum((box[2] - box[0]) * (box[3] - box[1]) for box in boxes)
    dark_pixels = sum(sum(mask.crop(box).histogram()[1:]) for box in boxes)
    return dark_pixels / max(1, edge_pixels)


def _source_protected_edge_dark_ratio(grayscale: Image.Image) -> float:
    margin = max(5, int(round(min(grayscale.width, grayscale.height) * 0.06)))
    boxes = (
        (0, 0, margin, grayscale.height),
        (grayscale.width - margin, 0, grayscale.width, grayscale.height),
        (0, 0, grayscale.width, margin),
        (0, grayscale.height - margin, grayscale.width, grayscale.height),
    )
    edge_pixels = sum((box[2] - box[0]) * (box[3] - box[1]) for box in boxes)
    dark_pixels = 0
    for box in boxes:
        histogram = grayscale.crop(box).histogram()
        dark_pixels += sum(histogram[:96])
    return dark_pixels / max(1, edge_pixels)


def _clear_mask_edges(mask: Image.Image, margin: int) -> Image.Image:
    if margin <= 0:
        return mask
    output = mask.copy()
    pixels = output.load()
    for y in range(output.height):
        for x in range(output.width):
            if x < margin or y < margin or x >= output.width - margin or y >= output.height - margin:
                pixels[x, y] = 0
    return output


def _mask_components(mask: Image.Image) -> list[list[tuple[int, int]]]:
    width, height = mask.size
    pixels = mask.load()
    visited: set[tuple[int, int]] = set()
    components: list[list[tuple[int, int]]] = []
    for y in range(height):
        for x in range(width):
            if not pixels[x, y] or (x, y) in visited:
                continue
            stack = [(x, y)]
            visited.add((x, y))
            component: list[tuple[int, int]] = []
            while stack:
                point = stack.pop()
                component.append(point)
                px, py = point
                for nx in range(max(0, px - 1), min(width, px + 2)):
                    for ny in range(max(0, py - 1), min(height, py + 2)):
                        neighbor = (nx, ny)
                        if neighbor in visited or not pixels[nx, ny]:
                            continue
                        visited.add(neighbor)
                        stack.append(neighbor)
            components.append(component)
    return components


def _dark_pixel_ratio(image: Image.Image, threshold: int) -> float:
    histogram = image.histogram()
    return sum(histogram[:threshold]) / max(1, image.width * image.height)


def _tone_color_risk_reason(image: Image.Image) -> str | None:
    if image.mode == "L":
        return None
    sample = image.convert("RGB")
    sample.thumbnail((600, 600), Image.Resampling.BILINEAR)
    total = max(1, sample.width * sample.height)
    colored = 0
    red = 0
    light_colored = 0
    pixel_data = sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata()
    for red_value, green_value, blue_value in pixel_data:
        high = max(red_value, green_value, blue_value)
        low = min(red_value, green_value, blue_value)
        spread = high - low
        brightness = (red_value + green_value + blue_value) / 3
        if spread > 18 and 30 < brightness < 250:
            colored += 1
        if red_value >= 110 and red_value - green_value >= 35 and red_value - blue_value >= 35:
            red += 1
        if spread > 12 and 125 <= brightness <= 245:
            light_colored += 1
    red_ratio = red / total
    if red_ratio >= 0.0004:
        return "tone normalization skipped: red stamp or red annotation risk"
    colored_ratio = colored / total
    light_colored_ratio = light_colored / total
    if light_colored_ratio >= 0.0008 and colored_ratio < 0.02:
        return "tone normalization skipped: light color annotation or faint mark risk"
    if colored_ratio >= 0.003:
        return "tone normalization skipped: obvious color content"
    return None


def _processing_audit(
    source: Image.Image,
    processed: Image.Image,
    options: ProcessingOptions,
    crop_bbox: tuple[int, int, int, int] | None,
    dark_border_bbox: tuple[int, int, int, int] | None,
    skew_angle_degrees: float | None,
    despeckle_pixels_changed: int,
    tone_normalized: bool = False,
    tone_background_before: float | None = None,
    tone_background_after: float | None = None,
    tone_contrast_before: float | None = None,
    tone_contrast_after: float | None = None,
    tone_changed_pixel_ratio: float = 0.0,
    edge_shadow_lightened: bool = False,
    edge_shadow_delta: float = 0.0,
    edge_shadow_changed_pixel_ratio: float = 0.0,
    edge_shadow_candidate_pixel_ratio: float = 0.0,
    background_stains_lightened: bool = False,
    background_stains_delta: float = 0.0,
    background_stains_changed_pixel_ratio: float = 0.0,
    background_stains_candidate_pixel_ratio: float = 0.0,
    scanlines_lightened: bool = False,
    scanlines_delta: float = 0.0,
    scanlines_changed_pixel_ratio: float = 0.0,
    scanlines_candidate_pixel_ratio: float = 0.0,
    faded_text_enhanced: bool = False,
    faded_text_delta: float = 0.0,
    faded_text_changed_pixel_ratio: float = 0.0,
    faded_text_candidate_pixel_ratio: float = 0.0,
    text_edges_sharpened: bool = False,
    text_edges_delta: float = 0.0,
    text_edges_changed_pixel_ratio: float = 0.0,
    text_edges_candidate_pixel_ratio: float = 0.0,
    cumulative_change_guard: dict[str, Any] | None = None,
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
    geometric_change_recorded = (
        size_change_ratio > 0
        or crop_ratio > 0
        or max_trim_margin_ratio > 0
        or deskew_abs_angle >= 0.2
    )
    tone_background_delta = (
        abs(tone_background_after - tone_background_before)
        if isinstance(tone_background_before, int | float) and isinstance(tone_background_after, int | float)
        else 0.0
    )
    tone_contrast_delta = (
        abs(tone_contrast_after - tone_contrast_before)
        if isinstance(tone_contrast_before, int | float) and isinstance(tone_contrast_after, int | float)
        else 0.0
    )
    metrics = {
        "size_change_ratio": round(size_change_ratio, 6),
        "pixel_change_ratio": round(pixel_change_ratio, 6),
        "pixel_change_guardrail_applied": not geometric_change_recorded and not tone_normalized,
        "pixel_change_guardrail_scope": (
            "same_size_pixel_change"
            if not geometric_change_recorded and not tone_normalized
            else (
                "tone_normalization_recorded_by_brightness_and_contrast"
                if tone_normalized and not geometric_change_recorded
                else "geometric_change_recorded_by_size_crop_trim_or_deskew"
            )
        ),
        "brightness_delta": round(brightness_delta, 6),
        "contrast_delta": round(contrast_delta, 6),
        "tone_normalized": tone_normalized,
        "tone_background_delta": round(tone_background_delta, 6),
        "tone_contrast_delta": round(tone_contrast_delta, 6),
        "tone_changed_pixel_ratio": round(tone_changed_pixel_ratio, 6),
        "edge_shadow_lightened": edge_shadow_lightened,
        "edge_shadow_delta": round(edge_shadow_delta, 6),
        "edge_shadow_changed_pixel_ratio": round(edge_shadow_changed_pixel_ratio, 6),
        "edge_shadow_candidate_pixel_ratio": round(edge_shadow_candidate_pixel_ratio, 6),
        "background_stains_lightened": background_stains_lightened,
        "background_stains_delta": round(background_stains_delta, 6),
        "background_stains_changed_pixel_ratio": round(background_stains_changed_pixel_ratio, 6),
        "background_stains_candidate_pixel_ratio": round(background_stains_candidate_pixel_ratio, 6),
        "scanlines_lightened": scanlines_lightened,
        "scanlines_delta": round(scanlines_delta, 6),
        "scanlines_changed_pixel_ratio": round(scanlines_changed_pixel_ratio, 6),
        "scanlines_candidate_pixel_ratio": round(scanlines_candidate_pixel_ratio, 6),
        "faded_text_enhanced": faded_text_enhanced,
        "faded_text_delta": round(faded_text_delta, 6),
        "faded_text_changed_pixel_ratio": round(faded_text_changed_pixel_ratio, 6),
        "faded_text_candidate_pixel_ratio": round(faded_text_candidate_pixel_ratio, 6),
        "text_edges_sharpened": text_edges_sharpened,
        "text_edges_delta": round(text_edges_delta, 6),
        "text_edges_changed_pixel_ratio": round(text_edges_changed_pixel_ratio, 6),
        "text_edges_candidate_pixel_ratio": round(text_edges_candidate_pixel_ratio, 6),
        "crop_ratio": round(max(0.0, crop_ratio), 6),
        "trim_margins": trim_margins,
        "max_trim_margin_ratio": round(max_trim_margin_ratio, 6),
        "deskew_abs_angle_degrees": deskew_abs_angle,
        "despeckle_pixel_ratio": round(despeckle_pixel_ratio, 6),
    }
    if cumulative_change_guard is None:
        cumulative_change_guard = _cumulative_change_guard(metrics, options)
    metrics.update(_cumulative_change_guard_audit_fields(cumulative_change_guard))
    failures = _audit_guardrail_failures(metrics, options)
    return {**metrics, "guardrail_failures": failures}


def _cumulative_change_guard(metrics: dict[str, Any], options: ProcessingOptions) -> dict[str, Any]:
    geometric_scope = metrics.get("pixel_change_guardrail_scope") == "geometric_change_recorded_by_size_crop_trim_or_deskew"
    pixel_ratio = (
        _float_metric(metrics, "pixel_change_ratio")
        if metrics.get("pixel_change_guardrail_applied") is True
        else 0.0
    )
    brightness_delta = 0.0 if geometric_scope else _float_metric(metrics, "brightness_delta")
    contrast_delta = 0.0 if geometric_scope else _float_metric(metrics, "contrast_delta")
    crop_ratio = max(_float_metric(metrics, "crop_ratio"), _float_metric(metrics, "max_trim_margin_ratio"))
    candidate_ratio = max(
        _float_metric(metrics, "background_stains_changed_pixel_ratio"),
        _float_metric(metrics, "background_stains_candidate_pixel_ratio"),
        _float_metric(metrics, "scanlines_changed_pixel_ratio"),
        _float_metric(metrics, "scanlines_candidate_pixel_ratio"),
        _float_metric(metrics, "faded_text_changed_pixel_ratio"),
        _float_metric(metrics, "faded_text_candidate_pixel_ratio"),
        _float_metric(metrics, "text_edges_changed_pixel_ratio"),
        _float_metric(metrics, "text_edges_candidate_pixel_ratio"),
    )
    score_components = {
        "pixel_change_ratio": _safe_ratio(pixel_ratio, options.audit_max_cumulative_pixel_change_ratio),
        "brightness_delta": _safe_ratio(brightness_delta, options.audit_max_cumulative_brightness_delta),
        "contrast_delta": _safe_ratio(contrast_delta, options.audit_max_cumulative_contrast_delta),
        "crop_ratio": _safe_ratio(crop_ratio, options.audit_max_cumulative_crop_ratio),
        "candidate_pixel_ratio": _safe_ratio(candidate_ratio, options.audit_max_cumulative_candidate_pixel_ratio),
    }
    score = round(max(score_components.values()), 6)
    reasons = [
        reason
        for reason, value in score_components.items()
        if value > 1.0
    ]
    if score > options.audit_max_cumulative_change_score:
        reasons.append("cumulative_change_score")

    existing_failures = _audit_guardrail_failures(metrics, options)
    hard_failures = {
        failure
        for failure in existing_failures
        if failure not in {"pixel_change_ratio", "brightness_delta", "contrast_delta"}
    }
    action = "passed"
    if hard_failures:
        action = "deferred_to_existing_guardrail"
        reasons = []
    elif reasons:
        action = "reverted_to_source"

    return {
        "checked": True,
        "action": action,
        "reverted": action == "reverted_to_source",
        "reasons": sorted(set(reasons)),
        "score": score,
        "pixel_ratio": round(pixel_ratio, 6),
        "brightness_delta": round(brightness_delta, 6),
        "contrast_delta": round(contrast_delta, 6),
        "crop_ratio": round(crop_ratio, 6),
        "candidate_pixel_ratio": round(candidate_ratio, 6),
    }


def _cumulative_change_guard_audit_fields(guard: dict[str, Any]) -> dict[str, Any]:
    return {
        "cumulative_change_guard_checked": guard.get("checked") is True,
        "cumulative_change_guard_action": guard.get("action", "passed"),
        "cumulative_change_guard_reverted": guard.get("reverted") is True,
        "cumulative_change_guard_reasons": guard.get("reasons") if isinstance(guard.get("reasons"), list) else [],
        "cumulative_change_score": _round_guard_metric(guard.get("score")),
        "cumulative_change_pixel_ratio": _round_guard_metric(guard.get("pixel_ratio")),
        "cumulative_change_brightness_delta": _round_guard_metric(guard.get("brightness_delta")),
        "cumulative_change_contrast_delta": _round_guard_metric(guard.get("contrast_delta")),
        "cumulative_change_crop_ratio": _round_guard_metric(guard.get("crop_ratio")),
        "cumulative_change_candidate_pixel_ratio": _round_guard_metric(guard.get("candidate_pixel_ratio")),
    }


def _float_metric(metrics: dict[str, Any], key: str) -> float:
    value = metrics.get(key)
    return float(value) if isinstance(value, int | float) else 0.0


def _safe_ratio(value: float, threshold: float) -> float:
    return value / threshold if threshold > 0 else 0.0


def _round_guard_metric(value: Any) -> float:
    return round(float(value), 6) if isinstance(value, int | float) else 0.0


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


def _safe_deskew_skip_from_scan_record(
    scan_record: dict[str, Any] | None,
    image: Image.Image,
    options: ProcessingOptions,
) -> SkewDetection | None:
    if not options.deskew:
        return None
    reusable = _scan_measurements_for_processing(scan_record, image)
    skew = reusable.get("skew")
    if not isinstance(skew, SkewDetection):
        return None
    if skew.angle_degrees is None:
        return skew if skew.reason in _SAFE_DESKEW_NO_CANDIDATE_REASONS else None
    if skew.confidence >= options.deskew_min_confidence and abs(skew.angle_degrees) < 0.2:
        return skew
    return None


_SAFE_DESKEW_NO_CANDIDATE_REASONS = {
    "image too small",
    "low contrast",
    "blank page",
    "insufficient foreground",
    "foreground too dense",
}


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
        ("faded_text_changed_pixel_ratio", 0.10),
        ("faded_text_candidate_pixel_ratio", 0.18),
        ("text_edges_changed_pixel_ratio", 0.08),
        ("text_edges_candidate_pixel_ratio", 0.12),
    ]
    failures = []
    for key, threshold in checks:
        if key == "pixel_change_ratio" and metrics.get("pixel_change_guardrail_applied") is False:
            continue
        if key.startswith("faded_text_") and metrics.get("faded_text_enhanced") is not True:
            continue
        if key.startswith("text_edges_") and metrics.get("text_edges_sharpened") is not True:
            continue
        value = metrics.get(key)
        if isinstance(value, int | float) and value > threshold:
            failures.append(key)
    return failures


def _detect_skew(image: Image.Image) -> SkewDetection:
    width, height = image.size
    if width < 30 or height < 30:
        return SkewDetection(None, 0.0, "image too small")

    raw_grayscale = image.convert("L")
    grayscale = ImageOps.autocontrast(raw_grayscale, cutoff=1)
    histogram = grayscale.histogram()
    total_pixels = width * height
    low = _histogram_percentile(histogram, total_pixels, 0.05)
    high = _histogram_percentile(histogram, total_pixels, 0.95)
    if high - low < 35:
        raw_histogram = raw_grayscale.histogram()
        raw_low = _histogram_percentile(raw_histogram, total_pixels, 0.005)
        raw_high = _histogram_percentile(raw_histogram, total_pixels, 0.995)
        if raw_high - raw_low < 35:
            return SkewDetection(None, 0.0, "low contrast")
        sparse_threshold = max(0, raw_high - 35)
        sparse_foreground = sum(raw_histogram[: sparse_threshold + 1]) / total_pixels
        if sparse_foreground < 0.002:
            return SkewDetection(None, 0.0, "low contrast")
        threshold = sparse_threshold
        threshold_source = raw_grayscale
    else:
        threshold = max(0, min(255, low + int((high - low) * 0.35)))
        threshold_source = grayscale
    ink = threshold_source.point(lambda value: 255 if value <= threshold else 0, mode="L")
    bbox = ink.getbbox()
    if not bbox:
        return SkewDetection(None, 0.0, "blank page")

    ink_ratio = _nonzero_ratio(ink, bbox)
    if ink_ratio < 0.002:
        return SkewDetection(None, 0.0, "insufficient foreground")
    if ink_ratio > 0.65:
        return SkewDetection(None, 0.0, "foreground too dense")
    if not _has_deskew_line_evidence(ink, bbox):
        return SkewDetection(None, 0.0, "low confidence")

    sample = ink.crop(bbox)
    sample.thumbnail((700, 700), Image.Resampling.BILINEAR)
    scores = _deskew_candidate_scores(sample)
    best_angle, best_score = max(scores.items(), key=lambda item: item[1])
    runner_up = max(score for angle, score in scores.items() if abs(angle - best_angle) >= 1.0)
    confidence = 0.0 if best_score <= 0 else max(0.0, min(1.0, (best_score - runner_up) / best_score))
    skew_angle = round(-best_angle, 2)
    return SkewDetection(skew_angle, round(confidence, 3), "skew detected")


def _deskew_has_edge_content_risk(image: Image.Image) -> bool:
    width, height = image.size
    if width < 30 or height < 30:
        return False

    grayscale = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    histogram = grayscale.histogram()
    total_pixels = width * height
    low = _histogram_percentile(histogram, total_pixels, 0.05)
    high = _histogram_percentile(histogram, total_pixels, 0.95)
    if high - low < 35:
        return False

    threshold = max(0, min(255, low + int((high - low) * 0.35)))
    ink = grayscale.point(lambda value: 255 if value <= threshold else 0, mode="L")
    bbox = ink.getbbox()
    if not bbox:
        return False

    left, top, right, bottom = bbox
    horizontal_margin = max(2, int(round(width * 0.035)))
    vertical_margin = max(2, int(round(height * 0.035)))
    return (
        left <= horizontal_margin
        or top <= vertical_margin
        or width - right <= horizontal_margin
        or height - bottom <= vertical_margin
    )


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


def _has_deskew_line_evidence(image: Image.Image, bbox: tuple[int, int, int, int]) -> bool:
    sample = image.crop(bbox)
    width, height = sample.size
    if width <= 0 or height <= 0:
        return False
    horizontal_min_run = max(8, int(round(width * 0.08)))
    vertical_min_run = max(8, int(round(height * 0.08)))
    pixels = sample.load()

    for y in range(height):
        run = 0
        for x in range(width):
            run = run + 1 if pixels[x, y] else 0
            if run >= horizontal_min_run:
                return True

    for x in range(width):
        run = 0
        for y in range(height):
            run = run + 1 if pixels[x, y] else 0
            if run >= vertical_min_run:
                return True
    return False


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
            scores[normalized_angle] = _deskew_projection_score(rotated)
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


def _deskew_projection_score(image: Image.Image) -> float:
    return _horizontal_projection_variance(image) + _vertical_projection_variance(image)


def _horizontal_projection_variance(image: Image.Image) -> float:
    width, height = image.size
    projection = image.resize((1, height), Image.Resampling.BOX)
    row_counts = [value * width / 255 for value in projection.tobytes()]
    mean = sum(row_counts) / len(row_counts)
    return sum((count - mean) ** 2 for count in row_counts) / len(row_counts)


def _vertical_projection_variance(image: Image.Image) -> float:
    width, height = image.size
    projection = image.resize((width, 1), Image.Resampling.BOX)
    column_counts = [value * height / 255 for value in projection.tobytes()]
    mean = sum(column_counts) / len(column_counts)
    return sum((count - mean) ** 2 for count in column_counts) / len(column_counts)


def _rotate_for_deskew(image: Image.Image, correction_angle: float) -> Image.Image:
    fill = _corner_background_value(image.convert("L"))
    fillcolor: int | tuple[int, int, int]
    if image.mode == "RGB":
        fillcolor = (fill, fill, fill)
    else:
        fillcolor = fill
    return image.rotate(correction_angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=fillcolor)


def _detect_post_deskew_canvas_crop_bbox(image: Image.Image) -> CropDetection:
    width, height = image.size
    if width < 40 or height < 40:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")

    grayscale = image.convert("L")
    canvas = float(_corner_background_value(grayscale))
    page_background = _post_deskew_inner_light_background(grayscale)
    if page_background < 135 or abs(page_background - canvas) < 3.0:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")

    max_x = min(width // 5, max(4, int(width * 0.14)))
    max_y = min(height // 5, max(4, int(height * 0.14)))
    threshold = max(3.0, min(7.0, abs(page_background - canvas) * 0.25))
    left = _first_post_deskew_canvas_edge(grayscale, canvas, "left", max_x, threshold)
    right_margin = _first_post_deskew_canvas_edge(grayscale, canvas, "right", max_x, threshold)
    top = _first_post_deskew_canvas_edge(grayscale, canvas, "top", max_y, threshold)
    bottom_margin = _first_post_deskew_canvas_edge(grayscale, canvas, "bottom", max_y, threshold)
    if None in {left, right_margin, top, bottom_margin}:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")
    assert left is not None and right_margin is not None and top is not None and bottom_margin is not None

    right = width - right_margin
    bottom = height - bottom_margin
    if right <= left or bottom <= top:
        return CropDetection(None, "post-deskew crop skipped: crop risk too large")

    margins = (left, top, width - right, height - bottom)
    if min(margins) < 2:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")
    max_trim_ratio = max(left / width, (width - right) / width, top / height, (height - bottom) / height)
    if max_trim_ratio > 0.14:
        return CropDetection(None, "post-deskew crop skipped: crop risk too large")

    crop_area_ratio = ((right - left) * (bottom - top)) / max(1, width * height)
    if crop_area_ratio < 0.70:
        return CropDetection(None, "post-deskew crop skipped: crop risk too large")
    if crop_area_ratio > 0.985:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")

    bbox = (left, top, right, bottom)
    if not _post_deskew_crop_has_page_boundary_evidence(grayscale, bbox, canvas, threshold):
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")
    if _has_protected_dark_content_near_trim_boundary(grayscale, bbox):
        return CropDetection(None, "post-deskew crop skipped: edge content protection")
    return CropDetection(bbox, "post-deskew safe canvas crop applied")


def _first_post_deskew_canvas_edge(
    image: Image.Image,
    canvas: float,
    side: str,
    max_margin: int,
    threshold: float,
) -> int | None:
    width, height = image.size
    band = 2
    required_run = 2
    run_start: int | None = None
    run_length = 0
    for offset in range(1, max_margin + 1):
        if side == "left":
            box = (offset, height // 5, min(width, offset + band), height - height // 5)
        elif side == "right":
            box = (max(0, width - offset - band), height // 5, width - offset, height - height // 5)
        elif side == "top":
            box = (width // 5, offset, width - width // 5, min(height, offset + band))
        else:
            box = (width // 5, max(0, height - offset - band), width - width // 5, height - offset)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        delta = abs(ImageStat.Stat(image.crop(box)).mean[0] - canvas)
        if delta >= threshold:
            if run_start is None:
                run_start = offset
            run_length += 1
            if run_length >= required_run:
                return run_start
        else:
            run_start = None
            run_length = 0
    return None


def _post_deskew_inner_light_background(image: Image.Image) -> float:
    width, height = image.size
    margin_x = max(1, int(width * 0.20))
    margin_y = max(1, int(height * 0.20))
    if margin_x * 2 >= width or margin_y * 2 >= height:
        sample = image
    else:
        sample = image.crop((margin_x, margin_y, width - margin_x, height - margin_y))
    histogram = sample.histogram()
    return float(_histogram_percentile(histogram, sample.width * sample.height, 0.82))


def _post_deskew_crop_has_page_boundary_evidence(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    canvas: float,
    threshold: float,
) -> bool:
    left, top, right, bottom = bbox
    crop_width = right - left
    crop_height = bottom - top
    band = max(2, int(min(crop_width, crop_height) * 0.015))
    boundaries = (
        image.crop((left, top, min(right, left + band), bottom)),
        image.crop((max(left, right - band), top, right, bottom)),
        image.crop((left, top, right, min(bottom, top + band))),
        image.crop((left, max(top, bottom - band), right, bottom)),
    )
    return all(abs(ImageStat.Stat(boundary).mean[0] - canvas) >= threshold for boundary in boundaries)


def _detect_conservative_crop_bbox(image: Image.Image) -> CropDetection:
    width, height = image.size
    if width < 20 or height < 20:
        return CropDetection(None, "image too small")

    grayscale = image.convert("L")
    background = _corner_background_value(grayscale)
    diff = grayscale.point(lambda value: 255 if abs(value - background) >= 18 else 0)
    strong_bbox = diff.getbbox()
    strong_result: CropDetection | None = None
    if strong_bbox:
        strong_result = _conservative_crop_candidate_from_bbox(diff, strong_bbox, image.size)
        if strong_result.bbox or strong_result.reason == "foreground reaches crop safety margin":
            return strong_result

    light_bbox = _detect_light_outer_margin_bbox(grayscale, background)
    if not light_bbox:
        return strong_result or CropDetection(None, "no confident foreground outside background")

    bbox = light_bbox
    left, top, right, bottom = bbox
    if not _light_crop_has_safe_inner_evidence(grayscale, diff, light_bbox, background):
        return strong_result or CropDetection(None, "low-confidence subtle page edge evidence")

    if not _crop_boundary_has_consistent_light_evidence(grayscale, light_bbox, background):
        return strong_result or CropDetection(None, "crop boundary evidence is too sparse")

    return CropDetection((left, top, right, bottom), "conservative crop applied")


def _conservative_crop_candidate_from_bbox(
    diff: Image.Image,
    bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> CropDetection:
    width, height = image_size
    left, top, right, bottom = bbox
    margins = (left, top, width - right, height - bottom)
    if min(margins) < 2:
        return CropDetection(None, "foreground reaches crop safety margin")
    if max(margins) > max(min(margins) * 3, min(margins) + max(4, int(min(width, height) * 0.08))):
        return CropDetection(None, "inconsistent crop margin evidence")

    crop_width = right - left
    crop_height = bottom - top
    crop_area_ratio = (crop_width * crop_height) / (width * height)
    if crop_area_ratio < 0.45:
        return CropDetection(None, "candidate crop exceeds conservative crop ratio")
    if crop_area_ratio > 0.98:
        return CropDetection(None, "candidate crop change is too small")

    if not _crop_boundary_has_consistent_evidence(diff, bbox):
        return CropDetection(None, "crop boundary evidence is too sparse")

    return CropDetection(bbox, "conservative crop applied")


def _detect_light_outer_margin_bbox(image: Image.Image, background: float) -> tuple[int, int, int, int] | None:
    width, height = image.size
    max_x = min(width // 4, max(4, int(width * 0.18)))
    max_y = min(height // 4, max(4, int(height * 0.18)))
    threshold = 5.0
    left = _first_consistent_light_edge(image, background, "left", max_x, threshold)
    right_margin = _first_consistent_light_edge(image, background, "right", max_x, threshold)
    top = _first_consistent_light_edge(image, background, "top", max_y, threshold)
    bottom_margin = _first_consistent_light_edge(image, background, "bottom", max_y, threshold)
    if None in {left, right_margin, top, bottom_margin}:
        return None
    assert left is not None and right_margin is not None and top is not None and bottom_margin is not None
    right = width - right_margin
    bottom = height - bottom_margin
    if right <= left or bottom <= top:
        return None
    margins = (left, top, width - right, height - bottom)
    if min(margins) < 2:
        return None
    if max(margins) > max(min(margins) * 3, min(margins) + max(4, int(min(width, height) * 0.08))):
        return None
    crop_area_ratio = ((right - left) * (bottom - top)) / max(1, width * height)
    if crop_area_ratio < 0.45 or crop_area_ratio > 0.98:
        return None
    return (left, top, right, bottom)


def _first_consistent_light_edge(
    image: Image.Image,
    background: float,
    side: str,
    max_margin: int,
    threshold: float,
) -> int | None:
    width, height = image.size
    band = 2
    required_run = 3
    run_start: int | None = None
    run_length = 0
    for offset in range(1, max_margin + 1):
        if side == "left":
            box = (offset, 0, min(width, offset + band), height)
        elif side == "right":
            box = (max(0, width - offset - band), 0, width - offset, height)
        elif side == "top":
            box = (0, offset, width, min(height, offset + band))
        else:
            box = (0, max(0, height - offset - band), width, height - offset)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        delta = abs(ImageStat.Stat(image.crop(box)).mean[0] - background)
        if delta >= threshold:
            if run_start is None:
                run_start = offset
            run_length += 1
            if run_length >= required_run:
                return run_start
        else:
            run_start = None
            run_length = 0
    return None


def _light_crop_has_safe_inner_evidence(
    grayscale: Image.Image,
    strong_diff: Image.Image,
    bbox: tuple[int, int, int, int],
    background: float,
) -> bool:
    left, top, right, bottom = bbox
    crop_width = right - left
    crop_height = bottom - top
    if crop_width < 10 or crop_height < 10:
        return False
    strong_bbox = strong_diff.getbbox()
    if not strong_bbox:
        return False
    strong_left, strong_top, strong_right, strong_bottom = strong_bbox
    strong_margins = (
        strong_left - left,
        strong_top - top,
        right - strong_right,
        bottom - strong_bottom,
    )
    boundary_band = max(2, int(min(crop_width, crop_height) * 0.025))
    strong_touches_boundary = min(strong_margins) <= boundary_band
    if strong_touches_boundary and not _crop_boundary_has_consistent_evidence(strong_diff, bbox):
        return False
    if _light_page_background_mean(grayscale.crop(bbox)) < 135:
        return False
    inner = grayscale.crop(
        (
            left + max(2, crop_width // 10),
            top + max(2, crop_height // 10),
            right - max(2, crop_width // 10),
            bottom - max(2, crop_height // 10),
        )
    )
    return abs(ImageStat.Stat(inner).mean[0] - background) >= 4.0


def _crop_boundary_has_consistent_light_evidence(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    background: float,
) -> bool:
    left, top, right, bottom = bbox
    crop_width = right - left
    crop_height = bottom - top
    band = max(2, int(min(crop_width, crop_height) * 0.02))
    boundaries = (
        image.crop((left, top, min(right, left + band), bottom)),
        image.crop((max(left, right - band), top, right, bottom)),
        image.crop((left, top, right, min(bottom, top + band))),
        image.crop((left, max(top, bottom - band), right, bottom)),
    )
    return all(abs(ImageStat.Stat(boundary).mean[0] - background) >= 5.0 for boundary in boundaries)


def _crop_boundary_has_consistent_evidence(image: Image.Image, bbox: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = bbox
    crop_width = right - left
    crop_height = bottom - top
    min_boundary_pixels = max(3, int(min(crop_width, crop_height) * 0.08))
    boundaries = (
        image.crop((left, top, left + 1, bottom)),
        image.crop((right - 1, top, right, bottom)),
        image.crop((left, top, right, top + 1)),
        image.crop((left, bottom - 1, right, bottom)),
    )
    for boundary in boundaries:
        if sum(1 for value in boundary.tobytes() if value) < min_boundary_pixels:
            return False

    return True


def _detect_dark_border_bbox(image: Image.Image) -> DarkBorderDetection:
    width, height = image.size
    if width < 40 or height < 40:
        return DarkBorderDetection(None, "image too small")

    grayscale = image.convert("L")
    if _light_page_background_mean(grayscale) < 135:
        return DarkBorderDetection(None, "no light page background for dark border trim")

    max_x = max(2, int(width * 0.08))
    max_y = max(2, int(height * 0.08))
    min_retain_width = int(width * 0.88)
    min_retain_height = int(height * 0.88)
    left = _dark_edge_run(grayscale, "left", max_x)
    right = _dark_edge_run(grayscale, "right", max_x)
    top = _dark_edge_run(grayscale, "top", max_y)
    bottom = _dark_edge_run(grayscale, "bottom", max_y)
    runs = (left, right, top, bottom)

    if max(runs) < 2:
        return DarkBorderDetection(None, "no confident dark edge border")
    if min(runs) < 2:
        return DarkBorderDetection(None, "incomplete dark edge border evidence")
    if max(runs) > max(min(runs) * 3, min(runs) + max(2, int(min(width, height) * 0.04))):
        return DarkBorderDetection(None, "unbalanced dark edge border evidence")

    bbox = (left, top, width - right, height - bottom)
    retained_width = bbox[2] - bbox[0]
    retained_height = bbox[3] - bbox[1]
    if retained_width < min_retain_width or retained_height < min_retain_height:
        return DarkBorderDetection(None, "candidate trim exceeds conservative retain ratio")
    if retained_width <= 0 or retained_height <= 0:
        return DarkBorderDetection(None, "invalid trim candidate")
    if _has_protected_dark_content_near_trim_boundary(grayscale, bbox):
        return DarkBorderDetection(None, "protected edge content near dark border")

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
        dark_ratio = sum(1 for value in values if value <= 70) / len(values)
        mean = sum(values) / len(values)
        if dark_ratio >= 0.70 and mean <= 105:
            run = offset + 1
        else:
            break
    return run


def _light_page_background_mean(image: Image.Image) -> float:
    width, height = image.size
    margin_x = max(1, int(width * 0.20))
    margin_y = max(1, int(height * 0.20))
    if margin_x * 2 >= width or margin_y * 2 >= height:
        return ImageStat.Stat(image).mean[0]
    return ImageStat.Stat(image.crop((margin_x, margin_y, width - margin_x, height - margin_y))).mean[0]


def _has_protected_dark_content_near_trim_boundary(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
) -> bool:
    width, height = image.size
    left, top, right, bottom = bbox
    protect_depth = max(3, min(10, int(min(width, height) * 0.04)))
    corner_pad_x = max(2, int(width * 0.04))
    corner_pad_y = max(2, int(height * 0.04))
    bands = [
        (left, top + corner_pad_y, min(right, left + protect_depth), bottom - corner_pad_y),
        (max(left, right - protect_depth), top + corner_pad_y, right, bottom - corner_pad_y),
        (left + corner_pad_x, top, right - corner_pad_x, min(bottom, top + protect_depth)),
        (left + corner_pad_x, max(top, bottom - protect_depth), right - corner_pad_x, bottom),
    ]
    for band in bands:
        x0, y0, x1, y1 = band
        if x1 <= x0 or y1 <= y0:
            continue
        crop = image.crop(band)
        values = crop.tobytes()
        dark_pixels = sum(1 for value in values if value <= 90)
        area = max(1, len(values))
        if dark_pixels >= 8 and dark_pixels / area >= 0.01:
            return True
    return False


_DESPECKLE_DARK_THRESHOLD = 60
_DESPECKLE_NEAR_DARK_THRESHOLD = 90
_DESPECKLE_MIN_BACKGROUND_MEDIAN = 120
_DESPECKLE_MAX_CANDIDATE_RATIO = 0.02
_DESPECKLE_MAX_CHANGED_RATIO = 0.01
_DESPECKLE_MAX_COMPONENT_PIXELS = 4
_DESPECKLE_MAX_COMPONENT_SPAN = 3
_DESPECKLE_DENSE_PREFILTER_MIN_DARK_PIXELS = 512
_DESPECKLE_DENSE_PREFILTER_MAX_LOW_CONNECTIVITY_RATIO = 0.01
_DESPECKLE_CONTENT_CONTEXT_RADIUS = 8
_DESPECKLE_CONTENT_CONTEXT_MIN_DARK_PIXELS = 6


def _despeckle_isolated_pixels(image: Image.Image, *, backend: str = "fallback") -> tuple[Image.Image, int, str]:
    processed, changed, backend_mode, _reason = _despeckle_isolated_pixels_with_reason(image, backend=backend)
    return processed, changed, backend_mode


def _despeckle_isolated_pixels_with_reason(image: Image.Image, *, backend: str = "fallback") -> tuple[Image.Image, int, str, str]:
    if backend not in {"fallback", "numpy"}:
        raise ValueError("despeckle backend must be fallback or numpy")

    grayscale = image.convert("L")
    width, height = grayscale.size
    if width < 3 or height < 3:
        return image.copy(), 0, "not_applicable", "despeckle not applicable to very small image"

    min_value, _max_value = grayscale.getextrema()
    if min_value > _DESPECKLE_DARK_THRESHOLD:
        return image.copy(), 0, "not_applicable", "no isolated dark pixels found"

    dark_mask = grayscale.point(lambda value: 255 if value <= _DESPECKLE_DARK_THRESHOLD else 0, mode="L")
    candidates, backend_mode = _despeckle_candidate_points_with_backend(dark_mask, backend=backend)
    if not candidates:
        reason = (
            "protected edge dark marks preserved"
            if _despeckle_mask_touches_protected_edge(dark_mask)
            else "no isolated dark pixels found"
        )
        return image.copy(), 0, backend_mode, reason

    source_area = max(1, width * height)
    if len(candidates) / source_area > _DESPECKLE_MAX_CANDIDATE_RATIO:
        return image.copy(), 0, backend_mode, "despeckle skipped: candidate density exceeds safety threshold"

    gray_pixels = grayscale.load()
    source: Image.Image | None = None
    output: Image.Image | None = None
    source_pixels: Any = None
    output_pixels: Any = None
    replacements: list[tuple[int, int, tuple[int, int, int]]] = []
    candidate_set = set(candidates)
    for x, y in candidates:
        dark_neighbors = 0
        neighbor_values: list[int] = []
        for ny in range(y - 1, y + 2):
            for nx in range(x - 1, x + 2):
                if nx == x and ny == y:
                    continue
                value = gray_pixels[nx, ny]
                neighbor_values.append(value)
                if value <= _DESPECKLE_NEAR_DARK_THRESHOLD and (nx, ny) not in candidate_set:
                    dark_neighbors += 1
        if dark_neighbors > 1:
            continue

        wider_dark = 0
        for ny in range(max(0, y - 2), min(height, y + 3)):
            for nx in range(max(0, x - 2), min(width, x + 3)):
                if nx == x and ny == y:
                    continue
                if gray_pixels[nx, ny] <= _DESPECKLE_NEAR_DARK_THRESHOLD and (nx, ny) not in candidate_set:
                    wider_dark += 1
        if wider_dark > 2:
            continue
        if _despeckle_has_nearby_content_context(gray_pixels, width, height, x, y):
            continue

        median_gray = sorted(neighbor_values)[len(neighbor_values) // 2]
        if median_gray < _DESPECKLE_MIN_BACKGROUND_MEDIAN:
            continue

        if source is None:
            source = image if image.mode == "RGB" else image.convert("RGB")
            source_pixels = source.load()
        neighbor_rgb = [
            source_pixels[nx, ny]
            for ny in range(y - 1, y + 2)
            for nx in range(x - 1, x + 2)
            if nx != x or ny != y
        ]
        replacement = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*neighbor_rgb))
        replacements.append((x, y, replacement))

    changed = len(replacements)
    if not changed:
        return image.copy(), 0, backend_mode, "no isolated dark pixels found"
    if changed / source_area > _DESPECKLE_MAX_CHANGED_RATIO:
        return image.copy(), 0, backend_mode, "despeckle skipped: pixel change ratio exceeds safety threshold"

    source = image if image.mode == "RGB" else image.convert("RGB")
    output = source.copy()
    output_pixels = output.load()
    for x, y, replacement in replacements:
        output_pixels[x, y] = replacement

    if image.mode == "L":
        return output.convert("L"), changed, backend_mode, "isolated dark pixels replaced"
    if image.mode == "RGB":
        return output, changed, backend_mode, "isolated dark pixels replaced"
    return output.convert(image.mode), changed, backend_mode, "isolated dark pixels replaced"


def _despeckle_protected_edge_margin(width: int, height: int) -> int:
    return min(5, max(1, min(width, height) // 12))


def _despeckle_mask_touches_protected_edge(dark_mask: Image.Image) -> bool:
    bbox = dark_mask.getbbox()
    if not bbox:
        return False
    width, height = dark_mask.size
    margin = _despeckle_protected_edge_margin(width, height)
    left, top, right, bottom = bbox
    return left < margin or top < margin or right > width - margin or bottom > height - margin


def _despeckle_has_nearby_content_context(gray_pixels: Any, width: int, height: int, x: int, y: int) -> bool:
    dark_pixels = 0
    radius = _DESPECKLE_CONTENT_CONTEXT_RADIUS
    for ny in range(max(0, y - radius), min(height, y + radius + 1)):
        for nx in range(max(0, x - radius), min(width, x + radius + 1)):
            if nx == x and ny == y:
                continue
            if abs(nx - x) <= 2 and abs(ny - y) <= 2:
                continue
            if gray_pixels[nx, ny] <= _DESPECKLE_NEAR_DARK_THRESHOLD:
                dark_pixels += 1
                if dark_pixels >= _DESPECKLE_CONTENT_CONTEXT_MIN_DARK_PIXELS:
                    return True
    return False


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
    left = max(0, left - 1)
    top = max(0, top - 1)
    right = min(width, right + 1)
    bottom = min(height, bottom + 1)
    if left >= width - 1 or top >= height - 1 or right <= 1 or bottom <= 1:
        return []

    try:
        crop = np.asarray(dark_mask.crop((left, top, right, bottom)), dtype=np.uint8) > 0
    except (TypeError, ValueError):
        return None

    _, crop_width = crop.shape
    local_y_values, local_x_values = np.nonzero(crop)
    dark_points = {
        (int(local_x), int(local_y))
        for local_y, local_x in zip(local_y_values, local_x_values)
    }
    return _despeckle_candidate_points_from_dark_points(
        dark_points,
        width=width,
        height=height,
        left=left,
        top=top,
    )


def _despeckle_candidate_points_fallback(dark_mask: Image.Image) -> list[tuple[int, int]]:
    width, height = dark_mask.size
    if width < 3 or height < 3:
        return []

    candidate_bbox = dark_mask.getbbox()
    if not candidate_bbox:
        return []

    left, top, right, bottom = candidate_bbox
    left = max(0, left - 1)
    top = max(0, top - 1)
    right = min(width, right + 1)
    bottom = min(height, bottom + 1)
    if left >= width - 1 or top >= height - 1 or right <= 1 or bottom <= 1:
        return []

    crop_width = right - left
    crop_height = bottom - top
    crop_values = dark_mask.crop((left, top, right, bottom)).tobytes()
    dark_pixel_count = crop_values.count(255)
    dense_prefilter_candidates = _despeckle_dense_connected_content_candidates(
        crop_values,
        crop_width=crop_width,
        crop_height=crop_height,
        dark_pixel_count=dark_pixel_count,
        width=width,
        height=height,
        left=left,
        top=top,
    )
    if dense_prefilter_candidates is not None:
        return dense_prefilter_candidates
    dark_points = {
        (index % crop_width, index // crop_width)
        for index, mask_value in enumerate(crop_values)
        if mask_value
    }
    return _despeckle_candidate_points_from_dark_points(
        dark_points,
        width=width,
        height=height,
        left=left,
        top=top,
    )


def _despeckle_dense_connected_content_candidates(
    crop_values: bytes,
    *,
    crop_width: int,
    crop_height: int,
    dark_pixel_count: int,
    width: int,
    height: int,
    left: int,
    top: int,
) -> list[tuple[int, int]] | None:
    if dark_pixel_count < _DESPECKLE_DENSE_PREFILTER_MIN_DARK_PIXELS:
        return None

    crop = Image.frombytes("L", (crop_width, crop_height), crop_values)
    neighbor_counts = crop.filter(ImageFilter.Kernel((3, 3), [1] * 9, scale=255))
    low_connectivity_mask = ImageChops.multiply(
        crop,
        neighbor_counts.point(
            lambda value: 255 if 0 < value <= _DESPECKLE_MAX_COMPONENT_PIXELS else 0,
            mode="L",
        ),
    )
    low_connectivity_bbox = low_connectivity_mask.getbbox()
    if not low_connectivity_bbox:
        return []

    low_connectivity_values = low_connectivity_mask.tobytes()
    low_connectivity_pixels = [
        (index % crop_width, index // crop_width)
        for index, mask_value in enumerate(low_connectivity_values)
        if mask_value
    ]

    low_connectivity_set = set(low_connectivity_pixels)
    margin = _despeckle_protected_edge_margin(width, height)
    candidates: list[tuple[int, int]] = []
    visited: set[tuple[int, int]] = set()
    neighbor_offsets = (
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    )
    for seed in low_connectivity_pixels:
        if seed in visited:
            continue
        stack = [seed]
        component: list[tuple[int, int]] = []
        oversized = False
        touches_connected_content = False
        while stack:
            local_x, local_y = stack.pop()
            if (local_x, local_y) in visited:
                continue
            visited.add((local_x, local_y))
            component.append((local_x, local_y))
            if len(component) > _DESPECKLE_MAX_COMPONENT_PIXELS:
                oversized = True
                break
            component_x = [point[0] for point in component]
            component_y = [point[1] for point in component]
            if max(component_x) - min(component_x) + 1 > _DESPECKLE_MAX_COMPONENT_SPAN:
                oversized = True
                break
            if max(component_y) - min(component_y) + 1 > _DESPECKLE_MAX_COMPONENT_SPAN:
                oversized = True
                break
            for offset_x, offset_y in neighbor_offsets:
                neighbor_x = local_x + offset_x
                neighbor_y = local_y + offset_y
                if neighbor_x < 0 or neighbor_y < 0 or neighbor_x >= crop_width or neighbor_y >= crop_height:
                    continue
                if not crop_values[neighbor_y * crop_width + neighbor_x]:
                    continue
                neighbor = (neighbor_x, neighbor_y)
                if neighbor not in low_connectivity_set:
                    touches_connected_content = True
                    continue
                if neighbor in visited:
                    continue
                stack.append(neighbor)

        if oversized or touches_connected_content:
            continue
        absolute_points = [(left + local_x, top + local_y) for local_x, local_y in component]
        if any(
            x == 0
            or y == 0
            or x == width - 1
            or y == height - 1
            or x < margin
            or y < margin
            or x >= width - margin
            or y >= height - margin
            for x, y in absolute_points
        ):
            continue
        candidates.extend(absolute_points)
    return sorted(candidates, key=lambda point: (point[1], point[0]))


def _despeckle_candidate_points_from_dark_points(
    dark_points: set[tuple[int, int]],
    *,
    width: int,
    height: int,
    left: int,
    top: int,
) -> list[tuple[int, int]]:
    margin = _despeckle_protected_edge_margin(width, height)
    candidates: list[tuple[int, int]] = []
    visited: set[tuple[int, int]] = set()
    neighbor_offsets = (
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    )
    for start in sorted(dark_points, key=lambda point: (point[1], point[0])):
        if start in visited:
            continue
        stack = [start]
        component: list[tuple[int, int]] = []
        visited.add(start)
        while stack:
            local_x, local_y = stack.pop()
            component.append((local_x, local_y))
            for offset_x, offset_y in neighbor_offsets:
                neighbor = (local_x + offset_x, local_y + offset_y)
                if neighbor in visited or neighbor not in dark_points:
                    continue
                visited.add(neighbor)
                stack.append(neighbor)

        if len(component) > _DESPECKLE_MAX_COMPONENT_PIXELS:
            continue
        component_x = [point[0] for point in component]
        component_y = [point[1] for point in component]
        if max(component_x) - min(component_x) + 1 > _DESPECKLE_MAX_COMPONENT_SPAN:
            continue
        if max(component_y) - min(component_y) + 1 > _DESPECKLE_MAX_COMPONENT_SPAN:
            continue

        absolute_points = [(left + local_x, top + local_y) for local_x, local_y in component]
        if any(
            x == 0
            or y == 0
            or x == width - 1
            or y == height - 1
            or x < margin
            or y < margin
            or x >= width - margin
            or y >= height - margin
            for x, y in absolute_points
        ):
            continue

        candidates.extend(absolute_points)

    return sorted(candidates, key=lambda point: (point[1], point[0]))


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
