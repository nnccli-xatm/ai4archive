"""Tests for DA/T 31-2017 10.2 deskew post-verification rules (AI4-804)."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.processing import ProcessingOptions, process_images
from archive_scan_qc.rule_registry import RULE_REGISTRY, RuleMetadata
from archive_scan_qc.rules import RulesProfile, default_rules_profile
from archive_scan_qc.scanner import ScanConfig, scan_batch


class TestDeskewResidualInProcessing(unittest.TestCase):
    def test_deskew_records_residual_angle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _tilted_text_page(-3.0)
            image.save(input_dir / "A001_0001.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir, workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    deskew=True,
                    trim_dark_border=False,
                    auto_crop=False,
                    despeckle=False,
                    workers=1,
                ),
            )
            record = manifest["files"][0]
            self.assertTrue(record["deskewed"])
            self.assertIsNotNone(record["deskew_residual_degrees"])

    def test_no_deskew_no_residual(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _tilted_text_page(-3.0)
            image.save(input_dir / "A001_0001.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir, workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    deskew=False,
                    trim_dark_border=False,
                    auto_crop=False,
                    despeckle=False,
                    workers=1,
                ),
            )
            record = manifest["files"][0]
            self.assertFalse(record["deskewed"])
            self.assertIsNone(record["deskew_residual_degrees"])

    def test_deskew_large_angle_not_applied(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _tilted_text_page(-7.0)
            image.save(input_dir / "A001_0001.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir, workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    deskew=True,
                    trim_dark_border=False,
                    auto_crop=False,
                    despeckle=False,
                    workers=1,
                ),
            )
            record = manifest["files"][0]
            self.assertFalse(record["deskewed"])
            self.assertIsNone(record["deskew_residual_degrees"])

    def test_deskew_small_tilt_residual_below_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _tilted_text_page(-1.0)
            image.save(input_dir / "A001_0001.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir, workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    deskew=True,
                    trim_dark_border=False,
                    auto_crop=False,
                    despeckle=False,
                    workers=1,
                ),
            )
            record = manifest["files"][0]
            if record["deskewed"]:
                self.assertLess(record["deskew_residual_degrees"], 1.0)


class TestSkewLargeAngleScannerFinding(unittest.TestCase):
    def test_large_angle_finding_logic(self):
        from archive_scan_qc.scanner import _add_processing_quality_findings

        profile = default_rules_profile()
        item = {
            "relative_path": "A001_0001.png",
            "quality_skew_angle_degrees": -7.2,
            "quality_skew_confidence": 0.15,
        }
        findings: list[dict[str, str]] = []
        _add_processing_quality_findings(item, findings, profile)
        rules = {f["rule"] for f in findings}
        self.assertIn("quality_skew_large_angle", rules)
        self.assertNotIn("quality_skew_candidate", rules)

    def test_moderate_angle_uses_candidate_not_large(self):
        from archive_scan_qc.scanner import _add_processing_quality_findings

        profile = default_rules_profile()
        item = {
            "relative_path": "A001_0001.png",
            "quality_skew_angle_degrees": -2.5,
            "quality_skew_confidence": 0.15,
        }
        findings: list[dict[str, str]] = []
        _add_processing_quality_findings(item, findings, profile)
        rules = {f["rule"] for f in findings}
        self.assertIn("quality_skew_candidate", rules)
        self.assertNotIn("quality_skew_large_angle", rules)

    def test_no_tilt_no_skew_finding(self):
        from archive_scan_qc.scanner import _add_processing_quality_findings

        profile = default_rules_profile()
        item = {
            "quality_skew_angle_degrees": None,
            "quality_skew_confidence": 0.0,
            "quality_skew_reason": "low contrast",
        }
        findings: list[dict[str, str]] = []
        _add_processing_quality_findings(item, findings, profile)
        skew_rules = {f["rule"] for f in findings if "skew" in f["rule"]}
        self.assertEqual(len(skew_rules), 0)


class TestDeskewResidualRuleRegistry(unittest.TestCase):
    def test_deskew_residual_angle_in_registry(self):
        self.assertIn("deskew_residual_angle", RULE_REGISTRY)

    def test_deskew_residual_angle_metadata(self):
        rule = RULE_REGISTRY["deskew_residual_angle"]
        self.assertEqual(rule.default_severity, "P2")
        self.assertTrue(any("10.2" in s for s in rule.standards))

    def test_quality_skew_large_angle_in_registry(self):
        self.assertIn("quality_skew_large_angle", RULE_REGISTRY)

    def test_quality_skew_large_angle_metadata(self):
        rule = RULE_REGISTRY["quality_skew_large_angle"]
        self.assertEqual(rule.default_severity, "P1")
        self.assertTrue(any("10.2" in s for s in rule.standards))


class TestDeskewResidualThreshold(unittest.TestCase):
    def test_default_threshold(self):
        profile = default_rules_profile()
        self.assertEqual(profile.deskew_residual_threshold, 0.5)

    def test_custom_threshold_from_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"quality_thresholds": {"deskew_residual_threshold": 0.3}}
            path.write_text(json.dumps(data), encoding="utf-8")
            from archive_scan_qc.rules import load_rules_profile

            profile = load_rules_profile(path)
            self.assertEqual(profile.deskew_residual_threshold, 0.3)

    def test_threshold_in_summary(self):
        profile = RulesProfile(deskew_residual_threshold=0.7)
        summary = profile.threshold_summary()
        self.assertEqual(summary["quality"]["deskew_residual_threshold"], 0.7)


def _tilted_text_page(angle_degrees: float) -> Image.Image:
    image = Image.new("RGB", (300, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 30, 260, 370), outline=(60, 60, 60), width=2)
    for y in range(60, 340, 20):
        draw.line((60, y, 240, y), fill=(20, 20, 20), width=2)
    return image.rotate(angle_degrees, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")


def _upright_text_page() -> Image.Image:
    image = Image.new("RGB", (300, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 30, 260, 370), outline=(60, 60, 60), width=2)
    for y in range(60, 340, 20):
        draw.line((60, y, 240, y), fill=(20, 20, 20), width=2)
    return image


if __name__ == "__main__":
    unittest.main()
