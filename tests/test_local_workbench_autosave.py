from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.local_workbench import (
    COMPLETION_NOTE_TXT,
    REVIEW_DECISION_DRAFT_JSON,
    REVIEW_DECISION_SUMMARY_JSON,
    WorkbenchController,
)
from archive_scan_qc.production_runner import ProductionRunConfig, build_production_run_summary
from archive_scan_qc.production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON
from archive_scan_qc.review_decisions import REVIEW_DECISION_VERIFICATION_JSON


def decision_summary(decisions: list[tuple[str, str]]) -> dict[str, object]:
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
            "p1": 0,
            "p2": 0,
            "review_completion": {
                "total": len(rows),
                "reviewed": sum(1 for _, decision in decisions if decision != "pending"),
                "pending": counts["pending"],
                "complete": counts["pending"] == 0,
                "counts": counts,
            },
        },
        "review_counts": counts,
        "reviewed_targets": sum(1 for _, decision in decisions if decision != "pending"),
        "decisions": rows,
    }


class LocalWorkbenchAutosaveTests(unittest.TestCase):
    def test_empty_batch_summary_gives_operator_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            admin_dir = metadata_dir / "admin_reports"
            input_dir.mkdir()
            summary = build_production_run_summary(
                config=ProductionRunConfig(input_dir=input_dir, derivative_output_dir=output_dir, metadata_output_dir=metadata_dir),
                report={
                    "summary": {
                        "total_files": 0,
                        "openable_files": 0,
                        "p0_findings": 0,
                        "p1_findings": 0,
                        "p2_findings": 0,
                        "total_findings": 0,
                        "performance": {},
                    }
                },
                processing_manifest={
                    "image_root": str(output_dir / "images"),
                    "summary": {
                        "total_files": 0,
                        "processed_files": 0,
                        "resumed_files": 0,
                        "skipped_files": 0,
                        "failed_files": 0,
                        "retry_list_files": 0,
                        "performance": {},
                    },
                },
                admin_report_dir=admin_dir,
                generated_at="2026-01-01T00:00:00+00:00",
            )

            self.assertEqual(summary["status"], "finished")
            self.assertFalse(summary["ready_for_operator_handoff"])
            self.assertEqual(summary["local_batch_state"], "empty_input_folder")
            self.assertEqual(summary["recovery_guidance"]["kind"], "empty_input_folder")
            self.assertIn("原图文件夹", summary["recovery_guidance"]["title_zh"])
            self.assertEqual(summary["operator_summary"]["files_needing_attention"], 0)

    def test_unsupported_only_batch_summary_gives_plain_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            admin_dir = metadata_dir / "admin_reports"
            input_dir.mkdir()
            summary = build_production_run_summary(
                config=ProductionRunConfig(input_dir=input_dir, derivative_output_dir=output_dir, metadata_output_dir=metadata_dir),
                report={
                    "summary": {
                        "total_files": 2,
                        "openable_files": 0,
                        "p0_findings": 0,
                        "p1_findings": 2,
                        "p2_findings": 0,
                        "total_findings": 2,
                        "performance": {},
                    }
                },
                processing_manifest={
                    "image_root": str(output_dir / "images"),
                    "summary": {
                        "total_files": 2,
                        "processed_files": 0,
                        "resumed_files": 0,
                        "skipped_files": 2,
                        "failed_files": 0,
                        "retry_list_files": 0,
                        "performance": {},
                    },
                },
                admin_report_dir=admin_dir,
                generated_at="2026-01-01T00:00:00+00:00",
            )

            self.assertEqual(summary["local_batch_state"], "no_supported_images")
            self.assertFalse(summary["ready_for_operator_handoff"])
            self.assertEqual(summary["recovery_guidance"]["kind"], "no_supported_images")
            self.assertIn("常见图片格式", " ".join(summary["recovery_guidance"]["next_steps_zh"]))

    def test_draft_decisions_are_saved_and_returned_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-review-queue.v1",
                        "items": [
                            {"local_id": "PRQ000001", "relative_path": "a.png"},
                            {"local_id": "PRQ000002", "relative_path": "b.png"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            draft = decision_summary([("PRQ000001", "needs_rescan"), ("PRQ000002", "pending")])
            draft["operator_decisions"] = [
                {"scope": "production_review_queue", "local_id": "PRQ000001", "decision": "keep_original_trace"}
            ]
            result = controller.save_draft_review_decisions(draft)

            self.assertTrue(result["saved"])
            self.assertEqual(result["message_zh"], "已自动保存")
            self.assertEqual(result["decision_summary"]["completion_status"], "incomplete")
            self.assertEqual(json.loads((metadata_dir / REVIEW_DECISION_DRAFT_JSON).read_text(encoding="utf-8")), draft)
            status = controller.status()
            self.assertEqual(status["draft_decisions"], draft)
            self.assertEqual(status["queue"]["items"][0]["local_id"], "PRQ000001")

    def test_final_completion_still_writes_verifier_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            summary = decision_summary([("PRQ000001", "needs_rescan"), ("PRQ000002", "false_positive")])
            result = controller.save_review_decisions(summary)

            self.assertTrue(result["finished"])
            self.assertEqual(result["message_zh"], "完成并导出结果：处理后图片和复核结果已保存。")
            self.assertEqual(result["decision_summary"]["completion_status"], "complete")
            self.assertEqual(result["completion_panel"]["title_zh"], "完成并导出结果")
            self.assertEqual(result["completion_panel"]["total_review_items"], 2)
            self.assertEqual(result["completion_panel"]["reviewed_items"], 2)
            self.assertEqual(result["completion_panel"]["pending_items"], 0)
            self.assertEqual(result["completion_panel"]["derivatives_dir"], str(output_dir.resolve()))
            self.assertEqual(result["completion_panel"]["metadata_dir"], str(metadata_dir.resolve()))
            self.assertTrue(result["completion_panel"]["decision_summary_path"].endswith(REVIEW_DECISION_SUMMARY_JSON))
            self.assertTrue(result["completion_panel"]["verification_summary_path"].endswith(REVIEW_DECISION_VERIFICATION_JSON))
            self.assertTrue(result["completion_panel"]["completion_note_path"].endswith(COMPLETION_NOTE_TXT))
            self.assertEqual(
                result["completion_panel"]["checklist_zh"],
                ["处理后图片已准备好", "复核结果已保存", "交接说明已保存", "可以准备下一批"],
            )
            self.assertTrue((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            completion_note = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")
            self.assertIn("处理后图片文件夹：", completion_note)
            self.assertIn("复核结果保存位置：", completion_note)
            self.assertIn("下一批：", completion_note)
            verification = json.loads((metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "pass")

    def test_final_completion_allows_no_review_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            summary = decision_summary([])
            result = controller.save_review_decisions(summary)

            self.assertTrue(result["finished"])
            self.assertEqual(result["decision_summary"]["completion_status"], "complete")
            self.assertEqual(result["completion_panel"]["total_review_items"], 0)
            self.assertEqual(result["completion_panel"]["reviewed_items"], 0)
            self.assertEqual(result["completion_panel"]["pending_items"], 0)
            self.assertTrue((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            completion_note = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")
            self.assertIn("复核总数：0", completion_note)
            verification = json.loads((metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "pass")

    def test_private_fields_are_rejected_for_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            draft = decision_summary([("PRQ000001", "pending")])
            draft["relative_path"] = "private.png"

            with self.assertRaises(ValueError):
                controller.save_draft_review_decisions(draft)
            self.assertFalse((metadata_dir / REVIEW_DECISION_DRAFT_JSON).exists())


if __name__ == "__main__":
    unittest.main()
