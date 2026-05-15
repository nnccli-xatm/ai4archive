from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from archive_scan_qc.benchmark import _processing_quality_regression, run_benchmark


class ScanProcessingAlgorithmRegressionTest(unittest.TestCase):
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
                self.assertTrue(quality["aggregate_only"])
                self.assertTrue(quality["privacy"]["aggregate_only"])
                for operation in REQUIRED_OPERATIONS:
                    self.assertIn(operation, quality["algorithm_metrics"])
                    self.assertIn("metrics", quality["algorithm_metrics"][operation])

            self.assertEqual(default_quality["status"], "pass")
            self.assertEqual(default_quality["counts"]["enhancement_changed_files"], 0)
            self.assertEqual(default_quality["counts"]["cumulative_change_guard_checked_files"], 6)
            for operation in CONSERVATIVE_REPAIR_OPERATIONS:
                self.assertFalse(default_quality["algorithm_metrics"][operation]["enabled"], operation)
                self.assertEqual(default_quality["algorithm_metrics"][operation]["changed_files"], 0, operation)

            _assert_algorithm_thresholds(self, base_quality)
            _assert_algorithm_thresholds(self, full_quality)
            _assert_required_metrics_present(self, full_quality)
            _assert_operation_timing_signal(self, full_quality)
            _assert_local_content_guard_signal(self, full_quality)

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
        local_guard = quality["local_content_change_guard"]
        self.assertTrue(local_guard["aggregate_only"])
        self.assertEqual(local_guard["checked_files"], 2)
        self.assertEqual(local_guard["skipped_files"], 1)
        self.assertEqual(local_guard["reverted_files"], 1)
        self.assertEqual(local_guard["reason_distribution"]["local_content_changed_ratio"], 1)
        self.assertEqual(local_guard["reason_distribution"]["edge_content_changed_ratio"], 1)
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
                    "performance": {"operation_timings": operation_timings},
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
        self.assertEqual(timing_budget["blocker_code"], "processing_operation_timing_budget_exceeded")
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


BASE_FLAGS = ("--deskew", "--trim-dark-border", "--auto-crop", "--despeckle")
CONSERVATIVE_REPAIR_FLAGS = (
    "--normalize-tones",
    "--lighten-edge-shadow",
    "--lighten-background-stains",
    "--lighten-scanlines",
    "--enhance-faded-text",
    "--sharpen-text-edges",
)
REQUIRED_OPERATIONS = (
    "deskew",
    "trim_dark_border",
    "auto_crop",
    "despeckle",
    "normalize_tones",
    "lighten_edge_shadow",
    "lighten_background_stains",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
)
CONSERVATIVE_REPAIR_OPERATIONS = (
    "normalize_tones",
    "lighten_edge_shadow",
    "lighten_background_stains",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
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
            despeckle="--despeckle" in flag_set,
            normalize_tones="--normalize-tones" in flag_set,
            lighten_edge_shadow="--lighten-edge-shadow" in flag_set,
            lighten_background_stains="--lighten-background-stains" in flag_set,
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
        "lighten_edge_shadow": ("delta", "changed_pixel_ratio"),
        "lighten_background_stains": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "lighten_scanlines": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "enhance_faded_text": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
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


def _assert_algorithm_thresholds(testcase: unittest.TestCase, quality: dict[str, object]) -> None:
    testcase.assertEqual(quality["threshold_violations"], [])
    thresholds = quality["thresholds"]
    algorithm_metrics = quality["algorithm_metrics"]
    checks = {
        ("deskew", "abs_angle_degrees"): "max_deskew_degrees",
        ("trim_dark_border", "max_trim_margin_ratio"): "max_trim_margin_ratio",
        ("auto_crop", "crop_ratio"): "max_crop_ratio",
        ("despeckle", "pixel_ratio"): "max_despeckle_pixel_ratio",
        ("normalize_tones", "background_delta"): "max_tone_background_delta",
        ("normalize_tones", "contrast_delta"): "max_tone_contrast_delta",
        ("normalize_tones", "changed_pixel_ratio"): "max_tone_changed_pixel_ratio",
        ("lighten_edge_shadow", "changed_pixel_ratio"): "max_edge_shadow_changed_pixel_ratio",
        ("lighten_background_stains", "changed_pixel_ratio"): "max_background_stains_changed_pixel_ratio",
        ("lighten_background_stains", "candidate_pixel_ratio"): "max_background_stains_candidate_pixel_ratio",
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


def _synthetic_pages(input_dir: Path) -> None:
    _text_page().save(input_dir / "private_default_page.png", dpi=(300, 300))
    _edge_shadow_page().save(input_dir / "private_edge_page.png", dpi=(300, 300))
    _stain_page().save(input_dir / "private_stain_page.png", dpi=(300, 300))
    _scanline_page().save(input_dir / "private_scanline_page.png", dpi=(300, 300))
    _faded_text_page().save(input_dir / "private_faded_text_page.png", dpi=(300, 300))
    _blurred_text_page().save(input_dir / "private_blurred_text_page.png", dpi=(300, 300))


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
