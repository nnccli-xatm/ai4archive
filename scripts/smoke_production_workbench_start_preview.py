"""Sandbox-safe smoke validation for production workbench start-to-preview readiness."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archive_scan_qc.local_workbench import WorkbenchController  # noqa: E402
from archive_scan_qc.production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON  # noqa: E402
from archive_scan_qc.production_runner import (  # noqa: E402
    PRODUCTION_RUN_PROGRESS_JSON,
    PRODUCTION_RUN_SUMMARY_JSON,
)


PRIVATE_TERMS = {
    "/Users/",
    "/private/",
    "\\",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    "relative_path",
    "source_path",
    "thumbnail",
    "sha256",
    "hash",
    "OCR",
    "ocr_text",
    "preview_url",
    "original_path",
    "processed_path",
}


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fake_progress(state: str, *, current_step: str | None, completed_steps: int) -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.production-run-progress.v1",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "state": state,
        "state_label_zh": "处理中" if state == "running" else "需要人工复核",
        "message_zh": "生成处理后图片中，请稍候。" if state == "running" else "已完成自动处理，仍有图片需要人工确认。",
        "current_step": current_step,
        "completed_steps": completed_steps,
        "total_steps": 3,
        "steps": [
            {"id": "scan", "label": "检查扫描图片", "state": "completed", "completed_items": 2, "total_items": 2},
            {
                "id": "process",
                "label": "生成处理后图片",
                "state": "running" if state == "running" else "completed",
                "completed_items": 1 if state == "running" else 2,
                "total_items": 2,
            },
            {
                "id": "summarize",
                "label": "整理处理结果",
                "state": "pending" if state == "running" else "completed",
                "completed_items": None,
                "total_items": None,
            },
        ],
    }


def _fake_summary() -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.production-run.v1",
        "generated_at": "2026-05-14T00:00:00+00:00",
        "status": "needs_review",
        "status_label_zh": "需要人工复核",
        "ready_for_operator_handoff": False,
        "local_batch_state": "review_required",
        "recovery_guidance": {
            "schema_version": "scan-qc.local-recovery-guidance.v1",
            "aggregate_only": True,
            "kind": "review_required",
            "title_zh": "需要人工确认",
            "message_zh": "自动处理已完成，仍有图片需要人工确认。",
            "next_steps_zh": ["查看大图后选择确认通过、返工或交管理员处理。"],
            "failed_files": 0,
            "retryable_files": 0,
            "derivative_images_ready": 2,
            "total_files": 2,
        },
        "operator_summary": {
            "message": "已完成自动处理，有图片需要人工确认。",
            "message_zh": "已完成自动处理，有图片需要人工确认。",
            "processing_mode": "standard",
            "processing_mode_label_zh": "标准优化",
            "processing_mode_purpose_zh": "推荐用于正常批量生产，兼顾批量图片质量和处理效率。",
            "processing_mode_output_zh": "会生成处理后优化图片，原图不覆盖。",
            "total_source_images": 2,
            "openable_source_images": 2,
            "derivative_images_ready": 2,
            "files_needing_attention": 1,
        },
        "counts": {
            "total_files": 2,
            "openable_files": 2,
            "p0_findings": 1,
            "p1_findings": 0,
            "p2_findings": 0,
            "total_findings": 1,
            "processed_files": 2,
            "resumed_files": 0,
            "skipped_files": 0,
            "failed_files": 0,
            "retry_list_files": 0,
        },
        "progress": {"state": "completed", "total_steps": 3, "completed_steps": 3, "total_items": 2, "completed_items": 2},
        "source_images_modified": False,
        "network_services_called": False,
        "model_inference_run": False,
    }


def _fake_queue() -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.production-review-queue.v1",
        "generated_at": "2026-05-14T00:00:00+00:00",
        "privacy": {
            "local_only": True,
            "aggregate_only": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_image_bytes": False,
            "contains_base64": False,
            "contains_ocr_text": False,
            "contains_public_evidence": False,
        },
        "summary": {
            "total_items": 1,
            "items_by_severity": {"P0": 1, "P1": 0, "P2": 0, "P3": 0, "info": 0},
            "items_by_suggested_action": {"pass": 0, "rescan": 1, "reprocess": 0, "keep_original_trace": 0, "skip": 0},
            "ready_for_operator_review": True,
        },
        "items": [
            {
                "local_id": "PRQ-SMOKE-001",
                "relative_path": "smoke-page-001.png",
                "severity": "P0",
                "source_category": "scan_qc",
                "source_ref": "openable",
                "reason_zh": "扫描质检发现阻断问题：源图无法打开。请优先重扫或剔除不可用源图。",
                "focus_hints_zh": ["看图片能否正常打开", "打不开就按重扫处理"],
                "suggested_action": "rescan",
                "sensitivity": {
                    "local_only": True,
                    "contains_image_bytes": False,
                    "contains_thumbnail": False,
                    "contains_hash": False,
                    "contains_ocr_text": False,
                },
            }
        ],
    }


def _assert_no_private_terms(payload: dict[str, Any], label: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    leaked = sorted(term for term in PRIVATE_TERMS if term.lower() in text.lower())
    _assert(not leaked, f"{label} includes forbidden private terms: {', '.join(leaked)}")


def run_smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="production-start-preview-smoke-") as temp_dir:
        root = Path(temp_dir)
        input_dir = root / "input"
        output_dir = root / "output"
        metadata_dir = root / "metadata"
        processed_dir = output_dir / "images"
        input_dir.mkdir()
        processed_dir.mkdir(parents=True)
        (input_dir / "smoke-page-001.png").write_bytes(b"synthetic original preview")
        (processed_dir / "smoke-page-001.png").write_bytes(b"synthetic processed preview")

        progress_written = threading.Event()
        finish_run = threading.Event()

        def fake_run(config: Any) -> dict[str, Any]:
            _write_json(config.metadata_output_dir / PRODUCTION_RUN_PROGRESS_JSON, _fake_progress("running", current_step="process", completed_steps=1))
            progress_written.set()
            _assert(finish_run.wait(timeout=5), "synthetic production run was not released")
            summary = _fake_summary()
            _write_json(config.metadata_output_dir / PRODUCTION_RUN_SUMMARY_JSON, summary)
            _write_json(config.metadata_output_dir / PRODUCTION_RUN_PROGRESS_JSON, _fake_progress("needs_review", current_step=None, completed_steps=3))
            return summary

        def fake_write_review_queue(self: WorkbenchController, summary: dict[str, Any]) -> None:
            _assert(summary.get("status") == "needs_review", "synthetic summary did not reach review state")
            _write_json(metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON, _fake_queue())

        controller = WorkbenchController()
        configured = controller.configure(input_dir, output_dir, metadata_dir, processing_mode="standard")
        readiness = configured["folder_readiness"]
        _assert(configured["configured"] is True, "folder configuration did not persist")
        _assert(readiness["ready_to_start"] is True, "folder readiness did not allow start")
        _assert(readiness["supported_image_count"] == 1, "folder readiness did not aggregate supported images")
        _assert(readiness["selected_processing_mode"]["label_zh"] == "标准优化", "Chinese processing mode label changed")

        with (
            mock.patch("archive_scan_qc.local_workbench.run_production_folder", side_effect=fake_run),
            mock.patch.object(WorkbenchController, "_write_review_queue", fake_write_review_queue),
        ):
            start_status = controller.start()
            _assert(start_status["running"] is True, "start did not enter running state")
            _assert(progress_written.wait(timeout=5), "running progress was not written")
            running_status = controller.status()
            finish_run.set()
            if controller._thread is not None:
                controller._thread.join(timeout=5)

        final_status = controller.status()
        progress = final_status.get("progress") or {}
        summary = final_status.get("summary") or {}
        operator_summary = summary.get("operator_summary") or {}
        queue = final_status.get("queue") or {}
        items = queue.get("items") or []
        first_item = items[0] if items else {}

        _assert((running_status.get("progress") or {}).get("state") == "running", "running progress state missing")
        _assert(progress.get("state") == "needs_review", "final progress did not reach review state")
        _assert(progress.get("completed_steps") == progress.get("total_steps") == 3, "aggregate progress did not complete")
        _assert(summary.get("status") == "needs_review", "summary did not reach preview review readiness")
        _assert(operator_summary.get("message_zh") == "已完成自动处理，有图片需要人工确认。", "Chinese operator summary changed")
        _assert(operator_summary.get("total_source_images") == 2, "operator total image count changed")
        _assert(operator_summary.get("derivative_images_ready") == 2, "operator derivative-ready count changed")
        _assert(operator_summary.get("files_needing_attention") == 1, "operator review-needed count changed")
        _assert(queue.get("summary", {}).get("ready_for_operator_review") is True, "queue is not ready for operator review")
        _assert(len(items) == 1, "review queue item count changed")
        _assert(first_item.get("preview_source") == "comparison", "preview comparison source was not available")
        _assert(first_item.get("preview_sources") == {"original": True, "processed": True}, "preview source availability changed")
        _assert(controller.preview_path("PRQ-SMOKE-001", "original")[1] == "original", "original preview route unavailable")
        _assert(controller.preview_path("PRQ-SMOKE-001", "processed")[1] == "processed", "processed preview route unavailable")

        public_evidence = {
            "configured": final_status["configured"],
            "readiness_status": readiness["status"],
            "processing_mode_zh": readiness["selected_processing_mode"]["label_zh"],
            "running_state_seen": (running_status.get("progress") or {}).get("state"),
            "final_progress_state": progress.get("state"),
            "completed_steps": progress.get("completed_steps"),
            "total_steps": progress.get("total_steps"),
            "total_source_images": operator_summary.get("total_source_images"),
            "derivative_images_ready": operator_summary.get("derivative_images_ready"),
            "review_queue_items": len(items),
            "preview_ready_items": sum(1 for item in items if isinstance(item, dict) and item.get("preview_source") != "unavailable"),
            "operator_message_zh": operator_summary.get("message_zh"),
            "summary_only": True,
        }
        _assert_no_private_terms(public_evidence, "smoke stdout evidence")
        return public_evidence


def main() -> int:
    try:
        result = run_smoke()
    except AssertionError as exc:
        print(f"Production workbench start-to-preview smoke failed: {exc}", file=sys.stderr)
        return 1
    print("Production workbench start-to-preview smoke passed")
    print(
        "configured={configured} readiness={readiness_status} mode={processing_mode_zh} "
        "running_state_seen={running_state_seen} final_state={final_progress_state} "
        "steps={completed_steps}/{total_steps} source_images={total_source_images} "
        "derivatives_ready={derivative_images_ready} review_items={review_queue_items} "
        "preview_ready={preview_ready_items} summary_only={summary_only}".format(**result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
