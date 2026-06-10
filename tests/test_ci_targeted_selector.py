from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from scripts import ci_targeted_selector as selector


class TargetedSelectorTests(unittest.TestCase):
    def test_modified_existing_method_maps_to_test_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_file = root / "tests" / "test_example.py"
            test_file.parent.mkdir(parents=True)
            test_file.write_text(
                textwrap.dedent(
                    """
                    import unittest

                    class Example(unittest.TestCase):
                        def test_existing(self):
                            value = 2
                            self.assertEqual(value, 2)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            diff = "@@ -4 +4 @@ class Example(unittest.TestCase):\n+        value = 2\n"

            with mock.patch("pathlib.Path.exists", return_value=True), mock.patch(
                "pathlib.Path.read_text", return_value=test_file.read_text(encoding="utf-8")
            ):
                selected, fallback = selector.select_test_ids_for_changed_test_file(
                    "tests/test_example.py", diff
                )

        self.assertEqual(selected, {"test_example.Example.test_existing"})
        self.assertFalse(fallback)

    def test_added_method_keeps_method_level_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_file = root / "tests" / "test_example.py"
            test_file.parent.mkdir(parents=True)
            test_file.write_text(
                textwrap.dedent(
                    """
                    import unittest

                    class Example(unittest.TestCase):
                        def test_existing(self):
                            self.assertTrue(True)

                        def test_new_guard(self):
                            self.assertTrue(True)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            diff = "@@ -6,0 +7,2 @@ class Example(unittest.TestCase):\n+    def test_new_guard(self):\n+        self.assertTrue(True)\n"

            with mock.patch("pathlib.Path.exists", return_value=True), mock.patch(
                "pathlib.Path.read_text", return_value=test_file.read_text(encoding="utf-8")
            ):
                selected, fallback = selector.select_test_ids_for_changed_test_file(
                    "tests/test_example.py", diff
                )

        self.assertIn("test_example.Example.test_new_guard", selected)
        self.assertFalse(fallback)

    def test_top_level_change_falls_back_to_full_file(self) -> None:
        source = textwrap.dedent(
            """
            import unittest

            HELPER_CONSTANT = 1

            class Example(unittest.TestCase):
                def test_existing(self):
                    self.assertTrue(True)
            """
        ).strip() + "\n"
        diff = "@@ -3 +3 @@\n+HELPER_CONSTANT = 2\n"

        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch(
            "pathlib.Path.read_text", return_value=source
        ):
            selected, fallback = selector.select_test_ids_for_changed_test_file(
                "tests/test_example.py", diff
            )

        self.assertEqual(selected, set())
        self.assertTrue(fallback)

    def test_deletion_only_hunk_maps_to_existing_method(self) -> None:
        source = textwrap.dedent(
            """
            import unittest

            class Example(unittest.TestCase):
                def test_existing(self):
                    value = 2
                    self.assertEqual(value, 2)
            """
        ).strip() + "\n"
        diff = "@@ -5 +4,0 @@ class Example(unittest.TestCase):\n-        value = 1\n"

        with mock.patch("pathlib.Path.exists", return_value=True), mock.patch(
            "pathlib.Path.read_text", return_value=source
        ):
            selected, fallback = selector.select_test_ids_for_changed_test_file(
                "tests/test_example.py", diff
            )

        self.assertEqual(selected, {"test_example.Example.test_existing"})
        self.assertFalse(fallback)

    def test_scan_processing_source_mapping_preserved(self) -> None:
        with mock.patch.object(selector, "_diff_for_path", return_value=""):
            selected = selector.select_targeted_tests(
                "refs/remotes/ci/base..HEAD",
                ["src/archive_scan_qc/processing.py"],
            )

        self.assertIn("test_backend_consistency", selected)
        self.assertIn("test_quality_suite", selected)
        self.assertIn("test_image_processing_capability_smoke", selected)
        self.assertIn("test_scan_processing_combo", selected)
        self.assertIn("test_scan_processing_reuse", selected)
        self.assertIn("test_scan_processing_workflow_regression", selected)
        self.assertNotIn("test_scan_qc", selected)
        self.assertNotIn("test_scan_processing_algorithm_regression", selected)

    def test_rules_source_mapping_uses_contract_tests(self) -> None:
        with mock.patch.object(selector, "_diff_for_path", return_value=""):
            selected = selector.select_targeted_tests(
                "refs/remotes/ci/base..HEAD",
                ["src/archive_scan_qc/rules.py"],
            )

        self.assertIn("test_rules", selected)
        self.assertIn("test_rule_registry", selected)
        self.assertIn("test_dat_9_4_tiered_resolution", selected)
        self.assertNotIn("test_scan_qc", selected)

    def test_extracted_module_mappings_avoid_scan_qc(self) -> None:
        with mock.patch.object(selector, "_diff_for_path", return_value=""):
            selected = selector.select_targeted_tests(
                "refs/remotes/ci/base..HEAD",
                [
                    "src/archive_scan_qc/__main__.py",
                    "src/archive_scan_qc/acceptance.py",
                    "src/archive_scan_qc/analysis_provider.py",
                    "src/archive_scan_qc/artifact_readiness.py",
                    "src/archive_scan_qc/calibration.py",
                    "src/archive_scan_qc/capability_probe.py",
                    "src/archive_scan_qc/deep_inspection_candidates.py",
                    "src/archive_scan_qc/deep_inspection_provider.py",
                    "src/archive_scan_qc/evidence_bundle.py",
                    "src/archive_scan_qc/final_handoff.py",
                    "src/archive_scan_qc/handoff.py",
                    "src/archive_scan_qc/image_processing_capability_smoke.py",
                    "src/archive_scan_qc/preflight.py",
                    "src/archive_scan_qc/private_validation.py",
                    "src/archive_scan_qc/production_rehearsal.py",
                    "src/archive_scan_qc/processing_review.py",
                    "src/archive_scan_qc/public_capability_contract.py",
                    "src/archive_scan_qc/run_plan.py",
                    "src/archive_scan_qc/review_decisions.py",
                    "src/archive_scan_qc/rework.py",
                    "src/archive_scan_qc/reports.py",
                    "src/archive_scan_qc/service_api.py",
                    "src/archive_scan_qc/service_http.py",
                    "src/archive_scan_qc/service_jobs.py",
                    "src/archive_scan_qc/validation_index.py",
                    "src/archive_scan_qc/workbench_summary.py",
                ],
            )

        self.assertIn("test_cli_smoke", selected)
        self.assertIn("test_acceptance_summary_regression", selected)
        self.assertIn("test_analysis_provider", selected)
        self.assertIn("test_artifact_readiness", selected)
        self.assertIn("test_rules_calibration", selected)
        self.assertIn("test_capability_probe", selected)
        self.assertIn("test_deep_inspection_candidates", selected)
        self.assertIn("test_deep_inspection_provider", selected)
        self.assertIn("test_evidence_bundle", selected)
        self.assertIn("test_final_handoff", selected)
        self.assertIn("test_handoff_manifest", selected)
        self.assertIn("test_image_processing_capability_smoke", selected)
        self.assertIn("test_preflight_run_plan", selected)
        self.assertIn("test_private_validation", selected)
        self.assertIn("test_production_rehearsal", selected)
        self.assertIn("test_processing_review", selected)
        self.assertIn("test_public_capability_contract", selected)
        self.assertIn("test_review_decisions", selected)
        self.assertIn("test_rework_actions", selected)
        self.assertIn("test_reports_contract", selected)
        self.assertIn("test_service_api", selected)
        self.assertIn("test_service_http", selected)
        self.assertIn("test_service_jobs", selected)
        self.assertIn("test_validation_index", selected)
        self.assertIn("test_workbench_summary", selected)
        self.assertNotIn("test_scan_qc", selected)

    def test_delivery_script_mappings_use_extracted_tooling_tests(self) -> None:
        with mock.patch.object(selector, "_diff_for_path", return_value=""):
            selected = selector.select_targeted_tests(
                "refs/remotes/ci/base..HEAD",
                [
                    "scripts/check_offline_dependencies.py",
                    "scripts/ci_regression_groups.py",
                    "scripts/frontend_issue_driver.py",
                    "scripts/generate_issue_plan.py",
                    "scripts/run_dibco_external_cli_test.py",
                    "scripts/run_noisyoffice_external_cli_test.py",
                ],
            )

        self.assertEqual(selected, ["test_ci_regression_groups", "test_delivery_tooling"])

    def test_release_script_mappings_use_extracted_summary_tests(self) -> None:
        with mock.patch.object(selector, "_diff_for_path", return_value=""):
            selected = selector.select_targeted_tests(
                "refs/remotes/ci/base..HEAD",
                ["scripts/release_candidate_summary.py", "scripts/release_readiness_summary.py"],
            )

        self.assertEqual(selected, ["test_release_summaries"])


if __name__ == "__main__":
    unittest.main()
