"""Tests for DA/T 31-2017 12.3 acceptance verdict logic (AI4-805)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.acceptance import (
    batch_acceptance_verdict,
    build_acceptance_summary,
    write_acceptance_summary,
)


class TestVerdictPass(unittest.TestCase):
    def test_all_clear_passes(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0),
            review_summary=_review(remaining_p0=0, remaining_p1=0),
        )
        self.assertEqual(summary["verdict"], "pass")
        self.assertTrue(summary["pass"])

    def test_verdict_method_returns_pass(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0),
            review_summary=_review(remaining_p0=0, remaining_p1=0),
        )
        result = batch_acceptance_verdict(summary)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["p0_open_count"], 0)
        self.assertEqual(result["p1_open_count"], 0)
        self.assertEqual(result["blocked_reasons"], [])


class TestVerdictFail(unittest.TestCase):
    def test_p0_open_fails(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0),
            review_summary=_review(remaining_p0=1, remaining_p1=0),
        )
        self.assertEqual(summary["verdict"], "fail")

    def test_failed_batches_fails(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=1),
            review_summary=_review(remaining_p0=0, remaining_p1=0),
        )
        self.assertEqual(summary["verdict"], "fail")

    def test_processing_failed_files_fails(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0, processing_failed=1),
            review_summary=_review(remaining_p0=0, remaining_p1=0),
        )
        self.assertEqual(summary["verdict"], "fail")

    def test_verdict_method_returns_fail(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0),
            review_summary=_review(remaining_p0=2, remaining_p1=0),
        )
        result = batch_acceptance_verdict(summary)
        self.assertEqual(result["verdict"], "fail")
        self.assertGreater(result["p0_open_count"], 0)


class TestVerdictConditionalPass(unittest.TestCase):
    def test_p1_open_no_p0_conditional(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0),
            review_summary=_review(remaining_p0=0, remaining_p1=3),
        )
        self.assertEqual(summary["verdict"], "conditional_pass")
        self.assertFalse(summary["pass"])

    def test_verdict_method_returns_conditional(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0),
            review_summary=_review(remaining_p0=0, remaining_p1=2),
        )
        result = batch_acceptance_verdict(summary)
        self.assertEqual(result["verdict"], "conditional_pass")
        self.assertEqual(result["p0_open_count"], 0)
        self.assertGreater(result["p1_open_count"], 0)


class TestVerdictPersistence(unittest.TestCase):
    def test_verdict_in_json_output(self):
        with tempfile.TemporaryDirectory() as td:
            output_path = Path(td) / "acceptance_summary.json"
            run_plan_path = Path(td) / "run_plan.json"
            review_path = Path(td) / "review.json"
            run_plan_path.write_text(json.dumps(_run_plan(failed_batches=0)), encoding="utf-8")
            review_path.write_text(json.dumps(_review(remaining_p0=0, remaining_p1=1)), encoding="utf-8")
            _, payload = write_acceptance_summary(
                output_path=output_path,
                run_plan_summary_path=run_plan_path,
                review_summary_path=review_path,
            )
            self.assertEqual(payload["verdict"], "conditional_pass")
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["verdict"], "conditional_pass")


class TestAutoCheckRate(unittest.TestCase):
    def test_all_clear_rate_is_one(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0),
            review_summary=_review(remaining_p0=0, remaining_p1=0),
        )
        result = batch_acceptance_verdict(summary)
        self.assertEqual(result["auto_check_pass_rate"], 1.0)

    def test_p0_open_rate_is_zero(self):
        summary = build_acceptance_summary(
            run_plan_summary=_run_plan(failed_batches=0),
            review_summary=_review(remaining_p0=1, remaining_p1=0),
        )
        result = batch_acceptance_verdict(summary)
        self.assertEqual(result["auto_check_pass_rate"], 0.0)


def _run_plan(*, failed_batches: int = 0, processing_failed: int = 0) -> dict:
    return {
        "schema_version": "scan-qc.run-plan-summary.v1",
        "privacy": {"aggregate_only": True},
        "summary": {
            "total_batches": 1,
            "failed_batches": failed_batches,
            "processing_failed_files": processing_failed,
        },
        "stage_timings": {
            "scan": {"elapsed_seconds": 10.0},
            "processing": {"elapsed_seconds": 5.0},
        },
    }


def _review(*, remaining_p0: int = 0, remaining_p1: int = 0) -> dict:
    return {
        "schema_version": "scan-qc.review-summary.v1",
        "privacy": {"aggregate_only": True},
        "remaining_p0": remaining_p0,
        "remaining_p1": remaining_p1,
    }


def _processing_audit(*, failed: int = 0) -> dict:
    return {
        "schema_version": "scan-qc.processing-audit-summary.v1",
        "privacy": {"aggregate_only": True},
        "counts": {
            "processed_files": 10,
            "processing_failed_files": failed,
        },
    }


if __name__ == "__main__":
    unittest.main()
