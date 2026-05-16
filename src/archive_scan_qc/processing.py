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
import math
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
    normalize_paper_color_cast: bool = False
    lighten_edge_shadow: bool = False
    lighten_corner_shadows: bool = False
    lighten_background_stains: bool = False
    lighten_fold_shadows: bool = False
    level_illumination_gradient: bool = False
    clean_bleed_through: bool = False
    lighten_scanlines: bool = False
    enhance_faded_text: bool = False
    sharpen_text_edges: bool = False
    scanner_gutter_trim: bool = False
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
    audit_max_local_content_changed_ratio: float = 0.20
    audit_max_local_content_tile_changed_ratio: float = 0.45
    audit_max_edge_content_changed_ratio: float = 0.18
    audit_max_text_combo_changed_pixel_ratio: float = 0.10
    audit_max_text_combo_local_changed_ratio: float = 0.12
    audit_max_text_combo_edge_changed_ratio: float = 0.12
    audit_max_geometry_combo_crop_ratio: float = 0.55
    audit_max_geometry_combo_size_change_ratio: float = 0.55
    audit_max_processed_near_white_ratio: float = 0.88
    audit_max_processed_near_white_delta: float = 0.30
    audit_max_processed_highlight_clip_ratio: float = 0.25
    audit_max_processed_highlight_clip_delta: float = 0.18
    audit_max_processed_bright_page_delta: float = 18.0
    audit_max_processed_dark_pixel_loss_ratio: float = 0.45
    audit_max_processed_dark_pixel_lift_ratio: float = 0.35
    audit_max_processed_full_page_change_ratio: float = 0.85
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
    reason_code: str | None = None
    edge_sides: tuple[str, ...] = ()
    band_width_bucket: str | None = None


@dataclass(frozen=True)
class CropDetection:
    bbox: tuple[int, int, int, int] | None
    reason: str


@dataclass(frozen=True)
class ScannerGutterTrimDetection:
    bbox: tuple[int, int, int, int] | None
    reason: str
    margins: dict[str, float]


@dataclass(frozen=True)
class DespeckleResult:
    image: Image.Image
    changed_pixels: int
    backend_mode: str
    reason: str
    reason_code: str
    candidate_pixels: int
    candidate_count: int
    candidate_count_bucket: str
    replacement_work_performed: bool


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
class PaperColorCastNormalizationResult:
    image: Image.Image
    applied: bool
    reason: str
    reason_code: str
    color_delta: float
    brightness_delta: float
    changed_pixel_ratio: float
    candidate_pixel_ratio: float


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
class CornerShadowCleanupResult:
    image: Image.Image
    applied: bool
    reason: str
    reason_code: str
    corners: tuple[str, ...]
    corner_mean_before: float | None
    corner_mean_after: float | None
    corner_delta: float
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
class FoldShadowCleanupResult:
    image: Image.Image
    applied: bool
    reason: str
    orientation: str | None
    band_count: int
    band_mean_before: float | None
    band_mean_after: float | None
    band_delta: float
    changed_pixel_ratio: float
    candidate_pixel_ratio: float


@dataclass(frozen=True)
class IlluminationGradientLevelingResult:
    image: Image.Image
    applied: bool
    reason: str
    reason_code: str
    orientation: str | None
    gradient_delta_before: float
    gradient_delta_after: float
    correction_delta: float
    changed_pixel_ratio: float
    candidate_pixel_ratio: float


@dataclass(frozen=True)
class BleedThroughCleanupResult:
    image: Image.Image
    applied: bool
    reason: str
    ghost_mean_before: float | None
    ghost_mean_after: float | None
    ghost_delta: float
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
            "scanner_gutter_trim_conservative" if options.scanner_gutter_trim else "scanner_gutter_trim_disabled",
            "auto_crop_conservative" if options.auto_crop else "auto_crop_disabled",
            "despeckle_isolated_pixels" if options.despeckle else "despeckle_disabled",
            "normalize_tones_conservative" if options.normalize_tones else "normalize_tones_disabled",
            (
                "normalize_paper_color_cast_conservative"
                if options.normalize_paper_color_cast
                else "normalize_paper_color_cast_disabled"
            ),
            "lighten_edge_shadow_conservative" if options.lighten_edge_shadow else "lighten_edge_shadow_disabled",
            (
                "lighten_corner_shadows_conservative"
                if options.lighten_corner_shadows
                else "lighten_corner_shadows_disabled"
            ),
            (
                "lighten_background_stains_conservative"
                if options.lighten_background_stains
                else "lighten_background_stains_disabled"
            ),
            "lighten_fold_shadows_conservative" if options.lighten_fold_shadows else "lighten_fold_shadows_disabled",
            (
                "level_illumination_gradient_conservative"
                if options.level_illumination_gradient
                else "level_illumination_gradient_disabled"
            ),
            "clean_bleed_through_conservative" if options.clean_bleed_through else "clean_bleed_through_disabled",
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
        "dark_border_reason_code": None,
        "dark_border_edge_sides": [],
        "dark_border_band_width_bucket": None,
        "scanner_gutter_trimmed": False,
        "scanner_gutter_bbox": None,
        "scanner_gutter_reason": None,
        "scanner_gutter_trim_margins": {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0},
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
        "paper_color_cast_normalized": False,
        "paper_color_cast_reason": None,
        "paper_color_cast_reason_code": None,
        "paper_color_cast_delta": 0.0,
        "paper_color_cast_brightness_delta": 0.0,
        "paper_color_cast_changed_pixel_ratio": 0.0,
        "paper_color_cast_candidate_pixel_ratio": 0.0,
        "edge_shadow_lightened": False,
        "edge_shadow_reason": None,
        "edge_shadow_edges": [],
        "edge_shadow_mean_before": None,
        "edge_shadow_mean_after": None,
        "edge_shadow_delta": 0.0,
        "edge_shadow_changed_pixel_ratio": 0.0,
        "edge_shadow_candidate_pixel_ratio": 0.0,
        "corner_shadows_lightened": False,
        "corner_shadows_reason": None,
        "corner_shadows_reason_code": None,
        "corner_shadows_corners": [],
        "corner_shadows_mean_before": None,
        "corner_shadows_mean_after": None,
        "corner_shadows_delta": 0.0,
        "corner_shadows_changed_pixel_ratio": 0.0,
        "corner_shadows_candidate_pixel_ratio": 0.0,
        "background_stains_lightened": False,
        "background_stains_reason": None,
        "background_stains_mean_before": None,
        "background_stains_mean_after": None,
        "background_stains_delta": 0.0,
        "background_stains_changed_pixel_ratio": 0.0,
        "background_stains_candidate_pixel_ratio": 0.0,
        "fold_shadows_lightened": False,
        "fold_shadows_reason": None,
        "fold_shadows_reason_code": None,
        "fold_shadows_orientation": None,
        "fold_shadows_count": 0,
        "fold_shadows_mean_before": None,
        "fold_shadows_mean_after": None,
        "fold_shadows_delta": 0.0,
        "fold_shadows_changed_pixel_ratio": 0.0,
        "fold_shadows_candidate_pixel_ratio": 0.0,
        "illumination_gradient_levelled": False,
        "illumination_gradient_reason": None,
        "illumination_gradient_reason_code": None,
        "illumination_gradient_orientation": None,
        "illumination_gradient_delta_before": 0.0,
        "illumination_gradient_delta_after": 0.0,
        "illumination_gradient_correction_delta": 0.0,
        "illumination_gradient_changed_pixel_ratio": 0.0,
        "illumination_gradient_candidate_pixel_ratio": 0.0,
        "bleed_through_cleaned": False,
        "bleed_through_reason": None,
        "bleed_through_mean_before": None,
        "bleed_through_mean_after": None,
        "bleed_through_delta": 0.0,
        "bleed_through_changed_pixel_ratio": 0.0,
        "bleed_through_candidate_pixel_ratio": 0.0,
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
        "faded_text_reason_code": None,
        "faded_text_reason_zh": None,
        "faded_text_delta": 0.0,
        "faded_text_changed_pixel_ratio": 0.0,
        "faded_text_candidate_pixel_ratio": 0.0,
        "text_edges_sharpened": False,
        "text_edges_reason": None,
        "text_edges_reason_code": None,
        "text_edges_reason_zh": None,
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
                "dark_border_reason_code": process_info["dark_border_reason_code"],
                "dark_border_edge_sides": process_info["dark_border_edge_sides"],
                "dark_border_band_width_bucket": process_info["dark_border_band_width_bucket"],
                "scanner_gutter_trimmed": process_info["scanner_gutter_trimmed"],
                "scanner_gutter_bbox": process_info["scanner_gutter_bbox"],
                "scanner_gutter_reason": process_info["scanner_gutter_reason"],
                "scanner_gutter_trim_margins": process_info["scanner_gutter_trim_margins"],
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
                "paper_color_cast_normalized": process_info["paper_color_cast_normalized"],
                "paper_color_cast_reason": process_info["paper_color_cast_reason"],
                "paper_color_cast_reason_code": process_info["paper_color_cast_reason_code"],
                "paper_color_cast_delta": process_info["paper_color_cast_delta"],
                "paper_color_cast_brightness_delta": process_info["paper_color_cast_brightness_delta"],
                "paper_color_cast_changed_pixel_ratio": process_info["paper_color_cast_changed_pixel_ratio"],
                "paper_color_cast_candidate_pixel_ratio": process_info["paper_color_cast_candidate_pixel_ratio"],
                "edge_shadow_lightened": process_info["edge_shadow_lightened"],
                "edge_shadow_reason": process_info["edge_shadow_reason"],
                "edge_shadow_edges": process_info["edge_shadow_edges"],
                "edge_shadow_mean_before": process_info["edge_shadow_mean_before"],
                "edge_shadow_mean_after": process_info["edge_shadow_mean_after"],
                "edge_shadow_delta": process_info["edge_shadow_delta"],
                "edge_shadow_changed_pixel_ratio": process_info["edge_shadow_changed_pixel_ratio"],
                "edge_shadow_candidate_pixel_ratio": process_info["edge_shadow_candidate_pixel_ratio"],
                "corner_shadows_lightened": process_info["corner_shadows_lightened"],
                "corner_shadows_reason": process_info["corner_shadows_reason"],
                "corner_shadows_reason_code": process_info["corner_shadows_reason_code"],
                "corner_shadows_corners": process_info["corner_shadows_corners"],
                "corner_shadows_mean_before": process_info["corner_shadows_mean_before"],
                "corner_shadows_mean_after": process_info["corner_shadows_mean_after"],
                "corner_shadows_delta": process_info["corner_shadows_delta"],
                "corner_shadows_changed_pixel_ratio": process_info["corner_shadows_changed_pixel_ratio"],
                "corner_shadows_candidate_pixel_ratio": process_info["corner_shadows_candidate_pixel_ratio"],
                "background_stains_lightened": process_info["background_stains_lightened"],
                "background_stains_reason": process_info["background_stains_reason"],
                "background_stains_mean_before": process_info["background_stains_mean_before"],
                "background_stains_mean_after": process_info["background_stains_mean_after"],
                "background_stains_delta": process_info["background_stains_delta"],
                "background_stains_changed_pixel_ratio": process_info["background_stains_changed_pixel_ratio"],
                "background_stains_candidate_pixel_ratio": process_info["background_stains_candidate_pixel_ratio"],
                "fold_shadows_lightened": process_info["fold_shadows_lightened"],
                "fold_shadows_reason": process_info["fold_shadows_reason"],
                "fold_shadows_reason_code": process_info["fold_shadows_reason_code"],
                "fold_shadows_orientation": process_info["fold_shadows_orientation"],
                "fold_shadows_count": process_info["fold_shadows_count"],
                "fold_shadows_mean_before": process_info["fold_shadows_mean_before"],
                "fold_shadows_mean_after": process_info["fold_shadows_mean_after"],
                "fold_shadows_delta": process_info["fold_shadows_delta"],
                "fold_shadows_changed_pixel_ratio": process_info["fold_shadows_changed_pixel_ratio"],
                "fold_shadows_candidate_pixel_ratio": process_info["fold_shadows_candidate_pixel_ratio"],
                "illumination_gradient_levelled": process_info["illumination_gradient_levelled"],
                "illumination_gradient_reason": process_info["illumination_gradient_reason"],
                "illumination_gradient_reason_code": process_info["illumination_gradient_reason_code"],
                "illumination_gradient_orientation": process_info["illumination_gradient_orientation"],
                "illumination_gradient_delta_before": process_info["illumination_gradient_delta_before"],
                "illumination_gradient_delta_after": process_info["illumination_gradient_delta_after"],
                "illumination_gradient_correction_delta": process_info["illumination_gradient_correction_delta"],
                "illumination_gradient_changed_pixel_ratio": process_info[
                    "illumination_gradient_changed_pixel_ratio"
                ],
                "illumination_gradient_candidate_pixel_ratio": process_info[
                    "illumination_gradient_candidate_pixel_ratio"
                ],
                "bleed_through_cleaned": process_info["bleed_through_cleaned"],
                "bleed_through_reason": process_info["bleed_through_reason"],
                "bleed_through_mean_before": process_info["bleed_through_mean_before"],
                "bleed_through_mean_after": process_info["bleed_through_mean_after"],
                "bleed_through_delta": process_info["bleed_through_delta"],
                "bleed_through_changed_pixel_ratio": process_info["bleed_through_changed_pixel_ratio"],
                "bleed_through_candidate_pixel_ratio": process_info["bleed_through_candidate_pixel_ratio"],
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
                "faded_text_reason_code": process_info["faded_text_reason_code"],
                "faded_text_reason_zh": process_info["faded_text_reason_zh"],
                "faded_text_delta": process_info["faded_text_delta"],
                "faded_text_changed_pixel_ratio": process_info["faded_text_changed_pixel_ratio"],
                "faded_text_candidate_pixel_ratio": process_info["faded_text_candidate_pixel_ratio"],
                "text_edges_sharpened": process_info["text_edges_sharpened"],
                "text_edges_reason": process_info["text_edges_reason"],
                "text_edges_reason_code": process_info["text_edges_reason_code"],
                "text_edges_reason_zh": process_info["text_edges_reason_zh"],
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
                    "dark_border_reason_code": process_info["dark_border_reason_code"],
                    "dark_border_edge_sides": process_info["dark_border_edge_sides"],
                    "dark_border_band_width_bucket": process_info["dark_border_band_width_bucket"],
                    "scanner_gutter_trimmed": process_info["scanner_gutter_trimmed"],
                    "scanner_gutter_bbox": process_info["scanner_gutter_bbox"],
                    "scanner_gutter_reason": process_info["scanner_gutter_reason"],
                    "scanner_gutter_trim_margins": process_info["scanner_gutter_trim_margins"],
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
                    "paper_color_cast_normalized": process_info["paper_color_cast_normalized"],
                    "paper_color_cast_reason": process_info["paper_color_cast_reason"],
                    "paper_color_cast_reason_code": process_info["paper_color_cast_reason_code"],
                    "paper_color_cast_delta": process_info["paper_color_cast_delta"],
                    "paper_color_cast_brightness_delta": process_info["paper_color_cast_brightness_delta"],
                    "paper_color_cast_changed_pixel_ratio": process_info["paper_color_cast_changed_pixel_ratio"],
                    "paper_color_cast_candidate_pixel_ratio": process_info["paper_color_cast_candidate_pixel_ratio"],
                    "edge_shadow_lightened": process_info["edge_shadow_lightened"],
                    "edge_shadow_reason": process_info["edge_shadow_reason"],
                    "edge_shadow_edges": process_info["edge_shadow_edges"],
                    "edge_shadow_mean_before": process_info["edge_shadow_mean_before"],
                    "edge_shadow_mean_after": process_info["edge_shadow_mean_after"],
                    "edge_shadow_delta": process_info["edge_shadow_delta"],
                    "edge_shadow_changed_pixel_ratio": process_info["edge_shadow_changed_pixel_ratio"],
                    "edge_shadow_candidate_pixel_ratio": process_info["edge_shadow_candidate_pixel_ratio"],
                    "corner_shadows_lightened": process_info["corner_shadows_lightened"],
                    "corner_shadows_reason": process_info["corner_shadows_reason"],
                    "corner_shadows_reason_code": process_info["corner_shadows_reason_code"],
                    "corner_shadows_corners": process_info["corner_shadows_corners"],
                    "corner_shadows_mean_before": process_info["corner_shadows_mean_before"],
                    "corner_shadows_mean_after": process_info["corner_shadows_mean_after"],
                    "corner_shadows_delta": process_info["corner_shadows_delta"],
                    "corner_shadows_changed_pixel_ratio": process_info["corner_shadows_changed_pixel_ratio"],
                    "corner_shadows_candidate_pixel_ratio": process_info["corner_shadows_candidate_pixel_ratio"],
                    "background_stains_lightened": process_info["background_stains_lightened"],
                    "background_stains_reason": process_info["background_stains_reason"],
                    "background_stains_mean_before": process_info["background_stains_mean_before"],
                    "background_stains_mean_after": process_info["background_stains_mean_after"],
                    "background_stains_delta": process_info["background_stains_delta"],
                    "background_stains_changed_pixel_ratio": process_info["background_stains_changed_pixel_ratio"],
                    "background_stains_candidate_pixel_ratio": process_info["background_stains_candidate_pixel_ratio"],
                    "fold_shadows_lightened": process_info["fold_shadows_lightened"],
                    "fold_shadows_reason": process_info["fold_shadows_reason"],
                    "fold_shadows_reason_code": process_info["fold_shadows_reason_code"],
                    "fold_shadows_orientation": process_info["fold_shadows_orientation"],
                    "fold_shadows_count": process_info["fold_shadows_count"],
                    "fold_shadows_mean_before": process_info["fold_shadows_mean_before"],
                    "fold_shadows_mean_after": process_info["fold_shadows_mean_after"],
                    "fold_shadows_delta": process_info["fold_shadows_delta"],
                    "fold_shadows_changed_pixel_ratio": process_info["fold_shadows_changed_pixel_ratio"],
                    "fold_shadows_candidate_pixel_ratio": process_info["fold_shadows_candidate_pixel_ratio"],
                    "illumination_gradient_levelled": process_info["illumination_gradient_levelled"],
                    "illumination_gradient_reason": process_info["illumination_gradient_reason"],
                    "illumination_gradient_reason_code": process_info["illumination_gradient_reason_code"],
                    "illumination_gradient_orientation": process_info["illumination_gradient_orientation"],
                    "illumination_gradient_delta_before": process_info["illumination_gradient_delta_before"],
                    "illumination_gradient_delta_after": process_info["illumination_gradient_delta_after"],
                    "illumination_gradient_correction_delta": process_info[
                        "illumination_gradient_correction_delta"
                    ],
                    "illumination_gradient_changed_pixel_ratio": process_info[
                        "illumination_gradient_changed_pixel_ratio"
                    ],
                    "illumination_gradient_candidate_pixel_ratio": process_info[
                        "illumination_gradient_candidate_pixel_ratio"
                    ],
                    "bleed_through_cleaned": process_info["bleed_through_cleaned"],
                    "bleed_through_reason": process_info["bleed_through_reason"],
                    "bleed_through_mean_before": process_info["bleed_through_mean_before"],
                    "bleed_through_mean_after": process_info["bleed_through_mean_after"],
                    "bleed_through_delta": process_info["bleed_through_delta"],
                    "bleed_through_changed_pixel_ratio": process_info["bleed_through_changed_pixel_ratio"],
                    "bleed_through_candidate_pixel_ratio": process_info["bleed_through_candidate_pixel_ratio"],
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
                    "faded_text_reason_code": process_info["faded_text_reason_code"],
                    "faded_text_reason_zh": process_info["faded_text_reason_zh"],
                    "faded_text_delta": process_info["faded_text_delta"],
                    "faded_text_changed_pixel_ratio": process_info["faded_text_changed_pixel_ratio"],
                    "faded_text_candidate_pixel_ratio": process_info["faded_text_candidate_pixel_ratio"],
                    "text_edges_sharpened": process_info["text_edges_sharpened"],
                    "text_edges_reason": process_info["text_edges_reason"],
                    "text_edges_reason_code": process_info["text_edges_reason_code"],
                    "text_edges_reason_zh": process_info["text_edges_reason_zh"],
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
        "audit_max_cumulative_change_score": options.audit_max_cumulative_change_score,
        "audit_max_cumulative_pixel_change_ratio": options.audit_max_cumulative_pixel_change_ratio,
        "audit_max_cumulative_brightness_delta": options.audit_max_cumulative_brightness_delta,
        "audit_max_cumulative_contrast_delta": options.audit_max_cumulative_contrast_delta,
        "audit_max_cumulative_crop_ratio": options.audit_max_cumulative_crop_ratio,
        "audit_max_cumulative_candidate_pixel_ratio": options.audit_max_cumulative_candidate_pixel_ratio,
        "audit_max_local_content_changed_ratio": options.audit_max_local_content_changed_ratio,
        "audit_max_local_content_tile_changed_ratio": options.audit_max_local_content_tile_changed_ratio,
        "audit_max_edge_content_changed_ratio": options.audit_max_edge_content_changed_ratio,
        "audit_max_text_combo_changed_pixel_ratio": options.audit_max_text_combo_changed_pixel_ratio,
        "audit_max_text_combo_local_changed_ratio": options.audit_max_text_combo_local_changed_ratio,
        "audit_max_text_combo_edge_changed_ratio": options.audit_max_text_combo_edge_changed_ratio,
        "audit_max_geometry_combo_crop_ratio": options.audit_max_geometry_combo_crop_ratio,
        "audit_max_geometry_combo_size_change_ratio": options.audit_max_geometry_combo_size_change_ratio,
        "audit_max_processed_near_white_ratio": options.audit_max_processed_near_white_ratio,
        "audit_max_processed_near_white_delta": options.audit_max_processed_near_white_delta,
        "audit_max_processed_highlight_clip_ratio": options.audit_max_processed_highlight_clip_ratio,
        "audit_max_processed_highlight_clip_delta": options.audit_max_processed_highlight_clip_delta,
        "audit_max_processed_bright_page_delta": options.audit_max_processed_bright_page_delta,
        "audit_max_processed_dark_pixel_loss_ratio": options.audit_max_processed_dark_pixel_loss_ratio,
        "audit_max_processed_dark_pixel_lift_ratio": options.audit_max_processed_dark_pixel_lift_ratio,
        "audit_max_processed_full_page_change_ratio": options.audit_max_processed_full_page_change_ratio,
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
    dark_border_reason_codes = [
        record.get("dark_border_reason_code")
        for record in processed_records
        if isinstance(record.get("dark_border_reason_code"), str)
    ]
    dark_border_edge_sides = [
        side
        for record in processed_records
        for side in record.get("dark_border_edge_sides", [])
        if isinstance(side, str)
    ]
    dark_border_band_width_buckets = [
        record.get("dark_border_band_width_bucket")
        for record in processed_records
        if isinstance(record.get("dark_border_band_width_bucket"), str)
    ]
    scanner_gutter_reasons = [
        record.get("scanner_gutter_reason")
        for record in processed_records
        if isinstance(record.get("scanner_gutter_reason"), str)
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
    fold_shadows_reasons = [
        record.get("fold_shadows_reason")
        for record in processed_records
        if isinstance(record.get("fold_shadows_reason"), str)
    ]
    fold_shadows_reason_codes = [
        record.get("fold_shadows_reason_code")
        for record in processed_records
        if isinstance(record.get("fold_shadows_reason_code"), str)
    ]
    fold_shadows_skipped_reason_codes = [
        code
        for record in processed_records
        for code in [record.get("fold_shadows_reason_code")]
        if record.get("fold_shadows_lightened") is False and isinstance(code, str) and code != "disabled"
    ]
    fold_shadows_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("fold_shadows_reason")]
        if record.get("fold_shadows_lightened") is False
        and isinstance(reason, str)
        and reason != "fold shadow cleanup disabled"
    ]
    illumination_gradient_reasons = [
        record.get("illumination_gradient_reason")
        for record in processed_records
        if isinstance(record.get("illumination_gradient_reason"), str)
    ]
    illumination_gradient_reason_codes = [
        record.get("illumination_gradient_reason_code")
        for record in processed_records
        if isinstance(record.get("illumination_gradient_reason_code"), str)
    ]
    illumination_gradient_skipped_reason_codes = [
        code
        for record in processed_records
        for code in [record.get("illumination_gradient_reason_code")]
        if record.get("illumination_gradient_levelled") is False and isinstance(code, str) and code != "disabled"
    ]
    illumination_gradient_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("illumination_gradient_reason")]
        if record.get("illumination_gradient_levelled") is False
        and isinstance(reason, str)
        and reason != "illumination gradient leveling disabled"
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
    faded_text_reason_codes = [
        record.get("faded_text_reason_code")
        for record in processed_records
        if isinstance(record.get("faded_text_reason_code"), str)
    ]
    faded_text_skipped_reason_codes = [
        code
        for record in processed_records
        for code in [record.get("faded_text_reason_code")]
        if record.get("faded_text_enhanced") is False
        and isinstance(code, str)
        and code != "disabled"
    ]
    faded_text_skipped_reasons_zh = [
        reason_zh
        for record in processed_records
        for reason_zh in [record.get("faded_text_reason_zh")]
        if record.get("faded_text_enhanced") is False
        and isinstance(reason_zh, str)
        and record.get("faded_text_reason_code") != "disabled"
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
    text_edges_reason_codes = [
        record.get("text_edges_reason_code")
        for record in processed_records
        if isinstance(record.get("text_edges_reason_code"), str)
    ]
    text_edges_skipped_reason_codes = [
        code
        for record in processed_records
        for code in [record.get("text_edges_reason_code")]
        if record.get("text_edges_sharpened") is False and isinstance(code, str) and code != "disabled"
    ]
    text_edges_skipped_reasons_zh = [
        reason_zh
        for record in processed_records
        for reason_zh in [record.get("text_edges_reason_zh")]
        if record.get("text_edges_sharpened") is False
        and isinstance(reason_zh, str)
        and record.get("text_edges_reason_code") != "disabled"
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
    corner_shadows_reasons = [
        record.get("corner_shadows_reason")
        for record in processed_records
        if isinstance(record.get("corner_shadows_reason"), str)
    ]
    corner_shadows_reason_codes = [
        record.get("corner_shadows_reason_code")
        for record in processed_records
        if isinstance(record.get("corner_shadows_reason_code"), str)
    ]
    corner_shadows_skipped_reason_codes = [
        code
        for record in processed_records
        for code in [record.get("corner_shadows_reason_code")]
        if record.get("corner_shadows_lightened") is False and isinstance(code, str) and code != "disabled"
    ]
    corner_shadows_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("corner_shadows_reason")]
        if record.get("corner_shadows_lightened") is False
        and isinstance(reason, str)
        and reason != "corner shadow cleanup disabled"
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
    paper_color_cast_reasons = [
        record.get("paper_color_cast_reason")
        for record in processed_records
        if isinstance(record.get("paper_color_cast_reason"), str)
    ]
    paper_color_cast_reason_codes = [
        record.get("paper_color_cast_reason_code")
        for record in processed_records
        if isinstance(record.get("paper_color_cast_reason_code"), str)
    ]
    paper_color_cast_skipped_reason_codes = [
        code
        for record in processed_records
        for code in [record.get("paper_color_cast_reason_code")]
        if record.get("paper_color_cast_normalized") is False and isinstance(code, str) and code != "disabled"
    ]
    paper_color_cast_skipped_reasons = [
        reason
        for record in processed_records
        for reason in [record.get("paper_color_cast_reason")]
        if record.get("paper_color_cast_normalized") is False
        and isinstance(reason, str)
        and reason != "paper color cast normalization disabled"
    ]
    combination_reason_codes = [
        audit.get("combination_quality_guard_reason_code")
        for audit in audit_records
        if isinstance(audit.get("combination_quality_guard_reason_code"), str)
    ]
    combination_risk_tiers = [
        audit.get("combination_quality_guard_risk_tier")
        for audit in audit_records
        if isinstance(audit.get("combination_quality_guard_risk_tier"), str)
    ]
    processed_output_reason_codes = [
        audit.get("processed_output_safety_guard_reason_code")
        for audit in audit_records
        if isinstance(audit.get("processed_output_safety_guard_reason_code"), str)
    ]
    return {
        "schema_version": "scan-qc.processing.audit.v1",
        "generated_at": manifest["generated_at"],
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
            "local_content_change_guard_checked_files": sum(
                1 for audit in audit_records if audit.get("local_content_change_guard_checked") is True
            ),
            "local_content_change_guard_reverted_files": sum(
                1 for audit in audit_records if audit.get("local_content_change_guard_reverted") is True
            ),
            "local_content_change_guard_warning_files": sum(
                1
                for audit in audit_records
                if audit.get("local_content_change_guard_action") in {"reverted_to_source", "warn_review"}
            ),
            "combination_quality_guard_checked_files": sum(
                1 for audit in audit_records if audit.get("combination_quality_guard_checked") is True
            ),
            "combination_quality_guard_reverted_files": sum(
                1 for audit in audit_records if audit.get("combination_quality_guard_reverted") is True
            ),
            "combination_quality_guard_low_confidence_original_files": sum(
                1
                for audit in audit_records
                if audit.get("combination_quality_guard_reason_code") == "low_confidence_original_preserved"
            ),
            "processed_output_safety_guard_checked_files": sum(
                1 for audit in audit_records if audit.get("processed_output_safety_guard_checked") is True
            ),
            "processed_output_safety_guard_reverted_files": sum(
                1 for audit in audit_records if audit.get("processed_output_safety_guard_reverted") is True
            ),
            "processed_output_washout_guard_reverted_files": sum(
                1
                for audit in audit_records
                if audit.get("processed_output_safety_guard_reverted") is True
                and any(
                    reason in {"near_white_saturation", "bright_page_washout"}
                    for reason in audit.get("processed_output_safety_guard_reasons", [])
                )
            ),
            "processed_output_clipping_guard_reverted_files": sum(
                1
                for audit in audit_records
                if audit.get("processed_output_safety_guard_reverted") is True
                and "highlight_clipping" in audit.get("processed_output_safety_guard_reasons", [])
            ),
            "processed_output_foreground_loss_guard_reverted_files": sum(
                1
                for audit in audit_records
                if audit.get("processed_output_safety_guard_reverted") is True
                and "dark_foreground_loss" in audit.get("processed_output_safety_guard_reasons", [])
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
            "scanner_gutter_trimmed_files": sum(
                1 for record in processed_records if record.get("scanner_gutter_trimmed") is True
            ),
            "scanner_gutter_skipped_files": sum(
                1
                for record in processed_records
                if record.get("scanner_gutter_trimmed") is False
                and record.get("scanner_gutter_reason") not in {None, "scanner gutter trim disabled"}
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
            "paper_color_cast_normalized_files": sum(
                1 for audit in audit_records if audit.get("paper_color_cast_normalized") is True
            ),
            "paper_color_cast_skipped_files": sum(
                1
                for record in processed_records
                if record.get("paper_color_cast_normalized") is False
                and record.get("paper_color_cast_reason")
                not in {None, "paper color cast normalization disabled"}
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
            "corner_shadows_lightened_files": sum(
                1 for audit in audit_records if audit.get("corner_shadows_lightened") is True
            ),
            "corner_shadows_skipped_files": sum(
                1
                for record in processed_records
                if record.get("corner_shadows_lightened") is False
                and record.get("corner_shadows_reason") not in {None, "corner shadow cleanup disabled"}
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
            "fold_shadows_lightened_files": sum(
                1 for audit in audit_records if audit.get("fold_shadows_lightened") is True
            ),
            "fold_shadows_skipped_files": sum(
                1
                for record in processed_records
                if record.get("fold_shadows_lightened") is False
                and record.get("fold_shadows_reason") not in {None, "fold shadow cleanup disabled"}
            ),
            "illumination_gradient_levelled_files": sum(
                1 for audit in audit_records if audit.get("illumination_gradient_levelled") is True
            ),
            "illumination_gradient_skipped_files": sum(
                1
                for record in processed_records
                if record.get("illumination_gradient_levelled") is False
                and record.get("illumination_gradient_reason")
                not in {None, "illumination gradient leveling disabled"}
            ),
            "bleed_through_cleaned_files": sum(1 for audit in audit_records if audit.get("bleed_through_cleaned") is True),
            "bleed_through_skipped_files": sum(
                1
                for record in processed_records
                if record.get("bleed_through_cleaned") is False
                and record.get("bleed_through_reason") not in {None, "bleed-through cleanup disabled"}
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
            "scanner_gutter_max_trim_margin_ratio": _aggregate_metric(
                audit_records, "scanner_gutter_max_trim_margin_ratio"
            ),
            "deskew_abs_angle_degrees": _aggregate_metric(audit_records, "deskew_abs_angle_degrees"),
            "despeckle_pixel_ratio": _aggregate_metric(audit_records, "despeckle_pixel_ratio"),
            "tone_background_delta": _aggregate_metric(audit_records, "tone_background_delta"),
            "tone_contrast_delta": _aggregate_metric(audit_records, "tone_contrast_delta"),
            "tone_changed_pixel_ratio": _aggregate_metric(audit_records, "tone_changed_pixel_ratio"),
            "paper_color_cast_delta": _aggregate_metric(audit_records, "paper_color_cast_delta"),
            "paper_color_cast_brightness_delta": _aggregate_metric(
                audit_records,
                "paper_color_cast_brightness_delta",
            ),
            "paper_color_cast_changed_pixel_ratio": _aggregate_metric(
                audit_records,
                "paper_color_cast_changed_pixel_ratio",
            ),
            "paper_color_cast_candidate_pixel_ratio": _aggregate_metric(
                audit_records,
                "paper_color_cast_candidate_pixel_ratio",
            ),
            "edge_shadow_delta": _aggregate_metric(audit_records, "edge_shadow_delta"),
            "edge_shadow_changed_pixel_ratio": _aggregate_metric(
                audit_records, "edge_shadow_changed_pixel_ratio"
            ),
            "edge_shadow_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "edge_shadow_candidate_pixel_ratio"
            ),
            "corner_shadows_delta": _aggregate_metric(audit_records, "corner_shadows_delta"),
            "corner_shadows_changed_pixel_ratio": _aggregate_metric(
                audit_records, "corner_shadows_changed_pixel_ratio"
            ),
            "corner_shadows_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "corner_shadows_candidate_pixel_ratio"
            ),
            "background_stains_delta": _aggregate_metric(audit_records, "background_stains_delta"),
            "background_stains_changed_pixel_ratio": _aggregate_metric(
                audit_records, "background_stains_changed_pixel_ratio"
            ),
            "background_stains_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "background_stains_candidate_pixel_ratio"
            ),
            "fold_shadows_delta": _aggregate_metric(audit_records, "fold_shadows_delta"),
            "fold_shadows_changed_pixel_ratio": _aggregate_metric(
                audit_records, "fold_shadows_changed_pixel_ratio"
            ),
            "fold_shadows_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "fold_shadows_candidate_pixel_ratio"
            ),
            "illumination_gradient_correction_delta": _aggregate_metric(
                audit_records,
                "illumination_gradient_correction_delta",
            ),
            "illumination_gradient_changed_pixel_ratio": _aggregate_metric(
                audit_records,
                "illumination_gradient_changed_pixel_ratio",
            ),
            "illumination_gradient_candidate_pixel_ratio": _aggregate_metric(
                audit_records,
                "illumination_gradient_candidate_pixel_ratio",
            ),
            "bleed_through_delta": _aggregate_metric(audit_records, "bleed_through_delta"),
            "bleed_through_changed_pixel_ratio": _aggregate_metric(
                audit_records, "bleed_through_changed_pixel_ratio"
            ),
            "bleed_through_candidate_pixel_ratio": _aggregate_metric(
                audit_records, "bleed_through_candidate_pixel_ratio"
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
            "local_content_pixel_ratio": _aggregate_metric(audit_records, "local_content_pixel_ratio"),
            "local_content_changed_ratio": _aggregate_metric(audit_records, "local_content_changed_ratio"),
            "local_content_tile_changed_ratio": _aggregate_metric(
                audit_records, "local_content_tile_changed_ratio"
            ),
            "edge_content_changed_ratio": _aggregate_metric(audit_records, "edge_content_changed_ratio"),
            "processed_output_brightness_increase": _aggregate_metric(
                audit_records, "processed_output_brightness_increase"
            ),
            "processed_output_near_white_ratio": _aggregate_metric(
                audit_records, "processed_output_near_white_ratio"
            ),
            "processed_output_near_white_delta": _aggregate_metric(
                audit_records, "processed_output_near_white_delta"
            ),
            "processed_output_highlight_clip_ratio": _aggregate_metric(
                audit_records, "processed_output_highlight_clip_ratio"
            ),
            "processed_output_highlight_clip_delta": _aggregate_metric(
                audit_records, "processed_output_highlight_clip_delta"
            ),
            "processed_output_dark_pixel_loss_ratio": _aggregate_metric(
                audit_records, "processed_output_dark_pixel_loss_ratio"
            ),
            "processed_output_dark_pixel_lift_ratio": _aggregate_metric(
                audit_records, "processed_output_dark_pixel_lift_ratio"
            ),
        },
        "distributions": {
            "pixel_change_ratio": _ratio_distribution(audit_records, "pixel_change_ratio"),
            "crop_ratio": _ratio_distribution(audit_records, "crop_ratio"),
            "max_trim_margin_ratio": _ratio_distribution(audit_records, "max_trim_margin_ratio"),
            "despeckle_pixel_ratio": _ratio_distribution(audit_records, "despeckle_pixel_ratio"),
            "tone_changed_pixel_ratio": _ratio_distribution(audit_records, "tone_changed_pixel_ratio"),
            "paper_color_cast_changed_pixel_ratio": _ratio_distribution(
                audit_records,
                "paper_color_cast_changed_pixel_ratio",
            ),
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
            "local_content_change_guard": {
                "checked_files": sum(
                    1 for audit in audit_records if audit.get("local_content_change_guard_checked") is True
                ),
                "reverted_files": sum(
                    1 for audit in audit_records if audit.get("local_content_change_guard_reverted") is True
                ),
                "warning_files": sum(
                    1
                    for audit in audit_records
                    if audit.get("local_content_change_guard_action") in {"reverted_to_source", "warn_review"}
                ),
                "reason_distribution": _reason_counts(
                    reason
                    for audit in audit_records
                    for reason in audit.get("local_content_change_guard_reasons", [])
                    if isinstance(reason, str)
                ),
            },
            "combination_quality_guard": {
                "checked_files": sum(
                    1 for audit in audit_records if audit.get("combination_quality_guard_checked") is True
                ),
                "reverted_files": sum(
                    1 for audit in audit_records if audit.get("combination_quality_guard_reverted") is True
                ),
                "low_confidence_original_files": sum(
                    1
                    for audit in audit_records
                    if audit.get("combination_quality_guard_reason_code") == "low_confidence_original_preserved"
                ),
                "reason_code_distribution": _reason_counts(combination_reason_codes),
                "risk_tier_distribution": _reason_counts(combination_risk_tiers),
                "reason_distribution": _reason_counts(
                    reason
                    for audit in audit_records
                    for reason in audit.get("combination_quality_guard_reasons", [])
                    if isinstance(reason, str)
                ),
            },
            "processed_output_safety_guard": {
                "checked_files": sum(
                    1 for audit in audit_records if audit.get("processed_output_safety_guard_checked") is True
                ),
                "reverted_files": sum(
                    1 for audit in audit_records if audit.get("processed_output_safety_guard_reverted") is True
                ),
                "warning_files": sum(
                    1
                    for audit in audit_records
                    if audit.get("processed_output_safety_guard_action") in {"reverted_to_source", "warn_review"}
                ),
                "reason_code_distribution": _reason_counts(processed_output_reason_codes),
                "reason_distribution": _reason_counts(
                    reason
                    for audit in audit_records
                    for reason in audit.get("processed_output_safety_guard_reasons", [])
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
                "guardrail_reason_code_distribution": _reason_counts(
                    code for code in dark_border_reason_codes if isinstance(code, str)
                ),
                "edge_side_distribution": _reason_counts(side for side in dark_border_edge_sides if isinstance(side, str)),
                "candidate_band_width_bucket_distribution": _reason_counts(
                    bucket for bucket in dark_border_band_width_buckets if isinstance(bucket, str)
                ),
            },
            "scanner_gutter_trim": {
                "trimmed_files": sum(
                    1 for record in processed_records if record.get("scanner_gutter_trimmed") is True
                ),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("scanner_gutter_trimmed") is False
                    and record.get("scanner_gutter_reason") not in {None, "scanner gutter trim disabled"}
                ),
                "reason_distribution": _reason_counts(
                    reason for reason in scanner_gutter_reasons if isinstance(reason, str)
                ),
            },
            "auto_crop": _auto_crop_audit_summary(processed_records, audit_records, auto_crop_reasons),
            "deskew": {
                "corrected_files": sum(1 for record in processed_records if record.get("deskewed") is True),
                "safe_skip_files": _int_count(deskew_timing.get("safe_skip_files")),
                "projection_detection_files": _int_count(deskew_timing.get("projection_detection_files")),
                "fallback_detection_files": _int_count(deskew_timing.get("fallback_detection_files")),
                "safe_skip_reason_code_distribution": (
                    deskew_timing.get("safe_skip_reason_code_distribution")
                    if isinstance(deskew_timing.get("safe_skip_reason_code_distribution"), dict)
                    else {}
                ),
                "safe_skip_source_distribution": (
                    deskew_timing.get("safe_skip_source_distribution")
                    if isinstance(deskew_timing.get("safe_skip_source_distribution"), dict)
                    else {}
                ),
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
            "paper_color_cast": {
                "applied_files": sum(
                    1 for audit in audit_records if audit.get("paper_color_cast_normalized") is True
                ),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("paper_color_cast_normalized") is False
                    and record.get("paper_color_cast_reason")
                    not in {None, "paper color cast normalization disabled"}
                ),
                "color_delta": _aggregate_metric(audit_records, "paper_color_cast_delta"),
                "brightness_delta": _aggregate_metric(audit_records, "paper_color_cast_brightness_delta"),
                "changed_pixel_ratio": _aggregate_metric(
                    audit_records,
                    "paper_color_cast_changed_pixel_ratio",
                ),
                "candidate_pixel_ratio": _aggregate_metric(
                    audit_records,
                    "paper_color_cast_candidate_pixel_ratio",
                ),
                "reason_distribution": _reason_counts(
                    reason for reason in paper_color_cast_reasons if isinstance(reason, str)
                ),
                "skip_reason_distribution": _reason_counts(paper_color_cast_skipped_reasons),
                "reason_code_distribution": _reason_counts(paper_color_cast_reason_codes),
                "skip_reason_code_distribution": _reason_counts(paper_color_cast_skipped_reason_codes),
                "protection_triggered_files": sum(
                    1
                    for code in paper_color_cast_skipped_reason_codes
                    if code
                    in {
                        "protected_color_content",
                        "protected_dark_content",
                        "protected_edge_mark",
                        "protected_photo_or_texture",
                        "colored_paper",
                        "guardrail_reverted",
                    }
                ),
                "conservative_scope_skip_files": sum(
                    1
                    for code in paper_color_cast_skipped_reason_codes
                    if code in {"too_dark", "too_bright", "cast_too_strong", "changed_area_too_large"}
                ),
                "low_confidence_skip_files": sum(
                    1
                    for code in paper_color_cast_skipped_reason_codes
                    if code in {"already_neutral", "low_confidence", "not_uniform"}
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
            "corner_shadows": {
                "applied_files": sum(1 for audit in audit_records if audit.get("corner_shadows_lightened") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("corner_shadows_lightened") is False
                    and record.get("corner_shadows_reason") not in {None, "corner shadow cleanup disabled"}
                ),
                "corner_distribution": _reason_counts(
                    corner
                    for record in processed_records
                    for corner in record.get("corner_shadows_corners", [])
                    if record.get("corner_shadows_lightened") is True and isinstance(corner, str)
                ),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "corner_shadows_changed_pixel_ratio"),
                "candidate_pixel_ratio": _aggregate_metric(audit_records, "corner_shadows_candidate_pixel_ratio"),
                "reason_distribution": _reason_counts(
                    reason for reason in corner_shadows_reasons if isinstance(reason, str)
                ),
                "skip_reason_distribution": _reason_counts(corner_shadows_skipped_reasons),
                "reason_code_distribution": _reason_counts(corner_shadows_reason_codes),
                "skip_reason_code_distribution": _reason_counts(corner_shadows_skipped_reason_codes),
                "protection_triggered_files": sum(
                    1
                    for code in corner_shadows_skipped_reason_codes
                    if code
                    in {
                        "protected_content",
                        "color_content",
                        "texture_or_photo",
                        "detail_too_high",
                        "guardrail_reverted",
                    }
                ),
                "conservative_scope_skip_files": sum(
                    1
                    for code in corner_shadows_skipped_reason_codes
                    if code in {"too_dark", "broad_uneven_lighting", "changed_area_too_large"}
                ),
                "low_confidence_skip_files": sum(
                    1
                    for code in corner_shadows_skipped_reason_codes
                    if code in {"low_confidence", "low_tonal_separation", "no_candidate"}
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
            "fold_shadows": {
                "applied_files": sum(1 for audit in audit_records if audit.get("fold_shadows_lightened") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("fold_shadows_lightened") is False
                    and record.get("fold_shadows_reason") not in {None, "fold shadow cleanup disabled"}
                ),
                "orientation_distribution": _reason_counts(
                    record.get("fold_shadows_orientation")
                    for record in processed_records
                    if record.get("fold_shadows_lightened") is True
                    and isinstance(record.get("fold_shadows_orientation"), str)
                ),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "fold_shadows_changed_pixel_ratio"),
                "candidate_pixel_ratio": _aggregate_metric(audit_records, "fold_shadows_candidate_pixel_ratio"),
                "reason_distribution": _reason_counts(
                    reason for reason in fold_shadows_reasons if isinstance(reason, str)
                ),
                "skip_reason_distribution": _reason_counts(fold_shadows_skipped_reasons),
                "reason_code_distribution": _reason_counts(fold_shadows_reason_codes),
                "skip_reason_code_distribution": _reason_counts(fold_shadows_skipped_reason_codes),
                "protection_triggered_files": sum(
                    1
                    for reason in fold_shadows_skipped_reasons
                    if any(marker in reason for marker in ("risk", "foreground", "edge-adjacent", "too dense"))
                ),
                "conservative_scope_skip_files": sum(
                    1
                    for reason in fold_shadows_skipped_reasons
                    if "conservative" in reason or "broad uneven lighting" in reason
                ),
                "low_confidence_skip_files": sum(
                    1
                    for reason in fold_shadows_skipped_reasons
                    if "no confident" in reason or "below conservative" in reason
                ),
            },
            "illumination_gradient": {
                "applied_files": sum(
                    1 for audit in audit_records if audit.get("illumination_gradient_levelled") is True
                ),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("illumination_gradient_levelled") is False
                    and record.get("illumination_gradient_reason")
                    not in {None, "illumination gradient leveling disabled"}
                ),
                "orientation_distribution": _reason_counts(
                    record.get("illumination_gradient_orientation")
                    for record in processed_records
                    if record.get("illumination_gradient_levelled") is True
                    and isinstance(record.get("illumination_gradient_orientation"), str)
                ),
                "correction_delta": _aggregate_metric(audit_records, "illumination_gradient_correction_delta"),
                "changed_pixel_ratio": _aggregate_metric(
                    audit_records,
                    "illumination_gradient_changed_pixel_ratio",
                ),
                "candidate_pixel_ratio": _aggregate_metric(
                    audit_records,
                    "illumination_gradient_candidate_pixel_ratio",
                ),
                "reason_distribution": _reason_counts(
                    reason for reason in illumination_gradient_reasons if isinstance(reason, str)
                ),
                "skip_reason_distribution": _reason_counts(illumination_gradient_skipped_reasons),
                "reason_code_distribution": _reason_counts(illumination_gradient_reason_codes),
                "skip_reason_code_distribution": _reason_counts(illumination_gradient_skipped_reason_codes),
                "protection_triggered_files": sum(
                    1 for code in illumination_gradient_skipped_reason_codes if code == "protected_content"
                ),
                "not_uniform_skip_files": sum(
                    1 for code in illumination_gradient_skipped_reason_codes if code == "not_uniform"
                ),
                "low_confidence_skip_files": sum(
                    1 for code in illumination_gradient_skipped_reason_codes if code == "low_confidence"
                ),
                "too_strong_skip_files": sum(
                    1 for code in illumination_gradient_skipped_reason_codes if code == "too_strong"
                ),
                "guardrail_reverted_files": sum(
                    1 for code in illumination_gradient_skipped_reason_codes if code == "guardrail_reverted"
                ),
            },
            "bleed_through": {
                "applied_files": sum(1 for audit in audit_records if audit.get("bleed_through_cleaned") is True),
                "skipped_files": sum(
                    1
                    for record in processed_records
                    if record.get("bleed_through_cleaned") is False
                    and record.get("bleed_through_reason") not in {None, "bleed-through cleanup disabled"}
                ),
                "changed_pixel_ratio": _aggregate_metric(audit_records, "bleed_through_changed_pixel_ratio"),
                "candidate_pixel_ratio": _aggregate_metric(audit_records, "bleed_through_candidate_pixel_ratio"),
                "reason_distribution": _reason_counts(
                    record.get("bleed_through_reason")
                    for record in processed_records
                    if isinstance(record.get("bleed_through_reason"), str)
                ),
                "skip_reason_distribution": _reason_counts(
                    record.get("bleed_through_reason")
                    for record in processed_records
                    if record.get("bleed_through_cleaned") is False
                    and isinstance(record.get("bleed_through_reason"), str)
                    and record.get("bleed_through_reason") != "bleed-through cleanup disabled"
                ),
                "protection_triggered_files": sum(
                    1
                    for record in processed_records
                    if record.get("bleed_through_cleaned") is False
                    and isinstance(record.get("bleed_through_reason"), str)
                    and "risk" in record.get("bleed_through_reason", "")
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
                "reason_code_distribution": _reason_counts(faded_text_reason_codes),
                "skip_reason_code_distribution": _reason_counts(faded_text_skipped_reason_codes),
                "skip_reason_zh_distribution": _reason_counts(faded_text_skipped_reasons_zh),
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
                "reason_code_distribution": _reason_counts(text_edges_reason_codes),
                "skip_reason_code_distribution": _reason_counts(text_edges_skipped_reason_codes),
                "skip_reason_zh_distribution": _reason_counts(text_edges_skipped_reasons_zh),
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
            timings[operation].update(_aggregate_despeckle_candidate_counts(records))
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


def _aggregate_deskew_detection_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "safe_skip_files": _operation_flag_count(records, "deskew", "safe_skip"),
        "projection_detection_files": _operation_flag_count(records, "deskew", "projection_detection"),
        "fallback_detection_files": _operation_flag_count(records, "deskew", "fallback_detection"),
        "safe_skip_reason_code_distribution": _operation_value_distribution(
            records,
            "deskew",
            "safe_skip_reason_code",
        ),
        "safe_skip_source_distribution": _operation_value_distribution(records, "deskew", "safe_skip_source"),
    }


def _aggregate_despeckle_candidate_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "safe_skip_files": _operation_flag_count(records, "despeckle", "safe_skip"),
        "replacement_work_files": _operation_flag_count(records, "despeckle", "replacement_work_performed"),
        "reason_code_distribution": _operation_value_distribution(records, "despeckle", "reason_code"),
        "candidate_count_bucket_distribution": _operation_value_distribution(
            records,
            "despeckle",
            "candidate_count_bucket",
        ),
        "candidate_pixels": _operation_numeric_summary(records, "despeckle", "candidate_pixels"),
        "candidate_count": _operation_numeric_summary(records, "despeckle", "candidate_count"),
    }


def _operation_value_distribution(records: list[dict[str, Any]], operation: str, key: str) -> dict[str, int]:
    values = [
        record["operation_timings"][operation].get(key)
        for record in records
        if isinstance(record.get("operation_timings"), dict)
        and isinstance(record["operation_timings"].get(operation), dict)
        and isinstance(record["operation_timings"][operation].get(key), str)
    ]
    return _reason_counts(value for value in values if isinstance(value, str))


def _operation_numeric_summary(records: list[dict[str, Any]], operation: str, key: str) -> dict[str, Any]:
    values = [
        int(record["operation_timings"][operation].get(key))
        for record in records
        if isinstance(record.get("operation_timings"), dict)
        and isinstance(record["operation_timings"].get(operation), dict)
        and isinstance(record["operation_timings"][operation].get(key), int)
    ]
    if not values:
        return {"count": 0, "max": None, "total": 0}
    return {"count": len(values), "max": max(values), "total": sum(values)}


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
        "deskew_safe_skip_reason_code_distribution": _operation_value_distribution(
            records,
            "deskew",
            "safe_skip_reason_code",
        ),
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
        "deskew_safe_skip_reason_code_distribution": {},
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
        "max_local_content_changed_ratio": options.audit_max_local_content_changed_ratio,
        "max_local_content_tile_changed_ratio": options.audit_max_local_content_tile_changed_ratio,
        "max_edge_content_changed_ratio": options.audit_max_edge_content_changed_ratio,
        "max_text_combo_changed_pixel_ratio": options.audit_max_text_combo_changed_pixel_ratio,
        "max_text_combo_local_changed_ratio": options.audit_max_text_combo_local_changed_ratio,
        "max_text_combo_edge_changed_ratio": options.audit_max_text_combo_edge_changed_ratio,
        "max_geometry_combo_crop_ratio": options.audit_max_geometry_combo_crop_ratio,
        "max_geometry_combo_size_change_ratio": options.audit_max_geometry_combo_size_change_ratio,
        "max_processed_near_white_ratio": options.audit_max_processed_near_white_ratio,
        "max_processed_near_white_delta": options.audit_max_processed_near_white_delta,
        "max_processed_highlight_clip_ratio": options.audit_max_processed_highlight_clip_ratio,
        "max_processed_highlight_clip_delta": options.audit_max_processed_highlight_clip_delta,
        "max_processed_bright_page_delta": options.audit_max_processed_bright_page_delta,
        "max_processed_dark_pixel_loss_ratio": options.audit_max_processed_dark_pixel_loss_ratio,
        "max_processed_dark_pixel_lift_ratio": options.audit_max_processed_dark_pixel_lift_ratio,
        "max_processed_full_page_change_ratio": options.audit_max_processed_full_page_change_ratio,
        "max_tone_changed_pixel_ratio": 1.0,
        "max_paper_color_cast_delta": 12.0,
        "max_paper_color_cast_brightness_delta": 4.0,
        "max_paper_color_cast_changed_pixel_ratio": 1.0,
        "max_paper_color_cast_candidate_pixel_ratio": 1.0,
        "max_corner_shadows_changed_pixel_ratio": 0.06,
        "max_corner_shadows_candidate_pixel_ratio": 0.10,
        "max_fold_shadows_changed_pixel_ratio": 0.075,
        "max_fold_shadows_candidate_pixel_ratio": 0.12,
        "max_bleed_through_changed_pixel_ratio": 0.045,
        "max_bleed_through_candidate_pixel_ratio": 0.065,
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
        safe_skip_source = "scan_measurement" if safe_skip_skew is not None else None
        if safe_skip_skew is None and options.deskew:
            safe_skip_skew = _safe_deskew_skip_from_page_evidence(processed)
            safe_skip_source = "page_preflight" if safe_skip_skew is not None else None
        skew = reusable.get("skew") if safe_skip_skew is None else safe_skip_skew
        skew_from_projection = False
        if isinstance(skew, SkewDetection):
            if safe_skip_source in {None, "scan_measurement"}:
                operations.append("skew_detect_reused_scan_measurement")
                operation_timings.setdefault("deskew", {})["reused_scan_measurement"] = True
            if safe_skip_skew is not None:
                safe_skip_operation = (
                    "deskew_safe_skip_scan_measurement"
                    if safe_skip_source == "scan_measurement"
                    else "deskew_safe_skip_page_preflight"
                )
                operations.append(safe_skip_operation)
                timing = operation_timings.setdefault("deskew", {})
                timing["safe_skip_reason"] = skew.reason
                timing["safe_skip_reason_code"] = _deskew_safe_skip_reason_code(skew.reason)
                timing["safe_skip_source"] = safe_skip_source or "unknown"
                timing["safe_skip"] = True
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
        elif skew_from_projection and _deskew_has_edge_content_risk(processed):
            operations.append("deskew_noop")
            deskew_reason = "edge content near rotation boundary"
        elif abs(skew.angle_degrees) <= 1.25 and _deskew_has_color_or_table_risk(processed):
            operations.append("deskew_noop")
            deskew_reason = "table or color mark rotation risk"
        elif abs(skew.angle_degrees) < 0.2:
            operations.append("deskew_noop")
            deskew_reason = "angle below correction threshold"
        else:
            processed = _rotate_for_deskew(processed, -skew.angle_degrees)
            operations.append("deskew_conservative")
            post_deskew_size = list(processed.size)
            deskewed = True
            deskew_reason = "deskew applied"

    dark_border = DarkBorderDetection(
        None,
        "dark border trim disabled",
        _dark_border_reason_code("dark border trim disabled"),
    )
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

    scanner_gutter = ScannerGutterTrimDetection(
        None,
        "scanner gutter trim disabled",
        {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0},
    )
    scanner_gutter_trimmed = False
    with _operation_timer(operation_timings, "scanner_gutter_trim", enabled=options.scanner_gutter_trim):
        if options.scanner_gutter_trim:
            scanner_gutter = _detect_light_scanner_gutter_bbox(processed)
            if scanner_gutter.bbox:
                processed = processed.crop(scanner_gutter.bbox)
                operations.append("scanner_gutter_trim_conservative")
                scanner_gutter_trimmed = True
            else:
                operations.append("scanner_gutter_trim_noop")
        else:
            operations.append("scanner_gutter_trim_disabled")

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
            despeckle_result = _despeckle_isolated_pixels_with_reason(
                processed,
                backend=options.despeckle_backend,
            )
            processed = despeckle_result.image
            despeckle_pixels_changed = despeckle_result.changed_pixels
            despeckle_backend_mode = despeckle_result.backend_mode
            despeckle_reason = despeckle_result.reason
            despeckle_timing = operation_timings.setdefault("despeckle", {})
            despeckle_timing["reason_code"] = despeckle_result.reason_code
            despeckle_timing["candidate_pixels"] = despeckle_result.candidate_pixels
            despeckle_timing["candidate_count"] = despeckle_result.candidate_count
            despeckle_timing["candidate_count_bucket"] = despeckle_result.candidate_count_bucket
            despeckle_timing["replacement_work_performed"] = despeckle_result.replacement_work_performed
            despeckle_timing["safe_skip"] = despeckle_result.changed_pixels == 0
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

    paper_color_cast = PaperColorCastNormalizationResult(
        processed,
        False,
        "paper color cast normalization disabled",
        "disabled",
        0.0,
        0.0,
        0.0,
        0.0,
    )
    with _operation_timer(
        operation_timings,
        "normalize_paper_color_cast",
        enabled=options.normalize_paper_color_cast,
    ):
        if options.normalize_paper_color_cast:
            paper_color_cast = _normalize_paper_color_cast_conservative(processed)
            processed = paper_color_cast.image
            operations.append(
                "normalize_paper_color_cast_conservative"
                if paper_color_cast.applied
                else "normalize_paper_color_cast_noop"
            )
        else:
            operations.append("normalize_paper_color_cast_disabled")

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

    corner_shadows = CornerShadowCleanupResult(
        processed, False, "corner shadow cleanup disabled", "disabled", (), None, None, 0.0, 0.0, 0.0
    )
    with _operation_timer(operation_timings, "lighten_corner_shadows", enabled=options.lighten_corner_shadows):
        if options.lighten_corner_shadows:
            corner_shadows = _lighten_corner_shadows_conservative(processed)
            processed = corner_shadows.image
            operations.append(
                "lighten_corner_shadows_conservative"
                if corner_shadows.applied
                else "lighten_corner_shadows_noop"
            )
        else:
            operations.append("lighten_corner_shadows_disabled")

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

    fold_shadows = FoldShadowCleanupResult(
        processed, False, "fold shadow cleanup disabled", None, 0, None, None, 0.0, 0.0, 0.0
    )
    with _operation_timer(operation_timings, "lighten_fold_shadows", enabled=options.lighten_fold_shadows):
        if options.lighten_fold_shadows:
            fold_shadows = _lighten_fold_shadows_conservative(processed)
            processed = fold_shadows.image
            operations.append(
                "lighten_fold_shadows_conservative" if fold_shadows.applied else "lighten_fold_shadows_noop"
            )
        else:
            operations.append("lighten_fold_shadows_disabled")

    illumination_gradient = IlluminationGradientLevelingResult(
        processed,
        False,
        "illumination gradient leveling disabled",
        "disabled",
        None,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    with _operation_timer(
        operation_timings,
        "level_illumination_gradient",
        enabled=options.level_illumination_gradient,
    ):
        if options.level_illumination_gradient:
            illumination_gradient = _level_illumination_gradient_conservative(processed)
            processed = illumination_gradient.image
            operations.append(
                "level_illumination_gradient_conservative"
                if illumination_gradient.applied
                else "level_illumination_gradient_noop"
            )
        else:
            operations.append("level_illumination_gradient_disabled")

    bleed_through = BleedThroughCleanupResult(
        processed, False, "bleed-through cleanup disabled", None, None, 0.0, 0.0, 0.0
    )
    with _operation_timer(operation_timings, "clean_bleed_through", enabled=options.clean_bleed_through):
        if options.clean_bleed_through:
            bleed_through = _clean_bleed_through_conservative(processed)
            processed = bleed_through.image
            operations.append(
                "clean_bleed_through_conservative" if bleed_through.applied else "clean_bleed_through_noop"
            )
        else:
            operations.append("clean_bleed_through_disabled")

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
        scanner_gutter.bbox,
        skew.angle_degrees,
        despeckle_pixels_changed,
        tone.applied,
        tone.background_before,
        tone.background_after,
        tone.contrast_before,
        tone.contrast_after,
        tone.changed_pixel_ratio,
        paper_color_cast.applied,
        paper_color_cast.color_delta,
        paper_color_cast.brightness_delta,
        paper_color_cast.changed_pixel_ratio,
        paper_color_cast.candidate_pixel_ratio,
        edge_shadow.applied,
        edge_shadow.edge_delta,
        edge_shadow.changed_pixel_ratio,
        edge_shadow.candidate_pixel_ratio,
        corner_shadows.applied,
        corner_shadows.corner_delta,
        corner_shadows.changed_pixel_ratio,
        corner_shadows.candidate_pixel_ratio,
        background_stains.applied,
        background_stains.stain_delta,
        background_stains.changed_pixel_ratio,
        background_stains.candidate_pixel_ratio,
        fold_shadows.applied,
        fold_shadows.band_delta,
        fold_shadows.changed_pixel_ratio,
        fold_shadows.candidate_pixel_ratio,
        illumination_gradient.applied,
        illumination_gradient.correction_delta,
        illumination_gradient.changed_pixel_ratio,
        illumination_gradient.candidate_pixel_ratio,
        bleed_through.applied,
        bleed_through.ghost_delta,
        bleed_through.changed_pixel_ratio,
        bleed_through.candidate_pixel_ratio,
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
    local_content_guard = {
        "checked": attempted_audit.get("local_content_change_guard_checked") is True,
        "action": attempted_audit.get("local_content_change_guard_action", "passed"),
        "reverted": attempted_audit.get("local_content_change_guard_reverted") is True,
        "reasons": attempted_audit.get("local_content_change_guard_reasons", []),
        "content_pixel_ratio": attempted_audit.get("local_content_pixel_ratio", 0.0),
        "changed_ratio": attempted_audit.get("local_content_changed_ratio", 0.0),
        "tile_changed_ratio": attempted_audit.get("local_content_tile_changed_ratio", 0.0),
        "edge_changed_ratio": attempted_audit.get("edge_content_changed_ratio", 0.0),
    }
    combination_guard = _combination_quality_guard(
        attempted_audit,
        options,
        cumulative_change_guard=cumulative_guard,
        local_content_change_guard=local_content_guard,
        low_confidence_original_preserved=_low_confidence_original_preserved(
            applied_flags=(
                deskewed,
                dark_border_trimmed,
                scanner_gutter_trimmed,
                crop_bbox is not None,
                despeckled,
                tone.applied,
                edge_shadow.applied,
                corner_shadows.applied,
                background_stains.applied,
                fold_shadows.applied,
                illumination_gradient.applied,
                bleed_through.applied,
                scanlines.applied,
                faded_text.applied,
                text_edges.applied,
                paper_color_cast.applied,
            ),
            reasons=(
                deskew_reason,
                dark_border.reason,
                scanner_gutter.reason,
                crop_reason,
                despeckle_reason,
                tone.reason,
                edge_shadow.reason,
                corner_shadows.reason,
                background_stains.reason,
                fold_shadows.reason,
                illumination_gradient.reason,
                bleed_through.reason,
                scanlines.reason,
                faded_text.reason,
                text_edges.reason,
                paper_color_cast.reason,
            ),
        ),
    )
    processed_output_guard = _processed_output_safety_guard(attempted_audit, options)
    local_content_guard_reverted = local_content_guard["action"] == "reverted_to_source"
    cumulative_guard_reverted = cumulative_guard["action"] == "reverted_to_source"
    combination_guard_reverted = combination_guard["action"] == "reverted_to_source"
    processed_output_guard_reverted = processed_output_guard["action"] == "reverted_to_source"
    guard_reverted = (
        local_content_guard_reverted
        or cumulative_guard_reverted
        or combination_guard_reverted
        or processed_output_guard_reverted
    )
    if guard_reverted:
        processed = audit_source.copy()
        if local_content_guard_reverted:
            operations.append("local_content_change_guard_reverted_to_source")
        if cumulative_guard_reverted:
            operations.append("cumulative_change_guard_reverted_to_source")
        if combination_guard_reverted:
            operations.append("combination_quality_guard_reverted_to_source")
        if processed_output_guard_reverted:
            operations.append("processed_output_safety_guard_reverted_to_source")
        processing_audit = _processing_audit(
            audit_source,
            processed,
            options,
            None,
            None,
            None,
            None,
            0,
            cumulative_change_guard=cumulative_guard,
            local_content_change_guard=local_content_guard,
            combination_quality_guard=combination_guard,
            processed_output_safety_guard=processed_output_guard,
        )
    else:
        processing_audit = {
            **attempted_audit,
            **_cumulative_change_guard_audit_fields(cumulative_guard),
            **_combination_quality_guard_audit_fields(combination_guard),
            **_processed_output_safety_guard_audit_fields(processed_output_guard),
        }
    processing_warnings = list(processing_audit["guardrail_failures"])
    if local_content_guard_reverted:
        processing_warnings.append("local_content_change_guard_reverted_to_source")
    if cumulative_guard["action"] == "reverted_to_source":
        processing_warnings.append("cumulative_change_guard_reverted_to_source")
    if combination_guard["action"] == "reverted_to_source":
        processing_warnings.append("combination_quality_guard_reverted_to_source")
    if processed_output_guard["action"] == "reverted_to_source":
        processing_warnings.append("processed_output_safety_guard_reverted_to_source")
    guard_reason = _guard_revert_reason(
        local_content_guard_reverted,
        combination_guard.get("reason_code") if isinstance(combination_guard, dict) else None,
        processed_output_guard.get("reason_code") if isinstance(processed_output_guard, dict) else None,
    )
    faded_text_reason = guard_reason if guard_reverted else faded_text.reason
    faded_text_reason_code = _faded_text_reason_code(faded_text_reason)
    faded_text_reason_zh = _faded_text_reason_zh(faded_text_reason_code)
    text_edges_reason = guard_reason if guard_reverted else text_edges.reason
    text_edges_reason_code = _text_edges_reason_code(text_edges_reason)
    text_edges_reason_zh = _text_edges_reason_zh(text_edges_reason_code)
    crop_info = {
        "original_size": original_size,
        "output_size": list(processed.size),
        "pre_deskew_size": original_size if guard_reverted else pre_deskew_size,
        "post_deskew_size": original_size if guard_reverted else post_deskew_size,
        "skew_angle_degrees": None if guard_reverted else skew.angle_degrees,
        "skew_confidence": skew.confidence,
        "deskewed": False if guard_reverted else deskewed,
        "deskew_reason": guard_reason if guard_reverted else deskew_reason,
        "dark_border_trimmed": False if guard_reverted else dark_border_trimmed,
        "dark_border_bbox": None if guard_reverted else (list(dark_border.bbox) if dark_border.bbox else None),
        "dark_border_reason": guard_reason if guard_reverted else dark_border.reason,
        "dark_border_reason_code": (
            _dark_border_reason_code(guard_reason) if guard_reverted else dark_border.reason_code
        ),
        "dark_border_edge_sides": [] if guard_reverted else list(dark_border.edge_sides),
        "dark_border_band_width_bucket": None if guard_reverted else dark_border.band_width_bucket,
        "scanner_gutter_trimmed": False if guard_reverted else scanner_gutter_trimmed,
        "scanner_gutter_bbox": None if guard_reverted else (list(scanner_gutter.bbox) if scanner_gutter.bbox else None),
        "scanner_gutter_reason": guard_reason if guard_reverted else scanner_gutter.reason,
        "scanner_gutter_trim_margins": (
            {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}
            if guard_reverted
            else scanner_gutter.margins
        ),
        "crop_bbox": None if guard_reverted else (list(crop_bbox) if crop_bbox else None),
        "crop_reason": guard_reason if guard_reverted else crop_reason,
        "cropped": False if guard_reverted else crop_bbox is not None,
        "despeckled": False if guard_reverted else despeckled,
        "despeckle_pixels_changed": 0 if guard_reverted else despeckle_pixels_changed,
        "despeckle_reason": guard_reason if guard_reverted else despeckle_reason,
        "despeckle_backend_mode": despeckle_backend_mode,
        "tone_normalized": False if guard_reverted else tone.applied,
        "tone_reason": guard_reason if guard_reverted else tone.reason,
        "tone_background_before": None if guard_reverted else tone.background_before,
        "tone_background_after": None if guard_reverted else tone.background_after,
        "tone_contrast_before": None if guard_reverted else tone.contrast_before,
        "tone_contrast_after": None if guard_reverted else tone.contrast_after,
        "tone_changed_pixel_ratio": 0.0 if guard_reverted else tone.changed_pixel_ratio,
        "paper_color_cast_normalized": False if guard_reverted else paper_color_cast.applied,
        "paper_color_cast_reason": guard_reason if guard_reverted else paper_color_cast.reason,
        "paper_color_cast_reason_code": (
            "guardrail_reverted" if guard_reverted else paper_color_cast.reason_code
        ),
        "paper_color_cast_delta": 0.0 if guard_reverted else paper_color_cast.color_delta,
        "paper_color_cast_brightness_delta": 0.0 if guard_reverted else paper_color_cast.brightness_delta,
        "paper_color_cast_changed_pixel_ratio": 0.0 if guard_reverted else paper_color_cast.changed_pixel_ratio,
        "paper_color_cast_candidate_pixel_ratio": (
            0.0 if guard_reverted else paper_color_cast.candidate_pixel_ratio
        ),
        "edge_shadow_lightened": False if guard_reverted else edge_shadow.applied,
        "edge_shadow_reason": guard_reason if guard_reverted else edge_shadow.reason,
        "edge_shadow_edges": list(edge_shadow.edges),
        "edge_shadow_mean_before": None if guard_reverted else edge_shadow.edge_mean_before,
        "edge_shadow_mean_after": None if guard_reverted else edge_shadow.edge_mean_after,
        "edge_shadow_delta": 0.0 if guard_reverted else edge_shadow.edge_delta,
        "edge_shadow_changed_pixel_ratio": 0.0 if guard_reverted else edge_shadow.changed_pixel_ratio,
        "edge_shadow_candidate_pixel_ratio": 0.0
        if guard_reverted
        else edge_shadow.candidate_pixel_ratio,
        "corner_shadows_lightened": False if guard_reverted else corner_shadows.applied,
        "corner_shadows_reason": guard_reason if guard_reverted else corner_shadows.reason,
        "corner_shadows_reason_code": (
            "guardrail_reverted" if guard_reverted else corner_shadows.reason_code
        ),
        "corner_shadows_corners": list(corner_shadows.corners),
        "corner_shadows_mean_before": None if guard_reverted else corner_shadows.corner_mean_before,
        "corner_shadows_mean_after": None if guard_reverted else corner_shadows.corner_mean_after,
        "corner_shadows_delta": 0.0 if guard_reverted else corner_shadows.corner_delta,
        "corner_shadows_changed_pixel_ratio": 0.0 if guard_reverted else corner_shadows.changed_pixel_ratio,
        "corner_shadows_candidate_pixel_ratio": 0.0 if guard_reverted else corner_shadows.candidate_pixel_ratio,
        "background_stains_lightened": False if guard_reverted else background_stains.applied,
        "background_stains_reason": guard_reason if guard_reverted else background_stains.reason,
        "background_stains_mean_before": None if guard_reverted else background_stains.stain_mean_before,
        "background_stains_mean_after": None if guard_reverted else background_stains.stain_mean_after,
        "background_stains_delta": 0.0 if guard_reverted else background_stains.stain_delta,
        "background_stains_changed_pixel_ratio": 0.0 if guard_reverted else background_stains.changed_pixel_ratio,
        "background_stains_candidate_pixel_ratio": 0.0 if guard_reverted else background_stains.candidate_pixel_ratio,
        "fold_shadows_lightened": False if guard_reverted else fold_shadows.applied,
        "fold_shadows_reason": guard_reason if guard_reverted else fold_shadows.reason,
        "fold_shadows_reason_code": _fold_shadows_reason_code(guard_reason if guard_reverted else fold_shadows.reason),
        "fold_shadows_orientation": fold_shadows.orientation,
        "fold_shadows_count": 0 if guard_reverted else fold_shadows.band_count,
        "fold_shadows_mean_before": None if guard_reverted else fold_shadows.band_mean_before,
        "fold_shadows_mean_after": None if guard_reverted else fold_shadows.band_mean_after,
        "fold_shadows_delta": 0.0 if guard_reverted else fold_shadows.band_delta,
        "fold_shadows_changed_pixel_ratio": 0.0 if guard_reverted else fold_shadows.changed_pixel_ratio,
        "fold_shadows_candidate_pixel_ratio": 0.0 if guard_reverted else fold_shadows.candidate_pixel_ratio,
        "illumination_gradient_levelled": False if guard_reverted else illumination_gradient.applied,
        "illumination_gradient_reason": guard_reason if guard_reverted else illumination_gradient.reason,
        "illumination_gradient_reason_code": (
            "guardrail_reverted" if guard_reverted else illumination_gradient.reason_code
        ),
        "illumination_gradient_orientation": illumination_gradient.orientation,
        "illumination_gradient_delta_before": 0.0
        if guard_reverted
        else illumination_gradient.gradient_delta_before,
        "illumination_gradient_delta_after": 0.0 if guard_reverted else illumination_gradient.gradient_delta_after,
        "illumination_gradient_correction_delta": 0.0 if guard_reverted else illumination_gradient.correction_delta,
        "illumination_gradient_changed_pixel_ratio": 0.0
        if guard_reverted
        else illumination_gradient.changed_pixel_ratio,
        "illumination_gradient_candidate_pixel_ratio": 0.0
        if guard_reverted
        else illumination_gradient.candidate_pixel_ratio,
        "bleed_through_cleaned": False if guard_reverted else bleed_through.applied,
        "bleed_through_reason": guard_reason if guard_reverted else bleed_through.reason,
        "bleed_through_mean_before": None if guard_reverted else bleed_through.ghost_mean_before,
        "bleed_through_mean_after": None if guard_reverted else bleed_through.ghost_mean_after,
        "bleed_through_delta": 0.0 if guard_reverted else bleed_through.ghost_delta,
        "bleed_through_changed_pixel_ratio": 0.0 if guard_reverted else bleed_through.changed_pixel_ratio,
        "bleed_through_candidate_pixel_ratio": 0.0 if guard_reverted else bleed_through.candidate_pixel_ratio,
        "scanlines_lightened": False if guard_reverted else scanlines.applied,
        "scanlines_reason": guard_reason if guard_reverted else scanlines.reason,
        "scanlines_orientation": scanlines.orientation,
        "scanlines_count": 0 if guard_reverted else scanlines.line_count,
        "scanlines_mean_before": None if guard_reverted else scanlines.line_mean_before,
        "scanlines_mean_after": None if guard_reverted else scanlines.line_mean_after,
        "scanlines_delta": 0.0 if guard_reverted else scanlines.line_delta,
        "scanlines_changed_pixel_ratio": 0.0 if guard_reverted else scanlines.changed_pixel_ratio,
        "scanlines_candidate_pixel_ratio": 0.0 if guard_reverted else scanlines.candidate_pixel_ratio,
        "faded_text_enhanced": False if guard_reverted else faded_text.applied,
        "faded_text_reason": faded_text_reason,
        "faded_text_reason_code": faded_text_reason_code,
        "faded_text_reason_zh": faded_text_reason_zh,
        "faded_text_delta": 0.0 if guard_reverted else faded_text.text_delta,
        "faded_text_changed_pixel_ratio": 0.0 if guard_reverted else faded_text.changed_pixel_ratio,
        "faded_text_candidate_pixel_ratio": 0.0 if guard_reverted else faded_text.candidate_pixel_ratio,
        "text_edges_sharpened": False if guard_reverted else text_edges.applied,
        "text_edges_reason": text_edges_reason,
        "text_edges_reason_code": text_edges_reason_code,
        "text_edges_reason_zh": text_edges_reason_zh,
        "text_edges_delta": 0.0 if guard_reverted else text_edges.edge_delta,
        "text_edges_changed_pixel_ratio": 0.0 if guard_reverted else text_edges.changed_pixel_ratio,
        "text_edges_candidate_pixel_ratio": 0.0 if guard_reverted else text_edges.candidate_pixel_ratio,
        "processing_audit": processing_audit,
        "processing_warnings": processing_warnings,
        "operation_timings": operation_timings,
        "scan_measurements_reused": any(
            timing.get("reused_scan_measurement") is True for timing in operation_timings.values()
        ),
    }
    return processed, operations, crop_info


def _guard_revert_reason(
    local_content_guard_reverted: bool,
    combination_reason_code: Any = None,
    processed_output_reason_code: Any = None,
) -> str:
    if local_content_guard_reverted:
        return "reverted by local content change guard"
    if processed_output_reason_code == "processed_output_quality_reverted":
        return "reverted by processed output safety guard"
    if combination_reason_code == "geometric_risk_reverted":
        return "reverted by geometric combination guard"
    if combination_reason_code == "text_high_frequency_risk_reverted":
        return "reverted by text high-frequency combination guard"
    if combination_reason_code == "combined_change_too_large_reverted":
        return "reverted by combined change guard"
    return "reverted by cumulative change guard"


def _low_confidence_original_preserved(
    *,
    applied_flags: tuple[bool, ...],
    reasons: tuple[str | None, ...],
) -> bool:
    if any(applied_flags):
        return False
    return any(_is_low_confidence_processing_reason(reason) for reason in reasons)


def _is_low_confidence_processing_reason(reason: str | None) -> bool:
    if not isinstance(reason, str):
        return False
    normalized = reason.lower()
    return any(
        marker in normalized
        for marker in (
            "low confidence",
            "low-confidence",
            "too weak",
            "too sparse",
            "insufficient",
            "no stable",
            "no confident",
            "below correction threshold",
            "candidate crop change is too small",
        )
    )


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


def _normalize_paper_color_cast_conservative(image: Image.Image) -> PaperColorCastNormalizationResult:
    if image.width < 60 or image.height < 60:
        return _paper_color_cast_noop(image, "paper color cast normalization skipped: image too small", "too_small")
    if image.mode == "L":
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: grayscale image",
            "not_rgb",
        )

    source = image.convert("RGB")
    sample = source.copy()
    sample.thumbnail((420, 420), Image.Resampling.BILINEAR)
    pixels = list(sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata())
    total = max(1, len(pixels))
    brightness_values = [sum(pixel) / 3 for pixel in pixels]
    brightness_mean = sum(brightness_values) / total
    brightness_std = _mean_stddev(brightness_values, brightness_mean)
    brightness_sorted = sorted(brightness_values)
    p05 = brightness_sorted[min(total - 1, int(total * 0.05))]
    p95 = brightness_sorted[min(total - 1, int(total * 0.95))]
    means = [sum(pixel[index] for pixel in pixels) / total for index in range(3)]
    protected_reason = _paper_color_cast_protection_reason(sample, means)
    if protected_reason:
        return _paper_color_cast_noop(image, protected_reason[0], protected_reason[1])
    paper_pixels = [
        pixel
        for pixel, brightness in zip(pixels, brightness_values, strict=False)
        if brightness >= brightness_mean - 45
    ]
    if len(paper_pixels) / total >= 0.90:
        pixels = paper_pixels
        total = max(1, len(pixels))
        brightness_values = [sum(pixel) / 3 for pixel in pixels]
        brightness_mean = sum(brightness_values) / total
        brightness_std = _mean_stddev(brightness_values, brightness_mean)
        brightness_sorted = sorted(brightness_values)
        p05 = brightness_sorted[min(total - 1, int(total * 0.05))]
        p95 = brightness_sorted[min(total - 1, int(total * 0.95))]
        means = [sum(pixel[index] for pixel in pixels) / total for index in range(3)]
    if brightness_mean < 218 or p05 < 205:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: page is too dark for scanner-cast correction",
            "too_dark",
        )
    if p95 > 252 and p05 > 244:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: page appears overexposed",
            "too_bright",
        )
    if brightness_std > 10.5 or p95 - p05 > 28:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: background is not uniform enough",
            "not_uniform",
        )

    channel_spread = max(means) - min(means)
    cool_cast_evidence = means[0] == min(means) and max(means[1], means[2]) - means[0] >= 2.5
    min_cast_spread = 2.5 if cool_cast_evidence and brightness_mean >= 240 else 4.0
    if channel_spread < min_cast_spread:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: already near neutral",
            "already_neutral",
        )
    if cool_cast_evidence and brightness_mean < 240 and channel_spread >= 8.0:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: colored paper or strong color evidence",
            "colored_paper",
        )
    if channel_spread > 18.0 or min(means) < 212:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: colored paper or strong color evidence",
            "colored_paper",
        )

    dominant_spreads = [max(pixel) - min(pixel) for pixel in pixels if sum(pixel) / 3 >= 205]
    if not dominant_spreads:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: low-confidence paper evidence",
            "low_confidence",
        )
    spread_mean = sum(dominant_spreads) / len(dominant_spreads)
    spread_std = _mean_stddev(dominant_spreads, spread_mean)
    if spread_std > 4.8:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: color cast is not uniform",
            "not_uniform",
        )

    target = sum(means) / 3
    offsets = [max(-9.0, min(9.0, target - mean)) for mean in means]
    min_offset = 1.25 if cool_cast_evidence and channel_spread < 4.0 else 2.0
    if max(abs(offset) for offset in offsets) < min_offset:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: correction below conservative threshold",
            "low_confidence",
        )

    protected_mask = _paper_color_cast_protected_mask(source, means)
    unprotected_count = source.width * source.height - _mask_pixel_count(protected_mask)
    candidate_ratio = unprotected_count / max(1, source.width * source.height)
    if candidate_ratio < 0.90:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: protected content occupies too much of the page",
            "protected_dark_content",
            candidate_ratio,
        )

    output = source.copy()
    output_pixels = output.load()
    protected_pixels = protected_mask.load()
    changed = 0
    for y in range(output.height):
        for x in range(output.width):
            if protected_pixels[x, y]:
                continue
            red_value, green_value, blue_value = output_pixels[x, y]
            values = (red_value, green_value, blue_value)
            corrected = tuple(max(0, min(255, int(round(value + offsets[index])))) for index, value in enumerate(values))
            if max(abs(corrected[index] - values[index]) for index in range(3)) > 1:
                changed += 1
            output_pixels[x, y] = corrected

    changed_ratio = changed / max(1, source.width * source.height)
    if changed_ratio < 0.85:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: low-confidence uniform paper area",
            "low_confidence",
            candidate_ratio,
        )
    if changed_ratio > 1.0:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: changed area exceeds conservative scope",
            "changed_area_too_large",
            candidate_ratio,
        )

    before_l = source.convert("L")
    after_l = output.convert("L")
    brightness_delta, _contrast_delta = _tonal_deltas(before_l, after_l)
    if brightness_delta > 4.0:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: brightness delta exceeds conservative threshold",
            "cast_too_strong",
            candidate_ratio,
        )
    after_sample = output.copy()
    after_sample.thumbnail(sample.size, Image.Resampling.BILINEAR)
    after_pixels = list(
        after_sample.get_flattened_data() if hasattr(after_sample, "get_flattened_data") else after_sample.getdata()
    )
    after_means = [sum(pixel[index] for pixel in after_pixels) / max(1, len(after_pixels)) for index in range(3)]
    after_spread = max(after_means) - min(after_means)
    color_delta = channel_spread - after_spread
    min_color_delta = 2.0 if cool_cast_evidence and channel_spread < 4.0 else 3.0
    if color_delta < min_color_delta or color_delta > 12.0:
        return _paper_color_cast_noop(
            image,
            "paper color cast normalization skipped: color delta outside conservative threshold",
            "cast_too_strong" if color_delta > 12.0 else "low_confidence",
            candidate_ratio,
        )
    return PaperColorCastNormalizationResult(
        output,
        True,
        "paper color cast normalization applied: mild uniform scanner cast on bright neutral paper",
        "applied_mild_uniform_scanner_cast",
        round(color_delta, 6),
        round(brightness_delta, 6),
        round(changed_ratio, 6),
        round(candidate_ratio, 6),
    )


def _paper_color_cast_protection_reason(
    sample: Image.Image,
    background_means: list[float],
) -> tuple[str, str] | None:
    pixels = list(sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata())
    total = max(1, len(pixels))
    bg_brightness = sum(background_means) / 3
    colored = 0
    red_or_blue = 0
    dark = 0
    texture = 0
    for red_value, green_value, blue_value in pixels:
        brightness = (red_value + green_value + blue_value) / 3
        spread = max(red_value, green_value, blue_value) - min(red_value, green_value, blue_value)
        bg_distance = max(abs(red_value - background_means[0]), abs(green_value - background_means[1]), abs(blue_value - background_means[2]))
        if brightness < bg_brightness - 45:
            dark += 1
        if red_value >= 110 and red_value - green_value >= 34 and red_value - blue_value >= 34:
            red_or_blue += 1
        if blue_value >= 105 and blue_value - red_value >= 30 and blue_value - green_value >= 18:
            red_or_blue += 1
        if spread > 24 and bg_distance > 18 and 40 < brightness < 248:
            colored += 1
        if bg_distance > 32 and brightness >= 120:
            texture += 1
    if red_or_blue / total >= 0.00035 or colored / total >= 0.0012:
        if not _paper_color_cast_tiny_color_mark_is_safe(sample, background_means):
            return (
                "paper color cast normalization skipped: protected color content, stamp, seal, map, chart, or annotation risk",
                "protected_color_content",
            )
    if dark / total >= 0.004 and not _paper_color_cast_sparse_dark_content_is_safe(sample, background_means):
        return (
            "paper color cast normalization skipped: protected handwriting, text, photograph, or archival mark risk",
            "protected_dark_content",
        )
    if texture / total >= 0.012:
        return (
            "paper color cast normalization skipped: photo, chart, map, texture, or colored record risk",
            "protected_photo_or_texture",
        )
    if _source_protected_edge_dark_ratio(sample.convert("L")) > 0.001:
        return (
            "paper color cast normalization skipped: protected edge or corner archival mark risk",
            "protected_edge_mark",
        )
    return None


def _paper_color_cast_tiny_color_mark_is_safe(sample: Image.Image, background_means: list[float]) -> bool:
    width, height = sample.size
    total = max(1, width * height)
    color_mask = Image.new("L", sample.size, 0)
    source_pixels = sample.load()
    mask_pixels = color_mask.load()
    for y in range(height):
        for x in range(width):
            red_value, green_value, blue_value = source_pixels[x, y]
            brightness = (red_value + green_value + blue_value) / 3
            spread = max(red_value, green_value, blue_value) - min(red_value, green_value, blue_value)
            bg_distance = max(
                abs(red_value - background_means[0]),
                abs(green_value - background_means[1]),
                abs(blue_value - background_means[2]),
            )
            red_mark = red_value >= 110 and red_value - green_value >= 34 and red_value - blue_value >= 34
            blue_mark = blue_value >= 105 and blue_value - red_value >= 30 and blue_value - green_value >= 18
            protected_color = spread > 24 and bg_distance > 18 and 40 < brightness < 248
            if (red_mark or blue_mark) and protected_color:
                mask_pixels[x, y] = 255

    colored_pixels = _mask_pixel_count(color_mask)
    colored_ratio = colored_pixels / total
    if colored_pixels < 8 or colored_ratio > 0.0025:
        return False
    components = [component for component in _mask_components(color_mask) if len(component) >= 3]
    if len(components) != 1:
        return False
    component = components[0]
    xs = [point[0] for point in component]
    ys = [point[1] for point in component]
    left = min(xs)
    top = min(ys)
    right = max(xs)
    bottom = max(ys)
    component_width = right - left + 1
    component_height = bottom - top + 1
    component_area = len(component)
    bbox_area = component_width * component_height
    edge_margin = max(4, int(round(min(width, height) * 0.06)))
    if left < edge_margin or top < edge_margin or right >= width - edge_margin or bottom >= height - edge_margin:
        return False
    if component_area > total * 0.002 or component_width > width * 0.12 or component_height > height * 0.12:
        return False
    if component_area / max(1, bbox_area) < 0.12:
        return False
    protected_mask = _paper_color_cast_protected_mask(sample, background_means)
    protected_overlap = _mask_intersection_count(color_mask, protected_mask)
    return protected_overlap == colored_pixels


def _paper_color_cast_sparse_dark_content_is_safe(sample: Image.Image, background_means: list[float]) -> bool:
    bg_brightness = sum(background_means) / 3
    dark = sample.convert("L").point(lambda value: 255 if value < bg_brightness - 45 else 0, mode="L")
    dark_ratio = _mask_pixel_count(dark) / max(1, sample.width * sample.height)
    if dark_ratio < 0.004:
        return True
    if dark_ratio > 0.08:
        return False
    if _source_protected_edge_dark_ratio(sample.convert("L")) > 0.001:
        return False

    components = [component for component in _mask_components(dark) if len(component) >= 8]
    if not components:
        return False
    safe_components = 0
    for component in components:
        area = len(component)
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        width = max(xs) - min(xs) + 1
        height = max(ys) - min(ys) + 1
        density = area / max(1, width * height)
        aspect_ratio = width / max(1, height)
        if area > sample.width * sample.height * 0.035:
            return False
        if width >= 8 and height <= 8 and aspect_ratio >= 2.0 and density >= 0.35:
            safe_components += 1
            continue
        if area <= 90 and width <= 28 and height <= 18 and density >= 0.20:
            safe_components += 1
            continue
        return False
    return safe_components >= 1


def _paper_color_cast_protected_mask(source: Image.Image, background_means: list[float]) -> Image.Image:
    mask = Image.new("L", source.size, 0)
    pixels = source.load()
    mask_pixels = mask.load()
    bg_brightness = sum(background_means) / 3
    for y in range(source.height):
        for x in range(source.width):
            red_value, green_value, blue_value = pixels[x, y]
            brightness = (red_value + green_value + blue_value) / 3
            spread = max(red_value, green_value, blue_value) - min(red_value, green_value, blue_value)
            bg_distance = max(
                abs(red_value - background_means[0]),
                abs(green_value - background_means[1]),
                abs(blue_value - background_means[2]),
            )
            if brightness < bg_brightness - 45 or (spread > 24 and bg_distance > 18):
                mask_pixels[x, y] = 255
    return mask.filter(ImageFilter.MaxFilter(5))


def _paper_color_cast_noop(
    image: Image.Image,
    reason: str,
    reason_code: str,
    candidate_pixel_ratio: float = 0.0,
) -> PaperColorCastNormalizationResult:
    return PaperColorCastNormalizationResult(
        image,
        False,
        reason,
        reason_code,
        0.0,
        0.0,
        0.0,
        round(candidate_pixel_ratio, 6),
    )


def _mean_stddev(values: list[float], mean: float) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _lighten_edge_shadow_conservative(image: Image.Image) -> EdgeShadowLighteningResult:
    if image.width < 80 or image.height < 80:
        return _edge_shadow_noop(image, "edge shadow lightening skipped: image too small")

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
    color_margin = max(strip * 4, int(round(min(image.width, image.height) * 0.16)))
    if image.mode != "L" and _edge_shadow_near_edge_color_risk(image, color_margin):
        return _edge_shadow_noop(image, "edge shadow lightening skipped: color content or annotation risk near page edge")
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
            candidate_pixels, continuity_ratio = _edge_shadow_candidate_profile(edge, side, inner_mean)
            candidate_ratio = candidate_pixels / max(1, total)
            if candidate_ratio < 0.008 or continuity_ratio < 0.72:
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


def _edge_shadow_candidate_profile(edge: Image.Image, side: str, inner_mean: float) -> tuple[int, float]:
    upper = max(132, min(248, int(round(inner_mean - 4))))
    pixels = edge.load()
    width, height = edge.size
    candidate_pixels = 0
    covered_positions = 0
    if side in {"left", "right"}:
        for y in range(height):
            row_has_candidate = False
            for x in range(width):
                if 132 <= pixels[x, y] <= upper:
                    candidate_pixels += 1
                    row_has_candidate = True
            if row_has_candidate:
                covered_positions += 1
        continuity_ratio = covered_positions / max(1, height)
    else:
        for x in range(width):
            column_has_candidate = False
            for y in range(height):
                if 132 <= pixels[x, y] <= upper:
                    candidate_pixels += 1
                    column_has_candidate = True
            if column_has_candidate:
                covered_positions += 1
        continuity_ratio = covered_positions / max(1, width)
    return candidate_pixels, continuity_ratio


def _edge_shadow_near_edge_color_risk(image: Image.Image, margin: int) -> bool:
    if image.mode == "L":
        return False
    source_width, source_height = image.size
    source_min_dimension = max(1, min(source_width, source_height))
    sample = image.convert("RGB")
    sample.thumbnail((600, 600), Image.Resampling.BILINEAR)
    width, height = sample.size
    scaled_margin = int(round(margin * (min(width, height) / source_min_dimension)))
    margin = max(1, min(scaled_margin, width // 2, height // 2))
    boxes = (
        (0, 0, margin, height),
        (width - margin, 0, width, height),
        (0, 0, width, margin),
        (0, height - margin, width, height),
    )
    for box in boxes:
        crop = sample.crop(box)
        total = max(1, crop.width * crop.height)
        red = 0
        colored = 0
        pixel_data = crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
        for red_value, green_value, blue_value in pixel_data:
            high = max(red_value, green_value, blue_value)
            low = min(red_value, green_value, blue_value)
            spread = high - low
            brightness = (red_value + green_value + blue_value) / 3
            if red_value >= 110 and red_value - green_value >= 35 and red_value - blue_value >= 35:
                red += 1
            if spread > 18 and 30 < brightness < 250:
                colored += 1
        if red / total >= 0.0008 or colored / total >= 0.004:
            return True
    return False


def _lighten_corner_shadows_conservative(image: Image.Image) -> CornerShadowCleanupResult:
    if image.width < 90 or image.height < 90:
        return _corner_shadows_noop(image, "corner shadow cleanup skipped: image too small", "too_small")

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p05 = _histogram_percentile(histogram, total, 0.05)
    p95 = _histogram_percentile(histogram, total, 0.95)
    if p95 < 182:
        return _corner_shadows_noop(image, "corner shadow cleanup skipped: page is too dark", "too_dark")
    soft_pair_mode = 14 <= p95 - p05 < 24 and p95 >= 225
    if p95 - p05 < 24 and not soft_pair_mode:
        return _corner_shadows_noop(
            image,
            "corner shadow cleanup skipped: low tonal separation",
            "low_tonal_separation",
        )

    radius = max(28, min(90, int(round(min(image.width, image.height) * 0.24))))
    if image.mode != "L" and _corner_shadow_color_risk(image, radius):
        return _corner_shadows_noop(
            image,
            "corner shadow cleanup skipped: color content or annotation risk in page corner",
            "color_content",
        )

    corner_specs = (
        ("top_left", 0, 0, 1, 1),
        ("top_right", image.width - radius, 0, -1, 1),
        ("bottom_left", 0, image.height - radius, 1, -1),
        ("bottom_right", image.width - radius, image.height - radius, -1, -1),
    )
    plans: list[dict[str, Any]] = []
    for corner, left, top, x_direction, y_direction in corner_specs:
        corner_box = (left, top, left + radius, top + radius)
        inner_left = radius if x_direction > 0 else image.width - radius * 2
        inner_top = radius if y_direction > 0 else image.height - radius * 2
        if inner_left < 0 or inner_top < 0 or inner_left + radius > image.width or inner_top + radius > image.height:
            continue
        inner_box = (inner_left, inner_top, inner_left + radius, inner_top + radius)
        corner_crop = grayscale.crop(corner_box)
        inner_crop = grayscale.crop(inner_box)
        corner_mean = ImageStat.Stat(corner_crop).mean[0]
        inner_mean = ImageStat.Stat(inner_crop).mean[0]
        corner_std = ImageStat.Stat(corner_crop).stddev[0]
        inner_std = ImageStat.Stat(inner_crop).stddev[0]
        dark_ratio = _dark_pixel_ratio(corner_crop, 142)
        foreground_ratio = _dark_pixel_ratio(corner_crop, 166)
        inner_dark_ratio = _dark_pixel_ratio(inner_crop, 150)
        delta = inner_mean - corner_mean
        if dark_ratio > 0.0015:
            return _corner_shadows_noop(
                image,
                f"corner shadow cleanup skipped: protected dark content near {corner} corner",
                "protected_content",
            )
        if inner_dark_ratio > 0.04:
            continue
        if foreground_ratio > 0.03:
            return _corner_shadows_noop(
                image,
                f"corner shadow cleanup skipped: detail too dense near {corner} corner",
                "detail_too_high",
            )
        if corner_std > 25 or inner_std > 30:
            return _corner_shadows_noop(
                image,
                f"corner shadow cleanup skipped: texture or photo detail near {corner} corner",
                "texture_or_photo",
            )
        candidate_pixels, continuity = _corner_shadow_candidate_profile(corner_crop, inner_mean, x_direction, y_direction)
        candidate_ratio = candidate_pixels / max(1, total)
        strict_candidate = (
            7 <= delta <= 58
            and corner_mean >= 165
            and inner_mean >= 205
            and candidate_ratio >= 0.003
            and continuity >= 0.52
        )
        soft_pair_candidate = (
            soft_pair_mode
            and 4.4 <= delta <= 22
            and corner_mean >= 210
            and inner_mean >= 226
            and corner_std <= 12
            and inner_std <= 12
            and candidate_ratio >= 0.018
            and continuity >= 0.50
        )
        if not (strict_candidate or soft_pair_candidate):
            continue
        plans.append(
            {
                "corner": corner,
                "box": corner_box,
                "radius": radius,
                "x_direction": x_direction,
                "y_direction": y_direction,
                "max_delta": min(32.0, delta * (1.15 if soft_pair_candidate and not strict_candidate else 0.72)),
                "candidate_pixels": candidate_pixels,
                "inner_mean": inner_mean,
                "soft_pair_candidate": soft_pair_candidate and not strict_candidate,
            }
        )

    if not plans:
        return _corner_shadows_noop(
            image,
            "corner shadow cleanup skipped: no confident smooth neutral corner shadow",
            "no_candidate",
        )
    if any(bool(plan.get("soft_pair_candidate")) for plan in plans):
        soft_pair_corners = tuple(str(plan["corner"]) for plan in plans)
        allowed_soft_pairs = {
            ("top_left", "top_right"),
            ("bottom_left", "bottom_right"),
            ("top_left", "bottom_right"),
            ("top_right", "bottom_left"),
        }
        if len(plans) != 2 or soft_pair_corners not in allowed_soft_pairs:
            return _corner_shadows_noop(
                image,
                "corner shadow cleanup skipped: no confident paired smooth neutral corner shadows",
                "no_candidate",
            )
    if len(plans) > 2:
        return _corner_shadows_noop(
            image,
            "corner shadow cleanup skipped: broad uneven lighting is outside conservative corner scope",
            "broad_uneven_lighting",
        )

    working_l = grayscale.copy()
    before_values: list[float] = []
    after_values: list[float] = []
    changed_pixels = 0
    candidate_pixels_total = sum(int(plan["candidate_pixels"]) for plan in plans)
    for plan in plans:
        box = plan["box"]
        corner_crop = working_l.crop(box)
        before_values.append(ImageStat.Stat(corner_crop).mean[0])
        pixels = corner_crop.load()
        width, height = corner_crop.size
        max_distance = math.sqrt((width - 1) ** 2 + (height - 1) ** 2)
        for y in range(height):
            for x in range(width):
                value = pixels[x, y]
                if value < 150 or value > plan["inner_mean"] - 4:
                    continue
                dx = x if plan["x_direction"] > 0 else width - 1 - x
                dy = y if plan["y_direction"] > 0 else height - 1 - y
                distance = math.sqrt(dx * dx + dy * dy)
                if distance > max_distance:
                    continue
                factor = max(0.0, 1.0 - (distance / max(1.0, max_distance)) * 0.75)
                new_value = min(255, int(round(value + float(plan["max_delta"]) * factor)))
                minimum_lift = 1 if bool(plan.get("soft_pair_candidate")) else 2
                if new_value - value > minimum_lift:
                    changed_pixels += 1
                    pixels[x, y] = new_value
        working_l.paste(corner_crop, box)
        after_values.append(ImageStat.Stat(working_l.crop(box)).mean[0])

    changed_ratio = changed_pixels / max(1, total)
    candidate_ratio = candidate_pixels_total / max(1, total)
    if changed_ratio <= 0.0:
        return _corner_shadows_noop(
            image,
            "corner shadow cleanup skipped: candidate correction below conservative threshold",
            "low_confidence",
        )
    if changed_ratio > 0.06 or candidate_ratio > 0.10:
        return _corner_shadows_noop(
            image,
            "corner shadow cleanup skipped: changed area exceeds conservative corner scope",
            "changed_area_too_large",
        )

    result_image = _replace_luminance_preserving_chroma(image, working_l)
    before_mean = round(sum(before_values) / len(before_values), 6)
    after_mean = round(sum(after_values) / len(after_values), 6)
    return CornerShadowCleanupResult(
        result_image,
        True,
        "corner shadow cleanup applied: smooth neutral corner shadow",
        "applied",
        tuple(str(plan["corner"]) for plan in plans),
        before_mean,
        after_mean,
        round(after_mean - before_mean, 6),
        round(changed_ratio, 6),
        round(candidate_ratio, 6),
    )


def _corner_shadows_noop(image: Image.Image, reason: str, reason_code: str) -> CornerShadowCleanupResult:
    return CornerShadowCleanupResult(image, False, reason, reason_code, (), None, None, 0.0, 0.0, 0.0)


def _corner_shadow_candidate_profile(
    corner: Image.Image,
    inner_mean: float,
    x_direction: int,
    y_direction: int,
) -> tuple[int, float]:
    upper = max(150, min(248, int(round(inner_mean - 4))))
    pixels = corner.load()
    width, height = corner.size
    candidate_pixels = 0
    covered_rings: set[int] = set()
    max_distance = math.sqrt((width - 1) ** 2 + (height - 1) ** 2)
    for y in range(height):
        for x in range(width):
            value = pixels[x, y]
            if 150 <= value <= upper:
                candidate_pixels += 1
                dx = x if x_direction > 0 else width - 1 - x
                dy = y if y_direction > 0 else height - 1 - y
                distance = math.sqrt(dx * dx + dy * dy)
                covered_rings.add(min(7, int((distance / max(1.0, max_distance)) * 8)))
    return candidate_pixels, len(covered_rings) / 8


def _corner_shadow_color_risk(image: Image.Image, radius: int) -> bool:
    if image.mode == "L":
        return False
    sample = image.convert("RGB")
    boxes = (
        (0, 0, radius, radius),
        (image.width - radius, 0, image.width, radius),
        (0, image.height - radius, radius, image.height),
        (image.width - radius, image.height - radius, image.width, image.height),
    )
    for box in boxes:
        crop = sample.crop(box)
        total = max(1, crop.width * crop.height)
        colored = 0
        red = 0
        pixel_data = crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
        for red_value, green_value, blue_value in pixel_data:
            high = max(red_value, green_value, blue_value)
            low = min(red_value, green_value, blue_value)
            brightness = (red_value + green_value + blue_value) / 3
            spread = high - low
            if red_value >= 105 and red_value - green_value >= 32 and red_value - blue_value >= 32:
                red += 1
            if spread > 16 and 35 < brightness < 250:
                colored += 1
        if red / total >= 0.0006 or colored / total >= 0.003:
            return True
    return False


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
    if raw_candidate_ratio > 0.10:
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
    if candidate_ratio > 0.10:
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
        component_box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
        edge_density = _background_stain_component_edge_density(grayscale, component_box, background)
        color_shift = _background_stain_component_color_shift(image, component)
        low_frequency_shape = (
            area_ratio <= 0.085
            and width <= image.width * 0.62
            and height <= image.height * 0.62
            and width >= image.width * 0.24
            and height >= image.height * 0.22
            and area_ratio >= 0.004
            and edge_density <= 0.12
            and color_shift <= 44
        )
        small_speckle_shape = (
            area_ratio <= 0.012
            and width <= image.width * 0.18
            and height <= image.height * 0.18
            and color_shift <= 36
        )
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
    if changed_ratio > 0.085:
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
        background_rgb = _background_stain_reference_rgb(source, grayscale, background)
        for x, y in selected:
            gray_value = gray_pixels[x, y]
            delta = min(22, max(4, int(round((background - gray_value) * 0.78))))
            red_value, green_value, blue_value = output_pixels[x, y]
            new_red, new_green, new_blue = _lighten_background_stain_pixel(
                (red_value, green_value, blue_value),
                background_rgb,
                delta,
            )
            output_pixels[x, y] = (new_red, new_green, new_blue)
            before_values.append(gray_value)
            after_values.append(int(round((new_red + new_green + new_blue) / 3)))
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


def _background_stain_component_edge_density(grayscale: Image.Image, box: tuple[int, int, int, int], background: int) -> float:
    crop = grayscale.crop(box)
    if crop.width <= 1 or crop.height <= 1:
        return 1.0
    edge_threshold = max(10, int(round((255 - min(background, 252)) * 0.5 + 8)))
    edges = crop.filter(ImageFilter.FIND_EDGES)
    histogram = edges.histogram()
    return sum(histogram[edge_threshold:]) / max(1, crop.width * crop.height)


def _background_stain_component_color_shift(image: Image.Image, component: list[tuple[int, int]]) -> float:
    if image.mode == "L":
        return 0.0
    pixels = image.convert("RGB").load()
    shifts: list[int] = []
    step = max(1, len(component) // 1800)
    for x, y in component[::step]:
        red_value, green_value, blue_value = pixels[x, y]
        shifts.append(max(red_value, green_value, blue_value) - min(red_value, green_value, blue_value))
    if not shifts:
        return 0.0
    return sum(shifts) / len(shifts)


def _background_stain_reference_rgb(source: Image.Image, grayscale: Image.Image, background: int) -> tuple[int, int, int]:
    threshold = max(205, min(252, background - 2))
    source_pixels = source.load()
    gray_pixels = grayscale.load()
    totals = [0, 0, 0]
    count = 0
    for y in range(source.height):
        for x in range(source.width):
            if gray_pixels[x, y] < threshold:
                continue
            red_value, green_value, blue_value = source_pixels[x, y]
            high = max(red_value, green_value, blue_value)
            low = min(red_value, green_value, blue_value)
            if high - low > 28:
                continue
            totals[0] += red_value
            totals[1] += green_value
            totals[2] += blue_value
            count += 1
    if count == 0:
        return (background, background, background)
    return tuple(min(255, max(0, int(round(value / count)))) for value in totals)  # type: ignore[return-value]


def _lighten_background_stain_pixel(
    pixel: tuple[int, int, int],
    background_rgb: tuple[int, int, int],
    delta: int,
) -> tuple[int, int, int]:
    red_value, green_value, blue_value = pixel
    values = (red_value, green_value, blue_value)
    output: list[int] = []
    for value, background_value in zip(values, background_rgb):
        tonal_lift = min(delta, max(0, background_value - value))
        neutral_lift = max(0, int(round((background_value - value) * 0.55)))
        output.append(min(255, value + max(tonal_lift, neutral_lift)))
    return (output[0], output[1], output[2])


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


def _level_illumination_gradient_conservative(image: Image.Image) -> IlluminationGradientLevelingResult:
    if image.width < 100 or image.height < 100:
        return _illumination_gradient_noop(image, "image too small", "low_confidence")
    color_risk = _illumination_gradient_color_reason_code(image)
    if color_risk:
        return _illumination_gradient_noop(image, "protected color content", color_risk)

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p05 = _histogram_percentile(histogram, total, 0.05)
    p50 = _histogram_percentile(histogram, total, 0.50)
    p90 = _histogram_percentile(histogram, total, 0.90)
    p98 = _histogram_percentile(histogram, total, 0.98)
    if p50 < 210 or p90 < 220 or p98 < 226:
        return _illumination_gradient_noop(image, "not a broad bright paper background", "not_uniform")
    if p98 - p05 > 82:
        return _illumination_gradient_noop(image, "protected dark or mixed foreground content", "protected_content")
    foreground = grayscale.point(lambda value: 255 if value <= max(150, min(190, p50 - 36)) else 0, mode="L")
    foreground_ratio = _mask_ratio(foreground)
    if foreground_ratio > 0.0015:
        return _illumination_gradient_noop(image, "protected foreground content", "protected_content")
    if _source_protected_edge_dark_ratio(grayscale) > 0.0018:
        return _illumination_gradient_noop(image, "protected edge or border content", "protected_content")
    if _illumination_gradient_texture_ratio(grayscale) > 0.055:
        return _illumination_gradient_noop(image, "protected photo, chart, map, or textured content", "protected_content")

    vertical = _illumination_gradient_axis_plan(grayscale, vertical=True)
    horizontal = _illumination_gradient_axis_plan(grayscale, vertical=False)
    diagonal_tl_br = _illumination_gradient_diagonal_plan(grayscale, top_left_to_bottom_right=True)
    diagonal_tr_bl = _illumination_gradient_diagonal_plan(grayscale, top_left_to_bottom_right=False)
    plan = max((vertical, horizontal, diagonal_tl_br, diagonal_tr_bl), key=lambda candidate_plan: candidate_plan["score"])
    if plan["reason_code"] != "applied":
        return _illumination_gradient_noop(image, plan["reason"], plan["reason_code"], plan["candidate_ratio"])

    profile: list[float] = plan["profile"]
    candidate_threshold = plan["candidate_threshold"]
    target = min(246.0, max(profile))
    corrections = [max(0, min(10, int(round((target - value) * 0.62)))) for value in profile]
    if max(corrections, default=0) < 3:
        return _illumination_gradient_noop(image, "low confidence correction below threshold", "low_confidence")

    candidate = grayscale.point(lambda value: 255 if value >= candidate_threshold else 0, mode="L")
    candidate = ImageChops.multiply(candidate, foreground.point(lambda value: 0 if value else 255, mode="L"))
    candidate_pixels = _mask_pixel_count(candidate)
    candidate_ratio = candidate_pixels / max(1, total)
    if candidate_ratio < 0.72:
        return _illumination_gradient_noop(image, "not a uniform bright background", "not_uniform", candidate_ratio)
    if candidate_ratio > 0.995:
        candidate_ratio = 1.0

    candidate_mask = candidate.load()
    if image.mode == "L":
        output = grayscale.copy()
        pixels = output.load()
        source_pixels = grayscale.load()
        changed = 0
        for y in range(image.height):
            for x in range(image.width):
                if not candidate_mask[x, y]:
                    continue
                index = _illumination_gradient_profile_index(
                    plan["orientation"],
                    x,
                    y,
                    image.width,
                    image.height,
                    len(corrections),
                )
                delta = corrections[index]
                if delta <= 0:
                    continue
                value = int(source_pixels[x, y])
                new_value = min(255, value + delta)
                if new_value != value:
                    pixels[x, y] = new_value
                    changed += 1
        result_image = output
    else:
        source = image.convert("RGB")
        output = source.copy()
        output_pixels = output.load()
        changed = 0
        for y in range(image.height):
            for x in range(image.width):
                if not candidate_mask[x, y]:
                    continue
                index = _illumination_gradient_profile_index(
                    plan["orientation"],
                    x,
                    y,
                    image.width,
                    image.height,
                    len(corrections),
                )
                delta = corrections[index]
                if delta <= 0:
                    continue
                red_value, green_value, blue_value = output_pixels[x, y]
                new_pixel = (
                    min(255, red_value + delta),
                    min(255, green_value + delta),
                    min(255, blue_value + delta),
                )
                if new_pixel != (red_value, green_value, blue_value):
                    output_pixels[x, y] = new_pixel
                    changed += 1
        result_image = output

    changed_ratio = changed / max(1, total)
    if changed_ratio < 0.05:
        return _illumination_gradient_noop(image, "low confidence changed area", "low_confidence", candidate_ratio)
    if plan["orientation"] == "vertical":
        after_plan = _illumination_gradient_axis_plan(result_image.convert("L"), vertical=True)
    elif plan["orientation"] == "horizontal":
        after_plan = _illumination_gradient_axis_plan(result_image.convert("L"), vertical=False)
    else:
        after_plan = _illumination_gradient_diagonal_plan(
            result_image.convert("L"),
            top_left_to_bottom_right=plan["orientation"] == "diagonal_tl_br",
        )
    delta_after = float(after_plan.get("delta", plan["delta"]))
    if delta_after >= plan["delta"] - 2.0:
        return _illumination_gradient_noop(image, "guardrail reverted insufficient improvement", "guardrail_reverted", candidate_ratio)
    return IlluminationGradientLevelingResult(
        result_image,
        True,
        "illumination gradient leveling applied: smooth low-amplitude bright paper gradient",
        "applied",
        plan["orientation"],
        round(plan["delta"], 6),
        round(delta_after, 6),
        round(max(corrections), 6),
        round(changed_ratio, 6),
        round(candidate_ratio, 6),
    )


def _illumination_gradient_profile_index(
    orientation: str,
    x: int,
    y: int,
    width: int,
    height: int,
    profile_length: int,
) -> int:
    if orientation == "vertical":
        return min(profile_length - 1, max(0, x))
    if orientation == "horizontal":
        return min(profile_length - 1, max(0, y))
    x_position = x / max(1, width - 1)
    y_position = y / max(1, height - 1)
    if orientation == "diagonal_tr_bl":
        x_position = 1.0 - x_position
    diagonal_position = (x_position + y_position) / 2.0
    return min(profile_length - 1, max(0, int(round(diagonal_position * (profile_length - 1)))))


def _illumination_gradient_axis_plan(grayscale: Image.Image, *, vertical: bool) -> dict[str, Any]:
    axis_length = grayscale.width if vertical else grayscale.height
    cross_length = grayscale.height if vertical else grayscale.width
    orientation = "vertical" if vertical else "horizontal"
    pixels = grayscale.load()
    profile: list[float] = []
    bright_counts = 0
    candidate_threshold = 206
    for index in range(axis_length):
        values: list[int] = []
        for cross in range(cross_length):
            x, y = (index, cross) if vertical else (cross, index)
            value = int(pixels[x, y])
            if value >= candidate_threshold:
                values.append(value)
                bright_counts += 1
        if len(values) < cross_length * 0.68:
            profile.append(0.0)
        else:
            values.sort()
            trim = max(1, len(values) // 10)
            trimmed = values[trim:-trim] if len(values) > trim * 2 else values
            profile.append(sum(trimmed) / len(trimmed))
    if any(value <= 0 for value in profile):
        return _empty_illumination_gradient_plan(orientation, "not a uniform bright background", "not_uniform")
    edge = max(4, axis_length // 10)
    start = sum(profile[:edge]) / edge
    end = sum(profile[-edge:]) / edge
    center_width = max(6, axis_length // 5)
    center_start = max(0, (axis_length - center_width) // 2)
    center_values = profile[center_start : center_start + center_width]
    center = sum(center_values) / max(1, len(center_values))
    candidate_ratio = bright_counts / max(1, grayscale.width * grayscale.height)

    linear_delta = abs(start - end)
    linear_expected = [start + (end - start) * (idx / max(1, axis_length - 1)) for idx in range(axis_length)]
    linear_residuals = [abs(value - want) for value, want in zip(profile, linear_expected)]
    linear_mean_residual = sum(linear_residuals) / len(linear_residuals)

    edge_mean = (start + end) / 2
    two_edge_delta = center - edge_mean
    side_delta_balance = abs((center - start) - (center - end))
    center_index = (axis_length - 1) / 2
    two_edge_expected: list[float] = []
    for idx in range(axis_length):
        if idx <= center_index:
            denominator = max(1.0, center_index)
            two_edge_expected.append(start + (center - start) * (idx / denominator))
        else:
            denominator = max(1.0, axis_length - 1 - center_index)
            two_edge_expected.append(center + (end - center) * ((idx - center_index) / denominator))
    two_edge_residuals = [abs(value - want) for value, want in zip(profile, two_edge_expected)]
    two_edge_mean_residual = sum(two_edge_residuals) / len(two_edge_residuals)

    if linear_delta < 6.0 and two_edge_delta < 6.0:
        return _empty_illumination_gradient_plan(orientation, "gradient below conservative threshold", "low_confidence")
    if linear_delta > 28.0 or two_edge_delta > 24.0:
        return _empty_illumination_gradient_plan(orientation, "gradient too strong for conservative leveling", "too_strong")

    plan_shape = "linear"
    delta = linear_delta
    mean_residual = linear_mean_residual
    score = linear_delta - linear_mean_residual
    if (
        two_edge_delta >= 6.0
        and side_delta_balance <= 4.0
        and two_edge_mean_residual <= 2.4
        and two_edge_delta - two_edge_mean_residual > score
    ):
        plan_shape = "two_edge"
        delta = two_edge_delta
        mean_residual = two_edge_mean_residual
        score = two_edge_delta - two_edge_mean_residual
    if mean_residual > 2.7:
        return _empty_illumination_gradient_plan(orientation, "gradient is not smooth", "not_uniform")

    direction_changes = 0
    last_sign = 0
    for left, right in zip(profile, profile[1:]):
        diff = right - left
        sign = 1 if diff > 0.35 else -1 if diff < -0.35 else 0
        if sign and last_sign and sign != last_sign:
            direction_changes += 1
        if sign:
            last_sign = sign
    max_direction_changes = max(2, axis_length // 80) if plan_shape == "linear" else max(3, axis_length // 90)
    if direction_changes > max_direction_changes:
        return _empty_illumination_gradient_plan(orientation, "gradient is not smooth", "not_uniform")
    return {
        "orientation": orientation,
        "reason": "",
        "reason_code": "applied",
        "score": score,
        "delta": round(delta, 6),
        "candidate_ratio": round(candidate_ratio, 6),
        "candidate_threshold": candidate_threshold,
        "profile": profile,
    }


def _illumination_gradient_diagonal_plan(
    grayscale: Image.Image,
    *,
    top_left_to_bottom_right: bool,
) -> dict[str, Any]:
    orientation = "diagonal_tl_br" if top_left_to_bottom_right else "diagonal_tr_bl"
    sample = grayscale.copy()
    sample.thumbnail((220, 220), Image.Resampling.BILINEAR)
    pixels = sample.load()
    candidate_threshold = 206
    bins = max(24, min(56, int(round(math.hypot(sample.width, sample.height) / 6))))
    values_by_bin: list[list[int]] = [[] for _ in range(bins)]
    bright_counts = 0
    for y in range(sample.height):
        y_position = y / max(1, sample.height - 1)
        for x in range(sample.width):
            value = int(pixels[x, y])
            if value < candidate_threshold:
                continue
            x_position = x / max(1, sample.width - 1)
            if not top_left_to_bottom_right:
                x_position = 1.0 - x_position
            index = min(bins - 1, max(0, int(round(((x_position + y_position) / 2.0) * (bins - 1)))))
            values_by_bin[index].append(value)
            bright_counts += 1
    min_bin_count = max(4, (sample.width * sample.height) // max(1, bins * 120))
    profile: list[float] = []
    for values in values_by_bin:
        if len(values) < min_bin_count:
            return _empty_illumination_gradient_plan(orientation, "not a uniform bright background", "not_uniform")
        values.sort()
        trim = max(1, len(values) // 10)
        trimmed = values[trim:-trim] if len(values) > trim * 2 else values
        profile.append(sum(trimmed) / len(trimmed))

    edge = max(3, bins // 8)
    start = sum(profile[:edge]) / edge
    end = sum(profile[-edge:]) / edge
    candidate_ratio = bright_counts / max(1, sample.width * sample.height)
    linear_delta = abs(start - end)
    if linear_delta < 8.0:
        return _empty_illumination_gradient_plan(orientation, "gradient below conservative threshold", "low_confidence")
    if linear_delta > 30.0:
        return _empty_illumination_gradient_plan(orientation, "gradient too strong for conservative leveling", "too_strong")

    linear_expected = [start + (end - start) * (idx / max(1, bins - 1)) for idx in range(bins)]
    linear_residuals = [abs(value - want) for value, want in zip(profile, linear_expected)]
    linear_mean_residual = sum(linear_residuals) / len(linear_residuals)
    if linear_mean_residual > 2.5:
        return _empty_illumination_gradient_plan(orientation, "gradient is not smooth", "not_uniform")

    direction_changes = 0
    last_sign = 0
    for left, right in zip(profile, profile[1:]):
        diff = right - left
        sign = 1 if diff > 0.35 else -1 if diff < -0.35 else 0
        if sign and last_sign and sign != last_sign:
            direction_changes += 1
        if sign:
            last_sign = sign
    if direction_changes > max(2, bins // 18):
        return _empty_illumination_gradient_plan(orientation, "gradient is not smooth", "not_uniform")

    return {
        "orientation": orientation,
        "reason": "",
        "reason_code": "applied",
        "score": linear_delta - linear_mean_residual,
        "delta": round(linear_delta, 6),
        "candidate_ratio": round(candidate_ratio, 6),
        "candidate_threshold": candidate_threshold,
        "profile": profile,
    }


def _illumination_gradient_texture_ratio(grayscale: Image.Image) -> float:
    sample = grayscale.copy()
    sample.thumbnail((220, 220), Image.Resampling.BILINEAR)
    edges = sample.filter(ImageFilter.FIND_EDGES)
    histogram = edges.histogram()
    return sum(histogram[18:]) / max(1, sample.width * sample.height)


def _illumination_gradient_color_reason_code(image: Image.Image) -> str | None:
    if image.mode == "L":
        return None
    sample = image.convert("RGB")
    sample.thumbnail((240, 240), Image.Resampling.BILINEAR)
    total = max(1, sample.width * sample.height)
    colored = 0
    red = 0
    pale_colored = 0
    pixel_data = sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata()
    for red_value, green_value, blue_value in pixel_data:
        high = max(red_value, green_value, blue_value)
        low = min(red_value, green_value, blue_value)
        spread = high - low
        brightness = (red_value + green_value + blue_value) / 3
        if red_value >= 110 and red_value - green_value >= 34 and red_value - blue_value >= 34:
            red += 1
        if spread > 16 and 35 < brightness < 250:
            colored += 1
        if spread > 10 and brightness >= 170:
            pale_colored += 1
    if red / total >= 0.00035:
        return "protected_content"
    colored_ratio = colored / total
    pale_ratio = pale_colored / total
    if pale_ratio >= 0.18:
        return "protected_content"
    if colored_ratio >= 0.0025:
        return "protected_content"
    return None


def _empty_illumination_gradient_plan(orientation: str, reason: str, reason_code: str) -> dict[str, Any]:
    return {
        "orientation": orientation,
        "reason": reason,
        "reason_code": reason_code,
        "score": 0.0,
        "delta": 0.0,
        "candidate_ratio": 0.0,
        "candidate_threshold": 206,
        "profile": [],
    }


def _illumination_gradient_noop(
    image: Image.Image,
    reason: str,
    reason_code: str,
    candidate_pixel_ratio: float = 0.0,
) -> IlluminationGradientLevelingResult:
    return IlluminationGradientLevelingResult(
        image,
        False,
        f"illumination gradient leveling skipped: {reason}",
        reason_code,
        None,
        0.0,
        0.0,
        0.0,
        0.0,
        round(candidate_pixel_ratio, 6),
    )


def _lighten_fold_shadows_conservative(image: Image.Image) -> FoldShadowCleanupResult:
    if image.width < 100 or image.height < 100:
        return _fold_shadows_noop(image, "fold shadow cleanup skipped: image too small")
    color_risk = _tone_color_risk_reason(image)
    if color_risk:
        return _fold_shadows_noop(image, "fold shadow cleanup skipped: color content, stamp, or annotation risk")

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p05 = _histogram_percentile(histogram, total, 0.05)
    p50 = _histogram_percentile(histogram, total, 0.50)
    p90 = _histogram_percentile(histogram, total, 0.90)
    p95 = _histogram_percentile(histogram, total, 0.95)
    if p95 < 218 or p50 < 205 or p90 < 214:
        return _fold_shadows_noop(image, "fold shadow cleanup skipped: page is not a light clean background")
    if p95 - p05 > 120:
        return _fold_shadows_noop(image, "fold shadow cleanup skipped: high-contrast foreground or mixed content risk")

    foreground_threshold = min(168, max(92, p50 - 42))
    foreground = grayscale.point(lambda value: 255 if value <= foreground_threshold else 0, mode="L")
    foreground_ratio = _mask_ratio(foreground)
    if foreground_ratio > 0.16:
        return _fold_shadows_noop(image, "fold shadow cleanup skipped: foreground too dense")
    if _protected_edge_dark_ratio(foreground) > 0.0015:
        return _fold_shadows_noop(image, "fold shadow cleanup skipped: edge-adjacent content or binding risk")

    protected = foreground.filter(ImageFilter.MaxFilter(17))
    vertical = _fold_shadow_axis_plan(grayscale, protected, vertical=True, background=p95)
    horizontal = _fold_shadow_axis_plan(grayscale, protected, vertical=False, background=p95)
    plan = vertical if vertical["score"] >= horizontal["score"] else horizontal
    if plan["reason"]:
        return _fold_shadows_noop(image, f"fold shadow cleanup skipped: {plan['reason']}", plan["candidate_ratio"])

    selected = plan["selected"]
    changed_ratio = len(selected) / max(1, total)
    candidate_ratio = round(plan["candidate_ratio"], 6)
    if changed_ratio < 0.002:
        return _fold_shadows_noop(image, "fold shadow cleanup skipped: no confident narrow background band", candidate_ratio)
    if changed_ratio > 0.075:
        return _fold_shadows_noop(
            image,
            "fold shadow cleanup skipped: changed area exceeds conservative fold scope",
            candidate_ratio,
        )

    before_values: list[int] = []
    after_values: list[int] = []
    if image.mode == "L":
        output = grayscale.copy()
        pixels = output.load()
        source_pixels = grayscale.load()
        for x, y in selected:
            value = int(source_pixels[x, y])
            delta = min(18, max(3, int(round((p95 - value) * 0.58))))
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
            gray_value = int(gray_pixels[x, y])
            delta = min(18, max(3, int(round((p95 - gray_value) * 0.58))))
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
    if after_mean - before_mean < 3:
        return _fold_shadows_noop(
            image,
            "fold shadow cleanup skipped: improvement below conservative threshold",
            candidate_ratio,
        )
    return FoldShadowCleanupResult(
        result_image,
        True,
        "fold shadow cleanup applied: narrow neutral background band",
        plan["orientation"],
        len(plan["bands"]),
        before_mean,
        after_mean,
        round(after_mean - before_mean, 6),
        round(changed_ratio, 6),
        candidate_ratio,
    )


def _fold_shadow_axis_plan(
    grayscale: Image.Image,
    protected: Image.Image,
    *,
    vertical: bool,
    background: int,
) -> dict[str, Any]:
    width, height = grayscale.size
    axis_length = width if vertical else height
    cross_length = height if vertical else width
    orientation = "vertical" if vertical else "horizontal"
    edge_margin = max(10, int(round(axis_length * 0.07)))
    if axis_length <= edge_margin * 2 or cross_length < 80:
        return _empty_fold_shadow_plan(orientation, "image too small")

    pixels = grayscale.load()
    protected_pixels = protected.load()
    stats: list[dict[str, Any]] = []
    candidate_indexes: list[int] = []
    all_candidates: set[tuple[int, int]] = set()
    for index in range(axis_length):
        values: list[int] = []
        selected: list[tuple[int, int]] = []
        protected_count = 0
        dark_count = 0
        for cross in range(cross_length):
            x, y = (index, cross) if vertical else (cross, index)
            if protected_pixels[x, y]:
                protected_count += 1
                continue
            value = int(pixels[x, y])
            values.append(value)
            if value <= 150:
                dark_count += 1
            if 4 <= background - value <= 32 and value >= 172:
                selected.append((x, y))
        available_ratio = len(values) / max(1, cross_length)
        candidate_ratio = len(selected) / max(1, cross_length)
        dark_ratio = dark_count / max(1, len(values)) if values else 1.0
        mean = sum(values) / len(values) if values else 0.0
        stats.append(
            {
                "mean": mean,
                "available_ratio": available_ratio,
                "protected_ratio": protected_count / max(1, cross_length),
                "candidate_ratio": candidate_ratio,
                "dark_ratio": dark_ratio,
                "selected": selected,
            }
        )
        if (
            edge_margin <= index < axis_length - edge_margin
            and available_ratio >= 0.92
            and candidate_ratio >= 0.55
            and dark_ratio <= 0.0015
        ):
            candidate_indexes.append(index)
            all_candidates.update(selected)

    candidate_total_ratio = len(all_candidates) / max(1, width * height)
    if candidate_total_ratio > 0.12:
        return _empty_fold_shadow_plan(
            orientation,
            "broad uneven lighting is outside conservative fold scope",
            candidate_total_ratio,
        )

    groups = _contiguous_groups(candidate_indexes)
    min_width = max(1, int(axis_length * 0.006))
    max_width = max(min_width, int(round(axis_length * 0.08)))
    selected_groups: list[list[int]] = []
    selected: set[tuple[int, int]] = set()
    score = 0.0
    for group in groups:
        band_width = len(group)
        if band_width < min_width or band_width > max_width:
            continue
        center = group[len(group) // 2]
        neighbor_means = [
            stats[neighbor]["mean"]
            for offset in range(-(max_width * 2), max_width * 2 + 1)
            if abs(offset) >= max(min_width, band_width + 2)
            for neighbor in [center + offset]
            if 0 <= neighbor < axis_length and stats[neighbor]["available_ratio"] >= 0.92
        ]
        if not neighbor_means:
            continue
        local_mean = sum(neighbor_means) / len(neighbor_means)
        band_mean = sum(stats[index]["mean"] for index in group) / len(group)
        local_delta = local_mean - band_mean
        if not (4.0 <= local_delta <= 28.0):
            continue
        if any(stats[index]["protected_ratio"] > 0.004 for index in group):
            return _empty_fold_shadow_plan(
                orientation,
                "foreground intersects candidate fold band",
                candidate_total_ratio,
            )
        selected_groups.append(group)
        for index in group:
            selected.update(stats[index]["selected"])
        score += min(1.0, local_delta / 12.0) * min(1.0, band_width / max(1, min_width))

    if not selected_groups:
        return _empty_fold_shadow_plan(
            orientation,
            "no confident narrow background fold band",
            candidate_total_ratio,
        )
    if len(selected_groups) > 2:
        return _empty_fold_shadow_plan(
            orientation,
            "too many fold candidates outside conservative scope",
            candidate_total_ratio,
        )
    return {
        "orientation": orientation,
        "score": score,
        "bands": selected_groups,
        "selected": selected,
        "candidate_ratio": candidate_total_ratio,
        "reason": None,
    }


def _empty_fold_shadow_plan(
    orientation: str,
    reason: str,
    candidate_ratio: float = 0.0,
) -> dict[str, Any]:
    return {
        "orientation": orientation,
        "score": 0.0,
        "bands": [],
        "selected": set(),
        "candidate_ratio": round(candidate_ratio, 6),
        "reason": reason,
    }


_FOLD_SHADOW_REASON_CODES: dict[str, str] = {
    "fold shadow cleanup disabled": "disabled",
    "fold shadow cleanup applied: narrow neutral background band": "applied_narrow_neutral_background_band",
    "reverted by local content change guard": "reverted_by_local_content_change_guard",
    "reverted by cumulative change guard": "reverted_by_cumulative_change_guard",
    "reverted by geometric combination guard": "reverted_by_geometric_combination_guard",
    "reverted by text high-frequency combination guard": "reverted_by_text_high_frequency_combination_guard",
    "reverted by combined change guard": "reverted_by_combined_change_guard",
}


def _fold_shadows_reason_code(reason: str | None) -> str | None:
    if reason is None:
        return None
    if reason in _FOLD_SHADOW_REASON_CODES:
        return _FOLD_SHADOW_REASON_CODES[reason]
    if reason.startswith("fold shadow cleanup skipped: "):
        detail = reason.removeprefix("fold shadow cleanup skipped: ")
        normalized = "".join(character if character.isalnum() else "_" for character in detail.lower()).strip("_")
        while "__" in normalized:
            normalized = normalized.replace("__", "_")
        if normalized:
            return normalized[:96]
    return "unknown"


def _fold_shadows_noop(
    image: Image.Image,
    reason: str,
    candidate_pixel_ratio: float = 0.0,
) -> FoldShadowCleanupResult:
    return FoldShadowCleanupResult(
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


def _clean_bleed_through_conservative(image: Image.Image) -> BleedThroughCleanupResult:
    if image.width < 80 or image.height < 80:
        return _bleed_through_noop(image, "bleed-through cleanup skipped: image too small")
    color_risk = _tone_color_risk_reason(image)
    if color_risk:
        return _bleed_through_noop(image, "bleed-through cleanup skipped: color content, stamp, or annotation risk")

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = image.width * image.height
    p50 = _histogram_percentile(histogram, total, 0.50)
    p90 = _histogram_percentile(histogram, total, 0.90)
    p95 = _histogram_percentile(histogram, total, 0.95)
    if p95 < 222 or p50 < 216:
        return _bleed_through_noop(image, "bleed-through cleanup skipped: page is not a light background")

    foreground = grayscale.point(lambda value: 255 if value <= max(150, min(196, p50 - 28)) else 0, mode="L")
    foreground_ratio = _mask_ratio(foreground)
    if foreground_ratio > 0.22:
        return _bleed_through_noop(image, "bleed-through cleanup skipped: foreground too dense")
    if _protected_edge_dark_ratio(foreground) > 0.0025:
        return _bleed_through_noop(image, "bleed-through cleanup skipped: edge content or binding risk")

    background = max(p90, p95 - 1)
    min_ghost_signal = 2 if p50 >= 230 and p95 >= 238 else 4
    edge_signal = grayscale.filter(ImageFilter.FIND_EDGES)
    protected = foreground.filter(ImageFilter.MaxFilter(17))
    edge_margin = max(5, int(round(min(image.width, image.height) * 0.045)))
    raw_candidate = Image.new("L", image.size, 0)
    candidate_pixels = raw_candidate.load()
    source_pixels = grayscale.load()
    edge_pixels = edge_signal.load()
    for y in range(edge_margin, image.height - edge_margin):
        for x in range(edge_margin, image.width - edge_margin):
            value = int(source_pixels[x, y])
            if not (
                190 <= value <= background - min_ghost_signal
                and min_ghost_signal <= background - value <= 32
            ):
                continue
            if int(edge_pixels[x, y]) >= 22:
                continue
            candidate_pixels[x, y] = 255
    edge_cleared_candidate = _clear_mask_edges(raw_candidate, edge_margin)
    edge_candidate_ratio = _mask_ratio(raw_candidate) - _mask_ratio(edge_cleared_candidate)
    if edge_candidate_ratio > 0.0002 or _protected_edge_dark_ratio(raw_candidate) > 0.00005:
        return _bleed_through_noop(
            image,
            "bleed-through cleanup skipped: edge content or binding risk",
            edge_candidate_ratio,
        )
    candidate = ImageChops.multiply(edge_cleared_candidate, ImageChops.invert(protected))
    candidate_ratio = _mask_ratio(candidate)
    if candidate_ratio < 0.0003:
        return _bleed_through_noop(image, "bleed-through cleanup skipped: no confident faint reverse-side ghosts")
    if candidate_ratio > 0.065:
        return _bleed_through_noop(
            image,
            "bleed-through cleanup skipped: broad uneven background is outside conservative scope",
            candidate_ratio,
        )
    if _bleed_through_line_risk(candidate):
        return _bleed_through_noop(
            image,
            "bleed-through cleanup skipped: table line, page number, or annotation risk",
            candidate_ratio,
        )

    components = [component for component in _mask_components(candidate) if len(component) >= 4]
    if not components:
        return _bleed_through_noop(image, "bleed-through cleanup skipped: no confident faint reverse-side ghosts")
    if len(components) > 28:
        return _bleed_through_noop(
            image,
            "bleed-through cleanup skipped: too many ghost candidates outside conservative scope",
            candidate_ratio,
        )

    selected: set[tuple[int, int]] = set()
    for component in components:
        area = len(component)
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        width = max(xs) - min(xs) + 1
        height = max(ys) - min(ys) + 1
        if (
            min(xs) < edge_margin * 3
            or min(ys) < max(edge_margin * 3, int(round(image.height * 0.18)))
            or max(xs) >= image.width - edge_margin * 3
            or max(ys) >= image.height - max(edge_margin * 3, int(round(image.height * 0.18)))
        ):
            return _bleed_through_noop(
                image,
                "bleed-through cleanup skipped: edge content or binding risk",
                candidate_ratio,
            )
        if area / total > 0.018 or width > image.width * 0.42 or height > image.height * 0.28:
            return _bleed_through_noop(
                image,
                "bleed-through cleanup skipped: large candidate or archival mark risk",
                candidate_ratio,
            )
        aspect = max(width / max(1, height), height / max(1, width))
        if aspect > 18 and (width > image.width * 0.18 or height > image.height * 0.18):
            return _bleed_through_noop(
                image,
                "bleed-through cleanup skipped: table line, page number, or annotation risk",
                candidate_ratio,
            )
        if width <= 1 or height <= 1:
            continue
        selected.update(component)

    changed_ratio = len(selected) / max(1, total)
    if changed_ratio < 0.0003:
        return _bleed_through_noop(image, "bleed-through cleanup skipped: no confident faint reverse-side ghosts", candidate_ratio)
    if changed_ratio > 0.045:
        return _bleed_through_noop(
            image,
            "bleed-through cleanup skipped: changed area exceeds conservative background scope",
            candidate_ratio,
        )

    before_values: list[int] = []
    after_values: list[int] = []
    if image.mode == "L":
        output = grayscale.copy()
        pixels = output.load()
        for x, y in selected:
            value = int(pixels[x, y])
            new_value = min(255, value + min(18, max(4, int(round((background - value) * 0.62)))))
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
            gray_value = int(gray_pixels[x, y])
            delta = min(18, max(4, int(round((background - gray_value) * 0.62))))
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
    if after_mean - before_mean < 3:
        return _bleed_through_noop(
            image,
            "bleed-through cleanup skipped: improvement below conservative threshold",
            candidate_ratio,
        )
    return BleedThroughCleanupResult(
        result_image,
        True,
        "bleed-through cleanup applied: faint reverse-side ghost on light background",
        before_mean,
        after_mean,
        round(after_mean - before_mean, 6),
        round(changed_ratio, 6),
        round(candidate_ratio, 6),
    )


def _bleed_through_noop(
    image: Image.Image,
    reason: str,
    candidate_pixel_ratio: float = 0.0,
) -> BleedThroughCleanupResult:
    return BleedThroughCleanupResult(image, False, reason, None, None, 0.0, 0.0, round(candidate_pixel_ratio, 6))


def _bleed_through_line_risk(candidate: Image.Image) -> bool:
    pixels = candidate.load()
    horizontal = 0
    for y in range(candidate.height):
        count = 0
        for x in range(candidate.width):
            if pixels[x, y]:
                count += 1
        if count / max(1, candidate.width) >= 0.16:
            horizontal += 1
    vertical = 0
    for x in range(candidate.width):
        count = 0
        for y in range(candidate.height):
            if pixels[x, y]:
                count += 1
        if count / max(1, candidate.height) >= 0.16:
            vertical += 1
    return horizontal >= 2 or vertical >= 2


def _lighten_scanlines_conservative(image: Image.Image) -> ScanlineLighteningResult:
    if image.width < 80 or image.height < 80:
        return _scanlines_noop(image, "scanline lightening skipped: image too small")
    color_risk = _tone_color_risk_reason(image)
    if color_risk:
        return _scanlines_noop(
            image,
            "scanline lightening skipped: SCANLINE_COLOR_CONTENT_RISK risk 彩色内容/印章/批注风险",
        )

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
            "scanline lightening skipped: SCANLINE_EDGE_CONTENT_RISK risk 装订边、边缘原痕或边距内容风险",
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
            if 4 <= background - value <= 24 and value >= 170:
                selected.append((x, y))
        available_ratio = len(values) / max(1, cross_length)
        protected_ratio = protected_count / max(1, cross_length)
        candidate_ratio = len(selected) / max(1, cross_length)
        candidate_available_ratio = len(selected) / max(1, len(values))
        candidate_runs = _scanline_candidate_runs(selected, horizontal=horizontal)
        long_run_floor = max(10, int(round(cross_length * 0.055)))
        long_runs = [run for run in candidate_runs if run >= long_run_floor]
        longest_run_ratio = (max(candidate_runs) if candidate_runs else 0) / max(1, cross_length)
        dark_ratio = dark / max(1, len(values)) if values else 1.0
        max_dark_ratio = max(max_dark_ratio, dark_ratio)
        mean = sum(values) / len(values) if values else 0.0
        candidate_mean = sum(pixels[x, y] for x, y in selected) / len(selected) if selected else None
        line_stats.append(
            {
                "mean": mean,
                "candidate_mean": candidate_mean,
                "available_ratio": available_ratio,
                "protected_ratio": protected_ratio,
                "candidate_ratio": candidate_ratio,
                "candidate_available_ratio": candidate_available_ratio,
                "segment_count": len(long_runs),
                "longest_segment_ratio": longest_run_ratio,
                "dark_ratio": dark_ratio,
                "selected": selected,
            }
        )
        all_candidates.update(selected)

    candidate_total_ratio = len(all_candidates) / max(1, width * height)
    if max_dark_ratio > 0.025:
        return _empty_scanline_lightening_plan(
            orientation,
            "SCANLINE_CONTENT_RISK risk 正文、表格线、印章、批注或档案原痕风险",
            candidate_total_ratio,
        )
    if candidate_total_ratio > 0.09:
        return _empty_scanline_lightening_plan(
            orientation,
            "SCANLINE_SCOPE_RISK risk 大范围不均匀明暗超出保守处理范围",
            candidate_total_ratio,
        )

    candidate_lines: list[int] = []
    selected: set[tuple[int, int]] = set()
    score = 0.0
    for index in range(margin, axis_length - margin):
        stat = line_stats[index]
        if stat["protected_ratio"] > 0.012:
            continue
        if stat["available_ratio"] < 0.72:
            continue
        continuous_candidate = stat["candidate_ratio"] >= 0.38 and stat["candidate_available_ratio"] >= 0.54
        segmented_candidate = (
            stat["candidate_ratio"] >= 0.24
            and stat["candidate_available_ratio"] >= 0.32
            and 3 <= stat["segment_count"] <= 8
            and stat["longest_segment_ratio"] >= 0.07
        )
        broken_segmented_candidate = (
            stat["candidate_ratio"] >= 0.20
            and stat["candidate_available_ratio"] >= 0.28
            and 4 <= stat["segment_count"] <= 10
            and stat["longest_segment_ratio"] >= 0.045
        )
        if not (continuous_candidate or segmented_candidate or broken_segmented_candidate):
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
        broken_only = broken_segmented_candidate and not (continuous_candidate or segmented_candidate)
        if broken_only:
            candidate_mean = stat["candidate_mean"]
            if candidate_mean is None:
                continue
            candidate_delta = local_mean - candidate_mean
            if not (1.2 <= local_delta <= 12.0 and 5.0 <= candidate_delta <= 24.0):
                continue
            if stat["dark_ratio"] > 0.001:
                continue
            score_delta = candidate_delta
        else:
            if not (3.0 <= local_delta <= 22.0):
                continue
            score_delta = local_delta
        if (
            segmented_candidate
            and not continuous_candidate
            and not broken_only
            and (local_delta < 4.5 or stat["dark_ratio"] > 0.002)
        ):
            continue
        if not broken_only and local_delta < 4.5 and (
            stat["candidate_ratio"] < 0.68
            or stat["candidate_available_ratio"] < 0.80
            or stat["dark_ratio"] > 0.002
        ):
            continue
        candidate_lines.append(index)
        selected.update(stat["selected"])
        score += min(1.0, stat["candidate_available_ratio"] / 0.8) * min(1.0, score_delta / 12.0)

    if not candidate_lines:
        return _empty_scanline_lightening_plan(
            orientation,
            "SCANLINE_LOW_CONFIDENCE low-confidence 低置信轻微扫描线证据不足",
            candidate_total_ratio,
        )
    groups = _contiguous_groups(candidate_lines)
    if len(groups) > 6 or any(len(group) > 4 for group in groups):
        return _empty_scanline_lightening_plan(
            orientation,
            "SCANLINE_SCOPE_RISK risk 大范围不均匀明暗或档案原有条痕风险",
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


def _scanline_candidate_runs(points: list[tuple[int, int]], *, horizontal: bool) -> list[int]:
    if not points:
        return []
    coordinates = sorted(x if horizontal else y for x, y in points)
    runs: list[int] = []
    run_length = 1
    for previous, current in zip(coordinates, coordinates[1:]):
        if current == previous + 1:
            run_length += 1
        elif current != previous:
            runs.append(run_length)
            run_length = 1
    runs.append(run_length)
    return runs


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
    if p99 - p01 < 12:
        return _faded_text_noop(image, "faded text enhancement skipped: text evidence too weak")
    if p95 - p05 > 92:
        return _faded_text_noop(image, "faded text enhancement skipped: contrast already normal or mixed content risk")

    threshold = min(230, p50 - 8, p95 - 12)
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
        lambda value: 255 if 95 <= value <= threshold and 10 <= p95 - value <= 76 else 0,
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


_FADED_TEXT_REASON_DETAILS: dict[str, tuple[str, str]] = {
    "faded text enhancement disabled": ("disabled", "褪色正文加深未启用。"),
    "faded text enhancement applied: stable low-contrast neutral text on light paper": (
        "applied_stable_low_contrast_text",
        "检测到浅色纸面上的稳定低对比正文，已保守加深。",
    ),
    "faded text enhancement skipped: image too small": ("image_too_small", "图片尺寸过小，跳过褪色正文加深。"),
    "faded text enhancement skipped: color content, stamp, or annotation risk": (
        "protected_color_stamp_annotation",
        "检测到彩色内容、印章或批注风险，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: page is not a light paper background": (
        "not_light_paper_background",
        "页面不是浅色纸面背景，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: dark foreground already present": (
        "protected_dark_foreground",
        "页面已有较深前景内容，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: text evidence too weak": (
        "low_confidence_text_evidence_too_weak",
        "正文证据过弱，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: contrast already normal or mixed content risk": (
        "protected_normal_contrast_or_mixed_content",
        "对比度已正常或存在混合内容风险，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: outside conservative faded ink range": (
        "outside_conservative_faded_ink_range",
        "不在保守褪色墨迹范围内，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: foreground evidence too sparse": (
        "low_confidence_foreground_too_sparse",
        "前景证据过少，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: foreground too dense": (
        "protected_foreground_too_dense",
        "前景候选过密，可能不是正文，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: edge mark or binding risk": (
        "protected_edge_mark_or_binding",
        "检测到边缘痕迹或装订边风险，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: no stable text components": (
        "low_confidence_no_stable_text_components",
        "没有稳定正文组件，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: broad stain, texture, illustration, or table-region risk": (
        "protected_texture_table_or_photo_region",
        "检测到大块污渍、纹理、照片或表格区域风险，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: stable text evidence insufficient": (
        "low_confidence_stable_text_insufficient",
        "稳定正文证据不足，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: changed area exceeds conservative text scope": (
        "outside_conservative_text_scope",
        "候选变化范围超过保守正文范围，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: readability delta below conservative threshold": (
        "low_confidence_readability_delta_too_low",
        "加深幅度低于保守可读性阈值，跳过褪色正文加深。",
    ),
    "faded text enhancement skipped: readability delta exceeds conservative threshold": (
        "outside_conservative_readability_delta",
        "加深幅度超过保守阈值，跳过褪色正文加深。",
    ),
    "reverted by local content change guard": (
        "reverted_by_local_content_change_guard",
        "局部内容变化保护线已回退本次处理。",
    ),
    "reverted by cumulative change guard": (
        "reverted_by_cumulative_change_guard",
        "累计变化保护线已回退本次处理。",
    ),
    "reverted by geometric combination guard": (
        "reverted_by_geometric_combination_guard",
        "几何组合风险保护线已回退本次处理。",
    ),
    "reverted by text high-frequency combination guard": (
        "reverted_by_text_high_frequency_combination_guard",
        "文字高频组合风险保护线已回退本次处理。",
    ),
    "reverted by combined change guard": (
        "reverted_by_combined_change_guard",
        "组合变化过大保护线已回退本次处理。",
    ),
}


def _faded_text_reason_code(reason: str | None) -> str | None:
    if reason is None:
        return None
    details = _FADED_TEXT_REASON_DETAILS.get(reason)
    if details:
        return details[0]
    return "unknown"


def _faded_text_reason_zh(reason_code: str | None) -> str | None:
    if reason_code is None:
        return None
    for code, reason_zh in _FADED_TEXT_REASON_DETAILS.values():
        if code == reason_code:
            return reason_zh
    return "褪色正文加深已按保守规则跳过。"


def _faded_text_sample_candidate_ratio(grayscale: Image.Image, threshold: float, p95: int) -> float:
    sample = grayscale.copy()
    sample.thumbnail((96, 96), Image.Resampling.NEAREST)
    candidate = sample.point(
        lambda value: 255 if 95 <= value <= threshold and 10 <= p95 - value <= 76 else 0,
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
    if _text_edge_margin_annotation_risk(grayscale, candidate):
        return _text_edges_noop(
            image,
            "text edge sharpening skipped: header, footer, or page number risk",
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


_TEXT_EDGES_REASON_DETAILS: dict[str, tuple[str, str]] = {
    "text edge sharpening disabled": ("disabled", "正文边缘锐化未启用。"),
    "text edge sharpening applied: stable neutral blurred text edges on light paper": (
        "applied_stable_blurred_text_edges",
        "检测到浅色纸面上的稳定模糊正文边缘，已保守锐化。",
    ),
    "text edge sharpening skipped: image too small": ("image_too_small", "图片尺寸过小，跳过正文边缘锐化。"),
    "text edge sharpening skipped: color content, stamp, or annotation risk": (
        "protected_color_stamp_annotation",
        "检测到彩色内容、印章或批注风险，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: page is not a light paper background": (
        "not_light_paper_background",
        "页面不是浅色纸面背景，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: text edge evidence too weak": (
        "low_confidence_text_edge_evidence_too_weak",
        "正文边缘证据过弱，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: dense dark foreground or illustration risk": (
        "protected_dense_dark_foreground_or_illustration",
        "检测到深色前景过密或插图风险，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: edge mark or binding risk": (
        "protected_edge_mark_or_binding",
        "检测到边缘痕迹或装订边风险，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: cheap candidate preflight found too little blurred text edge evidence": (
        "low_confidence_preflight_candidates_too_few",
        "快速预检显示模糊正文边缘候选过少，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: header, footer, or page number risk": (
        "protected_header_footer_or_page_number",
        "检测到页眉页脚或页码风险，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: blurred text edge evidence too sparse": (
        "low_confidence_text_edge_too_sparse",
        "模糊正文边缘证据过少，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: edge candidates too dense": (
        "protected_edge_candidates_too_dense",
        "边缘候选过密，可能不是正文，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: fine texture or photo-detail risk": (
        "protected_photo_texture_detail",
        "检测到照片纹理或细碎纹理风险，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: broad texture, illustration, or table-region risk": (
        "protected_texture_table_or_photo_region",
        "检测到大块纹理、插图、照片或表格区域风险，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: stable text edge evidence insufficient": (
        "low_confidence_stable_text_edge_insufficient",
        "稳定正文边缘证据不足，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: changed area exceeds conservative text edge scope": (
        "outside_conservative_text_edge_scope",
        "候选变化范围超过保守正文边缘范围，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: sharpening delta below conservative threshold": (
        "low_confidence_sharpening_delta_too_low",
        "锐化幅度低于保守可读性阈值，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: changed area exceeds conservative threshold": (
        "outside_conservative_changed_area",
        "变化范围超过保守阈值，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: edge delta outside conservative threshold": (
        "outside_conservative_edge_delta",
        "正文边缘锐化幅度不在保守范围内，跳过正文边缘锐化。",
    ),
    "text edge sharpening skipped: brightness or contrast delta exceeds conservative threshold": (
        "outside_conservative_tonal_delta",
        "亮度或对比度变化超过保守阈值，跳过正文边缘锐化。",
    ),
    "reverted by local content change guard": (
        "reverted_by_local_content_change_guard",
        "局部内容变化保护线已回退本次处理。",
    ),
    "reverted by cumulative change guard": (
        "reverted_by_cumulative_change_guard",
        "累计变化保护线已回退本次处理。",
    ),
    "reverted by geometric combination guard": (
        "reverted_by_geometric_combination_guard",
        "几何组合风险保护线已回退本次处理。",
    ),
    "reverted by text high-frequency combination guard": (
        "reverted_by_text_high_frequency_combination_guard",
        "文字高频组合风险保护线已回退本次处理。",
    ),
    "reverted by combined change guard": (
        "reverted_by_combined_change_guard",
        "组合变化过大保护线已回退本次处理。",
    ),
}


def _text_edges_reason_code(reason: str | None) -> str | None:
    if reason is None:
        return None
    details = _TEXT_EDGES_REASON_DETAILS.get(reason)
    if details:
        return details[0]
    return "unknown"


def _text_edges_reason_zh(reason_code: str | None) -> str | None:
    if reason_code is None:
        return None
    for code, reason_zh in _TEXT_EDGES_REASON_DETAILS.values():
        if code == reason_code:
            return reason_zh
    return "正文边缘锐化已按保守规则跳过。"


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


def _text_edge_margin_annotation_risk(grayscale: Image.Image, candidate: Image.Image) -> bool:
    margin = max(8, int(round(grayscale.height * 0.16)))
    if margin * 2 >= grayscale.height:
        return False
    boxes = (
        (0, 0, grayscale.width, margin),
        (0, grayscale.height - margin, grayscale.width, grayscale.height),
    )
    foreground = grayscale.point(lambda value: 255 if value <= 170 else 0, mode="L")
    total_candidate = sum(candidate.histogram()[1:])
    if total_candidate <= 0:
        return False
    for box in boxes:
        area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
        margin_candidate = sum(candidate.crop(box).histogram()[1:])
        margin_foreground = sum(foreground.crop(box).histogram()[1:])
        candidate_share = margin_candidate / total_candidate
        foreground_ratio = margin_foreground / area
        if margin_candidate >= 8 and candidate_share >= 0.18 and foreground_ratio >= 0.0008:
            return True
        if margin_foreground >= 10 and foreground_ratio >= 0.0025:
            return True
    return False


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
    scanner_gutter_bbox: tuple[int, int, int, int] | None,
    skew_angle_degrees: float | None,
    despeckle_pixels_changed: int,
    tone_normalized: bool = False,
    tone_background_before: float | None = None,
    tone_background_after: float | None = None,
    tone_contrast_before: float | None = None,
    tone_contrast_after: float | None = None,
    tone_changed_pixel_ratio: float = 0.0,
    paper_color_cast_normalized: bool = False,
    paper_color_cast_delta: float = 0.0,
    paper_color_cast_brightness_delta: float = 0.0,
    paper_color_cast_changed_pixel_ratio: float = 0.0,
    paper_color_cast_candidate_pixel_ratio: float = 0.0,
    edge_shadow_lightened: bool = False,
    edge_shadow_delta: float = 0.0,
    edge_shadow_changed_pixel_ratio: float = 0.0,
    edge_shadow_candidate_pixel_ratio: float = 0.0,
    corner_shadows_lightened: bool = False,
    corner_shadows_delta: float = 0.0,
    corner_shadows_changed_pixel_ratio: float = 0.0,
    corner_shadows_candidate_pixel_ratio: float = 0.0,
    background_stains_lightened: bool = False,
    background_stains_delta: float = 0.0,
    background_stains_changed_pixel_ratio: float = 0.0,
    background_stains_candidate_pixel_ratio: float = 0.0,
    fold_shadows_lightened: bool = False,
    fold_shadows_delta: float = 0.0,
    fold_shadows_changed_pixel_ratio: float = 0.0,
    fold_shadows_candidate_pixel_ratio: float = 0.0,
    illumination_gradient_levelled: bool = False,
    illumination_gradient_correction_delta: float = 0.0,
    illumination_gradient_changed_pixel_ratio: float = 0.0,
    illumination_gradient_candidate_pixel_ratio: float = 0.0,
    bleed_through_cleaned: bool = False,
    bleed_through_delta: float = 0.0,
    bleed_through_changed_pixel_ratio: float = 0.0,
    bleed_through_candidate_pixel_ratio: float = 0.0,
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
    local_content_change_guard: dict[str, Any] | None = None,
    combination_quality_guard: dict[str, Any] | None = None,
    processed_output_safety_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_width, source_height = source.size
    output_width, output_height = processed.size
    source_area = max(1, source_width * source_height)
    output_area = max(1, output_width * output_height)
    size_change_ratio = abs(output_area - source_area) / source_area
    crop_ratio = 0.0
    if crop_bbox:
        crop_ratio = 1.0 - (((crop_bbox[2] - crop_bbox[0]) * (crop_bbox[3] - crop_bbox[1])) / source_area)
    dark_trim_margins = _trim_margins(source.size, dark_border_bbox)
    scanner_gutter_trim_margins = _trim_margins(source.size, scanner_gutter_bbox)
    trim_margins = {
        side: max(dark_trim_margins.get(side, 0.0), scanner_gutter_trim_margins.get(side, 0.0))
        for side in ("left", "top", "right", "bottom")
    }
    max_trim_margin_ratio = max(trim_margins.values()) if trim_margins else 0.0
    scanner_gutter_max_trim_margin_ratio = (
        max(scanner_gutter_trim_margins.values()) if scanner_gutter_trim_margins else 0.0
    )
    source_l = source.convert("L")
    processed_l = processed.convert("L")
    brightness_delta, contrast_delta = _tonal_deltas(source_l, processed_l)
    pixel_change_ratio = _pixel_change_ratio(source_l, processed_l)
    processed_output_metrics = _processed_output_safety_metrics(source_l, processed_l)
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
        "pixel_change_guardrail_applied": (
            not geometric_change_recorded and not tone_normalized and not paper_color_cast_normalized
        ),
        "pixel_change_guardrail_scope": (
            "same_size_pixel_change"
            if not geometric_change_recorded and not tone_normalized and not paper_color_cast_normalized
            else (
                "tone_normalization_recorded_by_brightness_and_contrast"
                if tone_normalized and not geometric_change_recorded
                else (
                    "paper_color_cast_recorded_by_color_delta"
                    if paper_color_cast_normalized and not geometric_change_recorded
                    else "geometric_change_recorded_by_size_crop_trim_or_deskew"
                )
            )
        ),
        "brightness_delta": round(brightness_delta, 6),
        "contrast_delta": round(contrast_delta, 6),
        "tone_normalized": tone_normalized,
        "tone_background_delta": round(tone_background_delta, 6),
        "tone_contrast_delta": round(tone_contrast_delta, 6),
        "tone_changed_pixel_ratio": round(tone_changed_pixel_ratio, 6),
        "paper_color_cast_normalized": paper_color_cast_normalized,
        "paper_color_cast_delta": round(paper_color_cast_delta, 6),
        "paper_color_cast_brightness_delta": round(paper_color_cast_brightness_delta, 6),
        "paper_color_cast_changed_pixel_ratio": round(paper_color_cast_changed_pixel_ratio, 6),
        "paper_color_cast_candidate_pixel_ratio": round(paper_color_cast_candidate_pixel_ratio, 6),
        "edge_shadow_lightened": edge_shadow_lightened,
        "edge_shadow_delta": round(edge_shadow_delta, 6),
        "edge_shadow_changed_pixel_ratio": round(edge_shadow_changed_pixel_ratio, 6),
        "edge_shadow_candidate_pixel_ratio": round(edge_shadow_candidate_pixel_ratio, 6),
        "corner_shadows_lightened": corner_shadows_lightened,
        "corner_shadows_delta": round(corner_shadows_delta, 6),
        "corner_shadows_changed_pixel_ratio": round(corner_shadows_changed_pixel_ratio, 6),
        "corner_shadows_candidate_pixel_ratio": round(corner_shadows_candidate_pixel_ratio, 6),
        "background_stains_lightened": background_stains_lightened,
        "background_stains_delta": round(background_stains_delta, 6),
        "background_stains_changed_pixel_ratio": round(background_stains_changed_pixel_ratio, 6),
        "background_stains_candidate_pixel_ratio": round(background_stains_candidate_pixel_ratio, 6),
        "fold_shadows_lightened": fold_shadows_lightened,
        "fold_shadows_delta": round(fold_shadows_delta, 6),
        "fold_shadows_changed_pixel_ratio": round(fold_shadows_changed_pixel_ratio, 6),
        "fold_shadows_candidate_pixel_ratio": round(fold_shadows_candidate_pixel_ratio, 6),
        "illumination_gradient_levelled": illumination_gradient_levelled,
        "illumination_gradient_correction_delta": round(illumination_gradient_correction_delta, 6),
        "illumination_gradient_changed_pixel_ratio": round(illumination_gradient_changed_pixel_ratio, 6),
        "illumination_gradient_candidate_pixel_ratio": round(illumination_gradient_candidate_pixel_ratio, 6),
        "bleed_through_cleaned": bleed_through_cleaned,
        "bleed_through_delta": round(bleed_through_delta, 6),
        "bleed_through_changed_pixel_ratio": round(bleed_through_changed_pixel_ratio, 6),
        "bleed_through_candidate_pixel_ratio": round(bleed_through_candidate_pixel_ratio, 6),
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
        "scanner_gutter_trimmed": scanner_gutter_bbox is not None,
        "crop_ratio": round(max(0.0, crop_ratio), 6),
        "trim_margins": trim_margins,
        "max_trim_margin_ratio": round(max_trim_margin_ratio, 6),
        "scanner_gutter_trim_margins": scanner_gutter_trim_margins,
        "scanner_gutter_max_trim_margin_ratio": round(scanner_gutter_max_trim_margin_ratio, 6),
        "deskew_abs_angle_degrees": deskew_abs_angle,
        "despeckle_pixel_ratio": round(despeckle_pixel_ratio, 6),
        **processed_output_metrics,
    }
    if local_content_change_guard is None:
        if _should_check_local_content_change(
            despeckle_pixels_changed=despeckle_pixels_changed,
            source_area=source_area,
            tone_normalized=tone_normalized,
            tone_background_delta=tone_background_delta,
            tone_contrast_delta=tone_contrast_delta,
            paper_color_cast_normalized=paper_color_cast_normalized,
            paper_color_cast_changed_pixel_ratio=paper_color_cast_changed_pixel_ratio,
            edge_shadow_lightened=edge_shadow_lightened,
            edge_shadow_changed_pixel_ratio=edge_shadow_changed_pixel_ratio,
            corner_shadows_lightened=corner_shadows_lightened,
            corner_shadows_changed_pixel_ratio=corner_shadows_changed_pixel_ratio,
            fold_shadows_lightened=fold_shadows_lightened,
            fold_shadows_changed_pixel_ratio=fold_shadows_changed_pixel_ratio,
            illumination_gradient_levelled=illumination_gradient_levelled,
            illumination_gradient_changed_pixel_ratio=illumination_gradient_changed_pixel_ratio,
            bleed_through_cleaned=bleed_through_cleaned,
            bleed_through_changed_pixel_ratio=bleed_through_changed_pixel_ratio,
            scanlines_lightened=scanlines_lightened,
            scanlines_changed_pixel_ratio=scanlines_changed_pixel_ratio,
            faded_text_enhanced=faded_text_enhanced,
            faded_text_changed_pixel_ratio=faded_text_changed_pixel_ratio,
            text_edges_sharpened=text_edges_sharpened,
            text_edges_changed_pixel_ratio=text_edges_changed_pixel_ratio,
        ):
            local_content_change_guard = _local_content_change_guard(source_l, processed_l, options)
        else:
            local_content_change_guard = _local_content_change_guard_passed(checked=False)
    metrics.update(_local_content_change_guard_audit_fields(local_content_change_guard))
    if cumulative_change_guard is None:
        cumulative_change_guard = _cumulative_change_guard(metrics, options)
    metrics.update(_cumulative_change_guard_audit_fields(cumulative_change_guard))
    if combination_quality_guard is None:
        combination_quality_guard = _combination_quality_guard(
            metrics,
            options,
            cumulative_change_guard=cumulative_change_guard,
            local_content_change_guard=local_content_change_guard,
        )
    metrics.update(_combination_quality_guard_audit_fields(combination_quality_guard))
    if processed_output_safety_guard is None:
        processed_output_safety_guard = _processed_output_safety_guard(metrics, options)
    metrics.update(_processed_output_safety_guard_audit_fields(processed_output_safety_guard))
    failures = _audit_guardrail_failures(metrics, options)
    return {**metrics, "guardrail_failures": failures}


def _should_check_local_content_change(
    *,
    despeckle_pixels_changed: int,
    source_area: int,
    tone_normalized: bool,
    tone_background_delta: float,
    tone_contrast_delta: float,
    paper_color_cast_normalized: bool,
    paper_color_cast_changed_pixel_ratio: float,
    edge_shadow_lightened: bool,
    edge_shadow_changed_pixel_ratio: float,
    corner_shadows_lightened: bool,
    corner_shadows_changed_pixel_ratio: float,
    fold_shadows_lightened: bool,
    fold_shadows_changed_pixel_ratio: float,
    illumination_gradient_levelled: bool,
    illumination_gradient_changed_pixel_ratio: float,
    bleed_through_cleaned: bool,
    bleed_through_changed_pixel_ratio: float,
    scanlines_lightened: bool,
    scanlines_changed_pixel_ratio: float,
    faded_text_enhanced: bool,
    faded_text_changed_pixel_ratio: float,
    text_edges_sharpened: bool,
    text_edges_changed_pixel_ratio: float,
) -> bool:
    despeckle_guard_floor = max(24, int(max(1, source_area) * 0.00005))
    if despeckle_pixels_changed >= despeckle_guard_floor:
        return True
    if edge_shadow_lightened and edge_shadow_changed_pixel_ratio > 0:
        return True
    if corner_shadows_lightened and corner_shadows_changed_pixel_ratio > 0:
        return True
    if fold_shadows_lightened and fold_shadows_changed_pixel_ratio > 0:
        return True
    if illumination_gradient_levelled and illumination_gradient_changed_pixel_ratio > 0:
        return True
    if bleed_through_cleaned and bleed_through_changed_pixel_ratio > 0:
        return True
    if scanlines_lightened and scanlines_changed_pixel_ratio > 0:
        return True
    if faded_text_enhanced and faded_text_changed_pixel_ratio > 0:
        return True
    if text_edges_sharpened and text_edges_changed_pixel_ratio > 0:
        return True
    if tone_normalized and (tone_background_delta > 12.0 or tone_contrast_delta > 18.0):
        return True
    if paper_color_cast_normalized and 0.0 < paper_color_cast_changed_pixel_ratio < 0.25:
        return True
    return False


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
        _float_metric(metrics, "corner_shadows_changed_pixel_ratio"),
        _float_metric(metrics, "corner_shadows_candidate_pixel_ratio"),
        _float_metric(metrics, "fold_shadows_changed_pixel_ratio"),
        _float_metric(metrics, "fold_shadows_candidate_pixel_ratio"),
        _float_metric(metrics, "illumination_gradient_changed_pixel_ratio"),
        _float_metric(metrics, "illumination_gradient_candidate_pixel_ratio"),
        _float_metric(metrics, "bleed_through_changed_pixel_ratio"),
        _float_metric(metrics, "bleed_through_candidate_pixel_ratio"),
        _float_metric(metrics, "scanlines_changed_pixel_ratio"),
        _float_metric(metrics, "scanlines_candidate_pixel_ratio"),
        _float_metric(metrics, "faded_text_changed_pixel_ratio"),
        _float_metric(metrics, "faded_text_candidate_pixel_ratio"),
        _float_metric(metrics, "text_edges_changed_pixel_ratio"),
        _float_metric(metrics, "text_edges_candidate_pixel_ratio"),
        _float_metric(metrics, "paper_color_cast_candidate_pixel_ratio"),
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


def _combination_quality_guard(
    metrics: dict[str, Any],
    options: ProcessingOptions,
    *,
    cumulative_change_guard: dict[str, Any],
    local_content_change_guard: dict[str, Any],
    low_confidence_original_preserved: bool = False,
) -> dict[str, Any]:
    if low_confidence_original_preserved:
        return _combination_quality_guard_result(
            action="kept_original",
            reason_code="low_confidence_original_preserved",
            risk_tier="low_confidence",
            reasons=[],
        )
    if cumulative_change_guard.get("action") == "deferred_to_existing_guardrail":
        return _combination_quality_guard_result(
            action="passed",
            reason_code="safe_combination_passed",
            risk_tier=_combination_passed_risk_tier(metrics),
            reasons=[],
        )

    local_reverted = local_content_change_guard.get("reverted") is True
    cumulative_reverted = cumulative_change_guard.get("reverted") is True
    geometry_reasons = _combination_geometry_risk_reasons(metrics, options)
    text_reasons = _combination_text_high_frequency_risk_reasons(metrics, options)
    cumulative_reasons = [
        reason
        for reason in cumulative_change_guard.get("reasons", [])
        if isinstance(reason, str)
    ]
    local_reasons = [
        reason
        for reason in local_content_change_guard.get("reasons", [])
        if isinstance(reason, str)
    ]

    if local_reverted or cumulative_reverted or geometry_reasons or text_reasons:
        if geometry_reasons:
            return _combination_quality_guard_result(
                action="reverted_to_source",
                reason_code="geometric_risk_reverted",
                risk_tier="geometry",
                reasons=geometry_reasons + cumulative_reasons + local_reasons,
            )
        if text_reasons:
            return _combination_quality_guard_result(
                action="reverted_to_source",
                reason_code="text_high_frequency_risk_reverted",
                risk_tier="text_high_frequency",
                reasons=text_reasons + cumulative_reasons + local_reasons,
            )
        return _combination_quality_guard_result(
            action="reverted_to_source",
            reason_code="combined_change_too_large_reverted",
            risk_tier="combined_change",
            reasons=cumulative_reasons + local_reasons,
        )

    return _combination_quality_guard_result(
        action="passed",
        reason_code="safe_combination_passed",
        risk_tier=_combination_passed_risk_tier(metrics),
        reasons=[],
    )


def _combination_quality_guard_result(
    *,
    action: str,
    reason_code: str,
    risk_tier: str,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "checked": True,
        "action": action,
        "reverted": action == "reverted_to_source",
        "reason_code": reason_code,
        "risk_tier": risk_tier,
        "reasons": sorted(set(reasons)),
    }


def _combination_geometry_risk_reasons(metrics: dict[str, Any], options: ProcessingOptions) -> list[str]:
    if metrics.get("pixel_change_guardrail_scope") != "geometric_change_recorded_by_size_crop_trim_or_deskew":
        return []
    has_output_geometry_change = (
        _float_metric(metrics, "size_change_ratio") > 0
        or _float_metric(metrics, "crop_ratio") > 0
        or _float_metric(metrics, "max_trim_margin_ratio") > 0
    )
    if not has_output_geometry_change:
        return []
    reasons: list[str] = []
    if _float_metric(metrics, "size_change_ratio") > options.audit_max_geometry_combo_size_change_ratio:
        reasons.append("geometry_size_change_ratio")
    if max(_float_metric(metrics, "crop_ratio"), _float_metric(metrics, "max_trim_margin_ratio")) > options.audit_max_geometry_combo_crop_ratio:
        reasons.append("geometry_crop_or_trim_ratio")
    if _float_metric(metrics, "deskew_abs_angle_degrees") > 2.5 and _combination_non_geometry_candidate_ratio(metrics) > 0.04:
        reasons.append("geometry_deskew_plus_enhancement")
    return reasons


def _combination_text_high_frequency_risk_reasons(metrics: dict[str, Any], options: ProcessingOptions) -> list[str]:
    if metrics.get("faded_text_enhanced") is not True or metrics.get("text_edges_sharpened") is not True:
        return []
    reasons: list[str] = []
    changed_ratio = _float_metric(metrics, "faded_text_changed_pixel_ratio") + _float_metric(
        metrics, "text_edges_changed_pixel_ratio"
    )
    if changed_ratio > options.audit_max_text_combo_changed_pixel_ratio:
        reasons.append("text_combo_changed_pixel_ratio")
    if _float_metric(metrics, "local_content_changed_ratio") > options.audit_max_text_combo_local_changed_ratio:
        reasons.append("text_combo_local_content_changed_ratio")
    if _float_metric(metrics, "edge_content_changed_ratio") > options.audit_max_text_combo_edge_changed_ratio:
        reasons.append("text_combo_edge_content_changed_ratio")
    if _float_metric(metrics, "brightness_delta") > 18.0 or _float_metric(metrics, "contrast_delta") > 24.0:
        reasons.append("text_combo_tonal_delta")
    return reasons


def _combination_non_geometry_candidate_ratio(metrics: dict[str, Any]) -> float:
    return max(
        _float_metric(metrics, "background_stains_candidate_pixel_ratio"),
        _float_metric(metrics, "corner_shadows_candidate_pixel_ratio"),
        _float_metric(metrics, "fold_shadows_candidate_pixel_ratio"),
        _float_metric(metrics, "illumination_gradient_candidate_pixel_ratio"),
        _float_metric(metrics, "bleed_through_candidate_pixel_ratio"),
        _float_metric(metrics, "scanlines_candidate_pixel_ratio"),
        _float_metric(metrics, "faded_text_candidate_pixel_ratio"),
        _float_metric(metrics, "text_edges_candidate_pixel_ratio"),
        _float_metric(metrics, "paper_color_cast_candidate_pixel_ratio"),
    )


def _combination_passed_risk_tier(metrics: dict[str, Any]) -> str:
    if _float_metric(metrics, "crop_ratio") > 0 or _float_metric(metrics, "max_trim_margin_ratio") > 0:
        return "geometry"
    if metrics.get("faded_text_enhanced") is True or metrics.get("text_edges_sharpened") is True:
        return "text_high_frequency"
    return "low_risk_background"


def _processed_output_safety_metrics(source_l: Image.Image, processed_l: Image.Image) -> dict[str, float]:
    comparable = (
        processed_l.resize(source_l.size, Image.Resampling.BILINEAR)
        if processed_l.size != source_l.size
        else processed_l
    )
    width, height = source_l.size
    area = max(1, width * height)
    source_histogram = source_l.histogram()
    processed_histogram = comparable.histogram()
    source_mean = ImageStat.Stat(source_l).mean[0]
    processed_mean = ImageStat.Stat(comparable).mean[0]
    source_near_white_ratio = sum(source_histogram[245:]) / area
    processed_near_white_ratio = sum(processed_histogram[245:]) / area
    source_highlight_clip_ratio = sum(source_histogram[253:]) / area
    processed_highlight_clip_ratio = sum(processed_histogram[253:]) / area
    dark_threshold = _high_contrast_content_threshold(source_histogram, source_l.size)
    source_dark_mask = source_l.point(lambda value: 255 if value <= dark_threshold else 0)
    processed_dark_mask = comparable.point(lambda value: 255 if value <= dark_threshold else 0)
    source_dark_pixels = _mask_pixel_count(source_dark_mask)
    processed_dark_pixels = _mask_pixel_count(processed_dark_mask)
    dark_pixel_loss_ratio = (
        max(0, source_dark_pixels - processed_dark_pixels) / source_dark_pixels
        if source_dark_pixels
        else 0.0
    )
    lifted_dark_mask = comparable.point(lambda value: 255 if value > min(245, dark_threshold + 45) else 0)
    dark_pixel_lift_ratio = (
        _mask_intersection_count(source_dark_mask, lifted_dark_mask) / source_dark_pixels
        if source_dark_pixels
        else 0.0
    )
    return {
        "processed_output_source_mean_luma": round(source_mean, 6),
        "processed_output_mean_luma": round(processed_mean, 6),
        "processed_output_brightness_increase": round(max(0.0, processed_mean - source_mean), 6),
        "processed_output_source_near_white_ratio": round(source_near_white_ratio, 6),
        "processed_output_near_white_ratio": round(processed_near_white_ratio, 6),
        "processed_output_near_white_delta": round(
            max(0.0, processed_near_white_ratio - source_near_white_ratio),
            6,
        ),
        "processed_output_source_highlight_clip_ratio": round(source_highlight_clip_ratio, 6),
        "processed_output_highlight_clip_ratio": round(processed_highlight_clip_ratio, 6),
        "processed_output_highlight_clip_delta": round(
            max(0.0, processed_highlight_clip_ratio - source_highlight_clip_ratio),
            6,
        ),
        "processed_output_source_dark_pixel_ratio": round(source_dark_pixels / area, 6),
        "processed_output_dark_pixel_ratio": round(processed_dark_pixels / area, 6),
        "processed_output_dark_pixel_loss_ratio": round(dark_pixel_loss_ratio, 6),
        "processed_output_dark_pixel_lift_ratio": round(dark_pixel_lift_ratio, 6),
    }


def _processed_output_safety_guard(metrics: dict[str, Any], options: ProcessingOptions) -> dict[str, Any]:
    reasons: list[str] = []
    if metrics.get("pixel_change_guardrail_scope") == "geometric_change_recorded_by_size_crop_trim_or_deskew":
        return {
            "checked": True,
            "action": "passed",
            "reverted": False,
            "reason_code": "safe_processed_output_passed",
            "reasons": [],
        }
    source_mean = _float_metric(metrics, "processed_output_source_mean_luma")
    processed_mean = _float_metric(metrics, "processed_output_mean_luma")
    brightness_increase = _float_metric(metrics, "processed_output_brightness_increase")
    near_white_ratio = _float_metric(metrics, "processed_output_near_white_ratio")
    near_white_delta = _float_metric(metrics, "processed_output_near_white_delta")
    highlight_clip_ratio = _float_metric(metrics, "processed_output_highlight_clip_ratio")
    highlight_clip_delta = _float_metric(metrics, "processed_output_highlight_clip_delta")
    source_dark_ratio = _float_metric(metrics, "processed_output_source_dark_pixel_ratio")
    dark_loss_ratio = _float_metric(metrics, "processed_output_dark_pixel_loss_ratio")
    dark_lift_ratio = _float_metric(metrics, "processed_output_dark_pixel_lift_ratio")

    if (
        near_white_ratio > options.audit_max_processed_near_white_ratio
        and near_white_delta > options.audit_max_processed_near_white_delta
        and processed_mean >= 230.0
    ):
        reasons.append("near_white_saturation")
    if (
        highlight_clip_ratio > options.audit_max_processed_highlight_clip_ratio
        and highlight_clip_delta > options.audit_max_processed_highlight_clip_delta
    ):
        reasons.append("highlight_clipping")
    if (
        source_mean >= 190.0
        and processed_mean >= 225.0
        and brightness_increase > options.audit_max_processed_bright_page_delta
        and near_white_delta > 0.15
    ):
        reasons.append("bright_page_washout")
    if (
        source_dark_ratio >= 0.003
        and brightness_increase > 10.0
        and (
            dark_loss_ratio > options.audit_max_processed_dark_pixel_loss_ratio
            or dark_lift_ratio > options.audit_max_processed_dark_pixel_lift_ratio
        )
    ):
        reasons.append("dark_foreground_loss")
    if (
        _float_metric(metrics, "pixel_change_ratio") > options.audit_max_processed_full_page_change_ratio
        and metrics.get("pixel_change_guardrail_scope") == "same_size_pixel_change"
        and metrics.get("tone_normalized") is not True
        and metrics.get("paper_color_cast_normalized") is not True
    ):
        reasons.append("suspicious_full_page_change")

    return {
        "checked": True,
        "action": "reverted_to_source" if reasons else "passed",
        "reverted": bool(reasons),
        "reason_code": "processed_output_quality_reverted" if reasons else "safe_processed_output_passed",
        "reasons": sorted(set(reasons)),
    }


def _local_content_change_guard(source_l: Image.Image, processed_l: Image.Image, options: ProcessingOptions) -> dict[str, Any]:
    if processed_l.size != source_l.size:
        return _local_content_change_guard_passed(checked=False)

    numpy_guard = _local_content_change_guard_numpy(source_l, processed_l, options)
    if numpy_guard is not None:
        return numpy_guard

    histogram = source_l.histogram()
    threshold = _high_contrast_content_threshold(histogram, source_l.size)
    content_pixels = sum(histogram[: threshold + 1])
    changed_mask = ImageChops.difference(source_l, processed_l).point(lambda value: 255 if value > 32 else 0)
    content_mask = _high_contrast_content_mask(source_l, threshold)
    changed_content_mask = ImageChops.multiply(content_mask, changed_mask)
    width, height = source_l.size
    area = max(1, width * height)
    changed_content = _mask_pixel_count(changed_content_mask)
    local_ratio = changed_content / max(1, content_pixels)
    content_pixel_ratio = content_pixels / area
    max_tile_ratio = 0.0
    edge_ratio = _edge_content_change_ratio(content_mask, changed_content_mask, source_l.size)
    checked = content_pixels >= max(24, int(area * 0.002))
    reasons: list[str] = []
    if checked and local_ratio > options.audit_max_local_content_changed_ratio:
        reasons.append("local_content_changed_ratio")
    if checked and "local_content_changed_ratio" not in reasons:
        max_tile_ratio = _max_local_content_tile_change_ratio(content_mask, changed_content_mask)
    if checked and max_tile_ratio > options.audit_max_local_content_tile_changed_ratio:
        reasons.append("local_content_tile_changed_ratio")
    if checked and edge_ratio > options.audit_max_edge_content_changed_ratio:
        reasons.append("edge_content_changed_ratio")
    action = "reverted_to_source" if reasons else "passed"
    return {
        "checked": checked,
        "action": action,
        "reverted": action == "reverted_to_source",
        "reasons": sorted(set(reasons)),
        "content_pixel_ratio": round(content_pixel_ratio, 6),
        "changed_ratio": round(local_ratio, 6),
        "tile_changed_ratio": round(max_tile_ratio, 6),
        "edge_changed_ratio": round(edge_ratio, 6),
    }


def _local_content_change_guard_numpy(
    source_l: Image.Image,
    processed_l: Image.Image,
    options: ProcessingOptions,
) -> dict[str, Any] | None:
    np = _load_numpy()
    if np is None:
        return None
    try:
        source = np.asarray(source_l)
        processed = np.asarray(processed_l)
    except (TypeError, ValueError):
        return None
    if source.shape != processed.shape or source.ndim != 2:
        return None

    height, width = source.shape
    area = max(1, width * height)
    histogram = np.bincount(source.ravel(), minlength=256)
    running = np.cumsum(histogram)
    background = int(np.searchsorted(running, area * 0.9, side="left"))
    threshold = max(90, min(220, background - 35))
    content = source <= threshold
    content_pixels = int(np.count_nonzero(content))
    checked = content_pixels >= max(24, int(area * 0.002))

    if content_pixels == 0:
        return _local_content_change_guard_passed(checked=False)

    changed = ((source > processed) & ((source - processed) > 32)) | (
        (processed > source) & ((processed - source) > 32)
    )
    changed_content_mask = content & changed
    changed_content = int(np.count_nonzero(changed_content_mask))
    local_ratio = changed_content / content_pixels
    content_pixel_ratio = content_pixels / area
    edge_ratio = _edge_content_change_ratio_numpy(content, changed_content_mask)
    max_tile_ratio = 0.0

    reasons: list[str] = []
    if checked and local_ratio > options.audit_max_local_content_changed_ratio:
        reasons.append("local_content_changed_ratio")
    if checked and "local_content_changed_ratio" not in reasons:
        max_tile_ratio = _max_local_content_tile_change_ratio_numpy(content, changed_content_mask)
    if checked and max_tile_ratio > options.audit_max_local_content_tile_changed_ratio:
        reasons.append("local_content_tile_changed_ratio")
    if checked and edge_ratio > options.audit_max_edge_content_changed_ratio:
        reasons.append("edge_content_changed_ratio")
    action = "reverted_to_source" if reasons else "passed"
    return {
        "checked": checked,
        "action": action,
        "reverted": action == "reverted_to_source",
        "reasons": sorted(set(reasons)),
        "content_pixel_ratio": round(content_pixel_ratio, 6),
        "changed_ratio": round(local_ratio, 6),
        "tile_changed_ratio": round(max_tile_ratio, 6),
        "edge_changed_ratio": round(edge_ratio, 6),
    }


def _local_content_change_guard_passed(*, checked: bool) -> dict[str, Any]:
    return {
        "checked": checked,
        "action": "passed",
        "reverted": False,
        "reasons": [],
        "content_pixel_ratio": 0.0,
        "changed_ratio": 0.0,
        "tile_changed_ratio": 0.0,
        "edge_changed_ratio": 0.0,
    }


def _high_contrast_content_threshold(histogram: list[int], size: tuple[int, int]) -> int:
    total = max(1, size[0] * size[1])
    running = 0
    background = 255
    for value, count in enumerate(histogram):
        running += count
        if running >= total * 0.9:
            background = value
            break
    return max(90, min(220, background - 35))


def _high_contrast_content_mask(source_l: Image.Image, threshold: int | None = None) -> Image.Image:
    if threshold is None:
        threshold = _high_contrast_content_threshold(source_l.histogram(), source_l.size)
    return source_l.point(lambda value: 255 if value <= threshold else 0)


def _max_local_content_tile_change_ratio(content_mask: Image.Image, changed_content_mask: Image.Image) -> float:
    width, height = content_mask.size
    changed_bbox = changed_content_mask.getbbox()
    if changed_bbox is None:
        return 0.0
    tile_size = max(16, min(48, min(width, height) // 3 or 16))
    max_ratio = 0.0
    left_start = (changed_bbox[0] // tile_size) * tile_size
    top_start = (changed_bbox[1] // tile_size) * tile_size
    left_stop = min(width, ((changed_bbox[2] + tile_size - 1) // tile_size) * tile_size)
    top_stop = min(height, ((changed_bbox[3] + tile_size - 1) // tile_size) * tile_size)
    for top in range(top_start, top_stop, tile_size):
        for left in range(left_start, left_stop, tile_size):
            box = (left, top, min(width, left + tile_size), min(height, top + tile_size))
            content_pixels = _mask_pixel_count(content_mask.crop(box))
            if content_pixels < 8:
                continue
            changed_pixels = _mask_pixel_count(changed_content_mask.crop(box))
            max_ratio = max(max_ratio, changed_pixels / content_pixels)
    return max_ratio


def _max_local_content_tile_change_ratio_numpy(content: Any, changed_content: Any) -> float:
    np = _load_numpy()
    if np is None:
        return 0.0
    height, width = content.shape
    changed_y, changed_x = np.nonzero(changed_content)
    if changed_y.size == 0:
        return 0.0
    tile_size = max(16, min(48, min(width, height) // 3 or 16))
    max_ratio = 0.0
    tile_keys = set(zip((changed_y // tile_size).tolist(), (changed_x // tile_size).tolist()))
    for tile_y, tile_x in tile_keys:
        top = tile_y * tile_size
        left = tile_x * tile_size
        tile_content = content[top : min(height, top + tile_size), left : min(width, left + tile_size)]
        content_pixels = int(tile_content.sum())
        if content_pixels < 8:
            continue
        tile_changed = changed_content[top : min(height, top + tile_size), left : min(width, left + tile_size)]
        changed_pixels = int(tile_changed.sum())
        max_ratio = max(max_ratio, changed_pixels / content_pixels)
    return max_ratio


def _edge_content_change_ratio(
    content_mask: Image.Image,
    changed_mask: Image.Image,
    size: tuple[int, int],
) -> float:
    width, height = size
    margin = max(4, min(24, min(width, height) // 8))
    edge = Image.new("L", size, 0)
    edge.paste(255, (0, 0, width, margin))
    edge.paste(255, (0, max(0, height - margin), width, height))
    edge.paste(255, (0, 0, margin, height))
    edge.paste(255, (max(0, width - margin), 0, width, height))
    edge_content = ImageChops.multiply(content_mask, edge)
    edge_content_pixels = _mask_pixel_count(edge_content)
    if edge_content_pixels < 8:
        return 0.0
    return _mask_intersection_count(edge_content, changed_mask) / edge_content_pixels


def _edge_content_change_ratio_numpy(content: Any, changed: Any) -> float:
    height, width = content.shape
    margin = max(4, min(24, min(width, height) // 8))
    edge_content_pixels = 0
    edge_changed_pixels = 0
    for edge_slice in (
        (slice(0, margin), slice(0, width)),
        (slice(max(0, height - margin), height), slice(0, width)),
        (slice(margin, max(margin, height - margin)), slice(0, margin)),
        (slice(margin, max(margin, height - margin)), slice(max(0, width - margin), width)),
    ):
        edge_content = content[edge_slice]
        if edge_content.size == 0:
            continue
        edge_content_pixels += int(edge_content.sum())
        edge_changed_pixels += int((edge_content & changed[edge_slice]).sum())
    if edge_content_pixels < 8:
        return 0.0
    return edge_changed_pixels / edge_content_pixels


def _mask_pixel_count(mask: Image.Image) -> int:
    return mask.histogram()[255]


def _mask_intersection_count(first: Image.Image, second: Image.Image) -> int:
    return _mask_pixel_count(ImageChops.multiply(first, second))


def _local_content_change_guard_audit_fields(guard: dict[str, Any]) -> dict[str, Any]:
    return {
        "local_content_change_guard_checked": guard.get("checked") is True,
        "local_content_change_guard_action": guard.get("action", "passed"),
        "local_content_change_guard_reverted": guard.get("reverted") is True,
        "local_content_change_guard_reasons": guard.get("reasons") if isinstance(guard.get("reasons"), list) else [],
        "local_content_pixel_ratio": _round_guard_metric(guard.get("content_pixel_ratio")),
        "local_content_changed_ratio": _round_guard_metric(guard.get("changed_ratio")),
        "local_content_tile_changed_ratio": _round_guard_metric(guard.get("tile_changed_ratio")),
        "edge_content_changed_ratio": _round_guard_metric(guard.get("edge_changed_ratio")),
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


def _combination_quality_guard_audit_fields(guard: dict[str, Any]) -> dict[str, Any]:
    reason_code = guard.get("reason_code")
    risk_tier = guard.get("risk_tier")
    return {
        "combination_quality_guard_checked": guard.get("checked") is True,
        "combination_quality_guard_action": guard.get("action", "passed"),
        "combination_quality_guard_reverted": guard.get("reverted") is True,
        "combination_quality_guard_reason_code": reason_code if isinstance(reason_code, str) else "safe_combination_passed",
        "combination_quality_guard_risk_tier": risk_tier if isinstance(risk_tier, str) else "low_risk_background",
        "combination_quality_guard_reasons": guard.get("reasons") if isinstance(guard.get("reasons"), list) else [],
    }


def _processed_output_safety_guard_audit_fields(guard: dict[str, Any]) -> dict[str, Any]:
    reason_code = guard.get("reason_code")
    return {
        "processed_output_safety_guard_checked": guard.get("checked") is True,
        "processed_output_safety_guard_action": guard.get("action", "passed"),
        "processed_output_safety_guard_reverted": guard.get("reverted") is True,
        "processed_output_safety_guard_reason_code": (
            reason_code if isinstance(reason_code, str) else "safe_processed_output_passed"
        ),
        "processed_output_safety_guard_reasons": guard.get("reasons") if isinstance(guard.get("reasons"), list) else [],
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


def _safe_deskew_skip_from_page_evidence(image: Image.Image) -> SkewDetection | None:
    width, height = image.size
    if width < 30 or height < 30:
        return SkewDetection(None, 0.0, "image too small")

    raw_grayscale = image.convert("L")
    raw_histogram = raw_grayscale.histogram()
    total_pixels = width * height
    raw_low = _histogram_percentile(raw_histogram, total_pixels, 0.005)
    raw_high = _histogram_percentile(raw_histogram, total_pixels, 0.995)
    raw_span = raw_high - raw_low
    if raw_span < 8:
        return SkewDetection(None, 0.0, "low contrast")

    grayscale = ImageOps.autocontrast(raw_grayscale, cutoff=1)
    histogram = grayscale.histogram()
    low = _histogram_percentile(histogram, total_pixels, 0.05)
    high = _histogram_percentile(histogram, total_pixels, 0.95)
    if high - low < 35:
        if raw_high < 220 or raw_span < 8:
            return SkewDetection(None, 0.0, "low contrast")
        if raw_span < 35:
            return None
        sparse_threshold = max(0, raw_high - 35)
        sparse_foreground = sum(raw_histogram[: sparse_threshold + 1]) / total_pixels
        if sparse_foreground < 0.002:
            return SkewDetection(None, 0.0, "low contrast")
        return None

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
    if not _has_deskew_line_evidence(ink, bbox):
        return SkewDetection(None, 0.0, "low confidence")
    return None


def _deskew_safe_skip_reason_code(reason: str) -> str:
    return {
        "image too small": "image_too_small",
        "low contrast": "low_contrast",
        "blank page": "blank_page",
        "insufficient foreground": "insufficient_foreground",
        "foreground too dense": "foreground_too_dense",
        "low confidence": "low_line_evidence",
    }.get(reason, "no_reliable_skew_candidate")


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
    edge_sides = _dark_border_edge_sides_from_runs(left, width - right, top, height - bottom)
    band_width_bucket = _dark_border_band_width_bucket(max(left, width - right, top, height - bottom))
    return DarkBorderDetection(
        (left, top, right, bottom),
        reason,
        _dark_border_reason_code(reason),
        edge_sides,
        band_width_bucket,
    )


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
        ("paper_color_cast_delta", 12.0),
        ("paper_color_cast_brightness_delta", 4.0),
        ("paper_color_cast_changed_pixel_ratio", 1.0),
        ("paper_color_cast_candidate_pixel_ratio", 1.0),
        ("corner_shadows_changed_pixel_ratio", 0.06),
        ("corner_shadows_candidate_pixel_ratio", 0.10),
        ("fold_shadows_changed_pixel_ratio", 0.075),
        ("fold_shadows_candidate_pixel_ratio", 0.12),
        ("bleed_through_changed_pixel_ratio", 0.045),
        ("bleed_through_candidate_pixel_ratio", 0.065),
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
        if key.startswith("bleed_through_") and metrics.get("bleed_through_cleaned") is not True:
            continue
        if key.startswith("corner_shadows_") and metrics.get("corner_shadows_lightened") is not True:
            continue
        if key.startswith("fold_shadows_") and metrics.get("fold_shadows_lightened") is not True:
            continue
        if key.startswith("paper_color_cast_") and metrics.get("paper_color_cast_normalized") is not True:
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
    raw_histogram = raw_grayscale.histogram()
    total_pixels = width * height
    low = _histogram_percentile(histogram, total_pixels, 0.05)
    high = _histogram_percentile(histogram, total_pixels, 0.95)
    raw_low = _histogram_percentile(raw_histogram, total_pixels, 0.005)
    raw_high = _histogram_percentile(raw_histogram, total_pixels, 0.995)
    if high - low < 35:
        if raw_high - raw_low < 35:
            faint = _detect_faint_stable_text_skew(raw_grayscale, raw_low, raw_high)
            if faint.angle_degrees is not None:
                return faint
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
    if abs(skew_angle) < 0.2 or confidence < 0.08:
        shallow = _detect_shallow_stable_text_skew(image, ink, bbox, raw_high=raw_high)
        if shallow.angle_degrees is not None:
            return shallow
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


def _deskew_has_color_or_table_risk(image: Image.Image) -> bool:
    if image.mode == "RGB" and _tone_color_risk_reason(image):
        return True

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
    return bool(bbox and _deskew_has_table_line_risk(ink, bbox))


def _detect_shallow_stable_text_skew(
    image: Image.Image,
    ink: Image.Image,
    bbox: tuple[int, int, int, int],
    *,
    raw_high: int,
) -> SkewDetection:
    if raw_high < 220:
        return SkewDetection(None, 0.0, "low confidence")
    if _deskew_has_table_line_risk(ink, bbox):
        return SkewDetection(None, 0.0, "low confidence")

    sample = ink.crop(bbox)
    sample.thumbnail((700, 700), Image.Resampling.BILINEAR)
    sample_width, sample_height = sample.size
    if sample_width < 80 or sample_height < 60:
        return SkewDetection(None, 0.0, "low confidence")

    projection = sample.resize((1, sample_height), Image.Resampling.BOX)
    row_counts = [value * sample_width / 255 for value in projection.tobytes()]
    active_threshold = max(10.0, sample_width * 0.10)
    groups: list[tuple[int, int]] = []
    start: int | None = None
    for row, count in enumerate(row_counts):
        if count >= active_threshold:
            if start is None:
                start = row
        elif start is not None:
            groups.append((start, row))
            start = None
    if start is not None:
        groups.append((start, sample_height))
    groups = _merge_close_row_groups(groups, max_gap=2)

    line_angles: list[float] = []
    pixels = sample.load()
    max_band_height = max(8, int(round(sample_height * 0.045)))
    min_line_width = max(50, int(round(sample_width * 0.38)))
    for top, bottom in groups:
        if bottom - top > max_band_height:
            continue
        points: list[tuple[int, int]] = []
        min_x: int | None = None
        max_x: int | None = None
        for y in range(top, bottom):
            for x in range(sample_width):
                if pixels[x, y]:
                    points.append((x, y))
                    min_x = x if min_x is None else min(min_x, x)
                    max_x = x if max_x is None else max(max_x, x)
        if min_x is None or max_x is None or max_x - min_x < min_line_width:
            continue
        angle = _least_squares_line_angle(points)
        if angle is not None and 0.18 <= abs(angle) <= 1.25:
            line_angles.append(angle)

    if len(line_angles) < 4:
        return SkewDetection(None, 0.0, "low confidence")
    average_angle = sum(line_angles) / len(line_angles)
    spread = max(abs(angle - average_angle) for angle in line_angles)
    if spread > 0.35:
        return SkewDetection(None, 0.0, "low confidence")
    confidence = max(0.0, min(1.0, 0.24 + len(line_angles) * 0.045 - spread * 0.35))
    return SkewDetection(round(-average_angle, 2), round(confidence, 3), "shallow stable text skew detected")


def _detect_faint_stable_text_skew(
    raw_grayscale: Image.Image,
    raw_low: int,
    raw_high: int,
) -> SkewDetection:
    if raw_high < 220:
        return SkewDetection(None, 0.0, "low contrast")

    raw_span = raw_high - raw_low
    if raw_span < 8:
        return SkewDetection(None, 0.0, "low contrast")

    # Low-contrast scanner text can sit far above the normal foreground threshold.
    # Use a narrow paper-relative threshold, then require stable text-line geometry.
    threshold = max(0, raw_high - max(6, min(24, int(round(raw_span * 0.75)))))
    ink = raw_grayscale.point(lambda value: 255 if value <= threshold else 0, mode="L")
    bbox = ink.getbbox()
    if not bbox:
        return SkewDetection(None, 0.0, "low contrast")

    ink_ratio = _nonzero_ratio(ink, bbox)
    if ink_ratio < 0.003 or ink_ratio > 0.25:
        return SkewDetection(None, 0.0, "low contrast")

    shallow = _detect_shallow_stable_text_skew(raw_grayscale, ink, bbox, raw_high=raw_high)
    if shallow.angle_degrees is None:
        return SkewDetection(None, 0.0, "low contrast")

    confidence = max(shallow.confidence, 0.14)
    return SkewDetection(shallow.angle_degrees, round(confidence, 3), "faint stable text skew detected")


def _merge_close_row_groups(groups: list[tuple[int, int]], *, max_gap: int) -> list[tuple[int, int]]:
    if not groups:
        return []
    merged = [groups[0]]
    for top, bottom in groups[1:]:
        prev_top, prev_bottom = merged[-1]
        if top - prev_bottom <= max_gap:
            merged[-1] = (prev_top, bottom)
        else:
            merged.append((top, bottom))
    return merged


def _least_squares_line_angle(points: list[tuple[int, int]]) -> float | None:
    if len(points) < 20:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    variance_x = sum((point[0] - mean_x) ** 2 for point in points)
    if variance_x <= 0:
        return None
    covariance = sum((point[0] - mean_x) * (point[1] - mean_y) for point in points)
    slope = covariance / variance_x
    return math.degrees(math.atan(slope))


def _deskew_has_table_line_risk(ink: Image.Image, bbox: tuple[int, int, int, int]) -> bool:
    sample = ink.crop(bbox)
    sample.thumbnail((700, 700), Image.Resampling.BILINEAR)
    width, height = sample.size
    if width <= 0 or height <= 0:
        return False
    pixels = sample.load()
    long_vertical_runs = 0
    vertical_min_run = max(18, int(round(height * 0.20)))
    for x in range(width):
        run = 0
        for y in range(height):
            run = run + 1 if pixels[x, y] else 0
            if run >= vertical_min_run:
                long_vertical_runs += 1
                break
    if long_vertical_runs >= max(16, int(round(width * 0.05))):
        return True

    return False


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
    if page_background < 135:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")
    if abs(page_background - canvas) < 3.0:
        return _detect_post_deskew_corner_wedge_crop_bbox(image, grayscale, canvas, page_background)

    max_x = min(width // 5, max(4, int(width * 0.14)))
    max_y = min(height // 5, max(4, int(height * 0.14)))
    threshold = max(3.0, min(7.0, abs(page_background - canvas) * 0.25))
    left = _first_post_deskew_canvas_edge(grayscale, canvas, "left", max_x, threshold)
    right_margin = _first_post_deskew_canvas_edge(grayscale, canvas, "right", max_x, threshold)
    top = _first_post_deskew_canvas_edge(grayscale, canvas, "top", max_y, threshold)
    bottom_margin = _first_post_deskew_canvas_edge(grayscale, canvas, "bottom", max_y, threshold)
    if None in {left, right_margin, top, bottom_margin}:
        return _detect_post_deskew_corner_wedge_crop_bbox(image, grayscale, canvas, page_background)
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


def _detect_post_deskew_corner_wedge_crop_bbox(
    image: Image.Image,
    grayscale: Image.Image,
    canvas: float,
    page_background: float,
) -> CropDetection:
    if image.mode == "RGB" and _tone_color_risk_reason(image):
        return CropDetection(None, "post-deskew crop skipped: edge content protection")

    width, height = grayscale.size
    contrast = abs(page_background - canvas)
    if contrast < 0.75:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")

    threshold = max(0.75, min(5.0, contrast * 0.75))
    max_x = min(width // 10, max(3, int(width * 0.08)))
    max_y = min(height // 10, max(3, int(height * 0.08)))
    left = _first_post_deskew_corner_wedge_edge(grayscale, canvas, "left", max_x, threshold)
    right_margin = _first_post_deskew_corner_wedge_edge(grayscale, canvas, "right", max_x, threshold)
    top = _first_post_deskew_corner_wedge_edge(grayscale, canvas, "top", max_y, threshold)
    bottom_margin = _first_post_deskew_corner_wedge_edge(grayscale, canvas, "bottom", max_y, threshold)

    margins = (
        left or 0,
        top or 0,
        right_margin or 0,
        bottom_margin or 0,
    )
    active_margins = tuple(margin for margin in margins if margin >= 2)
    if len(active_margins) < 2:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")

    left = left or 0
    top = top or 0
    right_margin = right_margin or 0
    bottom_margin = bottom_margin or 0
    right = width - right_margin
    bottom = height - bottom_margin
    if right <= left or bottom <= top:
        return CropDetection(None, "post-deskew crop skipped: crop risk too large")

    max_trim_ratio = max(left / width, right_margin / width, top / height, bottom_margin / height)
    if max_trim_ratio > 0.08:
        return CropDetection(None, "post-deskew crop skipped: crop risk too large")

    crop_area_ratio = ((right - left) * (bottom - top)) / max(1, width * height)
    if crop_area_ratio < 0.88:
        return CropDetection(None, "post-deskew crop skipped: crop risk too large")
    if crop_area_ratio > 0.995:
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")

    bbox = (left, top, right, bottom)
    active = (left, top, right_margin, bottom_margin)
    if not _post_deskew_corner_wedge_has_boundary_evidence(grayscale, bbox, active, canvas, threshold):
        return CropDetection(None, "post-deskew crop skipped: low-confidence canvas edge")
    if _post_deskew_corner_wedge_trimmed_area_has_marks(grayscale, active, canvas, threshold):
        return CropDetection(None, "post-deskew crop skipped: edge content protection")
    if _has_protected_dark_content_near_active_trim_boundary(grayscale, bbox, active):
        return CropDetection(None, "post-deskew crop skipped: edge content protection")
    if not _has_inset_document_content_for_scanner_gutter(grayscale, bbox, active):
        return CropDetection(None, "post-deskew crop skipped: edge content protection")
    return CropDetection(bbox, "post-deskew safe canvas crop applied")


def _first_post_deskew_corner_wedge_edge(
    image: Image.Image,
    canvas: float,
    side: str,
    max_margin: int,
    threshold: float,
) -> int | None:
    width, height = image.size
    required_run = 2
    run_start: int | None = None
    run_length = 0
    for offset in range(1, max_margin + 1):
        if side == "left":
            box = (offset, 0, min(width, offset + 1), height)
        elif side == "right":
            box = (max(0, width - offset - 1), 0, width - offset, height)
        elif side == "top":
            box = (0, offset, width, min(height, offset + 1))
        else:
            box = (0, max(0, height - offset - 1), width, height - offset)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        values = image.crop(box).tobytes()
        page_like = sum(1 for value in values if abs(value - canvas) >= threshold)
        if page_like / max(1, len(values)) >= 0.85:
            if run_start is None:
                run_start = offset
            run_length += 1
            if run_length >= required_run:
                return run_start
        else:
            run_start = None
            run_length = 0
    return None


def _post_deskew_corner_wedge_has_boundary_evidence(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    active_margins: tuple[int, int, int, int],
    canvas: float,
    threshold: float,
) -> bool:
    left, top, right, bottom = bbox
    left_margin, top_margin, right_margin, bottom_margin = active_margins
    bands: list[tuple[int, int, int, int]] = []
    if left_margin:
        bands.append((left, top, min(right, left + 1), bottom))
    if right_margin:
        bands.append((max(left, right - 1), top, right, bottom))
    if top_margin:
        bands.append((left, top, right, min(bottom, top + 1)))
    if bottom_margin:
        bands.append((left, max(top, bottom - 1), right, bottom))
    if not bands:
        return False
    for band in bands:
        values = image.crop(band).tobytes()
        page_like = sum(1 for value in values if abs(value - canvas) >= threshold)
        if page_like / max(1, len(values)) < 0.85:
            return False
    return True


def _post_deskew_corner_wedge_trimmed_area_has_marks(
    image: Image.Image,
    active_margins: tuple[int, int, int, int],
    canvas: float,
    threshold: float,
) -> bool:
    width, height = image.size
    left, top, right_margin, bottom_margin = active_margins
    boxes = []
    if left:
        boxes.append((0, 0, left, height))
    if right_margin:
        boxes.append((width - right_margin, 0, width, height))
    if top:
        boxes.append((left, 0, width - right_margin, top))
    if bottom_margin:
        boxes.append((left, height - bottom_margin, width - right_margin, height))
    mark_threshold = max(12.0, threshold * 4.0)
    for box in boxes:
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        values = image.crop(box).tobytes()
        marked_pixels = sum(1 for value in values if abs(value - canvas) >= mark_threshold)
        if marked_pixels >= 6 and marked_pixels / max(1, len(values)) >= 0.004:
            return True
    return False


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


def _detect_light_scanner_gutter_bbox(image: Image.Image) -> ScannerGutterTrimDetection:
    width, height = image.size
    empty_margins = {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}
    if width < 80 or height < 80:
        return ScannerGutterTrimDetection(None, "image too small", empty_margins)

    if _has_colored_scanner_gutter_risk(image):
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: colored or non-neutral original", empty_margins)

    grayscale = image.convert("L")
    if _light_page_background_mean(grayscale) < 220:
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: page background not light", empty_margins)

    max_x = max(3, min(24, int(width * 0.06)))
    max_y = max(3, min(24, int(height * 0.06)))
    left = _light_scanner_gutter_run(grayscale, "left", max_x)
    right = _light_scanner_gutter_run(grayscale, "right", max_x)
    top = _light_scanner_gutter_run(grayscale, "top", max_y)
    bottom = _light_scanner_gutter_run(grayscale, "bottom", max_y)

    if max(left, right, top, bottom) < 3:
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: no narrow uniform light band", empty_margins)
    if sum(1 for margin in (left, right, top, bottom) if margin) > 2:
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: ambiguous multi-edge light band", empty_margins)

    bbox = (left, top, width - right, height - bottom)
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: invalid trim candidate", empty_margins)
    retained_area_ratio = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / max(1, width * height)
    if retained_area_ratio < 0.88:
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: trim exceeds narrow margin limit", empty_margins)
    if _light_scanner_gutter_trimmed_area_has_marks(grayscale, (left, top, right, bottom)):
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: protected edge content", empty_margins)
    if _has_protected_dark_content_near_active_trim_boundary(grayscale, bbox, (left, top, right, bottom)):
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: protected edge content", empty_margins)
    if not _has_inset_document_content_for_scanner_gutter(grayscale, bbox, (left, top, right, bottom)):
        return ScannerGutterTrimDetection(None, "scanner gutter skipped: no inset content evidence", empty_margins)

    margins = _trim_margins((width, height), bbox)
    return ScannerGutterTrimDetection(bbox, "scanner gutter trim applied", margins)


def _light_scanner_gutter_run(image: Image.Image, side: str, max_pixels: int) -> int:
    width, height = image.size
    if side == "left":
        reference = image.crop((min(width - 1, max_pixels + 2), 0, min(width, max_pixels + 8), height))
    elif side == "right":
        reference = image.crop((max(0, width - max_pixels - 8), 0, max(1, width - max_pixels - 2), height))
    elif side == "top":
        reference = image.crop((0, min(height - 1, max_pixels + 2), width, min(height, max_pixels + 8)))
    else:
        reference = image.crop((0, max(0, height - max_pixels - 8), width, max(1, height - max_pixels - 2)))
    run = 0
    for offset in range(max_pixels):
        if side == "left":
            box = (offset, 0, offset + 1, height)
        elif side == "right":
            box = (width - 1 - offset, 0, width - offset, height)
        elif side == "top":
            box = (0, offset, width, offset + 1)
        else:
            box = (0, height - 1 - offset, width, height - offset)
        if not _is_uniform_light_scanner_gutter_band(image.crop(box), reference):
            break
        run = offset + 1
    return run if run >= 3 else 0


def _is_uniform_light_scanner_gutter_band(band: Image.Image, inner: Image.Image) -> bool:
    stat = ImageStat.Stat(band)
    mean = stat.mean[0]
    if mean < 215 or mean > 252 or stat.stddev[0] > 5.5:
        return False
    values = band.tobytes()
    if not values:
        return False
    dark_or_marked = sum(1 for value in values if value <= 180)
    if dark_or_marked / len(values) > 0.001:
        return False
    inner_mean = ImageStat.Stat(inner).mean[0] if inner.size[0] and inner.size[1] else 255
    contrast = abs(inner_mean - mean)
    return inner_mean >= 225 and 4.0 <= contrast <= 18.0


def _has_colored_scanner_gutter_risk(image: Image.Image) -> bool:
    sample = image.convert("RGB")
    sample.thumbnail((160, 160))
    pixel_bytes = sample.tobytes()
    if not pixel_bytes:
        return False

    colored = 0
    colored_light_background = 0
    total = len(pixel_bytes) // 3
    for offset in range(0, len(pixel_bytes), 3):
        red, green, blue = pixel_bytes[offset], pixel_bytes[offset + 1], pixel_bytes[offset + 2]
        channel_range = max(red, green, blue) - min(red, green, blue)
        luminance = (red + green + blue) / 3
        if channel_range >= 34:
            colored += 1
            if luminance >= 170:
                colored_light_background += 1

    return (colored / total) >= 0.01 or (colored_light_background / total) >= 0.004


def _light_scanner_gutter_trimmed_area_has_marks(
    image: Image.Image,
    active_margins: tuple[int, int, int, int],
) -> bool:
    width, height = image.size
    left, top, right, bottom = active_margins
    boxes = []
    if left:
        boxes.append((0, 0, left, height))
    if right:
        boxes.append((width - right, 0, width, height))
    if top:
        boxes.append((left, 0, width - right, top))
    if bottom:
        boxes.append((left, height - bottom, width - right, height))
    for box in boxes:
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        values = image.crop(box).tobytes()
        marked_pixels = sum(1 for value in values if value <= 185)
        if marked_pixels >= 3:
            return True
    return False


def _has_protected_dark_content_near_active_trim_boundary(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    active_margins: tuple[int, int, int, int],
) -> bool:
    width, height = image.size
    left, top, right, bottom = bbox
    left_margin, top_margin, right_margin, bottom_margin = active_margins
    protect_depth = max(3, min(10, int(min(width, height) * 0.04)))
    corner_pad_x = max(2, int(width * 0.04))
    corner_pad_y = max(2, int(height * 0.04))
    bands: list[tuple[int, int, int, int]] = []
    if left_margin:
        bands.append((left, top + corner_pad_y, min(right, left + protect_depth), bottom - corner_pad_y))
    if right_margin:
        bands.append((max(left, right - protect_depth), top + corner_pad_y, right, bottom - corner_pad_y))
    if top_margin:
        bands.append((left + corner_pad_x, top, right - corner_pad_x, min(bottom, top + protect_depth)))
    if bottom_margin:
        bands.append((left + corner_pad_x, max(top, bottom - protect_depth), right - corner_pad_x, bottom))
    for band in bands:
        x0, y0, x1, y1 = band
        if x1 <= x0 or y1 <= y0:
            continue
        values = image.crop(band).tobytes()
        dark_pixels = sum(1 for value in values if value <= 90)
        if dark_pixels >= 8 and dark_pixels / max(1, len(values)) >= 0.01:
            return True
    return False


def _has_inset_document_content_for_scanner_gutter(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    active_margins: tuple[int, int, int, int],
) -> bool:
    width, height = image.size
    left, top, right, bottom = bbox
    left_margin, top_margin, right_margin, bottom_margin = active_margins
    inset_x = max(8, int(width * 0.08))
    inset_y = max(8, int(height * 0.08))
    content_box = (
        min(right, left + inset_x),
        min(bottom, top + inset_y),
        max(left, right - inset_x),
        max(top, bottom - inset_y),
    )
    if content_box[2] <= content_box[0] or content_box[3] <= content_box[1]:
        return False
    values = image.crop(content_box).tobytes()
    dark_content = sum(1 for value in values if value <= 170)
    if dark_content < max(12, int(len(values) * 0.0008)):
        return False

    # Active-edge gutters are only safe when the document marks are clearly inset
    # from the new crop boundary, not merely sitting on the paper edge.
    protect_depth = max(6, int(min(width, height) * 0.06))
    edge_boxes: list[tuple[int, int, int, int]] = []
    if left_margin:
        edge_boxes.append((left, top, min(right, left + protect_depth), bottom))
    if right_margin:
        edge_boxes.append((max(left, right - protect_depth), top, right, bottom))
    if top_margin:
        edge_boxes.append((left, top, right, min(bottom, top + protect_depth)))
    if bottom_margin:
        edge_boxes.append((left, max(top, bottom - protect_depth), right, bottom))
    for edge_box in edge_boxes:
        if edge_box[2] <= edge_box[0] or edge_box[3] <= edge_box[1]:
            continue
        edge_values = image.crop(edge_box).tobytes()
        edge_dark_content = sum(1 for value in edge_values if value <= 170)
        if edge_dark_content >= 6:
            return False

    return True


def _detect_dark_border_bbox(image: Image.Image) -> DarkBorderDetection:
    width, height = image.size
    if width < 40 or height < 40:
        reason = "image too small"
        return DarkBorderDetection(None, reason, _dark_border_reason_code(reason))

    grayscale = image.convert("L")
    if _light_page_background_mean(grayscale) < 135:
        reason = "no light page background for dark border trim"
        return DarkBorderDetection(None, reason, _dark_border_reason_code(reason))

    max_x = max(2, int(width * 0.08))
    max_y = max(2, int(height * 0.08))
    min_retain_width = int(width * 0.88)
    min_retain_height = int(height * 0.88)
    left, left_broken = _dark_edge_run(grayscale, "left", max_x)
    right, right_broken = _dark_edge_run(grayscale, "right", max_x)
    top, top_broken = _dark_edge_run(grayscale, "top", max_y)
    bottom, bottom_broken = _dark_edge_run(grayscale, "bottom", max_y)
    runs = (left, right, top, bottom)
    has_broken_edge = left_broken or right_broken or top_broken or bottom_broken
    edge_sides = _dark_border_edge_sides_from_runs(left, right, top, bottom)
    band_width_bucket = _dark_border_band_width_bucket(max(runs))

    if max(runs) < 2:
        reason = "no confident dark edge border"
        return DarkBorderDetection(None, reason, _dark_border_reason_code(reason), edge_sides, band_width_bucket)
    if min(runs) < 2:
        reason = "incomplete dark edge border evidence"
        return DarkBorderDetection(None, reason, _dark_border_reason_code(reason), edge_sides, band_width_bucket)
    if max(runs) > max(min(runs) * 3, min(runs) + max(2, int(min(width, height) * 0.04))):
        reason = "unbalanced dark edge border evidence"
        return DarkBorderDetection(None, reason, _dark_border_reason_code(reason), edge_sides, band_width_bucket)

    bbox = (left, top, width - right, height - bottom)
    retained_width = bbox[2] - bbox[0]
    retained_height = bbox[3] - bbox[1]
    if retained_width < min_retain_width or retained_height < min_retain_height:
        reason = "candidate trim exceeds conservative retain ratio"
        return DarkBorderDetection(None, reason, _dark_border_reason_code(reason), edge_sides, band_width_bucket)
    if retained_width <= 0 or retained_height <= 0:
        reason = "invalid trim candidate"
        return DarkBorderDetection(None, reason, _dark_border_reason_code(reason), edge_sides, band_width_bucket)
    if _has_protected_dark_content_near_trim_boundary(grayscale, bbox):
        reason = "protected edge content near dark border"
        return DarkBorderDetection(None, reason, _dark_border_reason_code(reason), edge_sides, band_width_bucket)

    reason = "broken dark edge border trimmed" if has_broken_edge else "dark edge border trimmed"
    return DarkBorderDetection(bbox, reason, _dark_border_reason_code(reason), edge_sides, band_width_bucket)


def _dark_border_edge_sides_from_runs(left: int, right: int, top: int, bottom: int) -> tuple[str, ...]:
    return tuple(
        side
        for side, run in (("left", left), ("right", right), ("top", top), ("bottom", bottom))
        if run >= 2
    )


def _dark_border_band_width_bucket(width: int) -> str | None:
    if width <= 0:
        return None
    if width <= 2:
        return "2px"
    if width <= 4:
        return "3-4px"
    if width <= 8:
        return "5-8px"
    if width <= 12:
        return "9-12px"
    return "13px+"


def _dark_border_reason_code(reason: str | None) -> str | None:
    if not reason:
        return None
    return {
        "dark edge border trimmed": "trimmed_continuous_edge",
        "broken dark edge border trimmed": "trimmed_broken_edge",
        "dark border trim disabled": "disabled",
        "image too small": "image_too_small",
        "no light page background for dark border trim": "no_light_page_background",
        "no confident dark edge border": "no_confident_dark_edge_border",
        "incomplete dark edge border evidence": "incomplete_dark_edge_border_evidence",
        "unbalanced dark edge border evidence": "unbalanced_dark_edge_border_evidence",
        "candidate trim exceeds conservative retain ratio": "candidate_trim_exceeds_conservative_retain_ratio",
        "invalid trim candidate": "invalid_trim_candidate",
        "protected edge content near dark border": "protected_edge_content_near_dark_border",
        "reverted by local content change guard": "reverted_by_local_content_change_guard",
        "reverted by processed output safety guard": "reverted_by_processed_output_safety_guard",
        "reverted by geometric combination guard": "reverted_by_geometric_combination_guard",
        "reverted by text high-frequency combination guard": "reverted_by_text_high_frequency_combination_guard",
        "reverted by combined change guard": "reverted_by_combined_change_guard",
        "reverted by cumulative change guard": "reverted_by_cumulative_change_guard",
    }.get(reason, "guardrail_reverted_or_unknown")


def _dark_edge_run(image: Image.Image, side: str, max_pixels: int) -> tuple[int, bool]:
    width, height = image.size
    pixels = image.load()
    run = 0
    used_broken_edge = False
    for offset in range(max_pixels):
        if side == "left":
            values = [pixels[offset, y] for y in range(height)]
        elif side == "right":
            values = [pixels[width - 1 - offset, y] for y in range(height)]
        elif side == "top":
            values = [pixels[x, offset] for x in range(width)]
        else:
            values = [pixels[x, height - 1 - offset] for x in range(width)]
        if _is_continuous_dark_edge_line(values):
            run = offset + 1
        elif _is_broken_dark_edge_line(values):
            run = offset + 1
            used_broken_edge = True
        else:
            break
    return run, used_broken_edge


def _is_continuous_dark_edge_line(values: list[int]) -> bool:
    dark_ratio = sum(1 for value in values if value <= 70) / len(values)
    deep_gray_ratio = sum(1 for value in values if value <= 110) / len(values)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    stddev = variance**0.5
    return (dark_ratio >= 0.70 and mean <= 105) or (
        deep_gray_ratio >= 0.96 and mean <= 118 and stddev <= 18
    )


def _is_broken_dark_edge_line(values: list[int]) -> bool:
    length = len(values)
    if length < 40:
        return False

    dark_pixels = [value <= 70 for value in values]
    deep_gray_pixels = [value <= 110 for value in values]
    light_pixels = [value >= 170 for value in values]
    dark_ratio = sum(1 for is_dark in dark_pixels if is_dark) / length
    deep_gray_ratio = sum(1 for is_gray in deep_gray_pixels if is_gray) / length
    mean = sum(values) / length
    if not ((dark_ratio >= 0.55 and mean <= 125) or (deep_gray_ratio >= 0.62 and mean <= 145)):
        return False

    light_runs = _boolean_runs(light_pixels)
    if not light_runs:
        return False
    total_light_gap = sum(end - start for start, end in light_runs)
    longest_light_gap = max(end - start for start, end in light_runs)
    total_light_gap_ratio = total_light_gap / length
    longest_light_gap_ratio = longest_light_gap / length
    if len(light_runs) > 2:
        if len(light_runs) > 4:
            return False
        if total_light_gap_ratio > 0.28 or longest_light_gap_ratio > 0.10:
            return False
        min_longest_dark_run_ratio = 0.18
    elif total_light_gap_ratio > 0.34 or longest_light_gap_ratio > 0.34:
        return False
    else:
        min_longest_dark_run_ratio = 0.35

    dark_runs = _boolean_runs(deep_gray_pixels)
    if not dark_runs:
        return False
    longest_dark_run = max(end - start for start, end in dark_runs)
    return longest_dark_run / length >= min_longest_dark_run_ratio


def _boolean_runs(flags: list[bool]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(flags)))
    return runs


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
_DESPECKLE_LIGHT_SOIL_MIN_BACKGROUND = 205
_DESPECKLE_LIGHT_SOIL_MIN_DELTA = 18
_DESPECKLE_LIGHT_SOIL_MAX_VALUE = 226
_DESPECKLE_NEAR_DARK_THRESHOLD = 90
_DESPECKLE_MIN_BACKGROUND_MEDIAN = 120
_DESPECKLE_MAX_CANDIDATE_RATIO = 0.02
_DESPECKLE_MAX_LIGHT_SOIL_PREFILTER_RATIO = _DESPECKLE_MAX_CANDIDATE_RATIO
_DESPECKLE_MAX_CHANGED_RATIO = 0.01
_DESPECKLE_MAX_COMPONENT_PIXELS = 4
_DESPECKLE_MAX_COMPONENT_SPAN = 3
_DESPECKLE_DENSE_PREFILTER_MIN_DARK_PIXELS = 512
_DESPECKLE_DENSE_PREFILTER_MAX_LOW_CONNECTIVITY_RATIO = 0.01
_DESPECKLE_CONTENT_CONTEXT_RADIUS = 8
_DESPECKLE_CONTENT_CONTEXT_MIN_DARK_PIXELS = 6
_DESPECKLE_SPARSE_TEXT_CLEARANCE_RADIUS = 5
_DESPECKLE_SPARSE_TEXT_MIN_BACKGROUND_MEDIAN = 220
_DESPECKLE_SPARSE_TEXT_MAX_NEARBY_CONTENT_PIXELS = 64
_DESPECKLE_NEIGHBOR_OFFSETS = tuple(
    (offset_x, offset_y)
    for offset_y in (-1, 0, 1)
    for offset_x in (-1, 0, 1)
    if offset_x or offset_y
)


def _despeckle_isolated_pixels(image: Image.Image, *, backend: str = "fallback") -> tuple[Image.Image, int, str]:
    result = _despeckle_isolated_pixels_with_reason(image, backend=backend)
    return result.image, result.changed_pixels, result.backend_mode


def _despeckle_isolated_pixels_with_reason(image: Image.Image, *, backend: str = "fallback") -> DespeckleResult:
    if backend not in {"fallback", "numpy"}:
        raise ValueError("despeckle backend must be fallback or numpy")

    grayscale = image.convert("L")
    width, height = grayscale.size
    if width < 3 or height < 3:
        return _despeckle_result(
            image,
            changed_pixels=0,
            backend_mode="not_applicable",
            reason="despeckle not applicable to very small image",
            candidate_pixels=0,
            candidate_count=0,
            replacement_work_performed=False,
        )

    candidate_mask, prefilter_reason = _despeckle_candidate_mask(image, grayscale)
    if prefilter_reason is not None:
        return _despeckle_result(
            image,
            changed_pixels=0,
            backend_mode="not_applicable",
            reason=prefilter_reason,
            candidate_pixels=0,
            candidate_count=0,
            replacement_work_performed=False,
        )
    if not candidate_mask.getbbox():
        return _despeckle_result(
            image,
            changed_pixels=0,
            backend_mode="not_applicable",
            reason="no isolated dark pixels found",
            candidate_pixels=0,
            candidate_count=0,
            replacement_work_performed=False,
        )
    candidate_pixels = _despeckle_mask_pixel_count(candidate_mask)
    if _despeckle_mask_confined_to_protected_edge(candidate_mask):
        return _despeckle_result(
            image,
            changed_pixels=0,
            backend_mode="not_applicable",
            reason="protected edge dark marks preserved",
            candidate_pixels=candidate_pixels,
            candidate_count=0,
            replacement_work_performed=False,
        )
    candidates, backend_mode = _despeckle_candidate_points_with_backend(candidate_mask, backend=backend)
    if not candidates:
        reason = (
            "protected edge dark marks preserved"
            if _despeckle_mask_touches_protected_edge(candidate_mask)
            else "no isolated dark pixels found"
        )
        return _despeckle_result(
            image,
            changed_pixels=0,
            backend_mode=backend_mode,
            reason=reason,
            candidate_pixels=candidate_pixels,
            candidate_count=0,
            replacement_work_performed=False,
        )

    source_area = max(1, width * height)
    if len(candidates) / source_area > _DESPECKLE_MAX_CANDIDATE_RATIO:
        return _despeckle_result(
            image,
            changed_pixels=0,
            backend_mode=backend_mode,
            reason="despeckle skipped: candidate density exceeds safety threshold",
            candidate_pixels=candidate_pixels,
            candidate_count=len(candidates),
            replacement_work_performed=False,
        )

    replacements: list[tuple[int, int, tuple[int, int, int]]] | None = None
    replacement_work_performed = True
    if backend_mode == "numpy":
        replacements = _despeckle_replacements_numpy(image, grayscale, candidates)
        if replacements is None:
            backend_mode = "fallback"
    if replacements is None:
        replacements = _despeckle_replacements_fallback(image, grayscale, candidates)

    changed = len(replacements)
    if not changed:
        return _despeckle_result(
            image,
            changed_pixels=0,
            backend_mode=backend_mode,
            reason="no isolated dark pixels found",
            candidate_pixels=candidate_pixels,
            candidate_count=len(candidates),
            replacement_work_performed=replacement_work_performed,
        )
    if changed / source_area > _DESPECKLE_MAX_CHANGED_RATIO:
        return _despeckle_result(
            image,
            changed_pixels=0,
            backend_mode=backend_mode,
            reason="despeckle skipped: pixel change ratio exceeds safety threshold",
            candidate_pixels=candidate_pixels,
            candidate_count=len(candidates),
            replacement_work_performed=replacement_work_performed,
        )

    source = image if image.mode == "RGB" else image.convert("RGB")
    output = source.copy()
    output_pixels = output.load()
    for x, y, replacement in replacements:
        output_pixels[x, y] = replacement

    if image.mode == "L":
        result_image = output.convert("L")
    elif image.mode == "RGB":
        result_image = output
    else:
        result_image = output.convert(image.mode)
    return _despeckle_result(
        result_image,
        changed_pixels=changed,
        backend_mode=backend_mode,
        reason="isolated dark pixels replaced",
        candidate_pixels=candidate_pixels,
        candidate_count=len(candidates),
        replacement_work_performed=replacement_work_performed,
    )


def _despeckle_result(
    image: Image.Image,
    *,
    changed_pixels: int,
    backend_mode: str,
    reason: str,
    candidate_pixels: int,
    candidate_count: int,
    replacement_work_performed: bool,
) -> DespeckleResult:
    return DespeckleResult(
        image=image.copy() if changed_pixels == 0 else image,
        changed_pixels=changed_pixels,
        backend_mode=backend_mode,
        reason=reason,
        reason_code=_despeckle_reason_code(reason, changed_pixels),
        candidate_pixels=candidate_pixels,
        candidate_count=candidate_count,
        candidate_count_bucket=_despeckle_count_bucket(candidate_count),
        replacement_work_performed=replacement_work_performed,
    )


def _despeckle_reason_code(reason: str, changed_pixels: int) -> str:
    if changed_pixels:
        return "applied_isolated_pixels"
    if reason == "despeckle skipped: candidate density exceeds safety threshold":
        return "candidate_density_exceeds_safety_threshold"
    if reason == "despeckle skipped: pixel change ratio exceeds safety threshold":
        return "pixel_change_ratio_exceeds_safety_threshold"
    if reason == "protected edge dark marks preserved":
        return "protected_edge_dark_marks"
    if reason == "despeckle not applicable to very small image":
        return "not_applicable_small_image"
    return "no_isolated_candidates"


def _despeckle_count_bucket(count: int) -> str:
    if count <= 0:
        return "0"
    if count <= 4:
        return "1-4"
    if count <= 16:
        return "5-16"
    if count <= 64:
        return "17-64"
    if count <= 256:
        return "65-256"
    if count <= 1024:
        return "257-1024"
    return "1025+"


def _despeckle_mask_pixel_count(mask: Image.Image) -> int:
    bbox = mask.getbbox()
    if bbox is None:
        return 0
    return mask.crop(bbox).tobytes().count(255)


def _despeckle_replacements_numpy(
    image: Image.Image,
    grayscale: Image.Image,
    candidates: list[tuple[int, int]],
) -> list[tuple[int, int, tuple[int, int, int]]] | None:
    np = _load_numpy()
    if np is None:
        return None
    if not candidates:
        return []

    width, height = grayscale.size
    try:
        gray = np.asarray(grayscale, dtype=np.uint8)
        candidate_x = np.asarray([point[0] for point in candidates], dtype=np.int64)
        candidate_y = np.asarray([point[1] for point in candidates], dtype=np.int64)
    except (TypeError, ValueError):
        return None
    if gray.shape != (height, width):
        return None
    if (
        bool(np.any(candidate_x <= 0))
        or bool(np.any(candidate_y <= 0))
        or bool(np.any(candidate_x >= width - 1))
        or bool(np.any(candidate_y >= height - 1))
    ):
        return None

    candidate_mask = np.zeros((height, width), dtype=bool)
    candidate_mask[candidate_y, candidate_x] = True
    near_non_candidate = (gray <= _DESPECKLE_NEAR_DARK_THRESHOLD) & ~candidate_mask
    dark_neighbors = _despeckle_numpy_rect_counts(np, near_non_candidate, candidate_x, candidate_y, radius=1)
    wider_dark = _despeckle_numpy_rect_counts(np, near_non_candidate, candidate_x, candidate_y, radius=2)
    texture_counts = _despeckle_numpy_context_counts(np, candidate_mask, candidate_x, candidate_y)
    nearby_content = _despeckle_numpy_context_counts(np, gray <= _DESPECKLE_NEAR_DARK_THRESHOLD, candidate_x, candidate_y)

    neighbor_values = np.stack(
        [
            gray[candidate_y + offset_y, candidate_x + offset_x]
            for offset_y in (-1, 0, 1)
            for offset_x in (-1, 0, 1)
            if offset_x or offset_y
        ],
        axis=1,
    )
    median_gray = np.partition(neighbor_values, len(_DESPECKLE_NEIGHBOR_OFFSETS) // 2, axis=1)[
        :, len(_DESPECKLE_NEIGHBOR_OFFSETS) // 2
    ]
    source = image if image.mode == "RGB" else image.convert("RGB")
    try:
        rgb = np.asarray(source, dtype=np.uint8)
    except (TypeError, ValueError):
        return None
    if rgb.shape[:2] != (height, width) or rgb.shape[2:] != (3,):
        return None

    candidate_set = set(candidates)
    protected_context = np.asarray(
        [
            _despeckle_has_sparse_text_protected_context(
                lambda nx, ny: int(gray[ny, nx]),
                lambda nx, ny: (int(rgb[ny, nx, 0]), int(rgb[ny, nx, 1]), int(rgb[ny, nx, 2])),
                candidate_set=candidate_set,
                width=width,
                height=height,
                x=int(x_value),
                y=int(y_value),
            )
            for x_value, y_value in zip(candidate_x.tolist(), candidate_y.tolist())
        ],
        dtype=bool,
    )
    eligible = (
        (dark_neighbors <= 1)
        & (wider_dark <= 2)
        & ~((gray[candidate_y, candidate_x] > _DESPECKLE_DARK_THRESHOLD) & (texture_counts >= 3))
        & (nearby_content < _DESPECKLE_CONTENT_CONTEXT_MIN_DARK_PIXELS)
        & (median_gray >= _DESPECKLE_MIN_BACKGROUND_MEDIAN)
        & ~protected_context
    )
    if bool(np.any(~eligible & (nearby_content >= _DESPECKLE_CONTENT_CONTEXT_MIN_DARK_PIXELS))):
        extra_eligible = []
        for x_value, y_value, median_value, nearby_value in zip(
            candidate_x.tolist(),
            candidate_y.tolist(),
            median_gray.tolist(),
            nearby_content.tolist(),
        ):
            extra_eligible.append(
                _despeckle_sparse_text_clearance_allows_cleanup(
                    lambda nx, ny: int(gray[ny, nx]),
                    lambda nx, ny: (int(rgb[ny, nx, 0]), int(rgb[ny, nx, 1]), int(rgb[ny, nx, 2])),
                    candidate_set,
                    width,
                    height,
                    int(x_value),
                    int(y_value),
                    int(median_value),
                    int(nearby_value),
                )
            )
        eligible = eligible | (np.asarray(extra_eligible, dtype=bool) & ~protected_context)
    if not bool(np.any(eligible)):
        return []

    eligible_x = candidate_x[eligible]
    eligible_y = candidate_y[eligible]
    neighbor_rgb = np.stack(
        [
            rgb[eligible_y + offset_y, eligible_x + offset_x, :]
            for offset_y in (-1, 0, 1)
            for offset_x in (-1, 0, 1)
            if offset_x or offset_y
        ],
        axis=1,
    )
    replacement_rgb = np.partition(neighbor_rgb, len(_DESPECKLE_NEIGHBOR_OFFSETS) // 2, axis=1)[
        :, len(_DESPECKLE_NEIGHBOR_OFFSETS) // 2, :
    ]
    return [
        (int(x), int(y), (int(rgb_value[0]), int(rgb_value[1]), int(rgb_value[2])))
        for x, y, rgb_value in zip(eligible_x, eligible_y, replacement_rgb)
    ]


def _despeckle_numpy_rect_counts(
    np: Any,
    mask: Any,
    candidate_x: Any,
    candidate_y: Any,
    *,
    radius: int,
) -> Any:
    height, width = mask.shape
    integral = np.pad(mask.astype(np.int32).cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    left = np.maximum(0, candidate_x - radius)
    top = np.maximum(0, candidate_y - radius)
    right = np.minimum(width, candidate_x + radius + 1)
    bottom = np.minimum(height, candidate_y + radius + 1)
    return integral[bottom, right] - integral[top, right] - integral[bottom, left] + integral[top, left]


def _despeckle_numpy_context_counts(np: Any, mask: Any, candidate_x: Any, candidate_y: Any) -> Any:
    radius = _DESPECKLE_CONTENT_CONTEXT_RADIUS
    outer = _despeckle_numpy_rect_counts(np, mask, candidate_x, candidate_y, radius=radius)
    inner = _despeckle_numpy_rect_counts(np, mask, candidate_x, candidate_y, radius=2)
    return outer - inner


def _despeckle_replacements_fallback(
    image: Image.Image,
    grayscale: Image.Image,
    candidates: list[tuple[int, int]],
) -> list[tuple[int, int, tuple[int, int, int]]]:
    width, height = grayscale.size
    gray_pixels = grayscale.load()
    source: Image.Image | None = None
    source_pixels: Any = None
    replacements: list[tuple[int, int, tuple[int, int, int]]] = []
    candidate_set = set(candidates)
    for x, y in candidates:
        dark_neighbors = 0
        neighbor_values: list[int] = []
        for offset_x, offset_y in _DESPECKLE_NEIGHBOR_OFFSETS:
            nx = x + offset_x
            ny = y + offset_y
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
        if gray_pixels[x, y] > _DESPECKLE_DARK_THRESHOLD and _despeckle_has_candidate_texture_context(
            candidate_set,
            width,
            height,
            x,
            y,
        ):
            continue
        median_gray = sorted(neighbor_values)[len(neighbor_values) // 2]
        if median_gray < _DESPECKLE_MIN_BACKGROUND_MEDIAN:
            continue

        if source is None:
            source = image if image.mode == "RGB" else image.convert("RGB")
            source_pixels = source.load()
        if _despeckle_has_sparse_text_protected_context(
            gray_pixels,
            source_pixels,
            candidate_set=candidate_set,
            width=width,
            height=height,
            x=x,
            y=y,
        ):
            continue

        nearby_content_count = _despeckle_nearby_content_context_count(gray_pixels, width, height, x, y)
        if nearby_content_count >= _DESPECKLE_CONTENT_CONTEXT_MIN_DARK_PIXELS:
            if not _despeckle_sparse_text_clearance_allows_cleanup(
                gray_pixels,
                source_pixels,
                candidate_set,
                width,
                height,
                x,
                y,
                median_gray,
                nearby_content_count,
            ):
                continue

        if source is None:
            source = image if image.mode == "RGB" else image.convert("RGB")
            source_pixels = source.load()
        neighbor_rgb = [
            source_pixels[x + offset_x, y + offset_y]
            for offset_x, offset_y in _DESPECKLE_NEIGHBOR_OFFSETS
        ]
        replacement = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*neighbor_rgb))
        replacements.append((x, y, replacement))
    return replacements


def _despeckle_candidate_mask(image: Image.Image, grayscale: Image.Image) -> tuple[Image.Image, str | None]:
    width, height = grayscale.size
    histogram = grayscale.histogram()
    total = width * height
    p50 = _histogram_percentile(histogram, total, 0.50)
    p95 = _histogram_percentile(histogram, total, 0.95)
    light_soil_threshold = min(_DESPECKLE_LIGHT_SOIL_MAX_VALUE, p95 - _DESPECKLE_LIGHT_SOIL_MIN_DELTA)
    include_light_soil = p50 >= _DESPECKLE_LIGHT_SOIL_MIN_BACKGROUND and light_soil_threshold > _DESPECKLE_DARK_THRESHOLD
    dark_mask = grayscale.point(lambda value: 255 if value <= _DESPECKLE_DARK_THRESHOLD else 0, mode="L")
    if include_light_soil:
        light_soil_ratio = sum(histogram[_DESPECKLE_DARK_THRESHOLD + 1 : light_soil_threshold + 1]) / max(1, total)
        if light_soil_ratio > _DESPECKLE_MAX_LIGHT_SOIL_PREFILTER_RATIO:
            if dark_mask.getbbox():
                return dark_mask, None
            return Image.new("L", grayscale.size, 0), "despeckle skipped: candidate density exceeds safety threshold"
    if include_light_soil:
        mask = grayscale.point(
            lambda value: 255
            if value <= _DESPECKLE_DARK_THRESHOLD
            or (value <= light_soil_threshold and p95 - value >= _DESPECKLE_LIGHT_SOIL_MIN_DELTA)
            else 0,
            mode="L",
        )
    else:
        mask = dark_mask
    if image.mode == "L" or not mask.getbbox():
        return mask, None

    source_rgb = image.convert("RGB")
    rgb_pixels = source_rgb.load()
    mask_pixels = mask.load()
    bbox = mask.getbbox()
    if bbox is None:
        return mask, None
    left, top, right, bottom = bbox
    crop_width = right - left
    crop_values = mask.crop(bbox).tobytes()
    index = crop_values.find(255)
    while index != -1:
        x = left + (index % crop_width)
        y = top + (index // crop_width)
        if _despeckle_pixel_color_protected(rgb_pixels[x, y]):
            mask_pixels[x, y] = 0
        index = crop_values.find(255, index + 1)
    return mask, None


def _despeckle_pixel_color_protected(pixel: tuple[int, int, int]) -> bool:
    red_value, green_value, blue_value = pixel
    high = max(pixel)
    low = min(pixel)
    spread = high - low
    brightness = sum(pixel) / 3
    if red_value >= 105 and red_value - green_value >= 30 and red_value - blue_value >= 30:
        return True
    weak_warm_soil = (
        brightness >= 155
        and red_value >= green_value >= blue_value
        and red_value - green_value <= 24
        and green_value - blue_value <= 55
    )
    if weak_warm_soil:
        return False
    return spread > 28 and brightness > 70


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


def _despeckle_mask_confined_to_protected_edge(dark_mask: Image.Image) -> bool:
    bbox = dark_mask.getbbox()
    if not bbox:
        return False
    width, height = dark_mask.size
    margin = _despeckle_protected_edge_margin(width, height)
    left, top, right, bottom = bbox
    crop_width = right - left
    crop_values = dark_mask.crop(bbox).tobytes()
    index = crop_values.find(255)
    while index != -1:
        x = left + (index % crop_width)
        y = top + (index // crop_width)
        if margin <= x < width - margin and margin <= y < height - margin:
            return False
        index = crop_values.find(255, index + 1)
    return True


def _despeckle_has_nearby_content_context(gray_pixels: Any, width: int, height: int, x: int, y: int) -> bool:
    return (
        _despeckle_nearby_content_context_count(
            gray_pixels,
            width,
            height,
            x,
            y,
            stop_at=_DESPECKLE_CONTENT_CONTEXT_MIN_DARK_PIXELS,
        )
        >= _DESPECKLE_CONTENT_CONTEXT_MIN_DARK_PIXELS
    )


def _despeckle_nearby_content_context_count(
    gray_pixels: Any,
    width: int,
    height: int,
    x: int,
    y: int,
    *,
    stop_at: int | None = None,
) -> int:
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
                if stop_at is not None and dark_pixels >= stop_at:
                    return dark_pixels
    return dark_pixels


def _despeckle_sparse_text_clearance_allows_cleanup(
    gray_pixels: Any,
    rgb_pixels: Any | None,
    candidate_set: set[tuple[int, int]],
    width: int,
    height: int,
    x: int,
    y: int,
    median_gray: int,
    nearby_content_count: int,
) -> bool:
    if _despeckle_pixel_at(gray_pixels, x, y) > _DESPECKLE_NEAR_DARK_THRESHOLD:
        return False
    if median_gray < _DESPECKLE_SPARSE_TEXT_MIN_BACKGROUND_MEDIAN:
        return False
    if nearby_content_count > _DESPECKLE_SPARSE_TEXT_MAX_NEARBY_CONTENT_PIXELS:
        return False

    component = _despeckle_candidate_component(candidate_set, x, y)
    if len(component) > _DESPECKLE_MAX_COMPONENT_PIXELS:
        return False
    component_x = [point[0] for point in component]
    component_y = [point[1] for point in component]
    if max(component_x) - min(component_x) + 1 > 2:
        return False
    if max(component_y) - min(component_y) + 1 > 2:
        return False

    clearance = _DESPECKLE_SPARSE_TEXT_CLEARANCE_RADIUS
    radius = _DESPECKLE_CONTENT_CONTEXT_RADIUS
    for ny in range(max(0, y - radius), min(height, y + radius + 1)):
        for nx in range(max(0, x - radius), min(width, x + radius + 1)):
            if (nx, ny) in candidate_set:
                continue
            if _despeckle_pixel_at(gray_pixels, nx, ny) > _DESPECKLE_NEAR_DARK_THRESHOLD:
                continue
            if abs(nx - x) <= clearance and abs(ny - y) <= clearance:
                return False
    return True


def _despeckle_has_sparse_text_protected_context(
    gray_pixels: Any,
    rgb_pixels: Any | None,
    *,
    candidate_set: set[tuple[int, int]],
    width: int,
    height: int,
    x: int,
    y: int,
) -> bool:
    component = _despeckle_candidate_component(candidate_set, x, y)
    component_x = [point[0] for point in component]
    component_y = [point[1] for point in component]
    radius = _DESPECKLE_CONTENT_CONTEXT_RADIUS
    for ny in range(max(0, min(component_y) - radius), min(height, max(component_y) + radius + 1)):
        for nx in range(max(0, min(component_x) - radius), min(width, max(component_x) + radius + 1)):
            if (nx, ny) in candidate_set:
                continue
            if _despeckle_pixel_at(gray_pixels, nx, ny) > _DESPECKLE_NEAR_DARK_THRESHOLD:
                continue
            if rgb_pixels is not None and _despeckle_pixel_color_protected(_despeckle_pixel_at(rgb_pixels, nx, ny)):
                return True
            if any(abs(nx - cx) <= 1 and abs(ny - cy) > 2 for cx, cy in component):
                return True
    return False


def _despeckle_pixel_at(pixels: Any, x: int, y: int) -> Any:
    if callable(pixels):
        return pixels(x, y)
    return pixels[x, y]


def _despeckle_candidate_component(
    candidate_set: set[tuple[int, int]],
    x: int,
    y: int,
) -> list[tuple[int, int]]:
    stack = [(x, y)]
    visited: set[tuple[int, int]] = set()
    component: list[tuple[int, int]] = []
    while stack:
        point = stack.pop()
        if point in visited or point not in candidate_set:
            continue
        visited.add(point)
        component.append(point)
        point_x, point_y = point
        for offset_x, offset_y in _DESPECKLE_NEIGHBOR_OFFSETS:
            neighbor = (point_x + offset_x, point_y + offset_y)
            if neighbor not in visited and neighbor in candidate_set:
                stack.append(neighbor)
    return component


def _despeckle_has_candidate_texture_context(
    candidate_set: set[tuple[int, int]],
    width: int,
    height: int,
    x: int,
    y: int,
) -> bool:
    nearby_candidates = 0
    radius = _DESPECKLE_CONTENT_CONTEXT_RADIUS
    for ny in range(max(0, y - radius), min(height, y + radius + 1)):
        for nx in range(max(0, x - radius), min(width, x + radius + 1)):
            if nx == x and ny == y:
                continue
            if abs(nx - x) <= 2 and abs(ny - y) <= 2:
                continue
            if (nx, ny) in candidate_set:
                nearby_candidates += 1
                if nearby_candidates >= 3:
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
