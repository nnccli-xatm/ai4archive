"""Tests for DA/T 31-2017 12.3 manual sampling loop (AI4-806)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.sampling import (
    ALLOWED_REVIEW_DECISIONS,
    generate_sampling_checklist,
    record_sampling_review,
    build_acceptance_sampling_export,
    _compute_effective_sample_ratio,
)


class TestRiskEscalation(unittest.TestCase):
    def test_low_risk_uses_base_ratio(self):
        report = _make_report(total_files=100, p0_findings=0, p1_findings=2)
        ratio = _compute_effective_sample_ratio(report, 0.05)
        self.assertEqual(ratio, 0.05)

    def test_high_p0_escalates(self):
        report = _make_report(total_files=100, p0_findings=8, p1_findings=0)
        ratio = _compute_effective_sample_ratio(report, 0.05)
        self.assertGreaterEqual(ratio, 0.10)

    def test_high_p1_escalates(self):
        report = _make_report(total_files=100, p0_findings=0, p1_findings=20)
        ratio = _compute_effective_sample_ratio(report, 0.05)
        self.assertGreaterEqual(ratio, 0.20)

    def test_empty_report_uses_base(self):
        report = {"files": [], "findings": []}
        ratio = _compute_effective_sample_ratio(report, 0.05)
        self.assertEqual(ratio, 0.05)


class TestGenerateSamplingChecklist(unittest.TestCase):
    def test_generates_checklist_csv(self):
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "report.json"
            output_dir = Path(td) / "sampling"
            report_path.write_text(json.dumps(_make_report(total_files=20, p0_findings=1, p1_findings=2)), encoding="utf-8")
            checklist_path, payload = generate_sampling_checklist(report_path, output_dir)
            self.assertTrue(checklist_path.exists())
            self.assertGreater(len(payload["samples"]), 0)
            self.assertIn("selection", payload)
            csv_text = checklist_path.read_text(encoding="utf-8")
            self.assertIn("sample_id", csv_text)

    def test_checklist_has_risk_tiers(self):
        with tempfile.TemporaryDirectory() as td:
            report_path = Path(td) / "report.json"
            output_dir = Path(td) / "sampling"
            report_path.write_text(json.dumps(_make_report(total_files=30, p0_findings=2, p1_findings=3)), encoding="utf-8")
            _, payload = generate_sampling_checklist(report_path, output_dir)
            tiers = {s["risk_tier"] for s in payload["samples"]}
            self.assertTrue(tiers & {"p0", "p1", "p2", "baseline", "problematic"})


class TestRecordSamplingReview(unittest.TestCase):
    def test_record_review_updates_status(self):
        with tempfile.TemporaryDirectory() as td:
            sampling_path = Path(td) / "sampling_review.json"
            report = _make_report(total_files=10, p0_findings=0, p1_findings=1)
            payload = build_acceptance_sampling_export(report, sample_ratio=0.5)
            sampling_path.write_text(json.dumps(payload), encoding="utf-8")

            samples = payload["samples"]
            if samples:
                reviews = [{"sample_id": samples[0]["sample_id"], "review_status": "false_positive", "reviewer_notes": "confirmed ok"}]
                result = record_sampling_review(sampling_path, reviews)
                self.assertEqual(result["updated"], 1)
                updated = json.loads(sampling_path.read_text(encoding="utf-8"))
                self.assertEqual(updated["samples"][0]["review_status"], "false_positive")

    def test_invalid_decision_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            sampling_path = Path(td) / "sampling_review.json"
            report = _make_report(total_files=10, p0_findings=0, p1_findings=1)
            payload = build_acceptance_sampling_export(report, sample_ratio=0.5)
            sampling_path.write_text(json.dumps(payload), encoding="utf-8")

            samples = payload["samples"]
            if samples:
                reviews = [{"sample_id": samples[0]["sample_id"], "review_status": "invalid_status"}]
                result = record_sampling_review(sampling_path, reviews)
                self.assertEqual(result["updated"], 0)

    def test_nonexistent_file_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                record_sampling_review(Path(td) / "missing.json", [])

    def test_allowed_decisions_complete(self):
        expected = {"pending", "accepted_issue", "false_positive", "fixed_externally", "needs_rescan", "blocked"}
        self.assertEqual(set(ALLOWED_REVIEW_DECISIONS), expected)


class TestStratifiedSampling(unittest.TestCase):
    def test_p0_pages_sampled_first(self):
        report = _make_report(total_files=100, p0_findings=3, p1_findings=5)
        payload = build_acceptance_sampling_export(report, sample_ratio=0.05)
        samples = payload["samples"]
        p0_tiers = [s for s in samples if s["risk_tier"] == "p0"]
        self.assertGreater(len(p0_tiers), 0)

    def test_all_p0_included_when_sampled(self):
        report = _make_report(total_files=30, p0_findings=2, p1_findings=1)
        payload = build_acceptance_sampling_export(report, sample_ratio=0.10)
        samples = payload["samples"]
        p0_tiers = [s for s in samples if s["risk_tier"] == "p0"]
        self.assertEqual(len(p0_tiers), 2)


def _make_report(*, total_files: int = 10, p0_findings: int = 0, p1_findings: int = 0) -> dict:
    files = []
    findings = []
    for i in range(total_files):
        path = f"A001_{i+1:04d}.png"
        files.append({"relative_path": path, "filename": path, "sha256": f"hash{i}", "openable": True})
    for i in range(min(p0_findings, total_files)):
        findings.append({"relative_path": f"A001_{i+1:04d}.png", "severity": "P0", "rule": "test_p0"})
    for i in range(p0_findings, min(p0_findings + p1_findings, total_files)):
        findings.append({"relative_path": f"A001_{i+1:04d}.png", "severity": "P1", "rule": "test_p1"})
    return {"files": files, "findings": findings}


if __name__ == "__main__":
    unittest.main()
