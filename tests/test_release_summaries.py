from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_READINESS_PATH = REPO_ROOT / "scripts" / "release_readiness_summary.py"
RELEASE_CANDIDATE_PATH = REPO_ROOT / "scripts" / "release_candidate_summary.py"


def _load_script_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_release_readiness_module():
    return _load_script_module("release_readiness_summary", RELEASE_READINESS_PATH)


def _load_release_candidate_module():
    return _load_script_module("release_candidate_summary", RELEASE_CANDIDATE_PATH)


def _release_candidate_baseline(*, processing_failed_files: int = 0) -> dict[str, object]:
    return {
        "schema_version": "scan-qc.aggregate-baseline.v1",
        "privacy": {"aggregate_only": True},
        "aggregate_counts": {
            "total_files": 20,
            "openable_files": 20,
            "total_findings": 2,
            "p0_findings": 0,
            "p1_findings": 0,
            "p2_findings": 2,
            "processing_processed_files": 20 - processing_failed_files,
            "processing_failed_files": processing_failed_files,
            "failed_batches": 0,
            "preflight_errors": 0,
        },
        "stage_timings": {
            "scan": {"files_per_minute": 120.0, "openable_files_per_minute": 120.0},
            "processing": {"processed_files_per_minute": 60.0},
        },
        "environment": {"python_version": "3.12.1", "pillow_version": "10.4.0"},
        "runtime_hardware": {
            "python_version_family": "3.12",
            "cpu_logical_count": 8,
            "gpu_visible_count": 1,
            "gpu_acceleration_used": False,
            "warnings": ["nvidia-smi unavailable"],
        },
        "cleanup": {
            "enabled": True,
            "removed_artifacts": ["scan-reports"],
            "preserved_artifacts": [],
            "retained_public_summary": "aggregate_baseline_summary.json",
        },
        "privacy_self_check": {"passed": True, "status": "pass", "violation_count": 0},
    }


def _release_candidate_acceptance(*, status: str) -> dict[str, object]:
    passed = status == "pass"
    return {
        "schema_version": "scan-qc.acceptance-summary.v1",
        "status": status,
        "pass": passed,
        "privacy": {"aggregate_only": True},
        "thresholds": {
            "remaining_p0_max": 0,
            "remaining_p1_max": 0,
            "processing_failed_files_max": 0,
            "min_scan_files_per_minute": 100.0,
            "min_processing_files_per_minute": 50.0,
        },
        "blocking_items": [],
        "throughput": {
            "scan_files_per_minute": {"provided": True, "best_observed": 120.0, "lowest_observed": 120.0},
            "processing_files_per_minute": {"provided": True, "best_observed": 60.0, "lowest_observed": 60.0},
        },
        "privacy_self_check": {"provided": True, "passed": True, "status": "pass", "violation_count": 0},
        "cleanup": {
            "provided": True,
            "enabled": True,
            "retained_public_summary_only": True,
            "removed_artifact_count": 1,
            "preserved_artifact_count": 0,
            "retained_public_summary": "aggregate_baseline_summary.json",
        },
    }


def _release_candidate_readiness(*, status: str) -> dict[str, object]:
    failed = 0 if status == "pass" else 1
    return {
        "schema_version": "scan-qc.release-readiness.v1",
        "status": status,
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
        "summary": {
            "checks_total": 4,
            "checks_passed": 4 - failed,
            "checks_failed": failed,
            "checks_warning": 0,
            "checks_skipped": 0,
            "blocking_items": failed,
        },
        "checks": {"unit_tests": {"status": status, "blocking": status == "fail", "evidence_count": 1}},
        "capability_probe": {
            "available": True,
            "status": "pass",
            "blocking": False,
            "provider_packages_found_count": 1,
            "gpu_visible_count": 1,
            "gpu_acceleration_configured": True,
            "model_acceleration_configured": False,
        },
        "scan_processing_semantics": "unchanged_cpu_pillow_baseline",
        "network_services_called": False,
        "model_inference_run": False,
    }


class ReleaseSummaryTests(unittest.TestCase):
    def test_release_readiness_summary_passes_with_aggregate_evidence(self) -> None:
        module = _load_release_readiness_module()
        checks = [
            module.ReadinessCheck("unit_tests", "pass", False),
            module.ReadinessCheck("compile_import_check", "pass", False),
            module.ReadinessCheck("offline_dependency_check", "pass", False),
            module.ReadinessCheck("package_cli_smoke", "pass", False),
        ]
        capability_probe = {
            "status": "pass",
            "readiness": {
                "provider_packages_found": ["torch", "onnxruntime"],
                "blocking": False,
                "gpu_acceleration_configured": True,
                "model_acceleration_configured": True,
            },
            "gpu_provider_visibility": {"gpu_visible_count": 2},
        }

        summary = module.build_release_readiness_summary(
            checks=checks,
            capability_probe=capability_probe,
            generated_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(summary["schema_version"], "scan-qc.release-readiness.v1")
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["summary"]["checks_passed"], 4)
        self.assertEqual(summary["summary"]["blocking_items"], 0)
        self.assertEqual(summary["capability_probe"]["provider_packages_found_count"], 2)
        self.assertEqual(summary["capability_probe"]["gpu_visible_count"], 2)
        self.assertTrue(summary["privacy"]["aggregate_only"])
        self.assertFalse(summary["privacy"]["contains_paths"])
        self.assertFalse(summary["network_services_called"])
        self.assertFalse(summary["model_inference_run"])

    def test_release_readiness_summary_fails_with_blocking_counts_only(self) -> None:
        module = _load_release_readiness_module()
        checks = [
            module.ReadinessCheck("unit_tests", "fail", True),
            module.ReadinessCheck("compile_import_check", "pass", False),
            module.ReadinessCheck("offline_dependency_check", "fail", True),
            module.ReadinessCheck("package_cli_smoke", "pass", False),
        ]

        summary = module.build_release_readiness_summary(checks=checks, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["summary"]["checks_failed"], 2)
        self.assertEqual(summary["summary"]["blocking_items"], 2)
        self.assertEqual(summary["checks"]["unit_tests"]["status"], "fail")
        self.assertEqual(summary["capability_probe"]["status"], "skipped")

    def test_release_readiness_command_output_omits_sensitive_values(self) -> None:
        module = _load_release_readiness_module()
        sensitive_stdout = (
            "/private/archive/A001_0001.png "
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef "
            "OCR TEXT secret-token"
        )

        def runner(command, **kwargs):
            return mock.Mock(returncode=0, stdout=sensitive_stdout, stderr=sensitive_stdout)

        with tempfile.TemporaryDirectory(prefix="private-readiness-") as temp_dir, mock.patch.object(
            module, "_compile_check", return_value=module.ReadinessCheck("compile_import_check", "pass", False)
        ):
            private_wheelhouse = Path(temp_dir) / "operator" / "wheelhouse"
            private_probe = Path(temp_dir) / "local" / "capability_probe.json"
            private_probe.parent.mkdir()
            private_probe.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "readiness": {
                            "provider_packages_found": ["torch"],
                            "blocking": False,
                            "gpu_acceleration_configured": True,
                            "model_acceleration_configured": False,
                            "private_path": "/private/model/path",
                        },
                        "gpu_provider_visibility": {"gpu_visible_count": 1},
                    }
                ),
                encoding="utf-8",
            )
            summary = module.run_release_readiness_checks(
                wheelhouse_path=private_wheelhouse,
                capability_probe_path=private_probe,
                command_runner=runner,
            )
            out_path = module.write_release_readiness_summary(summary, Path(temp_dir) / "out")
            raw = out_path.read_text(encoding="utf-8")

        self.assertEqual(summary["status"], "pass")
        self.assertNotIn("/private/archive", raw)
        self.assertNotIn("A001_0001.png", raw)
        self.assertNotIn("0123456789abcdef", raw)
        self.assertNotIn("OCR TEXT", raw)
        self.assertNotIn("secret-token", raw)
        self.assertNotIn("/private/model/path", raw)
        self.assertNotIn("private-readiness", raw)
        self.assertNotIn("wheelhouse", raw)

    def test_release_candidate_summary_passes_with_aggregate_evidence(self) -> None:
        module = _load_release_candidate_module()

        summary = module.build_release_candidate_summary(
            aggregate_baseline_summary=_release_candidate_baseline(),
            acceptance_summary=_release_candidate_acceptance(status="pass"),
            release_readiness_summary=_release_candidate_readiness(status="pass"),
            cleanup_requested=True,
            generated_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(summary["schema_version"], "scan-qc.release-candidate-summary.v1")
        self.assertEqual(summary["status"], "pass")
        self.assertTrue(summary["ready_for_release_candidate"])
        self.assertEqual(summary["production_validation"]["counts"]["processing_failed_files"], 0)
        self.assertEqual(summary["production_validation"]["counts"]["severity_counts"]["p2"], 2)
        self.assertEqual(summary["production_validation"]["throughput"]["scan_files_per_minute"], 120.0)
        self.assertTrue(summary["production_validation"]["threshold_outcomes"]["scan_throughput_passed"])
        self.assertTrue(summary["production_validation"]["privacy"]["self_check_passed"])
        self.assertTrue(summary["production_validation"]["cleanup"]["requested"])
        self.assertTrue(summary["production_validation"]["cleanup"]["retained_public_summary_only"])
        self.assertEqual(summary["release_readiness"]["capability_probe"]["provider_packages_found_count"], 1)
        self.assertEqual(summary["release_readiness"]["capability_probe"]["gpu_visible_count"], 1)
        self.assertEqual(summary["scan_processing_semantics"], "unchanged_cpu_pillow_baseline")
        self.assertFalse(summary["network_services_called"])
        self.assertFalse(summary["model_inference_run"])

    def test_release_candidate_summary_fails_on_production_or_readiness_blockers(self) -> None:
        module = _load_release_candidate_module()
        acceptance = _release_candidate_acceptance(status="fail")
        acceptance["blocking_items"] = [
            {"code": "processing_failed_files", "message": "PRIVATE_CASE_001.png failed", "observed": 1, "threshold": 0}
        ]
        readiness = _release_candidate_readiness(status="fail")
        readiness["summary"]["blocking_items"] = 2
        readiness["checks"]["unit_tests"] = {"status": "fail", "blocking": True, "evidence_count": 1}

        summary = module.build_release_candidate_summary(
            aggregate_baseline_summary=_release_candidate_baseline(processing_failed_files=1),
            acceptance_summary=acceptance,
            release_readiness_summary=readiness,
            cleanup_requested=True,
            generated_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(summary["status"], "fail")
        self.assertFalse(summary["ready_for_release_candidate"])
        self.assertEqual(summary["decision"]["production_blocking_item_count"], 1)
        self.assertEqual(summary["decision"]["release_readiness_blocking_item_count"], 2)
        self.assertEqual(summary["decision"]["blocking_item_count"], 3)
        self.assertNotIn("PRIVATE_CASE_001.png", json.dumps(summary))

    def test_release_candidate_summary_carries_sampling_gate_aggregate_only(self) -> None:
        module = _load_release_candidate_module()
        acceptance = _release_candidate_acceptance(status="fail")
        acceptance["blocking_items"] = [
            {
                "code": "sampling_review_target_not_met",
                "message": "Reviewed acceptance sampling count must meet the configured target ratio.",
                "observed": {"reviewed_sample_count": 3, "target_sample_count": 5},
                "threshold": {"reviewed_sample_count_min": 5},
            }
        ]
        acceptance["acceptance_sampling"] = {
            "provided": True,
            "status": "fail",
            "target_sample_ratio": 0.05,
            "target_sample_count": 5,
            "generated_sample_task_count": 5,
            "reviewed_sample_count": 3,
            "pending_sample_count": 2,
            "sample_task_target_met": True,
            "sampling_target_met": False,
            "admin_message_zh": "抽检比例未达标：目标 5 项，已生成 5 项，已复核 3 项。",
            "private_row": "/private/archive/page_0001.tif",
        }

        summary = module.build_release_candidate_summary(
            aggregate_baseline_summary=_release_candidate_baseline(),
            acceptance_summary=acceptance,
            release_readiness_summary=_release_candidate_readiness(status="pass"),
            cleanup_requested=True,
            generated_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(summary["status"], "fail")
        self.assertFalse(summary["ready_for_release_candidate"])
        sampling = summary["production_validation"]["acceptance_sampling"]
        self.assertEqual(sampling["target_sample_count"], 5)
        self.assertEqual(sampling["reviewed_sample_count"], 3)
        self.assertFalse(sampling["sampling_target_met"])
        self.assertFalse(summary["production_validation"]["threshold_outcomes"]["sampling_review_target_passed"])
        raw = json.dumps(summary, ensure_ascii=False)
        self.assertIn("抽检比例未达标", raw)
        self.assertNotIn("/private/archive", raw)
        self.assertNotIn("page_0001.tif", raw)

    def test_release_candidate_summary_explains_acceptance_blockers_in_chinese(self) -> None:
        module = _load_release_candidate_module()
        acceptance = _release_candidate_acceptance(status="fail")
        acceptance["closure_gate_summary"] = {
            "open_p0_count": 1,
            "open_p1_count": 2,
            "manually_handled_count": 4,
            "can_complete_delivery": False,
        }
        acceptance["acceptance_sampling"] = {
            "provided": True,
            "status": "fail",
            "target_sample_ratio": 0.05,
            "target_sample_count": 5,
            "generated_sample_task_count": 3,
            "reviewed_sample_count": 2,
            "pending_sample_count": 3,
            "sample_task_target_met": False,
            "sampling_target_met": False,
            "admin_message_zh": "抽检比例未达标：目标 5 项，已生成 3 项，已复核 2 项。",
        }
        acceptance["blocking_items"] = [
            {"code": "remaining_p0"},
            {"code": "remaining_p1"},
            {"code": "sample_task_target_not_met"},
            {"code": "sampling_review_target_not_met"},
        ]

        summary = module.build_release_candidate_summary(
            aggregate_baseline_summary=_release_candidate_baseline(),
            acceptance_summary=acceptance,
            release_readiness_summary=_release_candidate_readiness(status="pass"),
            cleanup_requested=True,
            generated_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(summary["handoff_status_zh"], "不可交接")
        digest = summary["acceptance_blocker_summary_zh"]
        self.assertFalse(digest["can_handoff"])
        self.assertIn("P0/P1 未关闭", digest["summary_zh"])
        self.assertIn("抽检任务未达到目标比例", digest["summary_zh"])
        self.assertIn("抽检复核未达到目标比例", digest["summary_zh"])
        self.assertEqual(digest["closure_gate_summary"]["open_p0_count"], 1)
        self.assertEqual(digest["acceptance_sampling"]["reviewed_sample_count"], 2)
        self.assertIn("closure_gate_summary", digest["reused_aggregate_fields"])
        self.assertIn("acceptance_sampling", digest["reused_aggregate_fields"])
        self.assertIn("warning_items", digest["reused_aggregate_fields"])

    def test_release_candidate_summary_carries_cleanup_quality_warning_digest_from_acceptance(self) -> None:
        module = _load_release_candidate_module()
        acceptance = _release_candidate_acceptance(status="pass")
        acceptance["warning_items"] = [
            {
                "code": "full_chain_cleanup_low_improved_ratio",
                "title_zh": "清理改善比例偏低",
                "message_zh": "全链路清理在聚合结果中的改善比例偏低，请在导出前复核清理参数与抽样结果。",
                "next_step_zh": "检查清理参数并抽检代表性处理结果；如偏差持续，请补充聚合证据后重跑验收。",
                "observed": {"ratios": {"improved_ratio": 0.35}},
            }
        ]
        summary = module.build_release_candidate_summary(
            aggregate_baseline_summary=_release_candidate_baseline(),
            acceptance_summary=acceptance,
            release_readiness_summary=_release_candidate_readiness(status="pass"),
            cleanup_requested=True,
            generated_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(summary["warning_item_count"], 1)
        self.assertEqual(summary["warning_items"][0]["code"], "full_chain_cleanup_low_improved_ratio")
        self.assertIn("清理改善比例偏低", summary["warning_items"][0]["title_zh"])
        raw = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("\"ratios\"", raw)
        self.assertNotIn("0.35", raw)

    def test_release_candidate_command_omits_sensitive_values_and_records_cleanup_intent(self) -> None:
        module = _load_release_candidate_module()
        with tempfile.TemporaryDirectory(prefix="private-rc-") as temp_dir:
            root = Path(temp_dir)
            baseline = _release_candidate_baseline()
            baseline["private_path"] = str(root / "A001_0001.png")
            acceptance = _release_candidate_acceptance(status="pass")
            acceptance["warnings"] = ["local review used /private/validation-host/tmp"]
            readiness = _release_candidate_readiness(status="pass")
            readiness["checks"]["unit_tests"]["stdout"] = "OCR TEXT secret-token"
            (root / "aggregate_baseline_summary.json").write_text(json.dumps(baseline), encoding="utf-8")
            (root / "acceptance_summary.json").write_text(json.dumps(acceptance), encoding="utf-8")
            (root / "release_readiness_summary.json").write_text(json.dumps(readiness), encoding="utf-8")

            exit_code = module.main(["--out", str(root), "--no-cleanup-artifacts"])
            raw = (root / "release_candidate_summary.json").read_text(encoding="utf-8")
            payload = json.loads(raw)

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["production_validation"]["cleanup"]["requested"])
        self.assertTrue(payload["production_validation"]["cleanup"]["enabled"])
        for forbidden in [
            "private-rc-",
            "A001_0001.png",
            "/private/validation-host/tmp",
            "OCR TEXT",
            "secret-token",
            "private_path",
            "stdout",
        ]:
            self.assertNotIn(forbidden, raw)


if __name__ == "__main__":
    unittest.main()
