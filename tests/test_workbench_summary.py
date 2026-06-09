from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.workbench_summary import build_workbench_public_summary

from test_evidence_bundle import (
    _acceptance_bundle_payload,
    _deep_inspection_candidate_bundle_payload,
    _release_candidate_bundle_payload,
    _write_json,
)
from test_final_handoff import _aggregate_evidence_bundle_payload
from test_validation_index import _final_handoff_bundle_payload, _write_public_safe_validation_index_fixtures


def _review_summary_bundle_payload() -> dict[str, object]:
    return {
        "schema_version": "scan-qc.review-summary.v1",
        "status": "pass",
        "acceptance_passed": True,
        "total_findings": 1,
        "remaining_p0": 0,
        "remaining_p1": 0,
        "status_counts": {"fixed": 1},
        "severity_counts": {"P1": 1},
        "severity_status_counts": {"P1": {"fixed": 1}},
        "rule_status_counts": {"dpi_below_minimum": {"fixed": 1}},
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
    }


def _capability_probe_bundle_payload() -> dict[str, object]:
    return {
        "schema_version": "scan-qc.capability-probe.v1",
        "status": "pass",
        "optional_packages": {
            "onnxruntime": {"available": True},
            "paddleocr": {"available": False},
        },
        "readiness": {
            "blocking": False,
            "provider_packages_found": ["onnxruntime"],
            "gpu_acceleration_configured": False,
            "model_acceleration_configured": False,
        },
        "gpu_provider_visibility": {"gpu_visible_count": 0, "torch_cuda": {"visible_count": 0}},
        "configuration": {
            "any_provider_configured": False,
            "analysis_provider_configured": False,
            "gpu_acceleration_configured": False,
            "model_acceleration_configured": False,
        },
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
    }


def _artifact_readiness_bundle_payload(*, ready: bool) -> dict[str, object]:
    return {
        "schema_version": "scan-qc-artifact-readiness-checklist.v1",
        "status": "pass" if ready else "fail",
        "artifact_readiness_checklist": {
            "ready": ready,
            "missing_count": 0 if ready else 1,
            "blocking_count": 0 if ready else 1,
            "warning_count": 0,
            "stale_count": 0,
        },
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
    }


def _workbench_public_summary_payload() -> dict[str, object]:
    return {
        "schema_version": "scan-qc.workbench-public-summary.v1",
        "status": "pass",
        "ready": True,
        "checks_passed": 16,
        "checks_failed": 0,
        "blocking_item_count": 0,
        "warning_item_count": 0,
        "summary": {
            "known_artifacts": 17,
            "artifacts_present": 16,
            "artifacts_passed": 16,
            "artifacts_failed": 0,
            "artifacts_missing": 1,
            "unsupported_inputs": 0,
        },
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
    }


class WorkbenchSummaryTests(unittest.TestCase):
    def test_workbench_summary_passes_with_public_aggregate_bundle(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_public_safe_validation_index_fixtures(root)
                _write_json(root / "acceptance_summary.json", _acceptance_bundle_payload())
                _write_json(root / "review_summary.json", _review_summary_bundle_payload())
                _write_json(root / "deep_inspection_candidate_summary.json", _deep_inspection_candidate_bundle_payload())
                _write_json(root / "capability_probe.json", _capability_probe_bundle_payload())
                _write_json(root / "artifact_readiness_checklist.json", _artifact_readiness_bundle_payload(ready=True))

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(["workbench-summary", "--evidence-dir", str(root), "--out", str(root / "workbench.json")])
                summary = json.loads((root / "workbench.json").read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertEqual(summary["schema_version"], "scan-qc.workbench-public-summary.v1")
            self.assertEqual(summary["status"], "pass")
            self.assertTrue(summary["ready"])
            self.assertEqual(summary["checks_failed"], 0)
            self.assertEqual(summary["blocking_counts_by_code"], {})
            self.assertEqual(summary["artifact_presence"]["final_production_handoff_summary.json"]["status"], "pass")
            self.assertEqual(summary["workflow_state"]["handoff_status"], "pass")
            self.assertEqual(summary["artifacts"]["deep_inspection_candidate_summary.json"]["metrics"]["candidate_total"], 3)
            self.assertEqual(summary["summary"]["deep_inspection_readiness"]["candidate_total"], 3)
            self.assertEqual(summary["summary"]["deep_inspection_readiness"]["candidates_by_severity"]["P1"], 1)
            self.assertFalse(summary["summary"]["deep_inspection_readiness"]["provider_configured"])
            self.assertEqual(summary["summary"]["provider_capability_readiness"]["provider_packages_found_count"], 1)
            self.assertEqual(summary["summary"]["provider_capability_readiness"]["optional_package_visible_count"], 1)
            self.assertEqual(summary["summary"]["provider_capability_readiness"]["optional_package_missing_count"], 1)
            self.assertFalse(summary["summary"]["provider_capability_readiness"]["gpu_acceleration_configured"])
            self.assertEqual(summary["summary"]["provider_capability_readiness"]["privacy_status"], "aggregate_public_safe")
            closure = summary["summary"]["human_review_closure"]
            self.assertEqual(closure["total_findings"], 1)
            self.assertEqual(closure["remaining_p0"], 0)
            self.assertEqual(closure["remaining_p1"], 0)
            self.assertEqual(closure["status_counts"]["fixed"], 1)
            self.assertEqual(closure["severity_status_counts"]["P1"]["fixed"], 1)
            self.assertTrue(closure["acceptance_passed"])
            self.assertTrue(closure["acceptance_pass"])
            self.assertFalse(summary["privacy"]["contains_paths"])
            self.assertFalse(summary["privacy"]["contains_filenames"])
            self.assertIn("Workbench summary status: pass", stdout.getvalue())

    def test_workbench_summary_promotes_review_acceptance_closure_without_private_values(self) -> None:
            forbidden_private_values = [
                "/Users/private/archive/page_0001.png",
                "page_0001.png",
                "reviewer note private",
                "finding_id_123",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "OCR TEXT",
            ]
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                review = _review_summary_bundle_payload()
                review["status_counts"]["/Users/private/archive/page_0001.png"] = 2
                review["rule_status_counts"]["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"] = {"fixed": 1}
                review["reviewer_note"] = " ".join(forbidden_private_values)
                acceptance = _acceptance_bundle_payload()
                acceptance["human_review"] = {
                    "remaining_p0": 0,
                    "remaining_p1": 0,
                    "total_findings": 1,
                    "status_counts": {"fixed": 1, "page_0001.png": 1},
                }
                _write_json(root / "review_summary.json", review)
                _write_json(root / "acceptance_summary.json", acceptance)

                summary = build_workbench_public_summary(files=[root / "review_summary.json", root / "acceptance_summary.json"])
                raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

            self.assertEqual(summary["status"], "fail")
            self.assertIn("private_value_present", summary["blocking_counts_by_code"])
            closure = summary["summary"]["human_review_closure"]
            self.assertEqual(closure["total_findings"], 1)
            self.assertEqual(closure["remaining_p0"], 0)
            self.assertEqual(closure["remaining_p1"], 0)
            self.assertEqual(closure["status_counts"], {"fixed": 1})
            self.assertEqual(closure["rule_status_counts"], {"dpi_below_minimum": {"fixed": 1}})
            self.assertTrue(closure["acceptance_passed"])
            self.assertTrue(closure["acceptance_pass"])
            for value in forbidden_private_values:
                self.assertNotIn(value, raw)

    def test_workbench_summary_promotes_readiness_without_private_provider_values(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                candidate = _deep_inspection_candidate_bundle_payload()
                candidate["provider_command"] = "python /Users/private/model.py"
                candidate["environment"] = {"SECRET_TOKEN": "PRIVATE_OCR_TEXT"}
                _write_json(root / "deep_inspection_candidate_summary.json", candidate)
                _write_json(root / "capability_probe.json", _capability_probe_bundle_payload())

                summary = build_workbench_public_summary(evidence_dir=root)
                raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

            self.assertIn("deep_inspection_readiness", summary["summary"])
            self.assertIn("provider_capability_readiness", summary["summary"])
            self.assertEqual(summary["summary"]["deep_inspection_readiness"]["candidate_total"], 3)
            self.assertNotIn("SECRET_TOKEN", raw)
            self.assertNotIn("PRIVATE_OCR_TEXT", raw)
            self.assertNotIn("/Users/private/model.py", raw)

    def test_workbench_summary_propagates_processing_reuse_counters(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(
                    root / "aggregate_baseline_summary.json",
                    {
                        "schema_version": "scan-qc.aggregate-baseline.v1",
                        "privacy": {"aggregate_only": True},
                        "aggregate_counts": {
                            "total_files": 8,
                            "processing_resumed_files": 2,
                            "processing_duplicate_reused_files": 3,
                            "processing_existing_derivative_reused_files": 4,
                        },
                    },
                )

                summary = build_workbench_public_summary(
                    evidence_dir=root,
                    generated_at="2026-01-01T00:00:00+00:00",
                )

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["summary"]["processing_resumed_files"], 2)
            self.assertEqual(summary["summary"]["processing_duplicate_reused_files"], 3)
            self.assertEqual(summary["summary"]["processing_existing_derivative_reused_files"], 4)
            metrics = summary["artifacts"]["aggregate_baseline_summary.json"]["metrics"]
            self.assertEqual(metrics["processing_resumed_files"], 2)
            self.assertEqual(metrics["processing_duplicate_reused_files"], 3)
            self.assertEqual(metrics["processing_existing_derivative_reused_files"], 4)

    def test_workbench_summary_omits_missing_processing_reuse_counters(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(
                    root / "run_plan_summary.json",
                    {
                        "schema_version": "scan-qc.run-plan-summary.v1",
                        "privacy": {"aggregate_only": True},
                        "summary": {"total_batches": 1, "failed_batches": 0},
                        "batches": [],
                    },
                )

                summary = build_workbench_public_summary(
                    evidence_dir=root,
                    generated_at="2026-01-01T00:00:00+00:00",
                )

            self.assertEqual(summary["status"], "pass")
            self.assertNotIn("processing_resumed_files", summary["summary"])
            self.assertNotIn("processing_duplicate_reused_files", summary["summary"])
            self.assertNotIn("processing_existing_derivative_reused_files", summary["summary"])

    def test_workbench_summary_propagates_processing_operation_timings(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(
                    root / "run_plan_summary.json",
                    {
                        "schema_version": "scan-qc.run-plan-summary.v1",
                        "privacy": {"aggregate_only": True},
                        "summary": {
                            "total_batches": 1,
                            "failed_batches": 0,
                            "processing_operation_timings": {
                                "auto_crop": {
                                    "enabled": True,
                                    "file_count": 7,
                                    "elapsed_seconds": 0.5,
                                    "files_per_minute": 840.0,
                                    "average_seconds_per_file": 0.071429,
                                },
                                "deskew": {
                                    "enabled": True,
                                    "file_count": 7,
                                    "elapsed_seconds": 3.5,
                                    "files_per_minute": 120.0,
                                    "average_seconds_per_file": 0.5,
                                    "reused_scan_measurement_files": 5,
                                    "safe_skip_files": 3,
                                    "projection_detection_files": 4,
                                    "fallback_detection_files": 1,
                                },
                                "trim_dark_border": {
                                    "enabled": False,
                                    "file_count": 0,
                                    "elapsed_seconds": 0.0,
                                    "files_per_minute": 0.0,
                                },
                                "despeckle": {
                                    "enabled": True,
                                    "file_count": 7,
                                    "elapsed_seconds": 1.25,
                                    "files_per_minute": 336.0,
                                    "average_seconds_per_file": 0.178571,
                                    "backend_mode": "numpy",
                                    "numpy_available": True,
                                    "backend_counts": {
                                        "numpy": 7,
                                        "fallback": 0,
                                        "not_applicable": 0,
                                        "unknown": 0,
                                    },
                                }
                            },
                        },
                        "batches": [],
                    },
                )

                summary = build_workbench_public_summary(
                    evidence_dir=root,
                    generated_at="2026-01-01T00:00:00+00:00",
                )

            self.assertEqual(summary["status"], "pass")
            timings = summary["summary"]["processing_operation_timings"]
            self.assertEqual(timings["auto_crop"]["file_count"], 7)
            self.assertEqual(timings["auto_crop"]["elapsed_seconds"], 0.5)
            self.assertEqual(timings["deskew"]["elapsed_seconds"], 3.5)
            self.assertEqual(timings["deskew"]["reused_scan_measurement_files"], 5)
            self.assertEqual(timings["deskew"]["safe_skip_files"], 3)
            self.assertEqual(timings["deskew"]["projection_detection_files"], 4)
            self.assertEqual(timings["deskew"]["fallback_detection_files"], 1)
            self.assertEqual(timings["trim_dark_border"]["enabled"], False)
            despeckle = timings["despeckle"]
            self.assertEqual(despeckle["enabled"], True)
            self.assertEqual(despeckle["file_count"], 7)
            self.assertEqual(despeckle["elapsed_seconds"], 1.25)
            self.assertEqual(despeckle["files_per_minute"], 336.0)
            self.assertEqual(despeckle["average_seconds_per_file"], 0.178571)
            self.assertEqual(despeckle["backend_mode"], "numpy")
            self.assertTrue(despeckle["numpy_available"])
            self.assertEqual(despeckle["backend_counts"]["numpy"], 7)
            self.assertEqual(despeckle["backend_counts"]["fallback"], 0)
            metrics = summary["artifacts"]["run_plan_summary.json"]["metrics"]
            self.assertEqual(metrics["processing_operation_timings"], timings)

    def test_workbench_summary_missing_despeckle_backend_metadata_keeps_aggregate_timing_non_blocking(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(
                    root / "run_plan_summary.json",
                    {
                        "schema_version": "scan-qc.run-plan-summary.v1",
                        "privacy": {"aggregate_only": True},
                        "summary": {
                            "total_batches": 1,
                            "failed_batches": 0,
                            "processing_operation_timings": {
                                "despeckle": {"enabled": True, "file_count": 7, "elapsed_seconds": 1.25}
                            },
                        },
                        "batches": [],
                    },
                )

                summary = build_workbench_public_summary(
                    evidence_dir=root,
                    generated_at="2026-01-01T00:00:00+00:00",
                )

            self.assertEqual(summary["status"], "pass")
            despeckle = summary["summary"]["processing_operation_timings"]["despeckle"]
            self.assertEqual(despeckle["enabled"], True)
            self.assertEqual(despeckle["file_count"], 7)
            self.assertEqual(despeckle["elapsed_seconds"], 1.25)
            self.assertNotIn("backend_mode", despeckle)

    def test_workbench_summary_blocks_private_despeckle_backend_values(self) -> None:
            private_value = "/Users/private/archive/page_0001.png"
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(
                    root / "run_plan_summary.json",
                    {
                        "schema_version": "scan-qc.run-plan-summary.v1",
                        "privacy": {"aggregate_only": True},
                        "summary": {
                            "total_batches": 1,
                            "failed_batches": 0,
                            "processing_operation_timings": {
                                "despeckle": {
                                    "backend_mode": private_value,
                                    "numpy_available": True,
                                    "backend_counts": {"numpy": 7},
                                }
                            },
                        },
                        "batches": [],
                    },
                )

                summary = build_workbench_public_summary(
                    files=[root / "run_plan_summary.json"],
                    generated_at="2026-01-01T00:00:00+00:00",
                )
                raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

            self.assertEqual(summary["status"], "fail")
            self.assertIn("private_value_present", summary["blocking_counts_by_code"])
            self.assertNotIn(private_value, raw)
            despeckle = summary["summary"]["processing_operation_timings"]["despeckle"]
            self.assertNotIn("backend_mode", despeckle)
            self.assertTrue(despeckle["numpy_available"])
            self.assertEqual(despeckle["backend_counts"]["numpy"], 7)

    def test_workbench_summary_reuse_counters_remain_aggregate_only(self) -> None:
            forbidden_private_values = [
                "/Users/private/archive/page_0001.png",
                "page_0001.png",
                "processing_manifest.json",
                "OCR TEXT",
                "thumbnail-preview-object",
                "data:image/png",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "provider --private /Users/private/archive",
                "raw_model_output: private answer",
                "derivative/page_0001.png",
            ]
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(
                    root / "run_plan_summary.json",
                    {
                        "schema_version": "scan-qc.run-plan-summary.v1",
                        "privacy": {"aggregate_only": True},
                        "summary": {
                            "total_batches": 1,
                            "failed_batches": 0,
                            "processing_resumed_files": 0,
                            "processing_duplicate_reused_files": 1,
                            "processing_existing_derivative_reused_files": 2,
                        },
                        "operator_warning": " ".join(forbidden_private_values),
                    },
                )

                summary = build_workbench_public_summary(
                    files=[root / "run_plan_summary.json"],
                    generated_at="2026-01-01T00:00:00+00:00",
                )
                raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

            self.assertEqual(summary["status"], "fail")
            self.assertEqual(summary["summary"]["processing_resumed_files"], 0)
            self.assertEqual(summary["summary"]["processing_duplicate_reused_files"], 1)
            self.assertEqual(summary["summary"]["processing_existing_derivative_reused_files"], 2)
            self.assertIn("private_value_present", summary["blocking_counts_by_code"])
            for value in forbidden_private_values:
                self.assertNotIn(value, raw)

    def test_workbench_summary_blocks_aggregate_failures_by_code_only(self) -> None:
            forbidden_private_values = [
                "/Users/private/archive",
                "page_0001.png",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "OCR TEXT",
                "SECRET123",
                "processing_manifest.json",
            ]
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_public_safe_validation_index_fixtures(root)
                failing = _release_candidate_bundle_payload()
                failing["status"] = "fail"
                failing["ready_for_release_candidate"] = False
                failing["blocking_items"] = [{"artifact": "acceptance_summary.json", "code": "acceptance_blocked"}]
                failing["operator_warning"] = " ".join(forbidden_private_values)
                _write_json(root / "release_candidate_summary.json", failing)

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(["workbench-summary", "--evidence-dir", str(root), "--out", str(root / "workbench.json")])
                raw = (root / "workbench.json").read_text(encoding="utf-8")
                summary = json.loads(raw)

            self.assertEqual(exit_code, 1)
            self.assertEqual(summary["status"], "fail")
            self.assertFalse(summary["ready"])
            self.assertGreater(summary["checks_failed"], 0)
            self.assertEqual(summary["blocking_counts_by_code"]["artifact_status_failed"], 1)
            self.assertEqual(summary["blocking_counts_by_code"]["acceptance_blocked"], 1)
            self.assertIn("private_value_present", summary["blocking_counts_by_code"])
            for value in forbidden_private_values:
                self.assertNotIn(value, raw)
            self.assertIn("Workbench summary status: fail", stdout.getvalue())

    def test_workbench_summary_rejects_explicit_private_inputs_without_reading(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                private_report = root / "scan_qc_report.json"
                private_report.write_text("/Users/private/archive/page_0001.png SECRET123", encoding="utf-8")
                _write_json(root / "final_production_handoff_summary.json", _final_handoff_bundle_payload())

                summary = build_workbench_public_summary(
                    files=[root / "final_production_handoff_summary.json", private_report],
                    generated_at="2026-01-01T00:00:00+00:00",
                )
                raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

            self.assertEqual(summary["status"], "fail")
            self.assertEqual(summary["privacy"]["unsupported_private_input_count"], 1)
            self.assertIn("unsupported_private_input_rejected", summary["blocking_counts_by_code"])
            self.assertEqual(summary["artifact_presence"]["final_production_handoff_summary.json"]["status"], "pass")
            self.assertNotIn("scan_qc_report.json", raw)
            self.assertNotIn("/Users/private/archive/page_0001.png", raw)
            self.assertNotIn("SECRET123", raw)

    def test_workbench_summary_accepts_explicit_public_summary_input(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(root / "workbench_public_summary.json", _workbench_public_summary_payload())

                summary = build_workbench_public_summary(
                    files=[root / "workbench_public_summary.json"],
                    generated_at="2026-01-01T00:00:00+00:00",
                )

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["summary"]["unsupported_inputs"], 0)
            self.assertNotIn("unsupported_aggregate_input_rejected", summary["blocking_counts_by_code"])
            presence = summary["artifact_presence"]["workbench_public_summary.json"]
            self.assertTrue(presence["present"])
            self.assertEqual(presence["status"], "pass")
            self.assertEqual(presence["reported_status"], "pass")
            self.assertTrue(presence["ready"])
            metrics = summary["artifacts"]["workbench_public_summary.json"]["metrics"]
            self.assertEqual(metrics["known_artifacts"], 17)
            self.assertEqual(metrics["artifacts_present"], 16)
            self.assertEqual(metrics["artifacts_failed"], 0)

    def test_workbench_summary_directory_mode_recognizes_public_summary_input(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(root / "workbench_public_summary.json", _workbench_public_summary_payload())

                summary = build_workbench_public_summary(
                    evidence_dir=root,
                    generated_at="2026-01-01T00:00:00+00:00",
                )

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["summary"]["unsupported_inputs"], 0)
            presence = summary["artifact_presence"]["workbench_public_summary.json"]
            self.assertTrue(presence["present"])
            self.assertEqual(presence["status"], "pass")
            metrics = summary["artifacts"]["workbench_public_summary.json"]["metrics"]
            self.assertEqual(metrics["artifacts_passed"], 16)

    def test_workbench_summary_directory_mode_ignores_unknown_private_files(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_json(root / "final_production_handoff_summary.json", _final_handoff_bundle_payload())
                (root / "scan_qc_report.json").write_text("/Users/private/archive/page_0001.png SECRET123", encoding="utf-8")

                summary = build_workbench_public_summary(evidence_dir=root, generated_at="2026-01-01T00:00:00+00:00")
                raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["privacy"]["unsupported_private_input_count"], 0)
            self.assertNotIn("scan_qc_report.json", summary["blocking_counts_by_code"])
            self.assertNotIn("/Users/private/archive/page_0001.png", raw)
            self.assertNotIn("SECRET123", raw)

    def test_workbench_summary_is_deterministic_for_same_inputs(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_public_safe_validation_index_fixtures(root)
                first = build_workbench_public_summary(evidence_dir=root)
                second = build_workbench_public_summary(evidence_dir=root)

            self.assertEqual(
                json.dumps(first, ensure_ascii=False, sort_keys=True),
                json.dumps(second, ensure_ascii=False, sort_keys=True),
            )
            self.assertIsNone(first["generated_at"])


if __name__ == "__main__":
    unittest.main()
