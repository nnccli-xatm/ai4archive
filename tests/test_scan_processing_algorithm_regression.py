from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

from archive_scan_qc import processing as processing_module
from archive_scan_qc.benchmark import _processing_quality_regression, run_benchmark
from archive_scan_qc.processing import ProcessingOptions, _combination_quality_guard, process_images
from archive_scan_qc.scanner import ScanConfig, scan_batch


class ScanProcessingAlgorithmRegressionTest(unittest.TestCase):
    def test_combination_quality_guard_classifies_public_reason_codes(self) -> None:
        options = ProcessingOptions()
        base_metrics = _combination_guard_metrics()
        passed = _combination_quality_guard(
            base_metrics,
            options,
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(passed["action"], "passed")
        self.assertEqual(passed["reason_code"], "safe_combination_passed")

        combined = _combination_quality_guard(
            dict(base_metrics, pixel_change_ratio=0.42),
            options,
            cumulative_change_guard=_guard_reverted("pixel_change_ratio", "cumulative_change_score"),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(combined["action"], "reverted_to_source")
        self.assertEqual(combined["reason_code"], "combined_change_too_large_reverted")

        geometry = _combination_quality_guard(
            dict(base_metrics, size_change_ratio=0.21, crop_ratio=0.19, deskew_abs_angle_degrees=1.0),
            ProcessingOptions(audit_max_geometry_combo_crop_ratio=0.18, audit_max_geometry_combo_size_change_ratio=0.18),
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(geometry["reason_code"], "safe_combination_passed")

        geometry = _combination_quality_guard(
            dict(
                base_metrics,
                pixel_change_guardrail_scope="geometric_change_recorded_by_size_crop_trim_or_deskew",
                pixel_change_guardrail_applied=False,
                size_change_ratio=0.21,
                crop_ratio=0.19,
                deskew_abs_angle_degrees=1.0,
            ),
            ProcessingOptions(audit_max_geometry_combo_crop_ratio=0.18, audit_max_geometry_combo_size_change_ratio=0.18),
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(geometry["reason_code"], "geometric_risk_reverted")
        self.assertIn("geometry_crop_or_trim_ratio", geometry["reasons"])

        text = _combination_quality_guard(
            dict(
                base_metrics,
                faded_text_enhanced=True,
                text_edges_sharpened=True,
                faded_text_changed_pixel_ratio=0.06,
                text_edges_changed_pixel_ratio=0.05,
                local_content_changed_ratio=0.13,
            ),
            options,
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(text["reason_code"], "text_high_frequency_risk_reverted")
        self.assertIn("text_combo_changed_pixel_ratio", text["reasons"])

        low_confidence = _combination_quality_guard(
            base_metrics,
            options,
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
            low_confidence_original_preserved=True,
        )
        self.assertEqual(low_confidence["action"], "kept_original")
        self.assertEqual(low_confidence["reason_code"], "low_confidence_original_preserved")

    def test_synthetic_combinations_emit_aggregate_quality_and_performance_regression_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-algorithm-regression-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _synthetic_pages(input_dir)

            default_payload = _benchmark_combo(root, input_dir, "default")
            base_payload = _benchmark_combo(root, input_dir, "base", *BASE_FLAGS)
            full_payload = _benchmark_combo(root, input_dir, "full", *(BASE_FLAGS + CONSERVATIVE_REPAIR_FLAGS))

            default_quality = _single_quality(default_payload)
            base_run = base_payload["runs"][0]
            full_run = full_payload["runs"][0]
            base_quality = _single_quality(base_payload)
            full_quality = _single_quality(full_payload)

            for quality in (base_quality, full_quality):
                self.assertEqual(quality["status"], "pass")
                self.assertEqual(quality["counts"]["failed_files"], 0)
                self.assertEqual(quality["counts"]["guardrail_failed_files"], 0)
                self.assertEqual(quality["counts"]["cumulative_change_guard_reverted_files"], 0)
                self.assertEqual(quality["counts"]["processed_output_safety_guard_reverted_files"], 0)
                self.assertTrue(quality["aggregate_only"])
                self.assertTrue(quality["privacy"]["aggregate_only"])
                self.assertTrue(quality["processed_output_safety_guard"]["aggregate_only"])
                for operation in REQUIRED_OPERATIONS:
                    self.assertIn(operation, quality["algorithm_metrics"])
                    self.assertIn("metrics", quality["algorithm_metrics"][operation])

            self.assertEqual(default_quality["status"], "pass")
            self.assertEqual(default_quality["counts"]["enhancement_changed_files"], 0)
            self.assertEqual(default_quality["counts"]["cumulative_change_guard_checked_files"], 6)
            self.assertEqual(default_quality["counts"]["processed_output_safety_guard_checked_files"], 6)
            for operation in CONSERVATIVE_REPAIR_OPERATIONS:
                self.assertFalse(default_quality["algorithm_metrics"][operation]["enabled"], operation)
                self.assertEqual(default_quality["algorithm_metrics"][operation]["changed_files"], 0, operation)

            _assert_algorithm_thresholds(self, base_quality)
            _assert_algorithm_thresholds(self, full_quality)
            _assert_required_metrics_present(self, full_quality)
            _assert_operation_timing_signal(self, full_quality)
            _assert_local_content_guard_signal(self, full_quality)
            _assert_full_chain_quality_guard_signal(self, full_payload)

            self.assertGreater(base_run["processing"]["processed_files_per_minute"], 0)
            self.assertGreater(full_run["processing"]["processed_files_per_minute"], 0)
            throughput_ratio = round(
                full_run["processing"]["processed_files_per_minute"]
                / base_run["processing"]["processed_files_per_minute"],
                6,
            )
            self.assertGreater(throughput_ratio, 0)
            self.assertGreaterEqual(throughput_ratio, 0.01)
            for operation in REQUIRED_OPERATIONS:
                self.assertIn(operation, full_run["processing"]["operation_timings"])
                self.assertIn("files_per_minute", full_run["processing"]["operation_timings"][operation])

            for payload in (default_payload, base_payload, full_payload):
                raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                self.assertIn("quality_regression", raw)
                for forbidden in (
                    "private_default_page.png",
                    "private_edge_page.png",
                    "private_stain_page.png",
                    "private_scanline_page.png",
                    "private_faded_text_page.png",
                    "private_blurred_text_page.png",
                    str(input_dir),
                    "source_relative_path",
                    "source_sha256",
                    "OCR TEXT",
                    '"files": [',
                    '"findings": [',
                ):
                    self.assertNotIn(forbidden, raw)

    def test_full_chain_safe_combination_page_stays_conservative(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-full-chain-safe-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "synthetic_safe_combination.png"
            _safe_full_chain_combination_page().save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "safe-combination", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            with Image.open(process_dir / record["output_relative_path"]) as output:
                self.assertEqual(output.size, tuple(record["output_size"]))
            self.assertTrue(record["despeckled"])
            self.assertTrue(record["scanlines_lightened"])
            self.assertIn("despeckle_isolated_pixels", record["operations"])
            self.assertIn("lighten_scanlines_conservative", record["operations"])
            self.assertFalse(record["background_stains_lightened"])
            self.assertFalse(record["faded_text_enhanced"])
            self.assertFalse(record["text_edges_sharpened"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertFalse(audit["local_content_change_guard_reverted"])
            self.assertFalse(audit["cumulative_change_guard_reverted"])
            self.assertFalse(audit["combination_quality_guard_reverted"])
            self.assertGreater(audit["scanlines_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["scanlines_changed_pixel_ratio"], 0.04)
            self.assertLessEqual(audit["despeckle_pixel_ratio"], 0.001)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.05)
            self.assertLessEqual(audit["cumulative_change_score"], 0.25)
            self.assertLessEqual(audit["local_content_changed_ratio"], 0.02)
            despeckle_timing = audit_summary["timing"]["operation_timings"]["despeckle"]
            self.assertIn("component_count_bucket_distribution", despeckle_timing)
            self.assertIn("max_component_size_bucket_distribution", despeckle_timing)
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["counts"]["local_content_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["counts"]["combination_quality_guard_reverted_files"], 0)
            self.assertEqual(
                audit_summary["guardrails"]["combination_quality_guard"]["reason_code_distribution"][
                    "safe_combination_passed"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("synthetic_safe_combination.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_warm_mild_bleed_through_is_cleaned_without_private_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-warm-bleed-through-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_warm_reverse_ghost.png"
            page = _warm_mild_bleed_through_page()
            page.save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "warm-bleed-through", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(clean_bleed_through=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(record["bleed_through_cleaned"])
            self.assertEqual(record["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertEqual(audit["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertGreater(audit["bleed_through_delta"], 3.0)
            self.assertGreater(audit["bleed_through_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["bleed_through_changed_pixel_ratio"], 0.045)
            self.assertLessEqual(audit["bleed_through_candidate_pixel_ratio"], 0.065)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(audit["processed_output_safety_guard_action"], "passed")

            original = page.convert("RGB")
            ghost_box = (118, 80, 176, 122)
            before = ImageStat.Stat(original.crop(ghost_box).convert("L")).mean[0]
            after = ImageStat.Stat(processed.crop(ghost_box).convert("L")).mean[0]
            self.assertGreater(after - before, 0.08)
            protected_box = (30, 34, 72, 50)
            self.assertIsNone(
                ImageChops.difference(original.crop(protected_box), processed.crop(protected_box)).getbbox()
            )

            self.assertEqual(audit_summary["counts"]["bleed_through_cleaned_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["bleed_through"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["bleed_through"]["reason_code_distribution"][
                    "applied_faint_reverse_ghost"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("private_warm_reverse_ghost", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_mild_blue_gray_cast_stays_guarded_and_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-blue-gray-cast-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_full_chain_blue_gray_cast.png"
            image = Image.new("RGB", (240, 180), (235, 239, 243))
            draw = ImageDraw.Draw(image)
            for y in (42, 66, 90):
                draw.rectangle((36, y, 128, y + 3), fill=(58, 58, 58))
            image.save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "blue-gray-cast", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["paper_color_cast_normalized"])
            self.assertEqual(record["paper_color_cast_reason_code"], "applied_mild_uniform_scanner_cast")
            self.assertIn("normalize_paper_color_cast_conservative", record["operations"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_action"], "passed")
            self.assertEqual(audit["processed_output_safety_guard_action"], "passed")
            self.assertLessEqual(audit["paper_color_cast_delta"], 10.0)
            self.assertLessEqual(audit["paper_color_cast_brightness_delta"], 3.0)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.10)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], 1)
            self.assertEqual(audit_summary["counts"]["combination_quality_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["counts"]["processed_output_safety_guard_reverted_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "private_full_chain_blue_gray_cast.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_segmented_scanline_chain_stays_aggregate_and_guarded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-segmented-scanline-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_segmented_scanline.png"
            _segmented_scanline_page().save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "segmented-scanline", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["scanlines_lightened"])
            self.assertEqual(record["scanlines_orientation"], "horizontal")
            self.assertIn("lighten_scanlines_conservative", record["operations"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_action"], "passed")
            self.assertGreater(audit["scanlines_changed_pixel_ratio"], 0.0007)
            self.assertLessEqual(audit["scanlines_changed_pixel_ratio"], 0.04)
            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["scanlines"]["applied_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("private_segmented_scanline.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_safe_cloud_background_stain_stays_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-cloud-stain-combo-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_cloud_stain_combo.png"
            _safe_cloud_stain_combination_page().save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "cloud-stain-combo", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["despeckled"])
            self.assertTrue(record["background_stains_lightened"])
            self.assertIn("lighten_background_stains_conservative", record["operations"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertLessEqual(audit["background_stains_changed_pixel_ratio"], 0.085)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.09)
            self.assertLessEqual(audit["cumulative_change_score"], 0.20)
            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("private_cloud_stain_combo.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_reverts_low_contrast_foreground_weakening_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-faint-foreground-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_faint_foreground_guard.png"
            image = Image.new("RGB", (180, 120), (242, 242, 238))
            draw = ImageDraw.Draw(image)
            draw.ellipse((104, 24, 152, 68), fill=(224, 224, 220))
            for y in (38, 56, 74):
                draw.rectangle((34, y, 136, y + 3), fill=(207, 207, 203))
            draw.line((42, 94, 136, 104), fill=(205, 205, 201), width=2)
            image.save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            def lift_faint_strokes(current: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                changed = current.copy()
                changed_draw = ImageDraw.Draw(changed)
                changed_draw.ellipse((104, 24, 152, 68), fill=(235, 235, 232))
                for y in (38, 56, 74):
                    changed_draw.rectangle((34, y, 136, y + 3), fill=(235, 235, 232))
                return processing_module.BackgroundStainLighteningResult(
                    changed,
                    True,
                    "background stains lightened: stable isolated stains on light paper",
                    224.0,
                    235.0,
                    11.0,
                    0.050,
                    0.050,
                )

            def lift_handwriting(current: Image.Image) -> processing_module.ScanlineLighteningResult:
                changed = current.copy()
                ImageDraw.Draw(changed).line((42, 94, 136, 104), fill=(235, 235, 232), width=2)
                return processing_module.ScanlineLighteningResult(
                    changed,
                    True,
                    "scanlines lightened: stable horizontal scanline pattern",
                    "horizontal",
                    1,
                    205.0,
                    235.0,
                    30.0,
                    0.010,
                    0.010,
                )

            report = scan_batch(ScanConfig("synthetic-regression", "faint-foreground-guard", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_lighten_background_stains_conservative",
                side_effect=lift_faint_strokes,
            ), mock.patch.object(
                processing_module,
                "_lighten_scanlines_conservative",
                side_effect=lift_handwriting,
            ):
                manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["background_stains_lightened"])
            self.assertFalse(record["scanlines_lightened"])
            self.assertEqual(audit["processed_output_safety_guard_action"], "reverted_to_source")
            self.assertEqual(audit["processed_output_safety_guard_reason_code"], "processed_output_quality_reverted")
            self.assertIn("protected_foreground_weakening", audit["processed_output_safety_guard_reasons"])
            self.assertIn("processed_output_safety_guard_reverted_to_source", record["operations"])
            with Image.open(process_dir / record["output_relative_path"]) as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            self.assertEqual(audit_summary["counts"]["processed_output_safety_guard_reverted_files"], 1)
            self.assertEqual(
                audit_summary["counts"]["processed_output_foreground_weakening_guard_reverted_files"],
                1,
            )
            self.assertEqual(
                audit_summary["guardrails"]["processed_output_safety_guard"]["reason_distribution"][
                    "protected_foreground_weakening"
                ],
                1,
            )
            quality = _processing_quality_regression(manifest)
            self.assertEqual(quality["counts"]["processed_output_foreground_weakening_guard_reverted_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("private_faint_foreground_guard", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_reverts_edge_annotation_foreground_weakening_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-edge-annotation-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_edge_annotation_guard.png"
            image = Image.new("RGB", (180, 120), (242, 242, 238))
            draw = ImageDraw.Draw(image)
            draw.ellipse((86, 34, 152, 84), fill=(224, 224, 220))
            draw.rectangle((4, 92, 24, 98), fill=(205, 205, 201))
            draw.text((144, 8), "12", fill=(206, 206, 202))
            image.save(source, dpi=(300, 300))

            def lift_page_number(current: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                changed = current.copy()
                changed_draw = ImageDraw.Draw(changed)
                changed_draw.ellipse((86, 34, 152, 84), fill=(235, 235, 232))
                changed_draw.text((144, 8), "12", fill=(235, 235, 232))
                return processing_module.BackgroundStainLighteningResult(
                    changed,
                    True,
                    "background stains lightened: stable isolated stains on light paper",
                    224.0,
                    235.0,
                    11.0,
                    0.045,
                    0.045,
                )

            def lift_edge_mark(current: Image.Image) -> processing_module.ScanlineLighteningResult:
                changed = current.copy()
                ImageDraw.Draw(changed).rectangle((4, 92, 24, 98), fill=(235, 235, 232))
                return processing_module.ScanlineLighteningResult(
                    changed,
                    True,
                    "scanlines lightened: stable horizontal scanline pattern",
                    "horizontal",
                    1,
                    205.0,
                    235.0,
                    30.0,
                    0.008,
                    0.008,
                )

            report = scan_batch(ScanConfig("synthetic-regression", "edge-annotation-guard", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_lighten_background_stains_conservative",
                side_effect=lift_page_number,
            ), mock.patch.object(
                processing_module,
                "_lighten_scanlines_conservative",
                side_effect=lift_edge_mark,
            ):
                manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["background_stains_lightened"])
            self.assertFalse(record["scanlines_lightened"])
            self.assertEqual(audit["processed_output_safety_guard_action"], "reverted_to_source")
            self.assertIn("protected_foreground_weakening", audit["processed_output_safety_guard_reasons"])
            with Image.open(process_dir / record["output_relative_path"]) as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            self.assertEqual(
                audit_summary["guardrails"]["processed_output_safety_guard"]["reason_distribution"][
                    "protected_foreground_weakening"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("private_edge_annotation_guard", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_low_confidence_combination_preserves_original_with_public_reason_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-low-confidence-combo-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "synthetic_low_confidence_combo.png"
            Image.new("RGB", (150, 110), (244, 244, 244)).save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "low-confidence-combo", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(enhance_faded_text=True, sharpen_text_edges=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["faded_text_enhanced"])
            self.assertFalse(record["text_edges_sharpened"])
            self.assertEqual(audit["combination_quality_guard_action"], "kept_original")
            self.assertEqual(
                audit["combination_quality_guard_reason_code"],
                "low_confidence_original_preserved",
            )
            self.assertEqual(
                audit_summary["guardrails"]["combination_quality_guard"]["reason_code_distribution"][
                    "low_confidence_original_preserved"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("synthetic_low_confidence_combo.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_trim_dark_border_auto_crop_combination_keeps_narrow_gray_edge_change_controlled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-narrow-gray-trim-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (160, 120), (244, 244, 240))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 159, 119), outline=(94, 94, 94), width=3)
            draw.rectangle((54, 45, 106, 50), fill=(20, 20, 20))
            draw.rectangle((54, 62, 118, 67), fill=(20, 20, 20))
            image.save(input_dir / "synthetic_narrow_gray_combo.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "narrow-gray-trim", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(trim_dark_border=True, auto_crop=True, deskew=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertTrue(record["dark_border_trimmed"])
            self.assertEqual(record["dark_border_bbox"], [3, 3, 157, 117])
            self.assertFalse(record["cropped"])
            self.assertEqual(record["crop_reason"], "candidate crop exceeds conservative crop ratio")
            self.assertEqual(record["output_size"], [154, 114])
            self.assertLessEqual(audit["max_trim_margin_ratio"], 0.025)
            self.assertLessEqual(audit["cumulative_change_crop_ratio"], 0.025)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("synthetic_narrow_gray_combo.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_shallow_deskew_auto_crop_trim_combination_stays_controlled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-shallow-deskew-combo-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _shallow_stable_text_page().rotate(
                -0.45,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(246, 246, 246),
            )
            image.save(input_dir / "synthetic_shallow_deskew_combo.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "shallow-deskew-combo", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(trim_dark_border=True, auto_crop=True, deskew=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertTrue(record["deskewed"])
            self.assertAlmostEqual(record["skew_angle_degrees"], -0.45, delta=0.25)
            self.assertFalse(record["dark_border_trimmed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew safe canvas crop applied")
            self.assertEqual(record["crop_bbox"], [2, 1, 246, 323])
            self.assertEqual(record["output_size"], record["pre_deskew_size"])
            self.assertLessEqual(audit["size_change_ratio"], 0.01)
            self.assertEqual(audit["max_trim_margin_ratio"], 0.0)
            self.assertEqual(audit["crop_ratio"], 0.0)
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit_summary["counts"]["deskewed_files"], 1)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["post_deskew_safe_crop_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["cropped_side_distribution"],
                {"left": 1, "top": 1, "right": 1, "bottom": 1},
            )
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["reason_distribution"],
                {"post-deskew safe canvas crop applied": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("synthetic_shallow_deskew_combo.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_post_deskew_corner_wedge_crop_stays_within_geometry_limits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-post-deskew-wedge-full-chain-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (320, 240), (248, 248, 248))
            draw = ImageDraw.Draw(image)
            draw.rectangle((58, 50, 262, 184), outline=(160, 160, 160), width=1)
            for y in (70, 92, 114, 136, 158):
                draw.rectangle((72, y, 248, y + 3), fill=(35, 35, 35))
            image.rotate(
                1.0,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(245, 245, 245),
            ).save(input_dir / "private_full_chain_post_deskew_wedge.png", dpi=(300, 300))

            report = scan_batch(
                ScanConfig("synthetic-regression", "post-deskew-wedge-full-chain", input_dir, output_dir)
            )
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    scanner_gutter_trim=True,
                    despeckle=True,
                    normalize_tones=True,
                    lighten_edge_shadow=True,
                    lighten_background_stains=True,
                    level_illumination_gradient=True,
                    lighten_scanlines=True,
                    enhance_faded_text=True,
                    sharpen_text_edges=True,
                    workers=1,
                ),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew safe canvas crop applied")
            self.assertEqual(record["output_size"], [318, 238])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed")
            self.assertLessEqual(audit["crop_ratio"], 0.06)
            self.assertLessEqual(audit["cumulative_change_crop_ratio"], 0.06)
            self.assertEqual(audit["max_trim_margin_ratio"], 0.0)
            self.assertTrue(audit_summary["operations"]["deskew"])
            self.assertTrue(audit_summary["operations"]["trim_dark_border"])
            self.assertTrue(audit_summary["operations"]["scanner_gutter_trim"])
            self.assertTrue(audit_summary["operations"]["auto_crop"])
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in (
                "private_full_chain_post_deskew_wedge.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_variable_pale_gutter_trim_stays_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-variable-gutter-full-chain-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (260, 190), (239, 239, 239))
            draw = ImageDraw.Draw(image)
            for y in range(190):
                for x in range(15):
                    value = 237 if (x + y) % 2 == 0 else 251
                    image.putpixel((x, y), (value, value, value))
            draw.rectangle((54, 38, 222, 154), outline=(80, 80, 80), width=2)
            for y in range(68, 130, 24):
                draw.rectangle((70, y, 180, y + 2), fill=(70, 70, 70))
            image.save(input_dir / "private_full_chain_variable_pale_gutter.png", dpi=(300, 300))

            report = scan_batch(
                ScanConfig("synthetic-regression", "variable-gutter-full-chain", input_dir, output_dir)
            )
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["scanner_gutter_trimmed"])
            self.assertEqual(record["scanner_gutter_reason"], "scanner gutter trim applied")
            self.assertEqual(record["output_size"], [245, 190])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertLessEqual(audit["scanner_gutter_max_trim_margin_ratio"], 0.06)
            self.assertLessEqual(audit["max_trim_margin_ratio"], 0.06)
            self.assertLessEqual(audit["cumulative_change_crop_ratio"], 0.06)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.08)
            self.assertTrue(audit_summary["operations"]["deskew"])
            self.assertTrue(audit_summary["operations"]["scanner_gutter_trim"])
            self.assertTrue(audit_summary["operations"]["auto_crop"])
            self.assertTrue(audit_summary["operations"]["lighten_scanlines"])
            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "private_full_chain_variable_pale_gutter.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_risk_combination_pages_skip_or_stay_low_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-full-chain-risk-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_table_page_number_annotation.png": _risk_table_page_number_annotation_page(),
                "synthetic_stamp_header_footer.png": _risk_stamp_header_footer_page(),
                "synthetic_dark_photo_edge_trace.png": _risk_dark_photo_edge_trace_page(),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "risk-combination", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            self.assertEqual(manifest["summary"]["processed_files"], len(pages))
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            for record in manifest["files"]:
                audit = record["processing_audit"]
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.size, tuple(record["output_size"]))
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["background_stains_lightened"], record["source_relative_path"])
                self.assertFalse(record["scanlines_lightened"], record["source_relative_path"])
                self.assertFalse(record["faded_text_enhanced"], record["source_relative_path"])
                self.assertFalse(record["text_edges_sharpened"], record["source_relative_path"])
                self.assertFalse(record["cropped"], record["source_relative_path"])
                self.assertEqual(audit["guardrail_failures"], [])
                self.assertIn(audit["cumulative_change_guard_action"], {"passed", "reverted_to_source"})
                self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.01)
                self.assertLessEqual(audit["cumulative_change_score"], 0.08)

            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 0)
            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 0)
            self.assertEqual(audit_summary["counts"]["faded_text_enhanced_files"], 0)
            self.assertEqual(audit_summary["counts"]["text_edges_sharpened_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["faded_text"]["applied_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["text_edges"]["applied_files"], 0)
            self.assertGreaterEqual(audit_summary["guardrails"]["faded_text"]["protection_triggered_files"], 2)
            self.assertGreaterEqual(audit_summary["guardrails"]["text_edges"]["protection_triggered_files"], 1)
            self.assertGreaterEqual(audit_summary["guardrails"]["scanlines"]["protection_triggered_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_quality_regression_reports_missing_operation_timing_code_without_private_rows(self) -> None:
        quality = _processing_quality_regression(
            {
                "summary": {
                    "total_files": 2,
                    "processed_files": 2,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "performance": {"operation_timings": {"deskew": {"enabled": True, "file_count": 2}}},
                },
                "files": [
                    {
                        "source_relative_path": "private_missing_timing_page.png",
                        "source_sha256": "a" * 64,
                        "processing_audit": {"cumulative_change_guard_checked": True},
                    }
                ],
            }
        )
        raw = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.assertEqual(quality["status"], "failed")
        integrity = quality["operation_timing_integrity"]
        self.assertTrue(integrity["aggregate_only"])
        self.assertEqual(integrity["status"], "missing")
        self.assertEqual(integrity["missing_code"], "missing_or_incomplete_processing_operation_timings")
        self.assertIn("auto_crop", integrity["missing_operations"])
        self.assertEqual(integrity["incomplete_operations"][0]["operation"], "deskew")
        self.assertIn("elapsed_seconds", integrity["incomplete_operations"][0]["missing_fields"])
        self.assertEqual(quality["slow_operations"][0]["operation"], "deskew")
        for forbidden in (
            "private_missing_timing_page.png",
            "source_relative_path",
            "source_sha256",
            "OCR TEXT",
            "a" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_quality_regression_aggregates_local_guard_reasons_without_private_rows(self) -> None:
        quality = _processing_quality_regression(
            {
                "summary": {
                    "total_files": 3,
                    "processed_files": 3,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "performance": {"operation_timings": _operation_timings_fixture()},
                },
                "files": [
                    {
                        "source_relative_path": "private_safe_stain_cleanup.png",
                        "source_sha256": "b" * 64,
                        "processing_audit": {
                            "background_stains_lightened": True,
                            "local_content_change_guard_checked": False,
                            "local_content_change_guard_reverted": False,
                            "local_content_change_guard_action": "passed",
                            "local_content_change_guard_reasons": [],
                            "combination_quality_guard_checked": False,
                            "combination_quality_guard_reverted": False,
                            "combination_quality_guard_action": "passed",
                            "combination_quality_guard_reason_code": "safe_combination_passed",
                            "combination_quality_guard_risk_tier": "low_risk_background",
                            "cumulative_change_score": 0.03,
                            "local_content_changed_ratio": 0.0,
                        },
                    },
                    {
                        "source_relative_path": "private_risky_text_damage.png",
                        "source_sha256": "c" * 64,
                        "processing_audit": {
                            "scanlines_lightened": True,
                            "local_content_change_guard_checked": True,
                            "local_content_change_guard_reverted": True,
                            "local_content_change_guard_action": "reverted_to_source",
                            "local_content_change_guard_reasons": [
                                "edge_content_changed_ratio",
                                "local_content_changed_ratio",
                            ],
                            "combination_quality_guard_checked": True,
                            "combination_quality_guard_reverted": True,
                            "combination_quality_guard_action": "reverted_to_source",
                            "combination_quality_guard_reason_code": "combined_change_too_large_reverted",
                            "combination_quality_guard_risk_tier": "high_risk_content",
                            "combination_quality_guard_reasons": ["cumulative_change_score"],
                            "processed_output_safety_guard_checked": True,
                            "processed_output_safety_guard_reverted": True,
                            "processed_output_safety_guard_action": "reverted_to_source",
                            "processed_output_safety_guard_reason_code": "processed_output_quality_reverted",
                            "processed_output_safety_guard_reasons": ["protected_foreground_weakening"],
                            "cumulative_change_score": 0.22,
                            "local_content_changed_ratio": 0.19,
                        },
                    },
                    {
                        "source_relative_path": "private_risky_text_sharpen.png",
                        "source_sha256": "d" * 64,
                        "processing_audit": {
                            "text_edges_sharpened": True,
                            "local_content_change_guard_checked": True,
                            "local_content_change_guard_reverted": False,
                            "local_content_change_guard_action": "passed",
                            "local_content_change_guard_reasons": [],
                            "combination_quality_guard_checked": True,
                            "combination_quality_guard_reverted": False,
                            "combination_quality_guard_action": "passed",
                            "combination_quality_guard_reason_code": "safe_combination_passed",
                            "combination_quality_guard_risk_tier": "low_risk_background",
                            "processed_output_safety_guard_checked": True,
                            "processed_output_safety_guard_reverted": False,
                            "processed_output_safety_guard_action": "passed",
                            "processed_output_safety_guard_reason_code": "safe_processed_output_passed",
                            "processed_output_safety_guard_reasons": [],
                            "cumulative_change_score": 0.05,
                            "local_content_changed_ratio": 0.02,
                        },
                    },
                ],
            }
        )
        raw = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.assertEqual(quality["status"], "pass")
        self.assertEqual(quality["counts"]["local_content_change_guard_checked_files"], 2)
        self.assertEqual(quality["counts"]["local_content_change_guard_skipped_files"], 1)
        self.assertEqual(quality["counts"]["local_content_change_guard_reverted_files"], 1)
        self.assertEqual(quality["counts"]["combination_quality_guard_checked_files"], 2)
        self.assertEqual(quality["counts"]["combination_quality_guard_reverted_files"], 1)
        self.assertEqual(quality["counts"]["processed_output_foreground_weakening_guard_reverted_files"], 1)
        local_guard = quality["local_content_change_guard"]
        self.assertTrue(local_guard["aggregate_only"])
        self.assertEqual(local_guard["checked_files"], 2)
        self.assertEqual(local_guard["skipped_files"], 1)
        self.assertEqual(local_guard["reverted_files"], 1)
        self.assertEqual(local_guard["reason_distribution"]["local_content_changed_ratio"], 1)
        self.assertEqual(local_guard["reason_distribution"]["edge_content_changed_ratio"], 1)
        combination_guard = quality["combination_quality_guard"]
        self.assertTrue(combination_guard["aggregate_only"])
        self.assertEqual(combination_guard["checked_files"], 2)
        self.assertEqual(combination_guard["reverted_files"], 1)
        self.assertEqual(combination_guard["reason_code_distribution"]["combined_change_too_large_reverted"], 1)
        processed_output_guard = quality["processed_output_safety_guard"]
        self.assertEqual(processed_output_guard["foreground_weakening_reverted_files"], 1)
        self.assertEqual(
            processed_output_guard["reason_distribution"]["protected_foreground_weakening"],
            1,
        )
        metric_signal = quality["guardrail_metric_signal"]
        self.assertTrue(metric_signal["aggregate_only"])
        self.assertEqual(metric_signal["metrics"]["cumulative_change_score"]["count"], 3)
        self.assertEqual(metric_signal["metrics"]["local_content_changed_ratio"]["max"], 0.19)
        self.assertEqual(quality["operation_timing_budget"]["status"], "pass")
        for forbidden in (
            "private_safe_stain_cleanup.png",
            "private_risky_text_damage.png",
            "private_risky_text_sharpen.png",
            "source_relative_path",
            "source_sha256",
            "b" * 64,
            "c" * 64,
            "d" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_quality_regression_reports_timing_budget_blocker_without_private_rows(self) -> None:
        operation_timings = _operation_timings_fixture()
        operation_timings["lighten_scanlines"] = {
            "enabled": True,
            "file_count": 2,
            "elapsed_seconds": 1.2,
            "files_per_minute": 100.0,
            "average_seconds_per_file": 0.6,
        }
        quality = _processing_quality_regression(
            {
                "summary": {
                    "total_files": 2,
                    "processed_files": 2,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "performance": {
                        "operation_timings": operation_timings,
                        "operation_timing_budget": {
                            "mode": "blocking",
                            "budgets_seconds_per_file": {"lighten_scanlines": 0.35},
                        },
                    },
                },
                "files": [
                    {
                        "source_relative_path": "private_slow_scanline_page.png",
                        "source_sha256": "e" * 64,
                        "processing_audit": {"local_content_change_guard_checked": True},
                    }
                ],
            }
        )
        raw = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.assertEqual(quality["status"], "failed")
        timing_budget = quality["operation_timing_budget"]
        self.assertTrue(timing_budget["aggregate_only"])
        self.assertEqual(timing_budget["status"], "failed")
        self.assertEqual(timing_budget["mode"], "blocking")
        self.assertEqual(timing_budget["budget_source"], "calibrated")
        self.assertEqual(timing_budget["blocker_code"], "processing_operation_timing_budget_exceeded")
        self.assertIsNone(timing_budget["diagnostic_code"])
        self.assertEqual(timing_budget["over_budget_operations"][0]["operation"], "lighten_scanlines")
        self.assertEqual(timing_budget["over_budget_operations"][0]["average_seconds_per_file"], 0.6)
        self.assertEqual(timing_budget["over_budget_operations"][0]["file_count"], 2)
        self.assertIn("lighten_scanlines", timing_budget["budgets_seconds_per_file"])
        for forbidden in (
            "private_slow_scanline_page.png",
            "source_relative_path",
            "source_sha256",
            "e" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_quality_regression_reports_default_timing_budget_as_diagnostic(self) -> None:
        quality = _processing_quality_regression(
            {
                "summary": {
                    "total_files": 20,
                    "processed_files": 20,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "performance": {"operation_timings": _fixed_sample_operation_timings()},
                },
                "files": [{"processing_audit": {}} for _ in range(20)],
            }
        )

        self.assertEqual(quality["status"], "pass")
        timing_budget = quality["operation_timing_budget"]
        self.assertTrue(timing_budget["aggregate_only"])
        self.assertEqual(timing_budget["status"], "pass")
        self.assertEqual(timing_budget["mode"], "diagnostic")
        self.assertEqual(timing_budget["budget_source"], "diagnostic_defaults")
        self.assertIsNone(timing_budget["blocker_code"])
        self.assertEqual(timing_budget["diagnostic_code"], "processing_operation_timing_budget_diagnostic")
        self.assertEqual(
            [operation["operation"] for operation in timing_budget["over_budget_operations"]],
            ["auto_crop", "deskew", "despeckle", "lighten_background_stains", "clean_bleed_through"],
        )

    def test_synthetic_performance_signal_covers_conservative_operations_without_private_rows(self) -> None:
        comparison = _synthetic_performance_comparison_module()
        operation_timings = {
            operation: {
                "enabled": operation in BASE_OPERATION_NAMES,
                "file_count": 4 if operation in BASE_OPERATION_NAMES else 0,
                "elapsed_seconds": 0.04 if operation in BASE_OPERATION_NAMES else 0.0,
                "files_per_minute": 6000.0 if operation in BASE_OPERATION_NAMES else 0.0,
                "average_seconds_per_file": 0.01 if operation in BASE_OPERATION_NAMES else None,
            }
            for operation in REQUIRED_OPERATIONS
            if operation != "lighten_scanlines"
        }
        operation_timings["despeckle"]["backend_counts"] = {
            "numpy": 0,
            "fallback": 4,
            "not_applicable": 0,
            "unknown": 0,
        }
        benchmark = {
            "runs": [
                {
                    "processing": {
                        "operation_timings": operation_timings,
                        "private_path": "/private/archive/private_scan_page.png",
                        "source_sha256": "f" * 64,
                    }
                }
            ]
        }

        signal = comparison._operation_timing_regression_signal(benchmark)
        raw = json.dumps(signal, ensure_ascii=False, sort_keys=True)

        self.assertTrue(signal["aggregate_only"])
        self.assertEqual(signal["required_operations"], list(REQUIRED_OPERATIONS))
        self.assertFalse(signal["signal_available"])
        self.assertEqual(signal["missing_operations"], ["lighten_scanlines"])
        self.assertEqual(set(signal["operations"]), set(REQUIRED_OPERATIONS))
        self.assertEqual(
            signal["operations"]["lighten_scanlines"]["missing_reason"],
            "missing_from_benchmark_processing_operation_timings",
        )
        self.assertTrue(signal["operations"]["trim_dark_border"]["signal_available"])
        self.assertFalse(signal["operations"]["enhance_faded_text"]["enabled"])
        self.assertEqual(signal["operations"]["despeckle"]["backend_mode"], "fallback")
        for value in signal["privacy"].values():
            self.assertFalse(value)
        for forbidden in (
            "/private/archive",
            "private_scan_page.png",
            "source_sha256",
            "OCR TEXT",
            "thumbnail_data",
            '"findings": [',
            "f" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_synthetic_full_chain_guard_requires_enabled_operations_without_private_rows(self) -> None:
        comparison = _synthetic_performance_comparison_module()
        operation_timings = _operation_timings_fixture()
        operation_timings["lighten_fold_shadows"] = dict(operation_timings["lighten_fold_shadows"], enabled=False)
        benchmark = _full_chain_benchmark_fixture(operation_timings, elapsed_seconds=0.6, processed_files=4)

        guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(benchmark),
                    "full_chain_budget_signal": comparison._full_chain_budget_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                    "full_chain_quality_guard_signal": comparison._full_chain_quality_guard_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                    "private_path": "/private/archive/private_full_chain_page.png",
                    "source_sha256": "g" * 64,
                }
            ]
        )
        raw = json.dumps(guard, ensure_ascii=False, sort_keys=True)

        self.assertEqual(guard["status"], "failed")
        self.assertEqual(guard["code"], "full_chain_operation_not_enabled")
        self.assertEqual(guard["disabled_operations"], ["lighten_fold_shadows"])
        for value in guard["privacy"].values():
            self.assertFalse(value)
        for forbidden in (
            "/private/archive",
            "private_full_chain_page.png",
            "source_sha256",
            "g" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_synthetic_full_chain_guard_reports_budget_failures_without_private_rows(self) -> None:
        comparison = _synthetic_performance_comparison_module()
        benchmark = _full_chain_benchmark_fixture(
            _operation_timings_fixture(),
            elapsed_seconds=comparison.FULL_CHAIN_SYNTHETIC_BUDGET_SECONDS_PER_FILE * 4 + 0.4,
            processed_files=4,
        )
        budget_signal = comparison._full_chain_budget_signal({"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark)
        guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(benchmark),
                    "full_chain_budget_signal": budget_signal,
                    "full_chain_quality_guard_signal": comparison._full_chain_quality_guard_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                }
            ]
        )
        raw = json.dumps(guard, ensure_ascii=False, sort_keys=True)

        self.assertEqual(budget_signal["status"], "failed")
        self.assertEqual(budget_signal["code"], "full_chain_processing_budget_exceeded")
        self.assertEqual(guard["status"], "failed")
        self.assertEqual(guard["code"], "full_chain_processing_budget_exceeded")
        self.assertEqual(guard["budget_signal"]["over_budget_runs"][0]["processed_files"], 4)
        for forbidden in (
            "/private/archive",
            "private_full_chain_page.png",
            "source_sha256",
            "h" * 64,
        ):
            self.assertNotIn(forbidden, raw)

        passing_benchmark = _full_chain_benchmark_fixture(
            _operation_timings_fixture(),
            elapsed_seconds=0.8,
            processed_files=4,
        )
        passing_guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(
                        passing_benchmark
                    ),
                    "full_chain_budget_signal": comparison._full_chain_budget_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, passing_benchmark
                    ),
                    "full_chain_quality_guard_signal": comparison._full_chain_quality_guard_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, passing_benchmark
                    ),
                }
            ]
        )

        self.assertEqual(passing_guard["status"], "pass")
        self.assertEqual(passing_guard["budget_signal"]["status"], "pass")

    def test_synthetic_full_chain_guard_requires_quality_guard_signal_without_private_rows(self) -> None:
        comparison = _synthetic_performance_comparison_module()
        benchmark = _full_chain_benchmark_fixture(
            _operation_timings_fixture(),
            elapsed_seconds=0.8,
            processed_files=4,
        )
        quality_signal = comparison._full_chain_quality_guard_signal(
            {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
        )
        guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(benchmark),
                    "full_chain_budget_signal": comparison._full_chain_budget_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                    "full_chain_quality_guard_signal": quality_signal,
                    "private_path": "/private/archive/private_guard_quality_page.png",
                    "source_sha256": "i" * 64,
                }
            ]
        )
        raw = json.dumps(guard, ensure_ascii=False, sort_keys=True)

        self.assertEqual(quality_signal["status"], "pass")
        self.assertEqual(quality_signal["runs"][0]["counts"]["combination_quality_guard_checked_files"], 4)
        self.assertIn("guardrail_metric_signal", quality_signal["runs"][0])
        self.assertIn("algorithm_metrics", quality_signal["runs"][0])
        self.assertEqual(guard["status"], "pass")
        self.assertEqual(guard["quality_guard_signal"]["status"], "pass")
        for forbidden in (
            "/private/archive",
            "private_guard_quality_page.png",
            "source_sha256",
            "i" * 64,
        ):
            self.assertNotIn(forbidden, raw)

        missing_quality_guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(benchmark),
                    "full_chain_budget_signal": comparison._full_chain_budget_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                }
            ]
        )
        self.assertEqual(missing_quality_guard["status"], "failed")
        self.assertEqual(missing_quality_guard["code"], "missing_full_chain_quality_guard_signal")


BASE_FLAGS = ("--deskew", "--trim-dark-border", "--scanner-gutter-trim", "--auto-crop", "--despeckle")
BASE_OPERATION_NAMES = ("auto_crop", "deskew", "trim_dark_border", "scanner_gutter_trim", "despeckle")
CONSERVATIVE_REPAIR_FLAGS = (
    "--normalize-tones",
    "--normalize-paper-color-cast",
    "--lighten-edge-shadow",
    "--lighten-corner-shadows",
    "--lighten-background-stains",
    "--lighten-fold-shadows",
    "--level-illumination-gradient",
    "--clean-bleed-through",
    "--lighten-scanlines",
    "--enhance-faded-text",
    "--sharpen-text-edges",
)
REQUIRED_OPERATIONS = (
    "deskew",
    "trim_dark_border",
    "scanner_gutter_trim",
    "auto_crop",
    "despeckle",
    "normalize_tones",
    "normalize_paper_color_cast",
    "lighten_edge_shadow",
    "lighten_corner_shadows",
    "lighten_background_stains",
    "lighten_fold_shadows",
    "level_illumination_gradient",
    "clean_bleed_through",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
)
CONSERVATIVE_REPAIR_OPERATIONS = (
    "normalize_tones",
    "normalize_paper_color_cast",
    "lighten_edge_shadow",
    "lighten_corner_shadows",
    "lighten_background_stains",
    "lighten_fold_shadows",
    "level_illumination_gradient",
    "clean_bleed_through",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
)


def _full_chain_options() -> ProcessingOptions:
    return ProcessingOptions(
        auto_crop=True,
        deskew=True,
        trim_dark_border=True,
        scanner_gutter_trim=True,
        despeckle=True,
        normalize_tones=True,
        normalize_paper_color_cast=True,
        lighten_edge_shadow=True,
        lighten_corner_shadows=True,
        lighten_background_stains=True,
        lighten_fold_shadows=True,
        level_illumination_gradient=True,
        clean_bleed_through=True,
        lighten_scanlines=True,
        enhance_faded_text=True,
        sharpen_text_edges=True,
        despeckle_backend="fallback",
        workers=1,
    )


def _benchmark_combo(root: Path, input_dir: Path, label: str, *flags: str) -> dict[str, object]:
    output_dir = root / f"benchmark-{label}"
    process_dir = root / f"processed-{label}"
    flag_set = set(flags)
    return run_benchmark(
        argparse.Namespace(
            input=input_dir,
            out=output_dir,
            process_out=process_dir,
            project="synthetic-regression",
            batch="synthetic-regression",
            workers_list=[1],
            repeats=1,
            scan_only=False,
            auto_crop="--auto-crop" in flag_set,
            deskew="--deskew" in flag_set,
            trim_dark_border="--trim-dark-border" in flag_set,
            scanner_gutter_trim="--scanner-gutter-trim" in flag_set,
            despeckle="--despeckle" in flag_set,
            normalize_tones="--normalize-tones" in flag_set,
            normalize_paper_color_cast="--normalize-paper-color-cast" in flag_set,
            lighten_edge_shadow="--lighten-edge-shadow" in flag_set,
            lighten_corner_shadows="--lighten-corner-shadows" in flag_set,
            lighten_background_stains="--lighten-background-stains" in flag_set,
            lighten_fold_shadows="--lighten-fold-shadows" in flag_set,
            level_illumination_gradient="--level-illumination-gradient" in flag_set,
            clean_bleed_through="--clean-bleed-through" in flag_set,
            lighten_scanlines="--lighten-scanlines" in flag_set,
            enhance_faded_text="--enhance-faded-text" in flag_set,
            sharpen_text_edges="--sharpen-text-edges" in flag_set,
            reuse_scan_measurements=False,
            despeckle_backend="fallback",
            min_dpi=None,
            name_pattern=None,
            manifest_csv=None,
            rules_profile=None,
        )
    )


def _single_quality(payload: dict[str, object]) -> dict[str, object]:
    runs = payload["runs"]
    assert isinstance(runs, list)
    run = runs[0]
    assert isinstance(run, dict)
    processing = run["processing"]
    assert isinstance(processing, dict)
    quality = processing["quality_regression"]
    assert isinstance(quality, dict)
    return quality


def _assert_required_metrics_present(
    testcase: unittest.TestCase, quality: dict[str, object]
) -> None:
    algorithm_metrics = quality["algorithm_metrics"]
    assert isinstance(algorithm_metrics, dict)
    expected = {
        "despeckle": ("pixel_ratio",),
        "normalize_tones": ("background_delta", "contrast_delta", "changed_pixel_ratio"),
        "normalize_paper_color_cast": (
            "delta",
            "brightness_delta",
            "changed_pixel_ratio",
            "candidate_pixel_ratio",
        ),
        "lighten_edge_shadow": ("delta", "changed_pixel_ratio"),
        "lighten_corner_shadows": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "lighten_background_stains": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "lighten_fold_shadows": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "level_illumination_gradient": (
            "correction_delta",
            "changed_pixel_ratio",
            "candidate_pixel_ratio",
        ),
        "clean_bleed_through": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "lighten_scanlines": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "enhance_faded_text": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio", "candidate_text_ratio"),
        "sharpen_text_edges": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
    }
    for operation, metric_names in expected.items():
        metrics = algorithm_metrics[operation]["metrics"]
        for metric_name in metric_names:
            testcase.assertIn(metric_name, metrics, operation)
            testcase.assertEqual(metrics[metric_name]["count"], 6, f"{operation}.{metric_name}")


def _assert_operation_timing_signal(testcase: unittest.TestCase, quality: dict[str, object]) -> None:
    integrity = quality["operation_timing_integrity"]
    testcase.assertTrue(integrity["aggregate_only"])
    testcase.assertEqual(integrity["status"], "pass")
    testcase.assertIsNone(integrity["missing_code"])
    testcase.assertEqual(integrity["missing_operations"], [])
    testcase.assertEqual(integrity["incomplete_operations"], [])
    timing_budget = quality["operation_timing_budget"]
    testcase.assertTrue(timing_budget["aggregate_only"])
    testcase.assertEqual(timing_budget["status"], "pass")
    testcase.assertIsNone(timing_budget["blocker_code"])
    testcase.assertEqual(timing_budget["over_budget_operations"], [])
    testcase.assertIn("lighten_scanlines", timing_budget["budgets_seconds_per_file"])
    slow_operations = quality["slow_operations"]
    testcase.assertGreaterEqual(len(slow_operations), 3)
    previous_average = None
    for summary in slow_operations:
        testcase.assertIn(summary["operation"], REQUIRED_OPERATIONS)
        testcase.assertIn("enabled", summary)
        testcase.assertGreaterEqual(summary["file_count"], 0)
        testcase.assertIsNotNone(summary["average_seconds_per_file"])
        testcase.assertIsNotNone(summary["files_per_minute"])
        if previous_average is not None:
            testcase.assertGreaterEqual(previous_average, summary["average_seconds_per_file"])
        previous_average = summary["average_seconds_per_file"]


def _assert_local_content_guard_signal(testcase: unittest.TestCase, quality: dict[str, object]) -> None:
    counts = quality["counts"]
    local_guard = quality["local_content_change_guard"]
    testcase.assertTrue(local_guard["aggregate_only"])
    testcase.assertEqual(counts["local_content_change_guard_checked_files"], 2)
    testcase.assertEqual(counts["local_content_change_guard_skipped_files"], 4)
    testcase.assertEqual(local_guard["checked_files"], 2)
    testcase.assertEqual(local_guard["skipped_files"], 4)
    testcase.assertEqual(local_guard["reverted_files"], 0)
    testcase.assertEqual(local_guard["reason_distribution"], {})


def _assert_full_chain_quality_guard_signal(testcase: unittest.TestCase, payload: dict[str, object]) -> None:
    quality = _single_quality(payload)
    counts = quality["counts"]
    testcase.assertEqual(counts["combination_quality_guard_checked_files"], 6)
    testcase.assertEqual(counts["processed_output_safety_guard_checked_files"], 6)
    testcase.assertIn("processed_output_foreground_weakening_guard_reverted_files", counts)
    combination_guard = quality["combination_quality_guard"]
    testcase.assertTrue(combination_guard["aggregate_only"])
    testcase.assertEqual(combination_guard["checked_files"], 6)
    testcase.assertEqual(combination_guard["reverted_files"], 0)
    testcase.assertEqual(sum(combination_guard["reason_code_distribution"].values()), 6)
    testcase.assertGreaterEqual(
        combination_guard["reason_code_distribution"].get("safe_combination_passed", 0),
        3,
    )
    processed_output_guard = quality["processed_output_safety_guard"]
    testcase.assertTrue(processed_output_guard["aggregate_only"])
    testcase.assertEqual(processed_output_guard["checked_files"], 6)
    testcase.assertEqual(processed_output_guard["reverted_files"], 0)
    testcase.assertEqual(
        processed_output_guard["reason_code_distribution"]["safe_processed_output_passed"],
        6,
    )
    metric_signal = quality["guardrail_metric_signal"]
    testcase.assertTrue(metric_signal["aggregate_only"])
    for metric_name in (
        "cumulative_change_score",
        "cumulative_change_pixel_ratio",
        "local_content_changed_ratio",
        "edge_content_changed_ratio",
        "processed_output_dark_pixel_lift_ratio",
    ):
        testcase.assertIn(metric_name, metric_signal["metrics"])
        testcase.assertEqual(metric_signal["metrics"][metric_name]["count"], 6, metric_name)


def _assert_algorithm_thresholds(testcase: unittest.TestCase, quality: dict[str, object]) -> None:
    testcase.assertEqual(quality["threshold_violations"], [])
    thresholds = quality["thresholds"]
    algorithm_metrics = quality["algorithm_metrics"]
    checks = {
        ("deskew", "abs_angle_degrees"): "max_deskew_degrees",
        ("trim_dark_border", "max_trim_margin_ratio"): "max_trim_margin_ratio",
        ("scanner_gutter_trim", "max_trim_margin_ratio"): "max_trim_margin_ratio",
        ("auto_crop", "crop_ratio"): "max_crop_ratio",
        ("despeckle", "pixel_ratio"): "max_despeckle_pixel_ratio",
        ("normalize_tones", "background_delta"): "max_tone_background_delta",
        ("normalize_tones", "contrast_delta"): "max_tone_contrast_delta",
        ("normalize_tones", "changed_pixel_ratio"): "max_tone_changed_pixel_ratio",
        ("normalize_paper_color_cast", "delta"): "max_paper_color_cast_delta",
        ("normalize_paper_color_cast", "brightness_delta"): "max_paper_color_cast_brightness_delta",
        ("normalize_paper_color_cast", "changed_pixel_ratio"): "max_paper_color_cast_changed_pixel_ratio",
        ("normalize_paper_color_cast", "candidate_pixel_ratio"): "max_paper_color_cast_candidate_pixel_ratio",
        ("lighten_edge_shadow", "changed_pixel_ratio"): "max_edge_shadow_changed_pixel_ratio",
        ("lighten_corner_shadows", "changed_pixel_ratio"): "max_corner_shadows_changed_pixel_ratio",
        ("lighten_corner_shadows", "candidate_pixel_ratio"): "max_corner_shadows_candidate_pixel_ratio",
        ("lighten_background_stains", "changed_pixel_ratio"): "max_background_stains_changed_pixel_ratio",
        ("lighten_background_stains", "candidate_pixel_ratio"): "max_background_stains_candidate_pixel_ratio",
        ("lighten_fold_shadows", "changed_pixel_ratio"): "max_fold_shadows_changed_pixel_ratio",
        ("lighten_fold_shadows", "candidate_pixel_ratio"): "max_fold_shadows_candidate_pixel_ratio",
        ("level_illumination_gradient", "changed_pixel_ratio"): "max_illumination_gradient_changed_pixel_ratio",
        ("level_illumination_gradient", "candidate_pixel_ratio"): "max_illumination_gradient_candidate_pixel_ratio",
        ("clean_bleed_through", "changed_pixel_ratio"): "max_bleed_through_changed_pixel_ratio",
        ("clean_bleed_through", "candidate_pixel_ratio"): "max_bleed_through_candidate_pixel_ratio",
        ("lighten_scanlines", "changed_pixel_ratio"): "max_scanlines_changed_pixel_ratio",
        ("lighten_scanlines", "candidate_pixel_ratio"): "max_scanlines_candidate_pixel_ratio",
        ("enhance_faded_text", "changed_pixel_ratio"): "max_faded_text_changed_pixel_ratio",
        ("enhance_faded_text", "candidate_pixel_ratio"): "max_faded_text_candidate_pixel_ratio",
        ("sharpen_text_edges", "changed_pixel_ratio"): "max_text_edges_changed_pixel_ratio",
        ("sharpen_text_edges", "candidate_pixel_ratio"): "max_text_edges_candidate_pixel_ratio",
    }
    for (operation, metric_name), threshold_name in checks.items():
        observed = algorithm_metrics[operation]["metrics"][metric_name]["max"]
        if observed is not None:
            testcase.assertLessEqual(observed, thresholds[threshold_name], f"{operation}.{metric_name}")


def _operation_timings_fixture() -> dict[str, dict[str, object]]:
    return {
        operation: {
            "enabled": True,
            "file_count": 3,
            "elapsed_seconds": 0.03,
            "files_per_minute": 6000.0,
            "average_seconds_per_file": 0.01,
        }
        for operation in REQUIRED_OPERATIONS
    }


def _full_chain_benchmark_fixture(
    operation_timings: dict[str, dict[str, object]], *, elapsed_seconds: float, processed_files: int
) -> dict[str, object]:
    return {
        "runs": [
            {
                "run_index": 1,
                "requested_workers": 1,
                "processing": {
                    "processed_files": processed_files,
                    "elapsed_seconds": elapsed_seconds,
                    "operation_timings": operation_timings,
                    "quality_regression": _full_chain_quality_regression_fixture(processed_files),
                    "private_path": "/private/archive/private_full_chain_page.png",
                    "source_sha256": "h" * 64,
                },
            }
        ]
    }


def _full_chain_quality_regression_fixture(processed_files: int) -> dict[str, object]:
    return {
        "aggregate_only": True,
        "status": "pass",
        "counts": {
            "processed_files": processed_files,
            "failed_files": 0,
            "guardrail_failed_files": 0,
            "cumulative_change_guard_checked_files": processed_files,
            "cumulative_change_guard_reverted_files": 0,
            "local_content_change_guard_checked_files": processed_files,
            "local_content_change_guard_reverted_files": 0,
            "combination_quality_guard_checked_files": processed_files,
            "combination_quality_guard_reverted_files": 0,
            "processed_output_safety_guard_checked_files": processed_files,
            "processed_output_safety_guard_reverted_files": 0,
            "processed_output_foreground_weakening_guard_reverted_files": 0,
        },
        "local_content_change_guard": {
            "aggregate_only": True,
            "checked_files": processed_files,
            "skipped_files": 0,
            "reverted_files": 0,
            "warning_files": 0,
            "reason_distribution": {},
        },
        "combination_quality_guard": {
            "aggregate_only": True,
            "checked_files": processed_files,
            "skipped_files": 0,
            "reverted_files": 0,
            "warning_files": 0,
            "low_confidence_original_files": 0,
            "reason_code_distribution": {"safe_combination_passed": processed_files},
            "risk_tier_distribution": {"low_risk_background": processed_files},
            "reason_distribution": {},
        },
        "processed_output_safety_guard": {
            "aggregate_only": True,
            "checked_files": processed_files,
            "skipped_files": 0,
            "reverted_files": 0,
            "warning_files": 0,
            "washout_reverted_files": 0,
            "clipping_reverted_files": 0,
            "foreground_loss_reverted_files": 0,
            "foreground_weakening_reverted_files": 0,
            "reason_code_distribution": {"safe_processed_output_passed": processed_files},
            "reason_distribution": {},
        },
        "guardrail_metric_signal": {
            "aggregate_only": True,
            "metrics": {
                "cumulative_change_score": {"count": processed_files, "max": 0.04},
                "cumulative_change_pixel_ratio": {"count": processed_files, "max": 0.01},
                "local_content_changed_ratio": {"count": processed_files, "max": 0.0},
                "edge_content_changed_ratio": {"count": processed_files, "max": 0.0},
                "processed_output_dark_pixel_lift_ratio": {"count": processed_files, "max": 0.0},
            },
        },
        "algorithm_metrics": {
            operation: {
                "enabled": True,
                "changed_files": 0,
                "file_count": processed_files,
                "elapsed_seconds": 0.01,
                "files_per_minute": 6000.0,
                "metrics": {},
            }
            for operation in REQUIRED_OPERATIONS
        },
        "threshold_violations": [],
    }


def _fixed_sample_operation_timings() -> dict[str, dict[str, object]]:
    average_seconds_by_operation = {
        "auto_crop": 0.21,
        "deskew": 0.16,
        "trim_dark_border": 0.01,
        "scanner_gutter_trim": 0.01,
        "despeckle": 0.26,
        "normalize_tones": 0.01,
        "normalize_paper_color_cast": 0.01,
        "lighten_edge_shadow": 0.01,
        "lighten_corner_shadows": 0.01,
        "lighten_background_stains": 0.36,
        "lighten_fold_shadows": 0.01,
        "level_illumination_gradient": 0.01,
        "clean_bleed_through": 0.36,
        "lighten_scanlines": 0.01,
        "enhance_faded_text": 0.01,
        "sharpen_text_edges": 0.01,
    }
    return {
        operation: {
            "enabled": True,
            "file_count": 20,
            "elapsed_seconds": round(average_seconds * 20, 6),
            "files_per_minute": round(60 / average_seconds, 6),
            "average_seconds_per_file": average_seconds,
        }
        for operation, average_seconds in average_seconds_by_operation.items()
    }


def _synthetic_performance_comparison_module() -> object:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_synthetic_performance_comparison.py"
    spec = importlib.util.spec_from_file_location("run_synthetic_performance_comparison", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _synthetic_pages(input_dir: Path) -> None:
    _text_page().save(input_dir / "private_default_page.png", dpi=(300, 300))
    _edge_shadow_page().save(input_dir / "private_edge_page.png", dpi=(300, 300))
    _stain_page().save(input_dir / "private_stain_page.png", dpi=(300, 300))
    _scanline_page().save(input_dir / "private_scanline_page.png", dpi=(300, 300))
    _faded_text_page().save(input_dir / "private_faded_text_page.png", dpi=(300, 300))
    _blurred_text_page().save(input_dir / "private_blurred_text_page.png", dpi=(300, 300))


def _shallow_stable_text_page() -> Image.Image:
    image = Image.new("RGB", (240, 320), (246, 246, 246))
    draw = ImageDraw.Draw(image)
    for y in range(50, 230, 30):
        draw.rectangle((45, y, 195, y + 3), fill=(50, 50, 50))
    return image


def _safe_full_chain_combination_page() -> Image.Image:
    image = _scanline_page().resize((240, 180))
    draw = ImageDraw.Draw(image)
    for y in range(86, 130, 14):
        draw.line((58, y, 174, y), fill=(202, 202, 202), width=2)
    draw.ellipse((165, 28, 210, 58), fill=(222, 222, 222))
    image.putpixel((24, 24), (0, 0, 0))
    return image


def _warm_mild_bleed_through_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (244, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.text((34, 36), "REAL", fill=(70, 70, 70))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((124, 82), "321", fill=255)
    mask_draw.text((124, 104), "654", fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(2.6))
    ghost = Image.new("RGB", image.size, (222, 198, 154))
    image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.60)))
    return image


def _safe_cloud_stain_combination_page() -> Image.Image:
    image = Image.new("RGB", (320, 220), (242, 242, 236))
    draw = ImageDraw.Draw(image)
    for y in (48, 74, 100):
        draw.rectangle((72, y, 220, y + 5), fill=(36, 36, 36))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((170, 125, 260, 185), fill=180)
    mask = mask.filter(ImageFilter.GaussianBlur(9))
    image = Image.composite(Image.new("RGB", image.size, (224, 218, 178)), image, mask)
    image.putpixel((28, 28), (10, 10, 10))
    return image


def _risk_table_page_number_annotation_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (242, 242, 242))
    draw = ImageDraw.Draw(image)
    for y in (42, 72, 102, 132):
        draw.line((26, y, 214, y), fill=(190, 190, 190), width=2)
    for x in (26, 88, 150, 214):
        draw.line((x, 42, x, 132), fill=(190, 190, 190), width=2)
    draw.text((180, 18), "12", fill=(60, 60, 60))
    draw.line((48, 150, 116, 158), fill=(120, 120, 120), width=2)
    return image


def _risk_stamp_header_footer_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for y in range(38, 132, 18):
        draw.rectangle((42, y, 164, y + 3), fill=(58, 58, 58))
        draw.rectangle((46, y + 8, 136, y + 10), fill=(76, 76, 76))
    draw.ellipse((166, 48, 216, 98), outline=(180, 40, 35), width=3)
    draw.rectangle((48, 18, 170, 20), fill=(64, 64, 64))
    draw.rectangle((54, 158, 154, 160), fill=(64, 64, 64))
    return image.filter(ImageFilter.GaussianBlur(radius=0.6))


def _risk_dark_photo_edge_trace_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    for y in range(20, 160):
        shade = 95 + ((y * 7) % 70)
        draw.line((24, y, 216, y), fill=(shade, shade, shade))
    for x in range(34, 210, 12):
        shade = 90 + (x % 50)
        draw.line((x, 26, min(216, x + 42), 154), fill=(shade, shade, shade), width=2)
    draw.rectangle((0, 74, 14, 102), fill=(45, 45, 45))
    draw.text((184, 12), "9", fill=(42, 42, 42))
    return image


def _text_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 12, 112, 82), outline=(50, 50, 50), width=2)
    for y in range(30, 68, 12):
        draw.line((32, y, 92, y), fill=(30, 30, 30), width=2)
    image.putpixel((24, 24), (0, 0, 0))
    return image


def _edge_shadow_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (232, 232, 232))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 14, 95), fill=(170, 170, 170))
    draw.rectangle((30, 26, 100, 70), fill=(238, 238, 238))
    for y in range(36, 60, 10):
        draw.line((42, y, 88, y), fill=(45, 45, 45), width=2)
    return image


def _stain_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (238, 238, 238))
    draw = ImageDraw.Draw(image)
    draw.ellipse((20, 18, 42, 36), fill=(214, 214, 214))
    draw.ellipse((86, 52, 110, 72), fill=(216, 216, 216))
    for y in range(34, 62, 12):
        draw.line((42, y, 78, y), fill=(45, 45, 45), width=2)
    return image


def _scanline_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    for y in (18, 45, 72):
        draw.line((6, y, 121, y), fill=(218, 218, 218), width=1)
    for y in range(34, 58, 12):
        draw.line((42, y, 86, y), fill=(50, 50, 50), width=2)
    return image


def _segmented_scanline_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (42, 64, 86):
        draw.rectangle((42, y, 158, y + 5), fill=(36, 36, 36))
    for x0, x1 in ((16, 48), (96, 128), (196, 228)):
        draw.rectangle((x0, 132, x1, 133), fill=(226, 226, 222))
    return image


def _faded_text_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (242, 242, 242))
    draw = ImageDraw.Draw(image)
    for y in range(22, 74, 13):
        draw.line((24, y, 104, y), fill=(188, 188, 188), width=2)
        draw.line((28, y + 6, 92, y + 6), fill=(192, 192, 192), width=2)
    return image


def _blurred_text_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for y in range(24, 72, 12):
        draw.line((24, y, 104, y), fill=(42, 42, 42), width=2)
        draw.line((28, y + 5, 92, y + 5), fill=(58, 58, 58), width=2)
    return image.filter(ImageFilter.GaussianBlur(radius=0.7))


def _combination_guard_metrics() -> dict[str, object]:
    return {
        "size_change_ratio": 0.0,
        "pixel_change_guardrail_scope": "same_size_pixel_change",
        "pixel_change_guardrail_applied": True,
        "pixel_change_ratio": 0.18,
        "brightness_delta": 8.0,
        "contrast_delta": 8.0,
        "crop_ratio": 0.0,
        "max_trim_margin_ratio": 0.0,
        "deskew_abs_angle_degrees": 0.0,
        "background_stains_candidate_pixel_ratio": 0.04,
        "scanlines_candidate_pixel_ratio": 0.04,
        "faded_text_enhanced": False,
        "faded_text_changed_pixel_ratio": 0.04,
        "faded_text_candidate_pixel_ratio": 0.04,
        "text_edges_sharpened": False,
        "text_edges_changed_pixel_ratio": 0.0,
        "text_edges_candidate_pixel_ratio": 0.0,
        "local_content_changed_ratio": 0.02,
        "edge_content_changed_ratio": 0.02,
    }


def _guard_passed() -> dict[str, object]:
    return {"checked": True, "action": "passed", "reverted": False, "reasons": []}


def _guard_reverted(*reasons: str) -> dict[str, object]:
    return {"checked": True, "action": "reverted_to_source", "reverted": True, "reasons": list(reasons)}
