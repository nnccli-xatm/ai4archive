"""Tests for manifest CSV parsing and validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.manifest import (
    ManifestEntry,
    ManifestValidation,
    manifest_summary,
    read_manifest,
)


def _write_csv(td: str, content: str, name: str = "manifest.csv") -> Path:
    path = Path(td) / name
    path.write_text(content, encoding="utf-8")
    return path


class TestReadManifestNone(unittest.TestCase):
    def test_none_returns_empty(self):
        result = read_manifest(None)
        self.assertFalse(result.used)
        self.assertEqual(result.entry_count, 0)
        self.assertEqual(result.entries, ())


class TestReadManifestBasic(unittest.TestCase):
    def test_simple_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\nA001.jpg\nA002.jpg\n")
            result = read_manifest(path)
            self.assertTrue(result.used)
            self.assertTrue(result.readable)
            self.assertTrue(result.has_relative_path_column)
            self.assertEqual(result.entry_count, 2)
            self.assertEqual(result.unique_entry_count, 2)
            self.assertEqual(len(result.entries), 2)
            self.assertEqual(result.entries[0].relative_path, "A001.jpg")
            self.assertEqual(result.entries[1].relative_path, "A002.jpg")

    def test_missing_relative_path_column(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "filename\nA001.jpg\n")
            result = read_manifest(path)
            self.assertTrue(result.used)
            self.assertTrue(result.readable)
            self.assertFalse(result.has_relative_path_column)
            self.assertEqual(result.entry_count, 0)

    def test_unreadable_file(self):
        result = read_manifest(Path("/nonexistent/manifest.csv"))
        self.assertTrue(result.used)
        self.assertFalse(result.readable)


class TestReadManifestPaths(unittest.TestCase):
    def test_backslash_normalized(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\nsub\\A001.jpg\n")
            result = read_manifest(path)
            self.assertEqual(result.entries[0].relative_path, "sub/A001.jpg")

    def test_whitespace_path_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\nA001.jpg\n   \nA002.jpg\n")
            result = read_manifest(path)
            self.assertEqual(result.unique_entry_count, 2)
            self.assertEqual(result.empty_path_count, 1)

    def test_absolute_path_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\n/A001.jpg\nA002.jpg\n")
            result = read_manifest(path)
            self.assertEqual(result.unique_entry_count, 1)
            self.assertEqual(result.absolute_path_count, 1)

    def test_parent_escape_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\n../A001.jpg\nA002.jpg\n")
            result = read_manifest(path)
            self.assertEqual(result.unique_entry_count, 1)
            self.assertEqual(result.parent_escape_count, 1)

    def test_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\nA001.jpg\nA001.jpg\n")
            result = read_manifest(path)
            self.assertEqual(result.duplicate_count, 1)


class TestReadManifestSequence(unittest.TestCase):
    def test_sequence_field_detected(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path,sequence\nA001.jpg,1\nA002.jpg,2\n")
            result = read_manifest(path)
            self.assertEqual(result.sequence_field, "sequence")
            self.assertEqual(result.sequence_entry_count, 2)
            self.assertEqual(result.entries[0].sequence, 1)
            self.assertEqual(result.entries[1].sequence, 2)

    def test_page_sequence_field(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path,page_sequence\nA001.jpg,10\n")
            result = read_manifest(path)
            self.assertEqual(result.sequence_field, "page_sequence")
            self.assertEqual(result.entries[0].sequence, 10)

    def test_invalid_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path,sequence\nA001.jpg,abc\nA002.jpg,2\n")
            result = read_manifest(path)
            self.assertEqual(result.sequence_invalid_count, 1)
            self.assertIsNone(result.entries[0].sequence)

    def test_duplicate_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path,sequence\nA001.jpg,1\nA002.jpg,1\n")
            result = read_manifest(path)
            self.assertEqual(result.sequence_duplicate_count, 1)

    def test_strict_sequence_gap(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(
                td,
                "relative_path,sequence,strict_sequence\nA001.jpg,1,yes\nA002.jpg,3,yes\n",
            )
            result = read_manifest(path)
            self.assertTrue(result.strict_sequence)
            self.assertEqual(result.sequence_gap_count, 1)


class TestReadManifestMatching(unittest.TestCase):
    def test_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\nA001.jpg\nA002.jpg\n")
            result = read_manifest(path, input_paths={"A001.jpg"})
            self.assertEqual(result.missing_count, 1)
            self.assertEqual(result.unexpected_count, 0)

    def test_unexpected_files(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\nA001.jpg\n")
            result = read_manifest(path, input_paths={"A001.jpg", "EXTRA.jpg"})
            self.assertEqual(result.missing_count, 0)
            self.assertEqual(result.unexpected_count, 1)


class TestManifestSummary(unittest.TestCase):
    def test_summary_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_csv(td, "relative_path\nA001.jpg\n")
            result = read_manifest(path)
            summary = manifest_summary(result)
            self.assertTrue(summary["used"])
            self.assertTrue(summary["readable"])
            self.assertEqual(summary["entry_count"], 1)


class TestManifestEntryFrozen(unittest.TestCase):
    def test_frozen(self):
        entry = ManifestEntry("A.jpg", 2, 1, None, None)
        with self.assertRaises(AttributeError):
            entry.relative_path = "B.jpg"  # type: ignore[misc]


class TestManifestValidationFrozen(unittest.TestCase):
    def test_frozen(self):
        val = ManifestValidation(
            used=True, readable=True, has_relative_path_column=True,
            path=None, sequence_field=None, strict_sequence=False,
            entries=(), entry_count=0, unique_entry_count=0,
            empty_path_count=0, absolute_path_count=0, parent_escape_count=0,
            duplicate_count=0, missing_count=0, unexpected_count=0,
            sequence_entry_count=0, sequence_invalid_count=0,
            sequence_duplicate_count=0, sequence_gap_count=0,
            sequence_order_mismatch_count=0,
        )
        with self.assertRaises(AttributeError):
            val.used = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
