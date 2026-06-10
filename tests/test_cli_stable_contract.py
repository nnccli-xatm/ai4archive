from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from archive_scan_qc.cli import main
from archive_scan_qc.processing_quality_summary import PROCESSING_QUALITY_SUMMARY_JSON, SCHEMA_VERSION as QUALITY_SCHEMA_VERSION
from archive_scan_qc.production_runner import (
    PRODUCTION_RUN_LOCK_JSON,
    PRODUCTION_RUN_PROGRESS_JSON,
    PRODUCTION_RUN_SUMMARY_JSON,
    ProductionRunConfig,
    run_production_folder,
)
from archive_scan_qc.rules import (
    builtin_rules_profile,
    processing_defaults_for_rule_template,
    processing_profile_for_rule_template,
)


class StableCliRuleTemplateTests(unittest.TestCase):
    def test_builtin_rule_template_metadata_and_processing_defaults_are_public(self) -> None:
        profile = builtin_rules_profile("text-clean-print")
        metadata = profile.metadata()

        self.assertEqual(metadata["template"]["id"], "text-clean-print")
        self.assertEqual(metadata["template"]["version"], "scan-qc.rule-template.v1")
        self.assertEqual(metadata["thresholds"]["min_dpi"], 300)

        defaults = processing_defaults_for_rule_template("text-clean-print")
        self.assertTrue(defaults["normalize_tones"])
        self.assertTrue(defaults["sharpen_text_edges"])
        self.assertFalse(defaults["despeckle_content_type_check"])

        readable = builtin_rules_profile("text-clean-readable-v1").metadata()
        readable_defaults = processing_defaults_for_rule_template("text-clean-readable-v1")
        print_clean_defaults = processing_defaults_for_rule_template("print-clean-v1")
        photo_defaults = processing_defaults_for_rule_template("photo-mixed-safe-v1")

        self.assertEqual(readable["template"]["id"], "text-clean-readable-v1")
        self.assertEqual(readable["thresholds"]["min_dpi"], 300)
        self.assertTrue(readable_defaults["normalize_tones"])
        self.assertTrue(readable_defaults["enhance_faded_text"])
        self.assertTrue(readable_defaults["sharpen_text_edges"])
        self.assertTrue(print_clean_defaults["clean_bleed_through"])
        self.assertFalse(print_clean_defaults["despeckle_content_type_check"])
        self.assertEqual(processing_profile_for_rule_template("text-clean-readable-v1"), "standard")
        self.assertEqual(processing_profile_for_rule_template("print-clean-v1"), "print_clean")
        self.assertTrue(photo_defaults["trim_dark_border"])
        self.assertNotIn("enhance_faded_text", photo_defaults)

    def test_production_run_rule_template_records_template_and_applies_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-template-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            _write_clean_page(input_dir / "BATCH001_PAGE_0001.png")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "text-clean-print",
                        "--workers",
                        "1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            progress = json.loads((metadata_dir / "production_run_progress.json").read_text(encoding="utf-8"))
            scan_report = json.loads(
                (metadata_dir / "admin_reports" / "scan_qc_report.json").read_text(encoding="utf-8")
            )
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )
            quality_path = derivatives_dir / PROCESSING_QUALITY_SUMMARY_JSON
            quality_summary = json.loads(quality_path.read_text(encoding="utf-8"))

            self.assertEqual(summary["rule_template"]["id"], "text-clean-print")
            self.assertEqual(scan_report["manifest"]["rules_profile"]["template"]["id"], "text-clean-print")
            self.assertTrue(summary["options"]["normalize_tones"])
            self.assertTrue(summary["options"]["sharpen_text_edges"])
            self.assertFalse(summary["options"]["despeckle_content_type_check"])
            self.assertEqual(processing_manifest["rule_template"]["id"], "text-clean-print")
            self.assertEqual(processing_manifest["rule_template"]["version"], "scan-qc.rule-template.v1")
            self.assertFalse(processing_manifest["options"]["despeckle_content_type_check"])
            self.assertEqual(summary["artifacts"]["processing_quality_summary"], str(quality_path.resolve()))
            self.assertTrue(summary["processing_quality_summary"]["provided"])
            self.assertEqual(summary["processing_quality_summary"]["status"], "pass")
            self.assertEqual(quality_summary["schema_version"], QUALITY_SCHEMA_VERSION)
            self.assertTrue(quality_summary["public_safe"])
            self.assertFalse(quality_summary["privacy"]["contains_paths"])
            self.assertEqual(progress["state"], "finished")
            self.assertFalse(summary["source_images_modified"])

    def test_production_run_accepts_v1_text_clean_template(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-template-v1-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            _write_clean_page(input_dir / "BATCH001_PAGE_0001.png")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "text-clean-readable-v1",
                        "--workers",
                        "1",
                    ]
                )

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["rule_template"]["id"], "text-clean-readable-v1")
        self.assertEqual(processing_manifest["rule_template"]["id"], "text-clean-readable-v1")
        self.assertEqual(summary["options"]["processing_profile"], "standard")
        self.assertEqual(processing_manifest["options"]["processing_profile"], "standard")
        self.assertTrue(summary["options"]["normalize_tones"])
        self.assertTrue(summary["options"]["enhance_faded_text"])
        self.assertTrue(summary["options"]["sharpen_text_edges"])

    def test_production_run_print_clean_template_records_print_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-print-clean-template-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            source = input_dir / "BATCH001_PAGE_0001.png"
            _write_blurred_text_edges_page(source)
            source_bytes = source.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "print-clean-v1",
                        "--workers",
                        "1",
                    ]
                )

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )
            record = processing_manifest["files"][0]
            source_bytes_after = source.read_bytes()

        self.assertEqual(exit_code, 0)
        self.assertEqual(source_bytes_after, source_bytes)
        self.assertEqual(summary["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(processing_manifest["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(summary["options"]["processing_profile"], "print_clean")
        self.assertEqual(processing_manifest["options"]["processing_profile"], "print_clean")
        self.assertTrue(record["text_edges_sharpened"])
        self.assertEqual(record["text_edges_reason_code"], "applied_print_clean_blurred_text_edges")
        self.assertGreater(record["text_edges_delta"], 10.0)
        self.assertGreater(record["text_edges_edge_energy_after"], record["text_edges_edge_energy_before"])

    def test_production_run_print_clean_template_records_faded_text_profile_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-print-clean-faded-text-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            source = input_dir / "BATCH001_PAGE_0001.png"
            _write_print_clean_faded_text_page(source)
            source_bytes = source.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "print-clean-v1",
                        "--workers",
                        "1",
                    ]
                )

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )
            record = processing_manifest["files"][0]
            source_bytes_after = source.read_bytes()

        self.assertEqual(exit_code, 0)
        self.assertEqual(source_bytes_after, source_bytes)
        self.assertEqual(summary["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(processing_manifest["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(summary["options"]["processing_profile"], "print_clean")
        self.assertEqual(processing_manifest["options"]["processing_profile"], "print_clean")
        self.assertTrue(record["faded_text_enhanced"])
        self.assertEqual(record["faded_text_reason_code"], "applied_print_clean_stable_low_contrast_text")
        self.assertGreaterEqual(record["faded_text_delta"], 18.0)
        self.assertGreater(record["faded_text_changed_pixel_ratio"], 0.0)
        self.assertLessEqual(record["faded_text_changed_pixel_ratio"], 0.10)
        self.assertEqual(record["processing_audit"]["guardrail_failures"], [])

    def test_production_run_print_clean_template_records_background_stain_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-print-clean-background-stain-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            source = input_dir / "BATCH001_PAGE_0001.png"
            _write_print_clean_background_stain_page(source)
            source_bytes = source.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "print-clean-v1",
                        "--workers",
                        "1",
                    ]
                )

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )
            quality_summary = json.loads(
                (derivatives_dir / PROCESSING_QUALITY_SUMMARY_JSON).read_text(encoding="utf-8")
            )
            record = processing_manifest["files"][0]
            audit = record["processing_audit"]
            source_bytes_after = source.read_bytes()

        self.assertEqual(exit_code, 0)
        self.assertEqual(source_bytes_after, source_bytes)
        self.assertEqual(summary["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(processing_manifest["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(summary["options"]["processing_profile"], "print_clean")
        self.assertEqual(processing_manifest["options"]["processing_profile"], "print_clean")
        self.assertTrue(record["background_stains_lightened"])
        self.assertIn("localized low-contrast stains", record["background_stains_reason"])
        self.assertGreaterEqual(audit["background_stains_delta"], 6.0)
        self.assertGreater(audit["background_stains_changed_pixel_ratio"], 0.02)
        self.assertLessEqual(audit["background_stains_changed_pixel_ratio"], 0.05)
        self.assertLessEqual(audit["background_stains_candidate_pixel_ratio"], 0.05)
        self.assertEqual(audit["guardrail_failures"], [])
        self.assertEqual(quality_summary["schema_version"], QUALITY_SCHEMA_VERSION)
        self.assertEqual(quality_summary["status"], "pass")
        self.assertEqual(quality_summary["quality_signal"]["status"], "measured_with_changes")
        self.assertEqual(quality_summary["counts"]["background_stains_lightened_files"], 1)
        self.assertGreaterEqual(quality_summary["quality_metrics"]["background_stains_delta"]["max"], 6.0)

    def test_production_run_print_clean_template_records_scanline_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-print-clean-scanline-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            source = input_dir / "BATCH001_PAGE_0001.png"
            _write_print_clean_scanline_page(source)
            source_bytes = source.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "print-clean-v1",
                        "--workers",
                        "1",
                    ]
                )

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )
            quality_summary = json.loads(
                (derivatives_dir / PROCESSING_QUALITY_SUMMARY_JSON).read_text(encoding="utf-8")
            )
            record = processing_manifest["files"][0]
            audit = record["processing_audit"]
            source_bytes_after = source.read_bytes()

        self.assertEqual(exit_code, 0)
        self.assertEqual(source_bytes_after, source_bytes)
        self.assertEqual(summary["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(processing_manifest["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(summary["options"]["processing_profile"], "print_clean")
        self.assertEqual(processing_manifest["options"]["processing_profile"], "print_clean")
        self.assertTrue(record["scanlines_lightened"])
        self.assertEqual(record["scanlines_orientation"], "horizontal")
        self.assertGreaterEqual(record["scanlines_count"], 1)
        self.assertIn("low-contrast neutral background scanlines", record["scanlines_reason"])
        self.assertGreaterEqual(audit["scanlines_delta"], 4.0)
        self.assertGreater(audit["scanlines_changed_pixel_ratio"], 0.0)
        self.assertLessEqual(audit["scanlines_changed_pixel_ratio"], 0.03)
        self.assertLessEqual(audit["scanlines_candidate_pixel_ratio"], 0.03)
        self.assertEqual(audit["guardrail_failures"], [])
        self.assertEqual(quality_summary["schema_version"], QUALITY_SCHEMA_VERSION)
        self.assertEqual(quality_summary["status"], "pass")
        self.assertEqual(quality_summary["quality_signal"]["status"], "measured_with_changes")
        self.assertEqual(quality_summary["counts"]["scanlines_lightened_files"], 1)
        self.assertGreaterEqual(quality_summary["quality_metrics"]["scanlines_delta"]["max"], 4.0)

    def test_production_run_print_clean_template_records_bleed_through_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-print-clean-bleed-through-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            source = input_dir / "BATCH001_PAGE_0001.png"
            _write_print_clean_bleed_through_page(source)
            source_bytes = source.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "print-clean-v1",
                        "--workers",
                        "1",
                    ]
                )

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )
            quality_summary = json.loads(
                (derivatives_dir / PROCESSING_QUALITY_SUMMARY_JSON).read_text(encoding="utf-8")
            )
            record = processing_manifest["files"][0]
            audit = record["processing_audit"]
            source_bytes_after = source.read_bytes()

        self.assertEqual(exit_code, 0)
        self.assertEqual(source_bytes_after, source_bytes)
        self.assertEqual(summary["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(processing_manifest["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(summary["options"]["processing_profile"], "print_clean")
        self.assertEqual(processing_manifest["options"]["processing_profile"], "print_clean")
        self.assertTrue(record["bleed_through_cleaned"])
        self.assertEqual(record["bleed_through_reason_code"], "applied_faint_reverse_ghost")
        self.assertGreaterEqual(audit["bleed_through_delta"], 4.0)
        self.assertGreater(audit["bleed_through_changed_pixel_ratio"], 0.0)
        self.assertLessEqual(audit["bleed_through_changed_pixel_ratio"], 0.03)
        self.assertLessEqual(audit["bleed_through_candidate_pixel_ratio"], 0.03)
        self.assertEqual(audit["guardrail_failures"], [])
        self.assertEqual(quality_summary["schema_version"], QUALITY_SCHEMA_VERSION)
        self.assertEqual(quality_summary["status"], "pass")
        self.assertEqual(quality_summary["quality_signal"]["status"], "measured_with_changes")
        self.assertEqual(quality_summary["counts"]["bleed_through_cleaned_files"], 1)
        self.assertGreaterEqual(quality_summary["quality_metrics"]["bleed_through_delta"]["max"], 4.0)

    def test_production_run_print_clean_template_records_illumination_gradient_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-print-clean-illumination-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            source = input_dir / "BATCH001_PAGE_0001.png"
            _write_print_clean_illumination_gradient_page(source)
            source_bytes = source.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "print-clean-v1",
                        "--workers",
                        "1",
                    ]
                )

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )
            quality_summary = json.loads(
                (derivatives_dir / PROCESSING_QUALITY_SUMMARY_JSON).read_text(encoding="utf-8")
            )
            record = processing_manifest["files"][0]
            audit = record["processing_audit"]
            source_bytes_after = source.read_bytes()

        self.assertEqual(exit_code, 0)
        self.assertEqual(source_bytes_after, source_bytes)
        self.assertEqual(summary["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(processing_manifest["rule_template"]["id"], "print-clean-v1")
        self.assertEqual(summary["options"]["processing_profile"], "print_clean")
        self.assertEqual(processing_manifest["options"]["processing_profile"], "print_clean")
        self.assertTrue(record["illumination_gradient_levelled"])
        self.assertEqual(record["illumination_gradient_reason_code"], "applied")
        self.assertGreaterEqual(audit["illumination_gradient_correction_delta"], 10.0)
        self.assertGreaterEqual(audit["illumination_gradient_changed_pixel_ratio"], 0.90)
        self.assertLessEqual(audit["illumination_gradient_changed_pixel_ratio"], 1.0)
        self.assertLessEqual(audit["illumination_gradient_candidate_pixel_ratio"], 1.0)
        self.assertEqual(audit["guardrail_failures"], [])
        self.assertEqual(quality_summary["schema_version"], QUALITY_SCHEMA_VERSION)
        self.assertEqual(quality_summary["status"], "pass")
        self.assertEqual(quality_summary["quality_signal"]["status"], "measured_with_changes")
        self.assertEqual(quality_summary["counts"]["illumination_gradient_levelled_files"], 1)
        self.assertGreaterEqual(
            quality_summary["quality_metrics"]["illumination_gradient_correction_delta"]["max"],
            10.0,
        )

    def test_production_run_photo_mixed_safe_template_keeps_strong_cleanup_disabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-photo-safe-template-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            source = input_dir / "MIXED001_PAGE_0001.png"
            _write_mixed_photo_safe_page(source)
            source_bytes = source.read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--rule-template",
                        "photo-mixed-safe-v1",
                        "--workers",
                        "1",
                    ]
                )

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (derivatives_dir / "processing_manifest.json").read_text(encoding="utf-8")
            )
            quality_summary = json.loads(
                (derivatives_dir / PROCESSING_QUALITY_SUMMARY_JSON).read_text(encoding="utf-8")
            )
            source_unchanged = source.read_bytes() == source_bytes

        record = processing_manifest["files"][0]
        self.assertEqual(exit_code, 0)
        self.assertTrue(source_unchanged)
        self.assertEqual(summary["rule_template"]["id"], "photo-mixed-safe-v1")
        self.assertEqual(processing_manifest["rule_template"]["id"], "photo-mixed-safe-v1")
        self.assertTrue(summary["options"]["trim_dark_border"])
        self.assertTrue(summary["options"]["scanner_gutter_trim"])
        self.assertTrue(summary["options"]["reuse_scan_measurements"])
        self.assertFalse(summary["options"]["normalize_tones"])
        self.assertFalse(summary["options"]["lighten_background_stains"])
        self.assertFalse(summary["options"]["clean_bleed_through"])
        self.assertFalse(summary["options"]["enhance_faded_text"])
        self.assertFalse(summary["options"]["sharpen_text_edges"])
        self.assertFalse(processing_manifest["options"]["normalize_tones"])
        self.assertFalse(processing_manifest["options"]["lighten_background_stains"])
        self.assertFalse(processing_manifest["options"]["enhance_faded_text"])
        self.assertFalse(processing_manifest["options"]["sharpen_text_edges"])
        self.assertIn("normalize_tones_disabled", record["operations"])
        self.assertIn("lighten_background_stains_disabled", record["operations"])
        self.assertIn("enhance_faded_text_disabled", record["operations"])
        self.assertIn("sharpen_text_edges_disabled", record["operations"])
        self.assertTrue(summary["processing_quality_summary"]["public_safe"])
        self.assertEqual(quality_summary["schema_version"], QUALITY_SCHEMA_VERSION)
        self.assertFalse(quality_summary["privacy"]["contains_paths"])
        self.assertFalse(summary["source_images_modified"])

    def test_run_plan_accepts_rule_template_and_records_public_batch_choice(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-run-plan-template-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "project-out"
            input_dir.mkdir()
            _write_clean_page(input_dir / "BATCH001_PAGE_0001.png")
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "project_id": "stable-cli",
                        "batches": [
                            {
                                "batch_id": "batch-001",
                                "input_dir": str(input_dir),
                                "report_dir": "batch-001",
                                "process_out": "processed-batch-001",
                                "rule_template": "text-clean-readable-v1",
                                "workers": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["run-plan", "--plan-json", str(plan_path), "--out", str(output_dir)])

            self.assertEqual(exit_code, 0)
            summary = json.loads((output_dir / "run_plan_summary.json").read_text(encoding="utf-8"))
            scan_report = json.loads((output_dir / "batch-001" / "scan_qc_report.json").read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (output_dir / "processed-batch-001" / "processing_manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["batches"][0]["rule_template"], "text-clean-readable-v1")
            self.assertEqual(scan_report["manifest"]["rules_profile"]["template"]["id"], "text-clean-readable-v1")
            self.assertEqual(processing_manifest["rule_template"]["id"], "text-clean-readable-v1")
            self.assertFalse(processing_manifest["options"]["despeckle_content_type_check"])
            self.assertEqual(processing_manifest["summary"]["failed_files"], 0)

    def test_rule_template_catalog_and_dry_run_are_public_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-template-dry-run-") as temp_dir:
            root = Path(temp_dir)
            catalog_dir = root / "catalog"
            dry_run_dir = root / "dry-run"
            catalog_stdout = io.StringIO()
            dry_run_stdout = io.StringIO()

            with contextlib.redirect_stdout(catalog_stdout):
                catalog_exit = main(["rule-template-catalog", "--out", str(catalog_dir)])
            with contextlib.redirect_stdout(dry_run_stdout):
                dry_run_exit = main(
                    [
                        "rule-template-dry-run",
                        "--rule-template",
                        "text-clean-print",
                        "--out",
                        str(dry_run_dir),
                    ]
                )

            catalog = json.loads((catalog_dir / "rule_template_catalog.json").read_text(encoding="utf-8"))
            dry_run = json.loads((dry_run_dir / "rule_template_dry_run.json").read_text(encoding="utf-8"))
            raw = json.dumps({"catalog": catalog, "dry_run": dry_run}, ensure_ascii=False)

        self.assertEqual(catalog_exit, 0)
        self.assertEqual(dry_run_exit, 0)
        self.assertEqual(catalog["schema_version"], "scan-qc.rule-template-catalog.v1")
        self.assertEqual(dry_run["schema_version"], "scan-qc.rule-template-dry-run.v1")
        self.assertIn("text-clean-print", {template["id"] for template in catalog["templates"]})
        self.assertIn("text-clean-readable-v1", {template["id"] for template in catalog["templates"]})
        self.assertIn("print-clean-v1", {template["id"] for template in catalog["templates"]})
        self.assertIn("photo-mixed-safe-v1", {template["id"] for template in catalog["templates"]})
        self.assertEqual(dry_run["template"]["id"], "text-clean-print")
        self.assertFalse(dry_run["derivative_images_written"])
        self.assertIn("scan_report_not_provided", dry_run["risk_codes"])
        self.assertIn("text_clean_requires_pure_text_batch_confirmation", dry_run["risk_codes"])
        self.assertFalse(dry_run["privacy"]["contains_paths"])
        self.assertNotIn(temp_dir, raw)
        self.assertIn("Derivative images written: no", dry_run_stdout.getvalue())

    def test_v1_template_dry_run_reports_quality_goal_risks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-template-v1-dry-run-") as temp_dir:
            root = Path(temp_dir)
            text_dir = root / "text-readable"
            photo_dir = root / "photo-safe"
            print_dir = root / "print-clean"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                text_exit = main(
                    [
                        "rule-template-dry-run",
                        "--rule-template",
                        "text-clean-readable-v1",
                        "--out",
                        str(text_dir),
                    ]
                )
                print_exit = main(
                    [
                        "rule-template-dry-run",
                        "--rule-template",
                        "print-clean-v1",
                        "--out",
                        str(print_dir),
                    ]
                )
                photo_exit = main(
                    [
                        "rule-template-dry-run",
                        "--rule-template",
                        "photo-mixed-safe-v1",
                        "--out",
                        str(photo_dir),
                    ]
                )
            text_payload = json.loads((text_dir / "rule_template_dry_run.json").read_text(encoding="utf-8"))
            print_payload = json.loads((print_dir / "rule_template_dry_run.json").read_text(encoding="utf-8"))
            photo_payload = json.loads((photo_dir / "rule_template_dry_run.json").read_text(encoding="utf-8"))
            raw = json.dumps({"text": text_payload, "print": print_payload, "photo": photo_payload}, ensure_ascii=False)

        self.assertEqual(text_exit, 0)
        self.assertEqual(print_exit, 0)
        self.assertEqual(photo_exit, 0)
        self.assertEqual(text_payload["template"]["id"], "text-clean-readable-v1")
        self.assertEqual(text_payload["template"]["output_profile"], "text-clean-readable")
        self.assertEqual(text_payload["template"]["processing_profile"], "standard")
        self.assertIn("text_clean_requires_pure_text_batch_confirmation", text_payload["risk_codes"])
        self.assertEqual(print_payload["template"]["output_profile"], "print-clean")
        self.assertEqual(print_payload["template"]["processing_profile"], "print_clean")
        self.assertIn("print_clean_requires_overprocessing_review", print_payload["risk_codes"])
        self.assertEqual(photo_payload["template"]["output_profile"], "photo-mixed-safe")
        self.assertIn("strong_cleanup_disabled_by_high_fidelity_goal", photo_payload["risk_codes"])
        self.assertNotIn(temp_dir, raw)

    def test_rule_template_dry_run_reduces_scan_report_to_public_safe_counts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-template-report-") as temp_dir:
            root = Path(temp_dir)
            scan_report = root / "scan_qc_report.json"
            output_dir = root / "dry-run"
            scan_report.write_text(
                json.dumps(
                    {
                        "summary": {
                            "total_files": 7,
                            "openable_files": 6,
                            "p0_findings": 1,
                            "p1_findings": 2,
                            "p2_findings": 3,
                            "total_findings": 6,
                        },
                        "manifest": {
                            "rules_profile": {
                                "template": {
                                    "id": str(root / "private-rules-profile.json"),
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "rule-template-dry-run",
                        "--rule-template",
                        "high-fidelity-original",
                        "--scan-report",
                        str(scan_report),
                        "--out",
                        str(output_dir),
                    ]
                )

            payload = json.loads((output_dir / "rule_template_dry_run.json").read_text(encoding="utf-8"))
            raw = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["scan_summary"]["total_files"], 7)
        self.assertEqual(payload["scan_summary"]["p0_findings"], 1)
        self.assertEqual(payload["scan_summary"]["source_rules_template_id"], "unknown_or_custom")
        self.assertTrue(payload["privacy"]["reads_scan_report"])
        self.assertIn("p0_findings_require_review_before_processing", payload["risk_codes"])
        self.assertFalse(payload["derivative_images_written"])
        self.assertNotIn(temp_dir, raw)

    def test_rule_template_dry_run_rejects_custom_template_without_private_profile(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as context:
                main(["rule-template-dry-run", "--rule-template", "custom"])

        self.assertEqual(context.exception.code, 2)
        self.assertIn("custom templates requires a validated rules profile path", stderr.getvalue())


class StableCliFailureStateTests(unittest.TestCase):
    def test_production_run_rejects_locked_output_directory_without_overwriting_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-output-lock-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            metadata_dir = root / "metadata"
            derivative_dir = root / "derivatives"
            input_dir.mkdir()
            metadata_dir.mkdir()
            derivative_dir.mkdir()
            _write_clean_page(input_dir / "page-001.png")
            (metadata_dir / PRODUCTION_RUN_LOCK_JSON).write_text("locked\n", encoding="utf-8")
            existing_progress = {"state": "running", "sentinel": "keep-existing-progress"}
            existing_summary = {"status": "running", "sentinel": "keep-existing-summary"}
            (metadata_dir / PRODUCTION_RUN_PROGRESS_JSON).write_text(
                json.dumps(existing_progress, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(existing_summary, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            config = ProductionRunConfig(
                input_dir=input_dir,
                derivative_output_dir=derivative_dir,
                metadata_output_dir=metadata_dir,
                workers=1,
            )

            with self.assertRaisesRegex(RuntimeError, "locked by another run"):
                run_production_folder(config)

            progress = json.loads((metadata_dir / PRODUCTION_RUN_PROGRESS_JSON).read_text(encoding="utf-8"))
            summary = json.loads((metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).read_text(encoding="utf-8"))
            self.assertEqual(progress, existing_progress)
            self.assertEqual(summary, existing_summary)
            self.assertFalse((derivative_dir / PRODUCTION_RUN_LOCK_JSON).exists())

    def test_production_run_rejects_same_metadata_and_derivative_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-output-same-dir-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "shared-output"
            input_dir.mkdir()
            _write_clean_page(input_dir / "page-001.png")

            config = ProductionRunConfig(
                input_dir=input_dir,
                derivative_output_dir=output_dir,
                metadata_output_dir=output_dir,
                workers=1,
            )

            with self.assertRaisesRegex(RuntimeError, "must be different"):
                run_production_folder(config)

            self.assertFalse((output_dir / PRODUCTION_RUN_PROGRESS_JSON).exists())
            self.assertFalse((output_dir / PRODUCTION_RUN_SUMMARY_JSON).exists())

    def test_production_run_writes_failed_progress_and_summary_on_input_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-cli-failure-") as temp_dir:
            root = Path(temp_dir)
            metadata_dir = root / "metadata"
            config = ProductionRunConfig(
                input_dir=root / "missing-input",
                derivative_output_dir=root / "derivatives",
                metadata_output_dir=metadata_dir,
                workers=1,
            )

            with self.assertRaises(FileNotFoundError):
                run_production_folder(config)

            progress = json.loads((metadata_dir / "production_run_progress.json").read_text(encoding="utf-8"))
            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(progress["state"], "failed")
            self.assertEqual(progress["steps"][0]["state"], "failed")
            self.assertEqual(progress["failure"]["stage"], "scan")
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["failure"]["stage"], "scan")
            self.assertFalse(summary["source_images_modified"])


def _write_clean_page(path: Path) -> None:
    image = Image.new("RGB", (640, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 50, 590, 850), outline=(210, 210, 210), width=3)
    for index in range(10):
        y = 140 + index * 45
        draw.line((100, y, 540, y), fill=(40, 40, 40), width=2)
    image.save(path, dpi=(300, 300))


def _write_blurred_text_edges_page(path: Path) -> None:
    image = Image.new("RGB", (420, 560), (245, 245, 242))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    lines = (
        "ARCHIVE QUALITY CONTROL PAGE",
        "TYPED TEXT EDGES ARE SOFT",
        "REVIEW SHOULD STAY SAFE",
        "PRINTED STROKES ONLY",
        "LOCAL BATCH SAMPLE",
        "NEUTRAL LIGHT PAPER",
        "MILD BLUR CASE",
        "STABLE ROW STRUCTURE",
        "FINAL TEXT LINE",
    )
    for index, line in enumerate(lines):
        draw.text((64, 100 + index * 34), line, fill=(72, 72, 72), font=font)
    image.filter(ImageFilter.GaussianBlur(radius=0.75)).save(path, dpi=(300, 300))


def _write_print_clean_faded_text_page(path: Path) -> None:
    image = Image.new("RGB", (360, 240), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    lines = ("ARCHIVE REGISTER 1948", "PALE PRINT LINE", "FILING COPY TEXT")
    for index, line in enumerate(lines):
        draw.text((48, 52 + index * 32), line, fill=(228, 228, 228), font=font)
    image.save(path, dpi=(300, 300))


def _write_print_clean_background_stain_page(path: Path) -> None:
    image = Image.new("RGB", (260, 190), (242, 242, 236))
    draw = ImageDraw.Draw(image)
    for y in (54, 82, 110):
        draw.rectangle((42, y, 142, y + 4), fill=(42, 42, 42))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((178, 58, 222, 102), fill=150)
    mask = mask.filter(ImageFilter.GaussianBlur(7))
    image = Image.composite(Image.new("RGB", image.size, (224, 220, 196)), image, mask)
    image.save(path, dpi=(300, 300))


def _write_print_clean_scanline_page(path: Path) -> None:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (42, 64, 86):
        draw.rectangle((42, y, 158, y + 5), fill=(36, 36, 36))
    for y in (122, 132, 144):
        for x0 in (18, 54, 92, 132, 172, 212):
            draw.rectangle((x0, y, x0 + 14, y + 1), fill=(237, 237, 233))
    image.save(path, dpi=(300, 300))


def _write_print_clean_bleed_through_page(path: Path) -> None:
    image = Image.new("RGB", (260, 180), (244, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.text((34, 36), "REAL", fill=(70, 70, 70))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((124, 82), "321", fill=255)
    mask_draw.text((124, 104), "654", fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(2.4))
    ghost = Image.new("RGB", image.size, (232, 232, 228))
    image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.55)))
    image.save(path, dpi=(300, 300))


def _write_print_clean_illumination_gradient_page(path: Path) -> None:
    width, height = 320, 240
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            value = int(226 + (246 - 226) * x / max(1, width - 1))
            pixels[x, y] = (value, value, value)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for index, line in enumerate(("ARCHIVE", "REGISTER", "PAGE")):
        draw.text((72, 70 + index * 28), line, fill=(90, 90, 90), font=font)
    image.save(path, dpi=(300, 300))


def _write_mixed_photo_safe_page(path: Path) -> None:
    image = Image.new("RGB", (640, 900), (246, 244, 238))
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 580, 840), outline=(208, 204, 194), width=2)
    pixels = image.load()
    for y in range(130, 430):
        for x in range(80, 360):
            red = 82 + (x - 80) // 3
            green = 92 + (y - 130) // 4
            blue = 118 + ((x + y) % 34)
            pixels[x, y] = (min(red, 182), min(green, 174), min(blue, 190))
    draw.ellipse((410, 140, 540, 260), outline=(190, 34, 30), width=8)
    draw.line((430, 200, 520, 200), fill=(190, 34, 30), width=3)
    for row in range(0, 6):
        y = 520 + row * 34
        draw.line((90, y, 550, y), fill=(72, 72, 72), width=1)
    for column in range(0, 5):
        x = 90 + column * 115
        draw.line((x, 520, x, 690), fill=(72, 72, 72), width=1)
    for index in range(5):
        y = 740 + index * 24
        draw.rectangle((100, y, 420, y + 7), fill=(74, 74, 74))
    image.save(path, dpi=(600, 600))


if __name__ == "__main__":
    unittest.main()
