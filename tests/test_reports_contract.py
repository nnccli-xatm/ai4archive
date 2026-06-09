from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from archive_scan_qc.reports import build_review_summary, write_reports, write_review_export, write_review_summary
from archive_scan_qc.scanner import ScanConfig, scan_batch


class ReportsContractTests(unittest.TestCase):
    def test_collects_metadata_and_writes_reports(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                input_dir.mkdir()

                Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
                Image.new("RGB", (16, 16), "white").save(input_dir / "A001_0002.png", dpi=(150, 150))

                report = scan_batch(
                    ScanConfig(
                        project_id="p1",
                        batch_id="b1",
                        input_dir=input_dir,
                        output_dir=output_dir,
                        min_dpi=200,
                        name_pattern=r"A001_\d{4}",
                    )
                )
                paths = write_reports(report, output_dir)

                self.assertEqual(report["summary"]["total_files"], 2)
                self.assertEqual(report["summary"]["openable_files"], 2)
                self.assertTrue(any(finding["rule"] == "dpi_minimum" for finding in report["findings"]))
                self.assertTrue(paths["json"].exists())
                self.assertTrue(paths["html"].exists())
                self.assertTrue(paths["files_csv"].exists())
                self.assertTrue(paths["findings_csv"].exists())

                saved = json.loads(paths["json"].read_text(encoding="utf-8"))
                self.assertEqual(saved["project"]["project_id"], "p1")
                self.assertEqual(saved["manifest"]["project_id"], "p1")
                self.assertEqual(saved["manifest"]["batch_id"], "b1")
                self.assertEqual(saved["manifest"]["rule_version"], "scan-qc.phase1.v1")
                self.assertEqual(saved["manifest"]["total_files"], 2)
                self.assertEqual(saved["manifest"]["p0_findings"], report["summary"]["p0_findings"])
                self.assertFalse(saved["manifest"]["manifest_used"])
                self.assertIn("performance", saved["summary"])
                self.assertIn("performance", saved["manifest"])
                self.assertEqual(saved["summary"]["performance"]["total_files"], 2)
                self.assertEqual(saved["summary"]["performance"]["openable_files"], 2)
                self.assertIn("effective_workers", saved["summary"]["performance"])
                self.assertIn(saved["summary"]["performance"]["mode"], {"serial", "parallel"})
                self.assertGreaterEqual(saved["summary"]["performance"]["elapsed_seconds"], 0)
                self.assertGreaterEqual(saved["summary"]["performance"]["files_per_minute"], 0)
                self.assertGreaterEqual(saved["summary"]["performance"]["openable_files_per_minute"], 0)

                html = paths["html"].read_text(encoding="utf-8")
                self.assertIn("<!doctype html>", html)
                self.assertIn("Scan QC Report", html)
                self.assertIn("Total Files", html)
                self.assertIn("A001_0002.png", html)
                self.assertIn("dpi_minimum", html)
                self.assertIn("P0", html)
                self.assertIn("Schema Version", html)
                self.assertIn("Project", html)
                self.assertIn("Dependency Notes", html)
                self.assertIn("Rules Profile", html)
                self.assertIn("Performance Metrics", html)
                self.assertIn("Skipped Inputs", html)
                self.assertIn("Manifest Consistency", html)
                self.assertIn("Quality Metrics", html)
                self.assertIn("Orientation And Blank Pages", html)
                self.assertIn("Findings Summary", html)
                self.assertIn("Rule Catalog", html)
                self.assertIn("Image openability", html)
                self.assertIn("Brightness Mean Avg", html)
                self.assertIn("EXIF Transpose Signals", html)
                self.assertIn("Manifest Unique Entries", html)
                self.assertIn("SHA256", html)
                self.assertIn(saved["files"][0]["sha256"], html)
                self.assertIn("Complete Report Data", html)
                self.assertIn('id="scan-qc-report-data"', html)
                self.assertNotIn("<img", html.lower())
                self.assertNotIn("data:image", html.lower())
                self.assertNotIn("src=", html.lower())

    def test_html_report_escapes_embedded_data_and_has_no_remote_resources(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                input_dir.mkdir()

                Image.new("RGB", (32, 24), "white").save(input_dir / "safe_name.png")

                report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
                report["files"][0]["relative_path"] = '<img src=x>.png'
                paths = write_reports(report, output_dir)
                html = paths["html"].read_text(encoding="utf-8")
                lower_html = html.lower()

                self.assertIn("&lt;img", lower_html)
                self.assertIn("\\u003cimg", lower_html)
                self.assertNotIn("<img", lower_html)
                self.assertNotIn("data:image", lower_html)
                self.assertNotIn('src="http', lower_html)
                self.assertNotIn("src='http", lower_html)

    def test_review_export_template_fields_are_stable(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                report_path = root / "scan_qc_report.json"
                csv_path = root / "review_template.csv"
                json_path = root / "review_template.json"
                report_path.write_text(
                    json.dumps(
                        {
                            "findings": [
                                {
                                    "relative_path": "private/A001_0001.tif",
                                    "rule": "dpi_minimum",
                                    "severity": "P0",
                                    "message": "private detail",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

                write_review_export(report_path, csv_path)
                write_review_export(report_path, json_path)

                with csv_path.open("r", newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    rows = list(reader)
                self.assertEqual(
                    reader.fieldnames,
                    ["finding_id", "rule", "severity", "relative_path", "status", "reviewer_notes"],
                )
                self.assertEqual(rows[0]["finding_id"], "F000001")
                self.assertEqual(rows[0]["status"], "pending")
                self.assertEqual(rows[0]["relative_path"], "private/A001_0001.tif")

                payload = json.loads(json_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["schema_version"], "scan-qc.review-template.v1")
                self.assertEqual(payload["findings"][0]["finding_id"], "F000001")
                self.assertIn("allowed_statuses", payload)

    def test_review_summary_counts_statuses_and_remaining_p0_p1(self) -> None:
            summary = build_review_summary(
                [
                    {"finding_id": "F000001", "rule": "dpi_minimum", "severity": "P0", "status": "fixed"},
                    {"finding_id": "F000002", "rule": "openability", "severity": "P0", "status": "accepted"},
                    {"finding_id": "F000003", "rule": "quality_too_dark", "severity": "P1", "status": "needs_rescan"},
                    {"finding_id": "F000004", "rule": "quality_low_contrast", "severity": "P2", "status": "false_positive"},
                ]
            )

            self.assertEqual(summary["total_findings"], 4)
            self.assertEqual(summary["severity_counts"], {"P0": 2, "P1": 1, "P2": 1})
            self.assertEqual(summary["rule_counts"]["dpi_minimum"], 1)
            self.assertEqual(summary["status_counts"]["fixed"], 1)
            self.assertEqual(summary["status_counts"]["accepted"], 1)
            self.assertEqual(summary["status_counts"]["needs_rescan"], 1)
            self.assertEqual(summary["remaining_p0"], 1)
            self.assertEqual(summary["remaining_p1"], 1)
            self.assertEqual(summary["manually_handled_count"], 4)
            self.assertEqual(
                summary["closure_gate_summary"],
                {
                    "open_p0_count": 1,
                    "open_p1_count": 1,
                    "manually_handled_count": 4,
                    "can_complete_delivery": False,
                    "operator_message_zh": "还有需要重扫/重新处理的图片，先处理后再完成导出。",
                },
            )
            self.assertFalse(summary["acceptance_passed"])

    def test_review_summary_is_aggregate_only_and_does_not_leak_private_values(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                review_path = root / "review_template.csv"
                summary_path = root / "review_summary.json"
                review_path.write_text(
                    "finding_id,rule,severity,relative_path,status,reviewer_notes,sha256\n"
                    "F000001,dpi_minimum,P0,private/name_001.tif,fixed,contains private note,abc123\n",
                    encoding="utf-8",
                )

                write_review_summary(review_path, summary_path)
                raw = summary_path.read_text(encoding="utf-8")

                self.assertIn("scan-qc.review-summary.v1", raw)
                for forbidden in ["private/name_001.tif", "contains private note", "abc123", "relative_path", "sha256"]:
                    self.assertNotIn(forbidden, raw)
                summary = json.loads(raw)
                self.assertTrue(summary["acceptance_passed"])
                self.assertEqual(summary["remaining_p0"], 0)
                self.assertTrue(summary["closure_gate_summary"]["can_complete_delivery"])

    def test_review_summary_rejects_invalid_status_with_clear_error(self) -> None:
            with self.assertRaisesRegex(ValueError, "Invalid review status 'done'.*expected one of"):
                build_review_summary(
                    [
                        {
                            "finding_id": "F000001",
                            "rule": "dpi_minimum",
                            "severity": "P0",
                            "status": "done",
                        }
                    ]
                )

    def test_empty_review_findings_generate_passing_summary(self) -> None:
            summary = build_review_summary([])

            self.assertEqual(summary["total_findings"], 0)
            self.assertEqual(summary["remaining_p0"], 0)
            self.assertEqual(summary["remaining_p1"], 0)
            self.assertTrue(summary["acceptance_passed"])


if __name__ == "__main__":
    unittest.main()
