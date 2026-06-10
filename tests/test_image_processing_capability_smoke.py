from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.image_processing_capability_smoke import (
    IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON,
    _REQUIRED_OPERATION_COUNT_BLOCKERS,
    _blocking_codes,
    SCHEMA_VERSION,
    run_image_processing_capability_smoke,
)
from archive_scan_qc.processing_quality_summary import (
    PROCESSING_QUALITY_SUMMARY_JSON,
    build_processing_quality_summary,
    SCHEMA_VERSION as QUALITY_SCHEMA_VERSION,
)

EXPECTED_SYNTHETIC_FIXTURES = 19


class ImageProcessingCapabilitySmokeTests(unittest.TestCase):
    def test_smoke_runs_real_processing_and_writes_public_safe_aggregate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-processing-capability-smoke-test-") as temp_dir:
            out_dir = Path(temp_dir) / "out"
            path, payload, quality_path = run_image_processing_capability_smoke(
                output_path=out_dir,
                generated_at="2026-06-09T00:00:00+00:00",
            )
            raw = path.read_text(encoding="utf-8") if path else ""
            quality_payload = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path else {}

            self.assertEqual(path, out_dir / IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON)
            self.assertEqual(quality_path, out_dir / PROCESSING_QUALITY_SUMMARY_JSON)
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["blocking_codes"], [])
            self.assertTrue(payload["processing_run"]["image_processing_run"])
            self.assertFalse(payload["processing_run"]["source_images_modified"])
            self.assertFalse(payload["privacy"]["private_inputs_read"])
            self.assertFalse(payload["privacy"]["contains_paths"])
            self.assertFalse(payload["privacy"]["contains_filenames"])
            self.assertFalse(payload["privacy"]["contains_hashes"])
            self.assertEqual(payload["counts"]["synthetic_fixture_count"], EXPECTED_SYNTHETIC_FIXTURES)
            self.assertEqual(payload["counts"]["processed_files"], EXPECTED_SYNTHETIC_FIXTURES)
            self.assertEqual(payload["counts"]["failed_files"], 0)
            self.assertEqual(payload["counts"]["retry_list_files"], 0)
            self.assertEqual(payload["counts"]["guardrail_failed_files"], 0)
            self.assertGreaterEqual(payload["operation_counts"]["deskewed_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["dark_border_trimmed_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["scanner_gutter_trimmed_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["despeckled_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["tone_normalized_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["paper_color_cast_normalized_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["edge_shadow_lightened_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["corner_shadows_lightened_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["background_stains_lightened_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["fold_shadows_lightened_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["illumination_gradient_levelled_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["bleed_through_cleaned_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["scanlines_lightened_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["faded_text_enhanced_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["text_edges_sharpened_files"], 1)
            self.assertGreater(payload["backend_summary"]["despeckle_backend_modes"]["fallback"], 0)
            self.assertEqual(payload["quality_baseline"]["schema_version"], QUALITY_SCHEMA_VERSION)
            self.assertEqual(payload["quality_baseline"]["status"], "pass")
            self.assertEqual(payload["quality_baseline"], quality_payload)
            self.assertTrue(quality_payload["privacy"]["public_safe"])
            self.assertFalse(quality_payload["privacy"]["contains_paths"])
            self.assertFalse(quality_payload["privacy"]["contains_hashes"])
            self.assertFalse(quality_payload["privacy"]["contains_image_content"])
            self.assertEqual(quality_payload["fixture_context"]["fixture_count"], EXPECTED_SYNTHETIC_FIXTURES)
            self.assertIn("skewed_text_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn("mixed_photo_stamp_table_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn("bleed_through_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn(
                "broad_thin_paper_bleed_through_page",
                quality_payload["fixture_context"]["fixture_groups"],
            )
            self.assertIn("background_stain_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn("scanner_gutter_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn("illumination_gradient_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn("ultra_pale_typed_text_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn("low_saturation_carbon_text_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn(
                "low_saturation_carbon_text_page",
                payload["synthetic_fixture_summary"]["fixture_groups"],
            )
            self.assertGreaterEqual(
                payload["operation_reason_code_counts"]["faded_text"]["applied_stable_low_saturation_text"],
                1,
            )
            protected_checks = quality_payload["fixture_context"]["protected_content_checks"]
            self.assertEqual(payload["protected_content_checks"], protected_checks)
            self.assertEqual(len(protected_checks), 1)
            mixed_check = protected_checks[0]
            self.assertEqual(mixed_check["fixture_group"], "mixed_photo_stamp_table_page")
            self.assertTrue(mixed_check["checked"])
            self.assertEqual(mixed_check["status"], "pass")
            self.assertEqual(mixed_check["fail_codes"], [])
            self.assertLessEqual(mixed_check["changed_pixel_ratio"], mixed_check["max_changed_pixel_ratio"])
            self.assertLessEqual(mixed_check["color_mean_abs_delta"], mixed_check["max_color_mean_abs_delta"])
            self.assertLessEqual(mixed_check["edge_energy_delta_ratio"], mixed_check["max_edge_energy_delta_ratio"])
            self.assertEqual(quality_payload["counts"]["processed_files"], EXPECTED_SYNTHETIC_FIXTURES)
            self.assertEqual(quality_payload["counts"]["failed_files"], 0)
            self.assertGreaterEqual(quality_payload["counts"]["deskewed_files"], 1)
            self.assertGreaterEqual(quality_payload["counts"]["dark_border_trimmed_files"], 1)
            self.assertGreaterEqual(quality_payload["counts"]["scanner_gutter_trimmed_files"], 1)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["deskew_abs_angle_degrees"]["max"], 0.3)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["max_trim_margin_ratio"]["max"], 0.04)
            self.assertGreaterEqual(
                quality_payload["quality_metrics"]["scanner_gutter_max_trim_margin_ratio"]["max"],
                0.04,
            )
            self.assertGreaterEqual(quality_payload["quality_metrics"]["tone_background_delta"]["max"], 6)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["tone_contrast_delta"]["max"], 40)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["tone_changed_pixel_ratio"]["max"], 0.05)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["paper_color_cast_delta"]["max"], 4)
            self.assertGreaterEqual(
                quality_payload["quality_metrics"]["paper_color_cast_changed_pixel_ratio"]["max"],
                0.5,
            )
            self.assertGreaterEqual(quality_payload["quality_metrics"]["edge_shadow_delta"]["max"], 8)
            self.assertGreaterEqual(
                quality_payload["quality_metrics"]["edge_shadow_changed_pixel_ratio"]["max"],
                0.02,
            )
            self.assertGreaterEqual(quality_payload["quality_metrics"]["corner_shadows_delta"]["max"], 2.5)
            self.assertGreaterEqual(
                quality_payload["quality_metrics"]["corner_shadows_changed_pixel_ratio"]["max"],
                0.02,
            )
            self.assertGreaterEqual(quality_payload["quality_metrics"]["background_stains_delta"]["max"], 6)
            self.assertGreaterEqual(
                quality_payload["quality_metrics"]["background_stains_changed_pixel_ratio"]["max"],
                0.01,
            )
            self.assertGreaterEqual(quality_payload["quality_metrics"]["fold_shadows_delta"]["max"], 4)
            self.assertGreaterEqual(
                quality_payload["quality_metrics"]["illumination_gradient_correction_delta"]["max"],
                8,
            )
            self.assertGreaterEqual(
                quality_payload["quality_metrics"]["illumination_gradient_changed_pixel_ratio"]["max"],
                0.7,
            )
            self.assertGreaterEqual(quality_payload["quality_metrics"]["bleed_through_delta"]["max"], 3)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["scanlines_delta"]["max"], 4)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["faded_text_delta"]["max"], 3)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["text_edges_delta"]["max"], 3)
            self.assertGreater(
                quality_payload["quality_metrics"]["text_edges_edge_energy_after"]["max"],
                quality_payload["quality_metrics"]["text_edges_edge_energy_before"]["max"],
            )
            self.assertGreaterEqual(
                quality_payload["quality_signal"]["any_quality_operation_changed_files"],
                1,
            )
            self.assertEqual(quality_payload["quality_signal"]["status"], "measured_with_changes")

            self.assertNotIn(temp_dir, raw)
            self.assertNotIn("synthetic_fixture_001", raw)
            self.assertNotIn("source_relative_path", raw)
            self.assertNotIn("output_sha256", raw)
            self.assertNotIn(temp_dir, json.dumps(quality_payload, ensure_ascii=False))
            self.assertNotIn("synthetic_fixture_001", json.dumps(quality_payload, ensure_ascii=False))

    def test_cli_writes_smoke_summary_without_private_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-processing-capability-smoke-cli-") as temp_dir:
            out_dir = Path(temp_dir) / "smoke"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["image-processing-capability-smoke", "--out", str(out_dir), "--workers", "1"])
            payload = json.loads((out_dir / IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON).read_text(encoding="utf-8"))
            quality_payload = json.loads((out_dir / PROCESSING_QUALITY_SUMMARY_JSON).read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(quality_payload["schema_version"], QUALITY_SCHEMA_VERSION)
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(quality_payload["status"], "pass")
        self.assertIn("Processing quality summary:", stdout.getvalue())
        self.assertIn("Quality baseline status: pass", stdout.getvalue())
        self.assertIn("Image processing run: yes", stdout.getvalue())
        self.assertIn("Private images read: no", stdout.getvalue())
        self.assertIn("Provider commands run: no", stdout.getvalue())

    def test_blocking_codes_require_declared_quality_operations_to_apply(self) -> None:
        base_audit_counts = {field: 1 for field in _REQUIRED_OPERATION_COUNT_BLOCKERS}
        base_audit_counts["guardrail_failed_files"] = 0
        audit_privacy = {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
        }

        for field, blocker in _REQUIRED_OPERATION_COUNT_BLOCKERS.items():
            with self.subTest(field=field):
                audit_counts = dict(base_audit_counts)
                audit_counts[field] = 0
                blockers = _blocking_codes(
                    fixture_count=EXPECTED_SYNTHETIC_FIXTURES,
                    scan_summary={
                        "total_files": EXPECTED_SYNTHETIC_FIXTURES,
                        "openable_files": EXPECTED_SYNTHETIC_FIXTURES,
                    },
                    processing_summary={
                        "processed_files": EXPECTED_SYNTHETIC_FIXTURES,
                        "failed_files": 0,
                        "retry_list_files": 0,
                    },
                    audit_counts=audit_counts,
                    audit_privacy=audit_privacy,
                    quality_summary=_passing_quality_summary(),
                    protected_content_checks=[{"status": "pass"}],
                    operation_reason_code_counts=_passing_operation_reason_code_counts(),
                    source_images_modified=False,
                )

                self.assertIn(blocker, blockers)

    def test_blocking_codes_require_quality_metric_minima(self) -> None:
        audit_counts = {field: 1 for field in _REQUIRED_OPERATION_COUNT_BLOCKERS}
        audit_counts["guardrail_failed_files"] = 0
        audit_privacy = {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
        }
        quality_summary = _passing_quality_summary()
        quality_summary["quality_metrics"]["tone_contrast_delta"]["max"] = 39.0

        blockers = _blocking_codes(
            fixture_count=EXPECTED_SYNTHETIC_FIXTURES,
            scan_summary={
                "total_files": EXPECTED_SYNTHETIC_FIXTURES,
                "openable_files": EXPECTED_SYNTHETIC_FIXTURES,
            },
            processing_summary={
                "processed_files": EXPECTED_SYNTHETIC_FIXTURES,
                "failed_files": 0,
                "retry_list_files": 0,
            },
            audit_counts=audit_counts,
            audit_privacy=audit_privacy,
            quality_summary=quality_summary,
            protected_content_checks=[{"status": "pass"}],
            operation_reason_code_counts=_passing_operation_reason_code_counts(),
            source_images_modified=False,
        )

        self.assertIn("tone_contrast_delta_below_min", blockers)

    def test_blocking_codes_require_low_saturation_faded_text_reason_evidence(self) -> None:
        audit_counts = {field: 1 for field in _REQUIRED_OPERATION_COUNT_BLOCKERS}
        audit_counts["guardrail_failed_files"] = 0
        audit_privacy = {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
        }

        blockers = _blocking_codes(
            fixture_count=EXPECTED_SYNTHETIC_FIXTURES,
            scan_summary={
                "total_files": EXPECTED_SYNTHETIC_FIXTURES,
                "openable_files": EXPECTED_SYNTHETIC_FIXTURES,
            },
            processing_summary={
                "processed_files": EXPECTED_SYNTHETIC_FIXTURES,
                "failed_files": 0,
                "retry_list_files": 0,
            },
            audit_counts=audit_counts,
            audit_privacy=audit_privacy,
            quality_summary=_passing_quality_summary(),
            protected_content_checks=[{"status": "pass"}],
            operation_reason_code_counts={"faded_text": {"applied_stable_low_saturation_text": 0}},
            source_images_modified=False,
        )

        self.assertIn("low_saturation_faded_text_reason_not_observed", blockers)

    def test_blocking_codes_require_text_edge_energy_to_improve(self) -> None:
        audit_counts = {field: 1 for field in _REQUIRED_OPERATION_COUNT_BLOCKERS}
        audit_counts["guardrail_failed_files"] = 0
        audit_privacy = {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
        }
        quality_summary = _passing_quality_summary()
        quality_summary["quality_metrics"]["text_edges_edge_energy_after"]["max"] = 20.0
        quality_summary["quality_metrics"]["text_edges_edge_energy_before"]["max"] = 20.0

        blockers = _blocking_codes(
            fixture_count=EXPECTED_SYNTHETIC_FIXTURES,
            scan_summary={
                "total_files": EXPECTED_SYNTHETIC_FIXTURES,
                "openable_files": EXPECTED_SYNTHETIC_FIXTURES,
            },
            processing_summary={
                "processed_files": EXPECTED_SYNTHETIC_FIXTURES,
                "failed_files": 0,
                "retry_list_files": 0,
            },
            audit_counts=audit_counts,
            audit_privacy=audit_privacy,
            quality_summary=quality_summary,
            protected_content_checks=[{"status": "pass"}],
            operation_reason_code_counts=_passing_operation_reason_code_counts(),
            source_images_modified=False,
        )

        self.assertIn("text_edge_energy_not_improved", blockers)

    def test_processing_quality_signal_distinguishes_no_quality_changes(self) -> None:
        base_audit = {
            "counts": {
                "processed_files": 2,
                "failed_files": 0,
                "retry_list_files": 0,
                "guardrail_failed_files": 0,
            },
            "guardrails": {"enabled": True, "failed_files": 0, "failure_reasons": {}},
            "privacy": {
                "aggregate_only": True,
                "contains_paths": False,
                "contains_hashes": False,
                "contains_thumbnails": False,
                "contains_ocr_text": False,
                "contains_image_content": False,
            },
        }

        no_change = build_processing_quality_summary(
            manifest={"summary": {"processed_files": 2, "failed_files": 0, "retry_list_files": 0}},
            audit_summary=base_audit,
            generated_at="2026-06-10T00:00:00+00:00",
        )
        changed_audit = json.loads(json.dumps(base_audit))
        changed_audit["counts"]["tone_normalized_files"] = 1
        changed = build_processing_quality_summary(
            manifest={"summary": {"processed_files": 2, "failed_files": 0, "retry_list_files": 0}},
            audit_summary=changed_audit,
            generated_at="2026-06-10T00:00:00+00:00",
        )

        self.assertEqual(no_change["status"], "pass")
        self.assertEqual(no_change["quality_signal"]["status"], "measured_no_quality_operations")
        self.assertEqual(no_change["quality_signal"]["any_quality_operation_changed_files"], 0)
        self.assertFalse(any(no_change["quality_signal"]["quality_operations_applied"].values()))
        self.assertEqual(changed["status"], "pass")
        self.assertEqual(changed["quality_signal"]["status"], "measured_with_changes")
        self.assertEqual(changed["quality_signal"]["background_cleanup_changed_files"], 1)


def _passing_quality_summary() -> dict[str, object]:
    metric_names = (
        "deskew_abs_angle_degrees",
        "max_trim_margin_ratio",
        "scanner_gutter_max_trim_margin_ratio",
        "tone_background_delta",
        "tone_contrast_delta",
        "tone_changed_pixel_ratio",
        "paper_color_cast_delta",
        "paper_color_cast_changed_pixel_ratio",
        "edge_shadow_delta",
        "edge_shadow_changed_pixel_ratio",
        "corner_shadows_delta",
        "corner_shadows_changed_pixel_ratio",
        "background_stains_delta",
        "background_stains_changed_pixel_ratio",
        "fold_shadows_delta",
        "illumination_gradient_correction_delta",
        "illumination_gradient_changed_pixel_ratio",
        "bleed_through_delta",
        "scanlines_delta",
        "faded_text_delta",
        "text_edges_delta",
        "text_edges_edge_energy_before",
        "text_edges_edge_energy_after",
    )
    payload = {
        "quality_metrics": {
            name: {"count": EXPECTED_SYNTHETIC_FIXTURES, "average": 1.0, "max": 100.0}
            for name in metric_names
        }
    }
    payload["quality_metrics"]["text_edges_edge_energy_before"]["max"] = 20.0
    payload["quality_metrics"]["text_edges_edge_energy_after"]["max"] = 30.0
    return payload


def _passing_operation_reason_code_counts() -> dict[str, dict[str, int]]:
    return {
        "faded_text": {
            "applied_stable_low_saturation_text": 1,
            "applied_print_clean_stable_low_saturation_text": 0,
        }
    }


if __name__ == "__main__":
    unittest.main()
