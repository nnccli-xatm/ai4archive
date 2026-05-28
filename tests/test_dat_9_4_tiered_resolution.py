"""Tests for DA/T 31-2017 9.4 tiered resolution rules (AI4-803)."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from archive_scan_qc.rules import (
    DPI_MINIMUM_BY_PURPOSE,
    VALID_DPI_PURPOSES,
    RulesProfile,
    RulesProfileError,
    default_rules_profile,
    load_rules_profile,
)
from archive_scan_qc.scanner import ScanConfig, scan_batch


class TestDpiPurposeConstants(unittest.TestCase):
    def test_valid_purposes(self):
        self.assertEqual(VALID_DPI_PURPOSES, {"standard", "com", "reproduction", "print"})

    def test_purpose_thresholds(self):
        self.assertEqual(DPI_MINIMUM_BY_PURPOSE["standard"], 200)
        self.assertEqual(DPI_MINIMUM_BY_PURPOSE["com"], 300)
        self.assertEqual(DPI_MINIMUM_BY_PURPOSE["reproduction"], 600)
        self.assertEqual(DPI_MINIMUM_BY_PURPOSE["print"], 300)


class TestDefaultProfile(unittest.TestCase):
    def test_default_purpose_is_standard(self):
        profile = default_rules_profile()
        self.assertEqual(profile.dpi_purpose, "standard")

    def test_default_effective_min_dpi(self):
        profile = default_rules_profile()
        self.assertEqual(profile.effective_min_dpi(), 200)


class TestEffectiveMinDpi(unittest.TestCase):
    def test_standard(self):
        profile = RulesProfile(dpi_purpose="standard")
        self.assertEqual(profile.effective_min_dpi(), 200)

    def test_com(self):
        profile = RulesProfile(dpi_purpose="com")
        self.assertEqual(profile.effective_min_dpi(), 300)

    def test_reproduction(self):
        profile = RulesProfile(dpi_purpose="reproduction")
        self.assertEqual(profile.effective_min_dpi(), 600)

    def test_print(self):
        profile = RulesProfile(dpi_purpose="print")
        self.assertEqual(profile.effective_min_dpi(), 300)

    def test_unknown_purpose_falls_back_to_min_dpi(self):
        profile = RulesProfile(dpi_purpose="unknown", min_dpi=150)
        self.assertEqual(profile.effective_min_dpi(), 150)


class TestLoadProfileWithDpiPurpose(unittest.TestCase):
    def test_load_valid_purpose(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"dpi_purpose": "com"}
            path.write_text(json.dumps(data), encoding="utf-8")
            profile = load_rules_profile(path)
            self.assertEqual(profile.dpi_purpose, "com")
            self.assertEqual(profile.effective_min_dpi(), 300)

    def test_load_invalid_purpose(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"dpi_purpose": "ultra"}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_null_purpose_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"dpi_purpose": None}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)


class TestThresholdSummaryWithDpiPurpose(unittest.TestCase):
    def test_includes_dpi_purpose(self):
        profile = RulesProfile(dpi_purpose="reproduction")
        summary = profile.threshold_summary()
        self.assertEqual(summary["dpi_purpose"], "reproduction")
        self.assertEqual(summary["effective_min_dpi"], 600)


class TestDpiMinimumCheckWithPurpose(unittest.TestCase):
    def test_standard_200dpi_passes(self):
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "reports"
            input_dir.mkdir()
            img = Image.new("RGB", (100, 100), "white")
            img.save(input_dir / "A001_0001.png", dpi=(200, 200))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            dpi_findings = [f for f in report["findings"] if f["rule"] == "dpi_minimum"]
            self.assertEqual(len(dpi_findings), 0)

    def test_standard_199dpi_fails(self):
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "reports"
            input_dir.mkdir()
            img = Image.new("RGB", (100, 100), "white")
            img.save(input_dir / "A001_0001.png", dpi=(199, 199))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            dpi_findings = [f for f in report["findings"] if f["rule"] == "dpi_minimum"]
            self.assertEqual(len(dpi_findings), 1)

    def test_com_300dpi_passes(self):
        from archive_scan_qc.scanner import ScanConfig, scan_batch
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "reports"
            input_dir.mkdir()
            img = Image.new("RGB", (100, 100), "white")
            img.save(input_dir / "A001_0001.png", dpi=(300, 300))
            profile = RulesProfile(dpi_purpose="com")
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=profile))
            dpi_findings = [f for f in report["findings"] if f["rule"] == "dpi_minimum"]
            self.assertEqual(len(dpi_findings), 0)

    def test_com_299dpi_fails(self):
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "reports"
            input_dir.mkdir()
            img = Image.new("RGB", (100, 100), "white")
            img.save(input_dir / "A001_0001.png", dpi=(299, 299))
            profile = RulesProfile(dpi_purpose="com")
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=profile))
            dpi_findings = [f for f in report["findings"] if f["rule"] == "dpi_minimum"]
            self.assertEqual(len(dpi_findings), 1)

    def test_reproduction_600dpi_passes(self):
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "reports"
            input_dir.mkdir()
            img = Image.new("RGB", (100, 100), "white")
            img.save(input_dir / "A001_0001.png", dpi=(600, 600))
            profile = RulesProfile(dpi_purpose="reproduction")
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=profile))
            dpi_findings = [f for f in report["findings"] if f["rule"] == "dpi_minimum"]
            self.assertEqual(len(dpi_findings), 0)

    def test_reproduction_599dpi_fails(self):
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "reports"
            input_dir.mkdir()
            img = Image.new("RGB", (100, 100), "white")
            img.save(input_dir / "A001_0001.png", dpi=(599, 599))
            profile = RulesProfile(dpi_purpose="reproduction")
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=profile))
            dpi_findings = [f for f in report["findings"] if f["rule"] == "dpi_minimum"]
            self.assertEqual(len(dpi_findings), 1)

    def test_backward_compatible_no_purpose(self):
        with tempfile.TemporaryDirectory() as td:
            input_dir = Path(td) / "input"
            output_dir = Path(td) / "reports"
            input_dir.mkdir()
            img = Image.new("RGB", (100, 100), "white")
            img.save(input_dir / "A001_0001.png", dpi=(200, 200))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            dpi_findings = [f for f in report["findings"] if f["rule"] == "dpi_minimum"]
            self.assertEqual(len(dpi_findings), 0)


if __name__ == "__main__":
    unittest.main()
