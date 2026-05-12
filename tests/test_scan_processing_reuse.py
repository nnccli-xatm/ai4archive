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
from archive_scan_qc.processing_plan import build_processing_plan
from archive_scan_qc.scanner import ScanConfig, scan_batch


REPO_ROOT = Path(__file__).resolve().parents[1]
AGGREGATE_BASELINE_PATH = REPO_ROOT / "scripts" / "run_aggregate_baseline.py"


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

    def test_dark_border_is_recomputed_after_reused_deskew_changes_coordinates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-reuse-deskew-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))

            with (
                mock.patch(
                    "archive_scan_qc.processing._scan_measurements_for_processing",
                    return_value={
                        "skew": processing_module.SkewDetection(1.0, 1.0, "synthetic reusable skew"),
                        "dark_border": processing_module.DarkBorderDetection((1, 1, 40, 40), "synthetic stale bbox"),
                    },
                ),
                mock.patch(
                    "archive_scan_qc.processing._detect_dark_border_bbox",
                    return_value=processing_module.DarkBorderDetection((2, 2, 90, 70), "recomputed after deskew"),
                ) as dark_border,
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    root / "processed",
                    ProcessingOptions(trim_dark_border=True, deskew=True, reuse_scan_measurements=True, workers=1),
                )

            dark_border.assert_called_once()
            record = manifest["files"][0]
            self.assertTrue(record["deskewed"])
            self.assertTrue(record["scan_measurements_reused"])
            self.assertIn("skew_detect_reused_scan_measurement", record["operations"])
            self.assertNotIn("dark_border_detect_reused_scan_measurement", record["operations"])
            reuse = manifest["summary"]["performance"]["scan_measurement_reuse"]
            self.assertEqual(reuse["operations_skipped"]["deskew"], 1)
            self.assertEqual(reuse["fallback_operations"]["trim_dark_border"], 1)

    def test_cli_entrypoints_accept_reuse_scan_measurements(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-reuse-cli-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            metadata_dir = root / "metadata"
            production_derivatives = root / "production-derivatives"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(report_dir),
                    "--process-out",
                    str(process_dir),
                    "--trim-dark-border",
                    "--reuse-scan-measurements",
                    "--workers",
                    "1",
                ]
            )
            self.assertEqual(exit_code, 0)
            manifest = json.loads((process_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["summary"]["performance"]["scan_measurement_reuse"]["files_with_any_reuse"], 1)

            production_exit_code = main(
                [
                    "production-run",
                    "--input",
                    str(input_dir),
                    "--derivatives-out",
                    str(production_derivatives),
                    "--metadata-out",
                    str(metadata_dir),
                    "--trim-dark-border",
                    "--reuse-scan-measurements",
                    "--workers",
                    "1",
                ]
            )
            self.assertIn(production_exit_code, {0, 1})
            production_manifest = json.loads(
                (production_derivatives / "processing_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                production_manifest["summary"]["performance"]["scan_measurement_reuse"]["files_with_any_reuse"],
                1,
            )

    def test_aggregate_baseline_reports_scan_measurement_reuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-reuse-baseline-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "aggregate"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))

            module = _load_aggregate_baseline_module()
            args = module.build_parser().parse_args(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers",
                    "1",
                    "--process-images",
                    "--deskew",
                    "--trim-dark-border",
                    "--reuse-scan-measurements",
                    "--skip-benchmark",
                    "--cleanup-artifacts",
                ]
            )
            payload = module.run_aggregate_baseline(args)

            self.assertTrue(payload["operations"]["reuse_scan_measurements"])
            self.assertEqual(payload["aggregate_counts"]["processing_scan_measurement_reused_files"], 1)
            self.assertTrue(payload["privacy_self_check"]["passed"])

    def test_processing_plan_reuses_scan_measurements_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-reuse-plan-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            with (
                mock.patch("archive_scan_qc.processing._detect_skew", wraps=processing_module._detect_skew) as skew,
                mock.patch(
                    "archive_scan_qc.processing._detect_dark_border_bbox",
                    wraps=processing_module._detect_dark_border_bbox,
                ) as dark_border,
            ):
                plan = build_processing_plan(
                    report,
                    input_dir,
                    ProcessingOptions(trim_dark_border=True, deskew=True, reuse_scan_measurements=True, workers=1),
                )

            self.assertEqual(skew.call_count, 0)
            self.assertEqual(dark_border.call_count, 0)
            self.assertTrue(plan["operations"]["reuse_scan_measurements"])
            record = plan["files"][0]
            self.assertTrue(record["processing_audit"])
            self.assertIn("skew_detect_reused_scan_measurement", record["proposed_operations"])
            self.assertIn("dark_border_detect_reused_scan_measurement", record["proposed_operations"])

    def test_processing_plan_cli_accepts_reuse_scan_measurements(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-reuse-plan-cli-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            report_dir = root / "scan"
            plan_dir = root / "plan"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, report_dir, workers=1))
            report_dir.mkdir()
            report_path = report_dir / "scan_qc_report.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

            exit_code = main(
                [
                    "processing-plan",
                    "--report",
                    str(report_path),
                    "--input",
                    str(input_dir),
                    "--out",
                    str(plan_dir),
                    "--deskew",
                    "--trim-dark-border",
                    "--reuse-scan-measurements",
                ]
            )

            self.assertEqual(exit_code, 0)
            plan = json.loads((plan_dir / "processing_plan.json").read_text(encoding="utf-8"))
            self.assertTrue(plan["operations"]["reuse_scan_measurements"])
            self.assertTrue(plan["privacy"]["contains_file_list"])


def _dark_border_page() -> Image.Image:
    image = Image.new("RGB", (100, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 99, 79), outline="black", width=5)
    draw.rectangle((25, 30, 75, 45), fill="black")
    return image


def _load_aggregate_baseline_module():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("run_aggregate_baseline", AGGREGATE_BASELINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load run_aggregate_baseline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
