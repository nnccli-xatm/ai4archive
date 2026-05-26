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

        self.assertEqual(selected, {"tests.test_example.Example.test_existing"})
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

        self.assertIn("tests.test_example.Example.test_new_guard", selected)
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

    def test_scan_processing_source_mapping_preserved(self) -> None:
        with mock.patch.object(selector, "_diff_for_path", return_value=""):
            selected = selector.select_targeted_tests(
                "refs/remotes/ci/base..HEAD",
                ["src/archive_scan_qc/scan_processing.py"],
            )

        self.assertIn("tests/test_scan_qc.py", selected)
        self.assertIn("tests/test_scan_processing_combo.py", selected)
        self.assertIn("tests/test_scan_processing_reuse.py", selected)
        self.assertIn("tests/test_scan_processing_algorithm_regression.py", selected)


if __name__ == "__main__":
    unittest.main()
