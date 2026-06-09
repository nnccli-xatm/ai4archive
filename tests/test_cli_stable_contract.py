from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.cli import main
from archive_scan_qc.processing_quality_summary import PROCESSING_QUALITY_SUMMARY_JSON, SCHEMA_VERSION as QUALITY_SCHEMA_VERSION
from archive_scan_qc.production_runner import ProductionRunConfig, run_production_folder
from archive_scan_qc.rules import builtin_rules_profile, processing_defaults_for_rule_template


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
            self.assertFalse(processing_manifest["options"]["despeckle_content_type_check"])
            self.assertEqual(summary["artifacts"]["processing_quality_summary"], str(quality_path.resolve()))
            self.assertTrue(summary["processing_quality_summary"]["provided"])
            self.assertEqual(summary["processing_quality_summary"]["status"], "pass")
            self.assertEqual(quality_summary["schema_version"], QUALITY_SCHEMA_VERSION)
            self.assertTrue(quality_summary["public_safe"])
            self.assertFalse(quality_summary["privacy"]["contains_paths"])
            self.assertEqual(progress["state"], "finished")
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
                                "rule_template": "text-clean-print",
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

            self.assertEqual(summary["batches"][0]["rule_template"], "text-clean-print")
            self.assertEqual(scan_report["manifest"]["rules_profile"]["template"]["id"], "text-clean-print")
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
        self.assertEqual(dry_run["template"]["id"], "text-clean-print")
        self.assertFalse(dry_run["derivative_images_written"])
        self.assertIn("scan_report_not_provided", dry_run["risk_codes"])
        self.assertIn("text_clean_requires_pure_text_batch_confirmation", dry_run["risk_codes"])
        self.assertFalse(dry_run["privacy"]["contains_paths"])
        self.assertNotIn(temp_dir, raw)
        self.assertIn("Derivative images written: no", dry_run_stdout.getvalue())

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


if __name__ == "__main__":
    unittest.main()
