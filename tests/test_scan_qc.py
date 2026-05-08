from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from archive_scan_qc.cli import main
from archive_scan_qc.reports import write_reports
from archive_scan_qc.scanner import ScanConfig, scan_batch


class ScanQcTest(unittest.TestCase):
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

            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("<!doctype html>", html)
            self.assertIn("Scan QC Report", html)
            self.assertIn("Total Files", html)
            self.assertIn("A001_0002.png", html)
            self.assertIn("dpi_minimum", html)
            self.assertIn("P0", html)

    def test_flags_unopenable_duplicate_names_and_duplicate_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            nested_dir = input_dir / "nested"
            nested_dir.mkdir(parents=True)

            source = input_dir / "DUP_0001.jpg"
            Image.new("RGB", (12, 12), "white").save(source, dpi=(300, 300))
            shutil.copyfile(source, nested_dir / "DUP_0001.jpg")
            (input_dir / "broken.jpg").write_text("not an image", encoding="utf-8")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertIn("openability", rules)
            self.assertIn("duplicate_name", rules)
            self.assertIn("duplicate_file", rules)
            self.assertGreaterEqual(report["summary"]["p0_findings"], 3)

    def test_manifest_flags_missing_unexpected_and_duplicate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            manifest_csv = root / "manifest.csv"

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0002.jpg", dpi=(300, 300))
            manifest_csv.write_text(
                "relative_path\n"
                "A001_0001.jpg\n"
                "A001_0001.jpg\n"
                "A001_0003.jpg\n",
                encoding="utf-8",
            )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, manifest_csv=manifest_csv))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertIn("manifest_missing_file", rules)
            self.assertIn("manifest_unexpected_file", rules)
            self.assertIn("manifest_duplicate_entry", rules)
            self.assertEqual(report["summary"]["manifest_entry_count"], 3)
            self.assertEqual(report["summary"]["manifest_unique_entry_count"], 2)
            self.assertEqual(report["summary"]["manifest_missing_count"], 1)
            self.assertEqual(report["summary"]["manifest_unexpected_count"], 1)
            self.assertEqual(report["summary"]["manifest_duplicate_count"], 1)
            self.assertTrue(report["manifest"]["manifest_used"])
            self.assertEqual(report["manifest"]["manifest_entry_count"], 3)

            paths = write_reports(report, output_dir)
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Manifest Entries", html)
            self.assertIn("Manifest Missing", html)
            self.assertIn("manifest_unexpected_file", html)

    def test_cli_accepts_manifest_and_returns_one_for_p0_manifest_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            manifest_csv = root / "manifest.csv"

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path\nA001_0002.jpg\n", encoding="utf-8")

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--manifest-csv",
                    str(manifest_csv),
                ]
            )

            self.assertEqual(exit_code, 1)
            saved = json.loads((output_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["manifest"]["manifest_used"])
            self.assertEqual(saved["summary"]["manifest_missing_count"], 1)
            self.assertEqual(saved["summary"]["manifest_unexpected_count"], 1)

    def test_output_dir_inside_input_is_skipped_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = input_dir / "reports"
            input_dir.mkdir()

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            first_report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            write_reports(first_report, output_dir)
            second_report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))

            scanned_paths = {item["relative_path"] for item in second_report["files"]}
            self.assertEqual(scanned_paths, {"A001_0001.jpg"})
            self.assertEqual(second_report["summary"]["total_files"], 1)
            self.assertEqual(second_report["summary"]["skipped_output_directory_count"], 1)
            self.assertFalse(any(path.startswith("reports/") for path in scanned_paths))

    def test_manifest_csv_inside_input_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            manifest_csv = input_dir / "manifest.csv"

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path\nA001_0001.jpg\n", encoding="utf-8")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, manifest_csv=manifest_csv))

            scanned_paths = {item["relative_path"] for item in report["files"]}
            rules = {finding["rule"] for finding in report["findings"]}
            self.assertEqual(scanned_paths, {"A001_0001.jpg"})
            self.assertNotIn("unsupported_format", rules)
            self.assertEqual(report["summary"]["manifest_unexpected_count"], 0)
            self.assertEqual(report["summary"]["skipped_manifest_file_count"], 1)

    def test_hidden_directories_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            hidden_dir = input_dir / ".cache"
            hidden_dir.mkdir(parents=True)

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (32, 24), "white").save(hidden_dir / "A001_0002.jpg", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))

            scanned_paths = {item["relative_path"] for item in report["files"]}
            self.assertEqual(scanned_paths, {"A001_0001.jpg"})
            self.assertEqual(report["summary"]["skipped_hidden_directory_count"], 1)
            self.assertEqual(report["summary"]["skipped_directory_count"], 1)


if __name__ == "__main__":
    unittest.main()
