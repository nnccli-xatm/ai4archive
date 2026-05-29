"""Tests for rule registry metadata and provider rule validation."""

from __future__ import annotations

import unittest

from archive_scan_qc.rule_registry import (
    PROVIDER_RULE_PATTERN,
    PROVIDER_RULE_POLICY,
    RULE_REGISTRY,
    RuleMetadata,
    rule_catalog,
    validate_provider_rule_id,
)


class TestRuleRegistryCompleteness(unittest.TestCase):
    def test_registry_is_dict(self):
        self.assertIsInstance(RULE_REGISTRY, dict)

    def test_all_keys_match_rule_id(self):
        for key, metadata in RULE_REGISTRY.items():
            self.assertEqual(key, metadata.rule_id, f"Key {key!r} does not match rule_id {metadata.rule_id!r}")

    def test_all_rule_ids_unique(self):
        ids = [m.rule_id for m in RULE_REGISTRY.values()]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate rule_id values found")

    def test_expected_builtin_rules_present(self):
        expected = [
            "openability",
            "unsupported_format",
            "dpi_missing",
            "dpi_minimum",
            "dimensions",
            "duplicate_name",
            "duplicate_file",
            "manifest_missing_file",
            "manifest_unexpected_file",
            "manifest_duplicate_entry",
            "name_pattern",
            "quality_too_dark",
            "quality_too_bright",
            "quality_low_contrast",
            "quality_suspected_blur",
            "quality_near_blank_page",
            "quality_skew_candidate",
            "quality_dark_border_candidate",
            "quality_scanline_candidate",
            "batch_format_consistency",
            "crop_margin_insufficient",
        ]
        for rule_id in expected:
            self.assertIn(rule_id, RULE_REGISTRY, f"Missing expected rule: {rule_id}")


class TestRuleMetadataFields(unittest.TestCase):
    def test_all_metadata_have_title(self):
        for rule_id, meta in RULE_REGISTRY.items():
            self.assertTrue(meta.title, f"Rule {rule_id} has empty title")

    def test_all_metadata_have_valid_severity(self):
        valid = {"P0", "P1", "P2"}
        for rule_id, meta in RULE_REGISTRY.items():
            self.assertIn(meta.default_severity, valid, f"Rule {rule_id} has invalid severity: {meta.default_severity!r}")

    def test_all_metadata_have_standards(self):
        for rule_id, meta in RULE_REGISTRY.items():
            self.assertTrue(meta.standards, f"Rule {rule_id} has empty standards")

    def test_all_metadata_have_check_target(self):
        for rule_id, meta in RULE_REGISTRY.items():
            self.assertTrue(meta.check_target, f"Rule {rule_id} has empty check_target")

    def test_all_metadata_have_automation_status(self):
        valid = {"automated", "automated screening"}
        for rule_id, meta in RULE_REGISTRY.items():
            self.assertIn(meta.automation_status, valid, f"Rule {rule_id} has unexpected automation_status: {meta.automation_status!r}")

    def test_all_metadata_have_report_explanation(self):
        for rule_id, meta in RULE_REGISTRY.items():
            self.assertTrue(meta.report_explanation, f"Rule {rule_id} has empty report_explanation")


class TestRuleMetadataToReportDict(unittest.TestCase):
    def test_to_report_dict_structure(self):
        rule = RULE_REGISTRY["openability"]
        d = rule.to_report_dict()
        self.assertEqual(d["rule_id"], "openability")
        self.assertEqual(d["title"], rule.title)
        self.assertEqual(d["default_severity"], rule.default_severity)
        self.assertIsInstance(d["standards"], list)
        self.assertNotIsInstance(d["standards"], tuple)

    def test_to_report_dict_converts_standards_to_list(self):
        rule = RULE_REGISTRY["openability"]
        d = rule.to_report_dict()
        self.assertIsInstance(d["standards"], list)
        self.assertEqual(len(d["standards"]), len(rule.standards))


class TestRuleCatalog(unittest.TestCase):
    def test_catalog_returns_dict(self):
        catalog = rule_catalog()
        self.assertIsInstance(catalog, dict)

    def test_catalog_contains_all_rules(self):
        catalog = rule_catalog()
        self.assertEqual(set(catalog.keys()), set(RULE_REGISTRY.keys()))

    def test_catalog_sorted_keys(self):
        catalog = rule_catalog()
        keys = list(catalog.keys())
        self.assertEqual(keys, sorted(keys))

    def test_catalog_entries_are_dicts(self):
        catalog = rule_catalog()
        for rule_id, entry in catalog.items():
            self.assertIsInstance(entry, dict)
            self.assertIn("rule_id", entry)
            self.assertEqual(entry["rule_id"], rule_id)


class TestValidateProviderRuleId(unittest.TestCase):
    def test_valid_provider_rule(self):
        try:
            validate_provider_rule_id("provider.opencv.blur_check")
        except ValueError:
            self.fail("Valid provider rule id raised ValueError")

    def test_valid_provider_rule_with_numbers(self):
        try:
            validate_provider_rule_id("provider.team1.rule_v2")
        except ValueError:
            self.fail("Valid provider rule id raised ValueError")

    def test_reject_builtin_rule_override(self):
        with self.assertRaises(ValueError) as ctx:
            validate_provider_rule_id("openability")
        self.assertIn("cannot override", str(ctx.exception))

    def test_reject_no_namespace(self):
        with self.assertRaises(ValueError):
            validate_provider_rule_id("some_rule")

    def test_reject_uppercase(self):
        with self.assertRaises(ValueError):
            validate_provider_rule_id("provider.Team.rule")

    def test_reject_spaces(self):
        with self.assertRaises(ValueError):
            validate_provider_rule_id("provider.team.my rule")


class TestProviderRulePolicy(unittest.TestCase):
    def test_has_protected_p0_rules(self):
        self.assertIn("protected_builtin_p0_rules", PROVIDER_RULE_POLICY)
        protected = PROVIDER_RULE_POLICY["protected_builtin_p0_rules"]
        self.assertIsInstance(protected, list)
        self.assertIn("openability", protected)
        self.assertIn("dpi_minimum", protected)
        self.assertIn("dimensions", protected)

    def test_has_constraints(self):
        self.assertIn("constraints", PROVIDER_RULE_POLICY)
        self.assertIsInstance(PROVIDER_RULE_POLICY["constraints"], list)
        self.assertGreaterEqual(len(PROVIDER_RULE_POLICY["constraints"]), 1)

    def test_pattern_regex(self):
        self.assertTrue(PROVIDER_RULE_PATTERN.fullmatch("provider.a.b"))
        self.assertTrue(PROVIDER_RULE_PATTERN.fullmatch("provider.my_tool.rule_v2"))
        self.assertIsNone(PROVIDER_RULE_PATTERN.fullmatch("provider."))
        self.assertIsNone(PROVIDER_RULE_PATTERN.fullmatch("a.b.c"))


class TestRuleMetadataFrozen(unittest.TestCase):
    def test_frozen_rule_metadata(self):
        rule = RULE_REGISTRY["openability"]
        with self.assertRaises(AttributeError):
            rule.rule_id = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
