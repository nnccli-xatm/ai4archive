from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.validation_index import build_public_safe_validation_index

from test_evidence_bundle import (
    _release_candidate_bundle_payload,
    _release_readiness_bundle_payload,
    _review_decision_verification_bundle_payload,
    _write_json,
)
from test_final_handoff import _aggregate_evidence_bundle_payload


def _final_handoff_bundle_payload() -> dict[str, object]:
    return {
        "schema_version": "scan-qc.final-production-handoff-summary.v1",
        "status": "pass",
        "ready_for_handoff": True,
        "checks_passed": 7,
        "checks_failed": 0,
        "blocking_item_count": 0,
        "blocking_items": [],
        "artifact_status_summary": {
            "aggregate_evidence_bundle_summary.json": {"present": True, "required": True, "status": "pass"},
            "release_candidate_summary.json": {"present": True, "required": False, "status": "pass"},
            "review_decision_verification_summary.json": {
                "present": True,
                "required": False,
                "status": "pass",
                "checks_passed": 1,
                "checks_failed": 0,
                "blocking_count": 0,
                "warning_count": 0,
                "privacy_status": "pass",
            },
        },
        "privacy": {
            "aggregate_only": True,
            "private_indicators_found": False,
            "private_indicator_count": 0,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
        },
        "sensitive_values_omitted": True,
    }


def _write_public_safe_validation_index_fixtures(root: Path) -> None:
    _write_json(
        root / "frontend_workbench_validation.json",
        {
            "status": "pass",
            "counts": {"required_regions": 8},
            "privacy": {
                "aggregate_only": True,
                "contains_paths": False,
                "contains_filenames": False,
                "contains_hashes": False,
                "contains_ocr_text": False,
                "contains_thumbnails": False,
                "contains_image_content": False,
                "contains_secrets": False,
                "contains_row_level_findings": False,
            },
            "error_count": 0,
            "errors": [],
        },
    )
    _write_json(root / "release_readiness_summary.json", _release_readiness_bundle_payload())
    _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
    _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
    _write_json(root / "review_decision_verification_summary.json", _review_decision_verification_bundle_payload())
    _write_json(root / "final_production_handoff_summary.json", _final_handoff_bundle_payload())


class ValidationIndexTests(unittest.TestCase):
    def test_public_safe_validation_index_passes_known_aggregate_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["public-safe-validation-index", "--input-dir", str(root), "--out", str(root / "index.json")])
            summary = json.loads((root / "index.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["summary"]["artifacts_present"], 6)
        self.assertEqual(summary["summary"]["artifacts_failed"], 0)
        self.assertEqual(summary["artifact_presence"]["frontend_workbench_validation.json"]["status"], "pass")
        self.assertEqual(summary["artifact_presence"]["review_decision_verification_summary.json"]["status"], "pass")
        self.assertEqual(summary["artifact_presence"]["final_production_handoff_summary.json"]["status"], "pass")
        review_decisions = summary["artifacts"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["counts"]["blocking_count"], 0)
        self.assertEqual(review_decisions["counts"]["warning_count"], 0)
        self.assertGreater(summary["checks_passed"], 0)
        self.assertEqual(summary["checks_failed"], 0)
        self.assertFalse(summary["privacy"]["contains_paths"])
        self.assertFalse(summary["privacy"]["contains_filenames"])
        self.assertFalse(summary["privacy"]["contains_hashes"])
        self.assertFalse(summary["privacy"]["contains_ocr_text"])
        self.assertFalse(summary["privacy"]["contains_thumbnails"])
        self.assertFalse(summary["privacy"]["contains_image_content"])
        self.assertFalse(summary["privacy"]["contains_row_level_findings"])
        self.assertIn("Validation index status: pass", stdout.getvalue())

    def test_public_safe_validation_index_reports_fail_and_missing_by_code_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            failing_release = _release_candidate_bundle_payload()
            failing_release["status"] = "fail"
            failing_release["ready_for_release_candidate"] = False
            _write_json(root / "release_candidate_summary.json", failing_release)
            (root / "aggregate_evidence_bundle_summary.json").unlink()

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["artifact_presence"]["release_candidate_summary.json"]["status"], "fail")
        self.assertEqual(summary["artifact_presence"]["aggregate_evidence_bundle_summary.json"]["status"], "missing")
        self.assertIn("artifact_status_failed", {item["code"] for item in summary["blocking_items"]})
        self.assertIn("aggregate_artifact_missing", {item["code"] for item in summary["blocking_items"]})
        self.assertEqual(summary["summary"]["artifacts_missing"], 1)
        self.assertTrue(summary["privacy"]["aggregate_only"])

    def test_public_safe_validation_index_covers_review_decision_handoff_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["summary"]["known_artifacts"], 6)
        review_decisions = summary["artifacts"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["schema_version"], "scan-qc.review-decision-verification-summary.v1")
        self.assertEqual(review_decisions["reported_status"], "pass")
        self.assertEqual(review_decisions["counts"]["blocking_counts_by_code"], {})
        evidence = summary["artifacts"]["aggregate_evidence_bundle_summary.json"]["review_decision_verification"]
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["blocking_count"], 0)
        handoff = summary["artifacts"]["final_production_handoff_summary.json"]["review_decision_verification"]
        self.assertTrue(handoff["present"])
        self.assertEqual(handoff["status"], "pass")
        for forbidden in ("decision_counts", "source_type", "scan-qc-review-decisions.local.v1", "aggregate_handoff"):
            self.assertNotIn(forbidden, raw)

    def test_public_safe_validation_index_blocks_review_decision_private_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            payload = _review_decision_verification_bundle_payload()
            payload["source"] = {
                "schema": "scan-qc-review-decisions.local.v1",
                "source_type": "aggregate_handoff",
            }
            _write_json(root / "review_decision_verification_summary.json", payload)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["artifact_presence"]["review_decision_verification_summary.json"]["status"], "fail")
        self.assertIn("private_source_metadata_present", {item["code"] for item in summary["blocking_items"]})
        self.assertNotIn("scan-qc-review-decisions.local.v1", raw)
        self.assertNotIn("aggregate_handoff", raw)

    def test_public_safe_validation_index_propagates_processing_reuse_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            payload = _aggregate_evidence_bundle_payload(status="pass")
            payload["summary"] = {
                "total_files": 8,
                "processing_resumed_files": 2,
                "processing_duplicate_reused_files": 3,
                "processing_existing_derivative_reused_files": 4,
            }
            _write_json(root / "aggregate_evidence_bundle_summary.json", payload)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["summary"]["processing_resumed_files"], 2)
        self.assertEqual(summary["summary"]["processing_duplicate_reused_files"], 3)
        self.assertEqual(summary["summary"]["processing_existing_derivative_reused_files"], 4)
        counts = summary["artifacts"]["aggregate_evidence_bundle_summary.json"]["counts"]
        self.assertEqual(counts["processing_resumed_files"], 2)
        self.assertEqual(counts["processing_duplicate_reused_files"], 3)
        self.assertEqual(counts["processing_existing_derivative_reused_files"], 4)

    def test_public_safe_validation_index_omits_missing_processing_reuse_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertNotIn("processing_resumed_files", summary["summary"])
        self.assertNotIn("processing_duplicate_reused_files", summary["summary"])
        self.assertNotIn("processing_existing_derivative_reused_files", summary["summary"])

    def test_public_safe_validation_index_reuse_counters_remain_aggregate_only(self) -> None:
        forbidden_private_values = [
            "/Users/private/archive/page_0001.png",
            "page_0001.png",
            "processing_manifest.json",
            "row_report.csv",
            "OCR text",
            "thumbnail-preview-object",
            "data:image/png",
            "blob:http://localhost/preview",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "provider --private /Users/private/archive",
            "prompt: inspect the private page",
            "raw_model_output: private answer",
            "derivative/page_0001.png",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            payload = _aggregate_evidence_bundle_payload(status="pass")
            payload["summary"] = {
                "processing_resumed_files": 0,
                "processing_duplicate_reused_files": 1,
                "processing_existing_derivative_reused_files": 2,
            }
            payload["operator_warning"] = " ".join(forbidden_private_values)
            _write_json(root / "aggregate_evidence_bundle_summary.json", payload)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["summary"]["processing_resumed_files"], 0)
        self.assertEqual(summary["summary"]["processing_duplicate_reused_files"], 1)
        self.assertEqual(summary["summary"]["processing_existing_derivative_reused_files"], 2)
        self.assertIn("private_value_present", {item["code"] for item in summary["blocking_items"]})
        for value in forbidden_private_values:
            self.assertNotIn(value, raw)


    def test_public_safe_validation_index_omits_private_values_from_privacy_failures(self) -> None:
            forbidden_private_values = [
                "/Users/private/archive/page_0001.png",
                "page_0001.png",
                "processing_manifest.json",
                "row_report.csv",
                "OCR text",
                "thumbnail-preview-object",
                "data:image/png",
                "blob:http://localhost/preview",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "provider --private /Users/private/archive",
                "prompt: inspect the private page",
                "raw_model_output: private answer",
                "derivative/page_0001.png",
            ]
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_public_safe_validation_index_fixtures(root)
                payload = _aggregate_evidence_bundle_payload(status="pass")
                payload["operator_warning"] = " ".join(forbidden_private_values)
                _write_json(root / "aggregate_evidence_bundle_summary.json", payload)

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(["public-safe-validation-index", "--input-dir", str(root), "--out", str(root / "index.json")])
                raw = (root / "index.json").read_text(encoding="utf-8")
                summary = json.loads(raw)

            self.assertEqual(exit_code, 1)
            self.assertEqual(summary["status"], "fail")
            self.assertTrue(summary["privacy"]["aggregate_only"])
            self.assertTrue(summary["privacy"]["private_indicators_found"])
            self.assertIn("private_value_present", {item["code"] for item in summary["blocking_items"]})
            self.assertIn("private_local_preview_object_url_present", {item["code"] for item in summary["blocking_items"]})
            self.assertIn("private_raw_model_output_present", {item["code"] for item in summary["blocking_items"]})
            self.assertFalse(summary["privacy"]["contains_local_preview_object_urls"])
            self.assertFalse(summary["privacy"]["contains_provider_command_strings"])
            self.assertFalse(summary["privacy"]["contains_prompts"])
            self.assertFalse(summary["privacy"]["contains_raw_model_output"])
            for value in forbidden_private_values:
                self.assertNotIn(value, raw)

if __name__ == "__main__":
    unittest.main()
