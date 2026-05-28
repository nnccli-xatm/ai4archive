"""Tests for DA/T 31-2017 10.3 crop margin enforcement (AI4-801)."""

from __future__ import annotations

import math
import unittest
from PIL import Image

from archive_scan_qc.processing import ProcessingOptions, _enforce_crop_margin


class TestEnforceCropMargin(unittest.TestCase):
    def test_crop_margin_300dpi(self):
        crop_bbox = (10, 10, 90, 90)
        image_size = (100, 100)
        scan_record = {"dpi_x": 300, "dpi_y": 300}
        result = _enforce_crop_margin(crop_bbox, image_size, 2.5, scan_record)
        left, top, right, bottom = result
        expected_margin = math.ceil(300 * 2.5 / 25.4)
        self.assertEqual(left, max(0, 10 - expected_margin))
        self.assertEqual(top, max(0, 10 - expected_margin))
        self.assertEqual(right, min(100, 90 + expected_margin))
        self.assertEqual(bottom, min(100, 90 + expected_margin))
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)

    def test_crop_margin_600dpi(self):
        crop_bbox = (20, 20, 980, 980)
        image_size = (1000, 1000)
        scan_record = {"dpi_x": 600, "dpi_y": 600}
        result = _enforce_crop_margin(crop_bbox, image_size, 2.5, scan_record)
        left, top, right, bottom = result
        expected_margin = math.ceil(600 * 2.5 / 25.4)
        self.assertEqual(left, max(0, 20 - expected_margin))
        self.assertEqual(right, min(1000, 980 + expected_margin))

    def test_crop_margin_no_dpi(self):
        crop_bbox = (10, 10, 90, 90)
        image_size = (100, 100)
        scan_record = {}
        result = _enforce_crop_margin(crop_bbox, image_size, 2.5, scan_record)
        left, top, right, bottom = result
        default_margin = math.ceil(300.0 * 2.5 / 25.4)
        self.assertEqual(left, max(0, 10 - default_margin))
        self.assertEqual(top, max(0, 10 - default_margin))

    def test_crop_margin_none_scan_record(self):
        crop_bbox = (10, 10, 90, 90)
        image_size = (100, 100)
        result = _enforce_crop_margin(crop_bbox, image_size, 2.5, None)
        left, top, right, bottom = result
        default_margin = math.ceil(300.0 * 2.5 / 25.4)
        self.assertEqual(left, max(0, 10 - default_margin))

    def test_crop_margin_zero_mm(self):
        crop_bbox = (10, 10, 90, 90)
        image_size = (100, 100)
        scan_record = {"dpi_x": 300, "dpi_y": 300}
        result = _enforce_crop_margin(crop_bbox, image_size, 0.0, scan_record)
        self.assertEqual(result, (10, 10, 90, 90))

    def test_crop_margin_clamped_to_image_bounds(self):
        crop_bbox = (2, 2, 5, 5)
        image_size = (10, 10)
        scan_record = {"dpi_x": 300, "dpi_y": 300}
        result = _enforce_crop_margin(crop_bbox, image_size, 2.5, scan_record)
        left, top, right, bottom = result
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(right, 10)
        self.assertLessEqual(bottom, 10)

    def test_crop_margin_asymmetric_dpi(self):
        crop_bbox = (10, 10, 90, 90)
        image_size = (100, 100)
        scan_record = {"dpi_x": 200, "dpi_y": 400}
        result = _enforce_crop_margin(crop_bbox, image_size, 2.5, scan_record)
        left, top, right, bottom = result
        avg_dpi = (200.0 + 400.0) / 2.0
        expected_margin = math.ceil(avg_dpi * 2.5 / 25.4)
        self.assertEqual(left, max(0, 10 - expected_margin))


class TestCropMarginRuleRegistry(unittest.TestCase):
    def test_crop_margin_insufficient_in_registry(self):
        from archive_scan_qc.rule_registry import RULE_REGISTRY
        self.assertIn("crop_margin_insufficient", RULE_REGISTRY)
        rule = RULE_REGISTRY["crop_margin_insufficient"]
        self.assertEqual(rule.rule_id, "crop_margin_insufficient")
        self.assertEqual(rule.default_severity, "P1")
        self.assertTrue(any("DA/T 31-2017 10.3" in s for s in rule.standards))

    def test_crop_margin_insufficient_metadata_complete(self):
        from archive_scan_qc.rule_registry import RULE_REGISTRY
        rule = RULE_REGISTRY["crop_margin_insufficient"]
        self.assertTrue(rule.title)
        self.assertTrue(rule.check_target)
        self.assertTrue(rule.automation_status)
        self.assertTrue(rule.report_explanation)


class TestProcessingOptionsCropMargin(unittest.TestCase):
    def test_default_crop_margin(self):
        opts = ProcessingOptions()
        self.assertEqual(opts.crop_margin_mm, 2.5)

    def test_custom_crop_margin(self):
        opts = ProcessingOptions(crop_margin_mm=3.0)
        self.assertEqual(opts.crop_margin_mm, 3.0)

    def test_crop_margin_frozen(self):
        opts = ProcessingOptions(crop_margin_mm=2.0)
        with self.assertRaises(AttributeError):
            opts.crop_margin_mm = 3.0  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
