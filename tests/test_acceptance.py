"""Tests for aggregate-only production acceptance gate summary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.acceptance import (
    ACCEPTANCE_JSON,
    SCHEMA_VERSION,
    build_acceptance_summary,
    write_acceptance_summary,
)


def _aggregate_only_payload(**overrides):
    base = {"schema_version": "test.v1", "privacy": {"aggregate_only": True}}
    base.update(overrides)
    return base


class TestBuildAcceptanceNoInput(unittest.TestCase):
    def test_no_evidence_raises(self):
        with self.assertRaises(ValueError) as ctx:
            build_acceptance_summary()
        self.assertIn("at least one", str(ctx.exception).lower())


class TestBuildAcceptancePass(unittest.TestCase):
    def test_minimal_pass(self):
        result = build_acceptance_summary(
            run_plan_summary=_aggregate_only_payload(summary={"failed_batches": 0, "processing_failed_files": 0}),
        )
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["pass"])
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["blocking_items"], [])

    def test_pass_with_all_evidence_clean(self):
        result = build_acceptance_summary(
            run_plan_summary=_aggregate_only_payload(
                summary={"failed_batches": 0, "processing_failed_files": 0},
            ),
            review_summary=_aggregate_only_payload(remaining_p0=0, remaining_p1=0),
            processing_audit_summary=_aggregate_only_payload(
                counts={"failed_files": 0},
            ),
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["remaining"]["p0"], 0)
        self.assertEqual(result["remaining"]["p1"], 0)


class TestBuildAcceptanceBlocking(unittest.TestCase):
    def test_p0_findings_block(self):
        result = build_acceptance_summary(
            review_summary=_aggregate_only_payload(remaining_p0=2, remaining_p1=0),
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("remaining_p0", codes)

    def test_p1_findings_block(self):
        result = build_acceptance_summary(
            review_summary=_aggregate_only_payload(remaining_p0=0, remaining_p1=3),
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("remaining_p1", codes)

    def test_failed_batches_block(self):
        result = build_acceptance_summary(
            run_plan_summary=_aggregate_only_payload(summary={"failed_batches": 1}),
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("failed_batches", codes)

    def test_processing_failed_files_block(self):
        result = build_acceptance_summary(
            processing_audit_summary=_aggregate_only_payload(counts={"failed_files": 2}),
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("processing_failed_files", codes)

    def test_non_aggregate_only_evidence_blocks(self):
        result = build_acceptance_summary(
            run_plan_summary={"schema_version": "test.v1", "privacy": {"aggregate_only": False}},
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("run_plan_summary_not_aggregate_only", codes)


class TestBuildAcceptancePrivacy(unittest.TestCase):
    def test_privacy_self_check_blocks(self):
        result = build_acceptance_summary(
            aggregate_baseline_summary=_aggregate_only_payload(
                privacy_self_check={"status": "fail"},
            ),
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("privacy_self_check_failed", codes)

    def test_privacy_self_check_pass_no_block(self):
        result = build_acceptance_summary(
            aggregate_baseline_summary=_aggregate_only_payload(
                privacy_self_check={"status": "pass"},
            ),
        )
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertNotIn("privacy_self_check_failed", codes)
        self.assertTrue(result["privacy_self_check"]["provided"])


class TestBuildAcceptanceSampling(unittest.TestCase):
    def test_sampling_target_not_met_blocks(self):
        counts = {
            "schema_version": "scan-qc.acceptance-sampling-counts.v1",
            "target_sample_count": 5,
            "generated_sample_task_count": 3,
            "reviewed_sample_count": 0,
        }
        result = build_acceptance_summary(
            aggregate_sampling_counts=counts,
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("sample_task_target_not_met", codes)

    def test_sampling_target_met_passes(self):
        counts = {
            "schema_version": "scan-qc.acceptance-sampling-counts.v1",
            "target_sample_count": 2,
            "generated_sample_task_count": 2,
            "reviewed_sample_count": 2,
        }
        result = build_acceptance_summary(
            aggregate_sampling_counts=counts,
        )
        gate = result["acceptance_sampling"]
        self.assertTrue(gate["provided"])
        self.assertTrue(gate["sampling_target_met"])


class TestBuildAcceptanceThroughput(unittest.TestCase):
    def test_throughput_below_threshold_blocks(self):
        baseline = _aggregate_only_payload(
            stage_timings={"scan": {"files_per_minute": 50.0}},
        )
        result = build_acceptance_summary(
            aggregate_baseline_summary=baseline,
            min_scan_files_per_minute=100.0,
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("scan_throughput_below_threshold", codes)


class TestBuildAcceptanceEvidence(unittest.TestCase):
    def test_evidence_tracking(self):
        result = build_acceptance_summary(
            run_plan_summary=_aggregate_only_payload(),
        )
        evidence = result["evidence"]
        self.assertTrue(evidence["run_plan_summary"]["provided"])
        self.assertFalse(evidence["review_summary"]["provided"])
        self.assertFalse(evidence["processing_audit_summary"]["provided"])

    def test_warnings_for_missing_evidence(self):
        result = build_acceptance_summary(
            run_plan_summary=_aggregate_only_payload(),
        )
        warning_texts = " ".join(result["warnings"])
        self.assertIn("review_summary was not provided", warning_texts)


class TestWriteAcceptanceSummary(unittest.TestCase):
    def test_writes_json_file(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            rps = td_path / "run_plan.json"
            rps.write_text(json.dumps(_aggregate_only_payload()), encoding="utf-8")
            output_path = td_path / "results" / ACCEPTANCE_JSON
            path, payload = write_acceptance_summary(
                output_path=output_path,
                run_plan_summary_path=rps,
            )
            self.assertTrue(path.exists())
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["status"], payload["status"])


class TestBuildAcceptanceCleanup(unittest.TestCase):
    def test_cleanup_not_enabled_blocks(self):
        result = build_acceptance_summary(
            aggregate_baseline_summary=_aggregate_only_payload(
                cleanup={"enabled": False, "retained_public_summary_only": False},
            ),
        )
        self.assertEqual(result["status"], "fail")
        codes = [item["code"] for item in result["blocking_items"]]
        self.assertIn("cleanup_retention_not_enabled", codes)

    def test_cleanup_enabled_passes(self):
        result = build_acceptance_summary(
            aggregate_baseline_summary=_aggregate_only_payload(
                cleanup={
                    "enabled": True,
                    "removed_artifacts": ["scan_qc_report.json"],
                    "preserved_artifacts": [],
                    "retained_public_summary": "aggregate_baseline_summary.json",
                },
            ),
        )
        cleanup = result["cleanup"]
        self.assertTrue(cleanup["provided"])
        self.assertTrue(cleanup["enabled"])
        self.assertTrue(cleanup["retained_public_summary_only"])


class TestBuildAcceptanceRecommendedNextSteps(unittest.TestCase):
    def test_pass_has_no_blocker_steps(self):
        result = build_acceptance_summary(
            run_plan_summary=_aggregate_only_payload(summary={"failed_batches": 0}),
        )
        steps = result["recommended_next_steps"]
        self.assertIsInstance(steps, list)


if __name__ == "__main__":
    unittest.main()
