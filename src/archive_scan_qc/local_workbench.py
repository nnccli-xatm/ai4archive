"""Loopback-only operator workbench server for local production runs."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from typing import Any
from urllib.parse import unquote, urlparse
import uuid
import webbrowser

from .processing import ProcessingOptions, aggregate_processing_reuse_precheck
from .processing_review import REVIEW_JSON as PROCESSING_REVIEW_JSON, write_processing_review_package
from .production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON, write_production_review_queue
from .production_runner import (
    PROCESSING_MODE_LABELS_ZH,
    PROCESSING_MODE_OUTPUTS_ZH,
    PROCESSING_MODE_PURPOSES_ZH,
    PRODUCTION_RUN_PROGRESS_JSON,
    PRODUCTION_RUN_SUMMARY_JSON,
    ProductionRunConfig,
    run_production_folder,
)
from .review_decisions import (
    REVIEW_DECISION_VERIFICATION_JSON,
    build_review_decision_verification_summary,
)


ROOT = Path(__file__).resolve().parents[2]
WORKBENCH_HTML = ROOT / "docs" / "production-workbench-prototype.html"
DOCS_DIR = ROOT / "docs"
DEFAULT_METADATA_DIRNAME = "_production_workbench"
SERVER_SCHEMA = "scan-qc.local-production-workbench.v1"
PREFLIGHT_SNAPSHOT_SCHEMA = "scan-qc.local-preflight-snapshot.v1"
PREFLIGHT_SNAPSHOT_MAX_AGE_SECONDS = 10 * 60
MAINTENANCE_ERROR_LOG_JSONL = "local_workbench_maintenance_errors.jsonl"
REVIEW_DECISION_SUMMARY_JSON = "scan-qc-review-decisions.summary.json"
REVIEW_DECISION_DRAFT_JSON = "scan-qc-review-decisions.draft.json"
COMPLETION_NOTE_TXT = "本批次完成交接说明.txt"
PREVIEW_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
DEFAULT_PROCESSING_MODE = "standard"
WINDOWS_DRIVE_MOUNT_ROOT = Path(os.environ.get("AI4ARCHIVE_WINDOWS_DRIVE_MOUNT_ROOT", "/mnt"))
PROCESSING_MODE_OPTIONS: dict[str, dict[str, Any]] = {
    "standard": {
        "label_zh": PROCESSING_MODE_LABELS_ZH["standard"],
        "purpose_zh": PROCESSING_MODE_PURPOSES_ZH["standard"],
        "output_zh": PROCESSING_MODE_OUTPUTS_ZH["standard"],
        "auto_crop": True,
        "deskew": True,
        "trim_dark_border": True,
        "despeckle": True,
    },
    "qc_only": {
        "label_zh": PROCESSING_MODE_LABELS_ZH["qc_only"],
        "purpose_zh": PROCESSING_MODE_PURPOSES_ZH["qc_only"],
        "output_zh": PROCESSING_MODE_OUTPUTS_ZH["qc_only"],
        "auto_crop": False,
        "deskew": False,
        "trim_dark_border": False,
        "despeckle": False,
    },
    "light": {
        "label_zh": PROCESSING_MODE_LABELS_ZH["light"],
        "purpose_zh": PROCESSING_MODE_PURPOSES_ZH["light"],
        "output_zh": PROCESSING_MODE_OUTPUTS_ZH["light"],
        "auto_crop": True,
        "deskew": False,
        "trim_dark_border": False,
        "despeckle": False,
    },
}
SAFE_STAGE_TIMING_LABELS_ZH = {
    "scan": "检查扫描图片",
    "process": "生成处理后图片",
    "summarize": "整理处理结果",
}
SAFE_STAGE_TIMING_STATUS_LABELS_ZH = {
    "running": "进行中",
    "pending": "未开始",
    "failed": "未完成",
}
DISK_SPACE_ESTIMATE_OVERHEAD_NUMERATOR = 6
DISK_SPACE_ESTIMATE_OVERHEAD_DENOMINATOR = 5


class WorkbenchPreflightError(ValueError):
    """Operator-safe folder preflight failure."""

    def __init__(self, guidance: dict[str, Any]) -> None:
        super().__init__(str(guidance.get("message_zh") or "文件夹预检没有通过。"))
        self.guidance = guidance


class WorkbenchController:
    """Small state holder shared by HTTP requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.input_dir: Path | None = None
        self.derivatives_dir: Path | None = None
        self.metadata_dir: Path | None = None
        self.input_dir_display: str | None = None
        self.derivatives_dir_display: str | None = None
        self.metadata_dir_display: str | None = None
        self.processing_mode = DEFAULT_PROCESSING_MODE
        self.last_error: str | None = None
        self.last_preflight_guidance: dict[str, Any] | None = None
        self.last_preflight_reuse_summary: dict[str, Any] | None = None
        self._last_folder_readiness: dict[str, Any] | None = None
        self._last_preflight_snapshot: dict[str, Any] | None = None

    def configure(
        self,
        input_dir: Path | str,
        derivatives_dir: Path | str,
        metadata_dir: Path | str | None = None,
        processing_mode: str | None = None,
    ) -> dict[str, Any]:
        if str(input_dir).strip() in {"", "."}:
            raise ValueError("请填写扫描原图文件夹。")
        if str(derivatives_dir).strip() in {"", "."}:
            raise ValueError("请填写处理后输出文件夹。")
        if metadata_dir is not None and str(metadata_dir).strip() in {"", "."}:
            raise ValueError("请填写本机状态文件夹，或留空使用默认位置。")
        input_path = _safe_resolve_path(input_dir)
        output_path = _safe_resolve_path(derivatives_dir)
        metadata_path = _safe_resolve_path(metadata_dir) if metadata_dir else output_path / DEFAULT_METADATA_DIRNAME
        input_display = _operator_display_path(input_dir, input_path)
        output_display = _operator_display_path(derivatives_dir, output_path)
        metadata_display = (
            _operator_display_path(metadata_dir, metadata_path)
            if metadata_dir
            else _child_display_path(output_display, output_path, metadata_path)
        )
        unsafe_guidance = _unsafe_folder_choice_guidance(input_path, output_path, metadata_path)
        if unsafe_guidance is not None:
            raise WorkbenchPreflightError(unsafe_guidance)
        if not _path_is_existing_dir(input_path):
            raise ValueError("扫描原图文件夹不存在。")
        if not _folder_can_be_listed(input_path):
            raise ValueError("扫描原图文件夹现在不能读取。请重新选择可以打开的原图文件夹。")
        selected_mode = _normalize_processing_mode(processing_mode)
        try:
            output_path.mkdir(parents=True, exist_ok=True)
            metadata_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ValueError("输出文件夹或本机状态文件夹不能创建。请确认磁盘已连接、没有只读，并重新选择文件夹。") from None
        folder_readiness, preflight_snapshot = _folder_readiness_summary_with_snapshot(
            input_path,
            output_path,
            metadata_path,
            selected_mode,
        )
        with self._lock:
            self.input_dir = input_path
            self.derivatives_dir = output_path
            self.metadata_dir = metadata_path
            self.input_dir_display = input_display
            self.derivatives_dir_display = output_display
            self.metadata_dir_display = metadata_display
            self.processing_mode = selected_mode
            self.last_error = None
            self.last_preflight_guidance = None
            self.last_preflight_reuse_summary = None
            self._last_folder_readiness = folder_readiness
            self._last_preflight_snapshot = preflight_snapshot
        return self.status()

    def start(self) -> dict[str, Any]:
        return self._start_run()

    def retry(self) -> dict[str, Any]:
        with self._lock:
            summary = _read_json(self.metadata_dir / PRODUCTION_RUN_SUMMARY_JSON) if self.metadata_dir else None
            progress = _read_json(self.metadata_dir / PRODUCTION_RUN_PROGRESS_JSON) if self.metadata_dir else None
            guidance = _status_recovery_guidance(
                configured=bool(self.input_dir and self.derivatives_dir and self.metadata_dir),
                running=bool(self._thread and self._thread.is_alive()),
                summary=summary,
                progress=progress,
                last_error=self.last_error,
                last_preflight_guidance=self.last_preflight_guidance,
            )
        if guidance.get("kind") != "processing_failed_retryable":
            raise ValueError("当前批次不能直接重试。请按提示检查文件夹，必要时交管理员处理。")
        return self._start_run()

    def reset_for_next_batch(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("当前批次正在处理，不能开始新批次。")
            metadata_path = self.metadata_dir
            derivatives_path = self.derivatives_dir
            retention_summary = _prepare_next_batch_retention_summary(metadata_path, derivatives_path)
            cleanup_summary = _clear_next_batch_transient_state(metadata_path)
            self._thread = None
            self.input_dir = None
            self.derivatives_dir = None
            self.metadata_dir = None
            self.input_dir_display = None
            self.derivatives_dir_display = None
            self.metadata_dir_display = None
            self.processing_mode = DEFAULT_PROCESSING_MODE
            self.last_error = None
            self.last_preflight_guidance = None
            self.last_preflight_reuse_summary = None
            self._last_folder_readiness = None
            self._last_preflight_snapshot = None
        status = self.status()
        status["next_batch_reset"] = {
            "schema_version": "scan-qc.local-next-batch-reset.v1",
            "message_zh": "上一批结果和交接说明已保留；当前工作台队列已清空，可以选择下一批文件夹。",
            "previous_batch_retained": retention_summary,
            "current_workspace_cleared": cleanup_summary,
            "next_step_zh": "请选择下一批扫描原图文件夹；输出文件夹可按本批需要重新选择或沿用。",
        }
        return status

    def _start_run(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("当前批次正在处理。")
            if self.input_dir is None or self.derivatives_dir is None or self.metadata_dir is None:
                raise ValueError("请先填写并保存两个文件夹位置。")
            reuse_summary = _preflight_reuse_summary("unavailable", "missing_preflight_snapshot", 0)
            guidance = None
            cached_ready = (
                isinstance(self._last_folder_readiness, dict)
                and self._last_folder_readiness.get("ready_to_start") is True
            )
            cache_matches, cache_reason = _preflight_snapshot_matches(
                self._last_preflight_snapshot,
                self.input_dir,
                self.derivatives_dir,
                self.metadata_dir,
                self.processing_mode,
            )
            if cached_ready and cache_matches:
                reuse_summary = _preflight_reuse_summary(
                    "reused",
                    "preflight_snapshot_matched",
                    _safe_nonnegative_int(self._last_folder_readiness.get("supported_image_count")),
                )
            else:
                guidance = _preflight_folder_guidance(self.input_dir, self.derivatives_dir, self.metadata_dir)
                fallback_supported_count = 0
                if guidance is None:
                    updated_readiness, updated_snapshot = _folder_readiness_summary_with_snapshot(
                        self.input_dir,
                        self.derivatives_dir,
                        self.metadata_dir,
                        self.processing_mode,
                    )
                    self._last_folder_readiness = updated_readiness
                    self._last_preflight_snapshot = updated_snapshot
                    fallback_supported_count = _safe_nonnegative_int(updated_readiness.get("supported_image_count"))
                else:
                    fallback_supported_count = _safe_nonnegative_int(guidance.get("supported_image_count"))
                reuse_summary = _preflight_reuse_summary(
                    "rescanned",
                    cache_reason,
                    fallback_supported_count,
                )
            if guidance is not None:
                self.last_error = None
                self.last_preflight_guidance = guidance
                self.last_preflight_reuse_summary = reuse_summary
                raise WorkbenchPreflightError(guidance)
            self.last_error = None
            self.last_preflight_guidance = None
            self.last_preflight_reuse_summary = reuse_summary
            self._thread = threading.Thread(target=self._run_once, name="production-workbench-run", daemon=True)
            self._thread.start()
        return self.status()

    def preview_path(self, local_id: str, requested_source: str | None = None) -> tuple[Path, str]:
        safe_id = local_id.strip()
        if not safe_id:
            raise ValueError("预览请求缺少复核编号。")
        source_filter = (requested_source or "").strip()
        if source_filter and source_filter not in {"original", "processed"}:
            raise ValueError("预览来源不正确。")
        with self._lock:
            input_dir = self.input_dir
            derivatives_dir = self.derivatives_dir
            metadata_dir = self.metadata_dir
        if input_dir is None or derivatives_dir is None or metadata_dir is None:
            raise ValueError("请先保存文件夹并生成复核队列。")
        queue = _read_json(metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON)
        items = queue.get("items") if isinstance(queue, dict) else None
        if not isinstance(items, list):
            raise ValueError("尚未生成可预览的复核队列。")
        item = next((entry for entry in items if isinstance(entry, dict) and entry.get("local_id") == safe_id), None)
        if not item:
            raise ValueError("未找到这条复核记录。")
        relative_path = _safe_relative_path(str(item.get("relative_path") or ""))
        for candidate, source in _preview_candidates(input_dir, derivatives_dir, relative_path):
            if source_filter and source != source_filter:
                continue
            if _valid_preview_path(candidate, input_dir, derivatives_dir):
                response_source = "original_fallback" if not source_filter and source == "original" else source
                return candidate.expanduser().resolve(), response_source
        raise ValueError("未找到这条复核记录对应的本机预览图。")

    def save_review_decisions(self, summary: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            derivatives_dir = self.derivatives_dir
            metadata_dir = self.metadata_dir
            derivatives_display = self.derivatives_dir_display
            metadata_display = self.metadata_dir_display
            processing_mode = self.processing_mode
        if derivatives_dir is None or metadata_dir is None:
            raise ValueError("请先保存文件夹并生成复核队列。")
        derivatives_display = derivatives_display or str(derivatives_dir)
        metadata_display = metadata_display or str(metadata_dir)
        verification = build_review_decision_verification_summary(summary)
        if verification.get("status") != "pass":
            raise ValueError("复核决定还不能完成，请检查是否还有待处理图片。")
        decision_summary = verification.get("decision_summary") if isinstance(verification.get("decision_summary"), dict) else {}
        closure_summary = (
            decision_summary.get("closure_gate_summary")
            if isinstance(decision_summary.get("closure_gate_summary"), dict)
            else {}
        )
        pending_decisions = int(decision_summary.get("pending") or 0)
        if pending_decisions > 0:
            raise ValueError(f"仍有 {pending_decisions} 项待人工确认，请先完成待看图复核后再完成导出。")
        if decision_summary.get("completion_status") != "complete" or closure_summary.get("can_complete_delivery") is not True:
            raise ValueError("还有需要重扫/重新处理的图片，先处理后再完成导出。")

        total_decisions = int(decision_summary.get("total_decisions") or 0)
        reviewed_decisions = max(0, total_decisions - pending_decisions)
        open_p0_count = int(closure_summary.get("open_p0_count") or 0)
        open_p1_count = int(closure_summary.get("open_p1_count") or 0)
        run_summary = _read_json(metadata_dir / PRODUCTION_RUN_SUMMARY_JSON)
        progress = _read_json(metadata_dir / PRODUCTION_RUN_PROGRESS_JSON)
        queue = _read_json(metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON)
        handoff_consistency = _completion_handoff_consistency(
            summary=run_summary,
            progress=progress,
            queue=queue,
            final_decisions=summary,
            final_verification=verification,
            decision_summary=decision_summary,
        )
        if handoff_consistency.get("consistent") is not True:
            guidance = _handoff_count_mismatch_guidance(handoff_consistency)
            raise ValueError(str(guidance["message_zh"]))
        metadata_dir.mkdir(parents=True, exist_ok=True)
        summary_path = metadata_dir / REVIEW_DECISION_SUMMARY_JSON
        verification_path = metadata_dir / REVIEW_DECISION_VERIFICATION_JSON
        completion_note_path = metadata_dir / COMPLETION_NOTE_TXT
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        verification_path.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        handoff_counts = _completion_handoff_counts(run_summary, decision_summary, summary)
        reuse_handoff_summary = _local_reuse_handoff_summary(run_summary)
        conservative_handoff = _conservative_auto_retouch_handoff_zh(run_summary)
        operator_name = str(summary.get("operator_name") or "").strip()
        stage_timings_note = _completion_stage_timings_note(run_summary)
        completion_note_lines = [
            "本批已完成交接说明",
            "本批次是否完成：已完成，可交接",
            f"复核人员：{operator_name or '未填写'}",
            f"处理方式：{_processing_mode_completion_label(processing_mode)}",
            f"扫描原图总数：{handoff_counts['total_source_images']} 张",
            f"处理后图片数量：{handoff_counts['processed_output_images']} 张",
            f"需要重扫：{handoff_counts['needs_rescan_images']} 张",
            f"需要重新处理：{handoff_counts['needs_reprocess_images']} 张",
            f"确认保留原貌：{handoff_counts['keep_original_images']} 张",
            f"待决定：{pending_decisions}",
            f"复核总数：{total_decisions}",
            f"已确认：{reviewed_decisions}",
            f"未关闭 P0：{open_p0_count}",
            f"未关闭 P1：{open_p1_count}",
            f"已有人工处理结论：{int(closure_summary.get('manually_handled_count') or reviewed_decisions)}",
        ]
        if isinstance(reuse_handoff_summary, dict):
            completion_note_lines.insert(9, reuse_handoff_summary["message_zh"])
        if isinstance(conservative_handoff, dict) and isinstance(conservative_handoff.get("message_zh"), str):
            completion_note_lines.append(
                f"{str(conservative_handoff.get('title_zh') or '保守自动修复决策汇总')}：{conservative_handoff['message_zh']}"
            )
        if stage_timings_note:
            completion_note_lines.append(stage_timings_note)
        completion_note_lines.extend(
            [
                (
                    "交接事项：处理后图片已保存到输出文件夹；需要重扫和重新处理的数量已写入本交接说明；"
                    "复核结果和交接说明已保存到本机状态文件夹。"
                ),
                "交接前检查：打开输出文件夹，确认本批处理后图片数量和画面状态符合交接要求。",
                f"下一批：{handoff_counts['next_batch_reminder_zh']}",
                "",
            ]
        )
        completion_note_path.write_text(
            "\n".join(completion_note_lines),
            encoding="utf-8",
        )
        completion_panel = {
            "title_zh": "本批已完成",
            "message_zh": "处理后图片已准备好。请检查输出文件夹后再交接。",
            "completion_status_zh": "本批已完成",
            "manual_work_zh": "没有待人工处理图片",
            "admin_handoff_zh": "不需要",
            "total_review_items": total_decisions,
            "reviewed_items": reviewed_decisions,
            "pending_items": pending_decisions,
            "closure_gate_summary": closure_summary,
            "processed_output_images": handoff_counts["processed_output_images"],
            "needs_rescan_images": handoff_counts["needs_rescan_images"],
            "needs_reprocess_images": handoff_counts["needs_reprocess_images"],
            "keep_original_images": handoff_counts["keep_original_images"],
            "total_source_images": handoff_counts["total_source_images"],
            "next_batch_reminder_zh": handoff_counts["next_batch_reminder_zh"],
            "processing_mode": _processing_mode_payload(processing_mode),
            "decision_summary_saved": True,
            "verification_summary_saved": True,
            "completion_note_saved": True,
            "open_output_folder_available": True,
            "checklist_zh": [
                f"打开输出文件夹，检查 {handoff_counts['processed_output_images']} 张处理后图片的数量和画面状态",
                f"需要重扫 {handoff_counts['needs_rescan_images']} 张，需要重新处理 {handoff_counts['needs_reprocess_images']} 张",
                f"确认保留原貌 {handoff_counts['keep_original_images']} 张",
                "复核结果和交接说明已保存到本机状态文件夹",
                "准备下一批会清空当前复核队列，请重新选择新一批文件夹",
            ],
            "next_steps_zh": [
                f"打开输出文件夹，检查 {handoff_counts['processed_output_images']} 张处理后图片的数量和画面状态。",
                f"需要重扫 {handoff_counts['needs_rescan_images']} 张；需要重新处理 {handoff_counts['needs_reprocess_images']} 张。",
                f"确认保留原貌 {handoff_counts['keep_original_images']} 张。",
                "本机状态文件夹已保存复核结果和交接说明，正常界面不显示具体路径或文件名。",
                handoff_counts["next_batch_reminder_zh"],
                "如果仍有异常或不能交接，请交管理员处理。",
            ],
        }
        if reuse_handoff_summary is not None:
            completion_panel["local_reuse_summary"] = reuse_handoff_summary
        if conservative_handoff is not None:
            completion_panel["conservative_auto_retouch_handoff_zh"] = conservative_handoff

        return {
            "schema_version": SERVER_SCHEMA,
            "finished": True,
            "message_zh": "本批已完成：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
            "saved": {
                "decision_summary": True,
                "verification_summary": True,
                "completion_note": True,
            },
            "completion_panel": completion_panel,
            "decision_summary": decision_summary,
        }

    def save_draft_review_decisions(self, summary: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            metadata_dir = self.metadata_dir
        if metadata_dir is None:
            raise ValueError("请先保存文件夹并生成复核队列。")
        verification = build_review_decision_verification_summary(summary)
        if verification.get("status") != "pass":
            raise ValueError("复核进度暂不能保存，请重新选择。")

        metadata_dir.mkdir(parents=True, exist_ok=True)
        draft_path = metadata_dir / REVIEW_DECISION_DRAFT_JSON
        draft_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {
            "schema_version": SERVER_SCHEMA,
            "saved": True,
            "message_zh": "已自动保存",
            "draft_decisions": summary,
            "decision_summary": verification.get("decision_summary"),
        }

    def open_output_folder(self) -> dict[str, Any]:
        with self._lock:
            derivatives_dir = self.derivatives_dir
            derivatives_display = self.derivatives_dir_display or (str(derivatives_dir) if derivatives_dir else "")
            metadata_dir = self.metadata_dir
        if derivatives_dir is None or not derivatives_dir.exists() or not derivatives_dir.is_dir():
            raise ValueError("处理后输出文件夹现在不能打开。请重新选择输出文件夹，或联系管理员处理。")
        if not _batch_has_completed(metadata_dir):
            raise ValueError("本批还没有完成。完成本批后才能打开输出文件夹。")
        if not _open_operator_folder(derivatives_dir, derivatives_display):
            raise ValueError("输出文件夹没有打开。请重新选择输出文件夹，或联系管理员处理。")
        return {
            "schema_version": SERVER_SCHEMA,
            "opened": True,
            "message_zh": "已打开输出文件夹。请检查处理后图片数量和画面状态。",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            input_path = self.input_dir
            derivatives_path = self.derivatives_dir
            metadata_path = self.metadata_dir
            input_dir = self.input_dir_display or (str(input_path) if input_path else None)
            derivatives_dir = self.derivatives_dir_display or (str(derivatives_path) if derivatives_path else None)
            metadata_dir = self.metadata_dir_display or (str(metadata_path) if metadata_path else None)
            processing_mode = self.processing_mode
            last_error = self.last_error
            last_preflight_guidance = self.last_preflight_guidance
            last_preflight_reuse_summary = self.last_preflight_reuse_summary
        folder_readiness = self._folder_readiness_for_status(input_path, derivatives_path, metadata_path, processing_mode)
        raw_summary = _read_json(metadata_path / PRODUCTION_RUN_SUMMARY_JSON) if metadata_path else None
        summary = _sanitize_operator_status_summary(raw_summary)
        progress = _read_json(metadata_path / PRODUCTION_RUN_PROGRESS_JSON) if metadata_path else None
        queue = self._queue_with_preview_sources(metadata_path) if metadata_path else None
        draft_decisions = _read_json(metadata_path / REVIEW_DECISION_DRAFT_JSON) if metadata_path else None
        final_decisions = _read_json(metadata_path / REVIEW_DECISION_SUMMARY_JSON) if metadata_path else None
        final_verification = _read_json(metadata_path / REVIEW_DECISION_VERIFICATION_JSON) if metadata_path else None
        restored_batch = _restored_batch_status(
            configured=bool(input_path and derivatives_path and metadata_path),
            running=running,
            summary=summary,
            progress=progress,
            queue=queue,
            draft_decisions=draft_decisions,
            final_decisions=final_decisions,
            final_verification=final_verification,
        )
        completion_panel = _restored_completion_panel(restored_batch, summary, final_decisions, processing_mode)
        recovery_guidance = _status_recovery_guidance(
            configured=bool(input_path and derivatives_path and metadata_path),
            running=running,
            summary=summary,
            progress=progress,
            last_error=last_error,
            last_preflight_guidance=last_preflight_guidance,
        )
        if isinstance(restored_batch, dict) and restored_batch.get("kind") == "handoff_count_mismatch":
            recovery_guidance = _sanitize_operator_guidance(restored_batch)
        return {
            "schema_version": SERVER_SCHEMA,
            "running": running,
            "configured": bool(input_path and derivatives_path and metadata_path),
            "last_error_zh": last_error,
            "preflight_guidance": last_preflight_guidance,
            "preflight_reuse_summary": last_preflight_reuse_summary,
            "recovery_guidance": recovery_guidance,
            "folders": {
                "input": input_dir,
                "derivatives": derivatives_dir,
                "metadata": metadata_dir,
            },
            "processing_mode": _processing_mode_payload(processing_mode),
            "folder_readiness": folder_readiness,
            "restored_batch": restored_batch,
            "completion_panel": completion_panel,
            "summary": summary,
            "progress": progress,
            "queue": queue,
            "draft_decisions": draft_decisions,
        }

    def _folder_readiness_for_status(
        self,
        input_path: Path | None,
        derivatives_path: Path | None,
        metadata_path: Path | None,
        processing_mode: str,
    ) -> dict[str, Any]:
        with self._lock:
            cached_readiness = self._last_folder_readiness
            cached_snapshot = self._last_preflight_snapshot
        cache_matches, _reason = _preflight_snapshot_matches(
            cached_snapshot,
            input_path,
            derivatives_path,
            metadata_path,
            processing_mode,
        )
        if cache_matches and isinstance(cached_readiness, dict):
            return dict(cached_readiness)
        readiness, snapshot = _folder_readiness_summary_with_snapshot(
            input_path,
            derivatives_path,
            metadata_path,
            processing_mode,
        )
        with self._lock:
            if self.input_dir == input_path and self.derivatives_dir == derivatives_path and self.metadata_dir == metadata_path:
                self._last_folder_readiness = readiness
                self._last_preflight_snapshot = snapshot
        return readiness

    def _queue_with_preview_sources(self, metadata_dir: Path) -> dict[str, Any] | None:
        queue = _read_json(metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON)
        if not isinstance(queue, dict):
            return None
        with self._lock:
            input_dir = self.input_dir
            derivatives_dir = self.derivatives_dir
        items = queue.get("items")
        if input_dir is None or derivatives_dir is None or not isinstance(items, list):
            return queue
        enriched = dict(queue)
        enriched_items = []
        for item in items:
            if not isinstance(item, dict):
                enriched_items.append(item)
                continue
            enriched_item = dict(item)
            sources = _preview_sources_for_item(item, input_dir, derivatives_dir)
            enriched_item["preview_sources"] = sources
            enriched_item["preview_source"] = _preview_source_label(sources)
            enriched_items.append(enriched_item)
        enriched["items"] = enriched_items
        return enriched

    def _run_once(self) -> None:
        try:
            with self._lock:
                assert self.input_dir is not None
                assert self.derivatives_dir is not None
                assert self.metadata_dir is not None
                processing_mode = self.processing_mode
                processing_options = PROCESSING_MODE_OPTIONS[processing_mode]
                config = ProductionRunConfig(
                    input_dir=self.input_dir,
                    derivative_output_dir=self.derivatives_dir,
                    metadata_output_dir=self.metadata_dir,
                    auto_crop=bool(processing_options["auto_crop"]),
                    deskew=bool(processing_options["deskew"]),
                    trim_dark_border=bool(processing_options["trim_dark_border"]),
                    despeckle=bool(processing_options["despeckle"]),
                    resume_processing=True,
                    reuse_scan_measurements=True,
                    processing_mode=processing_mode,
                )
            summary = run_production_folder(config)
            self._write_review_queue(summary)
        except Exception as exc:  # pragma: no cover - exercised through status in integration use.
            _write_maintenance_error(self.metadata_dir, exc)
            with self._lock:
                self.last_error = sanitize_operator_error_zh(exc)

    def _write_review_queue(self, summary: dict[str, Any]) -> None:
        with self._lock:
            metadata_dir = self.metadata_dir
            derivatives_dir = self.derivatives_dir
        if metadata_dir is None or derivatives_dir is None:
            return
        artifacts = summary.get("artifacts") if isinstance(summary, dict) else {}
        scan_report = (
            Path(str(artifacts.get("admin_scan_report")))
            if isinstance(artifacts, dict) and artifacts.get("admin_scan_report")
            else None
        )
        processing_manifest = derivatives_dir / "processing_manifest.json"
        processing_package = metadata_dir / PROCESSING_REVIEW_JSON
        if processing_manifest.exists():
            write_processing_review_package(processing_manifest, metadata_dir)
        write_production_review_queue(
            metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON,
            scan_qc_report_path=scan_report if scan_report and scan_report.exists() else None,
            processing_review_package_path=processing_package if processing_package.exists() else None,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc production-workbench",
        description="在本机回环地址启动中文生产工作台，并协调本地处理产物。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="只建议使用 127.0.0.1。")
    parser.add_argument("--port", default=8765, type=int, help="本机端口。")
    parser.add_argument("--no-open", action="store_true", help="只启动服务，不自动打开浏览器。")
    parser.add_argument("--input-dir", default=None, help="预先填写扫描原图文件夹。")
    parser.add_argument("--derivatives-dir", default=None, help="预先填写处理后输出文件夹。")
    parser.add_argument("--metadata-dir", default=None, help="预先填写本机状态文件夹；默认使用输出文件夹下的状态文件夹。")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("production-workbench is local-only; use 127.0.0.1, localhost, or ::1.")
    if bool(args.input_dir) != bool(args.derivatives_dir):
        parser.error("请同时提供 --input-dir 和 --derivatives-dir，或都不提供。")
    try:
        server = make_server(
            args.host,
            args.port,
            input_dir=args.input_dir,
            derivatives_dir=args.derivatives_dir,
            metadata_dir=args.metadata_dir,
        )
    except ValueError as exc:
        parser.error(str(exc))
    host_for_url = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{host_for_url}:{server.server_port}/"
    print(f"本地生产工作台: {url}")
    if args.input_dir and args.derivatives_dir:
        print("已预先填写演练文件夹，打开后可直接查看批次状态。")
    print("按 Ctrl+C 停止。")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止本地生产工作台。")
    finally:
        server.server_close()
    return 0


def make_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    input_dir: Path | str | None = None,
    derivatives_dir: Path | str | None = None,
    metadata_dir: Path | str | None = None,
) -> ThreadingHTTPServer:
    controller = WorkbenchController()
    if input_dir is not None or derivatives_dir is not None or metadata_dir is not None:
        if input_dir is None or derivatives_dir is None:
            raise ValueError("请同时提供扫描原图文件夹和处理后输出文件夹。")
        controller.configure(input_dir, derivatives_dir, metadata_dir)

    class Handler(WorkbenchRequestHandler):
        workbench_controller = controller

    return ThreadingHTTPServer((host, port), Handler)


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    workbench_controller: WorkbenchController

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        parsed = urlparse(self.path)
        if not _is_loopback_client(self.client_address[0]):
            self._send_json({"error_zh": "本机预览只允许回环地址访问。"}, HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/api/status":
            self._send_json(self.workbench_controller.status())
            return
        if parsed.path.startswith("/api/preview/"):
            parts = [unquote(part) for part in parsed.path.removeprefix("/api/preview/").split("/") if part]
            self._serve_preview(parts[0] if parts else "", parts[1] if len(parts) > 1 else None)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        try:
            payload = self._read_payload()
            if self.path == "/api/configure":
                result = self.workbench_controller.configure(
                    _required_path(payload, "input_dir", "扫描原图文件夹"),
                    _required_path(payload, "derivatives_dir", "处理后输出文件夹"),
                    _optional_path(payload, "metadata_dir", "本机状态文件夹"),
                    str(payload.get("processing_mode") or DEFAULT_PROCESSING_MODE),
                )
            elif self.path == "/api/pick-folder":
                result = _pick_operator_folder(payload)
            elif self.path == "/api/start":
                result = self.workbench_controller.start()
            elif self.path == "/api/retry":
                result = self.workbench_controller.retry()
            elif self.path == "/api/reset-batch":
                result = self.workbench_controller.reset_for_next_batch()
            elif self.path == "/api/finish-decisions":
                result = self.workbench_controller.save_review_decisions(payload)
            elif self.path == "/api/save-draft-decisions":
                result = self.workbench_controller.save_draft_review_decisions(payload)
            elif self.path == "/api/open-output-folder":
                result = self.workbench_controller.open_output_folder()
            else:
                self._send_json({"error_zh": "未知请求。"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(result)
        except WorkbenchPreflightError as exc:
            self._send_json(
                {
                    "error_zh": str(exc),
                    "preflight_guidance": exc.guidance,
                    "recovery_guidance": exc.guidance,
                },
                HTTPStatus.BAD_REQUEST,
            )
        except ValueError as exc:
            self._send_json({"error_zh": sanitize_operator_error_zh(exc)}, HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self._send_json({"error_zh": "请求内容无法读取。"}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _serve_static(self, raw_path: str) -> None:
        if raw_path in {"", "/"}:
            path = WORKBENCH_HTML
        else:
            candidate = (DOCS_DIR / unquote(raw_path.lstrip("/"))).resolve()
            if DOCS_DIR.resolve() not in candidate.parents and candidate != DOCS_DIR.resolve():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path = candidate
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8" if path.suffix == ".html" else "application/json; charset=utf-8"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_preview(self, local_id: str, requested_source: str | None = None) -> None:
        try:
            path, source = self.workbench_controller.preview_path(local_id, requested_source)
        except ValueError as exc:
            self._send_json({"error_zh": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Preview-Source", source)
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容格式不正确。")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _prepare_next_batch_retention_summary(
    metadata_dir: Path | None,
    derivatives_dir: Path | None,
) -> dict[str, Any]:
    metadata_available = metadata_dir is not None and metadata_dir.exists()
    output_available = derivatives_dir is not None and derivatives_dir.exists()
    completion_note_retained = bool(metadata_available and (metadata_dir / COMPLETION_NOTE_TXT).exists())
    aggregate_credentials_retained = bool(
        metadata_available
        and (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).exists()
        and (metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists()
        and (metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).exists()
    )
    return {
        "aggregate_only": True,
        "output_results_retained": output_available,
        "completion_note_retained": completion_note_retained,
        "handoff_credentials_retained": aggregate_credentials_retained,
        "message_zh": "上一批输出结果、中文交接说明和可交接聚合凭据已保留。",
    }


def _clear_next_batch_transient_state(metadata_dir: Path | None) -> dict[str, Any]:
    targets = [
        metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON if metadata_dir else None,
        metadata_dir / REVIEW_DECISION_DRAFT_JSON if metadata_dir else None,
        metadata_dir / PRODUCTION_RUN_PROGRESS_JSON if metadata_dir else None,
    ]
    cleared_count = 0
    failed_count = 0
    for target in targets:
        if target is None or not target.exists():
            continue
        try:
            target.unlink()
            cleared_count += 1
        except OSError as exc:
            failed_count += 1
            _write_maintenance_error(metadata_dir, exc)
    return {
        "aggregate_only": True,
        "queue_cleared": True,
        "draft_review_cleared": True,
        "running_state_cleared": True,
        "cleared_transient_artifacts": cleared_count,
        "cleanup_error_count": failed_count,
        "message_zh": "当前工作台队列、临时复核状态和运行进度已清空。",
    }


def _pick_operator_folder(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "").strip()
    titles = {
        "input": "选择本批次扫描原图文件夹",
        "derivatives": "选择处理后输出文件夹",
    }
    if kind not in titles:
        raise ValueError("未知文件夹选择请求。")
    selected = _pick_native_folder(titles[kind])
    if selected is None:
        return {"schema_version": SERVER_SCHEMA, "cancelled": True, "message_zh": "已取消选择文件夹。"}
    return {
        "schema_version": SERVER_SCHEMA,
        "cancelled": False,
        "path": selected,
        "message_zh": "已选择文件夹，请保存文件夹后再开始处理。",
    }


def _pick_native_folder(title_zh: str) -> str | None:
    forced = os.environ.get("AI4ARCHIVE_PICK_FOLDER_RESULT")
    if forced is not None:
        return forced or None
    if _running_under_wsl():
        return _pick_windows_folder_via_powershell(title_zh)
    if sys.platform == "darwin":
        return _pick_macos_folder(title_zh)
    return _pick_linux_folder(title_zh)


def _running_under_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _running_on_native_windows() -> bool:
    return sys.platform.startswith("win") and not _running_under_wsl()


def _pick_windows_folder_via_powershell(title_zh: str) -> str | None:
    powershell = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if not Path(powershell).exists():
        powershell = "powershell.exe"
    script = "\n".join(
        [
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
            "Add-Type -AssemblyName System.Windows.Forms",
            "$ownerForm = New-Object System.Windows.Forms.Form",
            "$ownerForm.ShowInTaskbar = $false",
            "$ownerForm.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen",
            "$ownerForm.Size = New-Object System.Drawing.Size(1, 1)",
            "$ownerForm.Opacity = 0",
            "$ownerForm.TopMost = $true",
            "$ownerForm.Add_Shown({ $ownerForm.Activate(); $ownerForm.BringToFront() })",
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog",
            f"$dialog.Description = {json.dumps(title_zh, ensure_ascii=False)}",
            "$dialog.ShowNewFolderButton = $true",
            "$ownerForm.Show()",
            "$result = $dialog.ShowDialog($ownerForm)",
            "$ownerForm.Dispose()",
            "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dialog.SelectedPath; exit 0 }",
            "exit 2",
        ]
    )
    return _run_folder_picker_command([powershell, "-NoProfile", "-STA", "-Command", script])


def _pick_macos_folder(title_zh: str) -> str | None:
    script = f'POSIX path of (choose folder with prompt {json.dumps(title_zh, ensure_ascii=False)})'
    return _run_folder_picker_command(["osascript", "-e", script])


def _pick_linux_folder(title_zh: str) -> str | None:
    zenity = shutil.which("zenity")
    if not zenity:
        raise ValueError("当前环境没有可用的系统文件夹选择器，请直接填写本机文件夹路径。")
    return _run_folder_picker_command([zenity, "--file-selection", "--directory", "--title", title_zh])


def _run_folder_picker_command(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("系统文件夹选择器没有正常打开，请直接填写本机文件夹路径。") from None
    output = completed.stdout.strip()
    if completed.returncode == 0 and output:
        return output.splitlines()[-1].strip()
    if completed.returncode in {1, 2}:
        return None
    raise ValueError("系统文件夹选择器没有正常返回路径，请直接填写本机文件夹路径。")


def _batch_has_completed(metadata_dir: Path | None) -> bool:
    if metadata_dir is None:
        return False
    summary = _read_json(metadata_dir / PRODUCTION_RUN_SUMMARY_JSON)
    progress = _read_json(metadata_dir / PRODUCTION_RUN_PROGRESS_JSON)
    queue = _read_json(metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON)
    final_decisions = _read_json(metadata_dir / REVIEW_DECISION_SUMMARY_JSON)
    final_verification = _read_json(metadata_dir / REVIEW_DECISION_VERIFICATION_JSON)
    consistency = _completion_handoff_consistency(
        summary=summary,
        progress=progress,
        queue=queue,
        final_decisions=final_decisions,
        final_verification=final_verification,
    )
    if consistency.get("consistent") is not True:
        return False
    if (metadata_dir / COMPLETION_NOTE_TXT).exists():
        return True
    status = str(summary.get("status") or "").strip().lower() if isinstance(summary, dict) else ""
    return status in {"completed", "finished"}


def _open_operator_folder(path: Path, display_path: str) -> bool:
    forced_command = os.environ.get("AI4ARCHIVE_OPEN_FOLDER_COMMAND")
    if forced_command:
        command = [forced_command, str(path)]
    elif _running_under_wsl():
        target = display_path if _display_looks_like_windows_path(display_path) else _wsl_path_to_windows(path)
        command = ["explorer.exe", target]
    elif sys.platform == "darwin":
        command = ["open", str(path)]
    elif os.name == "nt":
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            return False
        command = [opener, str(path)]
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def _wsl_path_to_windows(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(WINDOWS_DRIVE_MOUNT_ROOT)
    except (OSError, ValueError):
        return str(path)
    parts = relative.parts
    if not parts:
        return str(path)
    drive = parts[0].upper()
    if len(drive) != 1 or not drive.isalpha():
        return str(path)
    tail = "\\".join(parts[1:])
    return f"{drive}:\\" + tail if tail else f"{drive}:\\"


def _required_path(payload: dict[str, Any], key: str, label_zh: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"请填写{label_zh}。")
    return _strip_pasted_path_quotes(value.strip())


def _optional_path(payload: dict[str, Any], key: str, label_zh: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"请填写{label_zh}，或留空使用默认位置。")
    return _strip_pasted_path_quotes(value.strip())


def _safe_resolve_path(path: Path | str) -> Path:
    try:
        return _normalize_operator_path(path).expanduser().resolve()
    except OSError:
        raise ValueError("文件夹位置无法读取。请重新选择本机可以打开的文件夹。") from None


def _normalize_operator_path(path: Path | str) -> Path:
    raw = _strip_pasted_path_quotes(str(path).strip())
    raw = _decode_file_url_path(raw)
    raw = _strip_windows_extended_prefix(raw)
    wsl_unc_path = _windows_wsl_unc_path_to_linux(raw)
    if wsl_unc_path is not None:
        return wsl_unc_path
    native_windows_path = _windows_drive_path_to_native(raw)
    if native_windows_path is not None:
        return native_windows_path
    drive_path = _windows_drive_path_to_wsl(raw)
    if drive_path is not None:
        return drive_path
    if _looks_like_windows_unc_path(raw):
        raise ValueError("暂不支持 Windows 网络共享路径。请先映射为本机盘符，或在 WSL 中使用 /mnt/<盘符>/... 路径。")
    return Path(raw)


def _operator_display_path(path: Path | str, resolved_path: Path) -> str:
    raw = _strip_pasted_path_quotes(str(path).strip())
    raw = _decode_file_url_path(raw)
    raw = _strip_windows_extended_prefix(raw)
    if _windows_wsl_unc_path_to_linux(raw) is not None:
        return raw
    if _windows_drive_path_to_native(raw) is not None:
        return raw
    if _windows_drive_path_to_wsl(raw) is not None:
        return raw
    return str(resolved_path)


def _child_display_path(parent_display: str, parent_path: Path, child_path: Path) -> str:
    try:
        relative = child_path.relative_to(parent_path)
    except ValueError:
        return str(child_path)
    if _display_looks_like_windows_path(parent_display):
        separator = "\\" if "\\" in parent_display else "/"
        return parent_display.rstrip("\\/") + separator + separator.join(relative.parts)
    return str(child_path)


def _display_looks_like_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _strip_pasted_path_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _decode_file_url_path(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "file":
        return value
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return f"//{parsed.netloc}{unquote(parsed.path)}"
    path = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:", path):
        return path[1:]
    return path


def _strip_windows_extended_prefix(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value.removeprefix("\\\\?\\UNC\\")
    if value.startswith("//?/UNC/"):
        return "//" + value.removeprefix("//?/UNC/")
    if value.startswith("\\\\?\\") or value.startswith("//?/"):
        return value[4:]
    return value


def _windows_wsl_unc_path_to_linux(value: str) -> Path | None:
    if not re.match(r"^(?:\\\\|//)wsl(?:\$|\.localhost)[\\/]", value, re.IGNORECASE):
        return None
    parts = [part for part in re.split(r"[\\/]+", value.lstrip("\\/")) if part]
    if len(parts) < 3:
        raise ValueError("WSL 文件夹路径不完整。请重新选择本机可以打开的文件夹。")
    return Path("/") / Path(*parts[2:])


def _windows_drive_path_to_wsl(value: str) -> Path | None:
    parsed = _parse_windows_drive_path(value)
    if parsed is None or _running_on_native_windows():
        return None
    drive, tail = parsed
    parts = [part for part in re.split(r"[\\/]+", tail) if part]
    return WINDOWS_DRIVE_MOUNT_ROOT / drive / Path(*parts)


def _windows_drive_path_to_native(value: str) -> Path | None:
    if not _running_on_native_windows():
        return None
    parsed = _parse_windows_drive_path(value)
    if parsed is None:
        return None
    return Path(value.replace("/", "\\"))


def _parse_windows_drive_path(value: str) -> tuple[str, str] | None:
    match = re.match(r"^([A-Za-z]):(?:[\\/](.*))?$", value)
    if match is None:
        if re.match(r"^[A-Za-z]:", value):
            raise ValueError("Windows 路径请使用完整盘符路径，例如 C:\\... 或 C:/...。")
        return None
    drive = match.group(1).lower()
    tail = match.group(2) or ""
    return drive, tail


def _looks_like_windows_unc_path(value: str) -> bool:
    return bool(re.match(r"^(?:\\\\|//)[^\\/]+[\\/]+[^\\/]+", value))


_PRIVATE_OR_TECHNICAL_ERROR_PATTERNS = [
    re.compile(r"(/Users/|/Volumes/|/private/|/[A-Za-z0-9_. -]+/[A-Za-z0-9_. /-]+)"),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"\b[\w.-]+\.(?:tif|tiff|jpg|jpeg|png|bmp|webp|pdf|csv|json|py)\b", re.IGNORECASE),
    re.compile(r"\b[a-f0-9]{32,128}\b", re.IGNORECASE),
    re.compile(r"\b(?:Traceback|File \"|line \d+|Exception|Error|OSError|ValueError|RuntimeError|PermissionError)\b"),
    re.compile(r"\b(?:OCR|PRIVATE_OCR|sha256|hash|stack|numpy|PIL|cv2|python)\b", re.IGNORECASE),
]

_OPERATOR_SAFE_ERROR_MESSAGES_ZH = {
    "请填写扫描原图文件夹。",
    "请填写处理后输出文件夹。",
    "请填写本机状态文件夹，或留空使用默认位置。",
    "扫描原图文件夹不存在。",
    "扫描原图文件夹现在不能读取。请重新选择可以打开的原图文件夹。",
    "输出文件夹或本机状态文件夹不能创建。请确认磁盘已连接、没有只读，并重新选择文件夹。",
    "当前批次不能直接重试。请按提示检查文件夹，必要时交管理员处理。",
    "当前批次正在处理。",
    "当前批次正在处理，不能开始新批次。",
    "请先填写并保存两个文件夹位置。",
    "预览请求缺少复核编号。",
    "预览来源不正确。",
    "请先保存文件夹并生成复核队列。",
    "尚未生成可预览的复核队列。",
    "未找到这条复核记录。",
    "未找到这条复核记录对应的本机预览图。",
    "复核决定还不能完成，请检查是否还有待处理图片。",
    "复核进度暂不能保存，请重新选择。",
    "请求内容格式不正确。",
    "处理方式不正确，请重新选择。",
    "文件夹位置无法读取。请重新选择本机可以打开的文件夹。",
    "请同时提供扫描原图文件夹和处理后输出文件夹。",
    "处理后输出文件夹不能和扫描原图文件夹相同，也不能放在原图文件夹里面。",
    "本机状态文件夹不能放在扫描原图文件夹里面，处理没有启动。",
}


def sanitize_operator_error_zh(error: BaseException | str | None) -> str:
    """Return operator-safe Chinese guidance without raw local details."""
    text = str(error or "").strip()
    if not text:
        return "本批次没有正常启动，请交管理员处理。"
    lowered = text.lower()
    if _is_known_operator_safe_message_zh(text):
        return text
    if any(token in lowered for token in ["permission", "denied", "access", "not permitted", "无法读取", "不能读取"]):
        return "文件夹无法读取：请检查扫描原图文件夹是否存在、是否有权限。"
    if any(token in lowered for token in ["no such file", "not found", "不存在", "moved"]):
        return "文件夹无法读取：请检查扫描原图文件夹是否存在、是否有权限。"
    if any(token in lowered for token in ["read-only", "readonly", "no space", "disk", "write", "写入", "空间"]):
        return "输出文件夹无法写入：请检查输出文件夹和磁盘空间。"
    if any(token in lowered for token in ["cannot identify image", "unidentifiedimageerror", "truncated", "image", "图片"]):
        return "图片无法打开：请检查原图图片是否损坏。"
    return "其他异常：本批次没有正常启动，请交管理员处理。"


def _sanitize_operator_status_summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return summary
    sanitized = dict(summary)
    for key in ("message", "message_zh", "operator_message_zh", "last_error_zh"):
        if isinstance(sanitized.get(key), str):
            sanitized[key] = _sanitize_operator_visible_text_zh(sanitized[key])
    operator = sanitized.get("operator_summary")
    if isinstance(operator, dict):
        sanitized_operator = dict(operator)
        for key in ("message", "message_zh", "operator_message_zh", "last_error_zh"):
            if isinstance(sanitized_operator.get(key), str):
                sanitized_operator[key] = _sanitize_operator_visible_text_zh(sanitized_operator[key])
        sanitized["operator_summary"] = sanitized_operator
    guidance = sanitized.get("recovery_guidance")
    if isinstance(guidance, dict):
        sanitized["recovery_guidance"] = _sanitize_operator_guidance(guidance)
    return sanitized


def _sanitize_operator_guidance(guidance: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(guidance)
    if isinstance(sanitized.get("message_zh"), str):
        sanitized["message_zh"] = _sanitize_operator_visible_text_zh(sanitized["message_zh"])
    if isinstance(sanitized.get("title_zh"), str) and not _is_known_operator_guidance_text_zh(sanitized["title_zh"]):
        sanitized["title_zh"] = "处理没有正常完成"
    next_steps = sanitized.get("next_steps_zh")
    if isinstance(next_steps, list):
        sanitized["next_steps_zh"] = [
            _sanitize_operator_visible_text_zh(step) if isinstance(step, str) else "其他异常：本批次没有正常启动，请交管理员处理。"
            for step in next_steps
        ]
    return sanitized


def _sanitize_operator_visible_text_zh(text: str) -> str:
    return text if _is_known_operator_guidance_text_zh(text) else sanitize_operator_error_zh(text)


def _is_known_operator_guidance_text_zh(text: str) -> bool:
    if not text.strip():
        return False
    if any(pattern.search(text) for pattern in _PRIVATE_OR_TECHNICAL_ERROR_PATTERNS):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _is_known_operator_safe_message_zh(text: str) -> bool:
    if text not in _OPERATOR_SAFE_ERROR_MESSAGES_ZH:
        return False
    return not any(pattern.search(text) for pattern in _PRIVATE_OR_TECHNICAL_ERROR_PATTERNS)


def _write_maintenance_error(metadata_dir: Path | None, exc: BaseException) -> None:
    if metadata_dir is None:
        return
    try:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        safe_message = sanitize_operator_error_zh(exc)
        record = {
            "schema_version": "scan-qc.local-workbench-maintenance-error.v1",
            "privacy": "default metadata-safe; raw exception text and traceback omitted",
            "error_type": type(exc).__name__,
            "operator_message_zh": safe_message,
            "category": _maintenance_error_category(safe_message),
            "traceback_frame_count": len(traceback.extract_tb(exc.__traceback__)) if exc.__traceback__ else 0,
            "admin_note_zh": "默认本机状态文件不保存原始异常、路径、文件名、哈希、OCR文本或堆栈内容。",
        }
        path = metadata_dir / MAINTENANCE_ERROR_LOG_JSONL
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def _maintenance_error_category(safe_message_zh: str) -> str:
    if safe_message_zh.startswith("文件夹无法读取"):
        return "input_folder_unreadable"
    if safe_message_zh.startswith("输出文件夹无法写入"):
        return "output_folder_unwritable"
    if safe_message_zh.startswith("图片无法打开"):
        return "image_unopenable"
    return "startup_or_processing_failed"


def _restored_batch_status(
    *,
    configured: bool,
    running: bool,
    summary: dict[str, Any] | None,
    progress: dict[str, Any] | None,
    queue: dict[str, Any] | None,
    draft_decisions: dict[str, Any] | None,
    final_decisions: dict[str, Any] | None,
    final_verification: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not configured:
        return None
    counts = summary.get("counts") if isinstance(summary, dict) and isinstance(summary.get("counts"), dict) else {}
    queue_summary = queue.get("summary") if isinstance(queue, dict) and isinstance(queue.get("summary"), dict) else {}
    decision_summary = (
        final_verification.get("decision_summary")
        if isinstance(final_verification, dict) and isinstance(final_verification.get("decision_summary"), dict)
        else {}
    )
    draft_counts = _review_completion_counts(draft_decisions)
    final_counts = _review_completion_counts(final_decisions)
    review_total = max(
        _safe_nonnegative_int(queue_summary.get("total_items")),
        _safe_nonnegative_int(decision_summary.get("total_decisions")),
        final_counts["total"],
        draft_counts["total"],
    )
    reviewed_items = max(final_counts["reviewed"], draft_counts["reviewed"])
    pending_items = max(0, review_total - reviewed_items)
    total_files = _safe_nonnegative_int(counts.get("total_files"))
    derivative_images_ready = _safe_nonnegative_int(counts.get("processed_files")) + _safe_nonnegative_int(
        counts.get("resumed_files")
    )
    failed_files = _safe_nonnegative_int(counts.get("failed_files"))
    retryable_files = _safe_nonnegative_int(counts.get("retry_list_files"))
    openable_files = _safe_nonnegative_int(counts.get("openable_files"))
    status = str(summary.get("status") or "").strip().lower() if isinstance(summary, dict) else ""
    progress_state = str(progress.get("state") or "").strip().lower() if isinstance(progress, dict) else ""
    local_batch_state = str(summary.get("local_batch_state") or "").strip().lower() if isinstance(summary, dict) else ""
    summary_finished_without_operator_work = (
        status in {"finished", "completed"}
        and total_files > 0
        and openable_files > 0
        and local_batch_state not in {"empty_input_folder", "no_supported_images", "processing_blocked"}
    )
    has_final_completion = (
        final_counts["complete"]
        or str(decision_summary.get("completion_status") or "").strip().lower() == "complete"
        or summary_finished_without_operator_work
    )
    base = {
        "schema_version": "scan-qc.local-restored-batch.v1",
        "aggregate_only": True,
        "restored": True,
        "total_files": total_files,
        "derivative_images_ready": derivative_images_ready,
        "failed_files": failed_files,
        "retryable_files": retryable_files,
        "total_review_items": review_total,
        "reviewed_items": min(reviewed_items, review_total) if review_total else reviewed_items,
        "pending_items": pending_items,
        "can_open_output_folder": False,
        "can_prepare_next_batch": False,
        "can_retry": False,
        "auto_started": False,
    }
    if has_final_completion and failed_files == 0:
        handoff_consistency = _completion_handoff_consistency(
            summary=summary,
            progress=progress,
            queue=queue,
            final_decisions=final_decisions,
            final_verification=final_verification,
            decision_summary=decision_summary,
        )
        if handoff_consistency.get("consistent") is not True:
            guidance = _handoff_count_mismatch_guidance(handoff_consistency)
            return {
                **base,
                **guidance,
                "can_prepare_next_batch": True,
            }
        return {
            **base,
            "kind": "completed",
            "title_zh": "已恢复本批完成状态",
            "message_zh": "本机状态文件显示本批已完成，可以继续完成交接、打开输出文件夹或准备下一批。",
            "can_open_output_folder": True,
            "can_prepare_next_batch": True,
            "next_steps_zh": [
                "打开输出文件夹，检查处理后图片数量和画面状态。",
                "确认交接说明和复核结果已经保存。",
                "需要继续加工时，点击准备下一批；当前复核队列会清空。",
            ],
        }
    if status == "needs_review" or review_total > 0:
        return {
            **base,
            "kind": "needs_review",
            "title_zh": "已恢复本批待确认状态",
            "message_zh": "本机状态文件显示本批还有图片需要看图确认，已恢复待确认数量和已保存决定。",
            "next_steps_zh": [
                "继续逐张查看待确认图片，并保存决定。",
                "全部确认后再完成本批交接。",
                "如果文件夹位置不对，请重新保存文件夹或准备下一批。",
            ],
        }
    if failed_files or status == "blocked":
        return {
            **base,
            "kind": "interrupted_or_blocked",
            "title_zh": "已恢复本批中断状态",
            "message_zh": "本机状态文件显示上次处理没有完整完成，请检查文件夹后再决定是否重试本批。",
            "can_retry": retryable_files > 0,
            "can_prepare_next_batch": True,
            "next_steps_zh": [
                "检查扫描原图文件夹和输出文件夹是否选对。",
                "确认输出磁盘空间足够，原图图片可以正常打开。",
                "可以重新开始、重新保存文件夹或准备下一批。",
            ],
        }
    if progress_state == "running" and not running:
        return {
            **base,
            "kind": "interrupted_or_blocked",
            "title_zh": "已恢复上次中断状态",
            "message_zh": "本机状态文件显示上次处理可能中断，没有自动继续处理，也不会误报完成。",
            "can_prepare_next_batch": True,
            "next_steps_zh": [
                "确认扫描原图文件夹和输出文件夹仍然正确。",
                "需要继续本批时可以重新开始处理。",
                "如果这不是当前批次，请准备下一批后重新选择文件夹。",
            ],
        }
    if isinstance(summary, dict) or isinstance(progress, dict):
        return {
            **base,
            "kind": "incomplete_or_unknown",
            "title_zh": "已恢复本批未完成状态",
            "message_zh": "本机状态文件不完整，当前不会自动开始处理，也不会把本批显示为已完成。",
            "can_prepare_next_batch": True,
            "next_steps_zh": [
                "可以重新开始处理当前批次。",
                "也可以重新保存文件夹，确认本机状态文件夹是否选对。",
                "如果要处理新批次，请准备下一批后重新选择文件夹。",
            ],
        }
    return None


def _completion_handoff_consistency(
    *,
    summary: dict[str, Any] | None,
    progress: dict[str, Any] | None,
    queue: dict[str, Any] | None,
    final_decisions: dict[str, Any] | None,
    final_verification: dict[str, Any] | None,
    decision_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    counts = summary.get("counts") if isinstance(summary, dict) and isinstance(summary.get("counts"), dict) else {}
    operator = (
        summary.get("operator_summary")
        if isinstance(summary, dict) and isinstance(summary.get("operator_summary"), dict)
        else {}
    )
    queue_summary = queue.get("summary") if isinstance(queue, dict) and isinstance(queue.get("summary"), dict) else {}
    verification_summary = (
        final_verification.get("decision_summary")
        if isinstance(final_verification, dict) and isinstance(final_verification.get("decision_summary"), dict)
        else {}
    )
    decision_summary = decision_summary if isinstance(decision_summary, dict) else verification_summary
    decision_counts = (
        decision_summary.get("decision_counts")
        if isinstance(decision_summary, dict) and isinstance(decision_summary.get("decision_counts"), dict)
        else {}
    )
    final_counts = _review_completion_counts(final_decisions)
    issues: list[str] = []

    status = str(summary.get("status") or "").strip().lower() if isinstance(summary, dict) else ""
    local_batch_state = str(summary.get("local_batch_state") or "").strip().lower() if isinstance(summary, dict) else ""
    progress_state = str(progress.get("state") or "").strip().lower() if isinstance(progress, dict) else ""
    progress_status = str(progress.get("status") or "").strip().lower() if isinstance(progress, dict) else ""
    if status == "blocked":
        issues.append("本机摘要显示仍有失败或阻断，不能交接。")
    if status in {"running", "processing"} or local_batch_state in {"running", "processing"}:
        issues.append("本机摘要显示本批仍在处理中，请等待处理完成后再完成导出。")
    if progress_state in {"running", "processing"} or progress_status in {"running", "processing"}:
        issues.append("本机进度仍显示处理中，请等待处理完成后再完成导出。")

    total_files = _optional_nonnegative_int(counts, "total_files")
    openable_files = _optional_nonnegative_int(counts, "openable_files")
    processed_files = _optional_nonnegative_int(counts, "processed_files")
    resumed_files = _optional_nonnegative_int(counts, "resumed_files")
    skipped_files = _optional_nonnegative_int(counts, "skipped_files")
    failed_files = _optional_nonnegative_int(counts, "failed_files")
    retry_list_files = _optional_nonnegative_int(counts, "retry_list_files")
    operator_total = _optional_nonnegative_int(operator, "total_source_images")
    operator_ready = _optional_nonnegative_int(operator, "derivative_images_ready")
    operator_attention = _optional_nonnegative_int(operator, "files_needing_attention")

    if failed_files and failed_files > 0:
        issues.append("仍有处理失败图片，请检查输出文件夹后重新开始处理或交管理员确认。")
    if retry_list_files and retry_list_files > 0:
        issues.append("仍有可重试图片，请重新开始处理本批。")
    if total_files is not None and operator_total is not None and total_files != operator_total:
        issues.append("扫描原图总数与完成摘要不一致，请重新保存文件夹后再确认。")
    if total_files is not None and openable_files is not None and openable_files > total_files:
        issues.append("可处理原图数量大于原图总数，请重新保存文件夹后再确认。")
    if None not in (total_files, processed_files, resumed_files, skipped_files, failed_files):
        accounted = int(processed_files or 0) + int(resumed_files or 0) + int(skipped_files or 0) + int(failed_files or 0)
        if accounted != total_files:
            issues.append("原图总数与已处理、跳过、失败数量不一致，请重新开始处理。")
    derivative_from_counts = None
    if processed_files is not None or resumed_files is not None:
        derivative_from_counts = int(processed_files or 0) + int(resumed_files or 0)
    if derivative_from_counts is not None and operator_ready is not None and derivative_from_counts != operator_ready:
        issues.append("处理后图片数量与完成摘要不一致，请检查输出文件夹后再完成。")
    processable_files = openable_files if openable_files is not None else total_files
    if processable_files is not None and derivative_from_counts is not None:
        aggregate_handoff_exceptions = (
            _safe_nonnegative_int(decision_counts.get("needs_rescan"))
            + _safe_nonnegative_int(decision_counts.get("fixed_externally"))
            + _safe_nonnegative_int(decision_counts.get("blocked"))
        )
        expected_outputs = max(
            0,
            int(processable_files) - int(skipped_files or 0) - int(failed_files or 0) - aggregate_handoff_exceptions,
        )
        if derivative_from_counts < expected_outputs:
            issues.append("处理后图片数量少于可处理原图数量，请重新开始处理本批或检查输出文件夹后再完成。")
    if (
        operator_attention is not None
        and failed_files is not None
        and _optional_nonnegative_int(counts, "p0_findings") is not None
        and operator_attention != int(failed_files or 0) + int(_optional_nonnegative_int(counts, "p0_findings") or 0)
    ):
        issues.append("待人工确认数量与本机摘要不一致，请重新确认待看图项目。")

    review_totals = [
        value
        for value in (
            _optional_nonnegative_int(queue_summary, "total_items"),
            _optional_nonnegative_int(decision_summary, "total_decisions"),
            final_counts["total"] if final_counts["total"] else None,
        )
        if value is not None
    ]
    if len(set(review_totals)) > 1:
        issues.append("待人工确认数量与复核记录不一致，请重新确认待看图项目。")
    decision_total = _optional_nonnegative_int(decision_summary, "total_decisions")
    decision_pending = _optional_nonnegative_int(decision_summary, "pending")
    if decision_total is not None and decision_pending is not None:
        reviewed = max(0, decision_total - decision_pending)
        if reviewed + decision_pending != decision_total:
            issues.append("复核完成数量与待确认数量不一致，请重新确认待看图项目。")
    if decision_pending and decision_pending > 0:
        issues.append("仍有待人工确认图片，请先完成复核决定。")
    if final_counts["pending"] > 0:
        issues.append("已保存复核决定仍有待处理图片，请重新确认待看图项目。")
    if final_counts["total"] and final_counts["reviewed"] + final_counts["pending"] != final_counts["total"]:
        issues.append("已保存复核数量不一致，请重新确认待看图项目。")

    return {
        "schema_version": "scan-qc.local-handoff-consistency.v1",
        "aggregate_only": True,
        "consistent": not issues,
        "issue_count": len(issues),
        "issues_zh": _dedupe_preserve_order(issues),
    }


def _handoff_count_mismatch_guidance(consistency: dict[str, Any]) -> dict[str, Any]:
    issues = consistency.get("issues_zh") if isinstance(consistency, dict) else None
    safe_issues = [issue for issue in issues if isinstance(issue, str) and _is_known_operator_guidance_text_zh(issue)] if isinstance(issues, list) else []
    return {
        "kind": "handoff_count_mismatch",
        "title_zh": "交接前数量需要确认",
        "message_zh": "本机状态数量互相不一致，当前不能显示为可以交接。请检查输出文件夹，必要时重新开始处理本批。",
        "aggregate_only": True,
        "count_mismatch_detected": True,
        "issues_zh": safe_issues,
        "next_steps_zh": [
            "请先检查输出文件夹中的处理后图片数量。",
            "如果仍有待确认图片，请重新确认待看图项目。",
            "如本批处理被中断或文件夹被改动，请重新开始处理本批。",
            "确认无误后再完成本批；也可以准备下一批重新选择文件夹。",
        ],
    }


def _optional_nonnegative_int(container: dict[str, Any], key: str) -> int | None:
    if not isinstance(container, dict) or key not in container or container.get(key) is None:
        return None
    return _safe_nonnegative_int(container.get(key))


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _review_completion_counts(decisions: dict[str, Any] | None) -> dict[str, Any]:
    completion = (
        decisions.get("aggregate_counts", {}).get("review_completion")
        if isinstance(decisions, dict) and isinstance(decisions.get("aggregate_counts"), dict)
        else None
    )
    if isinstance(completion, dict):
        total = _safe_nonnegative_int(completion.get("total"))
        reviewed = _safe_nonnegative_int(completion.get("reviewed"))
        pending = _safe_nonnegative_int(completion.get("pending"))
        return {"total": total, "reviewed": reviewed, "pending": pending, "complete": completion.get("complete") is True}
    rows = decisions.get("decisions") if isinstance(decisions, dict) else None
    if not isinstance(rows, list):
        return {"total": 0, "reviewed": 0, "pending": 0, "complete": False}
    total = len([row for row in rows if isinstance(row, dict)])
    pending = len(
        [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("decision") or "").strip().lower() in {"", "pending"}
        ]
    )
    return {"total": total, "reviewed": max(0, total - pending), "pending": pending, "complete": total > 0 and pending == 0}


def _restored_completion_panel(
    restored_batch: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    final_decisions: dict[str, Any] | None,
    processing_mode: str,
) -> dict[str, Any] | None:
    if not isinstance(restored_batch, dict) or restored_batch.get("kind") != "completed":
        return None
    counts = summary.get("counts") if isinstance(summary, dict) and isinstance(summary.get("counts"), dict) else {}
    reuse_handoff_summary = _local_reuse_handoff_summary(summary)
    restored_rework_counts = _restored_rework_counts(final_decisions)
    keep_original_images = _restored_keep_original_count(final_decisions)
    panel = {
        "schema_version": "scan-qc.local-restored-completion-panel.v1",
        "aggregate_only": True,
        "title_zh": "已恢复本批完成状态",
        "message_zh": "处理后图片已准备好。请检查输出文件夹后再交接。",
        "completion_status_zh": "本批已完成",
        "manual_work_zh": "没有待人工处理图片" if restored_batch.get("pending_items") == 0 else "仍有待人工处理图片",
        "admin_handoff_zh": "不需要" if _safe_nonnegative_int(counts.get("failed_files")) == 0 else "需要管理员确认",
        "total_review_items": _safe_nonnegative_int(restored_batch.get("total_review_items")),
        "reviewed_items": _safe_nonnegative_int(restored_batch.get("reviewed_items")),
        "pending_items": _safe_nonnegative_int(restored_batch.get("pending_items")),
        "processed_output_images": _safe_nonnegative_int(restored_batch.get("derivative_images_ready")),
        "needs_rescan_images": restored_rework_counts["needs_rescan_images"],
        "needs_reprocess_images": restored_rework_counts["needs_reprocess_images"],
        "keep_original_images": keep_original_images,
        "processing_mode": _processing_mode_payload(processing_mode),
        "open_output_folder_available": True,
        "next_steps_zh": [
            f"打开输出文件夹，检查 {_safe_nonnegative_int(restored_batch.get('derivative_images_ready'))} 张处理后图片的数量和画面状态。",
            (
                f"需要重扫 {restored_rework_counts['needs_rescan_images']} 张；"
                f"需要重新处理 {restored_rework_counts['needs_reprocess_images']} 张。"
            ),
            f"确认保留原貌 {keep_original_images} 张。",
            "本机状态文件夹已保存本批处理状态，正常界面不显示具体路径或文件名。",
            "需要继续加工时，点击准备下一批；当前复核队列会清空。",
            "如果仍有异常或不能交接，请交管理员处理。",
        ],
    }
    if reuse_handoff_summary is not None:
        panel["local_reuse_summary"] = reuse_handoff_summary
    return panel


def _restored_keep_original_count(final_decisions: dict[str, Any] | None) -> int:
    if not isinstance(final_decisions, dict):
        return 0
    review_counts = final_decisions.get("review_counts")
    if isinstance(review_counts, dict):
        keep_original = _optional_nonnegative_int(review_counts, "keep_original_trace")
        if keep_original is not None:
            return keep_original
    completion_counts = (
        final_decisions.get("aggregate_counts", {}).get("review_completion", {}).get("counts")
        if isinstance(final_decisions.get("aggregate_counts"), dict)
        else None
    )
    if isinstance(completion_counts, dict):
        keep_original = _optional_nonnegative_int(completion_counts, "keep_original_trace")
        if keep_original is not None:
            return keep_original
    decisions = final_decisions.get("decisions")
    if isinstance(decisions, list):
        return sum(
            1
            for item in decisions
            if isinstance(item, dict) and str(item.get("decision") or "").strip().lower() == "keep_original_trace"
        )
    return 0


def _restored_rework_counts(final_decisions: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(final_decisions, dict):
        return {"needs_rescan_images": 0, "needs_reprocess_images": 0}

    def _count_from_bucket(bucket: dict[str, Any] | None) -> tuple[int | None, int | None]:
        if not isinstance(bucket, dict):
            return None, None
        needs_rescan = _optional_nonnegative_int(bucket, "needs_rescan")
        needs_reprocess = _optional_nonnegative_int(bucket, "needs_reprocess")
        if needs_reprocess is None:
            needs_reprocess = _optional_nonnegative_int(bucket, "fixed_externally")
        return needs_rescan, needs_reprocess

    for bucket in (
        final_decisions.get("aggregate_counts", {}).get("review_completion", {}).get("counts")
        if isinstance(final_decisions.get("aggregate_counts"), dict)
        else None,
        final_decisions.get("review_counts"),
    ):
        needs_rescan, needs_reprocess = _count_from_bucket(bucket)
        if needs_rescan is not None or needs_reprocess is not None:
            return {
                "needs_rescan_images": needs_rescan or 0,
                "needs_reprocess_images": needs_reprocess or 0,
            }

    decisions = final_decisions.get("decisions")
    if isinstance(decisions, list):
        needs_rescan_images = sum(
            1
            for item in decisions
            if isinstance(item, dict) and str(item.get("decision") or "").strip().lower() in {"needs_rescan", "rescan"}
        )
        needs_reprocess_images = sum(
            1
            for item in decisions
            if isinstance(item, dict)
            and str(item.get("decision") or "").strip().lower() in {"needs_reprocess", "fixed_externally", "reprocess"}
        )
        return {
            "needs_rescan_images": needs_rescan_images,
            "needs_reprocess_images": needs_reprocess_images,
        }

    return {"needs_rescan_images": 0, "needs_reprocess_images": 0}


def _path_is_existing_dir(path: Path) -> bool:
    try:
        return path.exists() and path.is_dir()
    except OSError:
        return False


def _folder_can_be_listed(path: Path) -> bool:
    if not os.access(path, os.R_OK | os.X_OK):
        return False
    try:
        next(path.iterdir(), None)
    except StopIteration:
        return True
    except OSError:
        return False
    return True


def _preflight_folder_guidance(input_dir: Path, derivatives_dir: Path, metadata_dir: Path) -> dict[str, Any] | None:
    try:
        input_path = _safe_resolve_path(input_dir)
        output_path = _safe_resolve_path(derivatives_dir)
        metadata_path = _safe_resolve_path(metadata_dir)
    except ValueError:
        return _folder_preflight_guidance(
            "folder_path_unreadable",
            "文件夹位置不能读取",
            "有文件夹位置当前不能读取，处理没有启动。",
            [
                "确认移动硬盘或共享盘已经连接到本机。",
                "重新选择可以打开的扫描原图、输出和本机状态文件夹。",
                "保存文件夹后再点击开始处理。",
            ],
        )
    unsafe_guidance = _unsafe_folder_choice_guidance(input_path, output_path, metadata_path)
    if unsafe_guidance is not None:
        return unsafe_guidance
    if not _path_is_existing_dir(input_path):
        return _folder_preflight_guidance(
            "input_folder_missing",
            "找不到扫描原图文件夹",
            "扫描原图文件夹不存在或已经被移动，处理没有启动。",
            [
                "重新选择本批次的扫描原图文件夹。",
                "确认移动硬盘或共享盘已经连接到本机。",
                "保存文件夹后再点击开始处理。",
            ],
        )
    if not os.access(input_path, os.R_OK | os.X_OK):
        return _folder_preflight_guidance(
            "input_folder_unreadable",
            "原图文件夹不能读取",
            "扫描原图文件夹现在不能读取，处理没有启动。",
            [
                "确认移动硬盘或共享盘已经连接，并且当前电脑有读取权限。",
                "重新选择可以打开的扫描原图文件夹。",
                "保存文件夹后再点击开始处理。",
            ],
        )
    try:
        input_is_empty = not any(input_path.iterdir())
    except OSError:
        return _folder_preflight_guidance(
            "input_folder_unreadable",
            "原图文件夹不能读取",
            "扫描原图文件夹现在不能读取，处理没有启动。",
            [
                "确认移动硬盘或共享盘已经连接，并且当前电脑有读取权限。",
                "重新选择可以打开的扫描原图文件夹。",
                "保存文件夹后再点击开始处理。",
            ],
        )
    if input_is_empty:
        return _folder_preflight_guidance(
            "input_folder_empty",
            "原图文件夹是空的",
            "扫描原图文件夹里没有文件，处理没有启动。",
            [
                "确认是否选到了本批次真正的扫描原图文件夹。",
                "如果还没有扫描图片，请先完成扫描或把图片放入原图文件夹。",
                "放好图片后，重新保存文件夹并开始处理。",
            ],
        )
    supported_image_count = _supported_image_count(input_path)
    if supported_image_count == 0:
        return _folder_preflight_guidance(
            "no_supported_images",
            "没有可处理的图片",
            "扫描原图文件夹里没有找到当前支持处理的图片，处理没有启动。",
            [
                "确认选对了本批次的扫描原图文件夹。",
                "确认原图是常见图片格式，并且能用本机图片查看器打开。",
                "如果文件格式不对，请重新导出为支持的图片格式后再处理。",
            ],
            input_empty=False,
            supported_image_count=0,
        )
    if not _path_is_existing_dir(output_path):
        return _folder_preflight_guidance(
            "output_folder_unusable",
            "输出文件夹不能使用",
            "处理后输出文件夹不存在或已经被移动，处理没有启动。",
            [
                "重新选择一个已经存在、可以写入的处理后输出文件夹。",
                "确认移动硬盘或输出磁盘已经连接到本机。",
                "保存文件夹后再点击开始处理。",
            ],
        )
    if not _folder_is_writable(output_path) or not _folder_is_writable(metadata_path):
        return _folder_preflight_guidance(
            "output_folder_unwritable",
            "输出文件夹不能写入",
            "处理后输出文件夹或本机状态文件夹不能写入，处理没有启动。",
            [
                "确认输出磁盘没有只读、已解锁，并且空间足够。",
                "换一个可以写入的处理后输出文件夹。",
                "保存文件夹后再点击开始处理。",
            ],
        )
    return None


def _unsafe_folder_choice_guidance(input_path: Path, output_path: Path, metadata_path: Path) -> dict[str, Any] | None:
    if input_path == output_path or _is_relative_to(output_path, input_path):
        return _folder_preflight_guidance(
            "unsafe_folder_choice",
            "原图和输出文件夹不能混在一起",
            "处理后输出文件夹不能和扫描原图文件夹相同，也不能放在原图文件夹里面。",
            [
                "为处理后图片选择单独的输出文件夹。",
                "不要把输出文件夹放进扫描原图文件夹。",
                "重新保存文件夹后再开始处理。",
            ],
        )
    if _is_relative_to(input_path, output_path):
        return _folder_preflight_guidance(
            "unsafe_source_inside_output",
            "原图和输出文件夹不能混在一起",
            "扫描原图文件夹不能放在处理后输出文件夹里面。",
            [
                "为本批扫描原图和处理后图片选择两个互相独立的文件夹。",
                "不要把扫描原图文件夹放进输出文件夹。",
                "重新保存文件夹后再开始处理。",
            ],
        )
    if _is_relative_to(metadata_path, input_path):
        return _folder_preflight_guidance(
            "unsafe_metadata_folder",
            "本机状态文件夹位置不安全",
            "本机状态文件夹不能放在扫描原图文件夹里面，处理没有启动。",
            [
                "使用默认状态文件夹，或选择输出文件夹里的状态文件夹。",
                "不要把状态文件夹放进扫描原图文件夹。",
                "重新保存文件夹后再开始处理。",
            ],
        )
    return None


def _folder_preflight_guidance(
    kind: str,
    title_zh: str,
    message_zh: str,
    next_steps_zh: list[str],
    *,
    input_empty: bool | None = None,
    supported_image_count: int | None = None,
    output_writable: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "scan-qc.local-folder-preflight.v1",
        "aggregate_only": True,
        "kind": kind,
        "title_zh": title_zh,
        "message_zh": message_zh,
        "next_steps_zh": next_steps_zh,
        "ready_to_start": False,
        "can_start_processing": False,
        "blocking_reasons_zh": [message_zh],
        "failed_files": 0,
        "retryable_files": 0,
        "derivative_images_ready": 0,
        "total_files": 0,
    }
    if input_empty is not None:
        payload["input_empty"] = input_empty
    if supported_image_count is not None:
        payload["supported_image_count"] = max(0, supported_image_count)
    if output_writable is not None:
        payload["output_writable"] = output_writable
    return payload


def _folder_readiness_summary(
    input_dir: Path | None,
    derivatives_dir: Path | None,
    metadata_dir: Path | None,
    processing_mode: str,
) -> dict[str, Any]:
    summary, _snapshot = _folder_readiness_summary_with_snapshot(
        input_dir,
        derivatives_dir,
        metadata_dir,
        processing_mode,
    )
    return summary


def _folder_readiness_summary_with_snapshot(
    input_dir: Path | None,
    derivatives_dir: Path | None,
    metadata_dir: Path | None,
    processing_mode: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    selected_mode = _normalize_processing_mode(processing_mode)
    mode_payload = _processing_mode_payload(selected_mode)
    base: dict[str, Any] = {
        "schema_version": "scan-qc.local-folder-readiness.v1",
        "aggregate_only": True,
        "selected_processing_mode": mode_payload,
        "supported_image_count": 0,
        "input_empty": True,
        "output_writable": False,
        "ready_to_start": False,
        "can_start_processing": False,
        "blocking_reasons_zh": [],
    }
    if input_dir is None or derivatives_dir is None or metadata_dir is None:
        return {
            **base,
            "status": "not_configured",
            "title_zh": "文件夹还没有保存",
            "message_zh": "请先保存扫描原图文件夹和处理后输出文件夹。",
            "next_steps_zh": ["填写两个文件夹位置。", "保存文件夹后查看准备情况。"],
        }, None
    try:
        input_path = _safe_resolve_path(input_dir)
        output_path = _safe_resolve_path(derivatives_dir)
        metadata_path = _safe_resolve_path(metadata_dir)
    except ValueError:
        return {
            **base,
            "status": "blocked",
            "title_zh": "文件夹位置不能读取",
            "message_zh": "有文件夹位置当前不能读取。",
            "next_steps_zh": ["重新选择本机可以打开的文件夹。", "保存文件夹后再查看准备情况。"],
        }, None
    unsafe_guidance = _unsafe_folder_choice_guidance(input_path, output_path, metadata_path)
    if unsafe_guidance is not None:
        return {
            **base,
            "status": "blocked",
            "title_zh": unsafe_guidance["title_zh"],
            "message_zh": unsafe_guidance["message_zh"],
            "blocking_reasons_zh": list(unsafe_guidance.get("blocking_reasons_zh") or [unsafe_guidance["message_zh"]]),
            "next_steps_zh": unsafe_guidance["next_steps_zh"],
        }, None
    input_exists = _path_is_existing_dir(input_path)
    input_readable = input_exists and os.access(input_path, os.R_OK | os.X_OK)
    input_empty = True
    supported_image_count = 0
    snapshot: dict[str, Any] | None = None
    if input_readable:
        try:
            scan_snapshot = _scan_input_folder_preflight(input_path)
            input_empty = bool(scan_snapshot["input_empty"])
            supported_image_count = _safe_nonnegative_int(scan_snapshot["supported_image_count"])
            snapshot = _preflight_snapshot_payload(
                input_path,
                output_path,
                metadata_path,
                selected_mode,
                input_empty=input_empty,
                supported_image_count=supported_image_count,
                input_snapshot=scan_snapshot,
            )
        except OSError:
            input_readable = False
    output_writable = _folder_is_writable(output_path) and _folder_is_writable(metadata_path)
    if snapshot is not None:
        snapshot["output_writable"] = output_writable
    summary = {
        **base,
        "input_empty": input_empty,
        "supported_image_count": supported_image_count,
        "output_writable": output_writable,
    }
    if not input_exists or not input_readable:
        return {
            **summary,
            "status": "blocked",
            "title_zh": "原图文件夹不能使用",
            "message_zh": "扫描原图文件夹不存在，或当前电脑不能读取。",
            "next_steps_zh": ["重新选择本批次的扫描原图文件夹。", "保存文件夹后再查看准备情况。"],
        }, None
    if input_empty:
        return {
            **summary,
            "status": "empty",
            "title_zh": "原图文件夹是空的",
            "message_zh": "这个扫描原图文件夹里没有发现可处理文件。",
            "next_steps_zh": ["确认是否选到了本批次真正的扫描原图文件夹。", "放好图片后，重新保存文件夹。"],
        }, snapshot
    if supported_image_count == 0:
        return {
            **summary,
            "status": "unsupported",
            "title_zh": "没有可处理的图片",
            "message_zh": "文件夹里没有找到当前支持处理的图片。",
            "next_steps_zh": ["确认原图是常见图片格式。", "如果格式不对，请重新导出为支持的图片格式后再处理。"],
        }, snapshot
    if not output_writable:
        message_zh = "处理后输出文件夹或本机状态文件夹不能写入。"
        return {
            **summary,
            "status": "blocked",
            "title_zh": "输出文件夹不能写入",
            "message_zh": message_zh,
            "blocking_reasons_zh": [message_zh],
            "next_steps_zh": ["确认输出磁盘没有只读、已解锁，并且空间足够。", "换一个可以写入的输出文件夹后重新保存。"],
        }, snapshot
    processing_precheck = _aggregate_processing_precheck_from_snapshot(
        input_path,
        output_path,
        selected_mode,
        snapshot,
    )
    output_space_check = _aggregate_output_space_check(
        output_path,
        snapshot=snapshot,
        processing_precheck=processing_precheck,
    )
    summary["output_space_check"] = output_space_check
    summary["available_bytes"] = output_space_check.get("available_bytes")
    summary["estimated_required_bytes"] = output_space_check.get("estimated_required_bytes")
    if output_space_check.get("status") == "blocked":
        message_zh = str(output_space_check.get("message_zh") or "输出磁盘空间明显不足，当前不能开始处理。")
        return {
            **summary,
            "status": "blocked",
            "title_zh": "输出磁盘空间不足",
            "message_zh": message_zh,
            "blocking_reasons_zh": [message_zh],
            "next_steps_zh": list(output_space_check.get("next_steps_zh") or ["更换可写且空间更充足的输出文件夹后再开始处理。"]),
            "preflight_processing_summary": processing_precheck,
        }, snapshot
    existing_output_risk = _existing_output_artifact_risk(output_path, metadata_path, processing_precheck)
    ready_title = "文件夹可以开始处理"
    ready_message = (
        f"本批预检结果：已识别到 {supported_image_count} 张可处理图片，输出文件夹可以写入；"
        "未发现已有工作台结果，可以开始。"
    )
    ready_steps = ["确认处理方式无误。", "点击开始处理。"]
    if existing_output_risk.get("kind") == "reusable_current_batch":
        ready_title = existing_output_risk["title_zh"]
        ready_message = existing_output_risk["message_zh"]
        ready_steps = list(existing_output_risk["next_steps_zh"])
    elif existing_output_risk.get("kind") != "none":
        ready_title = existing_output_risk["title_zh"]
        ready_message = existing_output_risk["message_zh"]
        ready_steps = list(existing_output_risk["next_steps_zh"])
    ready_summary = {
        **summary,
        "status": "ready",
        "ready_to_start": True,
        "can_start_processing": True,
        "blocking_reasons_zh": [],
        "title_zh": ready_title,
        "message_zh": ready_message,
        "next_steps_zh": ready_steps,
        "existing_output_risk": existing_output_risk,
        "preflight_processing_summary": processing_precheck,
    }
    if output_space_check.get("status") == "warning":
        ready_summary["message_zh"] = f"{ready_summary['message_zh']} {output_space_check.get('message_zh') or ''}".strip()
        ready_summary["next_steps_zh"] = list(dict.fromkeys([*ready_summary["next_steps_zh"], *(output_space_check.get("next_steps_zh") or [])]))
    if snapshot is not None:
        snapshot["ready_to_start"] = True
    return ready_summary, snapshot


def _aggregate_processing_precheck_from_snapshot(
    input_path: Path,
    derivatives_dir: Path,
    processing_mode: str,
    snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    records = snapshot.get("supported_file_snapshots") if isinstance(snapshot, dict) else None
    relative_paths = [
        str(record.get("relative_path"))
        for record in records or []
        if isinstance(record, dict) and isinstance(record.get("relative_path"), str) and record.get("relative_path") != "."
    ]
    options = PROCESSING_MODE_OPTIONS[_normalize_processing_mode(processing_mode)]
    return aggregate_processing_reuse_precheck(
        input_path,
        derivatives_dir,
        relative_paths,
        ProcessingOptions(
            auto_crop=bool(options["auto_crop"]),
            deskew=bool(options["deskew"]),
            trim_dark_border=bool(options["trim_dark_border"]),
            despeckle=bool(options["despeckle"]),
            despeckle_backend="fallback",
            resume_processing=True,
            reuse_scan_measurements=True,
        ),
    )


def _aggregate_output_space_check(
    output_dir: Path,
    *,
    snapshot: dict[str, Any] | None,
    processing_precheck: dict[str, Any] | None,
) -> dict[str, Any]:
    available_bytes: int | None = None
    try:
        available_bytes = max(0, int(shutil.disk_usage(output_dir).free))
    except OSError:
        available_bytes = None

    records = snapshot.get("supported_file_snapshots") if isinstance(snapshot, dict) else None
    if not isinstance(records, list) or not records:
        return _output_space_check_payload(
            "warning",
            available_bytes=available_bytes,
            estimated_required_bytes=None,
            message_zh="暂时无法可靠估算本批所需空间，建议先确认输出磁盘空间后再开始处理。",
            next_steps_zh=["先确认输出磁盘剩余空间。", "如空间紧张，请先清理磁盘或更换输出文件夹。"],
        )

    source_bytes_total = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        source_bytes_total += _safe_nonnegative_int(record.get("size"))
    if source_bytes_total <= 0:
        return _output_space_check_payload(
            "warning",
            available_bytes=available_bytes,
            estimated_required_bytes=None,
            message_zh="暂时无法可靠估算本批所需空间，建议先确认输出磁盘空间后再开始处理。",
            next_steps_zh=["先确认输出磁盘剩余空间。", "如空间紧张，请先清理磁盘或更换输出文件夹。"],
        )

    supported_count = max(1, _safe_nonnegative_int(snapshot.get("supported_image_count")))
    effective_source_bytes = source_bytes_total
    if isinstance(processing_precheck, dict) and processing_precheck.get("retry_scope_safe") is True:
        needs_processing = _safe_nonnegative_int(processing_precheck.get("needs_processing_files"))
        capped_needs = min(supported_count, needs_processing)
        effective_source_bytes = int((source_bytes_total * capped_needs) / supported_count)
    estimated_required_bytes = (
        effective_source_bytes * DISK_SPACE_ESTIMATE_OVERHEAD_NUMERATOR + DISK_SPACE_ESTIMATE_OVERHEAD_DENOMINATOR - 1
    ) // DISK_SPACE_ESTIMATE_OVERHEAD_DENOMINATOR

    if available_bytes is None:
        return _output_space_check_payload(
            "warning",
            available_bytes=None,
            estimated_required_bytes=estimated_required_bytes,
            message_zh="当前无法读取输出磁盘剩余空间；建议先确认空间后再开始处理。",
            next_steps_zh=["确认输出文件夹所在磁盘可读写。", "确认磁盘剩余空间后再开始处理。"],
        )
    if estimated_required_bytes <= 0:
        return _output_space_check_payload("ok", available_bytes=available_bytes, estimated_required_bytes=0)
    if available_bytes < estimated_required_bytes:
        return _output_space_check_payload(
            "blocked",
            available_bytes=available_bytes,
            estimated_required_bytes=estimated_required_bytes,
            message_zh="输出磁盘空间明显不足，按本批聚合预估暂时不能开始处理。",
            next_steps_zh=[
                "先清理输出磁盘空间，或更换剩余空间更充足的输出文件夹。",
                "重新保存文件夹后再开始处理。",
            ],
        )
    return _output_space_check_payload("ok", available_bytes=available_bytes, estimated_required_bytes=estimated_required_bytes)


def _output_space_check_payload(
    status: str,
    *,
    available_bytes: int | None,
    estimated_required_bytes: int | None,
    message_zh: str = "",
    next_steps_zh: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.local-output-space-check.v1",
        "aggregate_only": True,
        "status": status,
        "available_bytes": available_bytes,
        "estimated_required_bytes": estimated_required_bytes,
        "message_zh": message_zh,
        "next_steps_zh": list(next_steps_zh or []),
    }


def _existing_output_artifact_risk(
    derivatives_dir: Path,
    metadata_dir: Path,
    processing_precheck: dict[str, Any] | None = None,
) -> dict[str, Any]:
    processed_outputs_detected = _processed_output_images_detected(derivatives_dir)
    metadata_summary = _read_json(metadata_dir / PRODUCTION_RUN_SUMMARY_JSON)
    progress = _read_json(metadata_dir / PRODUCTION_RUN_PROGRESS_JSON)
    workbench_metadata_detected = _workbench_metadata_detected(metadata_dir)
    completed_handoff_detected = _completed_handoff_artifacts_detected(metadata_dir)
    reusable_current_batch_detected = _reusable_current_batch_detected(metadata_summary, progress)
    precheck_reusable = (
        _safe_nonnegative_int(processing_precheck.get("reusable_files"))
        if isinstance(processing_precheck, dict) and processing_precheck.get("retry_scope_safe") is True
        else 0
    )
    precheck_needs_processing = (
        _safe_nonnegative_int(processing_precheck.get("needs_processing_files"))
        if isinstance(processing_precheck, dict) and processing_precheck.get("retry_scope_safe") is True
        else 0
    )
    if precheck_reusable > 0:
        reusable_current_batch_detected = True
    base = {
        "schema_version": "scan-qc.local-existing-output-risk.v1",
        "aggregate_only": True,
        "processed_outputs_detected": processed_outputs_detected,
        "workbench_metadata_detected": workbench_metadata_detected,
        "completed_handoff_detected": completed_handoff_detected,
        "reusable_current_batch_detected": reusable_current_batch_detected,
    }
    if completed_handoff_detected:
        return {
            **base,
            "kind": "completed_handoff",
            "severity": "high",
            "existing_artifacts_detected": True,
            "title_zh": "输出文件夹已有完成交接材料",
            "message_zh": "发现输出文件夹里已有本工具完成交接材料；建议先完成或归档上一批，或选择空输出文件夹，避免新旧批次混在一起。",
            "next_steps_zh": [
                "如果这是上一批，请先完成交接或归档后再处理新批次。",
                "如果要处理新批次，请更换一个空的输出文件夹。",
                "如果确认继续同一批，请先核对本机状态和输出数量。",
            ],
        }
    if reusable_current_batch_detected:
        message_zh = "发现本批已有可复用处理结果；可以继续本批，重新开始会只补齐缺失输出，已经成功输出的图片不会删除。"
        if precheck_reusable > 0:
            message_zh = (
                f"本批预检结果：已有 {precheck_reusable} 张可复用处理后输出，"
                f"{precheck_needs_processing} 张需要新处理或补处理；开始处理会只补齐需要处理的图片。"
            )
        return {
            **base,
            "kind": "reusable_current_batch",
            "severity": "medium",
            "existing_artifacts_detected": True,
            "title_zh": "输出文件夹已有本批可复用结果",
            "message_zh": message_zh,
            "next_steps_zh": [
                "确认当前选择的是同一批扫描原图和输出文件夹。",
                "需要继续本批时，可以开始处理，系统会复用已有结果并补齐缺失输出。",
                "如果这是新批次，请更换一个空输出文件夹。",
            ],
        }
    if workbench_metadata_detected or processed_outputs_detected:
        return {
            **base,
            "kind": "existing_workbench_results",
            "severity": "medium",
            "existing_artifacts_detected": True,
            "title_zh": "输出文件夹已有工作台结果",
            "message_zh": "发现输出文件夹里已有本工具结果；请先确认是继续本批、换一个空输出文件夹，还是先处理上一批交接。",
            "next_steps_zh": [
                "如果这是同一批，请确认后继续，系统会尽量复用已有处理结果。",
                "如果这是新批次，请选择空的输出文件夹。",
                "如果上一批还没交接，请先完成交接或归档。",
            ],
        }
    return {
        **base,
        "kind": "none",
        "severity": "none",
        "existing_artifacts_detected": False,
        "title_zh": "未发现已有结果",
        "message_zh": "未发现已有工作台结果，可以开始。",
        "next_steps_zh": ["确认处理方式无误。", "点击开始处理。"],
    }


def _processed_output_images_detected(derivatives_dir: Path) -> bool:
    image_root = derivatives_dir / "images"
    if not _path_is_existing_dir(image_root):
        return False
    try:
        return any(candidate.is_file() and candidate.suffix.lower() in PREVIEW_IMAGE_SUFFIXES for candidate in image_root.rglob("*"))
    except OSError:
        return False


def _workbench_metadata_detected(metadata_dir: Path) -> bool:
    known_names = {
        PRODUCTION_RUN_SUMMARY_JSON,
        PRODUCTION_RUN_PROGRESS_JSON,
        PRODUCTION_REVIEW_QUEUE_JSON,
        REVIEW_DECISION_DRAFT_JSON,
        REVIEW_DECISION_SUMMARY_JSON,
        REVIEW_DECISION_VERIFICATION_JSON,
        PROCESSING_REVIEW_JSON,
        COMPLETION_NOTE_TXT,
        MAINTENANCE_ERROR_LOG_JSONL,
    }
    try:
        return any((metadata_dir / name).exists() for name in known_names)
    except OSError:
        return False


def _completed_handoff_artifacts_detected(metadata_dir: Path) -> bool:
    try:
        return (metadata_dir / COMPLETION_NOTE_TXT).exists() or (metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).exists()
    except OSError:
        return False


def _reusable_current_batch_detected(summary: dict[str, Any] | None, progress: dict[str, Any] | None) -> bool:
    if not isinstance(summary, dict):
        return False
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    processed_or_resumed = _safe_nonnegative_int(counts.get("processed_files")) + _safe_nonnegative_int(counts.get("resumed_files"))
    retryable_or_failed = _safe_nonnegative_int(counts.get("retry_list_files")) + _safe_nonnegative_int(counts.get("failed_files"))
    status = str(summary.get("status") or "").strip().lower()
    progress_state = str(progress.get("state") or "").strip().lower() if isinstance(progress, dict) else ""
    return processed_or_resumed > 0 and (retryable_or_failed > 0 or status in {"blocked", "needs_review"} or progress_state == "running")


def _scan_input_folder_preflight(input_path: Path) -> dict[str, Any]:
    directory_snapshots = [_path_stat_snapshot(input_path, ".")]
    supported_file_snapshots: list[dict[str, Any]] = []
    saw_entry = False
    for candidate in input_path.rglob("*"):
        saw_entry = True
        if candidate.is_dir():
            directory_snapshots.append(_path_stat_snapshot(candidate, _snapshot_relative_path(input_path, candidate)))
        elif candidate.is_file() and candidate.suffix.lower() in PREVIEW_IMAGE_SUFFIXES:
            supported_file_snapshots.append(_path_stat_snapshot(candidate, _snapshot_relative_path(input_path, candidate)))
    return {
        "input_empty": not saw_entry,
        "supported_image_count": len(supported_file_snapshots),
        "directory_snapshots": directory_snapshots,
        "supported_file_snapshots": supported_file_snapshots,
    }


def _preflight_snapshot_payload(
    input_path: Path,
    output_path: Path,
    metadata_path: Path,
    processing_mode: str,
    *,
    input_empty: bool,
    supported_image_count: int,
    input_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": PREFLIGHT_SNAPSHOT_SCHEMA,
        "created_monotonic": time.monotonic(),
        "input_dir": str(input_path),
        "derivatives_dir": str(output_path),
        "metadata_dir": str(metadata_path),
        "processing_mode": _normalize_processing_mode(processing_mode),
        "input_empty": bool(input_empty),
        "supported_image_count": max(0, supported_image_count),
        "ready_to_start": False,
        "output_writable": False,
        "directory_snapshots": list(input_snapshot.get("directory_snapshots") or []),
        "supported_file_snapshots": list(input_snapshot.get("supported_file_snapshots") or []),
    }


def _preflight_snapshot_matches(
    snapshot: dict[str, Any] | None,
    input_dir: Path | None,
    derivatives_dir: Path | None,
    metadata_dir: Path | None,
    processing_mode: str,
) -> tuple[bool, str]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != PREFLIGHT_SNAPSHOT_SCHEMA:
        return False, "missing_preflight_snapshot"
    if input_dir is None or derivatives_dir is None or metadata_dir is None:
        return False, "not_configured"
    created_monotonic = snapshot.get("created_monotonic")
    if not isinstance(created_monotonic, (int, float)):
        return False, "incomplete_preflight_snapshot"
    if time.monotonic() - float(created_monotonic) > PREFLIGHT_SNAPSHOT_MAX_AGE_SECONDS:
        return False, "expired_preflight_snapshot"
    try:
        input_path = _safe_resolve_path(input_dir)
        output_path = _safe_resolve_path(derivatives_dir)
        metadata_path = _safe_resolve_path(metadata_dir)
    except ValueError:
        return False, "folder_path_unreadable"
    if (
        snapshot.get("input_dir") != str(input_path)
        or snapshot.get("derivatives_dir") != str(output_path)
        or snapshot.get("metadata_dir") != str(metadata_path)
        or snapshot.get("processing_mode") != _normalize_processing_mode(processing_mode)
    ):
        return False, "preflight_identity_changed"
    if _unsafe_folder_choice_guidance(input_path, output_path, metadata_path) is not None:
        return False, "unsafe_folder_choice"
    if not _path_is_existing_dir(input_path) or not os.access(input_path, os.R_OK | os.X_OK):
        return False, "input_folder_unreadable"
    if not _path_is_existing_dir(output_path):
        return False, "output_folder_unusable"
    if not _folder_is_writable(output_path) or not _folder_is_writable(metadata_path):
        return False, "output_folder_unwritable"
    directory_snapshots = snapshot.get("directory_snapshots")
    supported_file_snapshots = snapshot.get("supported_file_snapshots")
    if not isinstance(directory_snapshots, list) or not isinstance(supported_file_snapshots, list):
        return False, "incomplete_preflight_snapshot"
    if not _snapshot_records_match(input_path, directory_snapshots, expect_directory=True):
        return False, "input_folder_changed"
    if not _snapshot_records_match(input_path, supported_file_snapshots, expect_directory=False):
        return False, "input_folder_changed"
    if _safe_nonnegative_int(snapshot.get("supported_image_count")) != len(supported_file_snapshots):
        return False, "incomplete_preflight_snapshot"
    return True, "preflight_snapshot_matched"


def _snapshot_records_match(input_path: Path, records: list[Any], *, expect_directory: bool) -> bool:
    for record in records:
        if not isinstance(record, dict):
            return False
        rel = record.get("relative_path")
        if not isinstance(rel, str) or not rel:
            return False
        candidate = input_path if rel == "." else input_path / _safe_relative_path(rel)
        try:
            stat_result = candidate.stat()
        except OSError:
            return False
        if expect_directory:
            if not candidate.is_dir():
                return False
        elif not candidate.is_file() or candidate.suffix.lower() not in PREVIEW_IMAGE_SUFFIXES:
            return False
        if (
            record.get("mtime_ns") != getattr(stat_result, "st_mtime_ns", None)
            or record.get("ctime_ns") != getattr(stat_result, "st_ctime_ns", None)
            or record.get("size") != stat_result.st_size
        ):
            return False
    return True


def _path_stat_snapshot(path: Path, relative_path: str) -> dict[str, Any]:
    stat_result = path.stat()
    return {
        "relative_path": relative_path,
        "mtime_ns": getattr(stat_result, "st_mtime_ns", None),
        "ctime_ns": getattr(stat_result, "st_ctime_ns", None),
        "size": stat_result.st_size,
    }


def _snapshot_relative_path(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return relative or "."


def _preflight_reuse_summary(status: str, reason: str, supported_image_count: int) -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.local-preflight-reuse-summary.v1",
        "aggregate_only": True,
        "status": status,
        "reason": reason,
        "supported_image_count": max(0, supported_image_count),
    }


def _supported_image_count(input_dir: Path) -> int:
    count = 0
    try:
        for candidate in input_dir.rglob("*"):
            if candidate.is_file() and candidate.suffix.lower() in PREVIEW_IMAGE_SUFFIXES:
                count += 1
    except OSError:
        return 0
    return count


def _normalize_processing_mode(processing_mode: str | None) -> str:
    mode = (processing_mode or DEFAULT_PROCESSING_MODE).strip()
    if mode not in PROCESSING_MODE_OPTIONS:
        raise ValueError("处理方式不正确，请重新选择。")
    return mode


def _processing_mode_payload(processing_mode: str) -> dict[str, Any]:
    mode = _normalize_processing_mode(processing_mode)
    option = PROCESSING_MODE_OPTIONS[mode]
    return {
        "id": mode,
        "label_zh": option["label_zh"],
        "purpose_zh": option["purpose_zh"],
        "output_zh": option["output_zh"],
        "available_modes": [
            {
                "id": mode_id,
                "label_zh": values["label_zh"],
                "purpose_zh": values["purpose_zh"],
                "output_zh": values["output_zh"],
            }
            for mode_id, values in PROCESSING_MODE_OPTIONS.items()
        ],
    }


def _processing_mode_completion_label(processing_mode: str) -> str:
    mode = _processing_mode_payload(processing_mode)
    return f"{mode['label_zh']}；{mode['purpose_zh']}；{mode['output_zh']}"


def _completion_handoff_counts(
    run_summary: dict[str, Any] | None,
    decision_summary: dict[str, Any],
    review_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operator = run_summary.get("operator_summary") if isinstance(run_summary, dict) else {}
    counts = run_summary.get("counts") if isinstance(run_summary, dict) else {}
    decision_counts = decision_summary.get("decision_counts") if isinstance(decision_summary, dict) else {}
    if not isinstance(operator, dict):
        operator = {}
    if not isinstance(counts, dict):
        counts = {}
    if not isinstance(decision_counts, dict):
        decision_counts = {}
    total_source_images = _safe_nonnegative_int(operator.get("total_source_images"))
    if total_source_images == 0:
        total_source_images = _safe_nonnegative_int(counts.get("openable_files"))
    if total_source_images == 0:
        total_source_images = _safe_nonnegative_int(counts.get("total_files"))
    processed_output_images = _safe_nonnegative_int(operator.get("derivative_images_ready"))
    if processed_output_images == 0:
        processed_output_images = _safe_nonnegative_int(counts.get("processed_files")) + _safe_nonnegative_int(
            counts.get("resumed_files")
        )
    needs_rescan_images = _safe_nonnegative_int(decision_counts.get("needs_rescan"))
    needs_reprocess_images = _safe_nonnegative_int(decision_counts.get("needs_reprocess"))
    if needs_reprocess_images == 0:
        needs_reprocess_images = _safe_nonnegative_int(decision_counts.get("fixed_externally"))
    keep_original_images = _safe_nonnegative_int(decision_counts.get("keep_original_trace"))
    if keep_original_images == 0 and isinstance(review_summary, dict):
        operator_decisions = review_summary.get("operator_decisions")
        if isinstance(operator_decisions, list):
            keep_original_images = sum(
                1
                for item in operator_decisions
                if isinstance(item, dict) and str(item.get("decision") or "").strip().lower() == "keep_original_trace"
            )
    return {
        "total_source_images": total_source_images,
        "processed_output_images": processed_output_images,
        "needs_rescan_images": needs_rescan_images,
        "needs_reprocess_images": needs_reprocess_images,
        "keep_original_images": keep_original_images,
        "next_batch_reminder_zh": (
            "需要继续加工时，点击准备下一批；当前复核队列会清空。"
            "为新批次必须重新选择扫描原图文件夹，不要混用批次；输出文件夹可沿用上次保存的位置。"
        ),
    }


def _local_reuse_handoff_summary(run_summary: dict[str, Any] | None) -> dict[str, Any] | None:
    reuse_summary = run_summary.get("local_reuse_summary") if isinstance(run_summary, dict) else None
    if not isinstance(reuse_summary, dict) or reuse_summary.get("aggregate_only") is not True:
        return None
    total_files = _safe_nonnegative_int(reuse_summary.get("total_files"))
    reused_files = _safe_nonnegative_int(reuse_summary.get("reused_files"))
    reprocessed_files = _safe_nonnegative_int(reuse_summary.get("reprocessed_files"))
    failed_files = _safe_nonnegative_int(reuse_summary.get("failed_files"))
    remaining_files = _safe_nonnegative_int(reuse_summary.get("remaining_files"))
    if total_files == 0:
        total_files = reused_files + reprocessed_files + failed_files + remaining_files
    next_action_zh = (
        "还有失败或待处理图片，请先重试本批次；仍失败再交管理员处理。"
        if failed_files > 0 or remaining_files > 0
        else "无需整批重跑，检查输出文件夹后交接。"
    )
    return {
        "schema_version": "scan-qc.local-processing-reuse-summary.v1",
        "aggregate_only": True,
        "total_files": total_files,
        "reused_files": reused_files,
        "reprocessed_files": reprocessed_files,
        "failed_files": failed_files,
        "remaining_files": remaining_files,
        "next_action_zh": next_action_zh,
        "message_zh": (
            f"本批共 {total_files} 张：已复用 {reused_files} 张，实际重新处理 {reprocessed_files} 张，"
            f"仍失败 {failed_files} 张，剩余待处理 {remaining_files} 张。{next_action_zh}"
        ),
    }


def _completion_stage_timings_note(run_summary: dict[str, Any] | None) -> str | None:
    stage_timings = run_summary.get("stage_timings") if isinstance(run_summary, dict) else None
    if not isinstance(stage_timings, dict) or stage_timings.get("aggregate_only") is not True:
        return None
    stages = stage_timings.get("stages")
    if not isinstance(stages, list):
        return None
    fragments: list[str] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("id") or "").strip()
        label_zh = SAFE_STAGE_TIMING_LABELS_ZH.get(stage_id)
        if label_zh is None:
            continue
        try:
            elapsed_seconds = max(0.0, float(stage.get("elapsed_seconds")))
        except (TypeError, ValueError):
            continue
        status = str(stage.get("status") or "").strip().lower()
        if status == "pending" and elapsed_seconds <= 0:
            continue
        status_suffix = (
            ""
            if status in {"completed", "finished"}
            else f"（{SAFE_STAGE_TIMING_STATUS_LABELS_ZH.get(status, '未完成')}）"
        )
        fragments.append(f"{label_zh} {elapsed_seconds:.1f} 秒{status_suffix}")
    if not fragments:
        return None
    return f"聚合阶段耗时：{'、'.join(fragments)}。"


def _conservative_auto_retouch_handoff_zh(run_summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run_summary, dict):
        return None
    payload = run_summary.get("conservative_auto_retouch_handoff_zh")
    if isinstance(payload, dict) and payload.get("aggregate_only") is True:
        return payload
    audit_summary = run_summary.get("processing_audit_summary")
    if isinstance(audit_summary, dict):
        payload = audit_summary.get("conservative_auto_retouch_handoff_zh")
        if isinstance(payload, dict) and payload.get("aggregate_only") is True:
            return payload
    artifacts = run_summary.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    audit_path = artifacts.get("processing_audit_summary")
    if not isinstance(audit_path, str) or not audit_path.strip():
        return None
    try:
        summary_payload = _read_json(Path(audit_path))
    except OSError:
        return None
    if not isinstance(summary_payload, dict):
        return None
    payload = summary_payload.get("conservative_auto_retouch_handoff_zh")
    if isinstance(payload, dict) and payload.get("aggregate_only") is True:
        return payload
    return None


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_optional_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _folder_is_writable(path: Path) -> bool:
    if not _path_is_existing_dir(path) or not os.access(path, os.W_OK | os.X_OK):
        return False
    for _ in range(8):
        probe_path = _unique_probe_path(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd: int | None = None
        created = False
        try:
            fd = os.open(probe_path, flags, 0o600)
            created = True
            os.write(fd, b"ok\n")
            return True
        except FileExistsError:
            continue
        except OSError:
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if created:
                try:
                    probe_path.unlink()
                except OSError:
                    pass
    return False


def _unique_probe_path(path: Path) -> Path:
    return path / f".scan_qc_preflight_{uuid.uuid4().hex}.tmp"


def _safe_relative_path(value: str) -> Path:
    stripped = value.strip()
    candidate = Path(stripped)
    if not stripped or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("复核记录预览路径不安全。")
    return candidate


def _status_recovery_guidance(
    *,
    configured: bool,
    running: bool,
    summary: dict[str, Any] | None,
    progress: dict[str, Any] | None,
    last_error: str | None,
    last_preflight_guidance: dict[str, Any] | None,
) -> dict[str, Any]:
    base = {
        "schema_version": "scan-qc.local-recovery-guidance.v1",
        "aggregate_only": True,
        "failed_files": 0,
        "retryable_files": 0,
        "derivative_images_ready": 0,
        "total_files": 0,
    }
    if not configured:
        return {
            **base,
            "kind": "folder_setup_missing",
            "title_zh": "文件夹还没有准备好",
            "message_zh": "请先填写扫描原图文件夹和处理后输出文件夹。",
            "next_steps_zh": [
                "确认扫描原图文件夹存在，里面是本批次原图。",
                "确认处理后输出文件夹可以写入，磁盘空间足够。",
                "保存文件夹后再开始处理。",
            ],
        }
    if isinstance(last_preflight_guidance, dict):
        return last_preflight_guidance
    if last_error:
        return {
            **base,
            "kind": "processing_failed_admin",
            "title_zh": "本机处理启动失败",
            "message_zh": "本批次没有正常启动，当前不能直接重试。",
            "next_steps_zh": [
                "检查扫描原图文件夹和输出文件夹是否选对。",
                "确认输出磁盘空间足够，原图图片可以正常打开。",
                "请交管理员处理，不要反复点击开始处理。",
                "如果文件夹选错了，请返回重新选择文件夹。",
            ],
        }
    if isinstance(summary, dict):
        guidance = summary.get("recovery_guidance")
        if isinstance(guidance, dict):
            return guidance
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        total_files_value = _safe_optional_nonnegative_int(counts.get("total_files"))
        failed_files_value = _safe_optional_nonnegative_int(counts.get("failed_files"))
        retryable_files_value = _safe_optional_nonnegative_int(counts.get("retry_list_files"))
        processed_files_value = _safe_optional_nonnegative_int(counts.get("processed_files"))
        resumed_files_value = _safe_optional_nonnegative_int(counts.get("resumed_files"))
        if (
            summary.get("status") == "blocked"
            and (
                total_files_value is None
                or failed_files_value is None
                or retryable_files_value is None
                or processed_files_value is None
                or resumed_files_value is None
            )
        ):
            known_counts = {
                key: value
                for key, value in {
                    "total_files": total_files_value,
                    "failed_files": failed_files_value,
                    "retryable_files": retryable_files_value,
                    "processed_files": processed_files_value,
                    "resumed_files": resumed_files_value,
                }.items()
                if value is not None
            }
            return {
                "schema_version": "scan-qc.local-recovery-guidance.v1",
                "aggregate_only": True,
                "kind": "processing_retry_scope_unknown",
                "title_zh": "不能安全判断重试范围",
                "message_zh": "本机状态文件不完整，不能安全判断本批应重试哪些图片；当前不会误报完成，也不会编造重试数量。",
                "retry_scope_safe": False,
                "known_counts": known_counts,
                "next_steps_zh": [
                    "检查扫描原图文件夹和处理后输出文件夹是否仍然正确。",
                    "需要继续本批时可以重新开始处理；系统会保守处理需要补齐或重新生成的输出。",
                    "如果反复出现，请交管理员查看本机私有状态报告。",
                ],
            }
        failed_files = failed_files_value or 0
        retryable_files = retryable_files_value or 0
        derivative_images_ready = (processed_files_value or 0) + (resumed_files_value or 0)
        total_files = total_files_value or 0
        missing_output_files = retryable_files if retryable_files else failed_files
        can_restart_fill_missing_outputs = retryable_files > 0
        aggregate = {
            **base,
            "failed_files": failed_files,
            "retryable_files": retryable_files,
            "derivative_images_ready": derivative_images_ready,
            "successful_output_files": derivative_images_ready,
            "missing_output_files": missing_output_files,
            "can_restart_fill_missing_outputs": can_restart_fill_missing_outputs,
            "total_files": total_files,
        }
        openable_files = int(counts.get("openable_files") or 0)
        if total_files == 0:
            return {
                **aggregate,
                "kind": "empty_input_folder",
                "title_zh": "原图文件夹是空的",
                "message_zh": "这个扫描原图文件夹里没有发现可处理文件。",
                "next_steps_zh": [
                    "确认是否选到了本批次真正的扫描原图文件夹。",
                    "如果还没有扫描图片，请先完成扫描或把图片放入原图文件夹。",
                    "放好图片后，重新保存文件夹并开始处理。",
                ],
            }
        if summary.get("status") == "blocked" or failed_files:
            return {
                **aggregate,
                "kind": "processing_failed_retryable" if retryable_files else "processing_failed_admin",
                "title_zh": "处理没有全部完成",
                "message_zh": (
                    f"本批有 {failed_files} 张处理失败，已成功输出 {derivative_images_ready} 张；"
                    "可以重新开始本批，系统会只补齐缺失的处理后图片，已经成功输出的图片不会删除。"
                    if can_restart_fill_missing_outputs
                    else f"本批有 {failed_files} 张处理失败，已成功输出 {derivative_images_ready} 张；"
                    "当前没有可自动补齐的缺失输出，请先检查权限、格式或交管理员确认。"
                ),
                "next_steps_zh": [
                    "检查扫描原图文件夹是否可读取，原图是否能正常打开。",
                    "检查处理后输出文件夹是否可写入，磁盘空间是否足够。",
                    "确认原图是当前支持的常见图片格式。",
                    (
                        "重新开始本批，系统会只补齐缺失输出并保留已成功输出。"
                        if can_restart_fill_missing_outputs
                        else "请交管理员处理，不要反复点击开始处理。"
                    ),
                ],
            }
        if openable_files == 0:
            return {
                **aggregate,
                "kind": "no_supported_images",
                "title_zh": "没有可处理的图片",
                "message_zh": "文件夹里没有找到当前支持处理的图片，或图片无法正常打开。",
                "next_steps_zh": [
                    "确认选对了扫描原图文件夹。",
                    "确认原图是常见图片格式，并且能用本机图片查看器打开。",
                    "如果文件格式不对，请重新导出为支持的图片格式后再处理。",
                ],
            }
        if summary.get("status") == "finished":
            return {
                **aggregate,
                "kind": "no_remaining_work",
                "title_zh": "没有剩余处理任务",
                "message_zh": "本批次没有需要人工确认的图片，处理后图片已经准备好。",
                "next_steps_zh": [
                    "打开输出文件夹，检查处理后图片数量和画面状态。",
                    "本机状态文件夹已保存复核结果和交接说明，正常界面不显示具体路径或文件名。",
                    "需要继续加工时，点击准备下一批；当前复核队列会清空。",
                    "为新批次必须重新选择扫描原图文件夹，不要混用批次；输出文件夹可沿用上次保存的位置。",
                ],
            }
    state = progress.get("state") if isinstance(progress, dict) else None
    if state == "running" and not running:
        return {
            **base,
            "kind": "processing_interrupted",
            "title_zh": "上次处理可能中断",
            "message_zh": "本机状态文件显示上次处理没有正常结束，当前不会自动继续处理，也不会误报完成。",
            "next_steps_zh": [
                "确认扫描原图文件夹和输出文件夹仍然正确。",
                "需要继续本批时可以重新开始处理。",
                "如果这不是当前批次，请准备下一批后重新选择文件夹。",
            ],
        }
    return {
        **base,
        "kind": "processing_running" if running or state == "running" else "ready_to_start",
        "title_zh": "正在处理" if running or state == "running" else "可以开始处理",
        "message_zh": "本机正在生成处理后图片，请稍候。" if running or state == "running" else "文件夹已保存，可以开始处理。",
        "next_steps_zh": ["等待处理完成后查看结果。"] if running or state == "running" else ["点击开始处理。"],
    }


def _preview_candidates(input_dir: Path, derivatives_dir: Path, relative_path: Path) -> list[tuple[Path, str]]:
    return [
        (derivatives_dir / "images" / relative_path, "processed"),
        (derivatives_dir / relative_path, "processed"),
        (input_dir / relative_path, "original"),
    ]


def _valid_preview_path(candidate: Path, input_dir: Path, derivatives_dir: Path) -> bool:
    resolved = candidate.expanduser().resolve()
    return resolved.suffix.lower() in PREVIEW_IMAGE_SUFFIXES and resolved.is_file() and (
        _is_relative_to(resolved, input_dir) or _is_relative_to(resolved, derivatives_dir)
    )


def _preview_sources_for_item(item: dict[str, Any], input_dir: Path, derivatives_dir: Path) -> dict[str, bool]:
    sources = {"original": False, "processed": False}
    try:
        relative_path = _safe_relative_path(str(item.get("relative_path") or ""))
    except ValueError:
        return sources
    for candidate, source in _preview_candidates(input_dir, derivatives_dir, relative_path):
        if _valid_preview_path(candidate, input_dir, derivatives_dir):
            sources[source] = True
    return sources


def _preview_source_label(sources: dict[str, bool]) -> str:
    if sources.get("original") and sources.get("processed"):
        return "comparison"
    if sources.get("processed"):
        return "processed"
    if sources.get("original"):
        return "original_fallback"
    return "unavailable"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def _is_loopback_client(host: str) -> bool:
    return host in {"127.0.0.1", "::1"} or host.startswith("127.")
