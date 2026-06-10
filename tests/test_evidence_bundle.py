from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.capability_probe import CapabilityProbeConfig, run_capability_probe
from archive_scan_qc.cli import main
from archive_scan_qc.deep_inspection_provider import build_deep_inspection_provider_probe
from archive_scan_qc.evidence_bundle import build_evidence_bundle_summary
from archive_scan_qc.review_decisions import build_review_decision_verification_summary


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _release_candidate_bundle_payload(*, real_artifact_metrics: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "scan-qc.release-candidate-summary.v1",
        "status": "pass",
        "ready_for_release_candidate": True,
        "privacy": {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
        },
        "production_validation": {"status": "pass", "counts": {"total_files": 2, "total_findings": 0}},
        "release_readiness": {"status": "pass", "blocking_item_count": 0},
        "decision": {"blocking_item_count": 0},
    }
    if real_artifact_metrics:
        payload["production_validation"] = {
            "status": "pass",
            "counts": {
                "total_files": 20,
                "openable_files": 20,
                "processing_processed_files": 20,
                "processing_failed_files": 0,
            },
            "scan": {"files_per_minute": 149.4, "openable_files_per_minute": 149.4},
            "processing": {
                "processed_files_per_minute": 294.74,
                "operation_timings": {"deskew": {"file_count": 20, "average_seconds_per_file": 0.2}},
            },
            "thresholds": {
                "min_scan_files_per_minute": 100.0,
                "min_processing_files_per_minute": 50.0,
                "processing_failed_files_max": 0,
            },
        }
        payload["benchmark"] = {
            "scan": {"benchmark_files_per_minute": 140.0},
            "processing": {"benchmark_processed_files_per_minute": 280.0},
            "finding_rule_counts_repeated_runs": {
                "duplicate_file": 0,
                "edge_cutoff": {"min": 0, "max": 1, "total": 1},
            },
        }
        payload["sensitive_artifacts"] = {"paths_embedded": False}
    return payload


def _release_readiness_bundle_payload(*, real_artifact_metrics: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "scan-qc.release-readiness.v1",
        "status": "pass",
        "privacy": {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
        },
        "summary": {"checks_total": 2, "checks_passed": 2, "checks_failed": 0, "blocking_items": 0},
        "checks": {"unit_tests": {"status": "pass", "blocking": False}},
    }
    if real_artifact_metrics:
        payload["sensitive_artifacts"] = {"paths_embedded": False}
    return payload


def _acceptance_bundle_payload(*, real_artifact_metrics: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "scan-qc.acceptance-summary.v1",
        "status": "pass",
        "pass": True,
        "privacy": {"aggregate_only": True},
        "blocking_item_count": 0,
        "warning_item_count": 0,
        "blocking_items": [],
        "human_review": {
            "remaining_p0": 0,
            "remaining_p1": 0,
            "total_findings": 1,
            "status_counts": {"fixed": 1},
        },
        "privacy_self_check": {"provided": True, "passed": True, "status": "pass", "violation_count": 0},
    }
    if real_artifact_metrics:
        payload["thresholds"] = {
            "min_scan_files_per_minute": 100.0,
            "min_processing_files_per_minute": 50.0,
            "processing_failed_files_max": 0,
        }
        payload["throughput"] = {
            "scan_files_per_minute": {"provided": True, "best_observed": 149.4, "lowest_observed": 149.4},
            "processing_files_per_minute": {"provided": True, "best_observed": 294.74, "lowest_observed": 294.74},
        }
    return payload


def _aggregate_baseline_bundle_payload(*, real_artifact_metrics: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "scan-qc.aggregate-baseline.v1",
        "status": "pass",
        "privacy": {"aggregate_only": True},
        "aggregate_counts": {"total_files": 2, "openable_files": 2, "total_findings": 0},
        "privacy_self_check": {"passed": True, "status": "pass", "violation_count": 0},
        "cleanup": {"enabled": True, "retained_public_summary_only": True},
    }
    if real_artifact_metrics:
        payload["stage_timings"] = {
            "scan": {"files_per_minute": 149.4, "openable_files_per_minute": 149.4},
            "processing": {"processed_files_per_minute": 294.74},
        }
        payload["benchmark"] = {
            "scan": {"benchmark_files_per_minute": 140.0},
            "processing": {"benchmark_processed_files_per_minute": 280.0},
            "finding_rule_counts_repeated_runs": {"duplicate_file": 0},
        }
    return payload


def _processing_quality_summary_bundle_payload() -> dict[str, object]:
    return {
        "schema_version": "scan-qc.processing-quality-summary.v1",
        "status": "pass",
        "blocking_codes": [],
        "aggregate_only": True,
        "public_safe": True,
        "quality_measurement": {
            "method": "processing_manifest_and_audit_aggregate",
            "before_after_evidence": "aggregate_metrics_only",
            "row_level_evidence_included": False,
            "image_content_included": False,
        },
        "fixture_context": {
            "source": "generated_at_runtime",
            "synthetic_inputs_only": True,
            "fixture_count": 18,
            "fixture_groups": ["low_contrast_text_page"],
            "protected_content_checks": [
                {
                    "fixture_group": "mixed_photo_stamp_table_page",
                    "checked": True,
                    "status": "pass",
                    "fail_codes": [],
                }
            ],
        },
        "counts": {"processed_files": 18, "failed_files": 0, "retry_list_files": 0, "guardrail_failed_files": 0},
        "quality_signal": {
            "status": "measured_with_changes",
            "processed_files": 18,
            "any_quality_operation_changed_files": 4,
            "quality_operations_applied": {"geometry": True, "background_cleanup": True, "text_enhancement": True},
        },
        "guardrails": {"enabled": True, "warning_files": 0, "failed_files": 0, "failure_reasons": {}},
        "privacy": {
            "aggregate_only": True,
            "public_safe": True,
            "contains_file_list": False,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "contains_environment_values": False,
            "contains_row_level_evidence": False,
        },
    }


def _image_processing_capability_smoke_bundle_payload(
    *,
    status: str = "pass",
    blocking_codes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "scan-qc.image-processing-capability-smoke.v1",
        "status": status,
        "blocking_codes": blocking_codes or [],
        "privacy": {
            "aggregate_only": True,
            "public_safe": True,
            "synthetic_inputs_only": True,
            "private_inputs_read": False,
            "contains_file_list": False,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "contains_environment_values": False,
        },
        "processing_run": {
            "scan_run": True,
            "image_processing_run": True,
            "provider_commands_run": False,
            "source_images_modified": False,
            "derivative_images_written": True,
            "temp_work_paths_published": False,
        },
        "synthetic_fixture_summary": {
            "fixture_count": 18,
            "fixture_source": "generated_at_runtime",
            "fixture_groups": ["low_contrast_text_page"],
            "private_source_images_required": False,
        },
        "quality_baseline": _processing_quality_summary_bundle_payload(),
        "related_public_safe_artifacts": {
            "image_processing_capability_smoke": "image_processing_capability_smoke.json",
            "processing_quality_summary": "processing_quality_summary.json",
        },
        "counts": {"synthetic_fixture_count": 18, "processed_files": 18, "failed_files": 0},
        "operation_counts": {"deskewed_files": 1, "tone_normalized_files": 1},
        "source_semantics": {
            "source_images_modified": False,
            "originals_read_only": True,
            "derivatives_only": True,
        },
    }


def _deep_inspection_candidate_bundle_payload() -> dict[str, object]:
    return {
        "schema_version": "scan-qc.deep-inspection-candidates.v1",
        "status": "pass",
        "candidate_total": 3,
        "candidates_by_reason": {
            "rule_bucket:quality": 2,
            "processing_review_status:failed": 1,
        },
        "candidates_by_severity": {"P0": 0, "P1": 1, "P2": 2, "unknown": 0},
        "provider_configured": False,
        "provider_count": 0,
        "checks_passed": ["scan_report_loaded", "provider_eligibility_summarized"],
        "checks_failed": [],
        "privacy_status": "aggregate_public_safe",
        "privacy": {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_row_level_findings": False,
            "contains_reviewer_notes": False,
            "contains_manifests": False,
            "contains_derivative_image_references": False,
            "contains_source_roots": False,
            "network_calls": False,
        },
        "no_inference_run": True,
        "dry_run_only": True,
    }


def _review_decision_verification_bundle_payload(*, blocked: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "scan-qc.review-decision-verification-summary.v1",
        "status": "pass",
        "checks_passed": 1,
        "checks_failed": 0,
        "decision_summary": {
            "total_decisions": 3,
            "pending": 0,
            "accepted": 1,
            "rejected": 1,
            "rework": 1,
            "completion_status": "complete",
            "decision_counts": {
                "pending": 0,
                "accepted_issue": 1,
                "false_positive": 1,
                "fixed_externally": 0,
                "needs_rescan": 1,
                "blocked": 0,
            },
        },
        "blocking_counts_by_code": {},
        "warning_counts_by_code": {},
        "blocking_count": 0,
        "warning_count": 0,
        "privacy": {
            "status": "pass",
            "aggregate_only": True,
            "sensitive_field_count": 0,
            "source_values_omitted": True,
        },
    }
    if blocked:
        payload.update(
            {
                "status": "blocked",
                "checks_passed": 0,
                "checks_failed": 1,
                "blocking_counts_by_code": {"unknown_decision_value": 1},
                "warning_counts_by_code": {"ignored_extra_decision_field": 2},
                "blocking_count": 1,
                "warning_count": 2,
                "privacy": {
                    "status": "blocked",
                    "aggregate_only": False,
                    "sensitive_field_count": 1,
                    "source_values_omitted": True,
                },
            }
        )
    return payload


def _review_decision_export_fixture(
    decisions: tuple[str, ...] = ("accepted_issue", "false_positive", "fixed_externally"),
) -> dict[str, object]:
    decision_rows = [
        {"scope": "finding", "local_id": f"RID{index:04d}", "decision": decision}
        for index, decision in enumerate(decisions, start=1)
    ]
    counts = {
        decision: 0
        for decision in ("pending", "accepted_issue", "false_positive", "fixed_externally", "needs_rescan", "blocked")
    }
    for decision in decisions:
        if decision in counts:
            counts[decision] += 1
    pending = counts["pending"]
    reviewed = len(decision_rows) - pending
    return {
        "schema": "scan-qc-review-decisions.local.v1",
        "source_type": "aggregate_handoff",
        "source_target_count": len(decision_rows),
        "generated_in_browser": True,
        "privacy": {"summary_only": True},
        "aggregate_counts": {
            "total_batches": 1,
            "total_findings": len(decision_rows),
            "p0": 1,
            "p1": 1,
            "p2": 1,
            "p0_pending": counts["pending"],
            "p1_pending": 0,
            "review_completion": {
                "total": len(decision_rows),
                "reviewed": reviewed,
                "pending": pending,
                "complete": pending == 0,
                "counts": counts,
            },
        },
        "review_counts": counts,
        "reviewed_targets": reviewed,
        "decisions": decision_rows,
    }


class EvidenceBundleTests(unittest.TestCase):
    def test_evidence_bundle_verifier_passes_aggregate_handoff_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "release_readiness_summary.json", _release_readiness_bundle_payload())
            _write_json(root / "acceptance_summary.json", _acceptance_bundle_payload())
            _write_json(root / "aggregate_baseline_summary.json", _aggregate_baseline_bundle_payload())
            _write_json(root / "capability_probe.json", run_capability_probe(CapabilityProbeConfig(include_torch_cuda=False)))
            _write_json(root / "deep_inspection_provider_probe.json", build_deep_inspection_provider_probe())
            _write_json(root / "deep_inspection_candidate_summary.json", _deep_inspection_candidate_bundle_payload())
            _write_json(root / "review_decision_verification_summary.json", _review_decision_verification_bundle_payload())

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertGreater(summary["checks_passed"], 0)
        self.assertEqual(summary["checks_failed"], 0)
        self.assertFalse(summary["privacy"]["private_indicators_found"])
        self.assertEqual(summary["artifacts"]["deep_inspection_candidate_summary.json"]["candidate_total"], 3)
        review_decisions = summary["artifacts"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["status"], "pass")
        self.assertEqual(review_decisions["decision_summary"]["total_decisions"], 3)
        self.assertEqual(review_decisions["privacy_status"], "pass")

    def test_evidence_bundle_verifier_allows_missing_optional_deep_inspection_candidate_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["artifact_presence"]["deep_inspection_candidate_summary.json"]["status"], "optional_missing")
        self.assertNotIn("deep_inspection_candidate_summary.json", {item["artifact"] for item in summary["blocking_items"]})

    def test_evidence_bundle_verifier_accepts_image_processing_public_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "image_processing_capability_smoke.json", _image_processing_capability_smoke_bundle_payload())
            _write_json(root / "processing_quality_summary.json", _processing_quality_summary_bundle_payload())

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["artifact_presence"]["image_processing_capability_smoke.json"]["status"], "pass")
        self.assertEqual(summary["artifact_presence"]["processing_quality_summary.json"]["status"], "pass")
        self.assertFalse(summary["privacy"]["private_indicators_found"])

    def test_evidence_bundle_verifier_propagates_image_processing_blocking_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(
                root / "image_processing_capability_smoke.json",
                _image_processing_capability_smoke_bundle_payload(
                    status="fail",
                    blocking_codes=["text_edge_energy_not_improved"],
                ),
            )

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "fail")
        codes = {item["code"] for item in summary["blocking_items"] if item["artifact"] == "image_processing_capability_smoke.json"}
        self.assertIn("artifact_status_failed", codes)
        self.assertIn("text_edge_energy_not_improved", codes)
        self.assertNotIn(str(root), raw)

    def test_evidence_bundle_verifier_blocks_deep_inspection_candidate_privacy_or_inference_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = _deep_inspection_candidate_bundle_payload()
            payload["privacy"]["contains_paths"] = True
            payload["no_inference_run"] = False
            payload["checks_failed"] = ["privacy_guard_failed"]
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "deep_inspection_candidate_summary.json", payload)

            exit_code = main(["evidence-bundle-verify", "--evidence-dir", str(root), "--out", str(root / "bundle.json")])
            raw = (root / "bundle.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 1)
        codes = {item["code"] for item in summary["blocking_items"] if item["artifact"] == "deep_inspection_candidate_summary.json"}
        self.assertIn("privacy_flag_contains_private_evidence", codes)
        self.assertIn("inference_run_not_allowed", codes)
        self.assertIn("checks_failed_present", codes)
        self.assertNotIn("privacy_guard_failed", raw)

    def test_evidence_bundle_verifier_blocks_review_decision_verification_by_code_count_only(self) -> None:
        forbidden_private_values = [
            "/Users/private/archive",
            "page_0001.png",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "OCR TEXT",
            "reviewer note",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = _review_decision_verification_bundle_payload(blocked=True)
            payload["operator_warning"] = " ".join(forbidden_private_values)
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "review_decision_verification_summary.json", payload)

            exit_code = main(["evidence-bundle-verify", "--evidence-dir", str(root), "--out", str(root / "bundle.json")])
            raw = (root / "bundle.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 1)
        codes = {item["code"] for item in summary["blocking_items"] if item["artifact"] == "review_decision_verification_summary.json"}
        self.assertIn("artifact_status_failed", codes)
        self.assertIn("checks_failed_present", codes)
        self.assertIn("review_decision_blocking_count_present", codes)
        self.assertIn("review_decision_privacy_not_public_safe", codes)
        self.assertEqual(summary["artifacts"]["review_decision_verification_summary.json"]["blocking_counts_by_code"]["unknown_decision_value"], 1)
        for value in forbidden_private_values:
            self.assertNotIn(value, raw)

    def test_evidence_bundle_verifier_passes_real_review_decision_verifier_output_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = build_review_decision_verification_summary(_review_decision_export_fixture())
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "review_decision_verification_summary.json", payload)

            exit_code = main(["evidence-bundle-verify", "--evidence-dir", str(root), "--out", str(root / "bundle.json")])
            raw = (root / "bundle.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 0)
        review_decisions = summary["artifacts"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["status"], "pass")
        self.assertEqual(review_decisions["decision_summary"]["total_decisions"], 3)
        self.assertEqual(review_decisions["privacy_status"], "pass")
        self.assertNotIn("scan-qc-review-decisions.local.v1", raw)
        self.assertNotIn("aggregate_handoff", raw)

    def test_evidence_bundle_verifier_blocks_arbitrary_review_decision_source_fields(self) -> None:
        private_source = "/Users/private/archive/page_0001.png"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = build_review_decision_verification_summary(_review_decision_export_fixture())
            payload["source"] = {
                "schema": "scan-qc-review-decisions.local.v1",
                "source_type": "aggregate_handoff",
                "source_path": private_source,
            }
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "review_decision_verification_summary.json", payload)

            exit_code = main(["evidence-bundle-verify", "--evidence-dir", str(root), "--out", str(root / "bundle.json")])
            raw = (root / "bundle.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 1)
        codes = {item["code"] for item in summary["blocking_items"] if item["artifact"] == "review_decision_verification_summary.json"}
        self.assertIn("private_key_present", codes)
        self.assertNotIn(private_source, raw)

    def test_evidence_bundle_verifier_allows_real_artifact_aggregate_metric_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload(real_artifact_metrics=True))
            _write_json(root / "release_readiness_summary.json", _release_readiness_bundle_payload(real_artifact_metrics=True))
            _write_json(root / "acceptance_summary.json", _acceptance_bundle_payload(real_artifact_metrics=True))
            _write_json(root / "aggregate_baseline_summary.json", _aggregate_baseline_bundle_payload(real_artifact_metrics=True))

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["checks_failed"], 0)
        self.assertEqual(summary["blocking_items"], [])
        self.assertFalse(summary["privacy"]["private_indicators_found"])

    def test_evidence_bundle_verifier_allows_aggregate_baseline_counter_paths_without_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = _aggregate_baseline_bundle_payload()
            baseline.pop("status")
            baseline["aggregate_counts"] = {
                "total_files": 20,
                "openable_files": 20,
                "total_findings": 2,
                "p0_findings": 0,
                "p1_findings": 0,
                "p2_findings": {"accepted": 2, "remaining": 0},
                "processing_failed_files": 0,
            }
            baseline["benchmark"] = {
                "source": "benchmark repeated worker runs",
                "worker_sweep": {
                    "workers": [
                        {"workers": 1, "processing": {"failed_files": 0}},
                        {"workers": 2, "processing": {"failed_files": {"observed": 0}}},
                    ]
                },
            }
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "aggregate_baseline_summary.json", baseline)

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["checks_failed"], 0)
        self.assertFalse(summary["privacy"]["private_indicators_found"])
        self.assertEqual(summary["artifacts"]["aggregate_baseline_summary.json"]["reported_status"], "pass")

    def test_evidence_bundle_verifier_still_blocks_private_values_under_metric_like_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = _release_candidate_bundle_payload(real_artifact_metrics=True)
            payload["benchmark"]["finding_rule_counts_repeated_runs"]["duplicate_file"] = {
                "count": 1,
                "source": "/private/validation-host/tmp/A001_0001.png",
            }
            payload["sensitive_artifacts"]["paths_embedded"] = "/private/validation-host/tmp/A001_0001.png"
            _write_json(root / "release_candidate_summary.json", payload)

            exit_code = main(["evidence-bundle-verify", "--evidence-dir", str(root), "--out", str(root / "bundle.json")])
            raw = (root / "bundle.json").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("private_key_present", raw)
        self.assertNotIn("/private/validation-host/tmp/A001_0001.png", raw)
        self.assertNotIn("A001_0001.png", raw)

    def test_evidence_bundle_verifier_rejects_private_benchmark_source_values(self) -> None:
        private_sources = [
            "/private/archive/A001_0001.png",
            "A001_0001.png",
            "benchmark repeated worker runs 0123456789abcdef0123456789abcdef",
        ]
        for private_source in private_sources:
            with self.subTest(private_source=private_source), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                baseline = _aggregate_baseline_bundle_payload()
                baseline["aggregate_counts"] = {"processing_failed_files": 0}
                baseline["benchmark"] = {"source": private_source}
                _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
                _write_json(root / "aggregate_baseline_summary.json", baseline)

                summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")
                raw = json.dumps(summary)

            self.assertEqual(summary["status"], "fail")
            codes = {item["code"] for item in summary["blocking_items"]}
            self.assertTrue(
                {
                    "private_key_present",
                    "private_value_present",
                    "private_absolute_path_pattern_present",
                    "private_filename_pattern_present",
                    "private_hash_pattern_present",
                }
                & codes
            )
            self.assertNotIn(private_source, raw)

    def test_evidence_bundle_verifier_rejects_private_counter_path_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            baseline = _aggregate_baseline_bundle_payload()
            baseline["aggregate_counts"] = {
                "p0_findings": "A001_0001.png",
                "p1_findings": 0,
                "p2_findings": 0,
                "processing_failed_files": 0,
            }
            baseline["benchmark"] = {
                "source": "benchmark repeated worker runs",
                "worker_sweep": {
                    "workers": [
                        {"workers": 1, "processing": {"failed_files": "A001_0001.png"}},
                    ]
                },
            }
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "aggregate_baseline_summary.json", baseline)

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary)

        self.assertEqual(summary["status"], "fail")
        codes = {item["code"] for item in summary["blocking_items"]}
        self.assertTrue({"private_key_present", "private_value_present", "private_filename_pattern_present"} & codes)
        self.assertNotIn("A001_0001.png", raw)

    def test_evidence_bundle_verifier_blocks_missing_required_but_allows_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["artifact_presence"]["release_candidate_summary.json"]["status"], "missing")
        self.assertEqual(summary["artifact_presence"]["release_readiness_summary.json"]["status"], "optional_missing")

    def test_evidence_bundle_verifier_flags_failed_privacy_and_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad = _release_candidate_bundle_payload()
            bad["privacy"]["contains_paths"] = True
            _write_json(root / "release_candidate_summary.json", bad)
            (root / "release_readiness_summary.json").write_text("{not-json", encoding="utf-8")

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        codes = {item["code"] for item in summary["blocking_items"]}
        self.assertEqual(summary["status"], "fail")
        self.assertIn("privacy_flag_contains_private_evidence", codes)
        self.assertIn("malformed_json", codes)

    def test_evidence_bundle_verifier_omits_private_token_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = _release_candidate_bundle_payload()
            payload["operator_warning"] = "source sample at /Users/private/archive/page_0001.png uses token SECRET123"
            _write_json(root / "release_candidate_summary.json", payload)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["evidence-bundle-verify", "--evidence-dir", str(root), "--out", str(root / "bundle.json")])
            raw = (root / "bundle.json").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("private_value_present", raw)
        self.assertNotIn("/Users/private/archive/page_0001.png", raw)
        self.assertNotIn("SECRET123", raw)
        self.assertNotIn("page_0001.png", raw)
        self.assertIn("Evidence bundle status: fail", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
