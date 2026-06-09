from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from archive_scan_qc.cli import main


class PreflightRunPlanTests(unittest.TestCase):
    def test_preflight_success_writes_privacy_safe_report(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                process_dir = root / "processed"
                manifest_csv = root / "manifest.csv"
                input_dir.mkdir()
                Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
                manifest_csv.write_text("relative_path\nA001_0001.jpg\n", encoding="utf-8")

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "preflight",
                            "--input",
                            str(input_dir),
                            "--out",
                            str(output_dir),
                            "--process-out",
                            str(process_dir),
                            "--auto-crop",
                            "--workers",
                            "1",
                            "--project",
                            "p1",
                            "--batch",
                            "b1",
                            "--manifest-csv",
                            str(manifest_csv),
                        ]
                    )

                report_path = output_dir / "preflight_report.json"
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 0)
                self.assertTrue(report_path.exists())
                self.assertEqual(report["status"], "pass")
                self.assertEqual(report["input_summary"]["candidate_file_count"], 1)
                self.assertEqual(report["manifest"]["missing_count"], 0)
                self.assertEqual(report["manifest"]["unexpected_count"], 0)
                self.assertFalse(report["privacy"]["contains_file_list"])
                self.assertIn("Preflight status: pass", stdout.getvalue())

    def test_preflight_processing_flag_without_process_out_reports_error(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                input_dir.mkdir()
                Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = main(["preflight", "--input", str(input_dir), "--out", str(output_dir), "--deskew"])

                report = json.loads((output_dir / "preflight_report.json").read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 1)
                self.assertEqual(report["status"], "fail")
                self.assertIn("process_output_required", {error["code"] for error in report["errors"]})

    def test_preflight_invalid_rules_profile_writes_failure_report(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                invalid_profile = root / "invalid.json"
                input_dir.mkdir()
                Image.new("RGB", (32, 24), "white").save(input_dir / "PRIVATE_PRESENT.jpg", dpi=(300, 300))
                invalid_profile.write_text('{"name": ""}', encoding="utf-8")

                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "preflight",
                            "--input",
                            str(input_dir),
                            "--out",
                            str(output_dir),
                            "--rules-profile",
                            str(invalid_profile),
                        ]
                    )

                report_path = output_dir / "preflight_report.json"
                report_text = report_path.read_text(encoding="utf-8")
                report = json.loads(report_text)
                self.assertEqual(exit_code, 1)
                self.assertTrue(report_path.exists())
                self.assertEqual(report["status"], "fail")
                self.assertFalse(report["configuration"]["rules_profile"]["loaded"])
                self.assertTrue(report["configuration"]["rules_profile"]["provided"])
                self.assertIn("rules_profile_invalid", {error["code"] for error in report["errors"]})
                self.assertNotIn("PRIVATE_PRESENT.jpg", report_text)
                self.assertNotIn(str(input_dir), report_text)
                self.assertNotIn("sha256", report_text.lower())
                self.assertFalse(report["privacy"]["contains_file_list"])
                self.assertFalse(report["privacy"]["contains_hashes"])
                self.assertFalse(report["privacy"]["contains_thumbnails"])
                self.assertFalse(report["privacy"]["contains_image_content"])

    def test_preflight_rejects_illegal_manifest_paths(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                manifest_csv = root / "manifest.csv"
                input_dir.mkdir()
                Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
                manifest_csv.write_text(
                    "relative_path\n"
                    " \n"
                    "/private/A001_0001.jpg\n"
                    "../escape.jpg\n"
                    "nested/../escape.jpg\n",
                    encoding="utf-8",
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = main(["preflight", "--input", str(input_dir), "--out", str(output_dir), "--manifest-csv", str(manifest_csv)])

                report = json.loads((output_dir / "preflight_report.json").read_text(encoding="utf-8"))
                codes = {error["code"] for error in report["errors"]}
                self.assertEqual(exit_code, 1)
                self.assertIn("manifest_empty_paths", codes)
                self.assertIn("manifest_absolute_paths", codes)
                self.assertIn("manifest_parent_escape_paths", codes)
                self.assertEqual(report["manifest"]["empty_path_count"], 1)
                self.assertEqual(report["manifest"]["absolute_path_count"], 1)
                self.assertEqual(report["manifest"]["parent_escape_count"], 2)

    def test_preflight_manifest_missing_unexpected_are_aggregate_only(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                manifest_csv = root / "manifest.csv"
                input_dir.mkdir()
                Image.new("RGB", (32, 24), "white").save(input_dir / "PRIVATE_PRESENT.jpg", dpi=(300, 300))
                manifest_csv.write_text("relative_path\nPRIVATE_MISSING.jpg\n", encoding="utf-8")

                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = main(["preflight", "--input", str(input_dir), "--out", str(output_dir), "--manifest-csv", str(manifest_csv)])

                report_text = (output_dir / "preflight_report.json").read_text(encoding="utf-8")
                report = json.loads(report_text)
                self.assertEqual(exit_code, 1)
                self.assertEqual(report["manifest"]["missing_count"], 1)
                self.assertEqual(report["manifest"]["unexpected_count"], 1)
                self.assertNotIn("PRIVATE_PRESENT.jpg", report_text)
                self.assertNotIn("PRIVATE_MISSING.jpg", report_text)
                self.assertNotIn("sha256", report_text.lower())
                self.assertFalse(report["privacy"]["contains_hashes"])
                self.assertFalse(report["privacy"]["contains_thumbnails"])

    def test_run_plan_two_batches_success_and_writes_aggregate_summary(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                plan_path = root / "plan.csv"
                output_root = root / "project-out"
                batch_one = root / "PRIVATE_BATCH_ONE"
                batch_two = root / "PRIVATE_BATCH_TWO"
                batch_one.mkdir()
                batch_two.mkdir()
                batch_one_image = Image.new("RGB", (48, 36), "white")
                batch_one_image.putpixel((12, 12), (0, 0, 0))
                batch_one_image.save(batch_one / "SECRET_ONE.png", dpi=(300, 300))
                Image.new("RGB", (48, 36), "white").save(batch_two / "SECRET_TWO.png", dpi=(300, 300))
                plan_path.write_text(
                    "batch_id,input_dir,report_dir,process_out,workers,auto_crop,despeckle,resume_processing\n"
                    f"batch-one,{batch_one},reports-one,processed-one,1,true,true,false\n"
                    f"batch-two,{batch_two},reports-two,processed-two,1,false,false,true\n",
                    encoding="utf-8",
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = main(["run-plan", "--plan-csv", str(plan_path), "--out", str(output_root), "--project", "project-x"])

                self.assertEqual(exit_code, 0)
                self.assertTrue((output_root / "reports-one" / "preflight_report.json").exists())
                self.assertTrue((output_root / "reports-one" / "scan_qc_report.json").exists())
                self.assertTrue((output_root / "processed-one" / "processing_manifest.json").exists())
                summary_path = output_root / "run_plan_summary.json"
                csv_path = output_root / "run_plan_summary.csv"
                self.assertTrue(summary_path.exists())
                self.assertTrue(csv_path.exists())
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                self.assertEqual(summary["project_id"], "project-x")
                self.assertEqual(summary["summary"]["total_batches"], 2)
                self.assertEqual(summary["summary"]["passed_batches"], 2)
                self.assertEqual(summary["summary"]["failed_batches"], 0)
                self.assertEqual(summary["summary"]["processing_failed_files"], 0)
                self.assertEqual(summary["batches"][1]["processing_resumed_files"], 0)
                despeckle_timing = summary["summary"]["processing_operation_timings"]["despeckle"]
                self.assertEqual(despeckle_timing["file_count"], 1)
                self.assertIn(despeckle_timing["backend_mode"], {"numpy", "fallback"})
                self.assertEqual(sum(despeckle_timing["backend_counts"].values()), 1)

    def test_run_plan_continue_on_error_records_preflight_failure(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                good_input = root / "good"
                good_input.mkdir()
                Image.new("RGB", (48, 36), "white").save(good_input / "public_synthetic.png", dpi=(300, 300))
                missing_input = root / "missing"
                output_root = root / "project-out"
                plan_path = root / "plan.json"
                plan_path.write_text(
                    json.dumps(
                        {
                            "batches": [
                                {"batch_id": "bad-batch", "input_dir": str(missing_input), "report_dir": "bad-report"},
                                {"batch_id": "good-batch", "input_dir": str(good_input), "report_dir": "good-report"},
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = main(["run-plan", "--plan-json", str(plan_path), "--out", str(output_root), "--continue-on-error"])

                self.assertEqual(exit_code, 1)
                summary = json.loads((output_root / "run_plan_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["summary"]["total_batches"], 2)
                self.assertEqual(summary["summary"]["passed_batches"], 1)
                self.assertEqual(summary["summary"]["failed_batches"], 1)
                self.assertEqual(summary["summary"]["failed_batch_ids"], ["bad-batch"])
                self.assertGreaterEqual(summary["summary"]["preflight_error_count"], 1)
                self.assertEqual(summary["batches"][0]["failure_stage"], "preflight")
                self.assertEqual(summary["batches"][1]["status"], "passed")

    def test_run_plan_stops_on_failure_without_continue_on_error(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                good_input = root / "good"
                good_input.mkdir()
                Image.new("RGB", (48, 36), "white").save(good_input / "page.png", dpi=(300, 300))
                output_root = root / "project-out"
                plan_path = root / "plan.json"
                plan_path.write_text(
                    json.dumps(
                        [
                            {"batch_id": "bad-batch", "input_dir": str(root / "missing"), "report_dir": "bad-report"},
                            {"batch_id": "good-batch", "input_dir": str(good_input), "report_dir": "good-report"},
                        ]
                    ),
                    encoding="utf-8",
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = main(["run-plan", "--plan-json", str(plan_path), "--out", str(output_root)])

                self.assertEqual(exit_code, 1)
                summary = json.loads((output_root / "run_plan_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["summary"]["total_batches"], 1)
                self.assertEqual(summary["summary"]["failed_batch_ids"], ["bad-batch"])
                self.assertFalse((output_root / "good-report").exists())

    def test_run_plan_summary_is_aggregate_only(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "private_input"
                input_dir.mkdir()
                private_name = "PRIVATE_CASE_ABC123.png"
                Image.new("RGB", (48, 36), "white").save(input_dir / private_name, dpi=(300, 300))
                output_root = root / "project-out"
                plan_path = root / "plan.csv"
                plan_path.write_text(f"batch_id,input_dir,report_dir\nsafe-batch,{input_dir},reports\n", encoding="utf-8")

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["run-plan", "--plan-csv", str(plan_path), "--out", str(output_root)]), 0)

                raw = (output_root / "run_plan_summary.json").read_text(encoding="utf-8")
                report = json.loads((output_root / "reports" / "scan_qc_report.json").read_text(encoding="utf-8"))
                private_hash = report["files"][0]["sha256"]
                for forbidden in [private_name, str(input_dir), private_hash, "relative_path", "sha256", '"files": [', '"findings": [']:
                    self.assertNotIn(forbidden, raw)
                self.assertTrue(json.loads(raw)["privacy"]["aggregate_only"])

    def test_run_plan_missing_and_invalid_fields_are_clear(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                output_root = root / "out"
                missing_field_plan = root / "missing.csv"
                missing_field_plan.write_text("batch_id,report_dir\nbatch-one,reports\n", encoding="utf-8")

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    main(["run-plan", "--plan-csv", str(missing_field_plan), "--out", str(output_root)])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("missing required field 'input_dir'", stderr.getvalue())

                input_dir = root / "input"
                input_dir.mkdir()
                invalid_plan = root / "invalid.csv"
                invalid_plan.write_text(f"batch_id,input_dir,workers\nbatch-one,{input_dir},0\n", encoding="utf-8")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    main(["run-plan", "--plan-csv", str(invalid_plan), "--out", str(output_root)])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("field 'workers' must be a positive integer", stderr.getvalue())

    def test_run_plan_resume_processing_is_compatible(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                input_dir.mkdir()
                Image.new("RGB", (48, 36), "white").save(input_dir / "page.png", dpi=(300, 300))
                output_root = root / "project-out"
                plan_path = root / "plan.csv"
                plan_path.write_text(
                    f"batch_id,input_dir,report_dir,process_out,resume_processing,workers\nbatch-one,{input_dir},reports,processed,true,1\n",
                    encoding="utf-8",
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["run-plan", "--plan-csv", str(plan_path), "--out", str(output_root)]), 0)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["run-plan", "--plan-csv", str(plan_path), "--out", str(output_root)]), 0)

                summary = json.loads((output_root / "run_plan_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["summary"]["processing_resumed_files"], 1)
                manifest = json.loads((output_root / "processed" / "processing_manifest.json").read_text(encoding="utf-8"))
                self.assertTrue(manifest["resume"]["enabled"])
                self.assertEqual(manifest["summary"]["resumed_files"], 1)


if __name__ == "__main__":
    unittest.main()
