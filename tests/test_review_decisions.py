from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.review_decisions import build_review_decision_verification_summary

from test_evidence_bundle import _review_decision_export_fixture


class ReviewDecisionVerificationTests(unittest.TestCase):
    def test_review_decisions_verify_passes_complete_aggregate_summary(self) -> None:
            summary = build_review_decision_verification_summary(_review_decision_export_fixture())

            self.assertEqual(summary["schema_version"], "scan-qc.review-decision-verification-summary.v1")
            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["decision_summary"]["total_decisions"], 3)
            self.assertEqual(summary["decision_summary"]["pending"], 0)
            self.assertEqual(summary["decision_summary"]["accepted"], 1)
            self.assertEqual(summary["decision_summary"]["rejected"], 1)
            self.assertEqual(summary["decision_summary"]["rework"], 1)
            self.assertEqual(summary["decision_summary"]["completion_status"], "complete")
            self.assertEqual(
                summary["decision_summary"]["closure_gate_summary"],
                {
                    "open_p0_count": 0,
                    "open_p1_count": 0,
                    "manually_handled_count": 3,
                    "can_complete_delivery": True,
                    "operator_message_zh": "P0/P1 问题已经有处理结论，可以完成交接。",
                },
            )
            self.assertEqual(summary["blocking_counts_by_code"], {})
            self.assertTrue(summary["privacy"]["aggregate_only"])

    def test_review_decisions_verify_treats_zero_review_items_as_complete(self) -> None:
            result = build_review_decision_verification_summary(_review_decision_export_fixture(decisions=()))

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["decision_summary"]["total_decisions"], 0)
            self.assertEqual(result["decision_summary"]["pending"], 0)
            self.assertEqual(result["decision_summary"]["completion_status"], "complete")

    def test_review_decisions_verify_allows_incomplete_without_blocking(self) -> None:
            fixture = _review_decision_export_fixture(decisions=("accepted_issue", "pending", "needs_rescan"))
            result = build_review_decision_verification_summary(fixture)

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["decision_summary"]["pending"], 1)
            self.assertEqual(result["decision_summary"]["completion_status"], "incomplete")
            self.assertEqual(result["decision_summary"]["closure_gate_summary"]["open_p0_count"], 1)
            self.assertFalse(result["decision_summary"]["closure_gate_summary"]["can_complete_delivery"])

    def test_review_decisions_verify_blocks_invalid_decision_value(self) -> None:
            fixture = _review_decision_export_fixture(decisions=("accepted_issue", "done", "blocked"))
            result = build_review_decision_verification_summary(fixture)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["blocking_counts_by_code"]["unknown_decision_value"], 1)
            self.assertNotIn("RID0002", json.dumps(result, ensure_ascii=False))
            self.assertNotIn("done", json.dumps(result, ensure_ascii=False))

    def test_review_decisions_verify_blocks_count_mismatch(self) -> None:
            fixture = _review_decision_export_fixture()
            fixture["source_target_count"] = 4
            fixture["review_counts"]["accepted_issue"] = 99
            fixture["aggregate_counts"]["review_completion"]["reviewed"] = 2

            result = build_review_decision_verification_summary(fixture)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["blocking_counts_by_code"]["source_target_count_mismatch"], 1)
            self.assertEqual(result["blocking_counts_by_code"]["review_count_mismatch"], 1)
            self.assertEqual(result["blocking_counts_by_code"]["review_completion_count_mismatch"], 1)

    def test_review_decisions_verify_blocks_private_fields_by_code_only(self) -> None:
            fixture = _review_decision_export_fixture()
            fixture["decisions"][0]["preview_filename"] = "private_scan_001.tif"
            fixture["sha256"] = "abc123-private-hash"

            result = build_review_decision_verification_summary(fixture)
            raw = json.dumps(result, ensure_ascii=False)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["blocking_counts_by_code"]["privacy_sensitive_field"], 2)
            self.assertFalse(result["privacy"]["aggregate_only"])
            self.assertNotIn("private_scan_001.tif", raw)
            self.assertNotIn("abc123-private-hash", raw)
            self.assertNotIn("preview_filename", raw)
            self.assertNotIn("sha256", raw)

    def test_review_decisions_verify_blocks_sensitive_field_name_variants_by_code_only(self) -> None:
            fixture = _review_decision_export_fixture(decisions=("accepted_issue",))
            fixture["decisions"][0]["image_path"] = "/private/archive/card-001.png"
            fixture["decisions"][0]["file_name"] = "card-001.png"
            fixture["decisions"][0]["sourceImageObjectUrl"] = "blob:https://local.invalid/private"
            fixture["decisions"][0]["ocrText"] = "private OCR text"
            fixture["decisions"][0]["reviewer_note"] = "private reviewer note"

            result = build_review_decision_verification_summary(fixture)
            raw = json.dumps(result, ensure_ascii=False)

            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["privacy"]["aggregate_only"])
            self.assertGreater(result["blocking_counts_by_code"]["privacy_sensitive_field"], 0)
            for sensitive_fragment in (
                "image_path",
                "file_name",
                "sourceImageObjectUrl",
                "ocrText",
                "reviewer_note",
                "/private/archive/card-001.png",
                "card-001.png",
                "blob:https://local.invalid/private",
                "private OCR text",
                "private reviewer note",
            ):
                self.assertNotIn(sensitive_fragment, raw)

    def test_review_decisions_verify_cli_smoke_writes_aggregate_only_output(self) -> None:
            with tempfile.TemporaryDirectory(prefix="review-decisions-") as temp_dir:
                root = Path(temp_dir)
                input_path = root / "scan-qc-review-decisions.summary.json"
                output_path = root / "review_decision_verification_summary.json"
                input_path.write_text(json.dumps(_review_decision_export_fixture()), encoding="utf-8")

                self.assertEqual(
                    main(["review-decisions-verify", "--summary", str(input_path), "--out", str(output_path)]),
                    0,
                )
                payload = json.loads(output_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["decision_summary"]["total_decisions"], 3)


if __name__ == "__main__":
    unittest.main()
