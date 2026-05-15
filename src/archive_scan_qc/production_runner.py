"""Operator-facing local production runner for folder-to-derivatives work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
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
    despeckle: bool = False
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
    _write_progress(metadata_dir, "running", steps, current_step="scan")
    steps[0] = _step("scan", STEP_LABELS["scan"], "running")

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
    report_paths = write_reports(report, admin_report_dir)
    steps[0] = _step("scan", STEP_LABELS["scan"], "completed", completed_items=report["summary"]["total_files"])
    steps[1] = _step("process", STEP_LABELS["process"], "running", total_items=report["summary"]["total_files"])
    _write_progress(metadata_dir, "running", steps, current_step="process")

    processing_manifest = process_images(
        report,
        config.input_dir,
        derivative_dir,
        ProcessingOptions(
            auto_crop=config.auto_crop,
            deskew=config.deskew,
            trim_dark_border=config.trim_dark_border,
            despeckle=config.despeckle,
            despeckle_backend=config.despeckle_backend,
            resume_processing=config.resume_processing,
            reuse_scan_measurements=config.reuse_scan_measurements,
            workers=config.workers,
        ),
    )
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
    _write_progress(metadata_dir, "running", steps, current_step="summarize")

    summary = build_production_run_summary(
        config=config,
        report=report,
        processing_manifest=processing_manifest,
        admin_report_dir=admin_report_dir,
        report_paths=report_paths,
    )
    summary_path = write_production_run_summary(summary, metadata_dir)
    steps[2] = _step("summarize", STEP_LABELS["summarize"], "completed")
    _write_progress(metadata_dir, summary["status"], steps, current_step=None, summary_path=summary_path)
    return summary


def build_production_run_summary(
    *,
    config: ProductionRunConfig,
    report: dict[str, Any],
    processing_manifest: dict[str, Any],
    admin_report_dir: Path,
    report_paths: dict[str, Path] | None = None,
    generated_at: str | None = None,
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
        "despeckle": config.despeckle,
        "despeckle_backend": config.despeckle_backend,
        "resume_processing": config.resume_processing,
    }
    local_reuse_summary = {
        "schema_version": "scan-qc.local-processing-reuse-summary.v1",
        "aggregate_only": True,
        "reused_files": int(processing_summary.get("existing_derivative_reused_files", 0)),
        "reprocessed_files": int(processing_summary.get("reprocessed_files", 0)),
        "failed_files": failed_files,
        "remaining_files": int(processing_summary.get("retry_list_files", failed_files)),
    }
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
        },
        "local_reuse_summary": local_reuse_summary,
        "progress": {
            "state": "completed",
            "total_steps": 3,
            "completed_steps": 3,
            "total_items": processing_summary["total_files"],
            "completed_items": processing_summary["total_files"],
        },
        "performance": {
            "scan": scan_summary["performance"],
            "processing": processing_summary["performance"],
        },
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
    }
    if summary_path is not None:
        payload["summary"] = str(summary_path)
    path = metadata_dir / PRODUCTION_RUN_PROGRESS_JSON
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


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
    guidance = {
        "schema_version": "scan-qc.local-recovery-guidance.v1",
        "aggregate_only": True,
        "failed_files": failed_files,
        "retryable_files": retry_list_files,
        "derivative_images_ready": derivative_images_ready,
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
        guidance.update(
            {
                "kind": "processing_failed_retryable" if retry_list_files else "processing_failed_admin",
                "title_zh": "处理没有全部完成",
                "message_zh": "有文件处理失败。请先检查原图是否能打开、文件夹是否选对、磁盘空间是否足够。",
                "next_steps_zh": [
                    "确认扫描原图文件夹和处理后输出文件夹选对。",
                    "检查磁盘空间是否足够，原图是否能正常打开。",
                    "如果只是少量文件失败，可重新开始处理；系统会尽量复用已经生成的处理后图片。",
                    "如果再次失败，请交管理员查看本机状态文件夹中的报告。",
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
