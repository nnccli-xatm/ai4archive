from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import csv
from pathlib import Path

from PIL import Image

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
            self.assertTrue(paths["files_csv"].exists())
            self.assertTrue(paths["findings_csv"].exists())

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual(saved["project"]["project_id"], "p1")

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

    def test_imports_catalog_and_flags_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            catalog_path = root / "catalog.csv"
            input_dir.mkdir()

            Image.new("RGB", (12, 12), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (12, 12), "white").save(input_dir / "extra.jpg", dpi=(300, 300))

            with catalog_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["relative_path", "archive_id"])
                writer.writeheader()
                writer.writerow({"relative_path": "A001_0001.jpg", "archive_id": "A001"})
                writer.writerow({"relative_path": "missing.jpg", "archive_id": "A002"})

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, catalog_path=catalog_path))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertEqual(report["summary"]["catalog_records"], 2)
            self.assertEqual(report["catalog"]["path_field"], "relative_path")
            self.assertIn("catalog_file_missing", rules)
            self.assertIn("catalog_unmatched_file", rules)


if __name__ == "__main__":
    unittest.main()
