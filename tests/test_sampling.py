"""Tests for acceptance sampling export."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.sampling import (
    DEFAULT_SAMPLE_RATIO,
    MIN_SAMPLE_RATIO,
    SAMPLING_CSV,
    SAMPLING_JSON,
    SCHEMA_VERSION,
    build_acceptance_sampling_export,
    write_acceptance_sampling_export,
)


def _make_report(files=None, findings=None):
    return {
        "files": files or [],
        "findings": findings or [],
    }


def _make_file(path, **kwargs):
    base = {"relative_path": path, "filename": path, "sha256": f"hash_{path}"}
    base.update(kwargs)
    return base


def _make_finding(path, rule="test_rule", severity="P1"):
    return {"relative_path": path, "rule": rule, "severity": severity}


class TestBuildSamplingExportBasic(unittest.TestCase):
    def test_empty_report(self):
        report = _make_report()
        result = build_acceptance_sampling_export(report)
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["samples"], [])
        self.assertEqual(result["selection"]["total_records"], 0)
        self.assertEqual(result["selection"]["sampled_records"], 0)

    def test_schema_version(self):
        report = _make_report()
        result = build_acceptance_sampling_export(report)
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)

    def test_sensitivity(self):
        report = _make_report()
        result = build_acceptance_sampling_export(report)
        self.assertTrue(result["privacy"]["sensitive_local_evidence"])
        self.assertFalse(result["privacy"]["aggregate_only"])


class TestBuildSamplingRatio(unittest.TestCase):
    def test_sample_ratio_too_low(self):
        report = _make_report([_make_file("a.jpg")])
        with self.assertRaises(ValueError):
            build_acceptance_sampling_export(report, sample_ratio=0.01)

    def test_default_sample_ratio(self):
        self.assertEqual(DEFAULT_SAMPLE_RATIO, 0.05)
        self.assertEqual(MIN_SAMPLE_RATIO, 0.05)

    def test_high_sample_ratio(self):
        report = _make_report([_make_file(f"A{i:03d}.jpg") for i in range(20)])
        result = build_acceptance_sampling_export(report, sample_ratio=0.5)
        self.assertEqual(result["selection"]["sampled_records"], 10)


class TestBuildSamplingRiskTiers(unittest.TestCase):
    def test_p0_files_selected_first(self):
        files = [_make_file(f"B{i:03d}.jpg") for i in range(20)]
        findings = [_make_finding("B000.jpg", severity="P0")]
        report = _make_report(files, findings)
        result = build_acceptance_sampling_export(report, sample_ratio=0.05)
        paths = [s["relative_path"] for s in result["samples"]]
        self.assertIn("B000.jpg", paths)

    def test_p1_finding_risk_tier(self):
        files = [_make_file("A.jpg")]
        findings = [_make_finding("A.jpg", severity="P1")]
        report = _make_report(files, findings)
        result = build_acceptance_sampling_export(report)
        self.assertEqual(result["samples"][0]["risk_tier"], "p1")

    def test_baseline_risk_tier(self):
        files = [_make_file("clean.jpg", openable=True)]
        report = _make_report(files)
        result = build_acceptance_sampling_export(report)
        self.assertEqual(result["samples"][0]["risk_tier"], "baseline")

    def test_problematic_risk_tier(self):
        files = [_make_file("bad.jpg", openable=False)]
        report = _make_report(files)
        result = build_acceptance_sampling_export(report)
        self.assertEqual(result["samples"][0]["risk_tier"], "problematic")


class TestBuildSamplingDeterministic(unittest.TestCase):
    def test_deterministic_selection(self):
        files = [_make_file(f"F{i:03d}.jpg") for i in range(100)]
        report = _make_report(files)
        result1 = build_acceptance_sampling_export(report)
        result2 = build_acceptance_sampling_export(report)
        paths1 = [s["relative_path"] for s in result1["samples"]]
        paths2 = [s["relative_path"] for s in result2["samples"]]
        self.assertEqual(paths1, paths2)


class TestBuildSamplingCounts(unittest.TestCase):
    def test_aggregate_counts_structure(self):
        files = [_make_file(f"A{i:03d}.jpg") for i in range(20)]
        report = _make_report(files)
        result = build_acceptance_sampling_export(report)
        counts = result["aggregate_sampling_counts"]
        self.assertEqual(counts["input_total"], 20)
        self.assertEqual(counts["target_sample_ratio"], 0.05)
        self.assertIn("total_by_risk_tier", counts)

    def test_sampling_target_met(self):
        files = [_make_file(f"A{i:03d}.jpg") for i in range(20)]
        report = _make_report(files)
        result = build_acceptance_sampling_export(report)
        counts = result["aggregate_sampling_counts"]
        self.assertTrue(counts["sample_task_target_met"])


class TestWriteSamplingExport(unittest.TestCase):
    def test_writes_json_and_csv(self):
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "report.json"
            output_dir = Path(td) / "sampling"
            report = _make_report([_make_file("A.jpg")])
            report_path.write_text(json.dumps(report), encoding="utf-8")
            json_path, csv_path, payload = write_acceptance_sampling_export(report_path, output_dir)
            self.assertTrue(json_path.exists())
            self.assertTrue(csv_path.exists())
            self.assertEqual(json_path.name, SAMPLING_JSON)
            self.assertEqual(csv_path.name, SAMPLING_CSV)

    def test_csv_has_header(self):
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "report.json"
            output_dir = Path(td) / "sampling"
            report = _make_report([_make_file("A.jpg")])
            report_path.write_text(json.dumps(report), encoding="utf-8")
            _, csv_path, _ = write_acceptance_sampling_export(report_path, output_dir)
            lines = csv_path.read_text(encoding="utf-8").strip().split("\n")
            self.assertGreaterEqual(len(lines), 2)
            self.assertIn("sample_id", lines[0])


class TestSamplingInvalidInput(unittest.TestCase):
    def test_files_not_list(self):
        report = {"files": "not_a_list", "findings": []}
        with self.assertRaises(ValueError):
            build_acceptance_sampling_export(report)

    def test_findings_not_list(self):
        report = {"files": [], "findings": "not_a_list"}
        with self.assertRaises(ValueError):
            build_acceptance_sampling_export(report)


if __name__ == "__main__":
    unittest.main()
