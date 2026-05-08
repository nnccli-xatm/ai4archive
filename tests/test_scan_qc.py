from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from archive_scan_qc.cli import main
from archive_scan_qc.processing import ProcessingOptions, process_images
from archive_scan_qc.reports import write_reports
from archive_scan_qc.rules import RulesProfileError, load_rules_profile
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
            self.assertIn("performance", saved["summary"])
            self.assertIn("performance", saved["manifest"])
            self.assertEqual(saved["summary"]["performance"]["total_files"], 2)
            self.assertEqual(saved["summary"]["performance"]["openable_files"], 2)
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

    def test_flags_unopenable_and_duplicate_hashes_without_cross_directory_name_false_positive(self) -> None:
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
            self.assertNotIn("duplicate_name", rules)
            self.assertIn("duplicate_file", rules)
            self.assertGreaterEqual(report["summary"]["p0_findings"], 2)

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

    def test_quality_metrics_do_not_flag_synthetic_normal_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().save(input_dir / "A001_0001.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules = {finding["rule"] for finding in report["findings"]}
            file_record = report["files"][0]

            self.assertNotIn("quality_too_dark", rules)
            self.assertNotIn("quality_too_bright", rules)
            self.assertNotIn("quality_low_contrast", rules)
            self.assertNotIn("quality_suspected_blur", rules)
            self.assertGreater(file_record["quality_brightness_mean"], 0)
            self.assertGreater(file_record["quality_contrast_stddev"], 0)
            self.assertGreater(file_record["quality_sharpness_laplacian_var"], 0)

    def test_quality_metrics_flag_dark_bright_low_contrast_and_blur(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (80, 80), (25, 25, 25)).save(input_dir / "dark.png")
            Image.new("RGB", (80, 80), (253, 253, 253)).save(input_dir / "bright.png")
            Image.new("RGB", (80, 80), (128, 128, 128)).save(input_dir / "low_contrast.png")
            _synthetic_text_page().filter(ImageFilter.GaussianBlur(radius=3)).save(input_dir / "blur.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules_by_path: dict[str, set[str]] = {}
            for finding in report["findings"]:
                rules_by_path.setdefault(finding["relative_path"], set()).add(finding["rule"])

            self.assertIn("quality_too_dark", rules_by_path["dark.png"])
            self.assertIn("quality_too_bright", rules_by_path["bright.png"])
            self.assertIn("quality_low_contrast", rules_by_path["low_contrast.png"])
            self.assertIn("quality_suspected_blur", rules_by_path["blur.png"])

    def test_default_rules_profile_metadata_preserves_existing_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(150, 150))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            profile = report["manifest"]["rules_profile"]

            self.assertEqual(profile["name"], "default")
            self.assertEqual(profile["version"], "scan-qc.phase1.v1")
            self.assertEqual(profile["source"], "builtin")
            self.assertEqual(profile["thresholds"]["min_dpi"], 200)
            self.assertEqual(profile["thresholds"]["quality"]["dark_mean_threshold"], 45.0)
            self.assertTrue(any(finding["rule"] == "dpi_minimum" and finding["severity"] == "P0" for finding in report["findings"]))

    def test_json_rules_profile_overrides_min_dpi_quality_threshold_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            profile_path = root / "rules.json"
            input_dir.mkdir()
            Image.new("RGB", (80, 80), (40, 40, 40)).save(input_dir / "A001_0001.jpg", dpi=(250, 250))
            profile_path.write_text(
                json.dumps(
                    {
                        "name": "project-standard",
                        "version": "2026.1",
                        "min_dpi": 300,
                        "quality_thresholds": {"dark_mean_threshold": 30},
                    }
                ),
                encoding="utf-8",
            )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=load_rules_profile(profile_path)))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertIn("dpi_minimum", rules)
            self.assertNotIn("quality_too_dark", rules)
            self.assertEqual(report["project"]["min_dpi"], 300)
            self.assertEqual(report["manifest"]["rules_profile"]["name"], "project-standard")
            self.assertEqual(report["manifest"]["rules_profile"]["version"], "2026.1")
            self.assertEqual(report["manifest"]["rules_profile"]["source"], str(profile_path.resolve()))
            self.assertEqual(report["manifest"]["rules_profile"]["thresholds"]["quality"]["dark_mean_threshold"], 30.0)

    def test_rules_profile_can_disable_quality_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            profile_path = root / "rules.json"
            input_dir.mkdir()
            Image.new("RGB", (80, 80), (25, 25, 25)).save(input_dir / "dark.png")
            profile_path.write_text(
                json.dumps({"rules": {"quality_too_dark": {"enabled": False}}}),
                encoding="utf-8",
            )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=load_rules_profile(profile_path)))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertNotIn("quality_too_dark", rules)
            self.assertIn("quality_low_contrast", rules)

    def test_rules_profile_severity_override_applies_but_protected_p0_stays_p0(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            profile_path = root / "rules.json"
            input_dir.mkdir()
            Image.new("RGB", (80, 80), (25, 25, 25)).save(input_dir / "dark.png", dpi=(150, 150))
            profile_path.write_text(
                json.dumps(
                    {
                        "rules": {
                            "quality_too_dark": {"severity": "P2"},
                            "dpi_minimum": {"severity": "P2", "enabled": False},
                        }
                    }
                ),
                encoding="utf-8",
            )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=load_rules_profile(profile_path)))
            severities = {(finding["rule"], finding["severity"]) for finding in report["findings"]}

            self.assertIn(("quality_too_dark", "P2"), severities)
            self.assertIn(("dpi_minimum", "P0"), severities)

    def test_invalid_rules_profile_errors_are_clear_and_cli_writes_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            bad_type_path = root / "bad-type.json"
            bad_json_path = root / "bad-json.json"
            input_dir.mkdir()
            bad_type_path.write_text(json.dumps({"min_dpi": "300"}), encoding="utf-8")
            bad_json_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(RulesProfileError, "min_dpi"):
                load_rules_profile(bad_type_path)
            with self.assertRaisesRegex(RulesProfileError, "line 1, column 2"):
                load_rules_profile(bad_json_path)
            with self.assertRaisesRegex(RulesProfileError, "does not exist"):
                load_rules_profile(root / "missing.json")

            with self.assertRaises(SystemExit) as raised:
                main(["--input", str(input_dir), "--out", str(output_dir), "--rules-profile", str(bad_type_path)])

            self.assertEqual(raised.exception.code, 2)
            self.assertFalse(output_dir.exists())

    def test_quality_metrics_are_visible_in_json_csv_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().filter(ImageFilter.GaussianBlur(radius=3)).save(input_dir / "blur.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            paths = write_reports(report, output_dir)

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertIn("quality_brightness_mean", saved["files"][0])
            self.assertIn("quality_contrast_stddev", paths["files_csv"].read_text(encoding="utf-8"))
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Brightness Mean", html)
            self.assertIn("quality_suspected_blur", html)

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

    def test_process_images_writes_derivatives_without_modifying_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.jpg"
            Image.new("RGB", (32, 24), (120, 120, 120)).save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir)

            processed = process_dir / "images" / "A001_0001.jpg"
            manifest_path = process_dir / "processing_manifest.json"
            self.assertTrue(processed.exists())
            self.assertTrue(manifest_path.exists())
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertIn("performance", manifest)
            self.assertIn("performance", manifest["summary"])
            self.assertEqual(manifest["performance"]["total_files"], 1)
            self.assertEqual(manifest["performance"]["processed_files"], 1)
            self.assertEqual(manifest["performance"]["skipped_files"], 0)
            self.assertEqual(manifest["performance"]["failed_files"], 0)
            self.assertGreaterEqual(manifest["performance"]["elapsed_seconds"], 0)
            self.assertGreaterEqual(manifest["performance"]["processed_files_per_minute"], 0)
            self.assertGreaterEqual(manifest["performance"]["total_files_per_minute"], 0)
            self.assertEqual(manifest["files"][0]["status"], "processed")
            self.assertEqual(manifest["files"][0]["source_relative_path"], "A001_0001.jpg")
            self.assertEqual(manifest["files"][0]["original_size"], [32, 24])
            self.assertEqual(manifest["files"][0]["output_size"], [32, 24])
            self.assertIsNone(manifest["files"][0]["crop_bbox"])
            self.assertFalse(manifest["files"][0]["cropped"])
            self.assertFalse(manifest["files"][0]["deskewed"])
            self.assertEqual(manifest["files"][0]["deskew_reason"], "deskew disabled")
            self.assertIn("auto_crop_disabled", manifest["files"][0]["operations"])
            self.assertIn("deskew_disabled", manifest["files"][0]["operations"])

    def test_deskew_corrects_synthetic_light_skew(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = _synthetic_text_page().rotate(-3.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertAlmostEqual(record["skew_angle_degrees"], -3.0, delta=0.75)
            self.assertGreaterEqual(record["skew_confidence"], 0.08)
            self.assertEqual(record["deskew_reason"], "deskew applied")
            self.assertNotEqual(record["pre_deskew_size"], record["post_deskew_size"])
            self.assertIn("deskew_conservative", record["operations"])

    def test_deskew_does_not_rotate_blank_or_low_contrast_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (120, 90), "white").save(input_dir / "A001_0001.png")
            low_contrast = Image.new("RGB", (160, 120), (245, 245, 245))
            draw = ImageDraw.Draw(low_contrast)
            for y in range(30, 90, 12):
                draw.line((35, y, 125, y), fill=(235, 235, 235), width=2)
            low_contrast.save(input_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertFalse(records["A001_0001.png"]["deskewed"])
            self.assertFalse(records["A001_0002.png"]["deskewed"])
            self.assertIn(records["A001_0001.png"]["deskew_reason"], {"blank page", "low contrast"})
            self.assertEqual(records["A001_0002.png"]["deskew_reason"], "low contrast")

    def test_deskew_does_not_rotate_large_angle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = _synthetic_text_page().rotate(-8.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))

            record = manifest["files"][0]
            self.assertFalse(record["deskewed"])
            self.assertGreater(abs(record["skew_angle_degrees"]), 5.0)
            self.assertEqual(record["deskew_reason"], "angle exceeds conservative threshold")
            self.assertIn("deskew_noop", record["operations"])

    def test_auto_crop_trims_white_margin_around_black_page_border(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 8, 69, 51), outline="black", width=3)
            image.save(source)
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                processed_size = processed.size
            record = manifest["files"][0]
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_bbox"], [10, 8, 70, 52])
            self.assertEqual(record["original_size"], [80, 60])
            self.assertEqual(record["output_size"], [60, 44])
            self.assertEqual(processed_size, (60, 44))
            self.assertIn("auto_crop_conservative", record["operations"])

    def test_auto_crop_does_not_overcrop_blank_or_low_contrast_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "A001_0001.png")
            low_contrast = Image.new("RGB", (80, 60), (245, 245, 245))
            draw = ImageDraw.Draw(low_contrast)
            draw.rectangle((10, 8, 69, 51), outline=(235, 235, 235), width=3)
            low_contrast.save(input_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertFalse(records["A001_0001.png"]["cropped"])
            self.assertFalse(records["A001_0002.png"]["cropped"])
            self.assertEqual(records["A001_0001.png"]["output_size"], [80, 60])
            self.assertEqual(records["A001_0002.png"]["output_size"], [80, 60])
            self.assertIn("auto_crop_noop", records["A001_0001.png"]["operations"])
            self.assertIn("auto_crop_noop", records["A001_0002.png"]["operations"])

    def test_cli_process_out_writes_processing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--process-out",
                        str(process_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((process_dir / "processing_manifest.json").exists())
            self.assertTrue((process_dir / "images" / "A001_0001.jpg").exists())
            output = stdout.getvalue()
            self.assertIn("Scan elapsed:", output)
            self.assertIn("Scan files/min:", output)
            self.assertIn("Processing elapsed:", output)
            self.assertIn("Processing files/min:", output)

    def test_cli_auto_crop_requires_process_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            with self.assertRaises(SystemExit) as raised:
                main(["--input", str(input_dir), "--out", str(output_dir), "--auto-crop"])

            self.assertEqual(raised.exception.code, 2)

    def test_cli_deskew_requires_process_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            with self.assertRaises(SystemExit) as raised:
                main(["--input", str(input_dir), "--out", str(output_dir), "--deskew"])

            self.assertEqual(raised.exception.code, 2)


def _synthetic_text_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(30, 30, 30), width=2)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    return image


if __name__ == "__main__":
    unittest.main()
