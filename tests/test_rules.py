"""Tests for rules profile loading and validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.rules import (
    RuleSetting,
    RulesProfile,
    RulesProfileError,
    default_rules_profile,
    load_rules_profile,
)


class TestDefaultRulesProfile(unittest.TestCase):
    def test_default_name(self):
        profile = default_rules_profile()
        self.assertEqual(profile.name, "default")

    def test_default_version(self):
        profile = default_rules_profile()
        self.assertEqual(profile.version, "scan-qc.phase1.v1")

    def test_default_min_dpi(self):
        profile = default_rules_profile()
        self.assertEqual(profile.min_dpi, 200)

    def test_default_quality_thresholds(self):
        profile = default_rules_profile()
        self.assertEqual(profile.dark_mean_threshold, 45.0)
        self.assertEqual(profile.bright_mean_threshold, 250.0)
        self.assertEqual(profile.low_contrast_stddev_threshold, 10.0)
        self.assertEqual(profile.blur_laplacian_variance_threshold, 20.0)

    def test_default_name_pattern_is_none(self):
        profile = default_rules_profile()
        self.assertIsNone(profile.name_pattern)


class TestRulesProfileFrozen(unittest.TestCase):
    def test_frozen_name(self):
        profile = RulesProfile()
        with self.assertRaises(AttributeError):
            profile.name = "other"  # type: ignore[misc]

    def test_frozen_min_dpi(self):
        profile = RulesProfile()
        with self.assertRaises(AttributeError):
            profile.min_dpi = 300  # type: ignore[misc]


class TestRuleSetting(unittest.TestCase):
    def test_default_enabled(self):
        setting = RuleSetting()
        self.assertTrue(setting.enabled)

    def test_default_severity_none(self):
        setting = RuleSetting()
        self.assertIsNone(setting.severity)

    def test_custom_values(self):
        setting = RuleSetting(enabled=False, severity="P2")
        self.assertFalse(setting.enabled)
        self.assertEqual(setting.severity, "P2")


class TestIsRuleEnabled(unittest.TestCase):
    def test_unknown_rule_enabled_by_default(self):
        profile = RulesProfile()
        self.assertTrue(profile.is_rule_enabled("nonexistent_rule"))

    def test_explicitly_enabled(self):
        profile = RulesProfile(rules={"dpi_missing": RuleSetting(enabled=True)})
        self.assertTrue(profile.is_rule_enabled("dpi_missing"))

    def test_explicitly_disabled(self):
        profile = RulesProfile(rules={"dpi_missing": RuleSetting(enabled=False)})
        self.assertFalse(profile.is_rule_enabled("dpi_missing"))


class TestSeverityFor(unittest.TestCase):
    def test_default_severity_when_no_override(self):
        profile = RulesProfile()
        self.assertEqual(profile.severity_for("nonexistent", "P0"), "P0")

    def test_override_severity(self):
        profile = RulesProfile(rules={"dpi_missing": RuleSetting(severity="P2")})
        self.assertEqual(profile.severity_for("dpi_missing", "P1"), "P2")


class TestThresholdSummary(unittest.TestCase):
    def test_includes_min_dpi(self):
        profile = RulesProfile(min_dpi=300)
        summary = profile.threshold_summary()
        self.assertEqual(summary["min_dpi"], 300)

    def test_includes_quality_thresholds(self):
        profile = default_rules_profile()
        summary = profile.threshold_summary()
        self.assertIn("quality", summary)
        q = summary["quality"]
        self.assertEqual(q["dark_mean_threshold"], profile.dark_mean_threshold)


class TestMetadata(unittest.TestCase):
    def test_metadata_structure(self):
        profile = RulesProfile(name="test-profile")
        meta = profile.metadata()
        self.assertEqual(meta["name"], "test-profile")
        self.assertIn("thresholds", meta)
        self.assertIn("rules", meta)


class TestLoadRulesProfile(unittest.TestCase):
    def test_load_valid_minimal(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            path.write_text('{"name": "custom"}', encoding="utf-8")
            profile = load_rules_profile(path)
            self.assertEqual(profile.name, "custom")

    def test_load_with_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"name": "high-dpi", "min_dpi": 300, "quality_thresholds": {"dark_mean_threshold": 50.0}}
            path.write_text(json.dumps(data), encoding="utf-8")
            profile = load_rules_profile(path)
            self.assertEqual(profile.min_dpi, 300)
            self.assertEqual(profile.dark_mean_threshold, 50.0)

    def test_load_nonexistent_file(self):
        with self.assertRaises(RulesProfileError):
            load_rules_profile(Path("/nonexistent/rules.json"))

    def test_load_invalid_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            path.write_text("{invalid", encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_non_object_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_with_rules(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {
                "rules": {
                    "dpi_missing": {"enabled": False},
                    "quality_too_dark": {"enabled": True, "severity": "P2"},
                },
            }
            path.write_text(json.dumps(data), encoding="utf-8")
            profile = load_rules_profile(path)
            self.assertFalse(profile.is_rule_enabled("dpi_missing"))
            self.assertTrue(profile.is_rule_enabled("quality_too_dark"))
            self.assertEqual(profile.severity_for("quality_too_dark", "P1"), "P2")

    def test_load_invalid_min_dpi_negative(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            path.write_text('{"min_dpi": -1}', encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_invalid_min_dpi_type(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            path.write_text('{"min_dpi": "high"}', encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_invalid_name_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            path.write_text('{"name": ""}', encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_quality_thresholds_not_object(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            path.write_text('{"quality_thresholds": "fast"}', encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_rule_with_invalid_severity(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"rules": {"dpi_missing": {"severity": "P5"}}}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_rule_enabled_not_bool(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"rules": {"dpi_missing": {"enabled": "yes"}}}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(RulesProfileError):
                load_rules_profile(path)

    def test_load_name_pattern_null(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"name_pattern": None}
            path.write_text(json.dumps(data), encoding="utf-8")
            profile = load_rules_profile(path)
            self.assertIsNone(profile.name_pattern)

    def test_load_name_pattern_value(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rules.json"
            data = {"name_pattern": "A\\d{4}_\\d{4}"}
            path.write_text(json.dumps(data), encoding="utf-8")
            profile = load_rules_profile(path)
            self.assertEqual(profile.name_pattern, "A\\d{4}_\\d{4}")


if __name__ == "__main__":
    unittest.main()
