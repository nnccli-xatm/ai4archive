"""Sandbox-safe smoke validation for production workbench completion/export."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archive_scan_qc.local_workbench import (  # noqa: E402
    COMPLETION_NOTE_TXT,
    PRODUCTION_RUN_SUMMARY_JSON,
    REVIEW_DECISION_SUMMARY_JSON,
    WorkbenchController,
)
from archive_scan_qc.review_decisions import REVIEW_DECISION_VERIFICATION_JSON  # noqa: E402


EXPECTED_MESSAGE_ZH = "本批已完成：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。"
PRIVATE_TERMS = {
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


def _decision_summary(decisions: list[tuple[str, str]]) -> dict[str, Any]:
    counts = {
        "pending": 0,
        "accepted_issue": 0,
        "false_positive": 0,
        "fixed_externally": 0,
        "needs_rescan": 0,
        "blocked": 0,
    }
    rows = []
    for local_id, decision in decisions:
        counts[decision] += 1
        rows.append({"scope": "production_review_queue", "local_id": local_id, "decision": decision})
    reviewed = sum(1 for _, decision in decisions if decision != "pending")
    return {
        "schema": "scan-qc-review-decisions.local.v1",
        "source_type": "production_workbench",
        "source_target_count": len(rows),
        "generated_in_browser": True,
        "privacy": {"summary_only": True},
        "aggregate_counts": {
            "total_batches": 1,
            "total_findings": len(rows),
            "p0": 0,
            "p1": 1,
            "p2": 1,
            "p0_pending": 0,
            "p1_pending": 0,
            "review_completion": {
                "total": len(rows),
                "reviewed": reviewed,
                "pending": counts["pending"],
                "complete": counts["pending"] == 0,
                "counts": counts,
            },
        },
        "review_counts": counts,
        "reviewed_targets": reviewed,
        "decisions": rows,
    }


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_no_private_terms(payload: Any, label: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    leaked = sorted(term for term in PRIVATE_TERMS if term.lower() in text.lower())
    _assert(not leaked, f"{label} includes forbidden private terms: {', '.join(leaked)}")


def run_smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="production-completion-smoke-") as temp_dir:
        root = Path(temp_dir)
        input_dir = root / "input"
        output_dir = root / "output"
        metadata_dir = root / "metadata"
        input_dir.mkdir()

        controller = WorkbenchController()
        controller.configure(input_dir, output_dir, metadata_dir)

        (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
            json.dumps(
                {
                    "operator_summary": {"derivative_images_ready": 7},
                    "counts": {"processed_files": 7, "resumed_files": 0},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        summary = _decision_summary(
            [
                ("PRQ-SMOKE-001", "needs_rescan"),
                ("PRQ-SMOKE-002", "fixed_externally"),
                ("PRQ-SMOKE-003", "false_positive"),
            ]
        )
        summary["operator_name"] = "复核员"
        summary["operator_decisions"] = [
            {
                "scope": "production_review_queue",
                "local_id": "PRQ-SMOKE-001",
                "decision": "rescan",
                "decided_at": "2026-05-14T03:00:00.000Z",
                "note_zh": "画面需要补扫。",
            },
            {
                "scope": "production_review_queue",
                "local_id": "PRQ-SMOKE-002",
                "decision": "reprocess",
                "decided_at": "2026-05-14T03:00:30.000Z",
                "note_zh": "已重新处理。",
            },
            {
                "scope": "production_review_queue",
                "local_id": "PRQ-SMOKE-003",
                "decision": "pass",
                "decided_at": "2026-05-14T03:01:00.000Z",
                "note_zh": "",
            },
        ]

        result = controller.save_review_decisions(summary)
        panel = result.get("completion_panel")
        decision_summary = result.get("decision_summary")

        _assert(result.get("finished") is True, "completion/export result did not finish")
        _assert(result.get("message_zh") == EXPECTED_MESSAGE_ZH, "completion/export final Chinese message changed")
        _assert(isinstance(panel, dict), "completion/export panel missing")
        _assert(isinstance(decision_summary, dict), "completion/export decision summary missing")
        _assert(decision_summary.get("completion_status") == "complete", "decision summary is not complete")
        _assert(
            decision_summary.get("closure_gate_summary", {}).get("can_complete_delivery") is True,
            "decision closure gate did not allow delivery",
        )
        _assert(panel.get("title_zh") == "本批已完成", "completion panel title changed")
        _assert(panel.get("completion_status_zh") == "本批已完成", "completion status copy changed")
        _assert(panel.get("manual_work_zh") == "没有待人工处理图片", "manual work copy changed")
        _assert(panel.get("admin_handoff_zh") == "不需要", "admin handoff copy changed")
        _assert(panel.get("total_review_items") == 3, "completion panel total count changed")
        _assert(panel.get("reviewed_items") == 3, "completion panel reviewed count changed")
        _assert(panel.get("pending_items") == 0, "completion panel pending count changed")
        _assert(panel.get("processed_output_images") == 7, "completion panel output count changed")
        _assert(panel.get("needs_rescan_images") == 1, "completion panel rescan count changed")
        _assert(panel.get("needs_reprocess_images") == 1, "completion panel reprocess count changed")
        _assert(str(output_dir.resolve()) == panel.get("derivatives_dir"), "derivative artifact pointer changed")
        _assert(str(metadata_dir.resolve()) == panel.get("metadata_dir"), "metadata artifact pointer changed")

        artifacts = [
            metadata_dir / REVIEW_DECISION_SUMMARY_JSON,
            metadata_dir / REVIEW_DECISION_VERIFICATION_JSON,
            metadata_dir / COMPLETION_NOTE_TXT,
        ]
        for artifact in artifacts:
            _assert(artifact.exists(), "completion/export did not write every local artifact")

        saved_summary = json.loads((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).read_text(encoding="utf-8"))
        verification = json.loads((metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).read_text(encoding="utf-8"))
        completion_note = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")

        _assert(saved_summary.get("privacy", {}).get("summary_only") is True, "saved summary is not summary-only")
        _assert(saved_summary.get("source_type") == "production_workbench", "saved summary source type changed")
        _assert(verification.get("status") == "pass", "saved verification did not pass")
        _assert("本批已完成交接说明" in completion_note, "completion note missing Chinese handoff title")
        _assert("复核总数：3" in completion_note, "completion note missing aggregate total")
        _assert("已输出处理后图片：7 张" in completion_note, "completion note missing output handoff count")
        _assert("需要重扫：1 张" in completion_note, "completion note missing rescan handoff count")
        _assert("需要重新处理：1 张" in completion_note, "completion note missing reprocess handoff count")
        _assert("待决定：0" in completion_note, "completion note missing aggregate pending count")
        _assert("未关闭 P0：0" in completion_note, "completion note missing aggregate open P0 count")
        _assert("未关闭 P1：0" in completion_note, "completion note missing aggregate open P1 count")
        _assert("已有人工处理结论：3" in completion_note, "completion note missing aggregate handled count")
        _assert_no_private_terms(saved_summary, "saved summary")
        _assert_no_private_terms(
            {
                "title_zh": panel.get("title_zh"),
                "message_zh": panel.get("message_zh"),
                "completion_status_zh": panel.get("completion_status_zh"),
                "manual_work_zh": panel.get("manual_work_zh"),
                "admin_handoff_zh": panel.get("admin_handoff_zh"),
                "total_review_items": panel.get("total_review_items"),
                "reviewed_items": panel.get("reviewed_items"),
                "pending_items": panel.get("pending_items"),
                "processed_output_images": panel.get("processed_output_images"),
                "needs_rescan_images": panel.get("needs_rescan_images"),
                "needs_reprocess_images": panel.get("needs_reprocess_images"),
                "checklist_zh": panel.get("checklist_zh"),
                "next_steps_zh": panel.get("next_steps_zh"),
                "processing_mode": panel.get("processing_mode"),
            },
            "operator completion panel",
        )

        return {
            "status": "pass",
            "review_items": panel.get("total_review_items"),
            "reviewed_items": panel.get("reviewed_items"),
            "pending_items": panel.get("pending_items"),
            "processed_output_images": panel.get("processed_output_images"),
            "needs_rescan_images": panel.get("needs_rescan_images"),
            "needs_reprocess_images": panel.get("needs_reprocess_images"),
            "local_artifact_count": len(artifacts),
            "summary_only": saved_summary.get("privacy", {}).get("summary_only") is True,
            "verification_status": verification.get("status"),
        }


def main() -> int:
    try:
        result = run_smoke()
    except AssertionError as exc:
        print(f"Production workbench completion/export smoke failed: {exc}", file=sys.stderr)
        return 1
    print("Production workbench completion/export smoke passed")
    print(
        "review_items={review_items} reviewed={reviewed_items} pending={pending_items} "
        "output={processed_output_images} rescan={needs_rescan_images} reprocess={needs_reprocess_images} "
        "local_artifacts={local_artifact_count} summary_only={summary_only} verification={verification_status}".format(
            **result
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
