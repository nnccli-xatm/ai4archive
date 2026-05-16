"""Operator-facing local production runner for folder-to-derivatives work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

from .processing import ProcessingOptions, process_images
from .reports import write_reports
from .rules import RulesProfile
from .scanner import ScanConfig, scan_batch


PRODUCTION_RUN_SUMMARY_JSON = "production_run_summary.json"
PRODUCTION_RUN_PROGRESS_JSON = "production_run_progress.json"
SCHEMA_VERSION = "scan-qc.production-run.v1"
STEP_LABELS = {
    "scan": "检查扫描图片",
    "process": "生成处理后图片",
    "summarize": "整理处理结果",
}
STAGE_TIMING_SCHEMA_VERSION = "scan-qc.production-stage-timings.v1"
AGGREGATE_PROCESSING_SCHEMA_VERSION = "scan-qc.aggregate-processing-rate.v1"
PROCESSING_MODE_LABELS_ZH = {
    "standard": "标准优化",
    "qc_only": "只质检不修图",
    "light": "轻度优化",
}
PROCESSING_MODE_PURPOSES_ZH = {
    "standard": "推荐用于正常批量生产，兼顾批量图片质量和处理效率。",
    "qc_only": "只做质量检查，适合本批不需要自动修图的情况。",
    "light": "用于担心过度处理的批次，只做较轻的保守优化。",
}
PROCESSING_MODE_OUTPUTS_ZH = {
    "standard": "会生成处理后优化图片，原图不覆盖。",
    "qc_only": "不会生成处理后优化图片。",
    "light": "会生成轻度处理后的优化图片，原图不覆盖。",
}


@dataclass(frozen=True)
class ProductionRunConfig:
    input_dir: Path
    derivative_output_dir: Path
    metadata_output_dir: Path
    project_id: str = "default-project"
    batch_id: str = "default-batch"
    min_dpi: int = 200
    name_pattern: str | None = None
    manifest_csv: Path | None = None
    rules_profile: RulesProfile | None = None
    auto_crop: bool = False
    deskew: bool = False
    trim_dark_border: bool = False
    scanner_gutter_trim: bool = False
    despeckle: bool = False
    normalize_tones: bool = False
    normalize_paper_color_cast: bool = False
    lighten_edge_shadow: bool = False
    lighten_corner_shadows: bool = False
    lighten_background_stains: bool = False
    lighten_fold_shadows: bool = False
    clean_bleed_through: bool = False
    lighten_scanlines: bool = False
    enhance_faded_text: bool = False
    sharpen_text_edges: bool = False
    despeckle_backend: str = "fallback"
    resume_processing: bool = True
    reuse_scan_measurements: bool = False
    workers: int | None = None
    analysis_provider_command: str | None = None
    processing_mode: str = "standard"


def run_production_folder(config: ProductionRunConfig) -> dict[str, Any]:
    """Run scan QC and derivative processing for a local image folder."""
    metadata_dir = config.metadata_output_dir.resolve()
    derivative_dir = config.derivative_output_dir.resolve()
    admin_report_dir = metadata_dir / "admin_reports"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    derivative_dir.mkdir(parents=True, exist_ok=True)

    steps = [
        _step("scan", STEP_LABELS["scan"], "pending"),
        _step("process", STEP_LABELS["process"], "pending"),
        _step("summarize", STEP_LABELS["summarize"], "pending"),
    ]
    stage_timings: dict[str, float] = {}
    _write_progress(metadata_dir, "running", steps, current_step="scan")
    steps[0] = _step("scan", STEP_LABELS["scan"], "running")

    scan_started_at = time.perf_counter()
    report = scan_batch(
        ScanConfig(
            project_id=config.project_id,
            batch_id=config.batch_id,
            input_dir=config.input_dir,
            output_dir=admin_report_dir,
            min_dpi=config.min_dpi,
            name_pattern=config.name_pattern,
            manifest_csv=config.manifest_csv,
            rules_profile=config.rules_profile,
            workers=config.workers,
            analysis_provider_command=config.analysis_provider_command,
        )
    )
    stage_timings["scan"] = time.perf_counter() - scan_started_at
    report_paths = write_reports(report, admin_report_dir)
    steps[0] = _step("scan", STEP_LABELS["scan"], "completed", completed_items=report["summary"]["total_files"])
    steps[1] = _step("process", STEP_LABELS["process"], "running", total_items=report["summary"]["total_files"])
    _write_progress(metadata_dir, "running", steps, current_step="process", stage_timings=stage_timings)

    process_started_at = time.perf_counter()
    processing_manifest = process_images(
        report,
        config.input_dir,
        derivative_dir,
        ProcessingOptions(
            auto_crop=config.auto_crop,
            deskew=config.deskew,
            trim_dark_border=config.trim_dark_border,
            scanner_gutter_trim=config.scanner_gutter_trim,
            despeckle=config.despeckle,
            normalize_tones=config.normalize_tones,
            normalize_paper_color_cast=config.normalize_paper_color_cast,
            lighten_edge_shadow=config.lighten_edge_shadow,
            lighten_corner_shadows=config.lighten_corner_shadows,
            lighten_background_stains=config.lighten_background_stains,
            lighten_fold_shadows=config.lighten_fold_shadows,
            clean_bleed_through=config.clean_bleed_through,
            lighten_scanlines=config.lighten_scanlines,
            enhance_faded_text=config.enhance_faded_text,
            sharpen_text_edges=config.sharpen_text_edges,
            despeckle_backend=config.despeckle_backend,
            resume_processing=config.resume_processing,
            reuse_scan_measurements=config.reuse_scan_measurements,
            workers=config.workers,
        ),
    )
    stage_timings["process"] = time.perf_counter() - process_started_at
    processed_done = (
        processing_manifest["summary"]["processed_files"]
        + processing_manifest["summary"]["resumed_files"]
        + processing_manifest["summary"]["skipped_files"]
        + processing_manifest["summary"]["failed_files"]
    )
    steps[1] = _step(
        "process",
        STEP_LABELS["process"],
        "completed",
        total_items=processing_manifest["summary"]["total_files"],
        completed_items=processed_done,
    )
    steps[2] = _step("summarize", STEP_LABELS["summarize"], "running")
    _write_progress(metadata_dir, "running", steps, current_step="summarize", stage_timings=stage_timings)

    summarize_started_at = time.perf_counter()
    summary = build_production_run_summary(
        config=config,
        report=report,
        processing_manifest=processing_manifest,
        admin_report_dir=admin_report_dir,
        report_paths=report_paths,
        stage_timings=stage_timings,
    )
    stage_timings["summarize"] = time.perf_counter() - summarize_started_at
    summary["stage_timings"] = _stage_timings_payload(stage_timings)
    summary_path = write_production_run_summary(summary, metadata_dir)
    steps[2] = _step("summarize", STEP_LABELS["summarize"], "completed")
    _write_progress(
        metadata_dir,
        summary["status"],
        steps,
        current_step=None,
        summary_path=summary_path,
        stage_timings=stage_timings,
    )
    return summary


def build_production_run_summary(
    *,
    config: ProductionRunConfig,
    report: dict[str, Any],
    processing_manifest: dict[str, Any],
    admin_report_dir: Path,
    report_paths: dict[str, Path] | None = None,
    generated_at: str | None = None,
    stage_timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    scan_summary = report["summary"]
    processing_summary = processing_manifest["summary"]
    failed_files = int(processing_summary["failed_files"])
    p0_findings = int(scan_summary["p0_findings"])
    total_files = int(scan_summary["total_files"])
    openable_files = int(scan_summary["openable_files"])
    derivative_images_ready = int(processing_summary["processed_files"]) + int(processing_summary["resumed_files"])
    local_batch_state = _local_batch_state(total_files, openable_files, derivative_images_ready, p0_findings, failed_files)
    status = _production_status(p0_findings, failed_files)
    operator_message = _operator_message(local_batch_state, p0_findings, failed_files)
    derivative_image_dir = Path(processing_manifest["image_root"])
    options = {
        "processing_mode": config.processing_mode,
        "processing_mode_label_zh": PROCESSING_MODE_LABELS_ZH.get(config.processing_mode, config.processing_mode),
        "processing_mode_purpose_zh": PROCESSING_MODE_PURPOSES_ZH.get(config.processing_mode, ""),
        "processing_mode_output_zh": PROCESSING_MODE_OUTPUTS_ZH.get(config.processing_mode, ""),
        "auto_crop": config.auto_crop,
        "deskew": config.deskew,
        "trim_dark_border": config.trim_dark_border,
        "scanner_gutter_trim": config.scanner_gutter_trim,
        "despeckle": config.despeckle,
        "normalize_tones": config.normalize_tones,
        "normalize_paper_color_cast": config.normalize_paper_color_cast,
        "lighten_edge_shadow": config.lighten_edge_shadow,
        "lighten_corner_shadows": config.lighten_corner_shadows,
        "lighten_background_stains": config.lighten_background_stains,
        "lighten_fold_shadows": config.lighten_fold_shadows,
        "clean_bleed_through": config.clean_bleed_through,
        "lighten_scanlines": config.lighten_scanlines,
        "enhance_faded_text": config.enhance_faded_text,
        "sharpen_text_edges": config.sharpen_text_edges,
        "despeckle_backend": config.despeckle_backend,
        "resume_processing": config.resume_processing,
    }
    reused_files = int(processing_summary.get("existing_derivative_reused_files", 0))
    reprocessed_files = int(processing_summary.get("reprocessed_files", 0))
    remaining_files = int(processing_summary.get("retry_list_files", failed_files))
    local_reuse_summary = {
        "schema_version": "scan-qc.local-processing-reuse-summary.v1",
        "aggregate_only": True,
        "total_files": int(processing_summary.get("total_files", total_files)),
        "reused_files": reused_files,
        "reprocessed_files": reprocessed_files,
        "failed_files": failed_files,
        "remaining_files": remaining_files,
        "message_zh": (
            f"本批共 {int(processing_summary.get('total_files', total_files))} 张；"
            f"复用 {reused_files} 张，重新处理 {reprocessed_files} 张，"
            f"仍失败 {failed_files} 张，剩余待处理 {remaining_files} 张。"
        ),
    }
    completed_processing_items = (
        int(processing_summary.get("processed_files", 0))
        + int(processing_summary.get("resumed_files", 0))
        + int(processing_summary.get("skipped_files", 0))
        + failed_files
    )
    aggregate_processing = _aggregate_processing_payload(
        total_items=processing_summary.get("total_files"),
        completed_items=completed_processing_items,
        elapsed_seconds=(stage_timings or {}).get("process"),
    )
    artifacts = {
        "summary": str(config.metadata_output_dir.resolve() / PRODUCTION_RUN_SUMMARY_JSON),
        "progress": str(config.metadata_output_dir.resolve() / PRODUCTION_RUN_PROGRESS_JSON),
        "derivative_images": str(derivative_image_dir),
        "processing_manifest": str(config.derivative_output_dir.resolve() / "processing_manifest.json"),
        "processing_retry_manifest": str(config.derivative_output_dir.resolve() / "processing_retry_manifest.json"),
        "processing_audit_summary": str(config.derivative_output_dir.resolve() / "processing_audit_summary.json"),
        "admin_reports": str(admin_report_dir.resolve()),
    }
    if report_paths:
        artifacts["admin_scan_report"] = str(report_paths["json"])
        artifacts["admin_scan_report_html"] = str(report_paths["html"])

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "status_label_zh": _status_label_zh(status),
        "ready_for_operator_handoff": local_batch_state == "no_review_items",
        "local_batch_state": local_batch_state,
        "recovery_guidance": _recovery_guidance(local_batch_state, processing_summary),
        "operator_summary": {
            "message": operator_message,
            "message_zh": operator_message,
            "processing_mode": config.processing_mode,
            "processing_mode_label_zh": PROCESSING_MODE_LABELS_ZH.get(config.processing_mode, config.processing_mode),
            "processing_mode_purpose_zh": PROCESSING_MODE_PURPOSES_ZH.get(config.processing_mode, ""),
            "processing_mode_output_zh": PROCESSING_MODE_OUTPUTS_ZH.get(config.processing_mode, ""),
            "input_folder": str(config.input_dir.resolve()),
            "derivative_image_folder": str(derivative_image_dir),
            "metadata_folder": str(config.metadata_output_dir.resolve()),
            "total_source_images": scan_summary["total_files"],
            "openable_source_images": scan_summary["openable_files"],
            "derivative_images_ready": derivative_images_ready,
            "files_needing_attention": failed_files + p0_findings,
        },
        "counts": {
            "total_files": scan_summary["total_files"],
            "openable_files": scan_summary["openable_files"],
            "p0_findings": p0_findings,
            "p1_findings": scan_summary["p1_findings"],
            "p2_findings": scan_summary["p2_findings"],
            "total_findings": scan_summary["total_findings"],
            "processed_files": processing_summary["processed_files"],
            "resumed_files": processing_summary["resumed_files"],
            "skipped_files": processing_summary["skipped_files"],
            "failed_files": failed_files,
            "retry_list_files": processing_summary["retry_list_files"],
            "reused_files": local_reuse_summary["reused_files"],
            "reprocessed_files": local_reuse_summary["reprocessed_files"],
            "remaining_files": local_reuse_summary["remaining_files"],
            "retry_total_files": local_reuse_summary["total_files"],
        },
        "local_reuse_summary": local_reuse_summary,
        "aggregate_processing": aggregate_processing,
        "progress": {
            "state": "completed",
            "total_steps": 3,
            "completed_steps": 3,
            "total_items": processing_summary["total_files"],
            "completed_items": processing_summary["total_files"],
            "aggregate_processing": aggregate_processing,
        },
        "performance": {
            "scan": scan_summary["performance"],
            "processing": processing_summary["performance"],
        },
        "stage_timings": _stage_timings_payload(stage_timings),
        "options": options,
        "artifacts": artifacts,
        "admin_artifacts_available": True,
        "source_images_modified": False,
        "network_services_called": False,
        "model_inference_run": bool(config.analysis_provider_command),
        "scan_processing_semantics": "unchanged_cpu_pillow_baseline",
    }


def write_production_run_summary(summary: dict[str, Any], metadata_output_dir: Path) -> Path:
    metadata_output_dir.mkdir(parents=True, exist_ok=True)
    path = metadata_output_dir / PRODUCTION_RUN_SUMMARY_JSON
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_progress(
    metadata_dir: Path,
    state: str,
    steps: list[dict[str, Any]],
    *,
    current_step: str | None,
    summary_path: Path | None = None,
    stage_timings: dict[str, float] | None = None,
) -> Path:
    completed_steps = sum(1 for step in steps if step["state"] == "completed")
    payload = {
        "schema_version": "scan-qc.production-run-progress.v1",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "state_label_zh": _state_label_zh(state),
        "message_zh": _progress_message_zh(state, current_step),
        "current_step": current_step,
        "completed_steps": completed_steps,
        "total_steps": len(steps),
        "steps": steps,
        "stage_timings": _stage_timings_payload(stage_timings, steps=steps),
        "aggregate_processing": _aggregate_processing_from_steps(steps, stage_timings),
    }
    if summary_path is not None:
        payload["summary"] = str(summary_path)
    path = metadata_dir / PRODUCTION_RUN_PROGRESS_JSON
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _stage_timings_payload(
    elapsed_by_stage: dict[str, float] | None,
    *,
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    elapsed_by_stage = elapsed_by_stage or {}
    status_by_stage = {step["id"]: step["state"] for step in steps or []}
    stages = []
    for stage_id in STEP_LABELS:
        elapsed_seconds = elapsed_by_stage.get(stage_id, 0.0)
        status = status_by_stage.get(stage_id, "completed" if stage_id in elapsed_by_stage else "pending")
        stages.append(
            {
                "id": stage_id,
                "label_zh": STEP_LABELS[stage_id],
                "elapsed_seconds": max(0.0, round(float(elapsed_seconds), 6)),
                "status": status,
            }
        )
    return {
        "schema_version": STAGE_TIMING_SCHEMA_VERSION,
        "aggregate_only": True,
        "stages": stages,
    }


def _aggregate_processing_from_steps(
    steps: list[dict[str, Any]],
    stage_timings: dict[str, float] | None,
) -> dict[str, Any]:
    process_step = next((step for step in steps if step.get("id") == "process"), {})
    return _aggregate_processing_payload(
        total_items=process_step.get("total_items"),
        completed_items=process_step.get("completed_items"),
        elapsed_seconds=(stage_timings or {}).get("process"),
    )


def _aggregate_processing_payload(
    *,
    total_items: Any,
    completed_items: Any,
    elapsed_seconds: Any,
) -> dict[str, Any]:
    total_images = _safe_nonnegative_int(total_items)
    processed_images = _safe_nonnegative_int(completed_items)
    elapsed = _safe_nonnegative_float(elapsed_seconds)
    remaining_images: int | None = None
    images_per_minute: float | None = None
    estimated_remaining_seconds: float | None = None
    unavailable_reason: str | None = None

    if total_images is None:
        unavailable_reason = "missing_total_images"
    elif processed_images is None:
        unavailable_reason = "missing_processed_images"
    else:
        processed_images = min(processed_images, total_images)
        remaining_images = max(total_images - processed_images, 0)
        if total_images == 0:
            unavailable_reason = "no_total_images"
        elif processed_images == 0:
            unavailable_reason = "no_processed_images"
        elif elapsed is None or elapsed <= 0:
            unavailable_reason = "no_elapsed_seconds"
            if remaining_images == 0:
                estimated_remaining_seconds = 0.0
        else:
            images_per_minute = round(processed_images / (elapsed / 60.0), 6)
            estimated_remaining_seconds = (
                0.0 if remaining_images == 0 else round(remaining_images / (images_per_minute / 60.0), 6)
            )

    return {
        "schema_version": AGGREGATE_PROCESSING_SCHEMA_VERSION,
        "aggregate_only": True,
        "total_images": total_images,
        "processed_images": processed_images,
        "remaining_images": remaining_images,
        "elapsed_seconds": None if elapsed is None else round(elapsed, 6),
        "images_per_minute": images_per_minute,
        "estimated_remaining_seconds": estimated_remaining_seconds,
        "unavailable_reason": unavailable_reason,
    }


def _safe_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _safe_nonnegative_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _step(
    step_id: str,
    label: str,
    state: str,
    *,
    total_items: int | None = None,
    completed_items: int | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "state": state,
        "total_items": total_items,
        "completed_items": completed_items,
    }


def _local_batch_state(
    total_files: int,
    openable_files: int,
    derivative_images_ready: int,
    p0_findings: int,
    failed_files: int,
) -> str:
    if total_files == 0:
        return "empty_input_folder"
    if failed_files:
        return "processing_blocked"
    if openable_files == 0:
        return "no_supported_images"
    if p0_findings:
        return "review_required"
    if derivative_images_ready == 0:
        return "no_derivative_images"
    return "no_review_items"


def _operator_message(local_batch_state: str, p0_findings: int, failed_files: int) -> str:
    if local_batch_state == "empty_input_folder":
        return "扫描原图文件夹里没有可处理文件，请确认是否选错文件夹。"
    if local_batch_state == "no_supported_images":
        return "没有找到可处理的图片，请确认原图是常见图片格式且可以打开。"
    if local_batch_state == "no_review_items":
        return "处理后图片已生成，可以完成并导出结果。"
    blockers = []
    if p0_findings:
        blockers.append(f"{p0_findings} 个质量问题需要人工确认")
    if failed_files:
        blockers.append(f"{failed_files} 个文件处理失败")
    return "需要处理：" + "，".join(blockers) + "。"


def _recovery_guidance(local_batch_state: str, processing_summary: dict[str, Any]) -> dict[str, Any]:
    failed_files = int(processing_summary.get("failed_files", 0))
    retry_list_files = int(processing_summary.get("retry_list_files", 0))
    derivative_images_ready = int(processing_summary.get("processed_files", 0)) + int(processing_summary.get("resumed_files", 0))
    total_files = int(processing_summary.get("total_files", 0))
    missing_output_files = retry_list_files if retry_list_files else failed_files
    can_restart_fill_missing_outputs = retry_list_files > 0
    guidance = {
        "schema_version": "scan-qc.local-recovery-guidance.v1",
        "aggregate_only": True,
        "failed_files": failed_files,
        "retryable_files": retry_list_files,
        "derivative_images_ready": derivative_images_ready,
        "successful_output_files": derivative_images_ready,
        "missing_output_files": missing_output_files,
        "can_restart_fill_missing_outputs": can_restart_fill_missing_outputs,
        "total_files": total_files,
    }
    if local_batch_state == "empty_input_folder":
        guidance.update(
            {
                "kind": "empty_input_folder",
                "title_zh": "原图文件夹是空的",
                "message_zh": "这个扫描原图文件夹里没有发现可处理文件。",
                "next_steps_zh": [
                    "确认是否选到了本批次真正的扫描原图文件夹。",
                    "如果还没有扫描图片，请先完成扫描或把图片放入原图文件夹。",
                    "放好图片后，重新保存文件夹并开始处理。",
                ],
            }
        )
    elif local_batch_state == "no_supported_images":
        guidance.update(
            {
                "kind": "no_supported_images",
                "title_zh": "没有可处理的图片",
                "message_zh": "文件夹里没有找到当前支持处理的图片，或图片无法正常打开。",
                "next_steps_zh": [
                    "确认选对了扫描原图文件夹。",
                    "确认原图是常见图片格式，并且能用本机图片查看器打开。",
                    "如果文件格式不对，请重新导出为支持的图片格式后再处理。",
                ],
            }
        )
    elif local_batch_state == "processing_blocked":
        restart_message = (
            "可以重新开始本批，系统会只补齐缺失的处理后图片，已经成功输出的图片不会删除。"
            if can_restart_fill_missing_outputs
            else "当前没有可自动补齐的缺失输出，请先检查权限、格式或交管理员确认。"
        )
        guidance.update(
            {
                "kind": "processing_failed_retryable" if retry_list_files else "processing_failed_admin",
                "title_zh": "处理没有全部完成",
                "message_zh": (
                    f"本批有 {failed_files} 张处理失败，已成功输出 {derivative_images_ready} 张；"
                    f"{restart_message}"
                ),
                "next_steps_zh": [
                    "检查扫描原图文件夹是否可读取，原图是否能正常打开。",
                    "检查处理后输出文件夹是否可写入，磁盘空间是否足够。",
                    "确认原图是当前支持的常见图片格式。",
                    (
                        "重新开始本批，系统会只补齐缺失输出并保留已成功输出。"
                        if can_restart_fill_missing_outputs
                        else "如果检查后仍不能处理，请交管理员查看本机私有状态报告。"
                    ),
                ],
            }
        )
    elif local_batch_state == "no_review_items":
        guidance.update(
            {
                "kind": "no_remaining_work",
                "title_zh": "没有剩余处理任务",
                "message_zh": "本批次没有需要人工确认的图片，处理后图片已经准备好。",
                "next_steps_zh": [
                    "确认处理后图片数量正常。",
                    "把处理后图片交给验收或移交流程。",
                    "点击准备下一批，重新选择新的扫描原图文件夹和输出文件夹。",
                ],
            }
        )
    elif local_batch_state == "no_derivative_images":
        guidance.update(
            {
                "kind": "no_derivative_images",
                "title_zh": "没有生成处理后图片",
                "message_zh": "本批次没有生成处理后图片，请检查原图是否能正常打开。",
                "next_steps_zh": [
                    "确认扫描原图文件夹和处理后输出文件夹选对。",
                    "确认原图能用本机图片查看器打开。",
                    "重新开始处理；如果仍没有结果，请交管理员查看本机状态文件夹。",
                ],
            }
        )
    else:
        guidance.update(
            {
                "kind": "review_required",
                "title_zh": "需要人工确认",
                "message_zh": "自动处理已完成，仍有图片需要人工确认。",
                "next_steps_zh": ["查看大图后选择确认通过、返工或交管理员处理。"],
            }
        )
    return guidance


def _production_status(p0_findings: int, failed_files: int) -> str:
    if failed_files:
        return "blocked"
    if p0_findings:
        return "needs_review"
    return "finished"


def _status_label_zh(status: str) -> str:
    return {
        "pending": "等待开始",
        "running": "处理中",
        "finished": "已完成",
        "needs_review": "需要人工复核",
        "blocked": "已阻断",
    }.get(status, status)


def _state_label_zh(state: str) -> str:
    return {
        "pending": "等待开始",
        "running": "处理中",
        "completed": "已完成",
        "finished": "已完成",
        "needs_review": "需要人工复核",
        "blocked": "已阻断",
    }.get(state, state)


def _progress_message_zh(state: str, current_step: str | None) -> str:
    if state == "running" and current_step:
        return f"{STEP_LABELS.get(current_step, current_step)}中，请稍候。"
    if state == "finished":
        return "处理后图片已生成。"
    if state == "needs_review":
        return "已完成自动处理，仍有图片需要人工确认。"
    if state == "blocked":
        return "处理被阻断，请管理员查看失败文件。"
    return _state_label_zh(state)
