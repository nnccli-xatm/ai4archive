from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import archive_scan_qc.processing as processing_module
from archive_scan_qc.cli import main
from archive_scan_qc.processing import ProcessingOptions, process_images
from archive_scan_qc.scanner import ScanConfig, scan_batch


class ScanProcessingReuseTest(unittest.TestCase):
    def test_reused_scan_measurements_keep_processing_decisions_and_guardrails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-reuse-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            baseline = process_images(
                report,
                input_dir,
                root / "baseline",
                ProcessingOptions(trim_dark_border=True, deskew=True, workers=1),
            )
            reused = process_images(
                report,
                input_dir,
                root / "reused",
                ProcessingOptions(trim_dark_border=True, deskew=True, reuse_scan_measurements=True, workers=1),
            )

            baseline_record = baseline["files"][0]
            reused_record = reused["files"][0]
            self.assertEqual(reused_record["status"], baseline_record["status"])
            self.assertEqual(reused_record["deskewed"], baseline_record["deskewed"])
            self.assertEqual(reused_record["skew_angle_degrees"], baseline_record["skew_angle_degrees"])
            self.assertEqual(reused_record["dark_border_trimmed"], baseline_record["dark_border_trimmed"])
            self.assertEqual(reused_record["dark_border_bbox"], baseline_record["dark_border_bbox"])
            self.assertEqual(reused_record["processing_audit"]["guardrail_failures"], [])
            self.assertEqual(reused_record["output_sha256"], baseline_record["output_sha256"])
            self.assertTrue(reused_record["scan_measurements_reused"])

            reuse = reused["summary"]["performance"]["scan_measurement_reuse"]
            self.assertEqual(reuse["files_with_any_reuse"], 1)
            self.assertEqual(reuse["operations_skipped"]["deskew"], 1)
            self.assertEqual(reuse["operations_skipped"]["trim_dark_border"], 1)
            self.assertEqual(
                reused["summary"]["performance"]["operation_timings"]["deskew"]["reused_scan_measurement_files"],
                1,
            )

    def test_partial_scan_measurements_fall_back_to_current_detection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-reuse-fallback-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            report["files"][0]["quality_skew_reason"] = None
            report["files"][0]["quality_dark_border_bbox"] = [1, 2]

            with (
                mock.patch("archive_scan_qc.processing._detect_skew", wraps=processing_module._detect_skew) as skew,
                mock.patch(
                    "archive_scan_qc.processing._detect_dark_border_bbox",
                    wraps=processing_module._detect_dark_border_bbox,
                ) as dark_border,
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    root / "processed",
                    ProcessingOptions(trim_dark_border=True, deskew=True, reuse_scan_measurements=True, workers=1),
                )

            self.assertGreaterEqual(skew.call_count, 1)
            self.assertGreaterEqual(dark_border.call_count, 1)
            record = manifest["files"][0]
            self.assertFalse(record["scan_measurements_reused"])
            reuse = manifest["summary"]["performance"]["scan_measurement_reuse"]
            self.assertEqual(reuse["files_with_any_reuse"], 0)
            self.assertEqual(reuse["fallback_operations"]["deskew"], 1)
            self.assertEqual(reuse["fallback_operations"]["trim_dark_border"], 1)

    def test_benchmark_reports_aggregate_scan_measurement_reuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-reuse-benchmark-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "benchmark"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))

            exit_code = main(
                [
                    "benchmark",
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers-list",
                    "1",
                    "--repeats",
                    "1",
                    "--deskew",
                    "--trim-dark-border",
                    "--reuse-scan-measurements",
                ]
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads((output_dir / "benchmark_results.json").read_text(encoding="utf-8"))
            run = payload["runs"][0]
            self.assertTrue(run["operations"]["reuse_scan_measurements"])
            self.assertEqual(run["processing"]["scan_measurement_reuse"]["files_with_any_reuse"], 1)
            self.assertEqual(
                run["processing"]["operation_timings"]["trim_dark_border"]["reused_scan_measurement_files"],
                1,
            )
            csv_text = (output_dir / "benchmark_results.csv").read_text(encoding="utf-8")
            self.assertIn("processing_scan_measurement_reused_files", csv_text)


def _dark_border_page() -> Image.Image:
    image = Image.new("RGB", (100, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 99, 79), outline="black", width=5)
    draw.rectangle((25, 30, 75, 45), fill="black")
    return image


if __name__ == "__main__":
    unittest.main()
