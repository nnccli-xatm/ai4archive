from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.deep_inspection_candidates import build_deep_inspection_candidate_summary


class DeepInspectionCandidatesTests(unittest.TestCase):
    def test_deep_inspection_candidate_summary_is_aggregate_only(self) -> None:
            scan_report = {
                "schema_version": "scan-qc.phase1.v1",
                "source_root": "/Users/private/archive/private_batch",
                "summary": {"total_findings": 2},
                "findings": [
                    {
                        "id": "row-001-private",
                        "relative_path": "secret_page_001.png",
                        "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                        "rule": "quality_too_dark",
                        "severity": "P1",
                        "source": "scanner",
                        "confidence": 0.71,
                        "message": "OCR text says private donor name",
                        "thumbnail": "data:image/png;base64,private",
                    },
                    {
                        "candidate_id": "candidate-private-002",
                        "relative_path": "secret_page_002.png",
                        "rule": "provider.local.review_needed",
                        "severity": "P2",
                        "source": "provider",
                        "confidence": 0.96,
                        "reviewer_notes": "call curator about restricted folder",
                    },
                ],
            }
            processing_review_package = {
                "schema_version": "scan-qc.processing-review.v1",
                "source_processing_manifest": "processing_manifest.json",
                "summary": {
                    "failed_files": 1,
                    "guardrail_warning_files": 1,
                    "status_counts": {"failed": 1, "needs_review": 2, "processed": 3},
                },
                "groups": {
                    "failed": {
                        "count": 1,
                        "records": [{"source_relative_path": "private_failed.png", "output_relative_path": "derivatives/out.png"}],
                    },
                    "guardrail_warnings": {
                        "count": 1,
                        "records": [{"reviewer_notes": "restricted note"}],
                    },
                },
            }
            provider_probe = {"configured": True, "provider_count": 2, "provider_names": ["private-local"]}

            summary = build_deep_inspection_candidate_summary(
                scan_report=scan_report,
                processing_review_package=processing_review_package,
                provider_probe=provider_probe,
            )
            raw = json.dumps(summary, ensure_ascii=False)

            self.assertEqual(summary["schema_version"], "scan-qc.deep-inspection-candidates.v1")
            self.assertEqual(summary["candidate_total"], 6)
            self.assertEqual(summary["candidates_by_severity"]["P1"], 1)
            self.assertEqual(summary["candidates_by_severity"]["P2"], 1)
            self.assertEqual(summary["candidates_by_reason"]["rule_bucket:quality"], 1)
            self.assertEqual(summary["candidates_by_reason"]["rule_bucket:provider"], 1)
            self.assertEqual(summary["candidates_by_reason"]["processing_review_status:failed"], 1)
            self.assertEqual(summary["candidates_by_reason"]["processing_review_status:needs_review"], 2)
            self.assertTrue(summary["provider_configured"])
            self.assertEqual(summary["provider_count"], 2)
            self.assertTrue(summary["no_inference_run"])
            self.assertEqual(summary["privacy_status"], "aggregate_public_safe")
            for forbidden in (
                "/Users/private/archive",
                "secret_page_001.png",
                "secret_page_002.png",
                "private_failed.png",
                "derivatives/out.png",
                "0123456789abcdef",
                "OCR text",
                "data:image",
                "row-001-private",
                "candidate-private-002",
                "restricted note",
                "processing_manifest.json",
                "private-local",
            ):
                self.assertNotIn(forbidden, raw)

    def test_deep_inspection_candidate_summary_cli_writes_privacy_safe_json(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                scan_path = temp / "scan_qc_report.json"
                review_path = temp / "processing_review_package.json"
                out_dir = temp / "summary"
                scan_path.write_text(
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "relative_path": "/Users/private/archive/secret_scan.png",
                                    "rule": "manifest_missing_file",
                                    "severity": "P0",
                                    "source": "manifest",
                                    "confidence": 1.0,
                                    "message": "private path /Users/private/archive/secret_scan.png",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                review_path.write_text(
                    json.dumps({"summary": {"failed_files": 1, "status_counts": {"failed": 1}}, "files": []}),
                    encoding="utf-8",
                )
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "deep-inspection-candidate-summary",
                            "--scan-report",
                            str(scan_path),
                            "--processing-review-package",
                            str(review_path),
                            "--out",
                            str(out_dir),
                        ]
                    )
                payload = json.loads((out_dir / "deep_inspection_candidate_summary.json").read_text(encoding="utf-8"))
                raw = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(exit_code, 0)
            self.assertIn("No inference run: true", stdout.getvalue())
            self.assertEqual(payload["candidate_total"], 2)
            self.assertEqual(payload["candidates_by_severity"]["P0"], 1)
            self.assertFalse(payload["provider_configured"])
            self.assertNotIn("/Users/private/archive", raw)
            self.assertNotIn("secret_scan.png", raw)

    def test_deep_inspection_candidate_summary_counts_groups_only_processing_review(self) -> None:
            processing_review_package = {
                "groups": {
                    "failed": {
                        "count": 2,
                        "records": [
                            {"source_relative_path": "private_failed_001.png"},
                            {"output_relative_path": "derivatives/private_failed_002.png"},
                        ],
                    },
                    "guardrail_warnings": {
                        "count": 1,
                        "records": [{"reviewer_notes": "restricted note for curator"}],
                    },
                }
            }

            summary = build_deep_inspection_candidate_summary(processing_review_package=processing_review_package)
            raw = json.dumps(summary, ensure_ascii=False)

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["candidate_total"], 3)
            self.assertEqual(summary["candidates_by_reason"]["processing_review_group:failed"], 2)
            self.assertEqual(summary["candidates_by_reason"]["processing_review_group:guardrail_warnings"], 1)
            self.assertTrue(summary["privacy"]["aggregate_only"])
            for forbidden in (
                "private_failed_001.png",
                "private_failed_002.png",
                "derivatives/private_failed_002.png",
                "restricted note",
            ):
                self.assertNotIn(forbidden, raw)


if __name__ == "__main__":
    unittest.main()
