from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import archive_scan_qc.processing as processing_module
from archive_scan_qc.cli import main
from archive_scan_qc.processing import ProcessingOptions, aggregate_processing_reuse_precheck, process_images
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

    def test_safe_deskew_skip_uses_scan_no_candidate_measurement_without_full_reuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-safe-deskew-skip-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (120, 90), "white").save(input_dir / "blank.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            fallback_report = json.loads(json.dumps(report))
            fallback_report["files"][0]["quality_skew_angle_degrees"] = None
            fallback_report["files"][0]["quality_skew_confidence"] = None
            fallback_report["files"][0]["quality_skew_reason"] = None
            fallback_manifest = process_images(
                fallback_report,
                input_dir,
                root / "fallback",
                ProcessingOptions(deskew=True, workers=1),
            )
            with mock.patch("archive_scan_qc.processing._detect_skew", side_effect=AssertionError("unsafe fallback")):
                manifest = process_images(
                    report,
                    input_dir,
                    root / "processed",
                    ProcessingOptions(deskew=True, workers=1),
                )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["deskewed"])
            self.assertIn(record["deskew_reason"], {"blank page", "low contrast"})
            self.assertEqual(record["output_sha256"], fallback_manifest["files"][0]["output_sha256"])
            self.assertEqual(record["processing_audit"], fallback_manifest["files"][0]["processing_audit"])
            self.assertIn("deskew_safe_skip_scan_measurement", record["operations"])
            self.assertTrue(record["scan_measurements_reused"])
            timing = manifest["summary"]["performance"]["operation_timings"]["deskew"]
            self.assertEqual(timing["reused_scan_measurement_files"], 1)
            self.assertEqual(timing["safe_skip_files"], 1)
            self.assertEqual(timing["projection_detection_files"], 0)
            self.assertEqual(timing["fallback_detection_files"], 0)
            self.assertEqual(manifest["summary"]["performance"]["scan_measurement_reuse"]["operations_skipped"]["deskew"], 1)
            self.assertEqual(manifest["summary"]["performance"]["scan_measurement_reuse"]["deskew_safe_skip_files"], 1)
            self.assertEqual(
                manifest["summary"]["performance"]["scan_measurement_reuse"]["deskew_projection_detection_files"],
                0,
            )
            self.assertEqual(
                manifest["summary"]["performance"]["scan_measurement_reuse"]["deskew_fallback_detection_files"],
                0,
            )
            audit = json.loads((root / "processed" / "processing_audit_summary.json").read_text(encoding="utf-8"))
            audit_text = json.dumps(audit, ensure_ascii=False, sort_keys=True)
            self.assertEqual(audit["counts"]["deskew_safe_skip_files"], 1)
            self.assertEqual(audit["counts"]["deskew_projection_detection_files"], 0)
            self.assertEqual(audit["counts"]["deskew_fallback_detection_files"], 0)
            self.assertEqual(audit["timing"]["operation_timings"]["deskew"]["safe_skip_files"], 1)
            self.assertEqual(audit["timing"]["scan_measurement_reuse"]["deskew_safe_skip_files"], 1)
            self.assertNotIn("blank.png", audit_text)
            self.assertNotIn(str(input_dir), audit_text)
            self.assertNotIn(manifest["files"][0]["source_sha256"], audit_text)

    def test_safe_deskew_skip_falls_back_when_scan_measurement_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-safe-deskew-fallback-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _text_page().save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            report["files"][0]["quality_skew_confidence"] = 0.01
            report["files"][0]["quality_skew_reason"] = "low confidence"

            with mock.patch("archive_scan_qc.processing._detect_skew", wraps=processing_module._detect_skew) as skew:
                manifest = process_images(
                    report,
                    input_dir,
                    root / "processed",
                    ProcessingOptions(deskew=True, workers=1),
                )

            self.assertGreaterEqual(skew.call_count, 1)
            record = manifest["files"][0]
            self.assertNotIn("deskew_safe_skip_scan_measurement", record["operations"])
            self.assertIn("skew_detect_projection", record["operations"])
            timing = manifest["summary"]["performance"]["operation_timings"]["deskew"]
            self.assertEqual(timing["safe_skip_files"], 0)
            self.assertEqual(timing["projection_detection_files"], 1)
            self.assertEqual(timing["fallback_detection_files"], 0)

    def test_reuse_deskew_fallback_reports_projection_detection_aggregate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-deskew-fallback-audit-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _text_page().save(input_dir / "private_page_name.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            report["files"][0]["quality_skew_reason"] = None

            with mock.patch("archive_scan_qc.processing._detect_skew", wraps=processing_module._detect_skew) as skew:
                manifest = process_images(
                    report,
                    input_dir,
                    root / "processed",
                    ProcessingOptions(deskew=True, reuse_scan_measurements=True, workers=1),
                )

            self.assertGreaterEqual(skew.call_count, 1)
            timing = manifest["summary"]["performance"]["operation_timings"]["deskew"]
            self.assertEqual(timing["safe_skip_files"], 0)
            self.assertEqual(timing["projection_detection_files"], 1)
            self.assertEqual(timing["fallback_detection_files"], 1)
            reuse = manifest["summary"]["performance"]["scan_measurement_reuse"]
            self.assertEqual(reuse["deskew_safe_skip_files"], 0)
            self.assertEqual(reuse["deskew_projection_detection_files"], 1)
            self.assertEqual(reuse["deskew_fallback_detection_files"], 1)
            audit = json.loads((root / "processed" / "processing_audit_summary.json").read_text(encoding="utf-8"))
            audit_text = json.dumps(audit, ensure_ascii=False, sort_keys=True)
            self.assertEqual(audit["counts"]["deskew_safe_skip_files"], 0)
            self.assertEqual(audit["counts"]["deskew_projection_detection_files"], 1)
            self.assertEqual(audit["counts"]["deskew_fallback_detection_files"], 1)
            self.assertEqual(audit["timing"]["operation_timings"]["deskew"]["fallback_detection_files"], 1)
            self.assertEqual(audit["timing"]["scan_measurement_reuse"]["deskew_fallback_detection_files"], 1)
            self.assertNotIn("private_page_name.png", audit_text)
            self.assertNotIn(str(input_dir), audit_text)

    def test_safe_deskew_skip_does_not_bypass_skew_risk_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-safe-deskew-risk-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _text_page().rotate(-3.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white").save(
                input_dir / "skew.png",
                dpi=(300, 300),
            )

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            with mock.patch("archive_scan_qc.processing._detect_skew", wraps=processing_module._detect_skew) as skew:
                manifest = process_images(
                    report,
                    input_dir,
                    root / "processed",
                    ProcessingOptions(deskew=True, workers=1),
                )

            self.assertGreaterEqual(skew.call_count, 1)
            record = manifest["files"][0]
            self.assertTrue(record["deskewed"])
            self.assertIn("skew_detect_projection", record["operations"])
            self.assertNotIn("deskew_safe_skip_scan_measurement", record["operations"])

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

    def test_production_run_rerun_reuses_completed_derivatives_by_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-production-reuse-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            derivatives_dir = root / "private-derivatives"
            metadata_dir = root / "private-metadata"
            input_dir.mkdir()
            source = input_dir / "private_page_name.png"
            Image.new("RGB", (80, 60), "white").save(source, dpi=(300, 300))
            original_sha = processing_module._sha256(source)
            args = [
                "production-run",
                "--input",
                str(input_dir),
                "--derivatives-out",
                str(derivatives_dir),
                "--metadata-out",
                str(metadata_dir),
                "--workers",
                "1",
            ]

            self.assertEqual(main(args), 0)
            first_manifest = json.loads((derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(first_manifest["summary"]["processed_files"], 1)
            self.assertEqual(first_manifest["summary"]["resumed_files"], 0)

            self.assertEqual(main(args), 0)
            second_manifest = json.loads((derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            production_summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))

            self.assertTrue(second_manifest["resume"]["enabled"])
            self.assertEqual(second_manifest["summary"]["processed_files"], 0)
            self.assertEqual(second_manifest["summary"]["resumed_files"], 1)
            self.assertEqual(second_manifest["summary"]["reprocessed_files"], 0)
            self.assertEqual(second_manifest["summary"]["failed_files"], 0)
            self.assertEqual(second_manifest["summary"]["existing_derivative_reused_files"], 1)
            self.assertEqual(production_summary["local_reuse_summary"]["reused_files"], 1)
            self.assertEqual(production_summary["local_reuse_summary"]["reprocessed_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["failed_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["remaining_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["total_files"], 1)
            self.assertIn("本批共 1 张", production_summary["local_reuse_summary"]["message_zh"])
            self.assertIn("复用 1 张", production_summary["local_reuse_summary"]["message_zh"])
            self.assertIn("重新处理 0 张", production_summary["local_reuse_summary"]["message_zh"])
            self.assertIn("仍失败 0 张", production_summary["local_reuse_summary"]["message_zh"])
            self.assertIn("剩余待处理 0 张", production_summary["local_reuse_summary"]["message_zh"])
            self.assertEqual(processing_module._sha256(source), original_sha)
            self.assertTrue(production_summary["stage_timings"]["aggregate_only"])
            self.assertEqual(
                [(stage["id"], stage["label_zh"], stage["status"]) for stage in production_summary["stage_timings"]["stages"]],
                [
                    ("scan", "检查扫描图片", "completed"),
                    ("process", "生成处理后图片", "completed"),
                    ("summarize", "整理处理结果", "completed"),
                ],
            )
            for stage in production_summary["stage_timings"]["stages"]:
                self.assertIsInstance(stage["elapsed_seconds"], float)
                self.assertGreaterEqual(stage["elapsed_seconds"], 0.0)

            public_reuse_text = json.dumps(production_summary["local_reuse_summary"], ensure_ascii=False, sort_keys=True)
            self.assertNotIn("private_page_name.png", public_reuse_text)
            self.assertNotIn(str(input_dir), public_reuse_text)
            self.assertNotIn(first_manifest["files"][0]["source_sha256"], public_reuse_text)
            self.assertEqual(
                set(production_summary["local_reuse_summary"]),
                {
                    "schema_version",
                    "aggregate_only",
                    "total_files",
                    "reused_files",
                    "reprocessed_files",
                    "failed_files",
                    "remaining_files",
                    "message_zh",
                },
            )

    def test_production_run_restart_only_fills_missing_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-production-partial-reuse-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            derivatives_dir = root / "private-derivatives"
            metadata_dir = root / "private-metadata"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "private_completed_page.png", dpi=(300, 300))
            Image.new("RGB", (80, 60), (230, 230, 230)).save(input_dir / "private_missing_page.png", dpi=(300, 300))
            args = [
                "production-run",
                "--input",
                str(input_dir),
                "--derivatives-out",
                str(derivatives_dir),
                "--metadata-out",
                str(metadata_dir),
                "--workers",
                "1",
            ]

            self.assertEqual(main(args), 0)
            first_manifest = json.loads((derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            first_records = {record["source_relative_path"]: record for record in first_manifest["files"]}
            completed_sha = first_records["private_completed_page.png"]["output_sha256"]
            missing_output = derivatives_dir / first_records["private_missing_page.png"]["output_relative_path"]
            missing_output.unlink()

            self.assertEqual(main(args), 0)
            second_manifest = json.loads((derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            production_summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            progress = json.loads((metadata_dir / "production_run_progress.json").read_text(encoding="utf-8"))
            records = {record["source_relative_path"]: record for record in second_manifest["files"]}

            self.assertEqual(records["private_completed_page.png"]["status"], "resumed")
            self.assertEqual(records["private_completed_page.png"]["output_sha256"], completed_sha)
            self.assertEqual(records["private_missing_page.png"]["status"], "processed")
            self.assertTrue(records["private_missing_page.png"]["reprocessed"])
            self.assertTrue(missing_output.exists())
            self.assertEqual(second_manifest["summary"]["processed_files"], 1)
            self.assertEqual(second_manifest["summary"]["resumed_files"], 1)
            self.assertEqual(second_manifest["summary"]["reprocessed_files"], 1)
            self.assertEqual(second_manifest["summary"]["existing_derivative_reused_files"], 1)
            self.assertEqual(second_manifest["summary"]["failed_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["reused_files"], 1)
            self.assertEqual(production_summary["local_reuse_summary"]["reprocessed_files"], 1)
            self.assertEqual(production_summary["local_reuse_summary"]["failed_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["remaining_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["total_files"], 2)
            self.assertIn("本批共 2 张", production_summary["local_reuse_summary"]["message_zh"])
            self.assertIn("复用 1 张", production_summary["local_reuse_summary"]["message_zh"])
            self.assertIn("重新处理 1 张", production_summary["local_reuse_summary"]["message_zh"])
            for timing_payload in (production_summary["stage_timings"], progress["stage_timings"]):
                self.assertTrue(timing_payload["aggregate_only"])
                self.assertEqual(
                    [(stage["id"], stage["label_zh"], stage["status"]) for stage in timing_payload["stages"]],
                    [
                        ("scan", "检查扫描图片", "completed"),
                        ("process", "生成处理后图片", "completed"),
                        ("summarize", "整理处理结果", "completed"),
                    ],
                )
                for stage in timing_payload["stages"]:
                    self.assertIsInstance(stage["elapsed_seconds"], float)
                    self.assertGreaterEqual(stage["elapsed_seconds"], 0.0)

    def test_production_run_restart_avoids_reuse_when_input_or_output_identity_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-production-mismatch-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            changed_derivatives_dir = root / "changed-derivatives"
            input_dir.mkdir()
            source = input_dir / "page.png"
            Image.new("RGB", (80, 60), "white").save(source, dpi=(300, 300))
            base_args = [
                "production-run",
                "--input",
                str(input_dir),
                "--derivatives-out",
                str(derivatives_dir),
                "--metadata-out",
                str(metadata_dir),
                "--workers",
                "1",
            ]

            self.assertEqual(main(base_args), 0)
            Image.new("RGB", (80, 60), (200, 200, 200)).save(source, dpi=(300, 300))
            self.assertEqual(main(base_args), 0)
            input_changed_manifest = json.loads((derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            input_changed_summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(input_changed_manifest["summary"]["resumed_files"], 0)
            self.assertEqual(input_changed_manifest["summary"]["reprocessed_files"], 1)
            self.assertEqual(input_changed_summary["local_reuse_summary"]["reused_files"], 0)
            self.assertEqual(input_changed_summary["local_reuse_summary"]["reprocessed_files"], 1)

            changed_output_args = [
                "production-run",
                "--input",
                str(input_dir),
                "--derivatives-out",
                str(changed_derivatives_dir),
                "--metadata-out",
                str(metadata_dir),
                "--workers",
                "1",
            ]
            self.assertEqual(main(changed_output_args), 0)
            output_changed_manifest = json.loads((changed_derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            output_changed_summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            self.assertFalse(output_changed_manifest["resume"]["previous_manifest_found"])
            self.assertEqual(output_changed_manifest["summary"]["resumed_files"], 0)
            self.assertEqual(output_changed_manifest["summary"]["reprocessed_files"], 0)
            self.assertEqual(output_changed_summary["local_reuse_summary"]["reused_files"], 0)
            self.assertEqual(output_changed_summary["local_reuse_summary"]["reprocessed_files"], 0)

    def test_production_run_rerun_reprocesses_when_processing_options_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-production-reprocess-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            _dark_border_page().save(input_dir / "page.png", dpi=(300, 300))
            base_args = [
                "production-run",
                "--input",
                str(input_dir),
                "--derivatives-out",
                str(derivatives_dir),
                "--metadata-out",
                str(metadata_dir),
                "--workers",
                "1",
            ]

            self.assertEqual(main(base_args), 0)
            self.assertEqual(main(base_args + ["--trim-dark-border"]), 0)
            manifest = json.loads((derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            production_summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))

            self.assertTrue(manifest["resume"]["enabled"])
            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertEqual(manifest["summary"]["resumed_files"], 0)
            self.assertEqual(manifest["summary"]["reprocessed_files"], 1)
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["reused_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["reprocessed_files"], 1)
            self.assertEqual(production_summary["local_reuse_summary"]["failed_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["remaining_files"], 0)
            self.assertEqual(production_summary["local_reuse_summary"]["total_files"], 1)
            self.assertIn("重新处理 1 张", production_summary["local_reuse_summary"]["message_zh"])

    def test_aggregate_processing_reuse_precheck_parallel_matches_serial_aggregate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-precheck-parallel-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            process_dir = root / "private-output"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "private_reusable_page.png", dpi=(300, 300))
            Image.new("RGB", (80, 60), (230, 230, 230)).save(input_dir / "private_missing_page.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            options = ProcessingOptions(resume_processing=True, reuse_scan_measurements=True, workers=1)
            manifest = process_images(report, input_dir, process_dir, options)
            missing_record = next(record for record in manifest["files"] if record["source_relative_path"] == "private_missing_page.png")
            (process_dir / missing_record["output_relative_path"]).unlink()

            relative_paths = ["private_reusable_page.png", "private_missing_page.png"]
            with mock.patch("archive_scan_qc.processing.ThreadPoolExecutor", side_effect=AssertionError("serial only")):
                serial = aggregate_processing_reuse_precheck(input_dir, process_dir, relative_paths, options)
            parallel = aggregate_processing_reuse_precheck(
                input_dir,
                process_dir,
                relative_paths,
                ProcessingOptions(resume_processing=True, reuse_scan_measurements=True, workers=4),
            )

            self.assertEqual(parallel, serial)
            self.assertTrue(parallel["aggregate_only"])
            self.assertTrue(parallel["retry_scope_safe"])
            self.assertEqual(parallel["state"], "ready")
            self.assertEqual(parallel["total_files"], 2)
            self.assertEqual(parallel["reusable_files"], 1)
            self.assertEqual(parallel["needs_processing_files"], 1)
            self.assertEqual(parallel["unknown_scope_files"], 0)
            public_precheck_text = json.dumps(parallel, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("private_reusable_page.png", public_precheck_text)
            self.assertNotIn("private_missing_page.png", public_precheck_text)
            self.assertNotIn(str(input_dir), public_precheck_text)
            self.assertNotIn(manifest["files"][0]["source_sha256"], public_precheck_text)
            self.assertNotIn(missing_record["output_sha256"], public_precheck_text)

    def test_aggregate_processing_reuse_precheck_parallel_unknown_matches_serial(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-precheck-unknown-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            process_dir = root / "private-output"
            input_dir.mkdir()
            source = input_dir / "private_unreadable_page.png"
            Image.new("RGB", (80, 60), "white").save(source, dpi=(300, 300))
            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(resume_processing=True, reuse_scan_measurements=True, workers=1),
            )
            source.unlink()
            relative_paths = ["private_unreadable_page.png"]
            serial = aggregate_processing_reuse_precheck(
                input_dir,
                process_dir,
                relative_paths,
                ProcessingOptions(resume_processing=True, reuse_scan_measurements=True, workers=1),
            )
            parallel = aggregate_processing_reuse_precheck(
                input_dir,
                process_dir,
                relative_paths,
                ProcessingOptions(resume_processing=True, reuse_scan_measurements=True, workers=4),
            )

            self.assertEqual(parallel, serial)
            self.assertFalse(parallel["retry_scope_safe"])
            self.assertEqual(parallel["state"], "unknown")
            self.assertEqual(parallel["total_files"], 1)
            self.assertIsNone(parallel["reusable_files"])
            self.assertIsNone(parallel["needs_processing_files"])
            self.assertEqual(parallel["unknown_scope_files"], 1)
            public_precheck_text = json.dumps(parallel, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("private_unreadable_page.png", public_precheck_text)
            self.assertNotIn(str(input_dir), public_precheck_text)


def _dark_border_page() -> Image.Image:
    image = Image.new("RGB", (100, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 99, 79), outline="black", width=5)
    draw.rectangle((25, 30, 75, 45), fill="black")
    return image


def _text_page() -> Image.Image:
    image = Image.new("RGB", (180, 140), "white")
    draw = ImageDraw.Draw(image)
    for y in range(30, 105, 14):
        draw.line((35, y, 145, y), fill=(20, 20, 20), width=2)
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
