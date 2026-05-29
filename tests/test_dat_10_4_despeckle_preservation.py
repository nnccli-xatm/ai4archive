"""Tests for DA/T 31-2017 10.4 despeckle preservation audit (AI4-802)."""

from __future__ import annotations

import math
import unittest
from PIL import Image

from archive_scan_qc.processing import (
    ContentTypeClassification,
    DespecklePreservationResult,
    ProcessingOptions,
    _classify_page_content_type,
)


class TestContentTypeClassification(unittest.TestCase):
    def test_uniform_white_page_is_text(self):
        image = Image.new("RGB", (200, 200), "white")
        result = _classify_page_content_type(image)
        self.assertEqual(result.content_type, "text")

    def test_uniform_black_page_is_text(self):
        image = Image.new("RGB", (200, 200), "black")
        result = _classify_page_content_type(image)
        self.assertEqual(result.content_type, "text")

    def test_noisy_page_is_photo(self):
        image = Image.new("RGB", (200, 200), "white")
        pixels = image.load()
        for y in range(200):
            for x in range(200):
                v = ((x * 37 + y * 73) % 256)
                pixels[x, y] = (v, v, v)
        result = _classify_page_content_type(image)
        self.assertIn(result.content_type, ("photo", "mixed"))
        self.assertGreater(result.high_variance_tiles, 0)

    def test_half_text_half_photo_is_mixed(self):
        image = Image.new("RGB", (200, 200), "white")
        pixels = image.load()
        for y in range(100, 200):
            for x in range(200):
                v = ((x * 37 + y * 73) % 256)
                pixels[x, y] = (v, v, v)
        result = _classify_page_content_type(image)
        self.assertIn(result.content_type, ("mixed", "photo"))

    def test_grid_dimensions(self):
        image = Image.new("RGB", (200, 200), "white")
        result = _classify_page_content_type(image, grid_size=4)
        self.assertEqual(result.grid_rows, 4)
        self.assertEqual(result.grid_cols, 4)

    def test_custom_threshold(self):
        image = Image.new("RGB", (200, 200), "white")
        pixels = image.load()
        for y in range(200):
            for x in range(200):
                v = 128 + (((x * 37 + y * 73) % 50) - 25)
                pixels[x, y] = (v, v, v)
        result_low = _classify_page_content_type(image, photo_stddev_threshold=5.0)
        result_high = _classify_page_content_type(image, photo_stddev_threshold=100.0)
        self.assertNotEqual(result_low.content_type, result_high.content_type)

    def test_very_small_image(self):
        image = Image.new("RGB", (4, 4), "white")
        result = _classify_page_content_type(image)
        self.assertIn(result.content_type, ("text", "photo", "mixed"))

    def test_result_is_frozen(self):
        result = ContentTypeClassification("text", 8, 8, 0, 64, "test")
        with self.assertRaises(AttributeError):
            result.content_type = "photo"  # type: ignore[misc]


class TestDespecklePreservationResult(unittest.TestCase):
    def test_frozen(self):
        result = DespecklePreservationResult("text", False, False, 0.0, "ok")
        with self.assertRaises(AttributeError):
            result.content_type = "photo"  # type: ignore[misc]


class TestDespecklePreservationRuleRegistry(unittest.TestCase):
    def test_despeckle_preservation_audit_in_registry(self):
        from archive_scan_qc.rule_registry import RULE_REGISTRY
        self.assertIn("despeckle_preservation_audit", RULE_REGISTRY)
        rule = RULE_REGISTRY["despeckle_preservation_audit"]
        self.assertEqual(rule.rule_id, "despeckle_preservation_audit")
        self.assertEqual(rule.default_severity, "P2")
        self.assertTrue(any("DA/T 31-2017 10.4" in s for s in rule.standards))

    def test_despeckle_preservation_audit_metadata(self):
        from archive_scan_qc.rule_registry import RULE_REGISTRY
        rule = RULE_REGISTRY["despeckle_preservation_audit"]
        self.assertTrue(rule.title)
        self.assertTrue(rule.check_target)
        self.assertTrue(rule.automation_status)
        self.assertTrue(rule.report_explanation)


class TestRulesProfileDespeckleConfig(unittest.TestCase):
    def test_default_despeckle_max_pixel_change_ratio(self):
        from archive_scan_qc.rules import RulesProfile
        profile = RulesProfile()
        self.assertEqual(profile.despeckle_max_pixel_change_ratio, 0.01)

    def test_custom_despeckle_max_pixel_change_ratio(self):
        from archive_scan_qc.rules import RulesProfile
        profile = RulesProfile(despeckle_max_pixel_change_ratio=0.005)
        self.assertEqual(profile.despeckle_max_pixel_change_ratio, 0.005)

    def test_despeckle_threshold_in_summary(self):
        from archive_scan_qc.rules import RulesProfile
        profile = RulesProfile()
        summary = profile.threshold_summary()
        self.assertIn("despeckle_max_pixel_change_ratio", summary["quality"])

    def test_despeckle_threshold_loadable_from_json(self):
        import json
        import tempfile
        from pathlib import Path
        from archive_scan_qc.rules import load_rules_profile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"quality_thresholds": {"despeckle_max_pixel_change_ratio": 0.02}}
            path.write_text(json.dumps(data), encoding="utf-8")
            profile = load_rules_profile(path)
            self.assertEqual(profile.despeckle_max_pixel_change_ratio, 0.02)


class TestDespecklePreservationInProcessing(unittest.TestCase):
    def test_photo_page_skips_despeckle_via_process_images(self):
        import json
        import tempfile
        from pathlib import Path
        from archive_scan_qc.processing import process_images
        from archive_scan_qc.scanner import ScanConfig, scan_batch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            noisy = Image.new("RGB", (200, 200), "white")
            pixels = noisy.load()
            for y in range(200):
                for x in range(200):
                    v = ((x * 37 + y * 73) % 256)
                    pixels[x, y] = (v, v, v)
            noisy.save(input_dir / "A001_0001.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(despeckle=True, crop_margin_mm=0.0, workers=1),
            )
            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed", f"unexpected status: {record.get('error')}")
            self.assertEqual(record["despeckle_content_type"], "photo")
            self.assertTrue(record["despeckle_preservation_skipped"])
            self.assertFalse(record["despeckle_preservation_rolled_back"])

    def test_text_page_runs_despeckle_via_process_images(self):
        import json
        import tempfile
        from pathlib import Path
        from archive_scan_qc.processing import process_images
        from archive_scan_qc.scanner import ScanConfig, scan_batch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            text_page = Image.new("RGB", (200, 200), "white")
            pixels = text_page.load()
            for x in range(90, 110):
                for y in range(90, 110):
                    pixels[x, y] = (0, 0, 0)
            text_page.save(input_dir / "A001_0001.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(despeckle=True, crop_margin_mm=0.0, workers=1),
            )
            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed", f"unexpected status: {record.get('error')}")
            self.assertEqual(record["despeckle_content_type"], "text")
            self.assertFalse(record["despeckle_preservation_skipped"])

    def test_despeckle_disabled_no_preservation_fields(self):
        import json
        import tempfile
        from pathlib import Path
        from archive_scan_qc.processing import process_images
        from archive_scan_qc.scanner import ScanConfig, scan_batch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (100, 100), "white")
            image.save(input_dir / "A001_0001.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(despeckle=False, crop_margin_mm=0.0, workers=1),
            )
            record = manifest["files"][0]
            self.assertIsNone(record["despeckle_preservation_skipped"])
            self.assertIsNone(record["despeckle_preservation_rolled_back"])
            self.assertIsNone(record["despeckle_preservation_reason"])


if __name__ == "__main__":
    unittest.main()
