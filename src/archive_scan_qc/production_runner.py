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
    resume_processing: bool = False
    workers: int | None = None
    analysis_provider_command: str | None = None


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
    status = _production_status(p0_findings, failed_files)
    operator_message = _operator_message(status, p0_findings, failed_files)
    derivative_image_dir = Path(processing_manifest["image_root"])
    options = {
        "auto_crop": config.auto_crop,
        "deskew": config.deskew,
        "trim_dark_border": config.trim_dark_border,
        "despeckle": config.despeckle,
        "despeckle_backend": config.despeckle_backend,
        "resume_processing": config.resume_processing,
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
        "ready_for_operator_handoff": status == "finished",
        "operator_summary": {
            "message": operator_message,
            "message_zh": operator_message,
            "input_folder": str(config.input_dir.resolve()),
            "derivative_image_folder": str(derivative_image_dir),
            "metadata_folder": str(config.metadata_output_dir.resolve()),
            "total_source_images": scan_summary["total_files"],
            "openable_source_images": scan_summary["openable_files"],
            "derivative_images_ready": processing_summary["processed_files"] + processing_summary["resumed_files"],
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
        },
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


def _operator_message(status: str, p0_findings: int, failed_files: int) -> str:
    if status == "finished":
        return "处理后图片已生成，可以完成导出。"
    blockers = []
    if p0_findings:
        blockers.append(f"{p0_findings} 个质量问题需要人工确认")
    if failed_files:
        blockers.append(f"{failed_files} 个文件处理失败")
    return "需要处理：" + "，".join(blockers) + "。"


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
