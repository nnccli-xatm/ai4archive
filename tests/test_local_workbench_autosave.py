from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.local_workbench import (
    REVIEW_DECISION_DRAFT_JSON,
    REVIEW_DECISION_SUMMARY_JSON,
    WorkbenchController,
)
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
                "complete": len(rows) > 0 and counts["pending"] == 0,
                "counts": counts,
            },
        },
        "review_counts": counts,
        "reviewed_targets": sum(1 for _, decision in decisions if decision != "pending"),
        "decisions": rows,
    }


class LocalWorkbenchAutosaveTests(unittest.TestCase):
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
            self.assertEqual(result["decision_summary"]["completion_status"], "complete")
            self.assertTrue((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
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
