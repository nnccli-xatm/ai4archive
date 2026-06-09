from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.acceptance import build_acceptance_summary
from archive_scan_qc.cli import main


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _aggregate_validation_summary(
    *,
    scan_rate: float,
    processing_rate: float,
    processing_failed_files: int = 0,
    cleanup_preserved_artifacts: list[str] | None = None,
    privacy_passed: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": "scan-qc.aggregate-baseline.v1",
        "privacy": {"aggregate_only": True},
        "aggregate_counts": {
            "total_files": 149,
            "openable_files": 149,
            "processing_processed_files": 149 - processing_failed_files,
            "processing_failed_files": processing_failed_files,
        },
        "stage_timings": {
            "scan": {"files_per_minute": scan_rate},
            "processing": {"processed_files_per_minute": processing_rate},
        },
        "cleanup": {
            "enabled": True,
            "removed_artifacts": ["scan-reports"],
            "preserved_artifacts": cleanup_preserved_artifacts or [],
            "retained_public_summary": "aggregate_baseline_summary.json",
        },
        "privacy_self_check": {
            "passed": privacy_passed,
            "status": "pass" if privacy_passed else "failed",
            "violation_count": 0 if privacy_passed else 1,
        },
    }


class AcceptanceSummaryRegressionTests(unittest.TestCase):
    def test_acceptance_summary_passes_with_clean_aggregate_evidence(self) -> None:
            payload = build_acceptance_summary(
                run_plan_summary={
                    "schema_version": "scan-qc.run-plan-summary.v1",
                    "privacy": {"aggregate_only": True},
                    "summary": {
                        "failed_batches": 0,
                        "processing_failed_files": 0,
                        "scan_files_per_minute": 120.0,
                        "processing_files_per_minute": 80.0,
                    },
                    "batches": [{"workers": 2}],
                },
                review_summary={
                    "schema_version": "scan-qc.review-summary.v1",
                    "sensitivity": "Aggregate-only summary.",
                    "total_findings": 0,
                    "status_counts": {"accepted": 0, "resolved": 0, "pending": 0},
                    "remaining_p0": 0,
                    "remaining_p1": 0,
                    "acceptance_passed": True,
                },
                processing_audit_summary={
                    "schema_version": "scan-qc.processing-audit.v1",
                    "privacy": {"aggregate_only": True},
                    "counts": {"failed_files": 0},
                    "throughput": {"processed_files_per_minute": 82.0},
                    "workers": {"effective_workers": 2},
                    "quality_signals": {
                        "full_chain_cleanup": {
                            "total_files": 10,
                            "improved_files": 6,
                            "preserved_files": 3,
                            "reverted_files": 1,
                            "skipped_files": 1,
                            "improved_ratio": 0.6,
                            "preserved_ratio": 0.3,
                            "reverted_ratio": 0.1,
                            "skipped_ratio": 0.1,
                        }
                    },
                },
                benchmark_results={
                    "schema_version": "scan-qc.benchmark.v1",
                    "privacy": {"aggregate_only": True},
                    "runs": [
                        {
                            "effective_workers": 2,
                            "scan": {"files_per_minute": 125.0},
                            "processing": {"failed_files": 0, "processed_files_per_minute": 84.0, "effective_workers": 2},
                        }
                    ],
                },
                min_scan_files_per_minute=100.0,
                min_processing_files_per_minute=70.0,
            )

            self.assertTrue(payload["pass"])
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["remaining"], {"p0": 0, "p1": 0})
            self.assertEqual(payload["failed_batches"], 0)
            self.assertEqual(payload["processing_failed_files"], 0)
            self.assertFalse(payload["blocking_items"])
            self.assertEqual(payload["full_chain_cleanup_quality"]["status"], "available")
            self.assertEqual(payload["full_chain_cleanup_quality"]["counts"]["improved_files"], 6)
            self.assertEqual(payload["full_chain_cleanup_quality"]["ratios"]["reverted_ratio"], 0.1)
            self.assertEqual(payload["warning_items"], [])

    def test_acceptance_summary_marks_full_chain_cleanup_quality_unknown_when_missing(self) -> None:
            payload = build_acceptance_summary(
                processing_audit_summary={
                    "schema_version": "scan-qc.processing-audit.v1",
                    "privacy": {"aggregate_only": True},
                    "counts": {"failed_files": 0},
                }
            )

            self.assertFalse(payload["blocking_items"])
            self.assertEqual(payload["full_chain_cleanup_quality"]["provided"], False)
            self.assertEqual(payload["full_chain_cleanup_quality"]["status"], "unknown")
            self.assertIsNone(payload["full_chain_cleanup_quality"]["counts"])
            self.assertIsNone(payload["full_chain_cleanup_quality"]["ratios"])
            self.assertEqual(payload["warning_items"], [])

    def test_acceptance_summary_warns_for_low_full_chain_cleanup_improved_ratio(self) -> None:
            payload = build_acceptance_summary(
                processing_audit_summary={
                    "schema_version": "scan-qc.processing-audit.v1",
                    "privacy": {"aggregate_only": True},
                    "quality_signals": {
                        "full_chain_cleanup": {
                            "total_files": 20,
                            "improved_files": 1,
                            "preserved_files": 17,
                            "reverted_files": 2,
                            "skipped_files": 0,
                            "improved_ratio": 0.05,
                            "preserved_ratio": 0.85,
                            "reverted_ratio": 0.1,
                            "skipped_ratio": 0.0,
                        }
                    },
                }
            )

            self.assertTrue(payload["pass"])
            self.assertIn("full_chain_cleanup_low_improved_ratio", {item["code"] for item in payload["warning_items"]})
            self.assertIn("review cleanup settings or sample outputs", " ".join(payload["warnings"]))
            low_item = next(item for item in payload["warning_items"] if item["code"] == "full_chain_cleanup_low_improved_ratio")
            self.assertEqual(low_item["title_zh"], "清理改善比例偏低")
            self.assertIn("改善比例偏低", low_item["message_zh"])
            self.assertIn("重跑验收", low_item["next_step_zh"])
            self.assertTrue(
                any("Review cleanup parameters and spot-check representative processed outputs" in step for step in payload["recommended_next_steps"])
            )
            raw = json.dumps(payload, ensure_ascii=False)
            self.assertIn("清理改善比例偏低", raw)
            for forbidden in ("/Users/private", "page_0001.png", "OCR TEXT", "abcdef0123456789"):
                self.assertNotIn(forbidden, raw)

    def test_acceptance_summary_warns_for_high_full_chain_cleanup_reverted_ratio(self) -> None:
            payload = build_acceptance_summary(
                processing_audit_summary={
                    "schema_version": "scan-qc.processing-audit.v1",
                    "privacy": {"aggregate_only": True},
                    "quality_signals": {
                        "full_chain_cleanup": {
                            "total_files": 24,
                            "improved_files": 8,
                            "preserved_files": 9,
                            "reverted_files": 7,
                            "skipped_files": 0,
                            "improved_ratio": 0.333333,
                            "preserved_ratio": 0.375,
                            "reverted_ratio": 0.291667,
                            "skipped_ratio": 0.0,
                        }
                    },
                }
            )

            self.assertTrue(payload["pass"])
            self.assertIn("full_chain_cleanup_high_reverted_ratio", {item["code"] for item in payload["warning_items"]})
            self.assertIn("review cleanup settings or sample outputs", " ".join(payload["warnings"]))
            high_item = next(item for item in payload["warning_items"] if item["code"] == "full_chain_cleanup_high_reverted_ratio")
            self.assertEqual(high_item["title_zh"], "清理回退比例偏高")
            self.assertIn("回退比例偏高", high_item["message_zh"])
            self.assertIn("重跑验收", high_item["next_step_zh"])

    def test_acceptance_summary_blocks_when_sampling_target_not_met(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary={
                    "schema_version": "scan-qc.aggregate-baseline.v1",
                    "privacy": {"aggregate_only": True},
                    "aggregate_counts": {"processing_failed_files": 0},
                },
                aggregate_sampling_counts={
                    "schema_version": "scan-qc.acceptance-sampling-counts.v1",
                    "privacy": {"aggregate_only": True},
                    "target_sample_ratio": 0.05,
                    "target_sample_count": 5,
                    "generated_sample_task_count": 2,
                    "sample_task_target_met": False,
                    "reviewed_sample_count": 1,
                    "pending_sample_count": 1,
                    "sampling_target_met": False,
                },
            )

            self.assertFalse(payload["pass"])
            self.assertEqual(payload["status"], "fail")
            codes = {item["code"] for item in payload["blocking_items"]}
            self.assertEqual(codes, {"sample_task_target_not_met", "sampling_review_target_not_met"})
            sampling = payload["acceptance_sampling"]
            self.assertEqual(sampling["target_sample_count"], 5)
            self.assertEqual(sampling["generated_sample_task_count"], 2)
            self.assertEqual(sampling["reviewed_sample_count"], 1)
            self.assertEqual(sampling["status"], "fail")
            self.assertIn("抽检比例未达标", sampling["admin_message_zh"])
            self.assertEqual(
                payload["closure_gate_summary"]["operator_message_zh"],
                "抽检还未达到验收比例，请管理员完成抽检复核后再交接。",
            )
            raw = json.dumps(payload, ensure_ascii=False)
            for forbidden in ("page_0001", "/private", "abcdef0123456789", "OCR TEXT"):
                self.assertNotIn(forbidden, raw)

    def test_acceptance_summary_passes_when_sampling_targets_are_met(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary={
                    "schema_version": "scan-qc.aggregate-baseline.v1",
                    "privacy": {"aggregate_only": True},
                    "aggregate_counts": {"processing_failed_files": 0},
                },
                aggregate_sampling_counts={
                    "schema_version": "scan-qc.acceptance-sampling-counts.v1",
                    "privacy": {"aggregate_only": True},
                    "target_sample_ratio": 0.05,
                    "target_sample_count": 5,
                    "generated_sample_task_count": 5,
                    "sample_task_target_met": True,
                    "reviewed_sample_count": 5,
                    "pending_sample_count": 0,
                    "sampling_target_met": True,
                },
            )

            self.assertTrue(payload["pass"])
            self.assertEqual(payload["status"], "pass")
            self.assertFalse(payload["blocking_items"])
            self.assertEqual(payload["acceptance_sampling"]["status"], "pass")
            self.assertIn("抽检比例已达标", payload["acceptance_sampling"]["admin_message_zh"])

    def test_acceptance_summary_passes_with_aggregate_baseline_summary(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary={
                    "schema_version": "scan-qc.aggregate-baseline.v1",
                    "privacy": {"aggregate_only": True},
                    "worker_settings": {"requested_workers": 4},
                    "aggregate_counts": {"processing_failed_files": 0},
                    "stage_timings": {
                        "scan": {"files_per_minute": 137.93},
                        "processing": {"processed_files_per_minute": 111.61},
                    },
                    "cleanup": {
                        "enabled": True,
                        "removed_artifacts": ["scan-reports", "processed-images"],
                        "preserved_artifacts": [],
                        "retained_public_summary": "aggregate_baseline_summary.json",
                    },
                    "privacy_self_check": {"passed": True, "status": "pass", "violation_count": 0},
                },
                min_scan_files_per_minute=120.0,
                min_processing_files_per_minute=100.0,
            )

            self.assertTrue(payload["pass"])
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["processing_failed_files"], 0)
            self.assertEqual(payload["throughput"]["scan_files_per_minute"]["best_observed"], 137.93)
            self.assertEqual(payload["throughput"]["processing_files_per_minute"]["best_observed"], 111.61)
            self.assertTrue(payload["privacy_self_check"]["passed"])
            self.assertTrue(payload["cleanup"]["retained_public_summary_only"])

    def test_acceptance_summary_fails_for_aggregate_baseline_regressions(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary={
                    "schema_version": "scan-qc.aggregate-baseline.v1",
                    "privacy": {"aggregate_only": True},
                    "aggregate_counts": {"processing_failed_files": 1},
                    "stage_timings": {
                        "scan": {"files_per_minute": 90.0},
                        "processing": {"processed_files_per_minute": 60.0},
                    },
                    "cleanup": {
                        "enabled": True,
                        "removed_artifacts": ["scan-reports"],
                        "preserved_artifacts": [],
                        "retained_public_summary": "aggregate_baseline_summary.json",
                    },
                    "privacy_self_check": {"passed": True, "status": "pass", "violation_count": 0},
                },
                min_scan_files_per_minute=100.0,
                min_processing_files_per_minute=70.0,
            )

            self.assertFalse(payload["pass"])
            codes = {item["code"] for item in payload["blocking_items"]}
            self.assertEqual(
                codes,
                {
                    "processing_failed_files",
                    "scan_throughput_below_threshold",
                    "processing_throughput_below_threshold",
                },
            )

    def test_acceptance_summary_diagnoses_main_scan_baseline_drift_without_hiding_absolute_gate(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary=_aggregate_validation_summary(scan_rate=96.0, processing_rate=124.0),
                main_aggregate_baseline_summary=_aggregate_validation_summary(scan_rate=100.0, processing_rate=126.0),
                min_scan_files_per_minute=120.0,
                min_processing_files_per_minute=90.0,
            )

            self.assertFalse(payload["pass"])
            self.assertEqual(payload["status"], "fail")
            codes = {item["code"] for item in payload["blocking_items"]}
            self.assertEqual(codes, {"scan_throughput_below_threshold"})
            comparison = payload["main_comparison"]
            self.assertTrue(comparison["provided"])
            self.assertEqual(comparison["diagnostic_code"], "baseline_scan_throughput_drift_not_pr_specific")
            self.assertIn("baseline_scan_throughput_drift_not_pr_specific", comparison["warning_codes"])
            self.assertEqual(comparison["throughput"]["scan_files_per_minute"]["delta_files_per_minute"], -4.0)
            self.assertFalse(comparison["throughput"]["scan_files_per_minute"]["pr_threshold_met"])
            self.assertFalse(comparison["throughput"]["scan_files_per_minute"]["main_threshold_met"])
            self.assertTrue(
                any("latest main aggregate evidence" in warning and "PR-specific regression" in warning for warning in payload["warnings"])
            )

    def test_acceptance_summary_fails_when_pr_throughput_regresses_versus_main(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary=_aggregate_validation_summary(scan_rate=80.0, processing_rate=76.0),
                main_aggregate_baseline_summary=_aggregate_validation_summary(scan_rate=115.0, processing_rate=100.0),
                min_scan_files_per_minute=120.0,
                min_processing_files_per_minute=90.0,
            )

            self.assertFalse(payload["pass"])
            codes = {item["code"] for item in payload["blocking_items"]}
            self.assertIn("scan_throughput_below_threshold", codes)
            self.assertIn("processing_throughput_below_threshold", codes)
            self.assertIn("scan_throughput_regressed_vs_main", codes)
            self.assertIn("processing_throughput_regressed_vs_main", codes)
            comparison = payload["main_comparison"]
            self.assertEqual(comparison["diagnostic_code"], "scan_throughput_regressed_vs_main")
            self.assertEqual(comparison["throughput"]["scan_files_per_minute"]["delta_files_per_minute"], -35.0)
            self.assertEqual(comparison["throughput"]["processing_files_per_minute"]["delta_files_per_minute"], -24.0)

    def test_acceptance_summary_main_comparison_does_not_mask_failure_privacy_or_cleanup_blocks(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary=_aggregate_validation_summary(
                    scan_rate=96.0,
                    processing_rate=124.0,
                    processing_failed_files=2,
                    cleanup_preserved_artifacts=["processed-images"],
                    privacy_passed=False,
                ),
                main_aggregate_baseline_summary=_aggregate_validation_summary(scan_rate=100.0, processing_rate=126.0),
                min_scan_files_per_minute=120.0,
                min_processing_files_per_minute=90.0,
            )

            self.assertFalse(payload["pass"])
            codes = {item["code"] for item in payload["blocking_items"]}
            self.assertIn("processing_failed_files", codes)
            self.assertIn("privacy_self_check_failed", codes)
            self.assertIn("cleanup_retention_failed", codes)
            self.assertIn("scan_throughput_below_threshold", codes)
            self.assertEqual(payload["main_comparison"]["diagnostic_code"], "baseline_scan_throughput_drift_not_pr_specific")

    def test_acceptance_summary_main_comparison_remains_public_safe(self) -> None:
            pr_summary = _aggregate_validation_summary(scan_rate=96.0, processing_rate=124.0)
            pr_summary["private_path"] = "/private/source/page_0001.tif"
            pr_summary["sha256"] = "abcdef0123456789abcdef0123456789"
            pr_summary["ocr_text"] = "SECRET OCR TEXT"
            main_summary = _aggregate_validation_summary(scan_rate=100.0, processing_rate=126.0)
            main_summary["files"] = [{"relative_path": "private_page_0001.png", "thumbnail": "data:image/png;base64,secret"}]

            payload = build_acceptance_summary(
                aggregate_baseline_summary=pr_summary,
                main_aggregate_baseline_summary=main_summary,
                min_scan_files_per_minute=120.0,
                min_processing_files_per_minute=90.0,
            )

            raw = json.dumps(payload, ensure_ascii=False)
            for forbidden in [
                "/private/source/page_0001.tif",
                "private_page_0001.png",
                "abcdef0123456789",
                "SECRET OCR TEXT",
                "relative_path",
                "data:image/png",
            ]:
                self.assertNotIn(forbidden, raw)
            self.assertTrue(payload["main_comparison"]["privacy"]["aggregate_only"])

    def test_acceptance_summary_allows_missing_optional_aggregate_baseline_fields(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary={
                    "schema_version": "scan-qc.aggregate-baseline.v1",
                    "privacy": {"aggregate_only": True},
                    "aggregate_counts": {"processing_failed_files": 0},
                }
            )

            self.assertTrue(payload["pass"])
            self.assertFalse(payload["throughput"]["scan_files_per_minute"]["provided"])
            self.assertEqual(payload["privacy_self_check"]["provided"], False)
            self.assertEqual(payload["cleanup"]["provided"], False)

    def test_acceptance_summary_fails_for_aggregate_baseline_privacy_self_check(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary={
                    "schema_version": "scan-qc.aggregate-baseline.v1",
                    "privacy": {"aggregate_only": True},
                    "aggregate_counts": {"processing_failed_files": 0},
                    "cleanup": {
                        "enabled": True,
                        "removed_artifacts": ["scan-reports"],
                        "preserved_artifacts": [],
                        "retained_public_summary": "aggregate_baseline_summary.json",
                    },
                    "privacy_self_check": {"passed": False, "status": "failed", "violation_count": 1},
                }
            )

            self.assertFalse(payload["pass"])
            self.assertIn("privacy_self_check_failed", {item["code"] for item in payload["blocking_items"]})

    def test_acceptance_summary_fails_for_aggregate_baseline_cleanup_retention(self) -> None:
            payload = build_acceptance_summary(
                aggregate_baseline_summary={
                    "schema_version": "scan-qc.aggregate-baseline.v1",
                    "privacy": {"aggregate_only": True},
                    "aggregate_counts": {"processing_failed_files": 0},
                    "cleanup": {
                        "enabled": True,
                        "removed_artifacts": ["scan-reports"],
                        "preserved_artifacts": ["processed-images"],
                        "retained_public_summary": "aggregate_baseline_summary.json",
                    },
                    "privacy_self_check": {"passed": True, "status": "pass", "violation_count": 0},
                }
            )

            self.assertFalse(payload["pass"])
            self.assertIn("cleanup_retention_failed", {item["code"] for item in payload["blocking_items"]})

    def test_acceptance_summary_fails_for_remaining_and_performance_thresholds(self) -> None:
            payload = build_acceptance_summary(
                run_plan_summary={
                    "schema_version": "scan-qc.run-plan-summary.v1",
                    "privacy": {"aggregate_only": True},
                    "summary": {
                        "failed_batches": 1,
                        "processing_failed_files": 2,
                        "scan_files_per_minute": 9.0,
                        "processing_files_per_minute": 4.0,
                    },
                },
                review_summary={
                    "schema_version": "scan-qc.review-summary.v1",
                    "sensitivity": "Aggregate-only summary.",
                    "remaining_p0": 1,
                    "remaining_p1": 1,
                    "acceptance_passed": False,
                },
                min_scan_files_per_minute=10.0,
                min_processing_files_per_minute=5.0,
            )

            self.assertFalse(payload["pass"])
            self.assertEqual(payload["status"], "fail")
            codes = {item["code"] for item in payload["blocking_items"]}
            self.assertEqual(
                codes,
                {
                    "remaining_p0",
                    "remaining_p1",
                    "failed_batches",
                    "processing_failed_files",
                    "scan_throughput_below_threshold",
                    "processing_throughput_below_threshold",
                },
            )

    def test_acceptance_summary_missing_inputs_warns_and_requires_some_input(self) -> None:
            with self.assertRaisesRegex(ValueError, "At least one aggregate evidence input is required"):
                build_acceptance_summary()

            payload = build_acceptance_summary(
                review_summary={
                    "schema_version": "scan-qc.review-summary.v1",
                    "sensitivity": "Aggregate-only summary.",
                    "remaining_p0": 0,
                    "remaining_p1": 0,
                    "acceptance_passed": True,
                }
            )

            self.assertTrue(payload["pass"])
            self.assertEqual(payload["closure_gate_summary"]["open_p0_count"], 0)
            self.assertEqual(payload["closure_gate_summary"]["open_p1_count"], 0)
            self.assertTrue(payload["closure_gate_summary"]["can_complete_delivery"])
            self.assertTrue(any("run_plan_summary was not provided" in warning for warning in payload["warnings"]))
            self.assertTrue(any("benchmark_results was not provided" in warning for warning in payload["warnings"]))

    def test_acceptance_summary_does_not_copy_private_values(self) -> None:
            payload = build_acceptance_summary(
                run_plan_summary={
                    "schema_version": "scan-qc.run-plan-summary.v1",
                    "privacy": {"aggregate_only": True},
                    "summary": {
                        "failed_batches": 0,
                        "processing_failed_files": 0,
                        "scan_files_per_minute": 100.0,
                        "failed_batch_ids": ["SECRET_BATCH_CASE_123"],
                    },
                    "batches": [
                        {
                            "batch_id": "SECRET_BATCH_CASE_123",
                            "report_dir": "/private/source/report",
                            "process_out": "../private/process",
                            "workers": 1,
                        }
                    ],
                },
                review_summary={
                    "schema_version": "scan-qc.review-summary.v1",
                    "sensitivity": "Aggregate-only summary.",
                    "remaining_p0": 0,
                    "remaining_p1": 0,
                    "status_counts": {"pending": 0},
                    "acceptance_passed": True,
                },
                benchmark_results={
                    "schema_version": "scan-qc.benchmark.v1",
                    "privacy": {"aggregate_only": True},
                    "runs": [
                        {
                            "scan": {"files_per_minute": 100.0},
                            "processing": {"failed_files": 0, "processed_files_per_minute": 50.0},
                            "finding_rule_counts": {"private_rule": 1},
                        }
                    ],
                },
            )

            raw = json.dumps(payload, ensure_ascii=False)
            for forbidden in [
                "SECRET_BATCH_CASE_123",
                "/private/source/report",
                "../private/process",
                "relative_path",
                "absolute_path",
                "sha256",
                "reviewer_notes",
                "ocr_text",
                "PRIVATE_CASE_0001.png",
            ]:
                self.assertNotIn(forbidden, raw)
            self.assertTrue(payload["privacy"]["aggregate_only"])

    def test_acceptance_summary_cli_reuses_sampling_counts_without_private_rows(self) -> None:
            with tempfile.TemporaryDirectory(prefix="private-sampling-") as temp_dir:
                root = Path(temp_dir)
                baseline_path = root / "aggregate_baseline_summary.json"
                sampling_path = root / "acceptance_sampling_review.json"
                out_path = root / "acceptance_summary.json"
                _write_json(
                    baseline_path,
                    {
                        "schema_version": "scan-qc.aggregate-baseline.v1",
                        "privacy": {"aggregate_only": True},
                        "aggregate_counts": {"processing_failed_files": 0},
                    },
                )
                _write_json(
                    sampling_path,
                    {
                        "schema_version": "scan-qc.acceptance-sampling.v1",
                        "sensitivity": "sensitive_local_evidence",
                        "aggregate_sampling_counts": {
                            "schema_version": "scan-qc.acceptance-sampling-counts.v1",
                            "privacy": {"aggregate_only": True},
                            "target_sample_ratio": 0.05,
                            "target_sample_count": 1,
                            "generated_sample_task_count": 1,
                            "sample_task_target_met": True,
                            "reviewed_sample_count": 0,
                            "pending_sample_count": 1,
                            "sampling_target_met": False,
                        },
                        "samples": [
                            {
                                "relative_path": "/private/archive/page_0001.tif",
                                "filename": "page_0001.tif",
                                "sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
                                "reviewer_notes": "OCR TEXT private note",
                            }
                        ],
                    },
                )

                exit_code = main(
                    [
                        "acceptance-summary",
                        "--aggregate-baseline-summary",
                        str(baseline_path),
                        "--acceptance-sampling-review",
                        str(sampling_path),
                        "--out",
                        str(out_path),
                    ]
                )
                raw = out_path.read_text(encoding="utf-8")
                payload = json.loads(raw)

            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["acceptance_sampling"]["reviewed_sample_count"], 0)
            self.assertIn("sampling_review_target_not_met", {item["code"] for item in payload["blocking_items"]})
            for forbidden in ("private-sampling-", "/private/archive", "page_0001.tif", "abcdef0123456789", "OCR TEXT"):
                self.assertNotIn(forbidden, raw)


if __name__ == "__main__":
    unittest.main()
