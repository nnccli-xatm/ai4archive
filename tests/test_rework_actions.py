from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.rework import build_rework_action_list, write_rework_action_list


class ReworkActionTests(unittest.TestCase):
    def test_rework_action_list_groups_qc_findings_and_processing_retry(self) -> None:
            report = {
                "schema_version": "scan-qc.phase1.v1",
                "project": {"project_id": "p1", "batch_id": "b1"},
                "summary": {"total_findings": 5, "p0_findings": 1, "p1_findings": 3, "p2_findings": 1},
                "findings": [
                    {
                        "relative_path": "001_rescan.png",
                        "rule": "openable",
                        "severity": "P0",
                        "source": "scanner",
                        "confidence": 1.0,
                        "message": "cannot open source image",
                    },
                    {
                        "relative_path": "002_process.png",
                        "rule": "quality_suspected_blur",
                        "severity": "P1",
                        "source": "scanner",
                        "confidence": 0.8,
                        "message": "blur candidate for derivative processing",
                    },
                    {
                        "relative_path": "003_manifest.png",
                        "rule": "manifest_duplicate_sequence",
                        "severity": "P1",
                        "source": "manifest",
                        "confidence": 1.0,
                        "message": "duplicate sequence value",
                    },
                    {
                        "relative_path": "004_manual.png",
                        "rule": "provider_needs_operator_check",
                        "severity": "P1",
                        "source": "provider",
                        "confidence": 0.6,
                        "message": "provider requested local review",
                    },
                    {
                        "relative_path": "005_info.png",
                        "rule": "name_pattern",
                        "severity": "P2",
                        "source": "scanner",
                        "confidence": 1.0,
                        "message": "filename does not match pattern",
                    },
                ],
            }
            retry_manifest = {
                "schema_version": "scan-qc.processing.retry.v1",
                "summary": {"failed_files": 1, "retry_list_files": 1},
                "files": [
                    {
                        "source_relative_path": "006_retry.png",
                        "source_sha256": "abc123",
                        "status": "failed",
                        "failure_reason": "source image is not openable",
                        "error": "cannot identify image file",
                    }
                ],
            }

            payload = build_rework_action_list(report, processing_retry_manifest=retry_manifest)

            by_path = {action["relative_path"]: action for action in payload["actions"]}
            self.assertEqual(by_path["001_rescan.png"]["action_type"], "rescan_required")
            self.assertEqual(by_path["002_process.png"]["action_type"], "reprocess_candidate")
            self.assertEqual(by_path["003_manifest.png"]["action_type"], "duplicate_manifest_correction")
            self.assertEqual(by_path["004_manual.png"]["action_type"], "manual_review")
            self.assertEqual(by_path["005_info.png"]["action_type"], "informational_follow_up")
            self.assertEqual(by_path["006_retry.png"]["action_type"], "processing_retry")
            self.assertEqual(by_path["006_retry.png"]["processing_retry_evidence"][0]["source_sha256"], "abc123")
            self.assertEqual(payload["summary"]["actions_by_type"]["processing_retry"], 1)
            self.assertEqual(payload["summary"]["actions_by_priority"]["P0"], 2)
            self.assertTrue(payload["privacy"]["local_only"])
            self.assertFalse(payload["privacy"]["contains_thumbnails"])
            self.assertFalse(payload["privacy"]["contains_image_content"])
            self.assertEqual(payload["summary"]["processing"]["full_chain_cleanup_quality"]["status"], "unknown")

    def test_rework_action_list_includes_aggregate_full_chain_cleanup_quality_when_present(self) -> None:
            report = {
                "schema_version": "scan-qc.phase1.v1",
                "summary": {"total_findings": 0, "p0_findings": 0, "p1_findings": 0, "p2_findings": 0},
                "findings": [],
            }
            payload = build_rework_action_list(
                report,
                processing_audit_summary={
                    "schema_version": "scan-qc.processing-audit.v1",
                    "privacy": {"aggregate_only": True},
                    "quality_signals": {
                        "full_chain_cleanup": {
                            "total_files": 8,
                            "improved_files": 5,
                            "preserved_files": 2,
                            "reverted_files": 1,
                            "skipped_files": 1,
                            "improved_ratio": 0.625,
                            "preserved_ratio": 0.25,
                            "reverted_ratio": 0.125,
                            "skipped_ratio": 0.125,
                        }
                    },
                },
            )

            quality = payload["summary"]["processing"]["full_chain_cleanup_quality"]
            self.assertTrue(quality["provided"])
            self.assertEqual(quality["status"], "available")
            self.assertEqual(quality["counts"]["improved_files"], 5)
            self.assertEqual(quality["ratios"]["improved_ratio"], 0.625)

    def test_rework_action_list_writes_deterministic_json_and_csv(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                report_path = temp / "scan_qc_report.json"
                retry_path = temp / "processing_retry_manifest.json"
                out_path = temp / "rework_action_list.json"
                csv_path = temp / "rework_action_list.csv"
                report_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "scan-qc.phase1.v1",
                            "summary": {"total_findings": 2, "p0_findings": 0, "p1_findings": 1, "p2_findings": 1},
                            "findings": [
                                {
                                    "relative_path": "b.png",
                                    "rule": "name_pattern",
                                    "severity": "P2",
                                    "source": "scanner",
                                    "confidence": 1.0,
                                    "message": "bad name",
                                },
                                {
                                    "relative_path": "a.png",
                                    "rule": "quality_too_dark",
                                    "severity": "P1",
                                    "source": "scanner",
                                    "confidence": 1.0,
                                    "message": "too dark",
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                retry_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "scan-qc.processing.retry.v1",
                            "summary": {"retry_list_files": 1},
                            "files": [
                                {
                                    "source_relative_path": "c.png",
                                    "source_sha256": "def456",
                                    "status": "failed",
                                    "failure_reason": "guardrail failure",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

                _, _, payload = write_rework_action_list(report_path, out_path, processing_retry_manifest_path=retry_path, csv_path=csv_path)
                with csv_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))

                self.assertEqual([action["action_id"] for action in payload["actions"]], ["RA000001", "RA000002", "RA000003"])
                self.assertEqual([action["relative_path"] for action in payload["actions"]], ["c.png", "a.png", "b.png"])
                self.assertEqual(rows[0]["action_type"], "processing_retry")
                self.assertEqual(rows[1]["action_type"], "reprocess_candidate")
                self.assertEqual(rows[2]["action_type"], "informational_follow_up")
                self.assertIn("LOCAL-ONLY SENSITIVE EVIDENCE", out_path.read_text(encoding="utf-8"))
                self.assertNotIn("thumbnail", json.dumps(payload["actions"]))


if __name__ == "__main__":
    unittest.main()
