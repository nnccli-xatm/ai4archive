from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import ci_regression_groups as groups


class CiRegressionGroupsTests(unittest.TestCase):
    def test_regression_groups_cover_each_test_module_exactly_once(self) -> None:
        groups.verify_group_coverage()

        existing = {path.stem for path in Path("tests").glob("test_*.py")}
        assigned = {
            test
            for tests in groups.REGRESSION_GROUPS.values()
            for test in tests
        } | set(groups.DEEP_FULL_ONLY_TESTS)

        self.assertEqual(existing, assigned)

    def test_four_semantic_groups_are_present(self) -> None:
        self.assertEqual(
            set(groups.REGRESSION_GROUPS),
            {
                "core-image-processing",
                "production-cli",
                "privacy-boundary",
                "external-validation",
            },
        )

    def test_external_validation_group_keeps_heavy_external_checks_out_of_core(self) -> None:
        external = set(groups.REGRESSION_GROUPS["external-validation"])
        core = set(groups.REGRESSION_GROUPS["core-image-processing"])
        production = set(groups.REGRESSION_GROUPS["production-cli"])
        deep = set(groups.DEEP_FULL_ONLY_TESTS)

        self.assertIn("test_delivery_tooling", external)
        self.assertIn("test_ci_targeted_selector", external)
        self.assertIn("test_ocr_validation", external)
        self.assertIn("test_private_validation", external)
        self.assertIn("test_scan_qc", deep)
        self.assertIn("test_scan_processing_algorithm_regression", deep)
        self.assertIn("test_service_api", production)
        self.assertIn("test_service_http", production)
        self.assertIn("test_service_jobs", production)
        self.assertNotIn("test_scan_qc", core)
        self.assertNotIn("test_scan_processing_algorithm_regression", core)
        self.assertNotIn("test_service_api", core)
        self.assertNotIn("test_service_http", core)
        self.assertNotIn("test_service_jobs", core)
        self.assertNotIn("test_scan_processing_algorithm_regression", external)


if __name__ == "__main__":
    unittest.main()
