from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

from archive_scan_qc import __version__
from archive_scan_qc import processing as processing_module
from archive_scan_qc.acceptance import build_acceptance_summary
from archive_scan_qc.artifact_readiness import build_artifact_readiness_checklist
from archive_scan_qc.benchmark import _comparison_plan, _recommendations
from archive_scan_qc.capability_probe import CapabilityProbeConfig, run_capability_probe
from archive_scan_qc.cli import main
from archive_scan_qc.deep_inspection_provider import (
    DeepInspectionProviderConfigError,
    build_deep_inspection_provider_probe,
    parse_deep_inspection_provider_config,
)
from archive_scan_qc.deep_inspection_candidates import build_deep_inspection_candidate_summary
from archive_scan_qc.evidence_bundle import build_evidence_bundle_summary
from archive_scan_qc.final_handoff import build_final_handoff_summary
from archive_scan_qc.handoff import write_delivery_handoff_manifest
from archive_scan_qc import local_workbench as local_workbench_module
from archive_scan_qc.local_workbench import DEFAULT_METADATA_DIRNAME, DEFAULT_PROCESSING_MODE, WorkbenchController, make_server
from archive_scan_qc.processing import (
    ProcessingOptions,
    _despeckle_candidate_points,
    _despeckle_candidate_points_fallback,
    _despeckle_candidate_points_numpy,
    _despeckle_isolated_pixels,
    _deskew_candidate_scores,
    _horizontal_projection_variance,
    detect_dark_border_bbox,
    process_images,
)
from archive_scan_qc.processing_plan import build_processing_plan
from archive_scan_qc.processing_review import build_processing_review_package
from archive_scan_qc.production_rehearsal import ProductionRehearsalConfig, run_production_rehearsal
from archive_scan_qc.production_runner import (
    ProductionRunConfig,
    _write_progress,
    build_production_run_summary,
    run_production_folder,
)
from archive_scan_qc.reports import build_review_summary, write_reports, write_review_export, write_review_summary
from archive_scan_qc.review_decisions import build_review_decision_verification_summary
from archive_scan_qc.rework import build_rework_action_list, write_rework_action_list
from archive_scan_qc.rule_registry import RULE_REGISTRY, validate_provider_rule_id
from archive_scan_qc.rules import RulesProfileError, load_rules_profile
from archive_scan_qc.sampling import build_acceptance_sampling_export
from archive_scan_qc.scanner import ScanConfig, scan_batch
from archive_scan_qc.validation_index import build_public_safe_validation_index
from archive_scan_qc.workbench_summary import build_workbench_public_summary
from archive_scan_qc.analysis_provider import _prepare_command


REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_INTEGRATION_PATH = REPO_ROOT / "scripts" / "run_private_integration.py"
AGGREGATE_BASELINE_PATH = REPO_ROOT / "scripts" / "run_aggregate_baseline.py"
PRODUCTION_VALIDATION_PATH = REPO_ROOT / "scripts" / "run_production_validation.py"
OFFLINE_DEPENDENCY_CHECK_PATH = REPO_ROOT / "scripts" / "check_offline_dependencies.py"
RELEASE_READINESS_PATH = REPO_ROOT / "scripts" / "release_readiness_summary.py"
RELEASE_CANDIDATE_PATH = REPO_ROOT / "scripts" / "release_candidate_summary.py"
FRONTEND_ISSUE_DRIVER_PATH = REPO_ROOT / "scripts" / "frontend_issue_driver.py"
LOCAL_PROVIDER_EXAMPLE = REPO_ROOT / "examples" / "local_analysis_provider.py"
ISSUE_PLAN_PATH = REPO_ROOT / "scripts" / "generate_issue_plan.py"
SYNTHETIC_PERFORMANCE_PATH = REPO_ROOT / "scripts" / "run_synthetic_performance_comparison.py"


def _load_private_integration_module():
    spec = importlib.util.spec_from_file_location("run_private_integration", PRIVATE_INTEGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load run_private_integration.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_aggregate_baseline_module():
    spec = importlib.util.spec_from_file_location("run_aggregate_baseline", AGGREGATE_BASELINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load run_aggregate_baseline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_production_validation_module():
    spec = importlib.util.spec_from_file_location("run_production_validation", PRODUCTION_VALIDATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load run_production_validation.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_offline_dependency_check_module():
    spec = importlib.util.spec_from_file_location("check_offline_dependencies", OFFLINE_DEPENDENCY_CHECK_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load check_offline_dependencies.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_release_readiness_module():
    spec = importlib.util.spec_from_file_location("release_readiness_summary", RELEASE_READINESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load release_readiness_summary.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_release_candidate_module():
    spec = importlib.util.spec_from_file_location("release_candidate_summary", RELEASE_CANDIDATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load release_candidate_summary.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_issue_plan_module():
    spec = importlib.util.spec_from_file_location("generate_issue_plan", ISSUE_PLAN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load generate_issue_plan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_synthetic_performance_module():
    spec = importlib.util.spec_from_file_location("run_synthetic_performance_comparison", SYNTHETIC_PERFORMANCE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load run_synthetic_performance_comparison.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_frontend_issue_driver_module():
    spec = importlib.util.spec_from_file_location("frontend_issue_driver", FRONTEND_ISSUE_DRIVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load frontend_issue_driver.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _production_summary_report(total_files: int, performance: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "summary": {
            "total_files": total_files,
            "openable_files": total_files,
            "p0_findings": 0,
            "p1_findings": 0,
            "p2_findings": 0,
            "total_findings": 0,
            "performance": performance or {},
        }
    }


def _production_processing_manifest(
    derivative_dir: Path,
    *,
    total_files: int,
    processed_files: int,
    resumed_files: int = 0,
    skipped_files: int = 0,
    failed_files: int = 0,
    retry_list_files: int = 0,
    performance: dict[str, object] | None = None,
    files: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "image_root": str(derivative_dir / "images"),
        "summary": {
            "total_files": total_files,
            "processed_files": processed_files,
            "resumed_files": resumed_files,
            "skipped_files": skipped_files,
            "failed_files": failed_files,
            "retry_list_files": retry_list_files,
            "performance": performance or {},
        },
        "files": files or [],
    }


class ScanQcTest(unittest.TestCase):
    def test_issue_plan_script_writes_one_task_issue_drafts(self) -> None:
        module = _load_issue_plan_module()
        with tempfile.TemporaryDirectory(prefix="issue-plan-") as temp_dir:
            json_path, markdown_path = module.write_issue_plan(Path(temp_dir))

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(payload["schema_version"], "scan-qc.issue-plan.v1")
        self.assertEqual(len(payload["issues"]), 7)
        self.assertIn("archive-scan-qc-retouch-design.md", payload["plan_source"])
        for issue in payload["issues"]:
            self.assertLessEqual(len(issue["scope"]), 3)
            self.assertLessEqual(len(issue["acceptance_criteria"]), 3)
            self.assertIn("puersai-hpc", json.dumps(issue, ensure_ascii=False))
            self.assertEqual(issue["test_plan"]["target_machine"], "puersai-hpc")
            self.assertEqual(issue["test_plan"]["administrator_account"], "ps")
        self.assertIn("Vectorize deskew and despeckle", markdown)

    def test_issue_plan_outputs_do_not_store_credentials_or_private_paths(self) -> None:
        module = _load_issue_plan_module()
        with tempfile.TemporaryDirectory(prefix="issue-plan-") as temp_dir:
            json_path, markdown_path = module.write_issue_plan(Path(temp_dir))
            combined = json_path.read_text(encoding="utf-8") + markdown_path.read_text(encoding="utf-8")

        self.assertNotIn("tm123", combined)
        self.assertNotIn("\\\\PUERSAI-HPC", combined)
        self.assertNotIn("/Volumes/", combined)
        self.assertFalse(module.build_issue_plan()["privacy"]["contains_credentials"])

    def test_frontend_issue_driver_loads_plan_and_rejects_duplicates(self) -> None:
        module = _load_frontend_issue_driver_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            plan_path = Path(temp_dir) / "frontend-issues.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "issues": [
                            {
                                "key": "home-page",
                                "title": "Build home page",
                                "description": "Implement the first viewport.",
                                "validation": ["python -m unittest discover -s tests"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            issues = module.load_plan(plan_path)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].key, "home-page")
            self.assertEqual(issues[0].validation, ("python -m unittest discover -s tests",))

            plan_path.write_text(
                json.dumps({"issues": [{"key": "dup", "title": "One"}, {"key": "dup", "title": "Two"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate issue key"):
                module.load_plan(plan_path)

    def test_frontend_issue_driver_creates_one_active_issue_at_a_time(self) -> None:
        module = _load_frontend_issue_driver_module()

        class FakeLinear:
            def __init__(self) -> None:
                self.created = []

            def create_issue(self, *, team_id, state_id, issue):
                self.created.append((team_id, state_id, issue.key))
                return f"linear-{issue.key}"

        plan = [
            module.PlannedIssue(key="nav", title="Navigation", description=""),
            module.PlannedIssue(key="report", title="Report page", description=""),
        ]
        linear = FakeLinear()

        state = module.create_next_issue(
            plan=plan,
            state=module.DriverState(),
            linear=linear,
            team_id="team",
            todo_state_id="todo",
        )
        unchanged = module.create_next_issue(
            plan=plan,
            state=state,
            linear=linear,
            team_id="team",
            todo_state_id="todo",
        )

        self.assertEqual(state.active_key, "nav")
        self.assertEqual(state.active_linear_id, "linear-nav")
        self.assertEqual(state.next_index, 1)
        self.assertEqual(unchanged, state)
        self.assertEqual(linear.created, [("team", "todo", "nav")])

    def test_frontend_issue_driver_complete_marks_done_after_validation(self) -> None:
        module = _load_frontend_issue_driver_module()

        class FakeLinear:
            def __init__(self) -> None:
                self.updated = []

            def update_issue_state(self, *, issue_id, state_id):
                self.updated.append((issue_id, state_id))

        plan = [module.PlannedIssue(key="nav", title="Navigation", description="", validation=("echo ok",))]
        state = module.DriverState(next_index=1, active_key="nav", active_linear_id="linear-nav")
        linear = FakeLinear()

        completed = module.complete_active_issue(
            plan=plan,
            state=state,
            linear=linear,
            done_state_id="done",
            cwd=REPO_ROOT,
            dry_run=False,
        )

        self.assertIsNone(completed.active_key)
        self.assertEqual(completed.completed_keys, ("nav",))
        self.assertEqual(linear.updated, [("linear-nav", "done")])

    def test_frontend_issue_driver_status_reports_public_safe_progress_without_linear(self) -> None:
        module = _load_frontend_issue_driver_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan_path = root / "plan.json"
            state_path = root / "state.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "issues": [
                            {"key": "nav", "title": "Navigation"},
                            {"key": "report", "title": "Report"},
                            {"key": "filters", "title": "Filters"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps(
                    {
                        "next_index": 2,
                        "active_key": "report",
                        "active_linear_id": "AI4-123",
                        "completed_keys": ["nav"],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": ""}, clear=False):
                with contextlib.redirect_stdout(stdout):
                    exit_code = module.main(["status", "--plan", str(plan_path), "--state-file", str(state_path)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["next_index"], 2)
        self.assertEqual(payload["active_key"], "report")
        self.assertEqual(payload["active_linear_id"], "AI4-123")
        self.assertEqual(payload["completed_keys"], ["nav"])
        self.assertEqual(payload["total_planned"], 3)
        self.assertEqual(payload["completed_count"], 1)
        self.assertEqual(payload["remaining_count"], 1)
        self.assertEqual(payload["driver_status"], "active")

    def test_frontend_issue_driver_status_reports_idle_and_complete_modes(self) -> None:
        module = _load_frontend_issue_driver_module()
        plan = [
            module.PlannedIssue(key="nav", title="Navigation", description=""),
            module.PlannedIssue(key="report", title="Report", description=""),
        ]

        idle = module.build_status_report(plan, module.DriverState())
        complete = module.build_status_report(
            plan,
            module.DriverState(next_index=2, completed_keys=("nav", "report")),
        )

        self.assertEqual(idle["driver_status"], "idle")
        self.assertEqual(idle["remaining_count"], 2)
        self.assertEqual(complete["driver_status"], "complete")
        self.assertEqual(complete["remaining_count"], 0)

    def test_frontend_issue_driver_non_status_requires_team_key(self) -> None:
        module = _load_frontend_issue_driver_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            plan_path = Path(temp_dir) / "plan.json"
            plan_path.write_text(json.dumps({"issues": [{"key": "nav", "title": "Navigation"}]}), encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    module.main(["next", "--plan", str(plan_path), "--dry-run"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--team-key is required unless action is status", stderr.getvalue())

    def test_offline_dependency_check_passes_with_complete_wheelhouse(self) -> None:
        module = _load_offline_dependency_check_module()
        with tempfile.TemporaryDirectory(prefix="private-wheelhouse-") as temp_dir:
            wheelhouse = Path(temp_dir) / "operator" / "private" / "wheelhouse"
            wheelhouse.mkdir(parents=True)
            for name in ("ai4archive-0.1.0-py3-none-any.whl", "Pillow-10.4.0-cp312-cp312-linux_x86_64.whl", "setuptools-69.0.0-py3-none-any.whl"):
                (wheelhouse / name).touch()

            exit_code, lines = module.check_dependencies(wheelhouse=wheelhouse)

        output = "\n".join(lines)
        self.assertEqual(exit_code, 0, output)
        self.assertIn("result: pass", output)
        self.assertIn("wheelhouse-package: pillow wheels=1 status=ok", output)
        self.assertNotIn("wheelhouse-package: ai4archive", output)
        self.assertNotIn(str(wheelhouse), output)
        self.assertNotIn("operator/private", output)
        self.assertNotIn("private-wheelhouse", output)

    def test_offline_dependency_check_fails_for_missing_runtime_package(self) -> None:
        module = _load_offline_dependency_check_module()

        def missing_pillow(requirement):
            if requirement.normalized_name == "pillow":
                return None
            return "99.0.0"

        with mock.patch.object(module, "_distribution_version", side_effect=missing_pillow), mock.patch.object(
            module, "_importable", return_value=True
        ):
            exit_code, lines = module.check_dependencies()

        output = "\n".join(lines)
        self.assertEqual(exit_code, 1)
        self.assertIn("package: pillow category=runtime version=not-installed", output)
        self.assertIn("result: fail", output)

    def test_offline_dependency_check_reports_missing_wheelhouse_as_failure_or_warning(self) -> None:
        module = _load_offline_dependency_check_module()
        with tempfile.TemporaryDirectory(prefix="private-wheelhouse-") as temp_dir:
            wheelhouse = Path(temp_dir) / "wheelhouse"
            wheelhouse.mkdir()
            (wheelhouse / "Pillow-10.4.0-cp312-cp312-linux_x86_64.whl").touch()

            failure_code, failure_lines = module.check_dependencies(wheelhouse=wheelhouse)
            warning_code, warning_lines = module.check_dependencies(wheelhouse=wheelhouse, wheelhouse_warning_only=True)

        failure_output = "\n".join(failure_lines)
        warning_output = "\n".join(warning_lines)
        self.assertEqual(failure_code, 1, failure_output)
        self.assertNotIn("wheelhouse-package: ai4archive", failure_output)
        self.assertIn("wheelhouse-package: setuptools wheels=0 status=missing", failure_output)
        self.assertEqual(warning_code, 0, warning_output)
        self.assertIn("result: pass", warning_output)

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
            self.assertTrue({"private_key_present", "private_value_present", "private_absolute_path_pattern_present", "private_filename_pattern_present", "private_hash_pattern_present"} & codes)
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
        self.assertIn("required_artifact_missing", {item["code"] for item in summary["blocking_items"]})
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

    def test_final_handoff_summary_passes_with_aggregate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
            _write_json(root / "deep_inspection_candidate_summary.json", _deep_inspection_candidate_bundle_payload())
            _write_json(root / "review_decision_verification_summary.json", _review_decision_verification_bundle_payload())

            summary = build_final_handoff_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertTrue(summary["ready_for_handoff"])
        self.assertEqual(summary["blocking_item_count"], 0)
        self.assertEqual(summary["artifact_status_summary"]["aggregate_evidence_bundle_summary.json"]["status"], "pass")
        candidate = summary["artifact_status_summary"]["deep_inspection_candidate_summary.json"]
        self.assertEqual(candidate["status"], "pass")
        self.assertEqual(candidate["candidate_total"], 3)
        self.assertEqual(candidate["candidates_by_severity"]["P1"], 1)
        review_decisions = summary["artifact_status_summary"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["status"], "pass")
        self.assertEqual(review_decisions["decision_summary"]["accepted"], 1)
        self.assertEqual(review_decisions["privacy_status"], "pass")
        self.assertTrue(summary["privacy"]["aggregate_only"])

    def test_final_handoff_summary_blocks_deep_inspection_candidate_aggregate_failures_by_code_only(self) -> None:
        forbidden_private_values = [
            "/Users/private/archive",
            "page_0001.png",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "OCR TEXT",
            "thumbnail-preview-object",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = _deep_inspection_candidate_bundle_payload()
            payload["schema_version"] = "scan-qc.phase1.v1"
            payload["privacy_status"] = "failed"
            payload["no_inference_run"] = False
            payload["operator_note"] = " ".join(forbidden_private_values)
            _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
            _write_json(root / "deep_inspection_candidate_summary.json", payload)

            exit_code = main(["final-handoff-summary", "--evidence-dir", str(root), "--out", str(root / "handoff.json")])
            raw = (root / "handoff.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 1)
        self.assertFalse(summary["ready_for_handoff"])
        codes = {item["code"] for item in summary["blocking_items"] if item["artifact"] == "deep_inspection_candidate_summary.json"}
        self.assertIn("schema_version_unexpected", codes)
        self.assertIn("privacy_status_not_aggregate_public_safe", codes)
        self.assertIn("inference_run_not_allowed", codes)
        self.assertIn("private_value_present", codes)
        for value in forbidden_private_values:
            self.assertNotIn(value, raw)

    def test_final_handoff_summary_blocks_review_decision_verification_by_code_count_only(self) -> None:
        forbidden_private_values = [
            "/Users/private/archive",
            "page_0001.png",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "OCR TEXT",
            "provider command",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = _review_decision_verification_bundle_payload(blocked=True)
            payload["operator_warning"] = " ".join(forbidden_private_values)
            _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
            _write_json(root / "review_decision_verification_summary.json", payload)

            exit_code = main(["final-handoff-summary", "--evidence-dir", str(root), "--out", str(root / "handoff.json")])
            raw = (root / "handoff.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 1)
        self.assertFalse(summary["ready_for_handoff"])
        codes = {item["code"] for item in summary["blocking_items"] if item["artifact"] == "review_decision_verification_summary.json"}
        self.assertIn("aggregate_status_failed", codes)
        self.assertIn("checks_failed_present", codes)
        self.assertIn("review_decision_blocking_count_present", codes)
        self.assertIn("review_decision_privacy_not_public_safe", codes)
        review_decisions = summary["artifact_status_summary"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["blocking_counts_by_code"]["unknown_decision_value"], 1)
        self.assertEqual(review_decisions["warning_counts_by_code"]["ignored_extra_decision_field"], 2)
        for value in forbidden_private_values:
            self.assertNotIn(value, raw)

    def test_final_handoff_summary_promotes_chinese_acceptance_blocker_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release_payload = _release_candidate_bundle_payload()
            release_payload["status"] = "fail"
            release_payload["ready_for_release_candidate"] = False
            release_payload["handoff_status_zh"] = "不可交接"
            release_payload["privacy"] = {"aggregate_only": True}
            release_payload["production_validation"] = {
                "closure_gate_summary": {
                    "open_p0_count": 1,
                    "open_p1_count": 0,
                    "manually_handled_count": 2,
                    "can_complete_delivery": False,
                },
                "acceptance_sampling": {
                    "provided": True,
                    "target_sample_count": 5,
                    "generated_sample_task_count": 4,
                    "reviewed_sample_count": 3,
                    "sample_task_target_met": False,
                    "sampling_target_met": False,
                },
            }
            release_payload["acceptance_blocker_summary_zh"] = {
                "status_zh": "不可交接",
                "can_handoff": False,
                "summary_zh": "不可交接：P0/P1 未关闭：未关闭 P0 1 项，未关闭 P1 0 项。；抽检复核未达到目标比例：目标 5 项，已复核 3 项。",
                "blockers_zh": [
                    "P0/P1 未关闭：未关闭 P0 1 项，未关闭 P1 0 项。",
                    "抽检复核未达到目标比例：目标 5 项，已复核 3 项。",
                ],
                "reused_aggregate_fields": ["closure_gate_summary", "acceptance_sampling", "blocking_items"],
            }
            _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
            _write_json(root / "release_candidate_summary.json", release_payload)

            summary = build_final_handoff_summary(root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(summary["handoff_status_zh"], "不可交接")
        digest = summary["handoff_blocker_summary_zh"]
        self.assertFalse(digest["can_handoff"])
        self.assertIn("P0/P1 未关闭", digest["summary_zh"])
        self.assertIn("抽检复核未达到目标比例", digest["summary_zh"])
        self.assertEqual(digest["closure_gate_summary"]["open_p0_count"], 1)
        self.assertEqual(digest["acceptance_sampling"]["reviewed_sample_count"], 3)
        self.assertIn("release_candidate_summary", digest["reused_aggregate_fields"])
        self.assertNotIn("page_0001", raw)
        self.assertNotIn("/Users/private/archive", raw)

    def test_final_handoff_summary_passes_real_review_decision_verifier_output_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = build_review_decision_verification_summary(_review_decision_export_fixture())
            _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
            _write_json(root / "review_decision_verification_summary.json", payload)

            exit_code = main(["final-handoff-summary", "--evidence-dir", str(root), "--out", str(root / "handoff.json")])
            raw = (root / "handoff.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 0)
        self.assertTrue(summary["ready_for_handoff"])
        review_decisions = summary["artifact_status_summary"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["status"], "pass")
        self.assertEqual(review_decisions["decision_summary"]["accepted"], 1)
        self.assertEqual(review_decisions["privacy_status"], "pass")
        self.assertNotIn("scan-qc-review-decisions.local.v1", raw)
        self.assertNotIn("aggregate_handoff", raw)

    def test_final_handoff_summary_fails_for_blocking_evidence_and_cli_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = _aggregate_evidence_bundle_payload(status="fail", blocking_codes=["artifact_status_failed"])
            _write_json(root / "aggregate_evidence_bundle_summary.json", payload)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["final-handoff-summary", "--evidence-dir", str(root), "--out", str(root / "handoff.json")])
            summary = json.loads((root / "handoff.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "fail")
        self.assertFalse(summary["ready_for_handoff"])
        self.assertIn("aggregate_evidence_blocking_items_present", {item["code"] for item in summary["blocking_items"]})
        self.assertIn("Handoff status: fail", stdout.getvalue())

    def test_final_handoff_summary_blocks_missing_required_aggregate_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = build_final_handoff_summary(Path(temp_dir), generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "fail")
        self.assertIn("required_aggregate_input_missing", {item["code"] for item in summary["blocking_items"]})
        self.assertEqual(summary["artifact_status_summary"]["release_candidate_summary.json"]["status"], "optional_missing")

    def test_final_handoff_summary_blocks_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "aggregate_evidence_bundle_summary.json").write_text("{not-json", encoding="utf-8")

            summary = build_final_handoff_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "fail")
        self.assertIn("malformed_json", {item["code"] for item in summary["blocking_items"]})

    def test_final_handoff_summary_omits_private_token_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = _aggregate_evidence_bundle_payload(status="pass")
            payload["operator_warning"] = "private source /Users/private/archive/page_0001.png token SECRET123"
            _write_json(root / "aggregate_evidence_bundle_summary.json", payload)

            exit_code = main(["final-handoff-summary", "--evidence-dir", str(root), "--out", str(root / "handoff.json")])
            raw = (root / "handoff.json").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("private_value_present", raw)
        self.assertNotIn("/Users/private/archive/page_0001.png", raw)
        self.assertNotIn("SECRET123", raw)
        self.assertNotIn("page_0001.png", raw)

    def test_synthetic_final_handoff_chain_smoke_validates_go_no_go_shape(self) -> None:
        forbidden_private_values = [
            "/Users/private/archive",
            "private-root",
            "page_0001.png",
            "row_report.csv",
            "processing_manifest.json",
            "ocr text",
            "thumbnail-preview-object",
            "data:image/png",
            "blob:http://localhost/preview",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "SECRET123",
            "derivative/page_0001.png",
        ]
        blocked_release = _release_candidate_bundle_payload()
        blocked_release["status"] = "fail"
        blocked_release["ready_for_release_candidate"] = False
        blocked_release["decision"] = {"blocking_item_count": 1}

        pass_cases = [
            (
                "ready",
                _aggregate_evidence_bundle_payload(status="pass"),
                _release_candidate_bundle_payload(),
                0,
                "pass",
                True,
            ),
            (
                "blocked",
                _aggregate_evidence_bundle_payload(status="fail", blocking_codes=["artifact_status_failed"]),
                blocked_release,
                1,
                "fail",
                False,
            ),
        ]

        for case_name, evidence_payload, release_payload, expected_exit, expected_status, expected_ready in pass_cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                if expected_status == "fail":
                    evidence_payload["operator_warning"] = " ".join(forbidden_private_values)
                _write_json(root / "aggregate_evidence_bundle_summary.json", evidence_payload)
                _write_json(root / "release_candidate_summary.json", release_payload)

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(["final-handoff-summary", "--evidence-dir", str(root), "--out", str(root / "handoff.json")])
                raw = (root / "handoff.json").read_text(encoding="utf-8")
                summary = json.loads(raw)

            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(summary["status"], expected_status)
            self.assertEqual(summary["ready_for_handoff"], expected_ready)
            self.assertIsInstance(summary["checks_passed"], int)
            self.assertIsInstance(summary["checks_failed"], int)
            self.assertEqual(summary["blocking_item_count"], len(summary["blocking_items"]))
            self.assertIn("aggregate_evidence_bundle_summary.json", summary["artifact_status_summary"])
            self.assertIn("release_candidate_summary.json", summary["artifact_status_summary"])
            self.assertTrue(summary["privacy"]["source_inputs"])
            self.assertFalse(summary["privacy"]["contains_paths"])
            self.assertFalse(summary["privacy"]["contains_filenames"])
            self.assertFalse(summary["privacy"]["contains_hashes"])
            self.assertFalse(summary["privacy"]["contains_ocr_text"])
            self.assertFalse(summary["privacy"]["contains_thumbnails"])
            self.assertFalse(summary["privacy"]["contains_image_content"])
            self.assertFalse(summary["privacy"]["contains_row_level_findings"])
            self.assertIn(f"Handoff status: {expected_status}", stdout.getvalue())
            for value in forbidden_private_values:
                self.assertNotIn(value, raw)
            if expected_status == "fail":
                self.assertIn("private_value_present", {item["code"] for item in summary["blocking_items"]})

    def test_synthetic_final_handoff_chain_smoke_blocks_missing_required_input_by_code_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["final-handoff-summary", "--evidence-dir", str(root), "--out", str(root / "handoff.json")])
            summary = json.loads((root / "handoff.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "fail")
        self.assertFalse(summary["ready_for_handoff"])
        self.assertEqual(summary["blocking_item_count"], 1)
        self.assertEqual(summary["blocking_items"], [{"artifact": "aggregate_evidence_bundle_summary.json", "code": "required_aggregate_input_missing"}])
        self.assertEqual(summary["artifact_status_summary"]["aggregate_evidence_bundle_summary.json"]["status"], "missing")
        self.assertEqual(summary["artifact_status_summary"]["release_candidate_summary.json"]["status"], "pass")
        self.assertIn("Blocking items: 1", stdout.getvalue())

    def test_documented_aggregate_handoff_commands_accept_current_cli_flags(self) -> None:
        docs_and_commands = {
            REPO_ROOT / "docs" / "operations-runbook.md": (
                "review-decisions-verify",
                "evidence-bundle-verify",
                "final-handoff-summary",
            ),
            REPO_ROOT / "docs" / "release-checklist.md": (
                "review-decisions-verify",
                "evidence-bundle-verify",
                "final-handoff-summary",
            ),
            REPO_ROOT / "README.md": ("final-handoff-summary",),
        }

        with tempfile.TemporaryDirectory(prefix="docs-handoff-cli-") as temp_dir:
            root = Path(temp_dir)
            private_decisions = root / "private-review-decisions"
            validation_output = root / "private-validation-output"
            release_candidate = validation_output / "release-candidate"
            private_decisions.mkdir()
            validation_output.mkdir()
            release_candidate.mkdir()
            (private_decisions / "review_decisions.json").write_text(
                json.dumps(_review_decision_export_fixture()),
                encoding="utf-8",
            )
            for evidence_dir in (validation_output, release_candidate):
                _write_json(evidence_dir / "release_candidate_summary.json", _release_candidate_bundle_payload())
                _write_json(evidence_dir / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))

            replacements = {
                "/placeholder/private-review-decisions": str(private_decisions),
                "/placeholder/private-validation-output": str(validation_output),
            }

            for doc_path, expected_commands in docs_and_commands.items():
                with self.subTest(doc=doc_path.name):
                    commands = _documented_archive_scan_qc_commands(doc_path, expected_commands, replacements)
                    self.assertEqual([command[0] for command in commands], list(expected_commands))
                    for command in commands:
                        with contextlib.redirect_stdout(io.StringIO()):
                            exit_code = main(command)
                        self.assertEqual(exit_code, 0, command)

    def test_public_safe_validation_index_passes_known_aggregate_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["public-safe-validation-index", "--input-dir", str(root), "--out", str(root / "index.json")])
            summary = json.loads((root / "index.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["summary"]["artifacts_present"], 6)
        self.assertEqual(summary["summary"]["artifacts_failed"], 0)
        self.assertEqual(summary["artifact_presence"]["frontend_workbench_validation.json"]["status"], "pass")
        self.assertEqual(summary["artifact_presence"]["review_decision_verification_summary.json"]["status"], "pass")
        self.assertEqual(summary["artifact_presence"]["final_production_handoff_summary.json"]["status"], "pass")
        review_decisions = summary["artifacts"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["counts"]["blocking_count"], 0)
        self.assertEqual(review_decisions["counts"]["warning_count"], 0)
        self.assertGreater(summary["checks_passed"], 0)
        self.assertEqual(summary["checks_failed"], 0)
        self.assertFalse(summary["privacy"]["contains_paths"])
        self.assertFalse(summary["privacy"]["contains_filenames"])
        self.assertFalse(summary["privacy"]["contains_hashes"])
        self.assertFalse(summary["privacy"]["contains_ocr_text"])
        self.assertFalse(summary["privacy"]["contains_thumbnails"])
        self.assertFalse(summary["privacy"]["contains_image_content"])
        self.assertFalse(summary["privacy"]["contains_row_level_findings"])
        self.assertIn("Validation index status: pass", stdout.getvalue())

    def test_public_safe_validation_index_reports_fail_and_missing_by_code_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            failing_release = _release_candidate_bundle_payload()
            failing_release["status"] = "fail"
            failing_release["ready_for_release_candidate"] = False
            _write_json(root / "release_candidate_summary.json", failing_release)
            (root / "aggregate_evidence_bundle_summary.json").unlink()

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["artifact_presence"]["release_candidate_summary.json"]["status"], "fail")
        self.assertEqual(summary["artifact_presence"]["aggregate_evidence_bundle_summary.json"]["status"], "missing")
        self.assertIn("artifact_status_failed", {item["code"] for item in summary["blocking_items"]})
        self.assertIn("aggregate_artifact_missing", {item["code"] for item in summary["blocking_items"]})
        self.assertEqual(summary["summary"]["artifacts_missing"], 1)
        self.assertTrue(summary["privacy"]["aggregate_only"])

    def test_public_safe_validation_index_covers_review_decision_handoff_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["summary"]["known_artifacts"], 6)
        review_decisions = summary["artifacts"]["review_decision_verification_summary.json"]
        self.assertEqual(review_decisions["schema_version"], "scan-qc.review-decision-verification-summary.v1")
        self.assertEqual(review_decisions["reported_status"], "pass")
        self.assertEqual(review_decisions["counts"]["blocking_counts_by_code"], {})
        evidence = summary["artifacts"]["aggregate_evidence_bundle_summary.json"]["review_decision_verification"]
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["blocking_count"], 0)
        handoff = summary["artifacts"]["final_production_handoff_summary.json"]["review_decision_verification"]
        self.assertTrue(handoff["present"])
        self.assertEqual(handoff["status"], "pass")
        for forbidden in ("decision_counts", "source_type", "scan-qc-review-decisions.local.v1", "aggregate_handoff"):
            self.assertNotIn(forbidden, raw)

    def test_public_safe_validation_index_blocks_review_decision_private_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            payload = _review_decision_verification_bundle_payload()
            payload["source"] = {
                "schema": "scan-qc-review-decisions.local.v1",
                "source_type": "aggregate_handoff",
            }
            _write_json(root / "review_decision_verification_summary.json", payload)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["artifact_presence"]["review_decision_verification_summary.json"]["status"], "fail")
        self.assertIn("private_source_metadata_present", {item["code"] for item in summary["blocking_items"]})
        self.assertNotIn("scan-qc-review-decisions.local.v1", raw)
        self.assertNotIn("aggregate_handoff", raw)

    def test_public_safe_validation_index_propagates_processing_reuse_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            payload = _aggregate_evidence_bundle_payload(status="pass")
            payload["summary"] = {
                "total_files": 8,
                "processing_resumed_files": 2,
                "processing_duplicate_reused_files": 3,
                "processing_existing_derivative_reused_files": 4,
            }
            _write_json(root / "aggregate_evidence_bundle_summary.json", payload)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["summary"]["processing_resumed_files"], 2)
        self.assertEqual(summary["summary"]["processing_duplicate_reused_files"], 3)
        self.assertEqual(summary["summary"]["processing_existing_derivative_reused_files"], 4)
        counts = summary["artifacts"]["aggregate_evidence_bundle_summary.json"]["counts"]
        self.assertEqual(counts["processing_resumed_files"], 2)
        self.assertEqual(counts["processing_duplicate_reused_files"], 3)
        self.assertEqual(counts["processing_existing_derivative_reused_files"], 4)

    def test_public_safe_validation_index_omits_missing_processing_reuse_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertNotIn("processing_resumed_files", summary["summary"])
        self.assertNotIn("processing_duplicate_reused_files", summary["summary"])
        self.assertNotIn("processing_existing_derivative_reused_files", summary["summary"])

    def test_public_safe_validation_index_reuse_counters_remain_aggregate_only(self) -> None:
        forbidden_private_values = [
            "/Users/private/archive/page_0001.png",
            "page_0001.png",
            "processing_manifest.json",
            "row_report.csv",
            "OCR text",
            "thumbnail-preview-object",
            "data:image/png",
            "blob:http://localhost/preview",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "provider --private /Users/private/archive",
            "prompt: inspect the private page",
            "raw_model_output: private answer",
            "derivative/page_0001.png",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            payload = _aggregate_evidence_bundle_payload(status="pass")
            payload["summary"] = {
                "processing_resumed_files": 0,
                "processing_duplicate_reused_files": 1,
                "processing_existing_derivative_reused_files": 2,
            }
            payload["operator_warning"] = " ".join(forbidden_private_values)
            _write_json(root / "aggregate_evidence_bundle_summary.json", payload)

            summary = build_public_safe_validation_index(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["summary"]["processing_resumed_files"], 0)
        self.assertEqual(summary["summary"]["processing_duplicate_reused_files"], 1)
        self.assertEqual(summary["summary"]["processing_existing_derivative_reused_files"], 2)
        self.assertIn("private_value_present", {item["code"] for item in summary["blocking_items"]})
        for value in forbidden_private_values:
            self.assertNotIn(value, raw)

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

    def test_artifact_readiness_checklist_passes_with_required_aggregate_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_artifact_readiness_required_fixtures(root)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "artifact-readiness-checklist",
                        "--evidence-dir",
                        str(root),
                        "--out",
                        str(root / "artifact_readiness_checklist.json"),
                    ]
                )
            raw = (root / "artifact_readiness_checklist.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["schema_version"], "scan-qc-artifact-readiness-checklist.v1")
        self.assertEqual(summary["status"], "pass")
        self.assertTrue(summary["ready"])
        self.assertEqual(summary["summary"]["artifacts_present"], 4)
        self.assertEqual(summary["summary"]["required_missing_count"], 0)
        self.assertGreater(summary["summary"]["optional_missing_count"], 0)
        self.assertEqual(summary["blocking_counts_by_code"], {})
        self.assertTrue(summary["artifact_readiness_checklist"]["workbench_public_summary.json"]["present"])
        row = summary["artifact_readiness_checklist"]["final_production_handoff_summary.json"]
        self.assertTrue(row["present"])
        self.assertTrue(row["required"])
        self.assertEqual(row["status"], "pass")
        self.assertEqual(row["generated_at"], "2026-01-01T00:00:00+00:00")
        self.assertIn("Artifact readiness status: pass", stdout.getvalue())

    def test_artifact_readiness_checklist_reports_missing_required_by_code_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))

            summary = build_artifact_readiness_checklist(
                evidence_dir=root,
                generated_at="2026-01-01T00:00:00+00:00",
            )
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "fail")
        self.assertFalse(summary["ready"])
        self.assertEqual(summary["summary"]["required_missing_count"], 3)
        self.assertEqual(summary["blocking_counts_by_code"]["required_aggregate_artifact_missing"], 3)
        self.assertNotIn(str(root), raw)

    def test_artifact_readiness_checklist_rejects_private_inputs_without_echoing_values(self) -> None:
        forbidden_private_values = [
            "/Users/private/archive/page_0001.png",
            "page_0001.png",
            "OCR TEXT",
            "reviewer note private",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "provider --private /Users/private/archive",
            "raw_model_output: private answer",
            "derivative/page_0001.png",
            "SECRET_TOKEN",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_artifact_readiness_required_fixtures(root)
            private_report = root / "scan_qc_report.json"
            private_report.write_text(" ".join(forbidden_private_values), encoding="utf-8")
            candidate = _deep_inspection_candidate_bundle_payload()
            candidate["operator_note"] = " ".join(forbidden_private_values)
            _write_json(root / "deep_inspection_candidate_summary.json", candidate)

            summary = build_artifact_readiness_checklist(
                files=[
                    root / "aggregate_evidence_bundle_summary.json",
                    root / "final_production_handoff_summary.json",
                    root / "public_safe_validation_index.json",
                    root / "workbench_public_summary.json",
                    root / "deep_inspection_candidate_summary.json",
                    private_report,
                ],
                generated_at="2026-01-01T00:00:00+00:00",
            )
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["summary"]["unsupported_inputs"], 1)
        self.assertIn("unsupported_private_input_rejected", summary["blocking_counts_by_code"])
        self.assertIn("private_value_present", summary["blocking_counts_by_code"])
        self.assertGreaterEqual(summary["privacy"]["unsupported_private_input_count"], 1)
        self.assertEqual(summary["artifact_readiness_checklist"]["deep_inspection_candidate_summary.json"]["privacy_status"], "fail")
        self.assertNotIn("scan_qc_report.json", raw)
        for value in forbidden_private_values:
            self.assertNotIn(value, raw)

    def test_artifact_readiness_checklist_loads_in_workbench_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_artifact_readiness_required_fixtures(root)
            checklist = build_artifact_readiness_checklist(
                evidence_dir=root,
                generated_at="2026-01-01T00:00:00+00:00",
            )
            _write_json(root / "artifact_readiness_checklist.json", checklist)

            summary = build_workbench_public_summary(
                files=[root / "artifact_readiness_checklist.json"],
                generated_at="2026-01-01T00:00:00+00:00",
            )

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["artifacts"]["artifact_readiness_checklist.json"]["status"], "pass")

    def test_public_safe_validation_index_omits_private_values_from_privacy_failures(self) -> None:
        forbidden_private_values = [
            "/Users/private/archive/page_0001.png",
            "page_0001.png",
            "processing_manifest.json",
            "row_report.csv",
            "OCR text",
            "thumbnail-preview-object",
            "data:image/png",
            "blob:http://localhost/preview",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "provider --private /Users/private/archive",
            "prompt: inspect the private page",
            "raw_model_output: private answer",
            "derivative/page_0001.png",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_public_safe_validation_index_fixtures(root)
            payload = _aggregate_evidence_bundle_payload(status="pass")
            payload["operator_warning"] = " ".join(forbidden_private_values)
            _write_json(root / "aggregate_evidence_bundle_summary.json", payload)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["public-safe-validation-index", "--input-dir", str(root), "--out", str(root / "index.json")])
            raw = (root / "index.json").read_text(encoding="utf-8")
            summary = json.loads(raw)

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "fail")
        self.assertTrue(summary["privacy"]["aggregate_only"])
        self.assertTrue(summary["privacy"]["private_indicators_found"])
        self.assertIn("private_value_present", {item["code"] for item in summary["blocking_items"]})
        self.assertIn("private_local_preview_object_url_present", {item["code"] for item in summary["blocking_items"]})
        self.assertIn("private_raw_model_output_present", {item["code"] for item in summary["blocking_items"]})
        self.assertFalse(summary["privacy"]["contains_local_preview_object_urls"])
        self.assertFalse(summary["privacy"]["contains_provider_command_strings"])
        self.assertFalse(summary["privacy"]["contains_prompts"])
        self.assertFalse(summary["privacy"]["contains_raw_model_output"])
        for value in forbidden_private_values:
            self.assertNotIn(value, raw)

    def test_version_output_matches_package_version(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"archive-scan-qc {__version__}")

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

    def test_delivery_handoff_manifest_classifies_and_hashes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            report = temp / "scan_qc_report.json"
            benchmark = temp / "benchmark_results.json"
            unknown = temp / "operator_notes.txt"
            out_dir = temp / "handoff"
            report.write_text(
                json.dumps({"schema_version": "scan-qc.phase1.v1", "files": [{"relative_path": "private/page.png"}]}),
                encoding="utf-8",
            )
            benchmark.write_text(json.dumps({"schema_version": "scan-qc.benchmark.v1", "runs": []}), encoding="utf-8")
            unknown.write_text("local note", encoding="utf-8")

            json_path, csv_path, payload = write_delivery_handoff_manifest(
                [
                    ("scan_report", report),
                    ("benchmark_results", benchmark),
                    ("artifact", unknown),
                ],
                out_dir,
            )

            self.assertTrue(json_path.exists())
            self.assertTrue(csv_path.exists())
            self.assertEqual(payload["schema_version"], "scan-qc.delivery-handoff-manifest.v1")
            self.assertEqual(payload["summary"]["artifact_count"], 3)
            self.assertEqual(payload["summary"]["aggregate_public_safe_count"], 1)
            self.assertEqual(payload["summary"]["sensitive_local_evidence_count"], 2)
            by_name = {record["name"]: record for record in payload["artifacts"]}
            self.assertEqual(by_name["benchmark_results.json"]["sensitivity"], "aggregate_public_safe")
            self.assertEqual(by_name["scan_qc_report.json"]["sensitivity"], "sensitive_local_evidence")
            self.assertEqual(by_name["operator_notes.txt"]["sensitivity"], "sensitive_local_evidence")
            self.assertEqual(by_name["benchmark_results.json"]["schema_version"], "scan-qc.benchmark.v1")
            self.assertRegex(by_name["scan_qc_report.json"]["sha256"], r"^[0-9a-f]{64}$")
            with csv_path.open(newline="", encoding="utf-8") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), 3)

    def test_delivery_handoff_manifest_rejects_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "missing artifact"):
                write_delivery_handoff_manifest([("scan_report", temp / "missing.json")], temp / "handoff")

    def test_aggregate_baseline_runner_writes_privacy_safe_public_summary(self) -> None:
        module = _load_aggregate_baseline_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "private_page_001.png", dpi=(300, 300))

            args = module.build_parser().parse_args(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers",
                    "1",
                    "--benchmark-workers-list",
                    "1",
                    "--no-process-images",
                    "--clean-bleed-through",
                ]
            )
            payload = module.run_aggregate_baseline(args)

            json_path = output_dir / "aggregate_baseline_summary.json"
            self.assertTrue(json_path.exists())
            self.assertEqual(payload["schema_version"], "scan-qc.aggregate-baseline.v1")
            self.assertEqual(payload["target_environment"]["validation_target"], "puersai-hpc")
            self.assertFalse(payload["target_environment"]["gpu_acceleration_used"])
            self.assertEqual(payload["worker_settings"]["requested_workers"], 1)
            self.assertTrue(payload["operations"]["clean_bleed_through"])
            self.assertEqual(payload["aggregate_counts"]["total_files"], 1)
            self.assertEqual(payload["aggregate_counts"]["openable_files"], 1)
            self.assertIn("environment", payload)
            hardware = payload["runtime_hardware"]
            self.assertEqual(hardware["schema_version"], "scan-qc.runtime-hardware.v1")
            self.assertIn(hardware["os_family"], {None, "Darwin", "Linux", "Windows", "Java"})
            self.assertRegex(hardware["python_version_family"], r"^\d+\.\d+$")
            self.assertEqual(hardware["gpu_acceleration_used"], False)
            self.assertIn("cpu_logical_count", hardware)
            self.assertIn("total_memory_gb", hardware)
            self.assertIn("output_disk_free_gb", hardware)
            self.assertIn("output_disk_total_gb", hardware)
            self.assertIn("gpu_visible_count", hardware)
            self.assertIn("gpu_memory_total_gb", hardware)
            self.assertIsInstance(hardware["warnings"], list)
            self.assertIn("scan", payload["stage_timings"])
            self.assertIn("processing", payload["stage_timings"])
            worker_sweep = payload["benchmark"]["worker_sweep"]
            self.assertTrue(worker_sweep["enabled"])
            self.assertFalse(worker_sweep["operation_timing_presence"])
            self.assertEqual(worker_sweep["recommendation"]["requested_workers"], 1)
            self.assertEqual(worker_sweep["recommendation"]["metric"], "scan_files_per_minute")
            self.assertEqual(len(worker_sweep["workers"]), 1)
            self.assertEqual(worker_sweep["workers"][0]["requested_workers"], 1)
            self.assertEqual(worker_sweep["workers"][0]["processing"]["failed_files"], 0)
            self.assertEqual(
                set(payload["stage_timings"]["scan"]),
                {"elapsed_seconds", "files_per_minute", "openable_files_per_minute", "benchmark_files_per_minute"},
            )
            self.assertEqual(
                set(payload["stage_timings"]["processing"]),
                {
                    "elapsed_seconds",
                    "processed_files_per_minute",
                    "benchmark_processed_files_per_minute",
                    "operation_timings",
                    "benchmark_operation_timings",
                },
            )
            self.assertIn("run_plan_and_benchmark", payload["stage_timings"])
            self.assertIn("report_write", payload["stage_timings"])
            self.assertIn("total_wall_clock", payload["stage_timings"])
            self.assertGreaterEqual(payload["stage_timings"]["run_plan_and_benchmark"]["elapsed_seconds"], 0.0)
            self.assertGreaterEqual(payload["stage_timings"]["report_write"]["elapsed_seconds"], 0.0)
            self.assertGreaterEqual(payload["stage_timings"]["total_wall_clock"]["elapsed_seconds"], 0.0)
            self.assertGreaterEqual(
                payload["stage_timings"]["total_wall_clock"]["elapsed_seconds"],
                payload["stage_timings"]["scan"]["elapsed_seconds"],
            )
            self.assertTrue(payload["privacy_self_check"]["passed"])

            raw_json = json_path.read_text(encoding="utf-8")
            saved_payload = json.loads(raw_json)
            self.assertTrue(saved_payload["privacy_self_check"]["passed"])
            self.assertIn("total_wall_clock", saved_payload["stage_timings"])
            self.assertIn("report_write", saved_payload["stage_timings"])
            for forbidden in [
                str(input_dir),
                str(output_dir),
                "private_page_001.png",
                "relative_path",
                "source_path",
                "filename",
                "sha256",
                "thumbnail",
                "ocr",
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                '"files": [',
                '"findings": [',
            ]:
                self.assertNotIn(forbidden, raw_json)

    def test_aggregate_baseline_runtime_hardware_tolerates_missing_nvidia_smi(self) -> None:
        module = _load_aggregate_baseline_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            with mock.patch.object(module.subprocess, "run", side_effect=FileNotFoundError):
                hardware = module._runtime_hardware_summary(output_dir)

        self.assertEqual(hardware["gpu_visible_count"], 0)
        self.assertEqual(hardware["gpu_memory_total_gb"], 0.0)
        self.assertFalse(hardware["gpu_acceleration_used"])
        self.assertTrue(any("nvidia-smi unavailable" in warning for warning in hardware["warnings"]))

    def test_private_integration_parser_reports_clean_bleed_through_option(self) -> None:
        module = _load_private_integration_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "private_page_001.png", dpi=(300, 300))

            args = module.build_parser().parse_args(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers",
                    "1",
                    "--clean-bleed-through",
                    "--skip-benchmark",
                ]
            )
            payload = module.run_private_integration(args).summary

        self.assertTrue(args.clean_bleed_through)
        self.assertTrue(payload["configuration"]["clean_bleed_through"])
        self.assertTrue(payload["privacy_self_check"]["passed"])

    def test_private_integration_reports_numpy_despeckle_backend_available(self) -> None:
        module = _load_private_integration_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            input_dir.mkdir()
            image = Image.new("RGB", (20, 20), "white")
            image.putpixel((10, 10), (0, 0, 0))
            image.save(input_dir / "private_page_001.png", dpi=(300, 300))

            args = module.build_parser().parse_args(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers",
                    "1",
                    "--process-images",
                    "--despeckle",
                    "--despeckle-backend",
                    "numpy",
                    "--skip-benchmark",
                ]
            )
            with (
                mock.patch.object(module, "_load_numpy", return_value=object()),
                mock.patch("archive_scan_qc.processing._despeckle_candidate_points_numpy", return_value=[(10, 10)]),
                mock.patch("archive_scan_qc.processing._despeckle_replacements_numpy", return_value=[(10, 10, (255, 255, 255))]),
            ):
                payload = module.run_private_integration(args).summary

        backend = payload["despeckle_backend"]
        self.assertEqual(backend["requested_backend"], "numpy")
        self.assertEqual(backend["effective_backend_mode"], "numpy")
        self.assertTrue(backend["numpy_available"])
        self.assertEqual(backend["backend_counts"]["numpy"], 1)
        self.assertEqual(backend["fallback_count"], 0)
        self.assertEqual(payload["warning_item_count"], 0)

    def test_aggregate_baseline_warns_when_requested_numpy_despeckle_falls_back(self) -> None:
        module = _load_aggregate_baseline_module()
        private_module = sys.modules["run_private_integration"]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            input_dir.mkdir()
            image = Image.new("RGB", (20, 20), "white")
            image.putpixel((10, 10), (0, 0, 0))
            image.save(input_dir / "private_page_001.png", dpi=(300, 300))

            args = module.build_parser().parse_args(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers",
                    "1",
                    "--benchmark-workers-list",
                    "1",
                    "--process-images",
                    "--despeckle",
                    "--despeckle-backend",
                    "numpy",
                    "--skip-benchmark",
                    "--cleanup-artifacts",
                ]
            )
            with (
                mock.patch.object(private_module, "_load_numpy", return_value=None),
                mock.patch("archive_scan_qc.processing._load_numpy", return_value=None),
            ):
                payload = module.run_aggregate_baseline(args)

        backend = payload["despeckle_backend"]
        self.assertEqual(backend["requested_backend"], "numpy")
        self.assertEqual(backend["effective_backend_mode"], "fallback")
        self.assertFalse(backend["numpy_available"])
        self.assertEqual(backend["backend_counts"]["numpy"], 0)
        self.assertEqual(backend["backend_counts"]["fallback"], 1)
        self.assertEqual(backend["fallback_count"], 1)
        self.assertEqual(backend["requested_numpy_fallback_count"], 1)
        self.assertIn("despeckle_numpy_unavailable_fallback", backend["warning_codes"])
        self.assertIn("despeckle_numpy_requested_all_fallback", backend["warning_codes"])
        self.assertEqual(payload["warning_counts_by_code"]["despeckle_numpy_unavailable_fallback"], 1)
        self.assertEqual(payload["warning_counts_by_code"]["despeckle_numpy_requested_all_fallback"], 1)
        self.assertTrue(payload["privacy_self_check"]["passed"])

    def test_capability_probe_reports_missing_optional_providers_as_non_blocking(self) -> None:
        def missing_package(module_name: str) -> bool:
            return False

        def missing_nvidia(*args, **kwargs):
            raise FileNotFoundError

        report = run_capability_probe(
            package_available=missing_package,
            command_runner=missing_nvidia,
            environ={},
        )

        self.assertEqual(report["schema_version"], "scan-qc.capability-probe.v1")
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["readiness"]["blocking"])
        self.assertEqual(report["readiness"]["provider_packages_found"], [])
        self.assertFalse(report["readiness"]["gpu_or_model_provider_visible"])
        self.assertFalse(report["readiness"]["gpu_acceleration_configured"])
        self.assertFalse(report["readiness"]["model_acceleration_configured"])
        self.assertTrue(report["privacy"]["aggregate_only"])
        self.assertFalse(report["privacy"]["contains_paths"])
        self.assertFalse(report["privacy"]["contains_environment_values"])

    def test_capability_probe_summarizes_visible_configured_providers_without_sensitive_data(self) -> None:
        def package_available(module_name: str) -> bool:
            return module_name in {"onnxruntime", "torch"}

        def visible_nvidia(*args, **kwargs):
            return mock.Mock(returncode=0, stdout="24576\n24576\n")

        report = run_capability_probe(
            CapabilityProbeConfig(
                analysis_provider_command="/private/tools/provider --token secret",
                gpu_acceleration_enabled=True,
                include_torch_cuda=False,
            ),
            package_available=package_available,
            command_runner=visible_nvidia,
            environ={"SCAN_QC_MODEL_ACCELERATION_ENABLED": "1"},
        )
        raw = json.dumps(report, ensure_ascii=False)

        self.assertEqual(report["gpu_provider_visibility"]["gpu_visible_count"], 2)
        self.assertEqual(report["gpu_provider_visibility"]["nvidia_smi"]["memory_total_gb"], 48.0)
        self.assertEqual(report["readiness"]["provider_packages_found"], ["onnxruntime", "torch"])
        self.assertTrue(report["configuration"]["analysis_provider_configured"])
        self.assertTrue(report["readiness"]["gpu_acceleration_configured"])
        self.assertTrue(report["readiness"]["model_acceleration_configured"])
        self.assertNotIn("/private/tools/provider", raw)
        self.assertNotIn("secret", raw)
        self.assertNotIn("SCAN_QC_MODEL_ACCELERATION_ENABLED", raw)

    def test_capability_probe_cli_writes_privacy_safe_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "probe"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["capability-probe", "--out", str(out_dir), "--no-torch-cuda-check"])

            report_path = out_dir / "capability_probe.json"
            payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertIn("Inference run: no", stdout.getvalue())
        self.assertTrue(payload["privacy"]["aggregate_only"])
        self.assertEqual(payload["readiness"]["scan_processing_semantics"], "unchanged_cpu_pillow_baseline")

    def test_deep_inspection_provider_probe_defaults_disabled_without_inference(self) -> None:
        report = build_deep_inspection_provider_probe()
        raw = json.dumps(report, ensure_ascii=False)

        self.assertFalse(report["configured"])
        self.assertEqual(report["provider_count"], 0)
        self.assertEqual(report["missing_requirements"], [])
        self.assertTrue(report["no_inference_run"])
        self.assertEqual(report["scan_processing_semantics"], "unchanged_cpu_pillow_baseline")
        self.assertTrue(report["privacy"]["aggregate_only"])
        self.assertFalse(report["privacy"]["contains_paths"])
        self.assertNotIn("relative_path", raw)
        self.assertNotIn("sha256", raw)

    def test_deep_inspection_provider_probe_reports_aggregate_config_only(self) -> None:
        config = parse_deep_inspection_provider_config(
            {
                "enabled": True,
                "providers": [
                    {
                        "name": "layout-local",
                        "command": "layout-provider --metadata-only",
                        "requirements": ["onnxruntime", "model_weights_installed"],
                        "config": {"backend": "onnx", "batch_size": 1, "notes": "metadata only"},
                    }
                ],
            }
        )
        report = build_deep_inspection_provider_probe(config)
        raw = json.dumps(report, ensure_ascii=False)

        self.assertTrue(report["configured"])
        self.assertEqual(report["provider_count"], 1)
        self.assertEqual(report["provider_names"], ["layout-local"])
        self.assertEqual(report["missing_requirements"], ["model_weights_installed", "onnxruntime"])
        self.assertTrue(report["no_inference_run"])
        self.assertNotIn("layout-provider", raw)
        self.assertNotIn("metadata only", raw)

    def test_deep_inspection_provider_config_rejects_private_fields_and_disabled_providers(self) -> None:
        with self.assertRaisesRegex(DeepInspectionProviderConfigError, "enabled=false"):
            parse_deep_inspection_provider_config({"enabled": False, "providers": [{"name": "local"}]})
        with self.assertRaisesRegex(DeepInspectionProviderConfigError, "Private provider field"):
            parse_deep_inspection_provider_config(
                {"enabled": True, "providers": [{"name": "local", "config": {"source_path": "/private/a.png"}}]}
            )
        with self.assertRaisesRegex(DeepInspectionProviderConfigError, "Private provider value"):
            parse_deep_inspection_provider_config(
                {"enabled": True, "providers": [{"name": "local", "config": {"sample": "private_page.png"}}]}
            )

    def test_deep_inspection_provider_probe_cli_writes_privacy_safe_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir) / "probe"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["deep-inspection-provider-probe", "--out", str(out_dir)])

            report_path = out_dir / "deep_inspection_provider_probe.json"
            payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertIn("No inference run: true", stdout.getvalue())
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["provider_count"], 0)
        self.assertTrue(payload["privacy"]["aggregate_only"])

    def test_deep_inspection_candidate_summary_is_aggregate_only(self) -> None:
        scan_report = {
            "schema_version": "scan-qc.phase1.v1",
            "source_root": "/Users/private/archive/private_batch",
            "summary": {"total_findings": 2},
            "findings": [
                {
                    "id": "row-001-private",
                    "relative_path": "secret_page_001.png",
                    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "rule": "quality_too_dark",
                    "severity": "P1",
                    "source": "scanner",
                    "confidence": 0.71,
                    "message": "OCR text says private donor name",
                    "thumbnail": "data:image/png;base64,private",
                },
                {
                    "candidate_id": "candidate-private-002",
                    "relative_path": "secret_page_002.png",
                    "rule": "provider.local.review_needed",
                    "severity": "P2",
                    "source": "provider",
                    "confidence": 0.96,
                    "reviewer_notes": "call curator about restricted folder",
                },
            ],
        }
        processing_review_package = {
            "schema_version": "scan-qc.processing-review.v1",
            "source_processing_manifest": "processing_manifest.json",
            "summary": {
                "failed_files": 1,
                "guardrail_warning_files": 1,
                "status_counts": {"failed": 1, "needs_review": 2, "processed": 3},
            },
            "groups": {
                "failed": {
                    "count": 1,
                    "records": [{"source_relative_path": "private_failed.png", "output_relative_path": "derivatives/out.png"}],
                },
                "guardrail_warnings": {
                    "count": 1,
                    "records": [{"reviewer_notes": "restricted note"}],
                },
            },
        }
        provider_probe = {"configured": True, "provider_count": 2, "provider_names": ["private-local"]}

        summary = build_deep_inspection_candidate_summary(
            scan_report=scan_report,
            processing_review_package=processing_review_package,
            provider_probe=provider_probe,
        )
        raw = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(summary["schema_version"], "scan-qc.deep-inspection-candidates.v1")
        self.assertEqual(summary["candidate_total"], 6)
        self.assertEqual(summary["candidates_by_severity"]["P1"], 1)
        self.assertEqual(summary["candidates_by_severity"]["P2"], 1)
        self.assertEqual(summary["candidates_by_reason"]["rule_bucket:quality"], 1)
        self.assertEqual(summary["candidates_by_reason"]["rule_bucket:provider"], 1)
        self.assertEqual(summary["candidates_by_reason"]["processing_review_status:failed"], 1)
        self.assertEqual(summary["candidates_by_reason"]["processing_review_status:needs_review"], 2)
        self.assertTrue(summary["provider_configured"])
        self.assertEqual(summary["provider_count"], 2)
        self.assertTrue(summary["no_inference_run"])
        self.assertEqual(summary["privacy_status"], "aggregate_public_safe")
        for forbidden in (
            "/Users/private/archive",
            "secret_page_001.png",
            "secret_page_002.png",
            "private_failed.png",
            "derivatives/out.png",
            "0123456789abcdef",
            "OCR text",
            "data:image",
            "row-001-private",
            "candidate-private-002",
            "restricted note",
            "processing_manifest.json",
            "private-local",
        ):
            self.assertNotIn(forbidden, raw)

    def test_deep_inspection_candidate_summary_cli_writes_privacy_safe_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            scan_path = temp / "scan_qc_report.json"
            review_path = temp / "processing_review_package.json"
            out_dir = temp / "summary"
            scan_path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "relative_path": "/Users/private/archive/secret_scan.png",
                                "rule": "manifest_missing_file",
                                "severity": "P0",
                                "source": "manifest",
                                "confidence": 1.0,
                                "message": "private path /Users/private/archive/secret_scan.png",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            review_path.write_text(
                json.dumps({"summary": {"failed_files": 1, "status_counts": {"failed": 1}}, "files": []}),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "deep-inspection-candidate-summary",
                        "--scan-report",
                        str(scan_path),
                        "--processing-review-package",
                        str(review_path),
                        "--out",
                        str(out_dir),
                    ]
                )
            payload = json.loads((out_dir / "deep_inspection_candidate_summary.json").read_text(encoding="utf-8"))
            raw = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(exit_code, 0)
        self.assertIn("No inference run: true", stdout.getvalue())
        self.assertEqual(payload["candidate_total"], 2)
        self.assertEqual(payload["candidates_by_severity"]["P0"], 1)
        self.assertFalse(payload["provider_configured"])
        self.assertNotIn("/Users/private/archive", raw)
        self.assertNotIn("secret_scan.png", raw)

    def test_deep_inspection_candidate_summary_counts_groups_only_processing_review(self) -> None:
        processing_review_package = {
            "groups": {
                "failed": {
                    "count": 2,
                    "records": [
                        {"source_relative_path": "private_failed_001.png"},
                        {"output_relative_path": "derivatives/private_failed_002.png"},
                    ],
                },
                "guardrail_warnings": {
                    "count": 1,
                    "records": [{"reviewer_notes": "restricted note for curator"}],
                },
            }
        }

        summary = build_deep_inspection_candidate_summary(processing_review_package=processing_review_package)
        raw = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["candidate_total"], 3)
        self.assertEqual(summary["candidates_by_reason"]["processing_review_group:failed"], 2)
        self.assertEqual(summary["candidates_by_reason"]["processing_review_group:guardrail_warnings"], 1)
        self.assertTrue(summary["privacy"]["aggregate_only"])
        for forbidden in (
            "private_failed_001.png",
            "private_failed_002.png",
            "derivatives/private_failed_002.png",
            "restricted note",
        ):
            self.assertNotIn(forbidden, raw)

    def test_aggregate_baseline_privacy_check_blocks_paths_filenames_and_hashes(self) -> None:
        module = _load_aggregate_baseline_module()
        payload = {
            "safe": "aggregate",
            "bad_path": "/Users/private/sample",
            "bad_file": "private_page_001.png",
            "bad_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        }

        leaks = module._aggregate_privacy_leaks(payload)

        self.assertIn("path-like value at bad_path", leaks)
        self.assertIn("filename-like value at bad_file", leaks)
        self.assertIn("hash-like value at bad_hash", leaks)

    def test_aggregate_baseline_worker_sweep_includes_processing_evidence(self) -> None:
        module = _load_aggregate_baseline_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "private_page_001.png", dpi=(300, 300))

            args = module.build_parser().parse_args(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers",
                    "1",
                    "--benchmark-workers-list",
                    "1,2",
                    "--process-images",
                    "--auto-crop",
                    "--cleanup-artifacts",
                ]
            )
            payload = module.run_aggregate_baseline(args)

            worker_sweep = payload["benchmark"]["worker_sweep"]
            self.assertTrue(worker_sweep["enabled"])
            self.assertTrue(worker_sweep["operation_timing_presence"])
            self.assertEqual([point["requested_workers"] for point in worker_sweep["workers"]], [1, 2])
            for point in worker_sweep["workers"]:
                self.assertEqual(point["run_count"], 1)
                self.assertIsNotNone(point["scan"]["files_per_minute"])
                self.assertIsNotNone(point["processing"]["processed_files_per_minute"])
                self.assertEqual(point["processing"]["failed_files"], 0)
                self.assertTrue(point["processing"]["operation_timing_presence"])
            recommendation = worker_sweep["recommendation"]
            self.assertIn(recommendation["requested_workers"], [1, 2])
            self.assertEqual(recommendation["metric"], "processing_processed_files_per_minute")
            self.assertIn("90%", recommendation["basis"])
            self.assertTrue(payload["privacy_self_check"]["passed"])
            self.assertEqual([child.name for child in output_dir.iterdir()], ["aggregate_baseline_summary.json"])

    def test_aggregate_baseline_cleanup_removes_generated_outputs_and_preserves_input(self) -> None:
        module = _load_aggregate_baseline_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            input_dir.mkdir()
            source_image = input_dir / "private_page_001.png"
            Image.new("RGB", (32, 24), "white").save(source_image, dpi=(300, 300))

            args = module.build_parser().parse_args(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers",
                    "1",
                    "--benchmark-workers-list",
                    "1",
                    "--process-images",
                    "--skip-benchmark",
                    "--cleanup-artifacts",
                ]
            )
            payload = module.run_aggregate_baseline(args)

            json_path = output_dir / "aggregate_baseline_summary.json"
            self.assertTrue(json_path.exists())
            self.assertTrue(source_image.exists())
            self.assertFalse((output_dir / "private_integration_summary.json").exists())
            self.assertFalse((output_dir / "scan-reports").exists())
            self.assertFalse((output_dir / "processed-images").exists())
            self.assertFalse((output_dir / "run-plan").exists())
            self.assertEqual(payload["schema_version"], "scan-qc.aggregate-baseline.v1")
            self.assertTrue(payload["privacy_self_check"]["passed"])
            self.assertTrue(payload["cleanup"]["enabled"])
            self.assertGreaterEqual(payload["cleanup"]["elapsed_seconds"], 0.0)
            self.assertEqual(payload["cleanup"]["elapsed_seconds"], json.loads(json_path.read_text(encoding="utf-8"))["cleanup"]["elapsed_seconds"])
            self.assertIn("processed-images", payload["cleanup"]["removed_artifacts"])
            self.assertEqual([child.name for child in output_dir.iterdir()], ["aggregate_baseline_summary.json"])

    def test_aggregate_baseline_cleanup_preserves_input_inside_output_root(self) -> None:
        module = _load_aggregate_baseline_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "private-output"
            input_dir = output_dir / "scan-reports"
            input_dir.mkdir(parents=True)
            source_image = input_dir / "private_page_001.png"
            Image.new("RGB", (32, 24), "white").save(source_image, dpi=(300, 300))

            cleanup = module.cleanup_generated_artifacts(output_root=output_dir, input_dir=input_dir)

            self.assertTrue(source_image.exists())
            self.assertTrue(input_dir.exists())
            self.assertIn("scan-reports", cleanup["preserved_artifacts"])

    def test_aggregate_baseline_parser_accepts_puersai_hpc_env_defaults(self) -> None:
        module = _load_aggregate_baseline_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            previous = {
                key: os.environ.get(key)
                for key in [
                    "PUERSAI_HPC_BASELINE_INPUT",
                    "PUERSAI_HPC_BASELINE_OUT",
                    "PUERSAI_HPC_BASELINE_WORKERS",
                    "PUERSAI_HPC_BASELINE_WORKERS_LIST",
                    "PUERSAI_HPC_BASELINE_CLEANUP_ARTIFACTS",
                ]
            }
            try:
                os.environ["PUERSAI_HPC_BASELINE_INPUT"] = str(input_dir)
                os.environ["PUERSAI_HPC_BASELINE_OUT"] = str(output_dir)
                os.environ["PUERSAI_HPC_BASELINE_WORKERS"] = "2"
                os.environ["PUERSAI_HPC_BASELINE_WORKERS_LIST"] = "1,2"
                os.environ["PUERSAI_HPC_BASELINE_CLEANUP_ARTIFACTS"] = "1"
                args = module.build_parser().parse_args([])
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            self.assertEqual(Path(args.input), input_dir)
            self.assertEqual(Path(args.out), output_dir)
            self.assertEqual(args.workers, 2)
            self.assertEqual(args.benchmark_workers_list, "1,2")
            self.assertTrue(args.cleanup_artifacts)

    def test_production_validation_wrapper_writes_aggregate_acceptance_and_cleans_up(self) -> None:
        module = _load_production_validation_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "private_page_001.png", dpi=(300, 300))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = module.main(
                    [
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--workers",
                        "1",
                        "--benchmark-workers-list",
                        "1",
                        "--process-images",
                        "--skip-benchmark",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                sorted(child.name for child in output_dir.iterdir()),
                ["acceptance_summary.json", "aggregate_baseline_summary.json"],
            )
            baseline = json.loads((output_dir / "aggregate_baseline_summary.json").read_text(encoding="utf-8"))
            acceptance = json.loads((output_dir / "acceptance_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(baseline["privacy_self_check"]["passed"])
            self.assertTrue(baseline["cleanup"]["enabled"])
            self.assertEqual(baseline["cleanup"]["preserved_artifacts"], [])
            self.assertEqual(acceptance["status"], "pass")
            self.assertTrue(acceptance["cleanup"]["retained_public_summary_only"])
            self.assertIn("Acceptance status: pass", stdout.getvalue())
            self.assertIn("Runtime OS family:", stdout.getvalue())
            self.assertIn("CPU logical count:", stdout.getvalue())
            self.assertIn("GPU acceleration used: False", stdout.getvalue())
            combined = json.dumps({"baseline": baseline, "acceptance": acceptance}) + stdout.getvalue()
            for forbidden in [str(input_dir), str(output_dir), "private_page_001.png", "relative_path", "sha256"]:
                self.assertNotIn(forbidden, combined)

    def test_production_validation_wrapper_exits_nonzero_on_threshold_regression(self) -> None:
        module = _load_production_validation_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "private_page_001.png", dpi=(300, 300))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = module.main(
                    [
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--workers",
                        "1",
                        "--benchmark-workers-list",
                        "1",
                        "--no-process-images",
                        "--min-scan-files-per-minute",
                        "1000000",
                    ]
                )

            self.assertEqual(exit_code, 1)
            acceptance = json.loads((output_dir / "acceptance_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(acceptance["status"], "fail")
            self.assertIn("scan_throughput_below_threshold", {item["code"] for item in acceptance["blocking_items"]})
            self.assertIn("Blocking items: scan_throughput_below_threshold", stdout.getvalue())

    def test_production_validation_wrapper_builds_baseline_args(self) -> None:
        module = _load_production_validation_module()
        args = module.build_parser().parse_args(
            [
                "--input",
                "/placeholder/private-20-image-sample",
                "--out",
                "/placeholder/private-validation-output",
                "--workers",
                "4",
                "--benchmark-workers-list",
                "1,2,4,8",
                "--normalize-tones",
                "--normalize-paper-color-cast",
                "--lighten-edge-shadow",
                "--lighten-background-stains",
                "--lighten-fold-shadows",
                "--clean-bleed-through",
                "--lighten-scanlines",
                "--enhance-faded-text",
                "--sharpen-text-edges",
                "--min-scan-files-per-minute",
                "100",
                "--min-processing-files-per-minute",
                "60",
            ]
        )
        baseline_args = module._build_baseline_args(args)

        self.assertEqual(Path(baseline_args.input), Path("/placeholder/private-20-image-sample"))
        self.assertEqual(Path(baseline_args.out), Path("/placeholder/private-validation-output"))
        self.assertEqual(baseline_args.workers, 4)
        self.assertEqual(baseline_args.benchmark_workers_list, "1,2,4,8")
        self.assertTrue(baseline_args.normalize_tones)
        self.assertTrue(baseline_args.normalize_paper_color_cast)
        self.assertTrue(baseline_args.lighten_edge_shadow)
        self.assertTrue(baseline_args.lighten_background_stains)
        self.assertTrue(baseline_args.lighten_fold_shadows)
        self.assertTrue(baseline_args.clean_bleed_through)
        self.assertTrue(baseline_args.lighten_scanlines)
        self.assertTrue(baseline_args.enhance_faded_text)
        self.assertTrue(baseline_args.sharpen_text_edges)
        self.assertTrue(baseline_args.cleanup_artifacts)

    def test_rework_action_list_groups_qc_findings_and_processing_retry(self) -> None:
        report = {
            "schema_version": "scan-qc.phase1.v1",
            "project": {"project_id": "p1", "batch_id": "b1"},
            "summary": {"total_findings": 5, "p0_findings": 1, "p1_findings": 3, "p2_findings": 1},
            "findings": [
                {
                    "relative_path": "001_rescan.png",
                    "rule": "openable",
                    "severity": "P0",
                    "source": "scanner",
                    "confidence": 1.0,
                    "message": "cannot open source image",
                },
                {
                    "relative_path": "002_process.png",
                    "rule": "quality_suspected_blur",
                    "severity": "P1",
                    "source": "scanner",
                    "confidence": 0.8,
                    "message": "blur candidate for derivative processing",
                },
                {
                    "relative_path": "003_manifest.png",
                    "rule": "manifest_duplicate_sequence",
                    "severity": "P1",
                    "source": "manifest",
                    "confidence": 1.0,
                    "message": "duplicate sequence value",
                },
                {
                    "relative_path": "004_manual.png",
                    "rule": "provider_needs_operator_check",
                    "severity": "P1",
                    "source": "provider",
                    "confidence": 0.6,
                    "message": "provider requested local review",
                },
                {
                    "relative_path": "005_info.png",
                    "rule": "name_pattern",
                    "severity": "P2",
                    "source": "scanner",
                    "confidence": 1.0,
                    "message": "filename does not match pattern",
                },
            ],
        }
        retry_manifest = {
            "schema_version": "scan-qc.processing.retry.v1",
            "summary": {"failed_files": 1, "retry_list_files": 1},
            "files": [
                {
                    "source_relative_path": "006_retry.png",
                    "source_sha256": "abc123",
                    "status": "failed",
                    "failure_reason": "source image is not openable",
                    "error": "cannot identify image file",
                }
            ],
        }

        payload = build_rework_action_list(report, processing_retry_manifest=retry_manifest)

        by_path = {action["relative_path"]: action for action in payload["actions"]}
        self.assertEqual(by_path["001_rescan.png"]["action_type"], "rescan_required")
        self.assertEqual(by_path["002_process.png"]["action_type"], "reprocess_candidate")
        self.assertEqual(by_path["003_manifest.png"]["action_type"], "duplicate_manifest_correction")
        self.assertEqual(by_path["004_manual.png"]["action_type"], "manual_review")
        self.assertEqual(by_path["005_info.png"]["action_type"], "informational_follow_up")
        self.assertEqual(by_path["006_retry.png"]["action_type"], "processing_retry")
        self.assertEqual(by_path["006_retry.png"]["processing_retry_evidence"][0]["source_sha256"], "abc123")
        self.assertEqual(payload["summary"]["actions_by_type"]["processing_retry"], 1)
        self.assertEqual(payload["summary"]["actions_by_priority"]["P0"], 2)
        self.assertTrue(payload["privacy"]["local_only"])
        self.assertFalse(payload["privacy"]["contains_thumbnails"])
        self.assertFalse(payload["privacy"]["contains_image_content"])

    def test_rework_action_list_writes_deterministic_json_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            report_path = temp / "scan_qc_report.json"
            retry_path = temp / "processing_retry_manifest.json"
            out_path = temp / "rework_action_list.json"
            csv_path = temp / "rework_action_list.csv"
            report_path.write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.phase1.v1",
                        "summary": {"total_findings": 2, "p0_findings": 0, "p1_findings": 1, "p2_findings": 1},
                        "findings": [
                            {
                                "relative_path": "b.png",
                                "rule": "name_pattern",
                                "severity": "P2",
                                "source": "scanner",
                                "confidence": 1.0,
                                "message": "bad name",
                            },
                            {
                                "relative_path": "a.png",
                                "rule": "quality_too_dark",
                                "severity": "P1",
                                "source": "scanner",
                                "confidence": 1.0,
                                "message": "too dark",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            retry_path.write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.processing.retry.v1",
                        "summary": {"retry_list_files": 1},
                        "files": [
                            {
                                "source_relative_path": "c.png",
                                "source_sha256": "def456",
                                "status": "failed",
                                "failure_reason": "guardrail failure",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            _, _, payload = write_rework_action_list(report_path, out_path, processing_retry_manifest_path=retry_path, csv_path=csv_path)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual([action["action_id"] for action in payload["actions"]], ["RA000001", "RA000002", "RA000003"])
            self.assertEqual([action["relative_path"] for action in payload["actions"]], ["c.png", "a.png", "b.png"])
            self.assertEqual(rows[0]["action_type"], "processing_retry")
            self.assertEqual(rows[1]["action_type"], "reprocess_candidate")
            self.assertEqual(rows[2]["action_type"], "informational_follow_up")
            self.assertIn("LOCAL-ONLY SENSITIVE EVIDENCE", out_path.read_text(encoding="utf-8"))
            self.assertNotIn("thumbnail", json.dumps(payload["actions"]))

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

    def test_collects_metadata_and_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (16, 16), "white").save(input_dir / "A001_0002.png", dpi=(150, 150))

            report = scan_batch(
                ScanConfig(
                    project_id="p1",
                    batch_id="b1",
                    input_dir=input_dir,
                    output_dir=output_dir,
                    min_dpi=200,
                    name_pattern=r"A001_\d{4}",
                )
            )
            paths = write_reports(report, output_dir)

            self.assertEqual(report["summary"]["total_files"], 2)
            self.assertEqual(report["summary"]["openable_files"], 2)
            self.assertTrue(any(finding["rule"] == "dpi_minimum" for finding in report["findings"]))
            self.assertTrue(paths["json"].exists())
            self.assertTrue(paths["html"].exists())
            self.assertTrue(paths["files_csv"].exists())
            self.assertTrue(paths["findings_csv"].exists())

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual(saved["project"]["project_id"], "p1")
            self.assertEqual(saved["manifest"]["project_id"], "p1")
            self.assertEqual(saved["manifest"]["batch_id"], "b1")
            self.assertEqual(saved["manifest"]["rule_version"], "scan-qc.phase1.v1")
            self.assertEqual(saved["manifest"]["total_files"], 2)
            self.assertEqual(saved["manifest"]["p0_findings"], report["summary"]["p0_findings"])
            self.assertFalse(saved["manifest"]["manifest_used"])
            self.assertIn("performance", saved["summary"])
            self.assertIn("performance", saved["manifest"])
            self.assertEqual(saved["summary"]["performance"]["total_files"], 2)
            self.assertEqual(saved["summary"]["performance"]["openable_files"], 2)
            self.assertIn("effective_workers", saved["summary"]["performance"])
            self.assertIn(saved["summary"]["performance"]["mode"], {"serial", "parallel"})
            self.assertGreaterEqual(saved["summary"]["performance"]["elapsed_seconds"], 0)
            self.assertGreaterEqual(saved["summary"]["performance"]["files_per_minute"], 0)
            self.assertGreaterEqual(saved["summary"]["performance"]["openable_files_per_minute"], 0)

            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("<!doctype html>", html)
            self.assertIn("Scan QC Report", html)
            self.assertIn("Total Files", html)
            self.assertIn("A001_0002.png", html)
            self.assertIn("dpi_minimum", html)
            self.assertIn("P0", html)
            self.assertIn("Schema Version", html)
            self.assertIn("Project", html)
            self.assertIn("Dependency Notes", html)
            self.assertIn("Rules Profile", html)
            self.assertIn("Performance Metrics", html)
            self.assertIn("Skipped Inputs", html)
            self.assertIn("Manifest Consistency", html)
            self.assertIn("Quality Metrics", html)
            self.assertIn("Orientation And Blank Pages", html)
            self.assertIn("Findings Summary", html)
            self.assertIn("Rule Catalog", html)
            self.assertIn("Image openability", html)
            self.assertIn("Brightness Mean Avg", html)
            self.assertIn("EXIF Transpose Signals", html)
            self.assertIn("Manifest Unique Entries", html)
            self.assertIn("SHA256", html)
            self.assertIn(saved["files"][0]["sha256"], html)
            self.assertIn("Complete Report Data", html)
            self.assertIn('id="scan-qc-report-data"', html)
            self.assertNotIn("<img", html.lower())
            self.assertNotIn("data:image", html.lower())
            self.assertNotIn("src=", html.lower())

    def test_default_analysis_provider_is_disabled_and_report_shape_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))

            self.assertFalse(report["analysis_provider"]["enabled"])
            self.assertEqual(report["summary"]["provider_findings"], 0)
            self.assertTrue(all(finding["source"] == "rules" for finding in report["findings"]))

    def test_fake_analysis_provider_finding_is_merged_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            provider = root / "fake_provider.py"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.png", dpi=(300, 300))
            _write_fake_provider(
                provider,
                [
                    {
                        "type": "metadata",
                        "provider": {"name": "fake", "version": "1.0", "model": "none"},
                    },
                    {
                        "type": "finding",
                        "relative_path": "A001_0001.png",
                        "rule": "provider.fake.suspected_issue",
                        "severity": "P2",
                        "confidence": 0.875,
                        "message": "Fake local model signal.",
                        "metadata": {"model": "none", "backend": "unit-test"},
                    },
                ],
            )

            report = scan_batch(
                ScanConfig(
                    "p1",
                    "b1",
                    input_dir,
                    output_dir,
                    analysis_provider_command=f"{sys.executable} {provider}",
                )
            )
            paths = write_reports(report, output_dir)
            provider_findings = [finding for finding in report["findings"] if finding["source"] == "provider"]

            self.assertEqual(len(provider_findings), 1)
            self.assertEqual(provider_findings[0]["rule"], "provider.fake.suspected_issue")
            self.assertEqual(provider_findings[0]["confidence"], 0.875)
            self.assertEqual(report["summary"]["provider_findings"], 1)
            self.assertEqual(report["analysis_provider"]["provider"]["name"], "fake")
            html = paths["html"].read_text(encoding="utf-8")
            csv_text = paths["findings_csv"].read_text(encoding="utf-8")
            self.assertIn("Provider Analysis", html)
            self.assertIn("provider.fake.suspected_issue", html)
            self.assertIn("provider,0.875", csv_text)

    def test_prepare_command_rejoins_spaced_provider_path(self) -> None:
        command = f"{sys.executable} {REPO_ROOT / 'examples' / 'local_analysis_provider.py'} --flag"
        argv = _prepare_command(command)

        self.assertEqual(argv, [
            sys.executable,
            str(REPO_ROOT / 'examples' / 'local_analysis_provider.py'),
            "--flag",
        ])

    def test_example_analysis_provider_runs_through_cli_and_sanitizes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.png", dpi=(300, 300))

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--project",
                    "p1",
                    "--batch",
                    "b1",
                    "--analysis-provider-command",
                    f"{sys.executable} {LOCAL_PROVIDER_EXAMPLE}",
                ]
            )

            self.assertEqual(exit_code, 0)
            report_text = (output_dir / "scan_qc_report.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            provider_findings = [finding for finding in report["findings"] if finding["source"] == "provider"]
            self.assertEqual(len(provider_findings), 1)
            self.assertEqual(provider_findings[0]["rule"], "provider.local-sample.small_canvas")
            self.assertEqual(provider_findings[0]["confidence"], 0.66)
            self.assertEqual(report["summary"]["provider_findings"], 1)
            self.assertEqual(report["analysis_provider"]["provider"]["name"], "local-sample")
            self.assertNotIn("sanitizer_probe_path", report_text)
            self.assertNotIn("image_probe", report_text)
            self.assertNotIn("synthetic-value-should-not-appear-in-reports", report_text)

    def test_invalid_analysis_provider_output_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            provider = root / "bad_provider.py"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.png", dpi=(300, 300))
            provider.write_text("print('{not-json')\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "output line 1 is not valid JSON"):
                scan_batch(
                    ScanConfig(
                        "p1",
                        "b1",
                        input_dir,
                        output_dir,
                        analysis_provider_command=f"{sys.executable} {provider}",
                    )
                )

    def test_provider_rule_ids_cannot_override_builtin_rules(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot override a built-in rule"):
            validate_provider_rule_id("openability")
        with self.assertRaisesRegex(ValueError, r"provider\.<name>\.<rule>"):
            validate_provider_rule_id("Provider.Fake.Rule")
        validate_provider_rule_id("provider.fake.quality_too_dark")

    def test_provider_metadata_filter_does_not_report_image_or_ocr_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            provider = root / "leaky_provider.py"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "private_page.png", dpi=(300, 300))
            _write_fake_provider(
                provider,
                [
                    {
                        "type": "metadata",
                        "provider": {
                            "name": "fake",
                            "ocr_text": "SECRET_OCR_TEXT",
                            "thumbnail": "data:image/png;base64,SECRET",
                        },
                    },
                    {
                        "type": "finding",
                        "relative_path": "private_page.png",
                        "rule": "provider.fake.signal",
                        "severity": "P2",
                        "confidence": 0.5,
                        "message": "Aggregate local signal.",
                        "metadata": {"ocr_text": "SECRET_OCR_TEXT", "model": "fake"},
                    },
                ],
            )

            report = scan_batch(
                ScanConfig(
                    "p1",
                    "b1",
                    input_dir,
                    output_dir,
                    analysis_provider_command=f"{sys.executable} {provider}",
                )
            )
            paths = write_reports(report, output_dir)
            report_text = paths["json"].read_text(encoding="utf-8") + paths["html"].read_text(encoding="utf-8")

            self.assertNotIn("SECRET_OCR_TEXT", report_text)
            self.assertNotIn("data:image", report_text)
            self.assertEqual(report["findings"][-1]["metadata"], {"model": "fake"})

    def test_rule_registry_covers_current_finding_rules_and_reports_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            manifest_csv = root / "manifest.csv"
            nested_dir = input_dir / "nested"
            input_dir.mkdir()
            nested_dir.mkdir()

            duplicate = input_dir / "DUP_0001.jpg"
            Image.new("RGB", (32, 24), "white").save(duplicate, dpi=(150, 150))
            shutil.copyfile(duplicate, nested_dir / "DUP_0001.jpg")
            (input_dir / "broken.jpg").write_text("not an image", encoding="utf-8")
            Image.new("RGB", (80, 80), (25, 25, 25)).save(input_dir / "dark.png")
            Image.new("RGB", (80, 80), (253, 253, 253)).save(input_dir / "bright.png")
            Image.new("RGB", (80, 80), (128, 128, 128)).save(input_dir / "low_contrast.png")
            _synthetic_text_page().filter(ImageFilter.GaussianBlur(radius=3)).save(input_dir / "blur.png")
            for index in range(2):
                Image.new("RGB", (90, 140), "white").save(input_dir / f"portrait_{index}.png", dpi=(300, 300))
                Image.new("RGB", (140, 90), "white").save(input_dir / f"landscape_{index}.png", dpi=(300, 300))
            manifest_csv.write_text("relative_path\nDUP_0001.jpg\nmissing.png\nmissing.png\n", encoding="utf-8")

            report = scan_batch(
                ScanConfig(
                    "p1",
                    "b1",
                    input_dir,
                    output_dir,
                    name_pattern=r"A001_\d{4}",
                    manifest_csv=manifest_csv,
                )
            )
            paths = write_reports(report, output_dir)
            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            html = paths["html"].read_text(encoding="utf-8")
            actual_rules = {finding["rule"] for finding in report["findings"]}

            self.assertTrue(actual_rules)
            self.assertLessEqual(actual_rules, set(RULE_REGISTRY))
            self.assertEqual(set(saved["rule_catalog"]), set(RULE_REGISTRY))
            self.assertIn("rule_catalog", saved)
            self.assertIn("Rule Catalog", html)
            self.assertIn("Minimum scan resolution", html)

    def test_rule_catalog_is_privacy_safe_static_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            private_name = "private_case_001.png"
            Image.new("RGB", (32, 24), "white").save(input_dir / private_name, dpi=(150, 150))

            report = scan_batch(ScanConfig("private-project", "private-batch", input_dir, output_dir))
            catalog_text = json.dumps(report["rule_catalog"], ensure_ascii=False)
            file_hash = report["files"][0]["sha256"]

            self.assertNotIn(private_name, catalog_text)
            self.assertNotIn(str(input_dir), catalog_text)
            self.assertNotIn(file_hash, catalog_text)
            self.assertNotIn("data:image", catalog_text.lower())

    def test_standards_traceability_doc_exists_and_references_public_sources(self) -> None:
        path = REPO_ROOT / "docs" / "standards-traceability.md"
        text = path.read_text(encoding="utf-8")

        self.assertIn("https://www.saac.gov.cn/daj/tzgg/201709/17b7764728de403fb1c0f0085de6fa61.shtml", text)
        self.assertIn("https://std.samr.gov.cn/hb/search/stdHBDetailed?id=8B1827F24605BB19E05397BE0A0AB44A", text)
        self.assertIn("https://www.ndls.org.cn/standard/detail/701c1aa6791563e848548a7f7199e355", text)
        self.assertIn("rule_registry.py", text)

    def test_rule_registry_uses_clause_numbered_standard_references(self) -> None:
        clause_pattern = re.compile(r"DA/T 31-2017 \d+(?:\.\d+)*")
        core_rules = {
            "openability",
            "dpi_minimum",
            "duplicate_file",
            "manifest_missing_file",
            "manifest_unexpected_file",
            "manifest_duplicate_entry",
            "name_pattern",
            "quality_too_dark",
            "quality_too_bright",
            "quality_low_contrast",
            "quality_suspected_blur",
            "quality_near_blank_page",
            "quality_skew_candidate",
            "quality_dark_border_candidate",
            "quality_scanline_candidate",
            "quality_content_edge_cutoff_candidate",
            "multi_page_image_container",
            "batch_orientation_consistency",
        }

        for rule_id in core_rules:
            with self.subTest(rule_id=rule_id):
                self.assertIn(rule_id, RULE_REGISTRY)
                standards = RULE_REGISTRY[rule_id].standards
                self.assertTrue(any(clause_pattern.search(item) for item in standards))

    def test_standards_traceability_doc_lists_required_clause_numbers(self) -> None:
        path = REPO_ROOT / "docs" / "standards-traceability.md"
        text = path.read_text(encoding="utf-8")
        required_clauses = [
            "DA/T 31-2017 9.5",
            "DA/T 31-2017 10.2",
            "DA/T 31-2017 10.3",
            "DA/T 31-2017 10.4",
            "DA/T 31-2017 10.5.1",
            "DA/T 31-2017 10.5.2",
            "DA/T 31-2017 10.5.3",
            "DA/T 31-2017 10.5.4",
            "DA/T 31-2017 11.2",
            "DA/T 31-2017 12.1.2",
            "DA/T 31-2017 12.2",
        ]

        for clause in required_clauses:
            with self.subTest(clause=clause):
                self.assertIn(clause, text)

    def test_html_report_escapes_embedded_data_and_has_no_remote_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()

            Image.new("RGB", (32, 24), "white").save(input_dir / '<img src=x>.png')

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            paths = write_reports(report, output_dir)
            html = paths["html"].read_text(encoding="utf-8")
            lower_html = html.lower()

            self.assertIn("&lt;img", lower_html)
            self.assertIn("\\u003cimg", lower_html)
            self.assertNotIn("<img", lower_html)
            self.assertNotIn("data:image", lower_html)
            self.assertNotIn('src="http', lower_html)
            self.assertNotIn("src='http", lower_html)

    def test_workers_one_and_default_have_compatible_report_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            serial_out = root / "serial"
            default_out = root / "default"
            input_dir.mkdir()

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (16, 16), "white").save(input_dir / "A001_0002.png", dpi=(150, 150))
            (input_dir / "broken.jpg").write_text("not an image", encoding="utf-8")

            serial = scan_batch(ScanConfig("p1", "b1", input_dir, serial_out, workers=1))
            default = scan_batch(ScanConfig("p1", "b1", input_dir, default_out))

            self.assertEqual(serial["files"], default["files"])
            self.assertEqual(serial["findings"], default["findings"])
            self.assertEqual(serial["summary"]["total_files"], default["summary"]["total_files"])
            self.assertEqual(serial["summary"]["total_findings"], default["summary"]["total_findings"])
            self.assertEqual(serial["summary"]["performance"]["mode"], "serial")

    def test_scan_inspection_opens_each_image_once_for_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (24, 32), "white").save(input_dir / "A001_0002.png", dpi=(300, 300))
            original_open = Image.open
            opened_paths: list[str] = []

            def counted_open(fp, *args, **kwargs):
                opened_paths.append(str(fp))
                return original_open(fp, *args, **kwargs)

            with mock.patch("archive_scan_qc.scanner.Image.open", side_effect=counted_open):
                report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=1))

            self.assertEqual(report["summary"]["openable_files"], 2)
            self.assertEqual(len(opened_paths), 2)
            self.assertEqual(
                sorted(Path(path).name for path in opened_paths),
                ["A001_0001.jpg", "A001_0002.png"],
            )

    def test_multi_worker_scan_output_and_findings_order_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()

            for name in ["B_0002.png", "a_0001.jpg", "nested/C_0003.png"]:
                path = input_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (24, 24), "white").save(path, dpi=(150, 150))
            (input_dir / "bad.png").write_text("not an image", encoding="utf-8")

            first = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=4))
            second = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=4))

            self.assertEqual([item["relative_path"] for item in first["files"]], sorted(item["relative_path"] for item in first["files"]))
            self.assertEqual(first["files"], second["files"])
            self.assertEqual(first["findings"], second["findings"])
            self.assertEqual(first["summary"]["performance"]["mode"], "parallel")

    def test_flags_unopenable_and_duplicate_hashes_without_cross_directory_name_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            nested_dir = input_dir / "nested"
            nested_dir.mkdir(parents=True)

            source = input_dir / "DUP_0001.jpg"
            Image.new("RGB", (12, 12), "white").save(source, dpi=(300, 300))
            shutil.copyfile(source, nested_dir / "DUP_0001.jpg")
            (input_dir / "broken.jpg").write_text("not an image", encoding="utf-8")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertIn("openability", rules)
            self.assertNotIn("duplicate_name", rules)
            self.assertIn("duplicate_file", rules)
            self.assertGreaterEqual(report["summary"]["p0_findings"], 2)

    def test_manifest_flags_missing_unexpected_and_duplicate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            manifest_csv = root / "manifest.csv"

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0002.jpg", dpi=(300, 300))
            manifest_csv.write_text(
                "relative_path\n"
                "A001_0001.jpg\n"
                "A001_0001.jpg\n"
                "A001_0003.jpg\n",
                encoding="utf-8",
            )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, manifest_csv=manifest_csv))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertIn("manifest_missing_file", rules)
            self.assertIn("manifest_unexpected_file", rules)
            self.assertIn("manifest_duplicate_entry", rules)
            self.assertEqual(report["summary"]["manifest_entry_count"], 3)
            self.assertEqual(report["summary"]["manifest_unique_entry_count"], 2)
            self.assertEqual(report["summary"]["manifest_missing_count"], 1)
            self.assertEqual(report["summary"]["manifest_unexpected_count"], 1)
            self.assertEqual(report["summary"]["manifest_duplicate_count"], 1)
            self.assertTrue(report["manifest"]["manifest_used"])
            self.assertEqual(report["manifest"]["manifest_entry_count"], 3)

            paths = write_reports(report, output_dir)
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Manifest Entries", html)
            self.assertIn("Manifest Missing", html)
            self.assertIn("manifest_unexpected_file", html)

    def test_cli_accepts_manifest_and_returns_one_for_p0_manifest_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            manifest_csv = root / "manifest.csv"

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path\nA001_0002.jpg\n", encoding="utf-8")

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--manifest-csv",
                    str(manifest_csv),
                ]
            )

            self.assertEqual(exit_code, 1)
            saved = json.loads((output_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["manifest"]["manifest_used"])
            self.assertEqual(saved["summary"]["manifest_missing_count"], 1)
            self.assertEqual(saved["summary"]["manifest_unexpected_count"], 1)

    def test_review_export_template_fields_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "scan_qc_report.json"
            csv_path = root / "review_template.csv"
            json_path = root / "review_template.json"
            report_path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "relative_path": "private/A001_0001.tif",
                                "rule": "dpi_minimum",
                                "severity": "P0",
                                "message": "private detail",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            write_review_export(report_path, csv_path)
            write_review_export(report_path, json_path)

            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(
                reader.fieldnames,
                ["finding_id", "rule", "severity", "relative_path", "status", "reviewer_notes"],
            )
            self.assertEqual(rows[0]["finding_id"], "F000001")
            self.assertEqual(rows[0]["status"], "pending")
            self.assertEqual(rows[0]["relative_path"], "private/A001_0001.tif")

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], "scan-qc.review-template.v1")
            self.assertEqual(payload["findings"][0]["finding_id"], "F000001")
            self.assertIn("allowed_statuses", payload)

    def test_review_summary_counts_statuses_and_remaining_p0_p1(self) -> None:
        summary = build_review_summary(
            [
                {"finding_id": "F000001", "rule": "dpi_minimum", "severity": "P0", "status": "fixed"},
                {"finding_id": "F000002", "rule": "openability", "severity": "P0", "status": "accepted"},
                {"finding_id": "F000003", "rule": "quality_too_dark", "severity": "P1", "status": "needs_rescan"},
                {"finding_id": "F000004", "rule": "quality_low_contrast", "severity": "P2", "status": "false_positive"},
            ]
        )

        self.assertEqual(summary["total_findings"], 4)
        self.assertEqual(summary["severity_counts"], {"P0": 2, "P1": 1, "P2": 1})
        self.assertEqual(summary["rule_counts"]["dpi_minimum"], 1)
        self.assertEqual(summary["status_counts"]["fixed"], 1)
        self.assertEqual(summary["status_counts"]["accepted"], 1)
        self.assertEqual(summary["status_counts"]["needs_rescan"], 1)
        self.assertEqual(summary["remaining_p0"], 1)
        self.assertEqual(summary["remaining_p1"], 1)
        self.assertEqual(summary["manually_handled_count"], 4)
        self.assertEqual(
            summary["closure_gate_summary"],
            {
                "open_p0_count": 1,
                "open_p1_count": 1,
                "manually_handled_count": 4,
                "can_complete_delivery": False,
                "operator_message_zh": "还有需要重扫/重新处理的图片，先处理后再完成导出。",
            },
        )
        self.assertFalse(summary["acceptance_passed"])

    def test_review_summary_is_aggregate_only_and_does_not_leak_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            review_path = root / "review_template.csv"
            summary_path = root / "review_summary.json"
            review_path.write_text(
                "finding_id,rule,severity,relative_path,status,reviewer_notes,sha256\n"
                "F000001,dpi_minimum,P0,private/name_001.tif,fixed,contains private note,abc123\n",
                encoding="utf-8",
            )

            write_review_summary(review_path, summary_path)
            raw = summary_path.read_text(encoding="utf-8")

            self.assertIn("scan-qc.review-summary.v1", raw)
            for forbidden in ["private/name_001.tif", "contains private note", "abc123", "relative_path", "sha256"]:
                self.assertNotIn(forbidden, raw)
            summary = json.loads(raw)
            self.assertTrue(summary["acceptance_passed"])
            self.assertEqual(summary["remaining_p0"], 0)
            self.assertTrue(summary["closure_gate_summary"]["can_complete_delivery"])

    def test_review_summary_rejects_invalid_status_with_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid review status 'done'.*expected one of"):
            build_review_summary(
                [
                    {
                        "finding_id": "F000001",
                        "rule": "dpi_minimum",
                        "severity": "P0",
                        "status": "done",
                    }
                ]
            )

    def test_empty_review_findings_generate_passing_summary(self) -> None:
        summary = build_review_summary([])

        self.assertEqual(summary["total_findings"], 0)
        self.assertEqual(summary["remaining_p0"], 0)
        self.assertEqual(summary["remaining_p1"], 0)
        self.assertTrue(summary["acceptance_passed"])

    def test_review_decisions_verify_passes_complete_aggregate_summary(self) -> None:
        summary = build_review_decision_verification_summary(_review_decision_export_fixture())

        self.assertEqual(summary["schema_version"], "scan-qc.review-decision-verification-summary.v1")
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["decision_summary"]["total_decisions"], 3)
        self.assertEqual(summary["decision_summary"]["pending"], 0)
        self.assertEqual(summary["decision_summary"]["accepted"], 1)
        self.assertEqual(summary["decision_summary"]["rejected"], 1)
        self.assertEqual(summary["decision_summary"]["rework"], 1)
        self.assertEqual(summary["decision_summary"]["completion_status"], "complete")
        self.assertEqual(
            summary["decision_summary"]["closure_gate_summary"],
            {
                "open_p0_count": 0,
                "open_p1_count": 0,
                "manually_handled_count": 3,
                "can_complete_delivery": True,
                "operator_message_zh": "P0/P1 问题已经有处理结论，可以完成交接。",
            },
        )
        self.assertEqual(summary["blocking_counts_by_code"], {})
        self.assertTrue(summary["privacy"]["aggregate_only"])

    def test_review_decisions_verify_treats_zero_review_items_as_complete(self) -> None:
        result = build_review_decision_verification_summary(_review_decision_export_fixture(decisions=()))

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["decision_summary"]["total_decisions"], 0)
        self.assertEqual(result["decision_summary"]["pending"], 0)
        self.assertEqual(result["decision_summary"]["completion_status"], "complete")

    def test_review_decisions_verify_allows_incomplete_without_blocking(self) -> None:
        fixture = _review_decision_export_fixture(decisions=("accepted_issue", "pending", "needs_rescan"))
        result = build_review_decision_verification_summary(fixture)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["decision_summary"]["pending"], 1)
        self.assertEqual(result["decision_summary"]["completion_status"], "incomplete")
        self.assertEqual(result["decision_summary"]["closure_gate_summary"]["open_p0_count"], 1)
        self.assertFalse(result["decision_summary"]["closure_gate_summary"]["can_complete_delivery"])

    def test_review_decisions_verify_blocks_invalid_decision_value(self) -> None:
        fixture = _review_decision_export_fixture(decisions=("accepted_issue", "done", "blocked"))
        result = build_review_decision_verification_summary(fixture)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocking_counts_by_code"]["unknown_decision_value"], 1)
        self.assertNotIn("RID0002", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("done", json.dumps(result, ensure_ascii=False))

    def test_review_decisions_verify_blocks_count_mismatch(self) -> None:
        fixture = _review_decision_export_fixture()
        fixture["source_target_count"] = 4
        fixture["review_counts"]["accepted_issue"] = 99
        fixture["aggregate_counts"]["review_completion"]["reviewed"] = 2

        result = build_review_decision_verification_summary(fixture)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocking_counts_by_code"]["source_target_count_mismatch"], 1)
        self.assertEqual(result["blocking_counts_by_code"]["review_count_mismatch"], 1)
        self.assertEqual(result["blocking_counts_by_code"]["review_completion_count_mismatch"], 1)

    def test_review_decisions_verify_blocks_private_fields_by_code_only(self) -> None:
        fixture = _review_decision_export_fixture()
        fixture["decisions"][0]["preview_filename"] = "private_scan_001.tif"
        fixture["sha256"] = "abc123-private-hash"

        result = build_review_decision_verification_summary(fixture)
        raw = json.dumps(result, ensure_ascii=False)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocking_counts_by_code"]["privacy_sensitive_field"], 2)
        self.assertFalse(result["privacy"]["aggregate_only"])
        self.assertNotIn("private_scan_001.tif", raw)
        self.assertNotIn("abc123-private-hash", raw)
        self.assertNotIn("preview_filename", raw)
        self.assertNotIn("sha256", raw)

    def test_review_decisions_verify_blocks_sensitive_field_name_variants_by_code_only(self) -> None:
        fixture = _review_decision_export_fixture(decisions=("accepted_issue",))
        fixture["decisions"][0]["image_path"] = "/private/archive/card-001.png"
        fixture["decisions"][0]["file_name"] = "card-001.png"
        fixture["decisions"][0]["sourceImageObjectUrl"] = "blob:https://local.invalid/private"
        fixture["decisions"][0]["ocrText"] = "private OCR text"
        fixture["decisions"][0]["reviewer_note"] = "private reviewer note"

        result = build_review_decision_verification_summary(fixture)
        raw = json.dumps(result, ensure_ascii=False)

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["privacy"]["aggregate_only"])
        self.assertGreater(result["blocking_counts_by_code"]["privacy_sensitive_field"], 0)
        for sensitive_fragment in (
            "image_path",
            "file_name",
            "sourceImageObjectUrl",
            "ocrText",
            "reviewer_note",
            "/private/archive/card-001.png",
            "card-001.png",
            "blob:https://local.invalid/private",
            "private OCR text",
            "private reviewer note",
        ):
            self.assertNotIn(sensitive_fragment, raw)

    def test_review_decisions_verify_cli_smoke_writes_aggregate_only_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-decisions-") as temp_dir:
            root = Path(temp_dir)
            input_path = root / "scan-qc-review-decisions.summary.json"
            output_path = root / "review_decision_verification_summary.json"
            input_path.write_text(json.dumps(_review_decision_export_fixture()), encoding="utf-8")

            self.assertEqual(
                main(["review-decisions-verify", "--summary", str(input_path), "--out", str(output_path)]),
                0,
            )
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["decision_summary"]["total_decisions"], 3)

    def test_rules_calibration_without_review_generates_rule_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "scan_qc_report.json"
            output_path = root / "rules_calibration_summary.json"
            _write_minimal_scan_report(
                report_path,
                [
                    {"relative_path": "private/A001.png", "rule": "dpi_minimum", "severity": "P0", "message": "low dpi"},
                    {"relative_path": "private/A002.png", "rule": "quality_too_dark", "severity": "P1", "message": "dark"},
                ],
            )

            exit_code = main(["calibrate-rules", "--report", str(report_path), "--out", str(output_path)])

            self.assertEqual(exit_code, 0)
            summary = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], "scan-qc.rules-calibration.v1")
            self.assertEqual(summary["rules"]["dpi_minimum"]["trigger_count"], 1)
            self.assertEqual(summary["rules"]["dpi_minimum"]["severity_distribution"]["P0"], 1)
            self.assertEqual(summary["rules"]["quality_too_dark"]["trigger_count"], 1)
            self.assertEqual(summary["rules"]["quality_too_dark"]["recommendation"]["action"], "need_more_samples")

    def test_rules_calibration_with_review_summary_generates_status_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "scan_qc_report.json"
            review_summary_path = root / "review_summary.json"
            output_path = root / "rules_calibration_summary.json"
            _write_minimal_scan_report(
                report_path,
                [
                    {"relative_path": f"private/A00{index}.png", "rule": "quality_too_dark", "severity": "P1", "message": "dark"}
                    for index in range(5)
                ],
            )
            review_summary_path.write_text(
                json.dumps(
                    build_review_summary(
                        [
                            {"finding_id": f"F{index:06d}", "rule": "quality_too_dark", "severity": "P1", "status": "false_positive"}
                            for index in range(1, 6)
                        ]
                    )
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                main(
                    [
                        "calibrate-rules",
                        "--report",
                        str(report_path),
                        "--review-summary",
                        str(review_summary_path),
                        "--out",
                        str(output_path),
                    ]
                ),
                0,
            )

            summary = json.loads(output_path.read_text(encoding="utf-8"))
            statuses = summary["rules"]["quality_too_dark"]["review_status_distribution"]
            self.assertEqual(statuses["false_positive"], 5)
            self.assertEqual(summary["rules"]["quality_too_dark"]["recommendation"]["action"], "loosen")

    def test_rules_calibration_suggested_profile_does_not_overwrite_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "scan_qc_report.json"
            profile_path = root / "rules.json"
            suggested_path = root / "rules.suggested.json"
            output_path = root / "rules_calibration_summary.json"
            original_profile = {"name": "local-standard", "version": "1", "min_dpi": 300}
            profile_path.write_text(json.dumps(original_profile), encoding="utf-8")
            _write_minimal_scan_report(report_path, [], rules_profile=load_rules_profile(profile_path).metadata())

            exit_code = main(
                [
                    "calibrate-rules",
                    "--report",
                    str(report_path),
                    "--out",
                    str(output_path),
                    "--write-suggested-profile",
                    str(suggested_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(profile_path.read_text(encoding="utf-8")), original_profile)
            suggested = json.loads(suggested_path.read_text(encoding="utf-8"))
            self.assertTrue(suggested["draft"])
            self.assertTrue(suggested["suggested"])
            self.assertEqual(suggested["name"], "local-standard-suggested")
            self.assertNotIn("source", suggested)
            loaded_suggested = load_rules_profile(suggested_path)
            self.assertEqual(loaded_suggested.min_dpi, 300)

    def test_rules_calibration_summary_does_not_leak_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "scan_qc_report.json"
            review_path = root / "review_template.csv"
            output_path = root / "rules_calibration_summary.json"
            private_path = "secret-folder/private_case_001.png"
            private_hash = "abc123privatehash"
            private_note = "operator note mentions private person"
            _write_minimal_scan_report(
                report_path,
                [{"relative_path": private_path, "rule": "dpi_minimum", "severity": "P0", "message": "private message"}],
                files=[{"relative_path": private_path, "sha256": private_hash}],
            )
            review_path.write_text(
                "finding_id,rule,severity,relative_path,status,reviewer_notes\n"
                f"F000001,dpi_minimum,P0,{private_path},fixed,{private_note}\n",
                encoding="utf-8",
            )

            self.assertEqual(
                main(["calibrate-rules", "--report", str(report_path), "--review", str(review_path), "--out", str(output_path)]),
                0,
            )

            raw = output_path.read_text(encoding="utf-8")
            self.assertIn("scan-qc.rules-calibration.v1", raw)
            for forbidden in [private_path, "private_case_001", private_hash, private_note, "private message", str(report_path)]:
                self.assertNotIn(forbidden, raw)

    def test_rules_calibration_invalid_input_errors_are_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad_report = root / "bad.json"
            output_path = root / "rules_calibration_summary.json"
            bad_report.write_text("{bad json", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(["calibrate-rules", "--report", str(bad_report), "--out", str(output_path)])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("Scan QC report JSON is invalid", stderr.getvalue())
            self.assertFalse(output_path.exists())

    def test_acceptance_sampling_default_samples_at_least_five_percent(self) -> None:
        files = [_sample_file(index) for index in range(40)]
        payload = build_acceptance_sampling_export({"files": files, "findings": []})

        self.assertEqual(payload["selection"]["sample_ratio"], 0.05)
        self.assertEqual(payload["selection"]["sampled_records"], 2)
        counts = payload["aggregate_sampling_counts"]
        self.assertEqual(counts["input_total"], 40)
        self.assertEqual(counts["target_sample_ratio"], 0.05)
        self.assertEqual(counts["target_sample_count"], 2)
        self.assertEqual(counts["generated_sample_task_count"], 2)
        self.assertEqual(counts["reviewed_sample_count"], 0)
        self.assertEqual(counts["pending_sample_count"], 2)
        self.assertTrue(counts["sample_task_target_met"])
        self.assertFalse(counts["sampling_target_met"])
        self.assertGreaterEqual(counts["effective_sample_ratio"], 0.05)
        self.assertEqual(payload["sensitivity"], "sensitive_local_evidence")

    def test_acceptance_sampling_is_deterministic_and_risk_prioritized(self) -> None:
        files = [_sample_file(index) for index in range(20)]
        findings = [
            {"relative_path": "batch/page-019.tif", "rule": "dpi_minimum", "severity": "P0", "message": "low dpi"},
            {"relative_path": "batch/page-018.tif", "rule": "quality_too_dark", "severity": "P1", "message": "dark"},
            {"relative_path": "batch/page-017.tif", "rule": "quality_skew_candidate", "severity": "P2", "message": "skew"},
        ]

        first = build_acceptance_sampling_export({"files": files, "findings": findings})
        second = build_acceptance_sampling_export({"files": list(reversed(files)), "findings": findings})

        first_paths = [row["relative_path"] for row in first["samples"]]
        second_paths = [row["relative_path"] for row in second["samples"]]
        self.assertEqual(first_paths, second_paths)
        self.assertEqual(first_paths[:3], ["batch/page-019.tif", "batch/page-018.tif", "batch/page-017.tif"])
        self.assertEqual(first["samples"][0]["selection_reason"], "risk_weighted_p0_finding")
        self.assertEqual(first["aggregate_sampling_counts"]["sampled_by_risk_tier"]["p0"], 1)

    def test_acceptance_sampling_cli_writes_json_and_csv_with_privacy_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "scan_qc_report.json"
            out_dir = root / "sampling"
            private_path = "secret-folder/private_case_001.tif"
            private_hash = "abc123privatehash"
            _write_minimal_scan_report(
                report_path,
                [{"relative_path": private_path, "rule": "dpi_minimum", "severity": "P0", "message": "private message"}],
                files=[_sample_file(1, relative_path=private_path, sha256=private_hash)],
            )

            self.assertEqual(main(["acceptance-sampling-export", "--report", str(report_path), "--out", str(out_dir)]), 0)

            json_path = out_dir / "acceptance_sampling_review.json"
            csv_path = out_dir / "acceptance_sampling_review.csv"
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            csv_text = csv_path.read_text(encoding="utf-8")
            self.assertTrue(payload["privacy"]["sensitive_local_evidence"])
            self.assertFalse(payload["privacy"]["aggregate_only"])
            self.assertEqual(payload["aggregate_sampling_counts"]["input_total"], 1)
            self.assertEqual(payload["aggregate_sampling_counts"]["target_sample_count"], 1)
            self.assertEqual(payload["aggregate_sampling_counts"]["generated_sample_task_count"], 1)
            self.assertEqual(payload["aggregate_sampling_counts"]["reviewed_sample_count"], 0)
            self.assertFalse(payload["aggregate_sampling_counts"]["sampling_target_met"])
            self.assertIn("image bytes", payload["privacy"]["omits"])
            self.assertIn(private_path, csv_text)
            self.assertIn(private_hash, csv_text)
            self.assertNotIn("private message", json_path.read_text(encoding="utf-8"))

    def test_acceptance_sampling_rejects_ratio_below_five_percent(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 5.00%"):
            build_acceptance_sampling_export({"files": [_sample_file(1)], "findings": []}, sample_ratio=0.01)

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

    def test_benchmark_writes_privacy_safe_json_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "benchmark"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "private_name_001.png", dpi=(300, 300))
            (input_dir / "private_broken.png").write_text("not an image", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "benchmark",
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--workers-list",
                        "1",
                        "--scan-only",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("Benchmark recommendation: scan workers=1", stdout.getvalue())
            json_path = output_dir / "benchmark_results.json"
            csv_path = output_dir / "benchmark_results.csv"
            self.assertTrue(json_path.exists())
            self.assertTrue(csv_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIn("recommendations", payload)
            self.assertEqual(payload["recommendations"]["generated_from_runs"], 1)
            self.assertEqual(payload["recommendations"]["scan_only"]["best_requested_workers"], 1)
            self.assertEqual(payload["recommendations"]["scan_only"]["best_effective_workers"], 1)
            self.assertIsNone(payload["recommendations"]["processing"])
            self.assertEqual(len(payload["runs"]), 1)
            run = payload["runs"][0]
            self.assertEqual(run["total_files"], 2)
            self.assertEqual(run["openable_files"], 1)
            self.assertEqual(run["finding_rule_counts"]["openability"], 1)
            self.assertEqual(run["finding_rule_counts"]["quality_near_blank_page"], 1)
            self.assertTrue(run["scan_only"])
            raw_json = json_path.read_text(encoding="utf-8")
            raw_csv = csv_path.read_text(encoding="utf-8")
            for forbidden in [
                "private_name_001.png",
                "private_broken.png",
                "filename",
                "relative_path",
                "sha256",
                '"files": [',
                '"findings": [',
            ]:
                self.assertNotIn(forbidden, raw_json)
                self.assertNotIn(forbidden, raw_csv)

            with csv_path.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["total_files"], "2")
            self.assertEqual(rows[0]["openable_files"], "1")
            self.assertEqual(payload["comparison_plan"]["schema_version"], "scan-qc.performance-comparison-plan.v1")
            self.assertIn("private_puersai_policy", payload["comparison_plan"]["privacy_boundary"])
            self.assertIn("pillow_version", payload["environment"])

    def test_benchmark_comparison_plan_makes_production_decision(self) -> None:
        plan = _comparison_plan(
            [
                _benchmark_run_stub(1, 1, 1, scan_rate=100.0, processing_rate=40.0),
                _benchmark_run_stub(2, 1, 2, scan_rate=120.0, processing_rate=55.0),
            ]
        )

        self.assertEqual(plan["schema_version"], "scan-qc.performance-comparison-plan.v1")
        self.assertEqual(
            [path["id"] for path in plan["paths"]],
            [
                "pillow_cpu_baseline",
                "numpy_vectorized_hotspots",
                "libvips_streaming_io",
                "gpu_model_providers",
            ],
        )
        self.assertEqual(plan["production_decision"]["worth_implementing_next"], "processing_worker_tuning")
        self.assertIn("processed images/min", plan["production_decision"]["reason"])

    def test_synthetic_performance_summary_ranks_variants(self) -> None:
        module = _load_synthetic_performance_module()
        variants = [
            {
                "id": "pillow_cpu_baseline",
                "label": "Current Python/Pillow CPU baseline",
                "best_processing_images_per_minute": 100.0,
            },
            {
                "id": "numpy_vectorized_hotspots",
                "label": "NumPy vectorized despeckle hotspot when available",
                "best_processing_images_per_minute": 125.0,
            },
        ]

        decision = module._production_decision(variants)

        self.assertEqual(decision["worth_implementing_next"], "numpy_vectorized_hotspots")
        self.assertIn("private aggregate validation", decision["reason"])

    def test_synthetic_performance_summary_promotes_operation_timing_regression_signal(self) -> None:
        module = _load_synthetic_performance_module()
        benchmark = {
            "schema_version": "scan-qc.benchmark.v1",
            "environment": {"python_version": "3.12", "platform": "test"},
            "comparison_plan": {"schema_version": "scan-qc.performance-comparison-plan.v1"},
            "recommendations": {
                "processing": {"files_per_minute": 120.0, "best_requested_workers": 1},
            },
            "runs": [
                {
                    "finding_severity_counts": {"P0": 0, "P1": 0, "P2": 0},
                    "processing": {
                        "failed_files": 0,
                        "operation_timings": {
                            "deskew": {
                                "enabled": True,
                                "file_count": 8,
                                "elapsed_seconds": 0.4,
                                "reused_scan_measurement_files": 8,
                                "safe_skip_files": 6,
                                "projection_detection_files": 2,
                                "fallback_detection_files": 1,
                            },
                            "despeckle": {
                                "enabled": True,
                                "file_count": 8,
                                "elapsed_seconds": 1.2,
                                "backend_counts": {
                                    "numpy": 8,
                                    "fallback": 0,
                                    "not_applicable": 0,
                                    "unknown": 0,
                                },
                            },
                        },
                        "source_path": "/Users/private/archive/private_page_0001.png",
                        "source_sha256": "a" * 64,
                    },
                },
                {
                    "finding_severity_counts": {"P0": 0, "P1": 0, "P2": 0},
                    "processing": {
                        "failed_files": 0,
                        "operation_timings": {
                            "deskew": {
                                "enabled": True,
                                "file_count": 8,
                                "elapsed_seconds": 0.2,
                                "reused_scan_measurement_files": 8,
                                "safe_skip_files": 4,
                                "projection_detection_files": 4,
                                "fallback_detection_files": 2,
                            },
                            "despeckle": {
                                "enabled": True,
                                "file_count": 8,
                                "elapsed_seconds": 0.8,
                                "backend_counts": {
                                    "numpy": 0,
                                    "fallback": 8,
                                    "not_applicable": 0,
                                    "unknown": 0,
                                },
                            },
                        },
                    },
                },
            ],
        }
        for run in benchmark["runs"]:
            operation_timings = run["processing"]["operation_timings"]
            for operation in module.REGRESSION_SIGNAL_OPERATIONS:
                operation_timings.setdefault(
                    operation,
                    {
                        "enabled": False,
                        "file_count": 0,
                        "elapsed_seconds": 0.0,
                        "files_per_minute": 0.0,
                        "average_seconds_per_file": None,
                    },
                )

        summary = module._variant_summary({"id": "candidate", "label": "Candidate"}, benchmark)
        raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        signal = summary["operation_timing_regression_signal"]
        self.assertTrue(signal["aggregate_only"])
        self.assertEqual(signal["required_operations"], list(module.REGRESSION_SIGNAL_OPERATIONS))
        self.assertTrue(signal["signal_available"])
        self.assertEqual(signal["missing_operations"], [])
        self.assertEqual(set(signal["operations"]), set(module.REGRESSION_SIGNAL_OPERATIONS))
        self.assertEqual(signal["operations"]["deskew"]["run_count"], 2)
        self.assertEqual(signal["operations"]["deskew"]["file_count"], 16)
        self.assertEqual(signal["operations"]["deskew"]["elapsed_seconds"], 0.6)
        self.assertEqual(signal["operations"]["deskew"]["average_seconds_per_file"], 0.0375)
        self.assertEqual(signal["operations"]["deskew"]["reused_scan_measurement_files"], 16)
        self.assertEqual(signal["operations"]["deskew"]["safe_skip_files"], 10)
        self.assertEqual(signal["operations"]["deskew"]["projection_detection_files"], 6)
        self.assertEqual(signal["operations"]["deskew"]["fallback_detection_files"], 3)
        despeckle = signal["operations"]["despeckle"]
        self.assertEqual(despeckle["elapsed_seconds"], 2.0)
        self.assertEqual(despeckle["backend_mode"], "mixed")
        self.assertTrue(despeckle["numpy_available"])
        self.assertEqual(despeckle["backend_counts"]["numpy"], 8)
        self.assertEqual(despeckle["backend_counts"]["fallback"], 8)
        self.assertNotIn("/Users/private/archive", raw)
        self.assertNotIn("private_page_0001.png", raw)
        self.assertNotIn("a" * 64, raw)

    def test_synthetic_performance_summary_reports_missing_operation_timing_signal(self) -> None:
        module = _load_synthetic_performance_module()
        summary = module._variant_summary(
            {"id": "scan_only", "label": "Scan only"},
            {
                "schema_version": "scan-qc.benchmark.v1",
                "environment": {},
                "comparison_plan": {},
                "recommendations": {"processing": None},
                "runs": [{"finding_severity_counts": {}, "processing": {"failed_files": 0}}],
            },
        )

        signal = summary["operation_timing_regression_signal"]
        self.assertFalse(signal["signal_available"])
        self.assertEqual(signal["missing_operations"], list(module.REGRESSION_SIGNAL_OPERATIONS))
        self.assertFalse(signal["operations"]["deskew"]["signal_available"])
        self.assertEqual(
            signal["operations"]["deskew"]["missing_reason"],
            "missing_from_benchmark_processing_operation_timings",
        )
        self.assertEqual(set(signal["operations"]), set(module.REGRESSION_SIGNAL_OPERATIONS))
        self.assertFalse(signal["operations"]["despeckle"]["signal_available"])

    def test_benchmark_workers_list_order_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "benchmark"
            input_dir.mkdir()
            for index in range(3):
                Image.new("RGB", (32, 24), "white").save(input_dir / f"page_{index}.png", dpi=(300, 300))

            exit_code = main(
                [
                    "benchmark",
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers-list",
                    "1,2",
                    "--scan-only",
                ]
            )

            self.assertEqual(exit_code, 0)
            payload = json.loads((output_dir / "benchmark_results.json").read_text(encoding="utf-8"))
            self.assertEqual([run["requested_workers"] for run in payload["runs"]], [1, 2])
            self.assertEqual([run["run_index"] for run in payload["runs"]], [1, 2])
            self.assertEqual(
                [point["requested_workers"] for point in payload["recommendations"]["scan_only"]["workers"]],
                [1, 2],
            )

    def test_benchmark_processing_options_generate_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "benchmark"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (40, 30), "white").save(input_dir / "private_processed.png", dpi=(300, 300))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "benchmark",
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--process-out",
                        str(process_dir),
                        "--workers-list",
                        "1",
                        "--auto-crop",
                        "--deskew",
                        "--trim-dark-border",
                        "--despeckle",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("Benchmark recommendation: processing workers=1", stdout.getvalue())
            payload = json.loads((output_dir / "benchmark_results.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["recommendations"]["processing"]["best_requested_workers"], 1)
            self.assertEqual(payload["recommendations"]["processing"]["best_effective_workers"], 1)
            self.assertIsNotNone(payload["recommendations"]["processing"]["files_per_minute"])
            run = payload["runs"][0]
            self.assertFalse(run["scan_only"])
            self.assertTrue(run["operations"]["auto_crop"])
            self.assertTrue(run["operations"]["deskew"])
            self.assertEqual(run["processing"]["processed_files"], 1)
            self.assertIsNotNone(run["processing"]["elapsed_seconds"])
            self.assertIn("operation_timings", run["processing"])
            self.assertEqual(run["processing"]["operation_timings"]["auto_crop"]["file_count"], 1)
            self.assertEqual(run["processing"]["operation_timings"]["deskew"]["file_count"], 1)
            quality = run["processing"]["quality_regression"]
            self.assertEqual(quality["operation_timing_integrity"]["status"], "pass")
            self.assertEqual(quality["operation_timing_integrity"]["missing_operations"], [])
            self.assertGreaterEqual(len(quality["slow_operations"]), 1)
            self.assertIn("average_seconds_per_file", quality["slow_operations"][0])
            self.assertFalse(any(process_dir.glob("*/processing_manifest.json")))

    def test_benchmark_recommendations_rank_workers_and_flag_diminishing_returns(self) -> None:
        runs = [
            _benchmark_run_stub(1, 1, 1, scan_rate=100.0, processing_rate=40.0),
            _benchmark_run_stub(2, 1, 2, scan_rate=105.0, processing_rate=80.0),
            _benchmark_run_stub(3, 1, 4, scan_rate=102.0, processing_rate=82.0),
        ]

        recommendations = _recommendations(runs)

        scan = recommendations["scan_only"]
        self.assertEqual(scan["best_requested_workers"], 2)
        self.assertTrue(scan["diminishing_returns"])
        self.assertIn("1 -> 2", scan["notes"][0])

        processing = recommendations["processing"]
        self.assertEqual(processing["best_requested_workers"], 4)
        self.assertTrue(processing["diminishing_returns"])
        self.assertIn("2 -> 4", processing["notes"][0])

    def test_benchmark_invalid_workers_and_repeats_are_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "benchmark"
            input_dir.mkdir()

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(["benchmark", "--input", str(input_dir), "--out", str(output_dir), "--workers-list", "1,0"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("--workers-list must be a positive integer", stderr.getvalue())

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "benchmark",
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--workers-list",
                        "1",
                        "--repeats",
                        "0",
                    ]
                )
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("--repeats must be a positive integer", stderr.getvalue())

    def test_output_dir_inside_input_is_skipped_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = input_dir / "reports"
            input_dir.mkdir()

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            first_report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            write_reports(first_report, output_dir)
            second_report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))

            scanned_paths = {item["relative_path"] for item in second_report["files"]}
            self.assertEqual(scanned_paths, {"A001_0001.jpg"})
            self.assertEqual(second_report["summary"]["total_files"], 1)
            self.assertEqual(second_report["summary"]["skipped_output_directory_count"], 1)
            self.assertFalse(any(path.startswith("reports/") for path in scanned_paths))

    def test_manifest_csv_inside_input_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            manifest_csv = input_dir / "manifest.csv"

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path\nA001_0001.jpg\n", encoding="utf-8")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, manifest_csv=manifest_csv))

            scanned_paths = {item["relative_path"] for item in report["files"]}
            rules = {finding["rule"] for finding in report["findings"]}
            self.assertEqual(scanned_paths, {"A001_0001.jpg"})
            self.assertNotIn("unsupported_format", rules)
            self.assertEqual(report["summary"]["manifest_unexpected_count"], 0)
            self.assertEqual(report["summary"]["skipped_manifest_file_count"], 1)

    def test_quality_metrics_do_not_flag_synthetic_normal_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().save(input_dir / "A001_0001.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules = {finding["rule"] for finding in report["findings"]}
            file_record = report["files"][0]

            self.assertNotIn("quality_too_dark", rules)
            self.assertNotIn("quality_too_bright", rules)
            self.assertNotIn("quality_low_contrast", rules)
            self.assertNotIn("quality_suspected_blur", rules)
            self.assertGreater(file_record["quality_brightness_mean"], 0)
            self.assertGreater(file_record["quality_contrast_stddev"], 0)
            self.assertGreater(file_record["quality_sharpness_laplacian_var"], 0)

    def test_processing_quality_findings_flag_skew_and_dark_border_without_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().rotate(-3.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white").save(
                input_dir / "skew.png",
                dpi=(300, 300),
            )
            _synthetic_dark_border_page().save(input_dir / "border.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            paths = write_reports(report, output_dir)
            rules_by_path: dict[str, set[str]] = {}
            for finding in report["findings"]:
                rules_by_path.setdefault(finding["relative_path"], set()).add(finding["rule"])
            records = {record["relative_path"]: record for record in report["files"]}

            self.assertIn("quality_skew_candidate", rules_by_path["skew.png"])
            self.assertIn("quality_dark_border_candidate", rules_by_path["border.png"])
            self.assertAlmostEqual(records["skew.png"]["quality_skew_angle_degrees"], -3.0, delta=0.75)
            self.assertGreaterEqual(records["skew.png"]["quality_skew_confidence"], 0.08)
            self.assertIsNotNone(records["border.png"]["quality_dark_border_bbox"])
            self.assertFalse((root / "processed").exists())

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertIn("quality_skew_candidate", saved["rule_catalog"])
            self.assertIn("quality_dark_border_bbox", paths["files_csv"].read_text(encoding="utf-8"))
            self.assertIn("quality_dark_border_candidate", paths["findings_csv"].read_text(encoding="utf-8"))
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Page skew candidate", html)
            self.assertIn("Dark scan border candidate", html)

    def test_processing_quality_findings_stay_quiet_on_clean_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().save(input_dir / "clean.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules = {finding["rule"] for finding in report["findings"]}
            record = report["files"][0]

            self.assertNotIn("quality_skew_candidate", rules)
            self.assertNotIn("quality_dark_border_candidate", rules)
            self.assertIsNone(record["quality_dark_border_bbox"])

    def test_scanline_quality_findings_flag_horizontal_and_vertical_streaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_scanline_page("horizontal").save(input_dir / "horizontal.png", dpi=(300, 300))
            _synthetic_scanline_page("vertical").save(input_dir / "vertical.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            paths = write_reports(report, output_dir)
            rules_by_path: dict[str, set[str]] = {}
            for finding in report["findings"]:
                rules_by_path.setdefault(finding["relative_path"], set()).add(finding["rule"])
            records = {record["relative_path"]: record for record in report["files"]}

            self.assertIn("quality_scanline_candidate", rules_by_path["horizontal.png"])
            self.assertIn("quality_scanline_candidate", rules_by_path["vertical.png"])
            self.assertEqual(records["horizontal.png"]["quality_scanline_orientation"], "horizontal")
            self.assertEqual(records["vertical.png"]["quality_scanline_orientation"], "vertical")
            self.assertGreaterEqual(records["horizontal.png"]["quality_scanline_score"], 0.85)
            self.assertGreaterEqual(records["vertical.png"]["quality_scanline_score"], 0.85)
            self.assertIsNotNone(records["horizontal.png"]["quality_scanline_reason"])

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertIn("quality_scanline_candidate", saved["rule_catalog"])
            self.assertIn("quality_scanline_score", paths["files_csv"].read_text(encoding="utf-8"))
            self.assertIn("quality_scanline_candidate", paths["findings_csv"].read_text(encoding="utf-8"))
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Scanline or streak artifact candidate", html)
            self.assertIn("Quality Scanline Candidate", html)

    def test_scanline_quality_findings_stay_quiet_on_clean_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().save(input_dir / "clean.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules = {finding["rule"] for finding in report["findings"]}
            record = report["files"][0]

            self.assertNotIn("quality_scanline_candidate", rules)
            self.assertIsNone(record["quality_scanline_orientation"])
            self.assertLess(record["quality_scanline_score"], 0.85)

    def test_content_edge_cutoff_flags_edge_touching_content_and_reports_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_edge_cutoff_page().save(input_dir / "edge_cutoff.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            paths = write_reports(report, output_dir)
            rules = {finding["rule"] for finding in report["findings"]}
            record = report["files"][0]

            self.assertIn("quality_content_edge_cutoff_candidate", rules)
            self.assertEqual(record["quality_content_edge_cutoff_side"], "left")
            self.assertGreaterEqual(record["quality_content_edge_cutoff_score"], 0.65)
            self.assertGreater(record["quality_content_edge_cutoff_dark_ratio"], 0)
            self.assertGreater(record["quality_content_edge_cutoff_span_ratio"], 0)
            self.assertIn("localized dark content", record["quality_content_edge_cutoff_reason"])

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertIn("quality_content_edge_cutoff_candidate", saved["rule_catalog"])
            self.assertIn("quality_content_edge_cutoff_score", paths["files_csv"].read_text(encoding="utf-8"))
            self.assertIn("quality_content_edge_cutoff_candidate", paths["findings_csv"].read_text(encoding="utf-8"))
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Content edge cutoff candidate", html)
            self.assertIn("Edge Cutoff Score", html)
            self.assertNotIn("<img", html.lower())

    def test_content_edge_cutoff_stays_quiet_on_normal_margin_and_dark_border(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().save(input_dir / "normal_margin.png", dpi=(300, 300))
            _synthetic_dark_border_page().save(input_dir / "dark_border.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules_by_path: dict[str, set[str]] = {}
            for finding in report["findings"]:
                rules_by_path.setdefault(finding["relative_path"], set()).add(finding["rule"])
            records = {record["relative_path"]: record for record in report["files"]}

            self.assertNotIn("quality_content_edge_cutoff_candidate", rules_by_path.get("normal_margin.png", set()))
            self.assertNotIn("quality_content_edge_cutoff_candidate", rules_by_path.get("dark_border.png", set()))
            self.assertIn("quality_dark_border_candidate", rules_by_path["dark_border.png"])
            self.assertIsNone(records["normal_margin.png"]["quality_content_edge_cutoff_side"])
            self.assertIsNone(records["dark_border.png"]["quality_content_edge_cutoff_side"])

    def test_quality_metrics_flag_dark_bright_low_contrast_and_blur(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (80, 80), (25, 25, 25)).save(input_dir / "dark.png")
            Image.new("RGB", (80, 80), (253, 253, 253)).save(input_dir / "bright.png")
            Image.new("RGB", (80, 80), (128, 128, 128)).save(input_dir / "low_contrast.png")
            _synthetic_text_page().filter(ImageFilter.GaussianBlur(radius=3)).save(input_dir / "blur.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules_by_path: dict[str, set[str]] = {}
            for finding in report["findings"]:
                rules_by_path.setdefault(finding["relative_path"], set()).add(finding["rule"])

            self.assertIn("quality_too_dark", rules_by_path["dark.png"])
            self.assertIn("quality_too_bright", rules_by_path["bright.png"])
            self.assertIn("quality_low_contrast", rules_by_path["low_contrast.png"])
            self.assertIn("quality_suspected_blur", rules_by_path["blur.png"])

    def test_blank_page_rule_flags_blank_and_light_noise_but_not_text_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (240, 180), "white").save(input_dir / "blank.png", dpi=(300, 300))
            _synthetic_light_noise_page().save(input_dir / "light_noise.png", dpi=(300, 300))
            _synthetic_text_page().save(input_dir / "text.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            rules_by_path: dict[str, set[str]] = {}
            for finding in report["findings"]:
                rules_by_path.setdefault(finding["relative_path"], set()).add(finding["rule"])
            records = {record["relative_path"]: record for record in report["files"]}

            self.assertIn("quality_near_blank_page", rules_by_path["blank.png"])
            self.assertIn("quality_near_blank_page", rules_by_path["light_noise.png"])
            self.assertNotIn("quality_near_blank_page", rules_by_path.get("text.png", set()))
            self.assertEqual(report["summary"]["blank_page_findings"], 2)
            self.assertLessEqual(records["blank.png"]["quality_foreground_coverage"], 0.003)
            self.assertGreater(records["text.png"]["quality_foreground_coverage"], 0.003)

    def test_rules_profile_can_disable_blank_rule_and_override_severity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            disabled_out = root / "disabled"
            severity_out = root / "severity"
            disabled_profile = root / "disabled.json"
            severity_profile = root / "severity.json"
            input_dir.mkdir()
            Image.new("RGB", (120, 90), "white").save(input_dir / "blank.png", dpi=(300, 300))
            disabled_profile.write_text(
                json.dumps({"rules": {"quality_near_blank_page": {"enabled": False}}}),
                encoding="utf-8",
            )
            severity_profile.write_text(
                json.dumps({"rules": {"quality_near_blank_page": {"severity": "P1"}}}),
                encoding="utf-8",
            )

            disabled = scan_batch(
                ScanConfig("p1", "b1", input_dir, disabled_out, rules_profile=load_rules_profile(disabled_profile))
            )
            severity = scan_batch(
                ScanConfig("p1", "b1", input_dir, severity_out, rules_profile=load_rules_profile(severity_profile))
            )

            self.assertFalse(any(finding["rule"] == "quality_near_blank_page" for finding in disabled["findings"]))
            self.assertTrue(
                any(
                    finding["rule"] == "quality_near_blank_page" and finding["severity"] == "P1"
                    for finding in severity["findings"]
                )
            )

    def test_default_rules_profile_metadata_preserves_existing_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(150, 150))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            profile = report["manifest"]["rules_profile"]

            self.assertEqual(profile["name"], "default")
            self.assertEqual(profile["version"], "scan-qc.phase1.v1")
            self.assertEqual(profile["source"], "builtin")
            self.assertEqual(profile["thresholds"]["min_dpi"], 200)
            self.assertEqual(profile["thresholds"]["quality"]["dark_mean_threshold"], 45.0)
            self.assertTrue(any(finding["rule"] == "dpi_minimum" and finding["severity"] == "P0" for finding in report["findings"]))

    def test_json_rules_profile_overrides_min_dpi_quality_threshold_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            profile_path = root / "rules.json"
            input_dir.mkdir()
            Image.new("RGB", (80, 80), (40, 40, 40)).save(input_dir / "A001_0001.jpg", dpi=(250, 250))
            profile_path.write_text(
                json.dumps(
                    {
                        "name": "project-standard",
                        "version": "2026.1",
                        "min_dpi": 300,
                        "quality_thresholds": {"dark_mean_threshold": 30},
                    }
                ),
                encoding="utf-8",
            )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=load_rules_profile(profile_path)))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertIn("dpi_minimum", rules)
            self.assertNotIn("quality_too_dark", rules)
            self.assertEqual(report["project"]["min_dpi"], 300)
            self.assertEqual(report["manifest"]["rules_profile"]["name"], "project-standard")
            self.assertEqual(report["manifest"]["rules_profile"]["version"], "2026.1")
            self.assertEqual(report["manifest"]["rules_profile"]["source"], str(profile_path.resolve()))
            self.assertEqual(report["manifest"]["rules_profile"]["thresholds"]["quality"]["dark_mean_threshold"], 30.0)

    def test_examples_rules_profile_manifest_and_cli_are_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_text_page().save(input_dir / "BATCH001_PAGE_0001.png", dpi=(300, 300))
            _synthetic_text_page().transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(
                input_dir / "BATCH001_PAGE_0002.png",
                dpi=(300, 300),
            )

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--process-out",
                    str(process_dir),
                    "--auto-crop",
                    "--deskew",
                    "--trim-dark-border",
                    "--despeckle",
                    "--workers",
                    "1",
                    "--project",
                    "test-example",
                    "--batch",
                    "synthetic",
                    "--manifest-csv",
                    str(REPO_ROOT / "examples" / "manifest.sample.csv"),
                    "--rules-profile",
                    str(REPO_ROOT / "examples" / "rules-profile.production-sample.json"),
                ]
            )

            self.assertEqual(exit_code, 0)
            saved = json.loads((output_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
            processing = json.loads((process_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["manifest"]["manifest_used"])
            self.assertEqual(saved["summary"]["manifest_missing_count"], 0)
            self.assertEqual(saved["summary"]["manifest_unexpected_count"], 0)
            self.assertEqual(saved["manifest"]["rules_profile"]["name"], "production-sample-standard")
            self.assertEqual(saved["project"]["min_dpi"], 300)
            self.assertEqual(processing["summary"]["processed_files"], 2)
            self.assertEqual(processing["summary"]["failed_files"], 0)

    def test_rules_profile_can_disable_quality_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            profile_path = root / "rules.json"
            input_dir.mkdir()
            Image.new("RGB", (80, 80), (25, 25, 25)).save(input_dir / "dark.png")
            profile_path.write_text(
                json.dumps({"rules": {"quality_too_dark": {"enabled": False}}}),
                encoding="utf-8",
            )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=load_rules_profile(profile_path)))
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertNotIn("quality_too_dark", rules)
            self.assertIn("quality_low_contrast", rules)

    def test_rules_profile_severity_override_applies_but_protected_p0_stays_p0(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            profile_path = root / "rules.json"
            input_dir.mkdir()
            Image.new("RGB", (80, 80), (25, 25, 25)).save(input_dir / "dark.png", dpi=(150, 150))
            profile_path.write_text(
                json.dumps(
                    {
                        "rules": {
                            "quality_too_dark": {"severity": "P2"},
                            "dpi_minimum": {"severity": "P2", "enabled": False},
                        }
                    }
                ),
                encoding="utf-8",
            )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, rules_profile=load_rules_profile(profile_path)))
            severities = {(finding["rule"], finding["severity"]) for finding in report["findings"]}

            self.assertIn(("quality_too_dark", "P2"), severities)
            self.assertIn(("dpi_minimum", "P0"), severities)

    def test_invalid_rules_profile_errors_are_clear_and_cli_writes_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            bad_type_path = root / "bad-type.json"
            bad_json_path = root / "bad-json.json"
            input_dir.mkdir()
            bad_type_path.write_text(json.dumps({"min_dpi": "300"}), encoding="utf-8")
            bad_json_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(RulesProfileError, "min_dpi"):
                load_rules_profile(bad_type_path)
            with self.assertRaisesRegex(RulesProfileError, "line 1, column 2"):
                load_rules_profile(bad_json_path)
            with self.assertRaisesRegex(RulesProfileError, "does not exist"):
                load_rules_profile(root / "missing.json")

            with self.assertRaises(SystemExit) as raised:
                main(["--input", str(input_dir), "--out", str(output_dir), "--rules-profile", str(bad_type_path)])

            self.assertEqual(raised.exception.code, 2)
            self.assertFalse(output_dir.exists())

    def test_quality_metrics_are_visible_in_json_csv_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().filter(ImageFilter.GaussianBlur(radius=3)).save(input_dir / "blur.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            paths = write_reports(report, output_dir)

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertIn("quality_brightness_mean", saved["files"][0])
            self.assertIn("quality_dark_pixel_ratio", saved["files"][0])
            self.assertIn("quality_foreground_coverage", saved["files"][0])
            self.assertIn("quality_edge_coverage", saved["files"][0])
            self.assertIn("quality_contrast_stddev", paths["files_csv"].read_text(encoding="utf-8"))
            self.assertIn("quality_foreground_coverage", paths["files_csv"].read_text(encoding="utf-8"))
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Brightness Mean", html)
            self.assertIn("Foreground Coverage", html)
            self.assertIn("quality_suspected_blur", html)

    def test_orientation_metrics_are_visible_in_json_csv_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (80, 120), "white").save(input_dir / "portrait.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            paths = write_reports(report, output_dir)

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual(saved["files"][0]["orientation_class"], "portrait")
            self.assertEqual(saved["files"][0]["aspect_ratio"], 0.6667)
            self.assertIn("exif_orientation_requires_transpose", saved["files"][0])
            csv_text = paths["files_csv"].read_text(encoding="utf-8")
            self.assertIn("orientation_class", csv_text)
            self.assertIn("aspect_ratio", csv_text)
            self.assertIn("exif_orientation_requires_transpose", csv_text)
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Orientation", html)
            self.assertIn("Aspect Ratio", html)
            self.assertIn("EXIF Transpose Signal", html)

    def test_multi_page_tiff_reports_frame_count_and_policy_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            frames = [_synthetic_text_page(), _synthetic_text_page().transpose(Image.Transpose.FLIP_LEFT_RIGHT)]
            frames[0].save(input_dir / "multi_page.tif", save_all=True, append_images=frames[1:], dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            paths = write_reports(report, output_dir)

            saved = json.loads(paths["json"].read_text(encoding="utf-8"))
            file_record = saved["files"][0]
            self.assertEqual(file_record["frame_count"], 2)
            findings = [finding for finding in saved["findings"] if finding["rule"] == "multi_page_image_container"]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["severity"], "P2")
            self.assertIn("2 frames/pages", findings[0]["message"])
            self.assertIn("frame_count", paths["files_csv"].read_text(encoding="utf-8"))
            self.assertIn("multi_page_image_container", paths["findings_csv"].read_text(encoding="utf-8"))
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Frames/Pages", html)
            self.assertIn("Multi-page image container", html)

    def test_single_page_tiff_reports_one_frame_without_multi_page_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().save(input_dir / "single_page.tif", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))

            self.assertEqual(report["files"][0]["frame_count"], 1)
            rules = {finding["rule"] for finding in report["findings"]}
            self.assertNotIn("multi_page_image_container", rules)

    def test_batch_orientation_consistency_flags_mixed_portrait_and_landscape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            for index in range(2):
                Image.new("RGB", (80, 120), "white").save(input_dir / f"portrait_{index}.png")
                Image.new("RGB", (120, 80), "white").save(input_dir / f"landscape_{index}.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            orientation_findings = [
                finding for finding in report["findings"] if finding["rule"] == "batch_orientation_consistency"
            ]

            self.assertEqual(len(orientation_findings), 4)
            self.assertTrue(all(finding["severity"] == "P2" for finding in orientation_findings))

    def test_batch_orientation_consistency_ignores_all_portrait_and_near_square(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            for index in range(3):
                Image.new("RGB", (80, 120), "white").save(input_dir / f"portrait_{index}.png")
            Image.new("RGB", (100, 102), "white").save(input_dir / "near_square.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            records = {record["relative_path"]: record for record in report["files"]}
            rules = {finding["rule"] for finding in report["findings"]}

            self.assertEqual(records["near_square.png"]["orientation_class"], "square")
            self.assertNotIn("batch_orientation_consistency", rules)

    def test_batch_orientation_consistency_can_be_disabled_and_severity_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            disabled_out = root / "disabled"
            severity_out = root / "severity"
            disabled_profile = root / "disabled.json"
            severity_profile = root / "severity.json"
            input_dir.mkdir()
            for index in range(2):
                Image.new("RGB", (80, 120), "white").save(input_dir / f"portrait_{index}.png")
                Image.new("RGB", (120, 80), "white").save(input_dir / f"landscape_{index}.png")
            disabled_profile.write_text(
                json.dumps({"rules": {"batch_orientation_consistency": {"enabled": False}}}),
                encoding="utf-8",
            )
            severity_profile.write_text(
                json.dumps({"rules": {"batch_orientation_consistency": {"severity": "P1"}}}),
                encoding="utf-8",
            )

            disabled = scan_batch(
                ScanConfig("p1", "b1", input_dir, disabled_out, rules_profile=load_rules_profile(disabled_profile))
            )
            severity = scan_batch(
                ScanConfig("p1", "b1", input_dir, severity_out, rules_profile=load_rules_profile(severity_profile))
            )

            self.assertFalse(any(finding["rule"] == "batch_orientation_consistency" for finding in disabled["findings"]))
            self.assertTrue(
                any(
                    finding["rule"] == "batch_orientation_consistency" and finding["severity"] == "P1"
                    for finding in severity["findings"]
                )
            )

    def test_benchmark_aggregate_includes_orientation_rule_count_without_file_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "benchmark"
            input_dir.mkdir()
            for index in range(2):
                Image.new("RGB", (80, 120), "white").save(input_dir / f"portrait_secret_{index}.png")
                Image.new("RGB", (120, 80), "white").save(input_dir / f"landscape_secret_{index}.png")

            exit_code = main(
                [
                    "benchmark",
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--workers-list",
                    "1",
                    "--scan-only",
                ]
            )

            self.assertEqual(exit_code, 0)
            json_path = output_dir / "benchmark_results.json"
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            run = payload["runs"][0]
            self.assertEqual(run["finding_rule_counts"]["batch_orientation_consistency"], 4)
            raw_json = json_path.read_text(encoding="utf-8")
            raw_csv = (output_dir / "benchmark_results.csv").read_text(encoding="utf-8")
            for forbidden in ["portrait_secret", "landscape_secret", "relative_path", "sha256"]:
                self.assertNotIn(forbidden, raw_json)
                self.assertNotIn(forbidden, raw_csv)

    def test_hidden_directories_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            hidden_dir = input_dir / ".cache"
            hidden_dir.mkdir(parents=True)

            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (32, 24), "white").save(hidden_dir / "A001_0002.jpg", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))

            scanned_paths = {item["relative_path"] for item in report["files"]}
            self.assertEqual(scanned_paths, {"A001_0001.jpg"})
            self.assertEqual(report["summary"]["skipped_hidden_directory_count"], 1)
            self.assertEqual(report["summary"]["skipped_directory_count"], 1)

    def test_process_images_writes_derivatives_without_modifying_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.jpg"
            Image.new("RGB", (32, 24), (120, 120, 120)).save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir)

            processed = process_dir / "images" / "A001_0001.jpg"
            manifest_path = process_dir / "processing_manifest.json"
            self.assertTrue(processed.exists())
            self.assertTrue(manifest_path.exists())
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertIn("performance", manifest)
            self.assertIn("performance", manifest["summary"])
            self.assertEqual(manifest["performance"]["total_files"], 1)
            self.assertEqual(manifest["performance"]["processed_files"], 1)
            self.assertEqual(manifest["performance"]["skipped_files"], 0)
            self.assertEqual(manifest["performance"]["failed_files"], 0)
            self.assertGreaterEqual(manifest["performance"]["elapsed_seconds"], 0)
            self.assertGreaterEqual(manifest["performance"]["processed_files_per_minute"], 0)
            self.assertGreaterEqual(manifest["performance"]["total_files_per_minute"], 0)
            self.assertIn("effective_workers", manifest["performance"])
            self.assertEqual(manifest["files"][0]["status"], "processed")
            self.assertEqual(manifest["files"][0]["source_relative_path"], "A001_0001.jpg")
            self.assertEqual(manifest["files"][0]["original_size"], [32, 24])
            self.assertEqual(manifest["files"][0]["output_size"], [32, 24])
            self.assertIsNone(manifest["files"][0]["crop_bbox"])
            self.assertFalse(manifest["files"][0]["cropped"])
            self.assertFalse(manifest["files"][0]["deskewed"])
            self.assertEqual(manifest["files"][0]["deskew_reason"], "deskew disabled")
            self.assertIn("auto_crop_disabled", manifest["files"][0]["operations"])
            self.assertIn("deskew_disabled", manifest["files"][0]["operations"])

    def test_enhance_faded_text_lightly_improves_low_contrast_text_and_records_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_faded_text.png"
            _synthetic_faded_text_page(ink=218).save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(enhance_faded_text=True, workers=1),
            )
            plan = build_processing_plan(report, input_dir, ProcessingOptions(enhance_faded_text=True))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            audit = record["processing_audit"]
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(record["faded_text_enhanced"])
            self.assertIn("enhance_faded_text_conservative", record["operations"])
            self.assertGreater(audit["faded_text_delta"], 8)
            self.assertGreater(audit["faded_text_changed_pixel_ratio"], 0)
            self.assertLessEqual(audit["faded_text_changed_pixel_ratio"], 0.10)
            self.assertGreater(audit["faded_text_candidate_pixel_ratio"], 0)
            self.assertEqual(record["faded_text_reason_code"], "applied_stable_low_contrast_text")
            self.assertEqual(record["faded_text_reason_zh"], "检测到浅色纸面上的稳定低对比正文，已保守加深。")
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertTrue(audit_summary["operations"]["enhance_faded_text"])
            self.assertEqual(audit_summary["counts"]["faded_text_enhanced_files"], 1)
            self.assertEqual(audit_summary["counts"]["faded_text_skipped_files"], 0)
            self.assertIn("faded_text_changed_pixel_ratio", audit_summary["metrics"])
            self.assertIn("faded_text_candidate_pixel_ratio", audit_summary["metrics"])
            self.assertEqual(audit_summary["guardrails"]["faded_text"]["applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["faded_text"]["skipped_files"], 0)
            self.assertGreater(audit_summary["guardrails"]["faded_text"]["changed_pixel_ratio"]["max"], 0)
            self.assertGreater(audit_summary["guardrails"]["faded_text"]["candidate_pixel_ratio"]["max"], 0)
            self.assertIn(record["faded_text_reason"], audit_summary["guardrails"]["faded_text"]["reason_distribution"])
            self.assertEqual(
                audit_summary["guardrails"]["faded_text"]["reason_code_distribution"],
                {"applied_stable_low_contrast_text": 1},
            )
            self.assertEqual(plan["summary"]["faded_text_enhancement_candidates"], 1)
            self.assertTrue(plan["files"][0]["faded_text_enhancement_candidate"])

    def test_enhance_faded_text_preserves_thin_low_contrast_gray_strokes_in_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_faded_text_page(ink=224).save(input_dir / "private_low_contrast_gray_text.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(enhance_faded_text=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            record = manifest["files"][0]
            audit = record["processing_audit"]
            self.assertTrue(record["faded_text_enhanced"])
            self.assertEqual(record["faded_text_reason_code"], "applied_stable_low_contrast_text")
            self.assertGreaterEqual(audit["faded_text_delta"], 8.0)
            self.assertGreater(audit["faded_text_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["faded_text_changed_pixel_ratio"], 0.10)
            self.assertLessEqual(audit["faded_text_candidate_pixel_ratio"], 0.16)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit_summary["counts"]["faded_text_enhanced_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["faded_text"]["reason_code_distribution"],
                {"applied_stable_low_contrast_text": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_low_contrast_gray_text", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_enhance_faded_text_noops_for_normal_color_dark_photo_texture_and_default_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            default_process_dir = root / "processed-default"
            input_dir.mkdir()
            pages = {
                "A001_high_contrast.png": _synthetic_text_page(),
                "A002_red_stamp.png": _synthetic_faded_text_page(red_stamp=True),
                "A003_dark_page.png": _synthetic_faded_text_page(background=150, ink=112),
                "A004_photo_like.png": _synthetic_photo_like_page(),
                "A005_texture_stain.png": _synthetic_texture_stain_page(),
                "A006_edge_archival_line.png": _synthetic_faded_text_edge_risk_page(),
                "A007_table_page_number_handwriting.png": _synthetic_faded_text_table_page(),
                "A008_too_faint_low_confidence.png": _synthetic_faded_text_page(ink=232),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(enhance_faded_text=True, workers=1),
            )
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            for record in default_manifest["files"]:
                self.assertFalse(record["faded_text_enhanced"])
                self.assertIn("enhance_faded_text_disabled", record["operations"])
                self.assertEqual(record["processing_audit"]["faded_text_changed_pixel_ratio"], 0.0)
            for record in manifest["files"]:
                self.assertFalse(record["faded_text_enhanced"], record["source_relative_path"])
                self.assertIn("enhance_faded_text_noop", record["operations"])
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
                self.assertEqual(record["processing_audit"]["faded_text_changed_pixel_ratio"], 0.0)
                self.assertIsInstance(record["faded_text_reason_code"], str)
                self.assertNotEqual(record["faded_text_reason_code"], "unknown")
                self.assertIsInstance(record["faded_text_reason_zh"], str)
                self.assertIn("跳过褪色正文加深", record["faded_text_reason_zh"])
            self.assertTrue(audit_summary["operations"]["enhance_faded_text"])
            self.assertEqual(audit_summary["counts"]["faded_text_enhanced_files"], 0)
            self.assertEqual(audit_summary["counts"]["faded_text_skipped_files"], len(pages))
            faded_guard = audit_summary["guardrails"]["faded_text"]
            self.assertEqual(faded_guard["applied_files"], 0)
            self.assertEqual(faded_guard["skipped_files"], len(pages))
            self.assertGreaterEqual(faded_guard["protection_triggered_files"], 4)
            self.assertGreaterEqual(faded_guard["low_confidence_skip_files"], 1)
            self.assertIn(
                "faded text enhancement skipped: edge mark or binding risk",
                faded_guard["skip_reason_distribution"],
            )
            self.assertIn(
                "faded text enhancement skipped: broad stain, texture, illustration, or table-region risk",
                faded_guard["skip_reason_distribution"],
            )
            self.assertIn("protected_color_stamp_annotation", faded_guard["skip_reason_code_distribution"])
            self.assertIn("protected_edge_mark_or_binding", faded_guard["skip_reason_code_distribution"])
            self.assertIn("protected_texture_table_or_photo_region", faded_guard["skip_reason_code_distribution"])
            self.assertIn("low_confidence_text_evidence_too_weak", faded_guard["skip_reason_code_distribution"])
            self.assertIn("检测到彩色内容、印章或批注风险，跳过褪色正文加深。", faded_guard["skip_reason_zh_distribution"])
            self.assertIn("检测到边缘痕迹或装订边风险，跳过褪色正文加深。", faded_guard["skip_reason_zh_distribution"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            self.assertNotIn("A006_edge_archival_line", audit_summary_text)
            self.assertNotIn("A007_table_page_number_handwriting", audit_summary_text)

    def test_enhance_faded_text_dense_noop_skips_component_scan_and_keeps_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_dense_faded_text_noop_page().save(input_dir / "private_dense_texture.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_mask_components",
                side_effect=AssertionError("dense no-op should not scan components"),
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(enhance_faded_text=True, workers=1),
                )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            record = manifest["files"][0]
            timing = manifest["summary"]["performance"]["operation_timings"]["enhance_faded_text"]
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["faded_text_enhanced"])
            self.assertIn("enhance_faded_text_noop", record["operations"])
            self.assertEqual(record["faded_text_reason"], "faded text enhancement skipped: foreground too dense")
            self.assertGreater(record["faded_text_candidate_pixel_ratio"], 0.20)
            self.assertEqual(record["processing_audit"]["faded_text_changed_pixel_ratio"], 0.0)
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertTrue(timing["enabled"])
            self.assertEqual(timing["file_count"], 1)
            self.assertIn("elapsed_seconds", timing)
            self.assertIn("faded_text_candidate_pixel_ratio", audit_summary["metrics"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertNotIn("private_dense_texture", audit_summary_text)

    def test_full_retouch_chain_with_faded_text_keeps_public_audit_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_faded_text_page(ink=210).save(input_dir / "private_combo_faded_text.png", dpi=(300, 300))
            _synthetic_faded_text_edge_risk_page().save(input_dir / "private_combo_edge_line.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    despeckle=True,
                    lighten_background_stains=True,
                    lighten_scanlines=True,
                    enhance_faded_text=True,
                    normalize_tones=True,
                    sharpen_text_edges=True,
                    workers=1,
                ),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            self.assertEqual(manifest["summary"]["processed_files"], 2)
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertTrue(audit_summary["operations"]["enhance_faded_text"])
            self.assertIn("faded_text", audit_summary["guardrails"])
            self.assertIn("skip_reason_distribution", audit_summary["guardrails"]["faded_text"])
            self.assertTrue(audit_summary["operations"]["sharpen_text_edges"])
            self.assertIn("text_edges", audit_summary["guardrails"])
            self.assertIn("skip_reason_distribution", audit_summary["guardrails"]["text_edges"])
            self.assertIn("candidate_preflight_skip_files", audit_summary["guardrails"]["text_edges"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            self.assertNotIn("private_combo_faded_text", audit_summary_text)
            self.assertNotIn("private_combo_edge_line", audit_summary_text)

    def test_sharpen_text_edges_lightly_improves_blurred_text_and_records_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_blurred_text.png"
            _synthetic_blurred_text_page().save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(sharpen_text_edges=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]
            processed = Image.open(process_dir / record["output_relative_path"])

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(record["text_edges_sharpened"])
            self.assertIn("sharpen_text_edges_conservative", record["operations"])
            self.assertGreater(_edge_energy(processed), _edge_energy(Image.open(source)))
            self.assertGreater(audit["text_edges_delta"], 3.0)
            self.assertGreater(audit["text_edges_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["text_edges_changed_pixel_ratio"], 0.08)
            self.assertLessEqual(audit["text_edges_candidate_pixel_ratio"], 0.12)
            self.assertEqual(record["text_edges_reason_code"], "applied_stable_blurred_text_edges")
            self.assertEqual(record["text_edges_reason_zh"], "检测到浅色纸面上的稳定模糊正文边缘，已保守锐化。")
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertTrue(audit_summary["operations"]["sharpen_text_edges"])
            self.assertEqual(audit_summary["counts"]["text_edges_sharpened_files"], 1)
            self.assertEqual(audit_summary["counts"]["text_edges_skipped_files"], 0)
            self.assertIn("text_edges_changed_pixel_ratio", audit_summary["metrics"])
            text_edge_guard = audit_summary["guardrails"]["text_edges"]
            self.assertEqual(text_edge_guard["applied_files"], 1)
            self.assertEqual(text_edge_guard["skipped_files"], 0)
            self.assertGreater(text_edge_guard["changed_pixel_ratio"]["max"], 0)
            self.assertGreater(text_edge_guard["candidate_pixel_ratio"]["max"], 0)
            self.assertIn(record["text_edges_reason"], text_edge_guard["reason_distribution"])
            self.assertEqual(
                text_edge_guard["reason_code_distribution"],
                {"applied_stable_blurred_text_edges": 1},
            )
            self.assertTrue(audit_summary["timing"]["operation_timings"]["sharpen_text_edges"]["enabled"])
            self.assertNotIn("private_blurred_text", audit_summary_text)

    def test_sharpen_text_edges_noops_for_clear_color_photo_texture_and_default_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            default_process_dir = root / "processed-default"
            input_dir.mkdir()
            pages = {
                "A001_clear_text.png": _synthetic_text_page(),
                "A002_red_stamp.png": _synthetic_blurred_text_page(red_stamp=True),
                "A003_photo_like.png": _synthetic_photo_like_page(),
                "A004_texture_stain.png": _synthetic_texture_stain_page(),
                "A005_edge_archival_line.png": _synthetic_blurred_text_edge_risk_page(),
                "A006_table_page_number_handwriting.png": _synthetic_blurred_table_text_edge_page(),
                "A007_too_faint_low_confidence.png": _synthetic_low_confidence_text_edge_page(),
                "A008_high_contrast_dense_text.png": _synthetic_high_contrast_dense_text_page(),
                "A009_page_number.png": _synthetic_blurred_text_page(page_number=True),
                "A010_header_footer.png": _synthetic_blurred_text_page(header_footer=True),
                "A011_dark_page.png": _synthetic_dark_blurred_text_page(),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(sharpen_text_edges=True, workers=1),
            )
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            for record in default_manifest["files"]:
                self.assertFalse(record["text_edges_sharpened"])
                self.assertIn("sharpen_text_edges_disabled", record["operations"])
                self.assertEqual(record["processing_audit"]["text_edges_changed_pixel_ratio"], 0.0)
            for record in manifest["files"]:
                self.assertFalse(record["text_edges_sharpened"], record["source_relative_path"])
                self.assertIn("sharpen_text_edges_noop", record["operations"])
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
                self.assertLessEqual(record["processing_audit"]["text_edges_changed_pixel_ratio"], 0.002)
                self.assertIsInstance(record["text_edges_reason_code"], str)
                self.assertNotEqual(record["text_edges_reason_code"], "unknown")
                self.assertIsInstance(record["text_edges_reason_zh"], str)
                self.assertIn("跳过正文边缘锐化", record["text_edges_reason_zh"])
            self.assertTrue(audit_summary["operations"]["sharpen_text_edges"])
            self.assertEqual(audit_summary["counts"]["text_edges_sharpened_files"], 0)
            self.assertEqual(audit_summary["counts"]["text_edges_skipped_files"], len(pages))
            text_edge_guard = audit_summary["guardrails"]["text_edges"]
            self.assertEqual(text_edge_guard["applied_files"], 0)
            self.assertEqual(text_edge_guard["skipped_files"], len(pages))
            self.assertGreaterEqual(text_edge_guard["protection_triggered_files"], 4)
            self.assertGreaterEqual(text_edge_guard["low_confidence_skip_files"], 2)
            self.assertEqual(text_edge_guard["changed_pixel_ratio"]["max"], 0.0)
            self.assertIn(
                "text edge sharpening skipped: edge mark or binding risk",
                text_edge_guard["skip_reason_distribution"],
            )
            self.assertIn(
                "text edge sharpening skipped: broad texture, illustration, or table-region risk",
                text_edge_guard["skip_reason_distribution"],
            )
            self.assertIn(
                "text edge sharpening skipped: header, footer, or page number risk",
                text_edge_guard["skip_reason_distribution"],
            )
            self.assertIn("protected_color_stamp_annotation", text_edge_guard["skip_reason_code_distribution"])
            self.assertIn("protected_edge_mark_or_binding", text_edge_guard["skip_reason_code_distribution"])
            self.assertIn("protected_header_footer_or_page_number", text_edge_guard["skip_reason_code_distribution"])
            self.assertIn("protected_texture_table_or_photo_region", text_edge_guard["skip_reason_code_distribution"])
            self.assertIn("low_confidence_text_edge_evidence_too_weak", text_edge_guard["skip_reason_code_distribution"])
            self.assertIn(
                "检测到页眉页脚或页码风险，跳过正文边缘锐化。",
                text_edge_guard["skip_reason_zh_distribution"],
            )
            self.assertIn(
                "页面不是浅色纸面背景，跳过正文边缘锐化。",
                text_edge_guard["skip_reason_zh_distribution"],
            )

    def test_sharpen_text_edges_low_candidate_page_uses_fast_noop_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_sparse_text_edge_preflight_skip_page().save(input_dir / "A001_sparse_marks.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            mask_call_sizes: list[tuple[int, int]] = []
            original_candidate_mask = processing_module._text_edge_candidate_mask

            def wrapped_candidate_mask(grayscale: Image.Image, p95: int) -> Image.Image:
                mask_call_sizes.append(grayscale.size)
                return original_candidate_mask(grayscale, p95)

            with mock.patch.object(processing_module, "_text_edge_candidate_mask", side_effect=wrapped_candidate_mask):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(sharpen_text_edges=True, workers=1),
                )

            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            record = manifest["files"][0]
            audit = record["processing_audit"]
            timing = audit_summary["timing"]["operation_timings"]["sharpen_text_edges"]

            self.assertFalse(record["text_edges_sharpened"])
            self.assertIn("sharpen_text_edges_noop", record["operations"])
            self.assertIn("cheap candidate preflight", record["text_edges_reason"])
            self.assertGreater(audit["text_edges_candidate_pixel_ratio"], 0.0)
            self.assertEqual(audit["text_edges_changed_pixel_ratio"], 0.0)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertTrue(mask_call_sizes)
            self.assertTrue(all(width <= 96 and height <= 96 for width, height in mask_call_sizes))
            self.assertTrue(timing["enabled"])
            self.assertEqual(timing["file_count"], 1)
            self.assertEqual(timing["candidate_preflight_skipped_files"], 1)
            self.assertEqual(audit_summary["counts"]["text_edges_sharpened_files"], 0)
            self.assertEqual(audit_summary["counts"]["text_edges_candidate_preflight_skipped_files"], 1)
            self.assertIn("text_edges_candidate_pixel_ratio", audit_summary["metrics"])

    def test_sharpen_text_edges_default_off_preserves_derivative_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_blurred_text_page().save(input_dir / "A001_0001.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            record = manifest["files"][0]

            self.assertFalse(record["text_edges_sharpened"])
            self.assertIn("sharpen_text_edges_disabled", record["operations"])
            self.assertEqual(record["processing_audit"]["text_edges_changed_pixel_ratio"], 0.0)
            self.assertTrue(record["processing_audit"]["cumulative_change_guard_checked"])
            self.assertEqual(record["processing_audit"]["cumulative_change_guard_action"], "passed")
            self.assertEqual(
                _sha256_for_test(input_dir / "A001_0001.png"),
                _sha256_for_test(process_dir / record["output_relative_path"]),
            )

    def test_cumulative_change_guard_passes_safe_stacked_repairs_and_writes_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_safe_stacked_repairs.png"
            Image.new("RGB", (40, 40), (245, 245, 245)).save(source, dpi=(300, 300))

            def small_background_change(image: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                output = image.copy()
                draw = ImageDraw.Draw(output)
                draw.rectangle((2, 2, 9, 9), fill=(230, 230, 230))
                return processing_module.BackgroundStainLighteningResult(
                    output, True, "synthetic safe background repair", 10.0, 15.0, 5.0, 0.04, 0.04
                )

            def small_text_edge_change(image: Image.Image) -> processing_module.TextEdgeSharpeningResult:
                output = image.copy()
                draw = ImageDraw.Draw(output)
                draw.rectangle((20, 20, 27, 27), fill=(228, 228, 228))
                return processing_module.TextEdgeSharpeningResult(
                    output, True, "synthetic safe text edge repair", 4.0, 0.04, 0.04
                )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_lighten_background_stains_conservative",
                side_effect=small_background_change,
            ), mock.patch.object(
                processing_module,
                "_sharpen_text_edges_conservative",
                side_effect=small_text_edge_change,
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(lighten_background_stains=True, sharpen_text_edges=True, workers=1),
                )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["background_stains_lightened"])
            self.assertTrue(record["text_edges_sharpened"])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertFalse(audit["cumulative_change_guard_reverted"])
            self.assertGreater(audit["cumulative_change_score"], 0.0)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_checked_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertIn("cumulative_change_score", audit_summary["metrics"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_stacked_repairs", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_background_stains_improves_gray_and_yellow_low_frequency_stains_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_gray_low_frequency_stain.png": _synthetic_background_stain_page((224, 224, 224)),
                "private_yellow_low_frequency_stain.png": _synthetic_background_stain_page((230, 226, 205)),
            }
            source_bytes: dict[str, bytes] = {}
            for name, image in pages.items():
                path = input_dir / name
                image.save(path, dpi=(300, 300))
                source_bytes[name] = path.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            for record in manifest["files"]:
                source_name = record["source_relative_path"]
                self.assertEqual((input_dir / source_name).read_bytes(), source_bytes[source_name])
                self.assertTrue(record["background_stains_lightened"], source_name)
                self.assertIn("lighten_background_stains_conservative", record["operations"])
                audit = record["processing_audit"]
                self.assertGreater(audit["background_stains_delta"], 6.0)
                self.assertGreater(audit["background_stains_changed_pixel_ratio"], 0.0)
                self.assertLessEqual(audit["background_stains_changed_pixel_ratio"], 0.08)
                self.assertLessEqual(audit["cumulative_change_candidate_pixel_ratio"], 0.08)
                self.assertEqual(audit["cumulative_change_guard_action"], "passed")
                self.assertEqual(audit["guardrail_failures"], [])

                original = pages[source_name].convert("RGB")
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                stain_before = ImageStat.Stat(original.crop((140, 60, 205, 112)).convert("L")).mean[0]
                stain_after = ImageStat.Stat(processed.crop((140, 60, 205, 112)).convert("L")).mean[0]
                self.assertGreater(stain_after - stain_before, 4.0)
                for protected_box in ((24, 40, 96, 128), (112, 26, 126, 38), (104, 136, 198, 154)):
                    diff = ImageChops.difference(original.crop(protected_box), processed.crop(protected_box))
                    self.assertIsNone(diff.getbbox(), f"{source_name} changed protected content {protected_box}")

            self.assertTrue(audit_summary["operations"]["lighten_background_stains"])
            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 2)
            self.assertEqual(audit_summary["counts"]["background_stains_skipped_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["background_stains"]["applied_files"], 2)
            self.assertEqual(audit_summary["guardrails"]["background_stains"]["skipped_files"], 0)
            self.assertIn("background_stains_changed_pixel_ratio", audit_summary["metrics"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_gray_low_frequency_stain", audit_summary_text)
            self.assertNotIn("private_yellow_low_frequency_stain", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_background_stains_improves_sparse_multi_spots_with_protection_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            page = _synthetic_background_multi_stain_page()
            source_name = "private_sparse_multi_spot_stains.png"
            page.save(input_dir / source_name, dpi=(300, 300))
            source_bytes = (input_dir / source_name).read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertEqual((input_dir / source_name).read_bytes(), source_bytes)
            self.assertTrue(record["background_stains_lightened"])
            self.assertIn("lighten_background_stains_conservative", record["operations"])
            audit = record["processing_audit"]
            self.assertGreater(audit["background_stains_delta"], 4.0)
            self.assertGreater(audit["background_stains_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["background_stains_changed_pixel_ratio"], 0.03)
            self.assertLessEqual(audit["background_stains_candidate_pixel_ratio"], 0.03)
            self.assertEqual(audit["guardrail_failures"], [])

            original = page.convert("RGB")
            for stain_box in ((130, 42, 148, 58), (176, 72, 198, 90), (144, 110, 162, 126)):
                before = ImageStat.Stat(original.crop(stain_box).convert("L")).mean[0]
                after = ImageStat.Stat(processed.crop(stain_box).convert("L")).mean[0]
                self.assertGreater(after - before, 3.0, stain_box)
            for protected_box in ((24, 40, 96, 128), (112, 26, 126, 38), (104, 136, 198, 154)):
                diff = ImageChops.difference(original.crop(protected_box), processed.crop(protected_box))
                self.assertIsNone(diff.getbbox(), f"changed protected content {protected_box}")

            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["background_stains"]["applied_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_sparse_multi_spot_stains", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_background_stains_skips_component_guard_risks_with_public_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_too_many_spots.png": _synthetic_background_multi_stain_page(variant="too_many"),
                "private_large_component.png": _synthetic_background_multi_stain_page(variant="large"),
                "private_foreground_near_spot.png": _synthetic_background_multi_stain_page(variant="near_foreground"),
                "private_edge_spot.png": _synthetic_background_multi_stain_page(variant="edge"),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            expected_reasons = {
                "private_too_many_spots.png": "too many stain candidates outside conservative scope",
                "private_large_component.png": "large stain or historical damage risk",
                "private_foreground_near_spot.png": "stain candidate near text, stamp, annotation, or original mark risk",
                "private_edge_spot.png": "binding, edge mark, or margin content risk",
            }
            for source_name, reason in expected_reasons.items():
                record = records[source_name]
                self.assertFalse(record["background_stains_lightened"], source_name)
                self.assertIn("lighten_background_stains_noop", record["operations"])
                self.assertIn(reason, record["background_stains_reason"])
                self.assertEqual(record["processing_audit"]["background_stains_changed_pixel_ratio"], 0.0)
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertIsNone(ImageChops.difference(pages[source_name].convert("RGB"), processed).getbbox())

            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 0)
            self.assertEqual(audit_summary["counts"]["background_stains_skipped_files"], 4)
            self.assertGreaterEqual(len(audit_summary["guardrails"]["background_stains"]["skip_reason_distribution"]), 4)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for private_name in ("private_too_many_spots", "private_large_component", "private_foreground_near_spot", "private_edge_spot"):
                self.assertNotIn(private_name, audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_background_stains_skips_uncertain_content_edges_and_default_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_default_compatible_stain.png": _synthetic_background_stain_page((224, 224, 224)),
                "private_red_annotation.png": _synthetic_background_stain_page((224, 224, 224), red_annotation=True),
                "private_photo_texture.png": _synthetic_photo_like_page(),
                "private_dense_dark_texture.png": _synthetic_dense_background_texture_page(),
                "private_low_confidence.png": Image.new("RGB", (240, 180), (236, 236, 236)),
                "private_edge_mark.png": _synthetic_background_stain_page((224, 224, 224), edge_mark=True),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            default_records = {record["source_relative_path"]: record for record in default_manifest["files"]}
            for source_name, record in default_records.items():
                self.assertFalse(record["background_stains_lightened"])
                self.assertIn("lighten_background_stains_disabled", record["operations"])
                self.assertEqual(
                    _sha256_for_test(input_dir / source_name),
                    _sha256_for_test(default_process_dir / record["output_relative_path"]),
                )

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertTrue(records["private_default_compatible_stain.png"]["background_stains_lightened"])
            for source_name in (
                "private_red_annotation.png",
                "private_photo_texture.png",
                "private_dense_dark_texture.png",
                "private_low_confidence.png",
                "private_edge_mark.png",
            ):
                self.assertFalse(records[source_name]["background_stains_lightened"], source_name)
                self.assertIn("lighten_background_stains_noop", records[source_name]["operations"])
                self.assertEqual(records[source_name]["processing_audit"]["background_stains_changed_pixel_ratio"], 0.0)

            self.assertIn("color content, stamp, or annotation risk", records["private_red_annotation.png"]["background_stains_reason"])
            self.assertIn("binding, edge mark, or margin content risk", records["private_edge_mark.png"]["background_stains_reason"])
            self.assertIn(
                records["private_photo_texture.png"]["background_stains_reason"],
                {
                    "background stain lightening skipped: page is too dark",
                    "background stain lightening skipped: foreground too dense",
                    "background stain lightening skipped: broad uneven lighting is outside conservative scope",
                    "background stain lightening skipped: large stain or historical damage risk",
                },
            )
            self.assertIn(
                records["private_dense_dark_texture.png"]["background_stains_reason"],
                {
                    "background stain lightening skipped: foreground too dense",
                    "background stain lightening skipped: broad uneven lighting is outside conservative scope",
                    "background stain lightening skipped: large stain or historical damage risk",
                    "background stain lightening skipped: page is too dark",
                    "background stain lightening skipped: binding, edge mark, or margin content risk",
                },
            )
            self.assertIn(
                records["private_low_confidence.png"]["background_stains_reason"],
                {
                    "background stain lightening skipped: low-confidence tonal evidence",
                    "background stain lightening skipped: foreground evidence too sparse",
                },
            )
            edge_processed = Image.open(process_dir / records["private_edge_mark.png"]["output_relative_path"]).convert("RGB")
            self.assertIsNone(
                ImageChops.difference(pages["private_edge_mark.png"].crop((0, 72, 18, 104)), edge_processed.crop((0, 72, 18, 104))).getbbox()
            )
            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["background_stains_skipped_files"], 5)
            self.assertGreaterEqual(audit_summary["guardrails"]["background_stains"]["protection_triggered_files"], 2)
            self.assertGreaterEqual(len(audit_summary["guardrails"]["background_stains"]["skip_reason_distribution"]), 3)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_default_compatible_stain", audit_summary_text)
            self.assertNotIn("private_red_annotation", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_clean_bleed_through_improves_faint_reverse_side_ghosts_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            page = _synthetic_bleed_through_page("pale_ghost")
            source_name = "private_faint_reverse_ghost.png"
            page.save(input_dir / source_name, dpi=(300, 300))
            source_bytes = (input_dir / source_name).read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(clean_bleed_through=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            default_record = default_manifest["files"][0]
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertEqual((input_dir / source_name).read_bytes(), source_bytes)
            self.assertFalse(default_record["bleed_through_cleaned"])
            self.assertIn("clean_bleed_through_disabled", default_record["operations"])
            self.assertTrue(record["bleed_through_cleaned"])
            self.assertIn("clean_bleed_through_conservative", record["operations"])
            audit = record["processing_audit"]
            self.assertGreater(audit["bleed_through_delta"], 3.0)
            self.assertGreater(audit["bleed_through_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["bleed_through_changed_pixel_ratio"], 0.01)
            self.assertGreater(audit["bleed_through_candidate_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["bleed_through_candidate_pixel_ratio"], 0.065)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")

            original = page.convert("RGB")
            ghost_box = (118, 80, 170, 112)
            before = ImageStat.Stat(original.crop(ghost_box).convert("L")).mean[0]
            after = ImageStat.Stat(processed.crop(ghost_box).convert("L")).mean[0]
            self.assertGreater(after - before, 0.05)
            protected_box = (30, 34, 72, 50)
            self.assertIsNone(
                ImageChops.difference(original.crop(protected_box), processed.crop(protected_box)).getbbox()
            )

            self.assertTrue(audit_summary["operations"]["clean_bleed_through"])
            self.assertEqual(audit_summary["counts"]["bleed_through_cleaned_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["bleed_through"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["bleed_through"]["reason_distribution"][
                    "bleed-through cleanup applied: faint reverse-side ghost on light background"
                ],
                1,
            )
            self.assertIn("bleed_through_changed_pixel_ratio", audit_summary["metrics"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_faint_reverse_ghost", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_clean_bleed_through_protects_foreground_marks_and_edge_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_light_foreground_text.png": _synthetic_bleed_through_page("text"),
                "private_page_number.png": _synthetic_bleed_through_page("page_number"),
                "private_table_lines.png": _synthetic_bleed_through_page("table"),
                "private_red_stamp.png": _synthetic_bleed_through_page("stamp"),
                "private_edge_mark.png": _synthetic_bleed_through_page("edge"),
                "private_dense_background.png": _synthetic_bleed_through_page("dense"),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(clean_bleed_through=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            for record in manifest["files"]:
                source_name = record["source_relative_path"]
                self.assertFalse(record["bleed_through_cleaned"], source_name)
                self.assertIn("clean_bleed_through_noop", record["operations"], source_name)
                self.assertEqual(record["processing_audit"]["bleed_through_changed_pixel_ratio"], 0.0)
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertIsNone(ImageChops.difference(pages[source_name].convert("RGB"), processed).getbbox())

            self.assertEqual(audit_summary["counts"]["bleed_through_cleaned_files"], 0)
            self.assertEqual(audit_summary["counts"]["bleed_through_skipped_files"], 6)
            self.assertGreaterEqual(
                len(audit_summary["guardrails"]["bleed_through"]["skip_reason_distribution"]),
                3,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for private_name in (
                "private_light_foreground_text",
                "private_page_number",
                "private_table_lines",
                "private_red_stamp",
                "private_edge_mark",
                "private_dense_background",
            ):
                self.assertNotIn(private_name, audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_normalize_tones_improves_safe_gray_page_with_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = _synthetic_tone_gray_text_page()
            source.save(input_dir / "private_safe_tone_page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_tones=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertTrue(record["tone_normalized"])
            self.assertIn("normalize_tones_conservative", record["operations"])
            self.assertGreater(record["tone_background_after"], record["tone_background_before"])
            self.assertGreater(record["tone_contrast_after"], record["tone_contrast_before"])
            self.assertGreater(record["processing_audit"]["tone_background_delta"], 12)
            self.assertGreater(record["processing_audit"]["tone_contrast_delta"], 12)
            self.assertGreater(record["tone_changed_pixel_ratio"], 0.70)
            self.assertLess(record["tone_changed_pixel_ratio"], 0.95)
            self.assertGreater(_box_luma(processed, (10, 10, 230, 28)), _box_luma(source, (10, 10, 230, 28)) + 12)
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])

            tone_guard = audit_summary["guardrails"]["tone_normalization"]
            self.assertEqual(audit_summary["counts"]["tone_normalized_files"], 1)
            self.assertEqual(audit_summary["counts"]["tone_skipped_files"], 0)
            self.assertEqual(tone_guard["applied_files"], 1)
            self.assertEqual(tone_guard["skipped_files"], 0)
            self.assertIn("tone_changed_pixel_ratio", audit_summary["metrics"])
            self.assertGreater(tone_guard["background_delta"]["max"], 12)
            self.assertGreater(tone_guard["contrast_delta"]["max"], 12)
            self.assertLess(tone_guard["changed_pixel_ratio"]["max"], 0.95)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_tone_page", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_normalize_tones_skips_protected_and_uncertain_pages_with_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_safe_tone_page.png": _synthetic_tone_gray_text_page(),
                "private_normal_exposure.png": _synthetic_tone_normal_exposure_page(),
                "private_high_contrast.png": _synthetic_tone_high_contrast_page(),
                "private_color_photo.png": _synthetic_tone_color_photo_page(),
                "private_red_stamp.png": _synthetic_tone_gray_text_page(red_stamp=True),
                "private_handwriting.png": _synthetic_tone_gray_text_page(handwriting=True),
                "private_paper_texture.png": _synthetic_tone_texture_page(),
                "private_too_dark.png": _synthetic_tone_dark_page(),
                "private_overexposed.png": _synthetic_tone_overexposed_page(),
                "private_high_noise.png": _synthetic_tone_noisy_page(),
                "private_color_risk.png": _synthetic_tone_color_annotation_page(),
                "private_low_confidence.png": _synthetic_tone_low_confidence_page(),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_tones=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            for record in default_manifest["files"]:
                self.assertFalse(record["tone_normalized"])
                self.assertIn("normalize_tones_disabled", record["operations"])
                self.assertEqual(
                    _sha256_for_test(input_dir / record["source_relative_path"]),
                    _sha256_for_test(default_process_dir / record["output_relative_path"]),
                )

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertTrue(records["private_safe_tone_page.png"]["tone_normalized"])
            skipped_expectations = {
                "private_normal_exposure.png": "already normal",
                "private_high_contrast.png": "high contrast",
                "private_color_photo.png": "obvious color content",
                "private_red_stamp.png": "red stamp",
                "private_handwriting.png": "light color annotation",
                "private_paper_texture.png": "low-confidence tonal separation",
                "private_too_dark.png": "too dark",
                "private_overexposed.png": "overexposed",
                "private_high_noise.png": "noise",
                "private_color_risk.png": "light color annotation",
                "private_low_confidence.png": "low-confidence tonal separation",
            }
            for source_name, reason_fragment in skipped_expectations.items():
                record = records[source_name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertFalse(record["tone_normalized"], source_name)
                self.assertIn("normalize_tones_noop", record["operations"], source_name)
                self.assertIn(reason_fragment, record["tone_reason"], source_name)
                self.assertEqual(record["processing_audit"]["tone_changed_pixel_ratio"], 0.0, source_name)
                self.assertLess(
                    _changed_ratio_for_test(pages[source_name], processed, (0, 0, processed.width, processed.height)),
                    0.001,
                    source_name,
                )

            tone_guard = audit_summary["guardrails"]["tone_normalization"]
            self.assertEqual(audit_summary["counts"]["tone_normalized_files"], 1)
            self.assertEqual(audit_summary["counts"]["tone_skipped_files"], len(skipped_expectations))
            self.assertEqual(tone_guard["applied_files"], 1)
            self.assertEqual(tone_guard["skipped_files"], len(skipped_expectations))
            self.assertGreaterEqual(tone_guard["protection_triggered_files"], 5)
            self.assertGreaterEqual(tone_guard["low_confidence_skip_files"], 2)
            self.assertGreaterEqual(tone_guard["conservative_scope_skip_files"], 2)
            self.assertGreaterEqual(len(tone_guard["skip_reason_distribution"]), 8)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_tone_page", audit_summary_text)
            self.assertNotIn("private_red_stamp", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_normalize_tones_preserves_subtle_paper_color(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = _synthetic_tone_gray_text_page(background=(190, 188, 178), foreground=(92, 90, 82))
            source.save(input_dir / "private_warm_paper.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_tones=True, workers=1),
            )
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
            before_pixel = source.getpixel((12, 12))
            after_pixel = processed.getpixel((12, 12))

            self.assertTrue(record["tone_normalized"])
            self.assertGreater(after_pixel[0] - after_pixel[2], 6)
            self.assertLess(abs((after_pixel[0] - after_pixel[2]) - (before_pixel[0] - before_pixel[2])), 4)

    def test_normalize_paper_color_cast_corrects_safe_uniform_cast_with_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            source_path = input_dir / "private_safe_uniform_cast.png"
            source = Image.new("RGB", (180, 140), (246, 243, 234))
            source.save(source_path, dpi=(300, 300))
            source_sha = _sha256_for_test(source_path)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_paper_color_cast=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            default_record = default_manifest["files"][0]
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertEqual(source_sha, _sha256_for_test(source_path))
            self.assertFalse(default_record["paper_color_cast_normalized"])
            self.assertIn("normalize_paper_color_cast_disabled", default_record["operations"])
            self.assertTrue(record["paper_color_cast_normalized"])
            self.assertEqual(record["paper_color_cast_reason_code"], "applied_mild_uniform_scanner_cast")
            self.assertIn("normalize_paper_color_cast_conservative", record["operations"])
            self.assertGreater(record["paper_color_cast_delta"], 6.0)
            self.assertLessEqual(record["paper_color_cast_delta"], 12.0)
            self.assertLessEqual(record["paper_color_cast_brightness_delta"], 4.0)
            self.assertGreater(record["paper_color_cast_changed_pixel_ratio"], 0.85)
            self.assertLessEqual(record["paper_color_cast_changed_pixel_ratio"], 1.0)
            before_spread = _mean_channel_spread(source)
            after_spread = _mean_channel_spread(processed)
            self.assertLess(after_spread, before_spread - 6.0)
            self.assertLess(_mean_luma_delta(source, processed), 4.0)
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])

            cast_guard = audit_summary["guardrails"]["paper_color_cast"]
            self.assertTrue(audit_summary["operations"]["normalize_paper_color_cast"])
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], 1)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_skipped_files"], 0)
            self.assertEqual(cast_guard["applied_files"], 1)
            self.assertEqual(cast_guard["skipped_files"], 0)
            self.assertIn("paper_color_cast_delta", audit_summary["metrics"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_uniform_cast", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_normalize_paper_color_cast_corrects_sparse_text_cast_without_foreground_shift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            source_path = input_dir / "private_sparse_text_cast.png"
            source = _sparse_text_uniform_cast_page()
            source.save(source_path, dpi=(300, 300))
            source_sha = _sha256_for_test(source_path)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_paper_color_cast=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            default_record = default_manifest["files"][0]
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertEqual(source_sha, _sha256_for_test(source_path))
            self.assertFalse(default_record["paper_color_cast_normalized"])
            self.assertIn("normalize_paper_color_cast_disabled", default_record["operations"])
            self.assertTrue(record["paper_color_cast_normalized"])
            self.assertEqual(record["paper_color_cast_reason_code"], "applied_mild_uniform_scanner_cast")
            self.assertGreater(record["paper_color_cast_delta"], 6.0)
            self.assertLessEqual(record["paper_color_cast_delta"], 12.0)
            self.assertLessEqual(record["paper_color_cast_brightness_delta"], 4.0)
            self.assertGreater(record["paper_color_cast_changed_pixel_ratio"], 0.90)
            self.assertLessEqual(record["paper_color_cast_changed_pixel_ratio"], 0.95)
            self.assertGreater(record["paper_color_cast_candidate_pixel_ratio"], 0.90)
            self.assertLessEqual(record["paper_color_cast_candidate_pixel_ratio"], 0.95)
            self.assertLess(_mean_channel_spread(processed), _mean_channel_spread(source) - 6.0)
            self.assertLess(_mean_luma_delta(source, processed), 4.0)
            self.assertLess(_changed_ratio_for_test(source, processed, (34, 40, 130, 95)), 0.001)
            self.assertLess(_changed_ratio_for_test(source, processed, (38, 130, 135, 135)), 0.001)
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])

            cast_guard = audit_summary["guardrails"]["paper_color_cast"]
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], 1)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_skipped_files"], 0)
            self.assertEqual(cast_guard["applied_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_sparse_text_cast", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_normalize_paper_color_cast_corrects_tiny_protected_color_marks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_tiny_red_mark.png": _uniform_cast_page(
                    background=(246, 243, 234),
                    tiny_red_mark=True,
                ),
                "private_tiny_blue_mark.png": _uniform_cast_page(
                    background=(246, 243, 234),
                    tiny_blue_mark=True,
                ),
            }
            mark_boxes = {
                "private_tiny_red_mark.png": (118, 76, 132, 90),
                "private_tiny_blue_mark.png": (74, 70, 91, 79),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_paper_color_cast=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            default_records = {record["source_relative_path"]: record for record in default_manifest["files"]}
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            for source_name, source in pages.items():
                default_record = default_records[source_name]
                record = records[source_name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                mark_box = mark_boxes[source_name]
                before_mark = source.crop(mark_box)
                after_mark = processed.crop(mark_box)
                before_mark_pixels = (
                    before_mark.get_flattened_data()
                    if hasattr(before_mark, "get_flattened_data")
                    else before_mark.getdata()
                )
                after_mark_pixels = (
                    after_mark.get_flattened_data()
                    if hasattr(after_mark, "get_flattened_data")
                    else after_mark.getdata()
                )
                colored_before = [
                    pixel
                    for pixel in before_mark_pixels
                    if max(pixel) - min(pixel) > 60 and min(pixel) < 120
                ]
                colored_after = [
                    pixel
                    for pixel in after_mark_pixels
                    if max(pixel) - min(pixel) > 60 and min(pixel) < 120
                ]

                self.assertFalse(default_record["paper_color_cast_normalized"], source_name)
                self.assertIn("normalize_paper_color_cast_disabled", default_record["operations"], source_name)
                self.assertTrue(record["paper_color_cast_normalized"], source_name)
                self.assertEqual(record["paper_color_cast_reason_code"], "applied_mild_uniform_scanner_cast")
                self.assertGreater(record["paper_color_cast_delta"], 6.0)
                self.assertLessEqual(record["paper_color_cast_delta"], 12.0)
                self.assertLessEqual(record["paper_color_cast_brightness_delta"], 4.0)
                self.assertGreater(record["paper_color_cast_changed_pixel_ratio"], 0.95)
                self.assertLessEqual(record["paper_color_cast_changed_pixel_ratio"], 1.0)
                self.assertGreater(record["paper_color_cast_candidate_pixel_ratio"], 0.98)
                self.assertLessEqual(record["paper_color_cast_candidate_pixel_ratio"], 1.0)
                self.assertLess(_mean_channel_spread(processed), _mean_channel_spread(source) - 6.0)
                self.assertLess(_mean_luma_delta(source, processed), 4.0)
                self.assertLess(_changed_ratio_for_test(source, processed, mark_box), 0.001, source_name)
                self.assertEqual(colored_after, colored_before, source_name)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [])

            guard = audit_summary["guardrails"]["paper_color_cast"]
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["paper_color_cast_skipped_files"], 0)
            self.assertEqual(guard["applied_files"], len(pages))
            self.assertEqual(guard["skipped_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_tiny_red_mark", audit_summary_text)
            self.assertNotIn("private_tiny_blue_mark", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_normalize_paper_color_cast_skips_dense_text_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = _dense_text_uniform_cast_page()
            source.save(input_dir / "private_dense_text_cast.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_paper_color_cast=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertFalse(record["paper_color_cast_normalized"])
            self.assertEqual(record["paper_color_cast_reason_code"], "protected_dark_content")
            self.assertIn("normalize_paper_color_cast_noop", record["operations"])
            self.assertLess(_changed_ratio_for_test(source, processed, (0, 0, source.width, source.height)), 0.001)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], 0)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_skipped_files"], 1)
            self.assertIn(
                "protected_dark_content",
                audit_summary["guardrails"]["paper_color_cast"]["skip_reason_code_distribution"],
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_dense_text_cast", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_normalize_paper_color_cast_skips_protected_color_and_archival_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_red_stamp.png": _uniform_cast_page(red_stamp=True),
                "private_blue_annotation.png": _uniform_cast_page(blue_annotation=True),
                "private_edge_tiny_red_mark.png": _uniform_cast_page(edge_tiny_red_mark=True),
                "private_handwriting.png": _uniform_cast_page(handwriting=True),
                "private_photo.png": _uniform_cast_page(photo=True),
                "private_chart.png": _uniform_cast_page(chart=True),
                "private_colored_paper.png": Image.new("RGB", (180, 140), (230, 214, 178)),
                "private_edge_mark.png": _uniform_cast_page(edge_mark=True),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_paper_color_cast=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            for source_name, source in pages.items():
                record = records[source_name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertFalse(record["paper_color_cast_normalized"], source_name)
                self.assertIn("normalize_paper_color_cast_noop", record["operations"], source_name)
                self.assertLess(_changed_ratio_for_test(source, processed, (0, 0, source.width, source.height)), 0.001)

            guard = audit_summary["guardrails"]["paper_color_cast"]
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], 0)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_skipped_files"], len(pages))
            self.assertEqual(guard["applied_files"], 0)
            self.assertEqual(guard["skipped_files"], len(pages))
            self.assertGreaterEqual(guard["protection_triggered_files"], 6)
            self.assertGreaterEqual(len(guard["skip_reason_code_distribution"]), 3)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_red_stamp", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_edge_shadow_improves_safe_shadow_with_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source_image = _synthetic_edge_shadow_repair_page()
            source_image.save(input_dir / "private_safe_edge_shadow.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_edge_shadow=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertTrue(record["edge_shadow_lightened"])
            self.assertEqual(record["edge_shadow_edges"], ["left"])
            self.assertIn("lighten_edge_shadow_conservative", record["operations"])
            self.assertGreater(_box_luma(processed, (0, 0, 14, 180)), _box_luma(source_image, (0, 0, 14, 180)) + 6.0)
            self.assertLess(_changed_ratio_for_test(source_image, processed, (58, 34, 200, 110)), 0.002)
            self.assertGreater(record["edge_shadow_changed_pixel_ratio"], 0.01)
            self.assertLess(record["edge_shadow_changed_pixel_ratio"], 0.08)
            self.assertGreater(record["edge_shadow_candidate_pixel_ratio"], record["edge_shadow_changed_pixel_ratio"])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])

            edge_guard = audit_summary["guardrails"]["edge_shadow"]
            self.assertTrue(audit_summary["operations"]["lighten_edge_shadow"])
            self.assertEqual(audit_summary["counts"]["edge_shadow_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["edge_shadow_skipped_files"], 0)
            self.assertEqual(edge_guard["applied_files"], 1)
            self.assertEqual(edge_guard["skipped_files"], 0)
            self.assertEqual(edge_guard["edge_distribution"]["left"], 1)
            self.assertIn("edge_shadow_candidate_pixel_ratio", audit_summary["metrics"])
            self.assertIn("candidate_pixel_ratio", edge_guard)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_edge_shadow", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_edge_shadow_skips_protected_content_and_uncertain_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_default_compatible_edge_shadow.png": _synthetic_edge_shadow_repair_page(),
                "private_center_red_annotation.png": _synthetic_edge_shadow_repair_page(center_red_annotation=True),
                "private_edge_text.png": _synthetic_edge_shadow_repair_page(edge_text=True),
                "private_page_number.png": _synthetic_edge_shadow_repair_page(page_number=True),
                "private_table_line.png": _synthetic_edge_shadow_repair_page(table_line=True),
                "private_red_stamp.png": _synthetic_edge_shadow_repair_page(red_stamp=True),
                "private_handwriting.png": _synthetic_edge_shadow_repair_page(handwriting=True),
                "private_binding_hole.png": _synthetic_edge_shadow_repair_page(binding_hole=True),
                "private_archive_line.png": _synthetic_edge_shadow_repair_page(archive_line=True),
                "private_photo_texture.png": _synthetic_photo_like_page(),
                "private_dense_texture.png": _synthetic_edge_shadow_dense_texture_page(),
                "private_high_contrast_normal.png": _synthetic_high_contrast_dense_text_page(),
                "private_low_confidence.png": _synthetic_edge_shadow_low_confidence_page(),
                "private_no_shadow.png": Image.new("RGB", (260, 180), (242, 242, 238)),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_edge_shadow=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            for record in default_manifest["files"]:
                self.assertFalse(record["edge_shadow_lightened"])
                self.assertIn("lighten_edge_shadow_disabled", record["operations"])
                self.assertEqual(
                    _sha256_for_test(input_dir / record["source_relative_path"]),
                    _sha256_for_test(default_process_dir / record["output_relative_path"]),
                )

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertTrue(records["private_default_compatible_edge_shadow.png"]["edge_shadow_lightened"])
            centered_red_record = records["private_center_red_annotation.png"]
            centered_red_processed = Image.open(process_dir / centered_red_record["output_relative_path"]).convert("RGB")
            self.assertTrue(centered_red_record["edge_shadow_lightened"])
            self.assertLess(
                _changed_ratio_for_test(
                    pages["private_center_red_annotation.png"],
                    centered_red_processed,
                    (104, 60, 156, 112),
                ),
                0.001,
            )
            skipped_expectations = {
                "private_edge_text.png": "risk",
                "private_page_number.png": "risk",
                "private_table_line.png": "risk",
                "private_red_stamp.png": "risk",
                "private_handwriting.png": "risk",
                "private_binding_hole.png": "risk",
                "private_archive_line.png": "risk",
                "private_photo_texture.png": "risk",
                "private_dense_texture.png": "risk",
                "private_high_contrast_normal.png": "risk",
                "private_low_confidence.png": "low tonal separation",
                "private_no_shadow.png": "low tonal separation",
            }
            for source_name, reason_fragment in skipped_expectations.items():
                record = records[source_name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertFalse(record["edge_shadow_lightened"], source_name)
                self.assertIn("lighten_edge_shadow_noop", record["operations"], source_name)
                self.assertIn(reason_fragment, record["edge_shadow_reason"], source_name)
                self.assertEqual(record["processing_audit"]["edge_shadow_changed_pixel_ratio"], 0.0, source_name)
                self.assertLess(
                    _changed_ratio_for_test(pages[source_name], processed, (0, 0, processed.width, processed.height)),
                    0.001,
                    source_name,
                )

            edge_guard = audit_summary["guardrails"]["edge_shadow"]
            self.assertEqual(audit_summary["counts"]["edge_shadow_lightened_files"], 2)
            self.assertEqual(audit_summary["counts"]["edge_shadow_skipped_files"], len(skipped_expectations))
            self.assertEqual(edge_guard["applied_files"], 2)
            self.assertEqual(edge_guard["skipped_files"], len(skipped_expectations))
            self.assertGreaterEqual(edge_guard["protection_triggered_files"], 8)
            self.assertGreaterEqual(edge_guard["low_confidence_skip_files"], 2)
            self.assertGreaterEqual(len(edge_guard["skip_reason_distribution"]), 4)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_default_compatible_edge_shadow", audit_summary_text)
            self.assertNotIn("private_red_stamp", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_corner_shadows_improves_safe_corner_shadow_with_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source_image = _synthetic_corner_shadow_page()
            source_image.save(input_dir / "private_safe_corner_shadow.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_corner_shadows=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertTrue(record["corner_shadows_lightened"])
            self.assertEqual(record["corner_shadows_reason_code"], "applied")
            self.assertEqual(record["corner_shadows_corners"], ["top_left"])
            self.assertIn("lighten_corner_shadows_conservative", record["operations"])
            self.assertGreater(_box_luma(processed, (0, 0, 54, 54)), _box_luma(source_image, (0, 0, 54, 54)) + 1.0)
            self.assertLess(_changed_ratio_for_test(source_image, processed, (72, 58, 188, 104)), 0.002)
            self.assertGreater(record["corner_shadows_changed_pixel_ratio"], 0.002)
            self.assertLess(record["corner_shadows_changed_pixel_ratio"], 0.06)
            self.assertGreater(record["corner_shadows_candidate_pixel_ratio"], record["corner_shadows_changed_pixel_ratio"])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])

            corner_guard = audit_summary["guardrails"]["corner_shadows"]
            self.assertTrue(audit_summary["operations"]["lighten_corner_shadows"])
            self.assertEqual(audit_summary["counts"]["corner_shadows_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["corner_shadows_skipped_files"], 0)
            self.assertEqual(corner_guard["applied_files"], 1)
            self.assertEqual(corner_guard["corner_distribution"]["top_left"], 1)
            self.assertIn("corner_shadows_candidate_pixel_ratio", audit_summary["metrics"])
            self.assertIn("candidate_pixel_ratio", corner_guard)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_corner_shadow", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_corner_shadows_improves_safe_paired_soft_vignettes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_safe_adjacent_soft_corner_vignettes.png": _synthetic_paired_soft_corner_vignette_page(
                    ("top_left", "top_right")
                ),
                "private_safe_diagonal_soft_corner_vignettes.png": _synthetic_paired_soft_corner_vignette_page(
                    ("top_left", "bottom_right")
                ),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_corner_shadows=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            for record in default_manifest["files"]:
                self.assertFalse(record["corner_shadows_lightened"])
                self.assertEqual(record["corner_shadows_reason_code"], "disabled")
                self.assertIn("lighten_corner_shadows_disabled", record["operations"])
                self.assertEqual(
                    _sha256_for_test(input_dir / record["source_relative_path"]),
                    _sha256_for_test(default_process_dir / record["output_relative_path"]),
                )

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            expected_corners = {
                "private_safe_adjacent_soft_corner_vignettes.png": ["top_left", "top_right"],
                "private_safe_diagonal_soft_corner_vignettes.png": ["top_left", "bottom_right"],
            }
            for source_name, corners in expected_corners.items():
                record = records[source_name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertTrue(record["corner_shadows_lightened"], source_name)
                self.assertEqual(record["corner_shadows_reason_code"], "applied", source_name)
                self.assertEqual(record["corner_shadows_corners"], corners, source_name)
                self.assertIn("lighten_corner_shadows_conservative", record["operations"], source_name)
                self.assertGreater(record["corner_shadows_delta"], 0.8, source_name)
                self.assertGreater(record["corner_shadows_changed_pixel_ratio"], 0.002, source_name)
                self.assertLessEqual(record["corner_shadows_changed_pixel_ratio"], 0.06, source_name)
                self.assertGreater(
                    record["corner_shadows_candidate_pixel_ratio"],
                    record["corner_shadows_changed_pixel_ratio"],
                    source_name,
                )
                self.assertGreater(
                    _box_luma(processed, _corner_test_box(corners[0], processed.size)),
                    _box_luma(pages[source_name], _corner_test_box(corners[0], processed.size)) + 0.8,
                    source_name,
                )
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [], source_name)

            corner_guard = audit_summary["guardrails"]["corner_shadows"]
            self.assertEqual(audit_summary["counts"]["corner_shadows_lightened_files"], 2)
            self.assertEqual(corner_guard["applied_files"], 2)
            self.assertEqual(corner_guard["reason_code_distribution"]["applied"], 2)
            self.assertIn("candidate_pixel_ratio", corner_guard)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_adjacent_soft_corner_vignettes", audit_summary_text)
            self.assertNotIn("private_safe_diagonal_soft_corner_vignettes", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_corner_shadows_preserves_corner_content_and_skips_uncertain_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_safe_corner_shadow.png": _synthetic_corner_shadow_page(),
                "private_corner_page_number.png": _synthetic_corner_shadow_page(page_number=True),
                "private_corner_stamp.png": _synthetic_corner_shadow_page(red_stamp=True),
                "private_corner_handwriting.png": _synthetic_corner_shadow_page(handwriting=True),
                "private_corner_table_line.png": _synthetic_corner_shadow_page(table_line=True),
                "private_corner_page_border.png": _synthetic_corner_shadow_page(page_border=True),
                "private_corner_blue_mark.png": _synthetic_corner_shadow_page(color_mark=True),
                "private_corner_photo_texture.png": _synthetic_photo_like_page(),
                "private_corner_dark_texture.png": _synthetic_corner_dark_texture_page(),
                "private_paired_soft_corner_page_number.png": _synthetic_paired_soft_corner_vignette_page(
                    ("top_left", "top_right"), page_number=True
                ),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_corner_shadows=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            for record in default_manifest["files"]:
                self.assertFalse(record["corner_shadows_lightened"])
                self.assertEqual(record["corner_shadows_reason_code"], "disabled")
                self.assertIn("lighten_corner_shadows_disabled", record["operations"])
                self.assertEqual(
                    _sha256_for_test(input_dir / record["source_relative_path"]),
                    _sha256_for_test(default_process_dir / record["output_relative_path"]),
                )

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertTrue(records["private_safe_corner_shadow.png"]["corner_shadows_lightened"])
            skipped_expectations = {
                "private_corner_page_number.png": "protected_content",
                "private_corner_stamp.png": "color_content",
                "private_corner_handwriting.png": "protected_content",
                "private_corner_table_line.png": "protected_content",
                "private_corner_page_border.png": "protected_content",
                "private_corner_blue_mark.png": "color_content",
                "private_corner_photo_texture.png": "protected_content",
                "private_corner_dark_texture.png": "detail_too_high",
                "private_paired_soft_corner_page_number.png": "protected_content",
            }
            for source_name, reason_code in skipped_expectations.items():
                record = records[source_name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertFalse(record["corner_shadows_lightened"], source_name)
                self.assertIn("lighten_corner_shadows_noop", record["operations"], source_name)
                self.assertEqual(record["corner_shadows_reason_code"], reason_code, source_name)
                self.assertEqual(record["processing_audit"]["corner_shadows_changed_pixel_ratio"], 0.0, source_name)
                self.assertLess(
                    _changed_ratio_for_test(pages[source_name], processed, (0, 0, processed.width, processed.height)),
                    0.001,
                    source_name,
                )

            corner_guard = audit_summary["guardrails"]["corner_shadows"]
            self.assertEqual(audit_summary["counts"]["corner_shadows_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["corner_shadows_skipped_files"], len(skipped_expectations))
            self.assertEqual(corner_guard["applied_files"], 1)
            self.assertEqual(corner_guard["skipped_files"], len(skipped_expectations))
            self.assertGreaterEqual(corner_guard["protection_triggered_files"], 6)
            self.assertGreaterEqual(len(corner_guard["skip_reason_code_distribution"]), 3)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_corner_page_number", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_combined_processing_chain_with_edge_shadow_audit_stays_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_edge_shadow_repair_page().save(input_dir / "private_combined_edge_shadow.png", dpi=(300, 300))

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--process-out",
                    str(process_dir),
                    "--auto-crop",
                    "--deskew",
                    "--trim-dark-border",
                    "--despeckle",
                    "--normalize-tones",
                    "--lighten-edge-shadow",
                    "--lighten-background-stains",
                    "--lighten-scanlines",
                    "--enhance-faded-text",
                    "--sharpen-text-edges",
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            manifest = json.loads((process_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertTrue(audit_summary["operations"]["auto_crop"])
            self.assertTrue(audit_summary["operations"]["deskew"])
            self.assertTrue(audit_summary["operations"]["trim_dark_border"])
            self.assertTrue(audit_summary["operations"]["despeckle"])
            self.assertTrue(audit_summary["operations"]["normalize_tones"])
            self.assertTrue(audit_summary["operations"]["lighten_edge_shadow"])
            self.assertTrue(audit_summary["operations"]["lighten_background_stains"])
            self.assertTrue(audit_summary["operations"]["lighten_scanlines"])
            self.assertTrue(audit_summary["operations"]["enhance_faded_text"])
            self.assertTrue(audit_summary["operations"]["sharpen_text_edges"])
            self.assertIn("edge_shadow", audit_summary["guardrails"])
            self.assertIn("background_stains", audit_summary["guardrails"])
            self.assertIn("scanlines", audit_summary["guardrails"])
            self.assertIn("faded_text", audit_summary["guardrails"])
            self.assertIn("text_edges", audit_summary["guardrails"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            self.assertNotIn("private_combined_edge_shadow", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_scanlines_improves_horizontal_and_vertical_lines_with_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_horizontal_scanline.png": _synthetic_repair_scanline_page("horizontal"),
                "private_vertical_scanline.png": _synthetic_repair_scanline_page("vertical"),
                "private_broken_horizontal_scanline.png": _synthetic_broken_repair_scanline_page("horizontal"),
                "private_broken_vertical_scanline.png": _synthetic_broken_repair_scanline_page("vertical"),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_scanlines=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            for source_name, orientation in (
                ("private_horizontal_scanline.png", "horizontal"),
                ("private_vertical_scanline.png", "vertical"),
                ("private_broken_horizontal_scanline.png", "horizontal"),
                ("private_broken_vertical_scanline.png", "vertical"),
            ):
                record = records[source_name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                line_box = (12, 132, 248, 134) if orientation == "horizontal" else (212, 18, 214, 164)
                protected_box = (36, 36, 164, 96)
                self.assertTrue(record["scanlines_lightened"], source_name)
                self.assertEqual(record["scanlines_orientation"], orientation)
                self.assertIn("lighten_scanlines_conservative", record["operations"])
                min_line_delta = 0.4 if "broken" in source_name else 3.0
                self.assertGreater(
                    _box_luma(processed, line_box),
                    _box_luma(pages[source_name], line_box) + min_line_delta,
                    source_name,
                )
                self.assertLess(_changed_ratio_for_test(pages[source_name], processed, protected_box), 0.002, source_name)
                self.assertGreater(record["scanlines_changed_pixel_ratio"], 0.0007, source_name)
                self.assertLess(record["scanlines_changed_pixel_ratio"], 0.035, source_name)
                self.assertGreater(record["scanlines_delta"], 3.0, source_name)
                self.assertGreater(record["scanlines_candidate_pixel_ratio"], 0.0007, source_name)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [], source_name)

            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 4)
            self.assertEqual(audit_summary["counts"]["scanlines_skipped_files"], 0)
            scanline_guard = audit_summary["guardrails"]["scanlines"]
            self.assertEqual(scanline_guard["applied_files"], 4)
            self.assertEqual(scanline_guard["skipped_files"], 0)
            self.assertEqual(scanline_guard["direction_distribution"]["horizontal"], 2)
            self.assertEqual(scanline_guard["direction_distribution"]["vertical"], 2)
            self.assertIn("scanlines_changed_pixel_ratio", audit_summary["metrics"])
            self.assertIn("changed_pixel_ratio", scanline_guard)
            self.assertEqual(scanline_guard["protection_triggered_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_horizontal_scanline", audit_summary_text)
            self.assertNotIn("private_vertical_scanline", audit_summary_text)
            self.assertNotIn("private_broken_horizontal_scanline", audit_summary_text)
            self.assertNotIn("private_broken_vertical_scanline", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_lighten_scanlines_skips_protected_content_and_uncertain_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_default_compatible_scanline.png": _synthetic_repair_scanline_page("horizontal"),
                "private_table_line.png": _synthetic_repair_scanline_page("horizontal", table_line=True),
                "private_page_number.png": _synthetic_repair_scanline_page("horizontal", page_number=True),
                "private_red_stamp.png": _synthetic_repair_scanline_page("horizontal", red_stamp=True),
                "private_handwriting.png": _synthetic_repair_scanline_page("horizontal", handwriting=True),
                "private_underlined_text.png": _synthetic_repair_scanline_page(
                    "horizontal", scanline=False, underline=True
                ),
                "private_header_footer.png": _synthetic_repair_scanline_page(
                    "horizontal", scanline=False, header_footer=True
                ),
                "private_photo_texture.png": _synthetic_photo_like_page(),
                "private_dense_table.png": _synthetic_dense_table_scanline_page(),
                "private_low_confidence.png": _synthetic_scanline_low_confidence_page(),
                "private_edge_archive_line.png": _synthetic_repair_scanline_page("horizontal", edge_archive_line=True),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_scanlines=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            for record in default_manifest["files"]:
                self.assertFalse(record["scanlines_lightened"])
                self.assertIn("lighten_scanlines_disabled", record["operations"])
                self.assertEqual(
                    _sha256_for_test(input_dir / record["source_relative_path"]),
                    _sha256_for_test(default_process_dir / record["output_relative_path"]),
                )

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertTrue(records["private_default_compatible_scanline.png"]["scanlines_lightened"])
            skipped_expectations = {
                "private_table_line.png": "SCANLINE_LOW_CONFIDENCE",
                "private_page_number.png": "SCANLINE_EDGE_CONTENT_RISK",
                "private_red_stamp.png": "SCANLINE_COLOR_CONTENT_RISK",
                "private_handwriting.png": "SCANLINE_LOW_CONFIDENCE",
                "private_underlined_text.png": "SCANLINE_LOW_CONFIDENCE",
                "private_header_footer.png": "SCANLINE_LOW_CONFIDENCE",
                "private_photo_texture.png": "texture risk",
                "private_dense_table.png": "foreground too dense",
                "private_low_confidence.png": "low-confidence",
                "private_edge_archive_line.png": "SCANLINE_EDGE_CONTENT_RISK",
            }
            for source_name, reason_fragment in skipped_expectations.items():
                record = records[source_name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertFalse(record["scanlines_lightened"], source_name)
                self.assertIn("lighten_scanlines_noop", record["operations"], source_name)
                self.assertIn(reason_fragment, record["scanlines_reason"], source_name)
                if reason_fragment.startswith("SCANLINE_"):
                    self.assertRegex(record["scanlines_reason"], r"[\u4e00-\u9fff]+", source_name)
                self.assertEqual(record["processing_audit"]["scanlines_changed_pixel_ratio"], 0.0, source_name)
                self.assertLess(
                    _changed_ratio_for_test(pages[source_name], processed, (0, 0, processed.width, processed.height)),
                    0.001,
                    source_name,
                )

            scanline_guard = audit_summary["guardrails"]["scanlines"]
            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["scanlines_skipped_files"], len(skipped_expectations))
            self.assertEqual(scanline_guard["applied_files"], 1)
            self.assertEqual(scanline_guard["skipped_files"], len(skipped_expectations))
            self.assertGreaterEqual(scanline_guard["protection_triggered_files"], 4)
            self.assertGreaterEqual(scanline_guard["low_confidence_skip_files"], 2)
            self.assertGreaterEqual(len(scanline_guard["skip_reason_distribution"]), 4)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_default_compatible_scanline", audit_summary_text)
            self.assertNotIn("private_red_stamp", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_combined_processing_chain_with_scanline_audit_stays_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_repair_scanline_page("horizontal").save(input_dir / "private_combined_scanline.png", dpi=(300, 300))

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(output_dir),
                    "--process-out",
                    str(process_dir),
                    "--auto-crop",
                    "--deskew",
                    "--trim-dark-border",
                    "--despeckle",
                    "--lighten-background-stains",
                    "--lighten-scanlines",
                    "--normalize-tones",
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            manifest = json.loads((process_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertTrue(audit_summary["operations"]["auto_crop"])
            self.assertTrue(audit_summary["operations"]["deskew"])
            self.assertTrue(audit_summary["operations"]["trim_dark_border"])
            self.assertTrue(audit_summary["operations"]["despeckle"])
            self.assertTrue(audit_summary["operations"]["lighten_background_stains"])
            self.assertTrue(audit_summary["operations"]["lighten_scanlines"])
            self.assertTrue(audit_summary["operations"]["normalize_tones"])
            self.assertIn("scanlines", audit_summary["guardrails"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_combined_scanline", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_cumulative_change_guard_reverts_high_risk_stacked_change_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_high_risk_stacked_repairs.png"
            Image.new("RGB", (40, 40), (245, 245, 245)).save(source, dpi=(300, 300))

            def high_risk_background_change(image: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                output = Image.new(image.mode, image.size, (20, 20, 20))
                return processing_module.BackgroundStainLighteningResult(
                    output, True, "synthetic high risk background repair", 245.0, 20.0, 225.0, 1.0, 1.0
                )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_lighten_background_stains_conservative",
                side_effect=high_risk_background_change,
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(lighten_background_stains=True, workers=1),
                )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]
            processed = Image.open(process_dir / record["output_relative_path"])

            self.assertEqual(record["status"], "processed")
            self.assertIn("cumulative_change_guard_reverted_to_source", record["operations"])
            self.assertIn("cumulative_change_guard_reverted_to_source", record["processing_warnings"])
            self.assertFalse(record["background_stains_lightened"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "reverted_to_source")
            self.assertTrue(audit["cumulative_change_guard_reverted"])
            self.assertIn("cumulative_change_score", audit["cumulative_change_guard_reasons"])
            self.assertGreater(audit["cumulative_change_score"], 1.0)
            self.assertIsNone(ImageChops.difference(Image.open(source).convert("RGB"), processed.convert("RGB")).getbbox())
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_checked_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["cumulative_change_guard"]["reverted_files"], 1)
            self.assertGreater(audit_summary["guardrails"]["cumulative_change_guard"]["max_score"], 1.0)
            self.assertEqual(
                audit_summary["guardrails"]["cumulative_change_guard"]["reason_distribution"][
                    "cumulative_change_score"
                ],
                1,
            )
            self.assertNotIn("private_high_risk_stacked_repairs", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_processed_output_safety_guard_reverts_washed_out_clipped_foreground_loss_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_washed_out_processed_output.png"
            image = Image.new("RGB", (180, 120), (216, 216, 212))
            draw = ImageDraw.Draw(image)
            for y in (24, 44, 64, 84):
                draw.rectangle((24, y, 146, y + 5), fill=(36, 36, 36))
            draw.rectangle((8, 104, 44, 110), fill=(48, 48, 48))
            image.save(source, dpi=(300, 300))

            def washed_out_repair(candidate: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                output = Image.new(candidate.mode, candidate.size, (255, 255, 255))
                return processing_module.BackgroundStainLighteningResult(
                    output,
                    True,
                    "synthetic washed out derivative",
                    216.0,
                    255.0,
                    29.0,
                    1.0,
                    1.0,
                )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_lighten_background_stains_conservative",
                side_effect=washed_out_repair,
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(lighten_background_stains=True, workers=1),
                )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["background_stains_lightened"])
            self.assertEqual(record["background_stains_reason"], "reverted by processed output safety guard")
            self.assertIn("processed_output_safety_guard_reverted_to_source", record["operations"])
            self.assertIn("processed_output_safety_guard_reverted_to_source", record["processing_warnings"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["processed_output_safety_guard_action"], "reverted_to_source")
            self.assertTrue(audit["processed_output_safety_guard_reverted"])
            self.assertEqual(audit["processed_output_safety_guard_reason_code"], "processed_output_quality_reverted")
            self.assertIn("near_white_saturation", audit["processed_output_safety_guard_reasons"])
            self.assertIn("highlight_clipping", audit["processed_output_safety_guard_reasons"])
            self.assertIn("dark_foreground_loss", audit["processed_output_safety_guard_reasons"])
            with Image.open(process_dir / "images" / "private_washed_out_processed_output.png") as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["processed_output_safety_guard_checked_files"], 1)
            self.assertEqual(audit_summary["counts"]["processed_output_safety_guard_reverted_files"], 1)
            self.assertEqual(audit_summary["counts"]["processed_output_washout_guard_reverted_files"], 1)
            self.assertEqual(audit_summary["counts"]["processed_output_clipping_guard_reverted_files"], 1)
            self.assertEqual(audit_summary["counts"]["processed_output_foreground_loss_guard_reverted_files"], 1)
            processed_output_guard = audit_summary["guardrails"]["processed_output_safety_guard"]
            self.assertEqual(processed_output_guard["reverted_files"], 1)
            self.assertEqual(processed_output_guard["reason_code_distribution"]["processed_output_quality_reverted"], 1)
            self.assertEqual(processed_output_guard["reason_distribution"]["near_white_saturation"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_washed_out_processed_output", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_process_images_writes_audit_summary_and_retry_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "private_success.png", dpi=(300, 300))
            failed_after_scan = input_dir / "private_failed.png"
            Image.new("RGB", (32, 24), "white").save(failed_after_scan, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            failed_after_scan.write_text("not an image anymore", encoding="utf-8")
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))

            retry_manifest = json.loads((process_dir / "processing_retry_manifest.json").read_text(encoding="utf-8"))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertEqual(manifest["summary"]["failed_files"], 1)
            self.assertEqual(manifest["summary"]["retry_list_files"], 1)
            self.assertEqual(retry_manifest["summary"]["failed_files"], 1)
            self.assertEqual(retry_manifest["files"][0]["source_relative_path"], "private_failed.png")
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 1)
            self.assertEqual(audit_summary["counts"]["retry_list_files"], 1)
            self.assertEqual(audit_summary["counts"]["processing_warning_files"], 0)
            self.assertIn("pixel_change_ratio", audit_summary["metrics"])
            self.assertIn("pixel_change_ratio", audit_summary["distributions"])
            self.assertIn("operation_timings", audit_summary["timing"])
            self.assertEqual(audit_summary["timing"]["operation_timings"]["auto_crop"]["enabled"], False)
            self.assertEqual(audit_summary["timing"]["operation_timings"]["deskew"]["file_count"], 0)
            self.assertTrue(audit_summary["guardrails"]["enabled"])
            for forbidden in ["private_success.png", "private_failed.png", "relative_path", "sha256", str(input_dir)]:
                self.assertNotIn(forbidden, audit_summary_text)

    def test_processing_review_package_groups_sensitive_local_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "processing_manifest.json"
            manifest = {
                "schema_version": "scan-qc.processing.v1",
                "generated_at": "2026-01-02T03:04:05+00:00",
                "project": {"project_id": "p1", "batch_id": "b1"},
                "summary": {"total_files": 4, "processed_files": 2, "resumed_files": 1, "failed_files": 1},
                "operations": ["deskew_conservative", "dark_border_trim_conservative"],
                "files": [
                    {
                        "source_relative_path": "source/page_001.png",
                        "output_relative_path": "images/page_001.png",
                        "source_sha256": "source-hash-1",
                        "output_sha256": "output-hash-1",
                        "status": "processed",
                        "deskewed": True,
                        "deskew_reason": "detected skew",
                        "skew_angle_degrees": 1.25,
                        "skew_confidence": 0.4,
                        "dark_border_trimmed": True,
                        "dark_border_reason": "trimmed",
                        "cropped": False,
                        "despeckled": True,
                        "despeckle_reason": "isolated dark pixels",
                        "despeckle_pixels_changed": 3,
                        "processing_warnings": ["pixel_change_ratio exceeds review threshold"],
                        "processing_audit": {
                            "pixel_change_ratio": 0.2,
                            "guardrail_failures": ["manual guardrail warning"],
                        },
                        "operations": ["deskew", "trim", "despeckle"],
                    },
                    {
                        "source_relative_path": "source/page_002.png",
                        "output_relative_path": "images/page_002.png",
                        "status": "resumed",
                        "resumed": True,
                        "cropped": True,
                        "crop_bbox": [1, 1, 20, 20],
                        "processing_warnings": [],
                        "processing_audit": {},
                        "operations": ["resume_skip_existing_derivative"],
                    },
                    {
                        "source_relative_path": "source/page_003.png",
                        "output_relative_path": None,
                        "status": "failed",
                        "failure_reason": "source image is not openable",
                        "processing_warnings": [],
                        "operations": [],
                    },
                    {
                        "source_relative_path": "../unsafe/page_004.png",
                        "output_relative_path": "/unsafe/output.png",
                        "status": "processed",
                        "processing_warnings": [],
                        "operations": [],
                    },
                ],
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            package = build_processing_review_package(manifest, manifest_path)

            self.assertEqual(package["schema_version"], "scan-qc.processing-review.v1")
            self.assertEqual(package["generated_at"], "2026-01-02T03:04:05+00:00")
            self.assertTrue(package["privacy"]["local_only"])
            self.assertFalse(package["privacy"]["aggregate_only"])
            self.assertEqual(package["summary"]["processed_files"], 2)
            self.assertEqual(package["summary"]["resumed_files"], 1)
            self.assertEqual(package["summary"]["failed_files"], 1)
            self.assertEqual(package["groups"]["deskewed"]["count"], 1)
            self.assertEqual(package["groups"]["dark_border_trimmed"]["count"], 1)
            self.assertEqual(package["groups"]["cropped"]["count"], 1)
            self.assertEqual(package["groups"]["despeckled"]["count"], 1)
            self.assertEqual(package["groups"]["failed"]["count"], 1)
            self.assertEqual(package["groups"]["guardrail_warnings"]["count"], 1)
            self.assertIsNone(package["files"][0]["before_href"])
            self.assertEqual(package["files"][0]["after_href"], "images/page_001.png")
            self.assertIsNone(package["files"][3]["before_href"])
            self.assertIsNone(package["files"][3]["after_href"])

    def test_processing_review_package_cli_writes_deterministic_json_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "processing_manifest.json"
            out_dir = root / "review"
            manifest = {
                "schema_version": "scan-qc.processing.v1",
                "generated_at": "2026-01-02T03:04:05+00:00",
                "summary": {"total_files": 1},
                "files": [
                    {
                        "source_relative_path": "source/page_<001>.png",
                        "output_relative_path": "images/page_001.png",
                        "status": "processed",
                        "deskewed": True,
                        "dark_border_trimmed": False,
                        "cropped": False,
                        "despeckled": False,
                        "processing_warnings": [],
                        "processing_audit": {},
                        "operations": ["deskew"],
                    }
                ],
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            self.assertEqual(main(["processing-review-package", "--manifest", str(manifest_path), "--out", str(out_dir)]), 0)
            first_json = (out_dir / "processing_review_package.json").read_text(encoding="utf-8")
            first_html = (out_dir / "processing_review_package.html").read_text(encoding="utf-8")
            self.assertEqual(main(["processing-review-package", "--manifest", str(manifest_path), "--out", str(out_dir)]), 0)
            self.assertEqual(first_json, (out_dir / "processing_review_package.json").read_text(encoding="utf-8"))
            self.assertEqual(first_html, (out_dir / "processing_review_package.html").read_text(encoding="utf-8"))

            self.assertIn("Sensitive local processing review package", first_json)
            self.assertIn("Sensitive local processing review package", first_html)
            self.assertIn("Deskewed", first_html)
            self.assertIn("Dark Border Trimmed", first_html)
            self.assertIn("Failed", first_html)
            self.assertIn("<code>source/page_&lt;001&gt;.png</code>", first_html)
            self.assertIn('href="../images/page_001.png"', first_html)
            self.assertNotIn("data:image", first_html.lower())
            self.assertNotIn("<img", first_html.lower())

    def test_resume_processing_skips_existing_successful_derivatives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (40, 30), "white").save(input_dir / "A001_0001.png", dpi=(300, 300))
            Image.new("RGB", (40, 30), "white").save(input_dir / "A001_0002.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            first = process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            first_hashes = {
                record["source_relative_path"]: record["output_sha256"]
                for record in first["files"]
                if record["status"] == "processed"
            }
            resumed = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(resume_processing=True, workers=1),
            )

            self.assertEqual(resumed["summary"]["processed_files"], 0)
            self.assertEqual(resumed["summary"]["resumed_files"], 2)
            self.assertEqual(resumed["summary"]["skipped_due_to_resume"], 2)
            self.assertEqual(resumed["summary"]["reprocessed_files"], 0)
            records = {record["source_relative_path"]: record for record in resumed["files"]}
            self.assertEqual({record["status"] for record in resumed["files"]}, {"resumed"})
            self.assertEqual(records["A001_0001.png"]["output_sha256"], first_hashes["A001_0001.png"])
            audit = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(audit["operations"]["resume_processing"])
            self.assertEqual(audit["counts"]["skipped_due_to_resume"], 2)
            self.assertEqual(audit["counts"]["existing_derivative_reused_files"], 2)
            self.assertEqual(audit["metrics"]["pixel_change_ratio"]["count"], 2)

    def test_resume_processing_reprocesses_when_processing_options_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (40, 30), "white").save(input_dir / "A001_0001.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            resumed = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(resume_processing=True, auto_crop=True, workers=1),
            )

            self.assertEqual(resumed["summary"]["processed_files"], 1)
            self.assertEqual(resumed["summary"]["resumed_files"], 0)
            self.assertEqual(resumed["summary"]["reprocessed_files"], 1)
            self.assertEqual(resumed["files"][0]["status"], "processed")
            self.assertIn("processing_options_fingerprint", resumed["files"][0])

    def test_resume_processing_reprocesses_when_source_no_longer_matches_report_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            Image.new("RGB", (40, 30), "white").save(source, dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            Image.new("RGB", (40, 30), (220, 220, 220)).save(source, dpi=(300, 300))
            resumed = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(resume_processing=True, workers=1),
            )

            self.assertEqual(resumed["summary"]["processed_files"], 1)
            self.assertEqual(resumed["summary"]["resumed_files"], 0)
            self.assertEqual(resumed["summary"]["reprocessed_files"], 1)
            self.assertEqual(resumed["files"][0]["status"], "processed")

    def test_resume_processing_reprocesses_missing_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (40, 30), "white").save(input_dir / "A001_0001.png", dpi=(300, 300))
            Image.new("RGB", (40, 30), (230, 230, 230)).save(input_dir / "A001_0002.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            missing = process_dir / "images" / "A001_0002.png"
            missing.unlink()

            resumed = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(resume_processing=True, workers=1),
            )

            records = {record["source_relative_path"]: record for record in resumed["files"]}
            self.assertEqual(records["A001_0001.png"]["status"], "resumed")
            self.assertEqual(records["A001_0002.png"]["status"], "processed")
            self.assertTrue(records["A001_0002.png"]["reprocessed"])
            self.assertTrue(missing.exists())
            self.assertEqual(resumed["summary"]["processed_files"], 1)
            self.assertEqual(resumed["summary"]["skipped_due_to_resume"], 1)
            self.assertEqual(resumed["summary"]["reprocessed_files"], 1)
            audit = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["metrics"]["pixel_change_ratio"]["count"], 2)

    def test_resume_processing_retries_previous_failed_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            good = input_dir / "A001_0001.png"
            failed_then_fixed = input_dir / "A001_0002.png"
            Image.new("RGB", (40, 30), "white").save(good)
            Image.new("RGB", (40, 30), "white").save(failed_then_fixed)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=1))
            failed_then_fixed.write_text("not an image anymore", encoding="utf-8")
            first = process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(first["summary"]["failed_files"], 1)

            Image.new("RGB", (40, 30), "white").save(failed_then_fixed)
            resumed = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(resume_processing=True, workers=1),
            )

            records = {record["source_relative_path"]: record for record in resumed["files"]}
            self.assertEqual(records["A001_0001.png"]["status"], "resumed")
            self.assertEqual(records["A001_0002.png"]["status"], "processed")
            self.assertTrue(records["A001_0002.png"]["reprocessed"])
            self.assertEqual(resumed["summary"]["failed_files"], 0)
            self.assertEqual(resumed["summary"]["retry_list_files"], 0)
            retry_manifest = json.loads((process_dir / "processing_retry_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(retry_manifest["summary"]["failed_files"], 0)

    def test_processing_failure_summary_gives_aggregate_chinese_recovery_advice(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-recovery-advice-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "processed"
            metadata_dir = root / "metadata"
            summary = build_production_run_summary(
                config=ProductionRunConfig(
                    input_dir=input_dir,
                    derivative_output_dir=output_dir,
                    metadata_output_dir=metadata_dir,
                ),
                report={
                    "summary": {
                        "total_files": 5,
                        "openable_files": 5,
                        "p0_findings": 0,
                        "p1_findings": 0,
                        "p2_findings": 0,
                        "total_findings": 0,
                        "performance": {},
                    }
                },
                processing_manifest={
                    "image_root": str(output_dir / "images"),
                    "summary": {
                        "total_files": 5,
                        "processed_files": 2,
                        "resumed_files": 1,
                        "skipped_files": 0,
                        "failed_files": 2,
                        "retry_list_files": 2,
                        "performance": {},
                    },
                },
                admin_report_dir=metadata_dir / "admin_reports",
                generated_at="2026-01-01T00:00:00+00:00",
            )

            guidance = summary["recovery_guidance"]
            guidance_text = json.dumps(guidance, ensure_ascii=False, sort_keys=True)
            self.assertTrue(guidance["aggregate_only"])
            self.assertEqual(guidance["kind"], "processing_failed_retryable")
            self.assertEqual(guidance["failed_files"], 2)
            self.assertEqual(guidance["successful_output_files"], 3)
            self.assertEqual(guidance["derivative_images_ready"], 3)
            self.assertEqual(guidance["missing_output_files"], 2)
            self.assertTrue(guidance["can_restart_fill_missing_outputs"])
            self.assertIn("本批有 2 张处理失败", guidance["message_zh"])
            self.assertIn("已成功输出 3 张", guidance["message_zh"])
            self.assertIn("只补齐缺失", guidance_text)
            self.assertIn("可读取", guidance_text)
            self.assertIn("可写入", guidance_text)
            self.assertIn("常见图片格式", guidance_text)
            reuse_summary = summary["local_reuse_summary"]
            self.assertTrue(reuse_summary["aggregate_only"])
            self.assertEqual(reuse_summary["total_files"], 5)
            self.assertEqual(reuse_summary["reused_files"], 0)
            self.assertEqual(reuse_summary["reprocessed_files"], 0)
            self.assertEqual(reuse_summary["failed_files"], 2)
            self.assertEqual(reuse_summary["remaining_files"], 2)
            self.assertIn("本批共 5 张", reuse_summary["message_zh"])
            self.assertIn("复用 0 张", reuse_summary["message_zh"])
            self.assertIn("重新处理 0 张", reuse_summary["message_zh"])
            self.assertIn("仍失败 2 张", reuse_summary["message_zh"])
            self.assertIn("剩余待处理 2 张", reuse_summary["message_zh"])

    def test_processing_failure_recovery_advice_is_public_aggregate_only(self) -> None:
        private_values = [
            "/Users/private/archive/input",
            "Secret_Case_0001.tif",
            "a" * 64,
            "OCR: 张三身份证 110101199001010011",
            "thumbnail",
            "Traceback File worker.py line 42",
            "data:image/png;base64",
        ]
        with tempfile.TemporaryDirectory(prefix="scan-processing-recovery-private-") as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "processed"
            metadata_dir = root / "metadata"
            summary = build_production_run_summary(
                config=ProductionRunConfig(
                    input_dir=Path(private_values[0]),
                    derivative_output_dir=output_dir,
                    metadata_output_dir=metadata_dir,
                ),
                report={
                    "summary": {
                        "total_files": 3,
                        "openable_files": 3,
                        "p0_findings": 0,
                        "p1_findings": 0,
                        "p2_findings": 0,
                        "total_findings": 0,
                        "performance": {"private_note": private_values[3]},
                    },
                    "files": [{"relative_path": private_values[1], "sha256": private_values[2]}],
                },
                processing_manifest={
                    "image_root": str(output_dir / "images"),
                    "summary": {
                        "total_files": 3,
                        "processed_files": 1,
                        "resumed_files": 0,
                        "skipped_files": 0,
                        "failed_files": 2,
                        "retry_list_files": 0,
                        "performance": {"stack": private_values[5]},
                    },
                    "files": [{"thumbnail": private_values[6], "failure_reason": private_values[5]}],
                },
                admin_report_dir=metadata_dir / "admin_reports",
                generated_at="2026-01-01T00:00:00+00:00",
                stage_timings={"scan": 0.1, "process": 0.2, "summarize": 0.3},
            )

            guidance_text = json.dumps(summary["recovery_guidance"], ensure_ascii=False, sort_keys=True)
            reuse_text = json.dumps(summary["local_reuse_summary"], ensure_ascii=False, sort_keys=True)
            stage_timings_text = json.dumps(summary["stage_timings"], ensure_ascii=False, sort_keys=True)
            self.assertIn("本批有 2 张处理失败", guidance_text)
            self.assertIn("已成功输出 1 张", guidance_text)
            self.assertIn("本批共 3 张", reuse_text)
            self.assertTrue(summary["stage_timings"]["aggregate_only"])
            self.assertEqual(
                [(stage["id"], stage["label_zh"], stage["status"]) for stage in summary["stage_timings"]["stages"]],
                [
                    ("scan", "检查扫描图片", "completed"),
                    ("process", "生成处理后图片", "completed"),
                    ("summarize", "整理处理结果", "completed"),
                ],
            )
            for private_value in private_values:
                self.assertNotIn(private_value, guidance_text)
                self.assertNotIn(private_value, reuse_text)
                self.assertNotIn(private_value, stage_timings_text)
            self.assertNotIn(".tif", guidance_text)
            self.assertNotIn(".tif", reuse_text)
            self.assertNotIn(".tif", stage_timings_text)
            self.assertNotIn("sha256", guidance_text.lower())
            self.assertNotIn("sha256", reuse_text.lower())
            self.assertNotIn("sha256", stage_timings_text.lower())
            self.assertNotIn("traceback", guidance_text.lower())
            self.assertNotIn("traceback", reuse_text.lower())
            self.assertNotIn("traceback", stage_timings_text.lower())
            self.assertNotIn("ocr", guidance_text.lower())
            self.assertNotIn("ocr", reuse_text.lower())
            self.assertNotIn("ocr", stage_timings_text.lower())

    def test_running_progress_includes_aggregate_processing_rate_and_wait_estimate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-production-progress-rate-") as temp_dir:
            metadata_dir = Path(temp_dir)

            _write_progress(
                metadata_dir,
                "running",
                [
                    {
                        "id": "scan",
                        "label": "检查扫描图片",
                        "state": "completed",
                        "total_items": None,
                        "completed_items": 10,
                    },
                    {
                        "id": "process",
                        "label": "生成处理后图片",
                        "state": "running",
                        "total_items": 10,
                        "completed_items": 4,
                    },
                    {
                        "id": "summarize",
                        "label": "整理处理结果",
                        "state": "pending",
                        "total_items": None,
                        "completed_items": None,
                    },
                ],
                current_step="process",
                stage_timings={"process": 120.0},
            )

            progress = json.loads((metadata_dir / "production_run_progress.json").read_text(encoding="utf-8"))
            aggregate = progress["aggregate_processing"]
            self.assertTrue(aggregate["aggregate_only"])
            self.assertEqual(aggregate["total_images"], 10)
            self.assertEqual(aggregate["processed_images"], 4)
            self.assertEqual(aggregate["remaining_images"], 6)
            self.assertEqual(aggregate["elapsed_seconds"], 120.0)
            self.assertEqual(aggregate["images_per_minute"], 2.0)
            self.assertEqual(aggregate["estimated_remaining_seconds"], 180.0)
            self.assertIsNone(aggregate["unavailable_reason"])

    def test_finished_summary_retains_final_aggregate_processing_rate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-production-summary-rate-") as temp_dir:
            root = Path(temp_dir)
            summary = build_production_run_summary(
                config=ProductionRunConfig(
                    input_dir=root / "input",
                    derivative_output_dir=root / "derivatives",
                    metadata_output_dir=root / "metadata",
                ),
                report=_production_summary_report(total_files=10),
                processing_manifest=_production_processing_manifest(
                    root / "derivatives",
                    total_files=10,
                    processed_files=7,
                    resumed_files=3,
                ),
                admin_report_dir=root / "metadata" / "admin_reports",
                generated_at="2026-01-01T00:00:00+00:00",
                stage_timings={"process": 300.0},
            )

            aggregate = summary["aggregate_processing"]
            self.assertEqual(aggregate["total_images"], 10)
            self.assertEqual(aggregate["processed_images"], 10)
            self.assertEqual(aggregate["remaining_images"], 0)
            self.assertEqual(aggregate["images_per_minute"], 2.0)
            self.assertEqual(aggregate["estimated_remaining_seconds"], 0.0)
            self.assertIsNone(aggregate["unavailable_reason"])
            self.assertEqual(summary["progress"]["aggregate_processing"], aggregate)

    def test_aggregate_processing_estimate_is_unavailable_without_safe_basis(self) -> None:
        cases = [
            (
                {
                    "id": "process",
                    "label": "生成处理后图片",
                    "state": "running",
                    "total_items": 0,
                    "completed_items": 0,
                },
                {"process": 10.0},
                "no_total_images",
            ),
            (
                {
                    "id": "process",
                    "label": "生成处理后图片",
                    "state": "running",
                    "total_items": 5,
                    "completed_items": 0,
                },
                {"process": 10.0},
                "no_processed_images",
            ),
            (
                {
                    "id": "process",
                    "label": "生成处理后图片",
                    "state": "running",
                    "total_items": 5,
                    "completed_items": 2,
                },
                {"process": 0.0},
                "no_elapsed_seconds",
            ),
            (
                {
                    "id": "process",
                    "label": "生成处理后图片",
                    "state": "running",
                    "total_items": 5,
                    "completed_items": None,
                },
                {},
                "missing_processed_images",
            ),
        ]
        with tempfile.TemporaryDirectory(prefix="scan-production-rate-unavailable-") as temp_dir:
            metadata_dir = Path(temp_dir)
            for index, (process_step, stage_timings, expected_reason) in enumerate(cases):
                _write_progress(
                    metadata_dir,
                    "running",
                    [
                        {
                            "id": "scan",
                            "label": "检查扫描图片",
                            "state": "completed",
                            "total_items": None,
                            "completed_items": None,
                        },
                        process_step,
                        {
                            "id": "summarize",
                            "label": "整理处理结果",
                            "state": "pending",
                            "total_items": None,
                            "completed_items": None,
                        },
                    ],
                    current_step="process",
                    stage_timings=stage_timings,
                )

                progress = json.loads((metadata_dir / "production_run_progress.json").read_text(encoding="utf-8"))
                aggregate = progress["aggregate_processing"]
                self.assertIsNone(aggregate["images_per_minute"], index)
                self.assertIsNone(aggregate["estimated_remaining_seconds"], index)
                self.assertEqual(aggregate["unavailable_reason"], expected_reason)

    def test_aggregate_processing_fields_are_public_aggregate_only(self) -> None:
        private_values = [
            "/Users/private/archive/input",
            "Secret_Case_0001.tif",
            "b" * 64,
            "OCR: 张三身份证 110101199001010011",
            "thumbnail",
            "Traceback File worker.py line 42",
            "data:image/png;base64",
        ]
        with tempfile.TemporaryDirectory(prefix="scan-production-rate-private-") as temp_dir:
            root = Path(temp_dir)
            summary = build_production_run_summary(
                config=ProductionRunConfig(
                    input_dir=Path(private_values[0]),
                    derivative_output_dir=root / "derivatives",
                    metadata_output_dir=root / "metadata",
                ),
                report=_production_summary_report(total_files=3, performance={"ocr": private_values[3]}),
                processing_manifest=_production_processing_manifest(
                    root / "derivatives",
                    total_files=3,
                    processed_files=1,
                    failed_files=2,
                    performance={"stack": private_values[5]},
                    files=[
                        {
                            "source_relative_path": private_values[1],
                            "source_sha256": private_values[2],
                            "thumbnail": private_values[6],
                        }
                    ],
                ),
                admin_report_dir=root / "metadata" / "admin_reports",
                generated_at="2026-01-01T00:00:00+00:00",
                stage_timings={"process": 60.0},
            )

            aggregate_text = json.dumps(summary["aggregate_processing"], ensure_ascii=False, sort_keys=True)
            self.assertTrue(summary["aggregate_processing"]["aggregate_only"])
            for private_value in private_values:
                self.assertNotIn(private_value, aggregate_text)
            self.assertNotIn(".tif", aggregate_text)
            self.assertNotIn("sha256", aggregate_text.lower())
            self.assertNotIn("traceback", aggregate_text.lower())
            self.assertNotIn("ocr", aggregate_text.lower())
            self.assertNotIn("thumbnail", aggregate_text.lower())

    def test_recovery_advice_generation_preserves_sources_and_successful_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-recovery-preserve-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "processed"
            metadata_dir = root / "metadata"
            source = input_dir / "source.png"
            derivative = output_dir / "images" / "source.png"
            source.parent.mkdir(parents=True)
            derivative.parent.mkdir(parents=True)
            source.write_bytes(b"original source bytes")
            derivative.write_bytes(b"successful derivative bytes")

            build_production_run_summary(
                config=ProductionRunConfig(
                    input_dir=input_dir,
                    derivative_output_dir=output_dir,
                    metadata_output_dir=metadata_dir,
                ),
                report={
                    "summary": {
                        "total_files": 2,
                        "openable_files": 2,
                        "p0_findings": 0,
                        "p1_findings": 0,
                        "p2_findings": 0,
                        "total_findings": 0,
                        "performance": {},
                    }
                },
                processing_manifest={
                    "image_root": str(output_dir / "images"),
                    "summary": {
                        "total_files": 2,
                        "processed_files": 1,
                        "resumed_files": 0,
                        "skipped_files": 0,
                        "failed_files": 1,
                        "retry_list_files": 1,
                        "performance": {},
                    },
                },
                admin_report_dir=metadata_dir / "admin_reports",
                generated_at="2026-01-01T00:00:00+00:00",
            )

            self.assertEqual(source.read_bytes(), b"original source bytes")
            self.assertEqual(derivative.read_bytes(), b"successful derivative bytes")

    def test_multi_worker_processing_manifest_order_and_outputs_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            for name in ["B_0002.png", "A_0001.png", "nested/C_0003.png"]:
                path = input_dir / name
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (40, 30), "white").save(path)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=4))
            first = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=4))
            second = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=4))

            self.assertEqual([record["source_relative_path"] for record in first["files"]], [item["relative_path"] for item in report["files"]])
            self.assertEqual(first["files"], second["files"])
            self.assertEqual(first["summary"]["performance"]["mode"], "parallel")
            for record in first["files"]:
                self.assertEqual(record["status"], "processed")
                self.assertTrue((process_dir / record["output_relative_path"]).exists())

    def test_processing_reuses_duplicate_source_derivative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            nested_dir = input_dir / "nested"
            nested_dir.mkdir(parents=True)

            source = input_dir / "A001_0001.png"
            Image.new("RGB", (40, 30), "white").save(source, dpi=(300, 300))
            shutil.copyfile(source, nested_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=2))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=2))
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            first = records["A001_0001.png"]
            duplicate = records["nested/A001_0002.png"]
            self.assertEqual(first["status"], "processed")
            self.assertEqual(duplicate["status"], "processed")
            self.assertIn("reuse_duplicate_derivative", duplicate["operations"])
            self.assertEqual(first["output_sha256"], duplicate["output_sha256"])
            self.assertEqual(manifest["summary"]["performance"]["operation_timings"]["auto_crop"]["file_count"], 1)
            self.assertEqual(manifest["summary"]["duplicate_reused_files"], 1)
            self.assertTrue((process_dir / duplicate["output_relative_path"]).exists())

    def test_processing_skips_current_duplicate_derivative_copy_on_repeat_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            nested_dir = input_dir / "nested"
            nested_dir.mkdir(parents=True)

            source = input_dir / "A001_0001.png"
            Image.new("RGB", (40, 30), "white").save(source, dpi=(300, 300))
            shutil.copyfile(source, nested_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=1))
            process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=1))
            with mock.patch("archive_scan_qc.processing.shutil.copyfile", wraps=shutil.copyfile) as copyfile:
                repeated = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=1))

            records = {record["source_relative_path"]: record for record in repeated["files"]}
            duplicate = records["nested/A001_0002.png"]
            self.assertEqual(copyfile.call_count, 0)
            self.assertIn("reuse_duplicate_derivative", duplicate["operations"])
            self.assertNotIn("_existing_derivative_reused", duplicate)
            self.assertEqual(repeated["summary"]["duplicate_reused_files"], 1)
            self.assertEqual(repeated["summary"]["existing_derivative_reused_files"], 1)

    def test_processing_refreshes_stale_duplicate_derivative_on_repeat_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            nested_dir = input_dir / "nested"
            nested_dir.mkdir(parents=True)

            source = input_dir / "A001_0001.png"
            Image.new("RGB", (40, 30), "white").save(source, dpi=(300, 300))
            shutil.copyfile(source, nested_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=1))
            first = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=1))
            duplicate = {
                record["source_relative_path"]: record for record in first["files"]
            }["nested/A001_0002.png"]
            Image.new("RGB", (40, 30), "black").save(process_dir / duplicate["output_relative_path"])

            with mock.patch("archive_scan_qc.processing.shutil.copyfile", wraps=shutil.copyfile) as copyfile:
                repeated = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=1))

            records = {record["source_relative_path"]: record for record in repeated["files"]}
            self.assertEqual(copyfile.call_count, 1)
            self.assertEqual(records["nested/A001_0002.png"]["status"], "processed")
            self.assertEqual(records["nested/A001_0002.png"]["output_sha256"], records["A001_0001.png"]["output_sha256"])
            self.assertEqual(repeated["summary"]["duplicate_reused_files"], 1)
            self.assertEqual(repeated["summary"]["existing_derivative_reused_files"], 0)

    def test_resume_processing_keeps_duplicate_derivative_reuse_without_reprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            nested_dir = input_dir / "nested"
            nested_dir.mkdir(parents=True)

            source = input_dir / "A001_0001.png"
            Image.new("RGB", (40, 30), "white").save(source, dpi=(300, 300))
            shutil.copyfile(source, nested_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=1))
            process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=1))
            with mock.patch("archive_scan_qc.processing.shutil.copyfile", wraps=shutil.copyfile) as copyfile:
                resumed = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(auto_crop=True, resume_processing=True, workers=1),
                )

            records = {record["source_relative_path"]: record for record in resumed["files"]}
            self.assertEqual(copyfile.call_count, 0)
            self.assertEqual({record["status"] for record in resumed["files"]}, {"resumed"})
            self.assertTrue(records["nested/A001_0002.png"]["duplicate_derivative_reused"])
            self.assertIn("resume_skip_existing_derivative", records["nested/A001_0002.png"]["operations"])
            self.assertEqual(resumed["summary"]["processed_files"], 0)
            self.assertEqual(resumed["summary"]["resumed_files"], 2)
            self.assertEqual(resumed["summary"]["duplicate_reused_files"], 1)
            self.assertEqual(resumed["summary"]["existing_derivative_reused_files"], 2)
            audit = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["reuse_decisions"]["resume_skipped_existing_derivatives"]["count"], 2)
            self.assertEqual(audit["reuse_decisions"]["duplicate_derivative_reused"]["count"], 1)
            self.assertEqual(audit["reuse_decisions"]["existing_derivative_write_skipped"]["count"], 2)

    def test_multi_worker_processing_failure_does_not_stop_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            good = input_dir / "A001_0001.png"
            broken_after_scan = input_dir / "A001_0002.png"
            Image.new("RGB", (40, 30), "white").save(good)
            Image.new("RGB", (40, 30), "white").save(broken_after_scan)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=2))
            broken_after_scan.write_text("not an image anymore", encoding="utf-8")
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(workers=2))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertEqual(records["A001_0001.png"]["status"], "processed")
            self.assertEqual(records["A001_0002.png"]["status"], "failed")
            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertEqual(manifest["summary"]["failed_files"], 1)

    def test_deskew_corrects_synthetic_light_skew(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = _synthetic_text_page().rotate(-3.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertAlmostEqual(record["skew_angle_degrees"], -3.0, delta=0.75)
            self.assertGreaterEqual(record["skew_confidence"], 0.08)
            self.assertEqual(record["deskew_reason"], "deskew applied")
            self.assertNotEqual(record["pre_deskew_size"], record["post_deskew_size"])
            self.assertIn("deskew_conservative", record["operations"])

    def test_deskew_corrects_sparse_stable_edge_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_edges.png"
            image = _synthetic_sparse_edge_evidence_page().rotate(
                -2.0,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor="white",
            )
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertAlmostEqual(record["skew_angle_degrees"], -2.0, delta=0.5)
            self.assertGreaterEqual(record["skew_confidence"], 0.08)
            self.assertEqual(record["deskew_reason"], "deskew applied")
            self.assertNotEqual(record["pre_deskew_size"], record["post_deskew_size"])
            self.assertIn("deskew_conservative", record["operations"])
            self.assertEqual(audit_summary["counts"]["deskewed_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["deskew"]["corrected_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["deskew"]["reason_distribution"]["deskew applied"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("A001_edges", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_deskew_corrects_shallow_stable_text_on_light_paper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _synthetic_shallow_stable_text_page().rotate(
                -0.45,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(246, 246, 246),
            )
            image.save(input_dir / "A001_shallow_text.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertAlmostEqual(record["skew_angle_degrees"], -0.45, delta=0.25)
            self.assertGreaterEqual(record["skew_confidence"], 0.08)
            self.assertEqual(record["deskew_reason"], "deskew applied")
            self.assertEqual(audit_summary["counts"]["deskewed_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["deskew"]["reason_distribution"]["deskew applied"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("A001_shallow_text", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_deskew_corrects_faint_segmented_text_on_light_paper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _synthetic_faint_segmented_text_page().rotate(
                -0.8,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(248, 248, 244),
            )
            source = input_dir / "A001_faint_segmented_text.png"
            image.save(source, dpi=(300, 300))
            source_sha_before = _sha256_for_test(source)
            alignment_before = _faint_text_horizontal_alignment_score(image)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            disabled_manifest = process_images(report, input_dir, root / "disabled", ProcessingOptions(deskew=False))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            disabled_record = disabled_manifest["files"][0]
            self.assertFalse(disabled_record["deskewed"])
            self.assertEqual(disabled_record["deskew_reason"], "deskew disabled")

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertAlmostEqual(record["skew_angle_degrees"], -0.8, delta=0.25)
            self.assertGreaterEqual(record["skew_confidence"], 0.08)
            self.assertLessEqual(abs(record["skew_angle_degrees"]), 1.25)
            self.assertEqual(record["deskew_reason"], "deskew applied")
            self.assertEqual(source_sha_before, _sha256_for_test(source))
            self.assertIn("deskew_conservative", record["operations"])
            with Image.open(process_dir / "images" / "A001_faint_segmented_text.png") as processed:
                alignment_after = _faint_text_horizontal_alignment_score(processed)
            self.assertGreater(alignment_after, alignment_before * 1.5)
            self.assertEqual(audit_summary["counts"]["deskewed_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["deskew"]["reason_distribution"]["deskew applied"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("A001_faint_segmented_text", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_deskew_does_not_rotate_blank_or_low_contrast_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (120, 90), "white").save(input_dir / "A001_0001.png")
            low_contrast = Image.new("RGB", (160, 120), (245, 245, 245))
            draw = ImageDraw.Draw(low_contrast)
            for y in range(30, 90, 12):
                draw.line((35, y, 125, y), fill=(235, 235, 235), width=2)
            low_contrast.save(input_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertFalse(records["A001_0001.png"]["deskewed"])
            self.assertFalse(records["A001_0002.png"]["deskewed"])
            self.assertIn(records["A001_0001.png"]["deskew_reason"], {"blank page", "low contrast"})
            self.assertEqual(records["A001_0002.png"]["deskew_reason"], "low contrast")
            self.assertEqual(audit_summary["counts"]["deskew_skipped_files"], 2)
            self.assertEqual(audit_summary["guardrails"]["deskew"]["skipped_files"], 2)
            self.assertGreaterEqual(audit_summary["guardrails"]["deskew"]["reason_distribution"]["low contrast"], 1)

    def test_deskew_noops_for_shallow_table_and_color_mark_risks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_shallow_table_page().rotate(
                -0.45,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(246, 246, 246),
            ).save(input_dir / "A001_table.png", dpi=(300, 300))
            _synthetic_shallow_stable_text_page(red_mark=True).rotate(
                -0.45,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(246, 246, 246),
            ).save(input_dir / "A001_red_mark.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True, workers=1))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            for name in ("A001_table.png", "A001_red_mark.png"):
                self.assertEqual(records[name]["status"], "processed")
                self.assertFalse(records[name]["deskewed"])
                self.assertEqual(records[name]["deskew_reason"], "table or color mark rotation risk")
                self.assertIn("deskew_noop", records[name]["operations"])
            self.assertEqual(audit_summary["counts"]["deskew_skipped_files"], 2)
            self.assertEqual(
                audit_summary["guardrails"]["deskew"]["reason_distribution"]["table or color mark rotation risk"],
                2,
            )

    def test_deskew_noops_for_sparse_and_inconsistent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            sparse = Image.new("RGB", (240, 180), "white")
            draw = ImageDraw.Draw(sparse)
            for point in [(20, 20), (90, 40), (130, 120), (200, 150)]:
                draw.point(point, fill=(10, 10, 10))
            sparse.save(input_dir / "A001_sparse.png")
            _synthetic_inconsistent_skew_page().save(input_dir / "A001_inconsistent.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertFalse(records["A001_sparse.png"]["deskewed"])
            self.assertIn(records["A001_sparse.png"]["deskew_reason"], {"low contrast", "insufficient foreground"})
            self.assertFalse(records["A001_inconsistent.png"]["deskewed"])
            self.assertEqual(records["A001_inconsistent.png"]["deskew_reason"], "low confidence")
            self.assertIn("deskew_noop", records["A001_inconsistent.png"]["operations"])

    def test_deskew_noops_for_faint_ambiguous_multi_angle_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_faint_ambiguous_text_page().save(input_dir / "A001_faint_ambiguous.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True, workers=1))

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["deskewed"])
            self.assertEqual(record["deskew_reason"], "low contrast")
            self.assertIn("deskew_noop", record["operations"])

    def test_deskew_noops_when_edge_content_would_expand_crop_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _synthetic_edge_content_skew_page().save(input_dir / "A001_edge_content.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["deskewed"])
            self.assertAlmostEqual(record["skew_angle_degrees"], -2.5, delta=0.75)
            self.assertGreaterEqual(record["skew_confidence"], 0.08)
            self.assertEqual(record["deskew_reason"], "edge content near rotation boundary")
            self.assertEqual(record["pre_deskew_size"], record["post_deskew_size"])
            self.assertEqual(record["output_size"], record["pre_deskew_size"])
            self.assertIn("deskew_noop", record["operations"])
            self.assertEqual(audit_summary["counts"]["deskew_skipped_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["deskew"]["reason_distribution"]["edge content near rotation boundary"],
                1,
            )
            self.assertNotIn(record["source_sha256"], json.dumps(audit_summary, ensure_ascii=False))

    def test_deskew_does_not_rotate_large_angle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = _synthetic_text_page().rotate(-8.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True))

            record = manifest["files"][0]
            self.assertFalse(record["deskewed"])
            self.assertGreater(abs(record["skew_angle_degrees"]), 5.0)
            self.assertEqual(record["deskew_reason"], "angle exceeds conservative threshold")
            self.assertIn("deskew_noop", record["operations"])

    def test_deskew_projection_variance_uses_row_ink_counts(self) -> None:
        image = Image.new("L", (12, 8), 0)
        draw = ImageDraw.Draw(image)
        draw.line((1, 1, 10, 1), fill=255, width=1)
        draw.line((2, 4, 8, 4), fill=255, width=1)
        draw.point((5, 6), fill=255)

        row_counts = [10, 0, 0, 0, 7, 0, 1, 0]
        mean = sum(row_counts) / len(row_counts)
        expected = sum((count - mean) ** 2 for count in row_counts) / len(row_counts)

        self.assertAlmostEqual(_horizontal_projection_variance(image), expected, delta=0.03)

    def test_deskew_candidate_scores_fast_path_near_zero_skew(self) -> None:
        sample = _synthetic_ink_text_page()

        scores = _deskew_candidate_scores(sample)

        self.assertEqual(set(scores), {-1.0, -0.5, 0.0, 0.5, 1.0})
        self.assertEqual(max(scores, key=scores.get), 0.0)

    def test_deskew_candidate_scores_refines_skewed_page(self) -> None:
        sample = _synthetic_ink_text_page().rotate(-3.0, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=0)

        scores = _deskew_candidate_scores(sample)

        best_angle = max(scores, key=scores.get)
        self.assertGreater(len(scores), 5)
        self.assertAlmostEqual(best_angle, 3.0, delta=0.5)

    def test_auto_crop_trims_white_margin_around_black_page_border(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 8, 69, 51), outline="black", width=3)
            image.save(source)
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                processed_size = processed.size
            record = manifest["files"][0]
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_bbox"], [10, 8, 70, 52])
            self.assertEqual(record["crop_reason"], "conservative crop applied")
            self.assertEqual(record["original_size"], [80, 60])
            self.assertEqual(record["output_size"], [60, 44])
            self.assertGreater(record["processing_audit"]["crop_ratio"], 0.0)
            self.assertEqual(record["processing_warnings"], [])
            self.assertEqual(processed_size, (60, 44))
            self.assertIn("auto_crop_conservative", record["operations"])

    def test_auto_crop_trims_light_outer_margin_with_consistent_page_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (120, 90), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((12, 10, 107, 79), fill=(225, 225, 225), outline=(80, 80, 80), width=2)
            image.save(input_dir / "A001_0001.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True))

            record = manifest["files"][0]
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_bbox"], [12, 10, 108, 80])
            self.assertEqual(record["crop_reason"], "conservative crop applied")
            self.assertEqual(record["output_size"], [96, 70])

    def test_auto_crop_trims_subtle_light_scanner_edge_when_subject_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (140, 105), (250, 250, 250))
            draw = ImageDraw.Draw(image)
            draw.rectangle((14, 12, 125, 93), fill=(242, 242, 242))
            draw.rectangle((44, 42, 100, 46), fill=(30, 30, 30))
            draw.rectangle((48, 58, 92, 62), fill=(35, 35, 35))
            image.save(input_dir / "A001_0001.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_bbox"], [14, 12, 126, 94])
            self.assertEqual(record["output_size"], [112, 82])
            self.assertLess(record["processing_audit"]["crop_ratio"], 0.40)
            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                self.assertLess(_box_luma(processed, (30, 30, 88, 54)), 245)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["cropped_side_distribution"],
                {"left": 1, "top": 1, "right": 1, "bottom": 1},
            )
            self.assertGreater(audit_summary["guardrails"]["auto_crop"]["crop_ratio"]["max"], 0.0)

    def test_auto_crop_trims_post_deskew_canvas_without_content_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (240, 180), (248, 248, 248))
            draw = ImageDraw.Draw(image)
            draw.rectangle((24, 20, 215, 159), fill=(238, 238, 238))
            for y in range(48, 128, 18):
                draw.rectangle((58, y, 182, y + 4), fill=(25, 25, 25))
            image.rotate(
                -2.5,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(252, 252, 252),
            ).save(input_dir / "private_post_deskew_canvas.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(auto_crop=True, deskew=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew safe canvas crop applied")
            self.assertEqual(record["output_size"], [240, 180])
            self.assertLess(record["processing_audit"]["crop_ratio"], 0.12)
            with Image.open(process_dir / "images" / "private_post_deskew_canvas.png") as processed:
                self.assertLess(_box_luma(processed, (58, 48, 182, 132)), 225)
                self.assertGreater(_box_luma(processed, (0, 0, 16, 16)), 240)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["post_deskew_safe_crop_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["reason_distribution"],
                {"post-deskew safe canvas crop applied": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_post_deskew_canvas", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_auto_crop_trims_safe_post_deskew_corner_wedges_with_bounded_margins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_post_deskew_corner_wedge.png"
            image = Image.new("RGB", (320, 240), (248, 248, 248))
            draw = ImageDraw.Draw(image)
            draw.rectangle((58, 50, 262, 184), outline=(160, 160, 160), width=1)
            for y in (70, 92, 114, 136, 158):
                draw.rectangle((72, y, 248, y + 3), fill=(35, 35, 35))
            image.rotate(
                1.0,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(245, 245, 245),
            ).save(source)
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(auto_crop=True, deskew=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew safe canvas crop applied")
            self.assertEqual(record["post_deskew_size"], [332, 252])
            self.assertEqual(record["output_size"], [318, 238])
            self.assertLessEqual(record["processing_audit"]["crop_ratio"], 0.12)
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            with Image.open(process_dir / "images" / source.name) as processed:
                self.assertLess(_box_luma(processed, (66, 64, 242, 158)), 235)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["post_deskew_safe_crop_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["reason_distribution"],
                {"post-deskew safe canvas crop applied": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_post_deskew_corner_wedge", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_auto_crop_skips_post_deskew_canvas_when_edge_content_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (240, 180), (248, 248, 248))
            draw = ImageDraw.Draw(image)
            for y in range(48, 128, 18):
                draw.rectangle((58, y, 182, y + 4), fill=(25, 25, 25))
            draw.rectangle((104, 164, 126, 172), fill=(20, 20, 20))
            image.rotate(
                -2.5,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(252, 252, 252),
            ).save(input_dir / "private_post_deskew_page_number.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(auto_crop=True, deskew=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertFalse(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew crop skipped: edge content protection")
            self.assertEqual(record["output_size"], record["post_deskew_size"])
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["edge_content_protection_skip_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["protection_triggered_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["skip_reason_distribution"],
                {"post-deskew crop skipped: edge content protection": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_post_deskew_page_number", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_auto_crop_preserves_post_deskew_corner_wedge_edge_marks_and_color_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            cases = {
                "private_wedge_page_number.png": lambda draw: draw.text((2, 118), "12", fill=(30, 30, 30)),
                "private_wedge_handwriting.png": lambda draw: draw.line(
                    (2, 140, 18, 150),
                    fill=(30, 30, 30),
                    width=2,
                ),
                "private_wedge_color_chart.png": lambda draw: draw.rectangle(
                    (268, 38, 306, 96),
                    fill=(170, 40, 40),
                ),
            }
            for filename, mark in cases.items():
                image = Image.new("RGB", (320, 240), (248, 248, 248))
                draw = ImageDraw.Draw(image)
                draw.rectangle((58, 50, 262, 184), outline=(160, 160, 160), width=1)
                for y in (70, 92, 114, 136, 158):
                    draw.rectangle((72, y, 248, y + 3), fill=(35, 35, 35))
                mark(draw)
                image.rotate(
                    1.0,
                    resample=Image.Resampling.BICUBIC,
                    expand=True,
                    fillcolor=(245, 245, 245),
                ).save(input_dir / filename)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(auto_crop=True, deskew=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], len(cases))
            for record in manifest["files"]:
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["cropped"], record["source_relative_path"])
                self.assertIn("auto_crop_noop", record["operations"])
                self.assertIn(
                    record["crop_reason"],
                    {
                        "inconsistent crop margin evidence",
                        "post-deskew crop skipped: edge content protection",
                    },
                )
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in (*cases, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_retouch_chain_stops_post_deskew_crop_when_edge_content_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (240, 180), (248, 248, 248))
            draw = ImageDraw.Draw(image)
            for y in range(48, 128, 18):
                draw.rectangle((58, y, 182, y + 4), fill=(25, 25, 25))
            draw.rectangle((104, 164, 126, 172), fill=(20, 20, 20))
            image.rotate(
                -2.5,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(252, 252, 252),
            ).save(input_dir / "private_full_chain_edge_protection.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    despeckle=True,
                    normalize_tones=True,
                    lighten_edge_shadow=True,
                    lighten_background_stains=True,
                    level_illumination_gradient=True,
                    lighten_scanlines=True,
                    enhance_faded_text=True,
                    sharpen_text_edges=True,
                    workers=1,
                ),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertFalse(record["dark_border_trimmed"])
            self.assertFalse(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew crop skipped: edge content protection")
            self.assertEqual(record["output_size"], record["post_deskew_size"])
            operation_order = [
                operation
                for operation in record["operations"]
                if operation
                in {
                    "deskew_conservative",
                    "dark_border_trim_noop",
                    "auto_crop_noop",
                    "despeckle_noop",
                    "normalize_tones_noop",
                    "lighten_edge_shadow_noop",
                    "lighten_background_stains_noop",
                    "lighten_scanlines_noop",
                    "enhance_faded_text_noop",
                    "sharpen_text_edges_noop",
                }
            ]
            self.assertEqual(
                operation_order,
                [
                    "deskew_conservative",
                    "dark_border_trim_noop",
                    "auto_crop_noop",
                    "despeckle_noop",
                    "normalize_tones_noop",
                    "lighten_edge_shadow_noop",
                    "lighten_background_stains_noop",
                    "lighten_scanlines_noop",
                    "enhance_faded_text_noop",
                    "sharpen_text_edges_noop",
                ],
            )
            self.assertEqual(record["processing_audit"]["cumulative_change_guard_action"], "passed")
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["edge_content_protection_skip_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["protection_triggered_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["skip_reason_distribution"],
                {"post-deskew crop skipped: edge content protection": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_full_chain_edge_protection", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_auto_crop_keeps_edge_content_near_any_side(self) -> None:
        cases = {
            "body_left": lambda draw: draw.rectangle((1, 35, 25, 40), fill=(20, 20, 20)),
            "page_number_bottom": lambda draw: draw.rectangle((55, 86, 65, 89), fill=(20, 20, 20)),
            "stamp_right": lambda draw: draw.ellipse((105, 30, 119, 44), outline=(140, 0, 0), width=2),
            "margin_note_top": lambda draw: draw.rectangle((45, 1, 75, 5), fill=(20, 20, 20)),
            "binding_line": lambda draw: draw.line((2, 0, 2, 89), fill=(40, 40, 40), width=2),
            "dark_archival_edge": lambda draw: draw.rectangle((0, 0, 5, 89), fill=(60, 60, 60)),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            for name, mark in cases.items():
                image = Image.new("RGB", (120, 90), "white")
                draw = ImageDraw.Draw(image)
                draw.rectangle((18, 15, 101, 74), outline=(25, 25, 25), width=2)
                mark(draw)
                image.save(input_dir / f"A001_{name}.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            for name in cases:
                record = records[f"A001_{name}.png"]
                self.assertFalse(record["cropped"], name)
                self.assertIsNone(record["crop_bbox"], name)
                self.assertEqual(record["output_size"], [120, 90], name)
                self.assertEqual(record["crop_reason"], "foreground reaches crop safety margin", name)
                self.assertIn("auto_crop_noop", record["operations"], name)
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], len(cases))
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["protection_triggered_files"], len(cases))
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["skip_reason_distribution"],
                {"foreground reaches crop safety margin": len(cases)},
            )

    def test_auto_crop_noops_for_unsafe_synthetic_candidates_with_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            Image.new("RGB", (120, 90), "white").save(input_dir / "A001_blank.png")

            low_contrast = Image.new("RGB", (120, 90), (245, 245, 245))
            draw = ImageDraw.Draw(low_contrast)
            draw.rectangle((12, 10, 107, 79), outline=(235, 235, 235), width=2)
            low_contrast.save(input_dir / "A001_low_contrast.png")

            near_full = Image.new("RGB", (120, 90), "white")
            draw = ImageDraw.Draw(near_full)
            draw.rectangle((1, 1, 118, 88), outline=(20, 20, 20), width=2)
            near_full.save(input_dir / "A001_near_full.png")

            excessive = Image.new("RGB", (120, 90), "white")
            draw = ImageDraw.Draw(excessive)
            draw.rectangle((45, 35, 55, 45), outline=(20, 20, 20), width=2)
            excessive.save(input_dir / "A001_excessive.png")

            inconsistent = Image.new("RGB", (120, 90), "white")
            draw = ImageDraw.Draw(inconsistent)
            draw.rectangle((4, 20, 115, 69), outline=(20, 20, 20), width=2)
            inconsistent.save(input_dir / "A001_inconsistent.png")

            local_noise = Image.new("RGB", (120, 90), "white")
            draw = ImageDraw.Draw(local_noise)
            draw.rectangle((18, 15, 101, 74), outline=(25, 25, 25), width=2)
            for point in [(8, 8), (112, 81), (60, 2)]:
                draw.point(point, fill=(0, 0, 0))
            local_noise.save(input_dir / "A001_local_noise.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            expected_reasons = {
                "A001_blank.png": "no confident foreground outside background",
                "A001_low_contrast.png": "no confident foreground outside background",
                "A001_near_full.png": "foreground reaches crop safety margin",
                "A001_excessive.png": "candidate crop exceeds conservative crop ratio",
                "A001_inconsistent.png": "inconsistent crop margin evidence",
                "A001_local_noise.png": "crop boundary evidence is too sparse",
            }
            for name, reason in expected_reasons.items():
                record = records[name]
                self.assertFalse(record["cropped"], name)
                self.assertIsNone(record["crop_bbox"], name)
                self.assertEqual(record["crop_reason"], reason, name)
                self.assertEqual(record["output_size"], [120, 90], name)
                self.assertIn("auto_crop_noop", record["operations"], name)
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            auto_crop = audit_summary["guardrails"]["auto_crop"]
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], len(expected_reasons))
            self.assertEqual(audit_summary["counts"]["auto_crop_low_confidence_skip_files"], 2)
            self.assertEqual(auto_crop["applied_files"], 0)
            self.assertEqual(auto_crop["skipped_files"], len(expected_reasons))
            self.assertEqual(auto_crop["low_confidence_skip_files"], 2)
            self.assertEqual(
                auto_crop["skip_reason_distribution"],
                {
                    "no confident foreground outside background": 2,
                    "foreground reaches crop safety margin": 1,
                    "candidate crop exceeds conservative crop ratio": 1,
                    "inconsistent crop margin evidence": 1,
                    "crop boundary evidence is too sparse": 1,
                },
            )

    def test_processing_audit_allows_small_synthetic_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (80, 60), "white")
            for point in [(10, 10), (20, 20), (30, 30)]:
                image.putpixel(point, (0, 0, 0))
            image.save(input_dir / "A001_0001.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertEqual(record["processing_warnings"], [])
            self.assertGreater(record["processing_audit"]["pixel_change_ratio"], 0.0)
            self.assertEqual(record["processing_audit"]["despeckle_pixel_ratio"], 0.000625)
            self.assertEqual(audit_summary["counts"]["processing_warning_files"], 0)
            self.assertEqual(audit_summary["metrics"]["despeckle_pixel_ratio"]["count"], 1)
            self.assertTrue(audit_summary["timing"]["operation_timings"]["despeckle"]["enabled"])
            self.assertEqual(audit_summary["timing"]["operation_timings"]["despeckle"]["file_count"], 1)
            self.assertGreaterEqual(audit_summary["timing"]["operation_timings"]["despeckle"]["elapsed_seconds"], 0.0)
            self.assertEqual(audit_summary["timing"]["operation_timings"]["despeckle"]["backend_mode"], "fallback")
            self.assertEqual(sum(audit_summary["timing"]["operation_timings"]["despeckle"]["backend_counts"].values()), 1)

    def test_numpy_despeckle_replacement_matches_fallback_for_conservative_cases(self) -> None:
        if processing_module._load_numpy() is None:
            self.skipTest("NumPy is not available")

        cases: dict[str, Image.Image] = {}
        isolated = Image.new("RGB", (100, 80), "white")
        for point in [(25, 20), (55, 34), (70, 58)]:
            isolated.putpixel(point, (0, 0, 0))
        cases["isolated speckles"] = isolated

        colored = Image.new("RGB", (100, 80), "white")
        colored.putpixel((30, 30), (190, 20, 20))
        colored.putpixel((65, 45), (0, 0, 0))
        cases["colored mark protected"] = colored

        edge = Image.new("RGB", (100, 80), "white")
        edge_draw = ImageDraw.Draw(edge)
        edge_draw.line((2, 8, 2, 70), fill=(0, 0, 0), width=1)
        edge.putpixel((50, 40), (0, 0, 0))
        cases["edge mark protected"] = edge

        text_context = Image.new("RGB", (100, 80), "white")
        for point in [(45, 40), (37, 40), (53, 40), (45, 32), (45, 48), (38, 35), (52, 45)]:
            text_context.putpixel(point, (0, 0, 0))
        cases["nearby content protected"] = text_context

        dense_noise = Image.new("RGB", (100, 80), "white")
        for y in range(20, 60, 2):
            for x in range(20, 80, 3):
                dense_noise.putpixel((x, y), (0, 0, 0))
        cases["dense noise skipped"] = dense_noise

        for name, image in cases.items():
            with self.subTest(name=name):
                fallback_image, fallback_changed, fallback_backend = _despeckle_isolated_pixels(image, backend="fallback")
                with mock.patch(
                    "archive_scan_qc.processing._despeckle_replacements_fallback",
                    side_effect=AssertionError("NumPy replacement path should not use fallback filtering"),
                ):
                    numpy_image, numpy_changed, numpy_backend = _despeckle_isolated_pixels(image, backend="numpy")

                self.assertEqual(numpy_backend, "numpy")
                self.assertEqual(numpy_changed, fallback_changed)
                self.assertEqual(numpy_image.tobytes(), fallback_image.tobytes())

    def test_processing_audit_reports_numpy_despeckle_backend_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (20, 20), "white")
            image.putpixel((10, 10), (0, 0, 0))
            image.save(input_dir / "synthetic.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with (
                mock.patch("archive_scan_qc.processing._despeckle_candidate_points_numpy", return_value=[(10, 10)]),
                mock.patch("archive_scan_qc.processing._despeckle_replacements_numpy", return_value=[(10, 10, (255, 255, 255))]),
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(despeckle=True, despeckle_backend="numpy", workers=1),
                )
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            despeckle_timing = audit_summary["timing"]["operation_timings"]["despeckle"]

            self.assertEqual(manifest["files"][0]["despeckle_backend_mode"], "numpy")
            self.assertEqual(despeckle_timing["backend_mode"], "numpy")
            self.assertTrue(despeckle_timing["numpy_available"])
            self.assertEqual(despeckle_timing["backend_counts"]["numpy"], 1)
            self.assertEqual(despeckle_timing["backend_counts"]["fallback"], 0)

    def test_processing_audit_reports_fallback_despeckle_backend_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (20, 20), "white")
            image.putpixel((10, 10), (0, 0, 0))
            image.save(input_dir / "synthetic.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch("archive_scan_qc.processing._load_numpy", return_value=None):
                manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True, workers=1))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            despeckle_timing = audit_summary["timing"]["operation_timings"]["despeckle"]

            self.assertEqual(manifest["files"][0]["despeckle_backend_mode"], "fallback")
            self.assertEqual(despeckle_timing["backend_mode"], "fallback")
            self.assertFalse(despeckle_timing["numpy_available"])
            self.assertEqual(despeckle_timing["backend_counts"]["numpy"], 0)
            self.assertEqual(despeckle_timing["backend_counts"]["fallback"], 1)

    def test_requested_numpy_despeckle_falls_back_when_numpy_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (20, 20), "white")
            image.putpixel((10, 10), (0, 0, 0))
            image.save(input_dir / "synthetic.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch("archive_scan_qc.processing._load_numpy", return_value=None):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(despeckle=True, despeckle_backend="numpy", workers=1),
                )
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            despeckle_timing = audit_summary["timing"]["operation_timings"]["despeckle"]

            self.assertEqual(manifest["files"][0]["despeckle_backend_mode"], "fallback")
            self.assertEqual(despeckle_timing["backend_mode"], "fallback")
            self.assertFalse(despeckle_timing["numpy_available"])
            self.assertEqual(despeckle_timing["backend_counts"]["numpy"], 0)
            self.assertEqual(despeckle_timing["backend_counts"]["fallback"], 1)

    def test_processing_guardrail_fails_overprocessed_derivative_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 8, 69, 51), outline="black", width=3)
            image.save(source)
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(auto_crop=True, audit_max_crop_ratio=0.10),
            )
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "failed")
            self.assertIn("processing guardrail exceeded", record["failure_reason"])
            self.assertIn("crop_ratio", record["processing_warnings"])
            self.assertFalse((process_dir / "images" / "A001_0001.png").exists())
            self.assertEqual(audit_summary["counts"]["guardrail_failed_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["failure_reasons"]["crop_ratio"], 1)

    def test_auto_crop_does_not_overcrop_blank_or_low_contrast_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "A001_0001.png")
            low_contrast = Image.new("RGB", (80, 60), (245, 245, 245))
            draw = ImageDraw.Draw(low_contrast)
            draw.rectangle((10, 8, 69, 51), outline=(235, 235, 235), width=3)
            low_contrast.save(input_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertFalse(records["A001_0001.png"]["cropped"])
            self.assertFalse(records["A001_0002.png"]["cropped"])
            self.assertEqual(records["A001_0001.png"]["output_size"], [80, 60])
            self.assertEqual(records["A001_0002.png"]["output_size"], [80, 60])
            self.assertEqual(records["A001_0001.png"]["crop_reason"], "no confident foreground outside background")
            self.assertEqual(records["A001_0002.png"]["crop_reason"], "no confident foreground outside background")
            self.assertIn("auto_crop_noop", records["A001_0001.png"]["operations"])
            self.assertIn("auto_crop_noop", records["A001_0002.png"]["operations"])

    def test_disabled_new_retouch_options_keep_derivative_size_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "A001_0001.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions())

            record = manifest["files"][0]
            self.assertEqual(record["output_size"], [80, 60])
            self.assertFalse(record["dark_border_trimmed"])
            self.assertIsNone(record["dark_border_bbox"])
            self.assertEqual(record["dark_border_reason"], "dark border trim disabled")
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "despeckle disabled")
            self.assertFalse(record["fold_shadows_lightened"])
            self.assertEqual(record["fold_shadows_reason"], "fold shadow cleanup disabled")
            self.assertIn("dark_border_trim_disabled", record["operations"])
            self.assertIn("despeckle_disabled", record["operations"])
            self.assertIn("lighten_fold_shadows_disabled", record["operations"])
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 0)
            self.assertEqual(audit_summary["counts"]["dark_border_skipped_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["applied_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["skipped_files"], 0)

    def test_trim_dark_border_trims_edge_border_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (100, 80), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 99, 79), outline="black", width=5)
            draw.rectangle((25, 24, 75, 28), fill=(20, 20, 20))
            image.save(source)
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                processed_size = processed.size
            record = manifest["files"][0]
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(record["dark_border_trimmed"])
            self.assertEqual(record["dark_border_bbox"], [5, 5, 95, 75])
            self.assertEqual(record["output_size"], [90, 70])
            self.assertEqual(processed_size, (90, 70))
            self.assertEqual(record["dark_border_reason"], "dark edge border trimmed")
            self.assertIn("dark_border_trim_conservative", record["operations"])

    def test_trim_dark_border_trims_gray_continuous_scan_edge_and_audits_aggregate_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (100, 80), (242, 242, 238))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 99, 79), outline=(55, 55, 55), width=4)
            draw.rectangle((25, 24, 75, 28), fill=(20, 20, 20))
            image.save(input_dir / "A001_gray_edge.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            self.assertTrue(record["dark_border_trimmed"])
            self.assertEqual(record["dark_border_bbox"], [4, 4, 96, 76])
            self.assertEqual(record["output_size"], [92, 72])
            self.assertAlmostEqual(record["processing_audit"]["max_trim_margin_ratio"], 0.05)
            self.assertEqual(record["dark_border_reason"], "dark edge border trimmed")
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["dark_border_trim"]["trimmed_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["dark_border_trim"]["reason_distribution"]["dark edge border trimmed"],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("A001_gray_edge", json.dumps(audit_summary, ensure_ascii=False))

    def test_trim_dark_border_trims_narrow_continuous_deep_gray_scan_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (120, 90), (242, 242, 238))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 119, 89), outline=(92, 92, 92), width=3)
            draw.rectangle((36, 32, 84, 36), fill=(20, 20, 20))
            image.save(input_dir / "A001_narrow_gray_edge.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            self.assertTrue(record["dark_border_trimmed"])
            self.assertEqual(record["dark_border_bbox"], [3, 3, 117, 87])
            self.assertEqual(record["output_size"], [114, 84])
            self.assertLessEqual(record["processing_audit"]["max_trim_margin_ratio"], 0.034)
            self.assertEqual(record["dark_border_reason"], "dark edge border trimmed")
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["dark_border_trim"]["reason_distribution"],
                {"dark edge border trimmed": 1},
            )
            self.assertNotIn("A001_narrow_gray_edge", json.dumps(audit_summary, ensure_ascii=False))

    def test_trim_dark_border_trims_broken_scan_edge_with_light_glare_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_broken_glare_edge.png"
            image = Image.new("RGB", (120, 90), (244, 244, 240))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 119, 89), outline=(20, 20, 20), width=4)
            draw.rectangle((0, 26, 3, 54), fill=(244, 244, 240))
            draw.rectangle((40, 38, 84, 43), fill=(25, 25, 25))
            image.save(source)
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(record["dark_border_trimmed"])
            self.assertEqual(record["dark_border_bbox"], [4, 4, 116, 86])
            self.assertEqual(record["output_size"], [112, 82])
            self.assertLessEqual(record["processing_audit"]["max_trim_margin_ratio"], 0.045)
            self.assertEqual(record["dark_border_reason"], "broken dark edge border trimmed")
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["dark_border_trim"]["reason_distribution"],
                {"broken dark edge border trimmed": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("A001_broken_glare_edge", json.dumps(audit_summary, ensure_ascii=False))

    def test_trim_dark_border_keeps_edge_content_inside_narrow_deep_gray_scan_edge(self) -> None:
        image = Image.new("RGB", (120, 90), (242, 242, 238))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 119, 89), outline=(92, 92, 92), width=3)
        draw.rectangle((3, 38, 12, 43), fill=(15, 15, 15))

        detection = detect_dark_border_bbox(image)

        self.assertIsNone(detection.bbox)
        self.assertEqual(detection.reason, "protected edge content near dark border")

    def test_trim_dark_border_noops_for_content_adjacent_to_each_edge(self) -> None:
        cases = {
            "left": (5, 34, 13, 39),
            "right": (87, 34, 94, 39),
            "top": (44, 5, 55, 12),
            "bottom": (44, 67, 55, 74),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            for side, source_mark in cases.items():
                image = Image.new("RGB", (100, 80), "white")
                draw = ImageDraw.Draw(image)
                draw.rectangle((0, 0, 99, 79), outline="black", width=5)
                draw.rectangle(source_mark, fill=(15, 15, 15))
                image.save(input_dir / f"A001_{side}.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(trim_dark_border=True, audit_max_contrast_delta=100.0),
            )

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            for side in cases:
                name = f"A001_{side}.png"
                record = records[name]
                self.assertFalse(record["dark_border_trimmed"], side)
                self.assertIsNone(record["dark_border_bbox"], side)
                self.assertEqual(record["output_size"], [100, 80], side)
                self.assertEqual(record["dark_border_reason"], "protected edge content near dark border")
                self.assertIn("dark_border_trim_noop", record["operations"])
            self.assertEqual(audit_summary["counts"]["dark_border_skipped_files"], 4)
            self.assertEqual(
                audit_summary["guardrails"]["dark_border_trim"]["reason_distribution"][
                    "protected edge content near dark border"
                ],
                4,
            )

    def test_trim_dark_border_noops_for_broken_edge_with_near_boundary_page_number(self) -> None:
        image = Image.new("RGB", (120, 90), (244, 244, 240))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 119, 89), outline=(20, 20, 20), width=4)
        draw.rectangle((0, 26, 3, 54), fill=(244, 244, 240))
        draw.rectangle((4, 36, 13, 42), fill=(15, 15, 15))
        draw.rectangle((42, 48, 82, 53), fill=(25, 25, 25))

        detection = detect_dark_border_bbox(image)

        self.assertIsNone(detection.bbox)
        self.assertEqual(detection.reason, "protected edge content near dark border")

    def test_trim_dark_border_noops_for_fragmented_broken_frame_blocks(self) -> None:
        image = Image.new("RGB", (120, 90), (244, 244, 240))
        draw = ImageDraw.Draw(image)
        for box in [
            (0, 0, 3, 18),
            (0, 36, 3, 53),
            (0, 72, 3, 89),
            (116, 0, 119, 18),
            (116, 36, 119, 53),
            (116, 72, 119, 89),
            (0, 0, 24, 3),
            (48, 0, 72, 3),
            (96, 0, 119, 3),
            (0, 86, 24, 89),
            (48, 86, 72, 89),
            (96, 86, 119, 89),
        ]:
            draw.rectangle(box, fill=(20, 20, 20))
        draw.rectangle((42, 40, 82, 45), fill=(25, 25, 25))

        detection = detect_dark_border_bbox(image)

        self.assertIsNone(detection.bbox)
        self.assertEqual(detection.reason, "no confident dark edge border")

    def test_trim_dark_border_noops_for_single_sided_and_unbalanced_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            single_edge = Image.new("RGB", (100, 80), "white")
            draw = ImageDraw.Draw(single_edge)
            draw.rectangle((0, 0, 4, 79), fill="black")
            draw.rectangle((5, 18, 28, 24), fill=(15, 15, 15))
            draw.rectangle((65, 70, 80, 75), fill=(15, 15, 15))
            single_edge.save(input_dir / "A001_single_edge.png")

            unbalanced = Image.new("RGB", (100, 80), "white")
            draw = ImageDraw.Draw(unbalanced)
            draw.rectangle((0, 0, 99, 79), outline="black", width=2)
            draw.rectangle((0, 0, 9, 79), fill="black")
            draw.rectangle((10, 24, 35, 29), fill=(15, 15, 15))
            unbalanced.save(input_dir / "A001_unbalanced.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertEqual(records["A001_single_edge.png"]["dark_border_reason"], "incomplete dark edge border evidence")
            self.assertEqual(records["A001_unbalanced.png"]["dark_border_reason"], "unbalanced dark edge border evidence")
            for record in records.values():
                self.assertFalse(record["dark_border_trimmed"])
                self.assertIsNone(record["dark_border_bbox"])
                self.assertEqual(record["output_size"], [100, 80])
                self.assertIn("dark_border_trim_noop", record["operations"])

    def test_trim_dark_border_noops_for_archival_dark_edge_with_auditable_reason(self) -> None:
        image = Image.new("RGB", (100, 80), (238, 238, 232))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 5, 79), fill=(35, 35, 35))
        draw.rectangle((10, 35, 80, 38), fill=(70, 70, 70))

        detection = detect_dark_border_bbox(image)

        self.assertIsNone(detection.bbox)
        self.assertEqual(detection.reason, "incomplete dark edge border evidence")

    def test_trim_dark_border_noops_for_discontinuous_blocks_and_broad_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            discontinuous = Image.new("RGB", (100, 80), "white")
            draw = ImageDraw.Draw(discontinuous)
            for box in [(0, 0, 4, 18), (0, 30, 4, 48), (0, 60, 4, 79), (95, 0, 99, 18), (95, 60, 99, 79)]:
                draw.rectangle(box, fill=(0, 0, 0))
            discontinuous.save(input_dir / "A001_discontinuous.png")

            broad_shadow = Image.new("RGB", (100, 80), (242, 242, 238))
            draw = ImageDraw.Draw(broad_shadow)
            draw.rectangle((0, 0, 19, 79), fill=(35, 35, 35))
            draw.rectangle((80, 0, 99, 79), fill=(35, 35, 35))
            draw.rectangle((0, 0, 99, 15), fill=(35, 35, 35))
            draw.rectangle((0, 64, 99, 79), fill=(35, 35, 35))
            broad_shadow.save(input_dir / "A001_broad_shadow.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertEqual(
                records["A001_discontinuous.png"]["dark_border_reason"],
                "incomplete dark edge border evidence",
            )
            self.assertEqual(
                records["A001_broad_shadow.png"]["dark_border_reason"],
                "candidate trim exceeds conservative retain ratio",
            )
            for record in records.values():
                self.assertFalse(record["dark_border_trimmed"])
                self.assertIsNone(record["dark_border_bbox"])
                self.assertIn("dark_border_trim_noop", record["operations"])

    def test_trim_dark_border_combines_with_crop_deskew_and_cumulative_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (120, 90), (245, 245, 240))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 119, 89), outline=(8, 8, 8), width=4)
            draw.rectangle((32, 30, 88, 34), fill=(20, 20, 20))
            draw.rectangle((35, 44, 82, 47), fill=(30, 30, 30))
            image.save(input_dir / "private_combined_trim.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(auto_crop=True, deskew=True, trim_dark_border=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]

            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["dark_border_trimmed"])
            self.assertEqual(record["processing_audit"]["cumulative_change_guard_action"], "passed")
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertTrue(audit_summary["operations"]["auto_crop"])
            self.assertTrue(audit_summary["operations"]["deskew"])
            self.assertTrue(audit_summary["operations"]["trim_dark_border"])
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_checked_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_combined_trim", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_full_retouch_chain_removes_safe_border_with_ordered_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (140, 105), (250, 250, 250))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 139, 104), outline=(8, 8, 8), width=4)
            draw.rectangle((18, 15, 121, 90), fill=(242, 242, 242))
            draw.rectangle((44, 42, 100, 46), fill=(30, 30, 30))
            draw.rectangle((48, 58, 92, 62), fill=(35, 35, 35))
            image.save(input_dir / "private_full_chain_safe_border.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    despeckle=True,
                    normalize_tones=True,
                    lighten_edge_shadow=True,
                    lighten_background_stains=True,
                    level_illumination_gradient=True,
                    lighten_scanlines=True,
                    enhance_faded_text=True,
                    sharpen_text_edges=True,
                    workers=1,
                ),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["dark_border_trimmed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["dark_border_reason"], "dark edge border trimmed")
            self.assertEqual(record["crop_reason"], "conservative crop applied")
            self.assertEqual(record["processing_audit"]["cumulative_change_guard_action"], "passed")
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            operation_order = [
                operation
                for operation in record["operations"]
                if operation
                in {
                    "deskew_noop",
                    "dark_border_trim_conservative",
                    "auto_crop_conservative",
                    "despeckle_noop",
                    "normalize_tones_noop",
                    "lighten_edge_shadow_noop",
                    "lighten_background_stains_noop",
                    "lighten_scanlines_noop",
                    "enhance_faded_text_noop",
                    "sharpen_text_edges_noop",
                }
            ]
            self.assertEqual(
                operation_order,
                [
                    "deskew_noop",
                    "dark_border_trim_conservative",
                    "auto_crop_conservative",
                    "despeckle_noop",
                    "normalize_tones_noop",
                    "lighten_edge_shadow_noop",
                    "lighten_background_stains_noop",
                    "lighten_scanlines_noop",
                    "enhance_faded_text_noop",
                    "sharpen_text_edges_noop",
                ],
            )
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_checked_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["dark_border_trim"]["trimmed_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["dark_border_trim"]["reason_distribution"],
                {"dark edge border trimmed": 1},
            )
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["reason_distribution"],
                {"conservative crop applied": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_full_chain_safe_border", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_trim_dark_border_noops_for_blank_and_edge_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (100, 80), "white").save(input_dir / "A001_0001.png")
            edge_content = Image.new("RGB", (100, 80), "white")
            draw = ImageDraw.Draw(edge_content)
            draw.rectangle((0, 20, 18, 25), fill=(10, 10, 10))
            draw.line((0, 50, 40, 50), fill=(10, 10, 10), width=2)
            edge_content.save(input_dir / "A001_0002.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True))

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            for record in records.values():
                self.assertFalse(record["dark_border_trimmed"])
                self.assertIsNone(record["dark_border_bbox"])
                self.assertEqual(record["dark_border_reason"], "no confident dark edge border")
                self.assertIn("dark_border_trim_noop", record["operations"])
            self.assertEqual(records["A001_0001.png"]["output_size"], [100, 80])
            self.assertEqual(records["A001_0002.png"]["output_size"], [100, 80])

    def test_scanner_gutter_trim_removes_safe_narrow_light_gutter_with_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-light-gutter-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (240, 180), (236, 236, 236))
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 0, 239, 179), fill=(248, 248, 248))
            draw.rectangle((36, 32, 204, 148), outline=(60, 60, 60), width=2)
            image.save(input_dir / "A001_safe_light_gutter.png")

            report = scan_batch(ScanConfig("synthetic", "light-gutter", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(scanner_gutter_trim=True, workers=1))
            record = manifest["files"][0]
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(record["output_size"], [230, 180])
            self.assertTrue(record["scanner_gutter_trimmed"])
            self.assertEqual(record["scanner_gutter_reason"], "scanner gutter trim applied")
            self.assertIn("scanner_gutter_trim_conservative", record["operations"])
            self.assertLessEqual(record["processing_audit"]["scanner_gutter_max_trim_margin_ratio"], 0.05)
            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["scanner_gutter_trim"]["trimmed_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["scanner_gutter_trim"]["reason_distribution"],
                {"scanner gutter trim applied": 1},
            )
            self.assertTrue(audit_summary["operations"]["scanner_gutter_trim"])
            self.assertIn("scanner_gutter_trim", audit_summary["timing"]["operation_timings"])
            self.assertNotIn("A001_safe_light_gutter", json.dumps(audit_summary))

    def test_scanner_gutter_trim_removes_safe_pale_edge_band_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-pale-gutter-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            disabled_process_dir = root / "processed-disabled"
            input_dir.mkdir()
            source_path = input_dir / "A001_safe_pale_edge_band.png"

            image = Image.new("RGB", (240, 180), (248, 248, 248))
            draw = ImageDraw.Draw(image)
            draw.rectangle((12, 0, 239, 179), fill=(236, 236, 236))
            draw.rectangle((46, 32, 204, 148), outline=(60, 60, 60), width=2)
            image.save(source_path)
            source_bytes_before = source_path.read_bytes()

            report = scan_batch(ScanConfig("synthetic", "pale-gutter", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(scanner_gutter_trim=True, workers=1))
            record = manifest["files"][0]
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(source_path.read_bytes(), source_bytes_before)
            self.assertEqual(record["output_size"], [228, 180])
            self.assertTrue(record["scanner_gutter_trimmed"])
            self.assertEqual(record["scanner_gutter_reason"], "scanner gutter trim applied")
            self.assertEqual(record["scanner_gutter_trim_margins"]["left"], 0.05)
            self.assertLessEqual(record["processing_audit"]["scanner_gutter_max_trim_margin_ratio"], 0.05)
            with Image.open(process_dir / "images" / source_path.name) as processed:
                self.assertLessEqual(processed.convert("L").getpixel((34, 32)), 80)
            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["scanner_gutter_trim"]["reason_distribution"],
                {"scanner gutter trim applied": 1},
            )
            self.assertNotIn("A001_safe_pale_edge_band", json.dumps(audit_summary))

            disabled = process_images(
                report,
                input_dir,
                disabled_process_dir,
                ProcessingOptions(scanner_gutter_trim=False, workers=1),
            )
            disabled_record = disabled["files"][0]
            self.assertEqual(disabled_record["output_size"], [240, 180])
            self.assertFalse(disabled_record["scanner_gutter_trimmed"])
            self.assertEqual(disabled_record["scanner_gutter_reason"], "scanner gutter trim disabled")
            self.assertIn("scanner_gutter_trim_disabled", disabled_record["operations"])
            self.assertEqual(source_path.read_bytes(), source_bytes_before)

    def test_scanner_gutter_trim_preserves_edge_content_and_archival_marks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-light-gutter-protection-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            cases = {
                "A001_page_number.png": lambda draw: draw.text((1, 80), "12", fill=(40, 40, 40)),
                "A002_border_line.png": lambda draw: draw.line((10, 0, 10, 179), fill=(40, 40, 40), width=2),
                "A003_stamp_mark.png": lambda draw: draw.rectangle((3, 20, 8, 28), fill=(160, 30, 30)),
                "A004_handwritten_mark.png": lambda draw: draw.line((2, 120, 8, 126), fill=(30, 30, 30), width=2),
                "A005_archival_edge.png": lambda draw: draw.rectangle((0, 0, 6, 179), fill=(70, 70, 70)),
            }
            for filename, mark in cases.items():
                image = Image.new("RGB", (240, 180), (236, 236, 236))
                draw = ImageDraw.Draw(image)
                draw.rectangle((10, 0, 239, 179), fill=(248, 248, 248))
                mark(draw)
                image.save(input_dir / filename)

            report = scan_batch(ScanConfig("synthetic", "light-gutter-protection", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(scanner_gutter_trim=True, workers=1))
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 0)
            self.assertEqual(audit_summary["counts"]["scanner_gutter_skipped_files"], len(cases))
            for record in manifest["files"]:
                self.assertEqual(record["output_size"], [240, 180])
                self.assertFalse(record["scanner_gutter_trimmed"], record["source_relative_path"])
                self.assertIn("scanner_gutter_trim_noop", record["operations"])
                self.assertEqual(record["processing_audit"]["scanner_gutter_max_trim_margin_ratio"], 0.0)
                self.assertIn(
                    record["scanner_gutter_reason"],
                    {
                        "scanner gutter skipped: protected edge content",
                        "scanner gutter skipped: no narrow uniform light band",
                    },
                )

    def test_scanner_gutter_trim_skips_ambiguous_pale_edge_bands_with_public_reasons(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-light-gutter-ambiguous-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            multi_edge = Image.new("RGB", (240, 180), (248, 248, 248))
            multi_draw = ImageDraw.Draw(multi_edge)
            multi_draw.rectangle((12, 12, 227, 179), fill=(236, 236, 236))
            multi_draw.rectangle((46, 32, 194, 148), outline=(60, 60, 60), width=2)
            multi_edge.save(input_dir / "A001_multi_edge.png")

            colored_paper = Image.new("RGB", (240, 180), (248, 248, 248))
            colored_draw = ImageDraw.Draw(colored_paper)
            colored_draw.rectangle((12, 0, 239, 179), fill=(226, 238, 202))
            colored_draw.rectangle((46, 32, 204, 148), outline=(60, 60, 60), width=2)
            colored_paper.save(input_dir / "A002_colored_paper.png")

            non_uniform = Image.new("RGB", (240, 180), (248, 248, 248))
            non_uniform_draw = ImageDraw.Draw(non_uniform)
            non_uniform_draw.rectangle((12, 0, 239, 179), fill=(236, 236, 236))
            for y in range(0, 180, 6):
                non_uniform_draw.line((0, y, 11, y), fill=(230, 230, 230))
            non_uniform_draw.rectangle((46, 32, 204, 148), outline=(60, 60, 60), width=2)
            non_uniform.save(input_dir / "A003_non_uniform.png")

            report = scan_batch(ScanConfig("synthetic", "light-gutter-ambiguous", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(scanner_gutter_trim=True, workers=1))
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            self.assertEqual(records["A001_multi_edge.png"]["output_size"], [240, 180])
            self.assertFalse(records["A001_multi_edge.png"]["scanner_gutter_trimmed"])
            self.assertEqual(
                records["A001_multi_edge.png"]["scanner_gutter_reason"],
                "scanner gutter skipped: ambiguous multi-edge light band",
            )
            self.assertEqual(records["A002_colored_paper.png"]["output_size"], [240, 180])
            self.assertFalse(records["A002_colored_paper.png"]["scanner_gutter_trimmed"])
            self.assertEqual(
                records["A002_colored_paper.png"]["scanner_gutter_reason"],
                "scanner gutter skipped: colored or non-neutral original",
            )
            self.assertEqual(records["A003_non_uniform.png"]["output_size"], [240, 180])
            self.assertFalse(records["A003_non_uniform.png"]["scanner_gutter_trimmed"])
            self.assertEqual(
                records["A003_non_uniform.png"]["scanner_gutter_reason"],
                "scanner gutter skipped: no narrow uniform light band",
            )

    def test_despeckle_removes_isolated_noise_without_breaking_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            draw = ImageDraw.Draw(image)
            draw.line((10, 30, 70, 30), fill=(0, 0, 0), width=2)
            for point in [(5, 5), (20, 8), (74, 12), (40, 50)]:
                image.putpixel(point, (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                black_pixels = _dark_pixel_count(processed)
                self.assertLess(black_pixels, _dark_pixel_count(image))
                self.assertLessEqual(abs(processed.convert("L").getpixel((40, 30)) - 0), 5)
            record = manifest["files"][0]
            self.assertTrue(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 4)
            self.assertEqual(record["despeckle_reason"], "isolated dark pixels replaced")
            self.assertIn("despeckle_isolated_pixels", record["operations"])

    def test_despeckle_removes_tiny_isolated_blob_and_records_aggregate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_blob_noise.png"
            image = Image.new("RGB", (100, 80), "white")
            for point in [(52, 38), (53, 38), (52, 39), (53, 39)]:
                image.putpixel(point, (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            with Image.open(process_dir / "images" / "private_blob_noise.png") as processed:
                grayscale = processed.convert("L")
                for point in [(52, 38), (53, 38), (52, 39), (53, 39)]:
                    self.assertGreaterEqual(grayscale.getpixel(point), 240)
            record = manifest["files"][0]
            self.assertTrue(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 4)
            self.assertEqual(record["processing_audit"]["despeckle_pixel_ratio"], round(4 / (100 * 80), 6))
            self.assertLessEqual(record["processing_audit"]["despeckle_pixel_ratio"], 0.001)
            self.assertFalse(record["processing_audit"]["local_content_change_guard_checked"])
            self.assertEqual(audit_summary["counts"]["despeckled_files"], 1)
            self.assertEqual(audit_summary["counts"]["despeckle_skipped_files"], 0)
            self.assertEqual(audit_summary["counts"]["local_content_change_guard_checked_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["pixels_changed"], 4)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["backend_mode"], "fallback")
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_distribution"]["isolated dark pixels replaced"],
                1,
            )
            self.assertIn("despeckle_pixel_ratio", audit_summary["metrics"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_blob_noise", audit_summary_text)

    def test_despeckle_removes_isolated_dark_gray_and_light_soil_spots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_small_soil_spots.png"
            image = Image.new("RGB", (130, 96), (246, 246, 242))
            draw = ImageDraw.Draw(image)
            spots = {
                "black": ((20, 18, 21, 19), (0, 0, 0)),
                "deep_gray": ((48, 30, 49, 31), (72, 72, 72)),
                "light_gray": ((76, 48, 77, 49), (184, 184, 178)),
                "pale_yellow": ((104, 68, 105, 69), (218, 210, 170)),
            }
            for box, color in spots.values():
                draw.rectangle(box, fill=color)
            image.save(source)
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            self.assertEqual(source.read_bytes(), source_bytes)
            with Image.open(process_dir / "images" / "private_small_soil_spots.png") as processed:
                grayscale = processed.convert("L")
                for box, _color in spots.values():
                    self.assertGreaterEqual(grayscale.getpixel((box[0], box[1])), 238)
            record = manifest["files"][0]
            self.assertTrue(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 16)
            self.assertEqual(record["despeckle_reason"], "isolated dark pixels replaced")
            self.assertLessEqual(record["processing_audit"]["despeckle_pixel_ratio"], 0.002)
            self.assertEqual(audit_summary["counts"]["despeckled_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["pixels_changed"], 16)
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_distribution"]["isolated dark pixels replaced"],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("private_small_soil_spots", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_despeckle_protects_punctuation_lines_red_marks_edges_and_texture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_despeckle_protected_marks.png"
            image = Image.new("RGB", (160, 120), (246, 246, 242))
            draw = ImageDraw.Draw(image)
            draw.line((24, 34, 94, 34), fill=(25, 25, 25), width=2)
            draw.rectangle((98, 32, 99, 33), fill=(28, 28, 28))
            draw.rectangle((28, 60, 120, 92), outline=(25, 25, 25), width=2)
            draw.rectangle((130, 22, 132, 24), fill=(190, 25, 25))
            draw.rectangle((2, 86, 3, 87), fill=(184, 184, 178))
            for y in range(12, 108, 6):
                for x in range(12, 72, 6):
                    image.putpixel((x, y), (213, 213, 211))
            draw.rectangle((142, 104, 143, 105), fill=(186, 186, 180))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "private_despeckle_protected_marks.png") as processed:
                rgb = processed.convert("RGB")
                grayscale = rgb.convert("L")
                self.assertLessEqual(grayscale.getpixel((98, 32)), 35)
                self.assertLessEqual(grayscale.getpixel((60, 34)), 35)
                self.assertLessEqual(grayscale.getpixel((28, 60)), 35)
                self.assertEqual(rgb.getpixel((130, 22)), (190, 25, 25))
                self.assertLessEqual(grayscale.getpixel((2, 86)), 190)
                self.assertLessEqual(grayscale.getpixel((12, 12)), 214)
                self.assertGreaterEqual(grayscale.getpixel((142, 104)), 238)
            record = manifest["files"][0]
            self.assertTrue(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 4)
            self.assertEqual(record["despeckle_reason"], "isolated dark pixels replaced")

    def test_despeckle_skips_high_density_light_soil_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_dense_light_soil.png"
            image = Image.new("RGB", (100, 100), (246, 246, 242))
            draw = ImageDraw.Draw(image)
            for y in range(8, 94, 5):
                for x in range(8, 94, 5):
                    draw.rectangle((x, y, x + 1, y + 1), fill=(184, 184, 178))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            with Image.open(process_dir / "images" / "private_dense_light_soil.png") as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "despeckle skipped: candidate density exceeds safety threshold")
            self.assertEqual(audit_summary["counts"]["despeckle_skipped_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_distribution"][
                    "despeckle skipped: candidate density exceeds safety threshold"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_dense_light_soil", audit_summary_text)

    def test_despeckle_skips_uncertain_light_soil_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (100, 80), (246, 246, 242))
            ImageDraw.Draw(image).rectangle((44, 36, 48, 38), fill=(184, 184, 178))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "no isolated dark pixels found")

    def test_despeckle_preserves_synthetic_text_marks_and_cleans_sparse_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (120, 90), "white")
            draw = ImageDraw.Draw(image)
            draw.line((14, 35, 45, 35), fill=(0, 0, 0), width=2)
            draw.line((18, 30, 18, 42), fill=(0, 0, 0), width=2)
            image.putpixel((50, 36), (0, 0, 0))
            draw.line((92, 8, 110, 8), fill=(0, 0, 0), width=2)
            image.putpixel((114, 8), (0, 0, 0))
            draw.rectangle((31, 55, 72, 75), outline=(0, 0, 0), width=2)
            draw.line((5, 12, 5, 82), fill=(0, 0, 0), width=2)
            draw.line((11, 21, 18, 25), fill=(0, 0, 0), width=2)
            draw.line((78, 58, 90, 64), fill=(0, 0, 0), width=2)
            draw.rectangle((96, 56, 101, 61), outline=(0, 0, 0), width=1)
            image.putpixel((98, 58), (0, 0, 0))
            for point in [(82, 15), (65, 28)]:
                image.putpixel(point, (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                grayscale = processed.convert("L")
                for protected_point in [(30, 35), (50, 36), (114, 8), (31, 55), (5, 40), (13, 22), (84, 61), (98, 58)]:
                    self.assertLessEqual(grayscale.getpixel(protected_point), 5)
                for removed_noise in [(82, 15), (65, 28)]:
                    self.assertGreaterEqual(grayscale.getpixel(removed_noise), 240)
            record = manifest["files"][0]
            self.assertTrue(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 2)
            self.assertEqual(record["despeckle_reason"], "isolated dark pixels replaced")
            self.assertEqual(record["processing_audit"]["despeckle_pixel_ratio"], round(2 / (120 * 90), 6))

    def test_despeckle_preserves_edge_near_dark_marks_with_auditable_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            for point in [(4, 4), (76, 55)]:
                image.putpixel(point, (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                grayscale = processed.convert("L")
                for point in [(4, 4), (76, 55)]:
                    self.assertLessEqual(grayscale.getpixel(point), 5)
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "protected edge dark marks preserved")
            self.assertIn("despeckle_noop", record["operations"])
            self.assertEqual(audit_summary["counts"]["despeckle_skipped_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_distribution"]["protected edge dark marks preserved"],
                1,
            )

    def test_despeckle_skips_excessive_pixel_changes_with_manual_review_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (100, 100), "white")
            for y in range(8, 94, 7):
                for x in range(8, 94, 7):
                    image.putpixel((x, y), (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch("archive_scan_qc.processing._despeckle_has_nearby_content_context", return_value=False):
                manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                self.assertEqual(_dark_pixel_count(processed), _dark_pixel_count(image))
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "despeckle skipped: pixel change ratio exceeds safety threshold")

    def test_despeckle_skips_high_density_candidates_with_manual_review_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (100, 100), "white")
            for y in range(7, 95, 5):
                for x in range(7, 95, 5):
                    image.putpixel((x, y), (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                self.assertEqual(_dark_pixel_count(processed), _dark_pixel_count(image))
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "despeckle skipped: candidate density exceeds safety threshold")

    def test_despeckle_skips_dense_texture_and_audits_aggregate_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_dense_texture.png"
            image = Image.new("RGB", (100, 100), "white")
            for y in range(10, 90, 5):
                for x in range(10, 90, 5):
                    image.putpixel((x, y), (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "despeckle skipped: candidate density exceeds safety threshold")
            self.assertEqual(audit_summary["counts"]["despeckle_skipped_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_distribution"][
                    "despeckle skipped: candidate density exceeds safety threshold"
                ],
                1,
            )
            self.assertNotIn("private_dense_texture", audit_summary_text)

    def test_despeckle_candidate_points_prefilter_isolated_speckles_only(self) -> None:
        mask = Image.new("L", (80, 60), 0)
        draw = ImageDraw.Draw(mask)
        draw.line((10, 30, 70, 30), fill=255, width=2)
        draw.rectangle((15, 12, 20, 17), fill=255)
        for point in [(50, 20), (51, 20), (50, 21), (51, 21)]:
            mask.putpixel(point, 255)
        for point in [(5, 5), (20, 8), (74, 12), (40, 50)]:
            mask.putpixel(point, 255)

        self.assertEqual(
            sorted(_despeckle_candidate_points(mask)),
            [(5, 5), (20, 8), (40, 50), (50, 20), (50, 21), (51, 20), (51, 21), (74, 12)],
        )

    def test_despeckle_candidate_points_protect_near_edge_candidates(self) -> None:
        mask = Image.new("L", (80, 60), 0)
        for point in [(4, 4), (5, 5), (74, 54), (75, 55)]:
            mask.putpixel(point, 255)

        self.assertEqual(sorted(_despeckle_candidate_points(mask)), [])

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional fast path unavailable")
    def test_despeckle_candidate_points_numpy_matches_fallback_synthetic_masks(self) -> None:
        masks: list[Image.Image] = []

        clean = Image.new("L", (80, 60), 0)
        masks.append(clean)

        isolated = Image.new("L", (80, 60), 0)
        for point in [(5, 5), (20, 8), (74, 12), (40, 50)]:
            isolated.putpixel(point, 255)
        masks.append(isolated)

        tiny_blob = Image.new("L", (80, 60), 0)
        for point in [(50, 20), (51, 20), (50, 21), (51, 21)]:
            tiny_blob.putpixel(point, 255)
        masks.append(tiny_blob)

        clustered = Image.new("L", (80, 60), 0)
        draw = ImageDraw.Draw(clustered)
        draw.rectangle((15, 12, 20, 17), fill=255)
        draw.line((10, 30, 70, 30), fill=255, width=2)
        clustered.putpixel((40, 50), 255)
        masks.append(clustered)

        edges = Image.new("L", (80, 60), 0)
        for point in [(0, 10), (79, 20), (30, 0), (40, 59), (4, 4)]:
            edges.putpixel(point, 255)
        masks.append(edges)

        dense = Image.new("L", (120, 80), 0)
        draw = ImageDraw.Draw(dense)
        for y in range(12, 70, 9):
            draw.rectangle((12, y, 108, y + 2), fill=255)
        for x in range(18, 108, 12):
            draw.line((x, 10, x, 72), fill=255, width=3)
        masks.append(dense)

        for mask in masks:
            self.assertEqual(
                _despeckle_candidate_points_numpy(mask),
                _despeckle_candidate_points_fallback(mask),
            )

    def test_despeckle_candidate_points_falls_back_when_numpy_unavailable(self) -> None:
        mask = Image.new("L", (20, 20), 0)
        mask.putpixel((10, 10), 255)

        with mock.patch("archive_scan_qc.processing._load_numpy", return_value=None):
            self.assertEqual(_despeckle_candidate_points(mask, backend="numpy"), [(10, 10)])

    def test_despeckle_candidate_points_default_backend_is_fallback(self) -> None:
        mask = Image.new("L", (20, 20), 0)
        mask.putpixel((10, 10), 255)

        with mock.patch("archive_scan_qc.processing._despeckle_candidate_points_numpy", return_value=[(5, 5)]):
            self.assertEqual(_despeckle_candidate_points(mask), [(10, 10)])

    def test_despeckle_candidate_points_dense_content_fast_path(self) -> None:
        mask = Image.new("L", (120, 80), 0)
        draw = ImageDraw.Draw(mask)
        for y in range(12, 70, 9):
            draw.rectangle((12, y, 108, y + 2), fill=255)
        for x in range(18, 108, 12):
            draw.line((x, 10, x, 72), fill=255, width=3)

        self.assertEqual(_despeckle_candidate_points(mask), [])

    def test_despeckle_dense_prefilter_keeps_real_tiny_blob_candidates(self) -> None:
        mask = Image.new("L", (240, 180), 0)
        draw = ImageDraw.Draw(mask)
        draw.rectangle((20, 20, 150, 140), fill=255)
        for point in [(190, 90), (191, 90), (190, 91), (191, 91)]:
            mask.putpixel(point, 255)

        self.assertEqual(
            sorted(_despeckle_candidate_points(mask)),
            [(190, 90), (190, 91), (191, 90), (191, 91)],
        )

    def test_despeckle_fast_path_preserves_noop_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "no isolated dark pixels found")
            self.assertIn("despeckle_noop", record["operations"])

    def test_despeckle_skips_candidate_scan_when_no_dark_pixels_possible(self) -> None:
        image = Image.new("RGB", (80, 60), (245, 245, 245))

        with mock.patch("archive_scan_qc.processing._despeckle_candidate_points_with_backend") as candidates:
            processed, changed, backend_mode = _despeckle_isolated_pixels(image, backend="fallback")

        candidates.assert_not_called()
        self.assertEqual(changed, 0)
        self.assertEqual(backend_mode, "not_applicable")
        self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())

    def test_despeckle_still_validates_backend_on_no_dark_fast_path(self) -> None:
        image = Image.new("RGB", (80, 60), "white")

        with self.assertRaisesRegex(ValueError, "despeckle backend must be fallback or numpy"):
            _despeckle_isolated_pixels(image, backend="invalid")

    def test_despeckle_fast_path_preserves_border_dark_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            for point in [(0, 10), (79, 20), (30, 0), (40, 59)]:
                image.putpixel(point, (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                grayscale = processed.convert("L")
                for point in [(0, 10), (79, 20), (30, 0), (40, 59)]:
                    self.assertLessEqual(grayscale.getpixel(point), 5)
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "protected edge dark marks preserved")

    def test_despeckle_preserves_clustered_dark_marks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            for y in range(20, 23):
                for x in range(20, 23):
                    image.putpixel((x, y), (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                grayscale = processed.convert("L")
                for y in range(20, 23):
                    for x in range(20, 23):
                        self.assertLessEqual(grayscale.getpixel((x, y)), 5)
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "no isolated dark pixels found")

    def test_despeckle_combines_with_crop_deskew_trim_and_tones_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_combined_despeckle.png"
            image = Image.new("RGB", (140, 100), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((12, 12, 127, 87), outline=(0, 0, 0), width=1)
            draw.line((24, 42, 116, 42), fill=(0, 0, 0), width=2)
            draw.line((24, 60, 74, 60), fill=(0, 0, 0), width=2)
            for point in [(88, 25), (89, 25), (88, 26), (89, 26)]:
                image.putpixel(point, (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    despeckle=True,
                    lighten_background_stains=True,
                    normalize_tones=True,
                    workers=1,
                ),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            record = manifest["files"][0]
            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertTrue(audit_summary["operations"]["auto_crop"])
            self.assertTrue(audit_summary["operations"]["deskew"])
            self.assertTrue(audit_summary["operations"]["trim_dark_border"])
            self.assertTrue(audit_summary["operations"]["despeckle"])
            self.assertTrue(audit_summary["operations"]["lighten_background_stains"])
            self.assertTrue(audit_summary["operations"]["normalize_tones"])
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertIn("despeckle_pixel_ratio", audit_summary["metrics"])
            self.assertIn("background_stains", audit_summary["guardrails"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_combined_despeckle", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_full_retouch_chain_preserves_auto_crop_aggregate_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (140, 105), (250, 250, 250))
            draw = ImageDraw.Draw(image)
            draw.rectangle((14, 12, 125, 93), fill=(242, 242, 242))
            draw.rectangle((44, 42, 100, 46), fill=(30, 30, 30))
            draw.rectangle((48, 58, 92, 62), fill=(35, 35, 35))
            image.save(input_dir / "private_full_chain_crop.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    despeckle=True,
                    normalize_tones=True,
                    lighten_edge_shadow=True,
                    lighten_background_stains=True,
                    level_illumination_gradient=True,
                    lighten_scanlines=True,
                    enhance_faded_text=True,
                    sharpen_text_edges=True,
                    workers=1,
                ),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]

            self.assertEqual(manifest["summary"]["processed_files"], 1)
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            self.assertEqual(record["status"], "processed")
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertEqual(record["processing_audit"]["cumulative_change_guard_action"], "passed")
            self.assertTrue(record["cropped"])
            for option in (
                "auto_crop",
                "deskew",
                "trim_dark_border",
                "despeckle",
                "normalize_tones",
                "lighten_edge_shadow",
                "lighten_background_stains",
                "level_illumination_gradient",
                "lighten_scanlines",
                "enhance_faded_text",
                "sharpen_text_edges",
            ):
                self.assertTrue(audit_summary["operations"][option], option)
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["skipped_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_full_chain_crop", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_local_content_change_guard_reverts_small_text_damage_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_local_text_damage.png"
            image = Image.new("RGB", (160, 120), "white")
            draw = ImageDraw.Draw(image)
            for y in (36, 52, 68):
                draw.rectangle((30, y, 130, y + 3), fill=(0, 0, 0))
            draw.rectangle((4, 96, 24, 100), fill=(0, 0, 0))
            image.save(source)

            def erase_one_text_line(candidate: Image.Image) -> processing_module.ScanlineLighteningResult:
                damaged = candidate.copy()
                ImageDraw.Draw(damaged).rectangle((30, 52, 130, 55), fill=(255, 255, 255))
                ImageDraw.Draw(damaged).rectangle((4, 96, 24, 100), fill=(255, 255, 255))
                return processing_module.ScanlineLighteningResult(
                    damaged,
                    True,
                    "synthetic local text damage",
                    "horizontal",
                    1,
                    0.0,
                    255.0,
                    255.0,
                    (101 * 4 + 21 * 5) / (160 * 120),
                    (101 * 4 + 21 * 5) / (160 * 120),
                )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch(
                "archive_scan_qc.processing._lighten_scanlines_conservative",
                side_effect=erase_one_text_line,
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(lighten_scanlines=True, workers=1),
                )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]

            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["scanlines_lightened"])
            self.assertEqual(record["scanlines_reason"], "reverted by local content change guard")
            self.assertIn("local_content_change_guard_reverted_to_source", record["operations"])
            self.assertIn("local_content_change_guard_reverted_to_source", record["processing_warnings"])
            self.assertEqual(record["processing_audit"]["local_content_change_guard_action"], "reverted_to_source")
            self.assertTrue(record["processing_audit"]["local_content_change_guard_reverted"])
            self.assertIn(
                "local_content_changed_ratio",
                record["processing_audit"]["local_content_change_guard_reasons"],
            )
            self.assertIn(
                "edge_content_changed_ratio",
                record["processing_audit"]["local_content_change_guard_reasons"],
            )
            with Image.open(process_dir / "images" / "private_local_text_damage.png") as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            self.assertEqual(audit_summary["counts"]["local_content_change_guard_checked_files"], 1)
            self.assertEqual(audit_summary["counts"]["local_content_change_guard_reverted_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["local_content_change_guard"]["reason_distribution"][
                    "local_content_changed_ratio"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_local_text_damage", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_local_content_change_guard_allows_safe_background_stain_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_safe_background_stain.png"
            image = Image.new("RGB", (240, 170), (240, 240, 236))
            draw = ImageDraw.Draw(image)
            for y in (42, 66, 90):
                draw.rectangle((48, y, 178, y + 5), fill=(38, 38, 38))
            draw.ellipse((58, 116, 80, 134), fill=(216, 216, 211))
            draw.ellipse((188, 18, 210, 34), fill=(218, 218, 214))
            draw.rectangle((196, 112, 208, 124), fill=(214, 214, 210))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["background_stains_lightened"])
            self.assertEqual(record["processing_audit"]["local_content_change_guard_action"], "passed")
            self.assertFalse(record["processing_audit"]["local_content_change_guard_reverted"])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertEqual(audit_summary["counts"]["local_content_change_guard_checked_files"], 0)
            self.assertEqual(audit_summary["counts"]["local_content_change_guard_reverted_files"], 0)
            self.assertEqual(
                audit_summary["guardrails"]["local_content_change_guard"]["reason_distribution"],
                {},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_background_stain", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_fold_shadow_cleanup_is_opt_in_and_lightens_safe_narrow_background_band(self) -> None:
        cases = {
            "vertical": ((109, 14, 110, 146), (109, 40), (30, 40)),
            "horizontal": ((34, 78, 186, 79), (80, 78), (80, 30)),
        }
        for orientation, (band_box, changed_point, unchanged_point) in cases.items():
            with self.subTest(orientation=orientation), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                process_dir = root / "processed"
                input_dir.mkdir()
                source = input_dir / f"private_safe_fold_shadow_{orientation}.png"
                image = Image.new("RGB", (220, 160), (242, 242, 238))
                draw = ImageDraw.Draw(image)
                draw.rectangle(band_box, fill=(234, 234, 230))
                image.save(source)
                source_bytes = source.read_bytes()

                report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
                disabled_manifest = process_images(
                    report,
                    input_dir,
                    process_dir / "disabled",
                    ProcessingOptions(workers=1),
                )
                disabled_record = disabled_manifest["files"][0]
                self.assertFalse(disabled_record["fold_shadows_lightened"])
                self.assertEqual(disabled_record["fold_shadows_reason"], "fold shadow cleanup disabled")
                with Image.open(process_dir / "disabled" / "images" / source.name) as disabled:
                    self.assertEqual(disabled.convert("RGB").tobytes(), image.tobytes())

                manifest = process_images(
                    report,
                    input_dir,
                    process_dir / "enabled",
                    ProcessingOptions(lighten_fold_shadows=True, workers=1),
                )
                audit_summary_text = (process_dir / "enabled" / "processing_audit_summary.json").read_text(
                    encoding="utf-8"
                )
                audit_summary = json.loads(audit_summary_text)
                record = manifest["files"][0]

                self.assertEqual(source.read_bytes(), source_bytes)
                self.assertTrue(record["fold_shadows_lightened"])
                self.assertEqual(record["fold_shadows_reason_code"], "applied_narrow_neutral_background_band")
                self.assertEqual(record["fold_shadows_orientation"], orientation)
                self.assertEqual(record["fold_shadows_count"], 1)
                self.assertGreaterEqual(record["fold_shadows_delta"], 4.0)
                self.assertGreater(record["fold_shadows_candidate_pixel_ratio"], 0.002)
                self.assertGreater(record["fold_shadows_changed_pixel_ratio"], 0.002)
                self.assertLessEqual(record["fold_shadows_changed_pixel_ratio"], 0.075)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
                with Image.open(process_dir / "enabled" / "images" / source.name) as processed:
                    grayscale = processed.convert("L")
                    original_grayscale = image.convert("L")
                    self.assertGreater(
                        grayscale.getpixel(changed_point),
                        original_grayscale.getpixel(changed_point),
                    )
                    self.assertEqual(
                        grayscale.getpixel(unchanged_point),
                        original_grayscale.getpixel(unchanged_point),
                    )
                self.assertTrue(audit_summary["operations"]["lighten_fold_shadows"])
                self.assertEqual(audit_summary["counts"]["fold_shadows_lightened_files"], 1)
                self.assertEqual(audit_summary["guardrails"]["fold_shadows"]["applied_files"], 1)
                self.assertEqual(
                    audit_summary["guardrails"]["fold_shadows"]["reason_code_distribution"][
                        "applied_narrow_neutral_background_band"
                    ],
                    1,
                )
                self.assertTrue(audit_summary["privacy"]["aggregate_only"])
                self.assertNotIn("private_safe_fold_shadow", audit_summary_text)
                self.assertNotIn(str(input_dir), audit_summary_text)

    def test_illumination_gradient_leveling_is_opt_in_and_reduces_safe_mild_gradient(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_safe_illumination_gradient.png"
            width, height = 220, 150
            image = Image.new("RGB", (width, height))
            pixels = image.load()
            for x in range(width):
                value = int(240 - (18 * x / (width - 1)))
                for y in range(height):
                    pixels[x, y] = (value, value, value)
            image.save(source)
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            disabled_manifest = process_images(report, input_dir, process_dir / "disabled", ProcessingOptions(workers=1))
            disabled_record = disabled_manifest["files"][0]
            self.assertFalse(disabled_record["illumination_gradient_levelled"])
            self.assertEqual(disabled_record["illumination_gradient_reason_code"], "disabled")
            with Image.open(process_dir / "disabled" / "images" / source.name) as disabled:
                self.assertEqual(disabled.convert("RGB").tobytes(), image.tobytes())

            manifest = process_images(
                report,
                input_dir,
                process_dir / "enabled",
                ProcessingOptions(level_illumination_gradient=True, workers=1),
            )
            audit_summary_text = (process_dir / "enabled" / "processing_audit_summary.json").read_text(
                encoding="utf-8"
            )
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(record["illumination_gradient_levelled"])
            self.assertEqual(record["illumination_gradient_reason_code"], "applied")
            self.assertEqual(record["illumination_gradient_orientation"], "vertical")
            self.assertGreater(record["illumination_gradient_delta_before"], 12.0)
            self.assertLess(record["illumination_gradient_delta_after"], record["illumination_gradient_delta_before"])
            self.assertLessEqual(record["illumination_gradient_correction_delta"], 10.0)
            self.assertGreater(record["illumination_gradient_changed_pixel_ratio"], 0.70)
            self.assertLessEqual(record["processing_audit"]["brightness_delta"], 12.0)
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            with Image.open(process_dir / "enabled" / "images" / source.name) as processed:
                grayscale = processed.convert("L")
                left = ImageStat.Stat(grayscale.crop((0, 0, 30, height))).mean[0]
                right = ImageStat.Stat(grayscale.crop((width - 30, 0, width, height))).mean[0]
                self.assertLess(abs(left - right), 9.0)
            self.assertTrue(audit_summary["operations"]["level_illumination_gradient"])
            self.assertEqual(audit_summary["counts"]["illumination_gradient_levelled_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["illumination_gradient"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["illumination_gradient"]["reason_code_distribution"]["applied"],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertNotIn("private_safe_illumination_gradient", audit_summary_text)
            self.assertNotIn(str(input_dir), audit_summary_text)

    def test_illumination_gradient_leveling_reduces_safe_two_edge_falloff(self) -> None:
        def two_edge_page(orientation: str) -> Image.Image:
            width, height = 220, 150
            page = Image.new("RGB", (width, height))
            pixels = page.load()
            for y in range(height):
                for x in range(width):
                    position = x / (width - 1) if orientation == "vertical" else y / (height - 1)
                    edge_weight = abs(position - 0.5) * 2
                    value = int(round(242 - 14 * edge_weight))
                    pixels[x, y] = (value, value, value)
            return page

        for orientation in ("vertical", "horizontal"):
            with self.subTest(orientation=orientation), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                process_dir = root / "processed"
                input_dir.mkdir()
                image = two_edge_page(orientation)
                source = input_dir / f"private_safe_two_edge_illumination_{orientation}.png"
                image.save(source)
                source_bytes = source.read_bytes()

                report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
                disabled_manifest = process_images(
                    report,
                    input_dir,
                    process_dir / "disabled",
                    ProcessingOptions(workers=1),
                )
                disabled_record = disabled_manifest["files"][0]
                self.assertFalse(disabled_record["illumination_gradient_levelled"])
                self.assertEqual(disabled_record["illumination_gradient_reason_code"], "disabled")
                with Image.open(process_dir / "disabled" / "images" / source.name) as disabled:
                    self.assertEqual(disabled.convert("RGB").tobytes(), image.tobytes())

                manifest = process_images(
                    report,
                    input_dir,
                    process_dir / "enabled",
                    ProcessingOptions(level_illumination_gradient=True, workers=1),
                )
                audit_summary_text = (process_dir / "enabled" / "processing_audit_summary.json").read_text(
                    encoding="utf-8"
                )
                audit_summary = json.loads(audit_summary_text)
                record = manifest["files"][0]

                self.assertEqual(source.read_bytes(), source_bytes)
                self.assertTrue(record["illumination_gradient_levelled"])
                self.assertEqual(record["illumination_gradient_reason_code"], "applied")
                self.assertEqual(record["illumination_gradient_orientation"], orientation)
                self.assertGreater(record["illumination_gradient_delta_before"], 8.0)
                self.assertLess(record["illumination_gradient_delta_after"], record["illumination_gradient_delta_before"])
                self.assertLessEqual(record["illumination_gradient_correction_delta"], 10.0)
                self.assertGreater(record["illumination_gradient_candidate_pixel_ratio"], 0.90)
                self.assertGreater(record["illumination_gradient_changed_pixel_ratio"], 0.70)
                self.assertLessEqual(record["processing_audit"]["brightness_delta"], 12.0)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
                with Image.open(process_dir / "enabled" / "images" / source.name) as processed:
                    original_gray = image.convert("L")
                    processed_gray = processed.convert("L")
                    if orientation == "vertical":
                        original_edge = ImageStat.Stat(original_gray.crop((0, 0, 24, image.height))).mean[0]
                        original_center = ImageStat.Stat(original_gray.crop((98, 0, 122, image.height))).mean[0]
                        processed_edge = ImageStat.Stat(processed_gray.crop((0, 0, 24, image.height))).mean[0]
                        processed_center = ImageStat.Stat(processed_gray.crop((98, 0, 122, image.height))).mean[0]
                    else:
                        original_edge = ImageStat.Stat(original_gray.crop((0, 0, image.width, 18))).mean[0]
                        original_center = ImageStat.Stat(original_gray.crop((0, 66, image.width, 84))).mean[0]
                        processed_edge = ImageStat.Stat(processed_gray.crop((0, 0, image.width, 18))).mean[0]
                        processed_center = ImageStat.Stat(processed_gray.crop((0, 66, image.width, 84))).mean[0]
                    self.assertLess(processed_center - processed_edge, original_center - original_edge)
                self.assertTrue(audit_summary["operations"]["level_illumination_gradient"])
                self.assertEqual(audit_summary["counts"]["illumination_gradient_levelled_files"], 1)
                self.assertEqual(audit_summary["guardrails"]["illumination_gradient"]["applied_files"], 1)
                self.assertEqual(
                    audit_summary["guardrails"]["illumination_gradient"]["orientation_distribution"][orientation],
                    1,
                )
                self.assertTrue(audit_summary["privacy"]["aggregate_only"])
                self.assertNotIn("private_safe_two_edge_illumination", audit_summary_text)
                self.assertNotIn(str(input_dir), audit_summary_text)

    def test_illumination_gradient_leveling_skips_two_edge_protected_content(self) -> None:
        def two_edge_page() -> Image.Image:
            width, height = 220, 150
            page = Image.new("RGB", (width, height))
            pixels = page.load()
            for x in range(width):
                edge_weight = abs((x / (width - 1)) - 0.5) * 2
                value = int(round(242 - 14 * edge_weight))
                for y in range(height):
                    pixels[x, y] = (value, value, value)
            return page

        cases = {
            "page_number": lambda draw: draw.rectangle((104, 132, 116, 138), fill=(35, 35, 35)),
            "header_footer": lambda draw: [
                draw.rectangle((72, 18, 148, 24), fill=(35, 35, 35)),
                draw.rectangle((72, 136, 148, 142), fill=(35, 35, 35)),
            ],
            "table_lines": lambda draw: [
                draw.line((52, y, 168, y), fill=(35, 35, 35), width=2) for y in (46, 78, 110)
            ],
            "underline": lambda draw: draw.line((76, 88, 144, 88), fill=(35, 35, 35), width=2),
            "binding_holes": lambda draw: [
                draw.ellipse((7, y, 17, y + 10), fill=(24, 24, 24)) for y in (34, 72, 110)
            ],
            "water_stain": lambda draw: draw.ellipse((74, 42, 146, 118), fill=(198, 190, 166)),
            "original_fold_mark": lambda draw: draw.line((109, 14, 109, 146), fill=(148, 148, 144), width=1),
            "tear": lambda draw: [
                draw.polygon([(0, 82), (20, 72), (16, 96)], fill=(202, 198, 184)),
                draw.line((0, 82, 20, 72, 16, 96), fill=(120, 116, 106), width=1),
            ],
        }
        for name, draw_mark in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                process_dir = root / "processed"
                input_dir.mkdir()
                image = two_edge_page()
                draw_mark(ImageDraw.Draw(image))
                source = input_dir / f"private_two_edge_illumination_protected_{name}.png"
                image.save(source)

                report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(level_illumination_gradient=True, workers=1),
                )
                audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
                audit_summary = json.loads(audit_summary_text)
                record = manifest["files"][0]

                self.assertFalse(record["illumination_gradient_levelled"], name)
                self.assertIn(
                    record["illumination_gradient_reason_code"],
                    {"protected_content", "not_uniform", "low_confidence"},
                )
                with Image.open(process_dir / "images" / source.name) as processed:
                    self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes(), name)
                self.assertEqual(audit_summary["counts"]["illumination_gradient_levelled_files"], 0)
                self.assertTrue(audit_summary["privacy"]["aggregate_only"])
                self.assertNotIn(f"private_two_edge_illumination_protected_{name}", audit_summary_text)

    def test_illumination_gradient_leveling_skips_protected_content(self) -> None:
        def gradient_page() -> Image.Image:
            width, height = 220, 150
            page = Image.new("RGB", (width, height))
            pixels = page.load()
            for x in range(width):
                value = int(240 - (18 * x / (width - 1)))
                for y in range(height):
                    pixels[x, y] = (value, value, value)
            return page

        cases = {
            "text": lambda draw: draw.rectangle((48, 58, 172, 65), fill=(30, 30, 30)),
            "handwriting": lambda draw: draw.line((54, 48, 170, 92), fill=(42, 42, 42), width=3),
            "stamp": lambda draw: draw.ellipse((88, 42, 132, 86), outline=(190, 20, 20), width=4),
            "photo": lambda draw: [draw.rectangle((x, 36, x + 3, 108), fill=(70 + x % 80, 80, 92)) for x in range(58, 154, 4)],
            "chart_map": lambda draw: [draw.line((40, y, 180, y + 18), fill=(40, 90, 150), width=2) for y in range(36, 104, 14)],
            "border": lambda draw: draw.rectangle((8, 8, 212, 142), outline=(20, 20, 20), width=3),
            "edge_mark": lambda draw: draw.rectangle((0, 50, 18, 90), fill=(35, 35, 35)),
            "archival_mark": lambda draw: draw.rectangle((186, 116, 206, 136), fill=(45, 45, 45)),
            "colored_paper": None,
        }
        for name, draw_mark in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                process_dir = root / "processed"
                input_dir.mkdir()
                image = Image.new("RGB", (220, 150), (232, 224, 206)) if name == "colored_paper" else gradient_page()
                draw = ImageDraw.Draw(image)
                if draw_mark is not None:
                    draw_mark(draw)
                source = input_dir / f"private_illumination_protected_{name}.png"
                image.save(source)

                report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(level_illumination_gradient=True, workers=1),
                )
                audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
                audit_summary = json.loads(audit_summary_text)
                record = manifest["files"][0]

                self.assertFalse(record["illumination_gradient_levelled"], name)
                self.assertIn(
                    record["illumination_gradient_reason_code"],
                    {"protected_content", "not_uniform", "low_confidence"},
                )
                with Image.open(process_dir / "images" / source.name) as processed:
                    self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes(), name)
                self.assertEqual(audit_summary["counts"]["illumination_gradient_levelled_files"], 0)
                self.assertTrue(audit_summary["privacy"]["aggregate_only"])
                self.assertNotIn(f"private_illumination_protected_{name}", audit_summary_text)

    def test_fold_shadow_cleanup_preserves_foreground_marks_and_edge_content(self) -> None:
        cases = {
            "text": lambda draw: draw.rectangle((78, 58, 140, 64), fill=(30, 30, 30)),
            "header": lambda draw: draw.rectangle((72, 18, 148, 24), fill=(35, 35, 35)),
            "footer": lambda draw: draw.rectangle((72, 136, 148, 142), fill=(35, 35, 35)),
            "stamp": lambda draw: draw.ellipse((96, 48, 124, 76), outline=(190, 20, 20), width=3),
            "table_grid": lambda draw: [
                draw.line((52, y, 168, y), fill=(35, 35, 35), width=2) for y in (46, 78, 110)
            ],
            "underline": lambda draw: draw.line((76, 88, 144, 88), fill=(35, 35, 35), width=2),
            "page_number": lambda draw: draw.rectangle((104, 132, 116, 138), fill=(35, 35, 35)),
            "handwriting": lambda draw: draw.line((82, 44, 138, 92), fill=(45, 45, 45), width=2),
            "edge_content": lambda draw: draw.rectangle((4, 32, 18, 118), fill=(35, 35, 35)),
            "dense_photo_like": lambda draw: [
                draw.rectangle((x, y, x + 3, y + 3), fill=(82 + ((x * 7 + y * 5) % 92), 86, 96))
                for x in range(50, 168, 6)
                for y in range(34, 124, 6)
            ],
            "binding_holes": lambda draw: [
                draw.ellipse((7, y, 17, y + 10), fill=(24, 24, 24)) for y in (34, 72, 110)
            ],
            "water_stain": lambda draw: draw.ellipse((74, 42, 146, 118), fill=(198, 190, 166)),
            "original_fold_mark": lambda draw: draw.line((109, 14, 109, 146), fill=(148, 148, 144), width=1),
        }
        for name, draw_mark in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                output_dir = root / "reports"
                process_dir = root / "processed"
                input_dir.mkdir()
                image = Image.new("RGB", (220, 160), (242, 242, 238))
                draw = ImageDraw.Draw(image)
                draw.rectangle((105, 14, 113, 146), fill=(218, 218, 214))
                draw_mark(draw)
                source = input_dir / f"private_fold_protected_{name}.png"
                image.save(source)

                report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(lighten_fold_shadows=True, workers=1),
                )
                audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
                audit_summary = json.loads(audit_summary_text)

                with Image.open(process_dir / "images" / f"private_fold_protected_{name}.png") as processed:
                    self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes(), name)
                record = manifest["files"][0]
                self.assertFalse(record["fold_shadows_lightened"])
                self.assertNotEqual(record["fold_shadows_reason_code"], "applied_narrow_neutral_background_band")
                self.assertEqual(audit_summary["counts"]["fold_shadows_lightened_files"], 0)
                self.assertTrue(audit_summary["privacy"]["aggregate_only"])
                self.assertNotIn(f"private_fold_protected_{name}", audit_summary_text)

    def test_combined_change_guard_reverts_high_risk_stack_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_high_risk_combo.png"
            image = Image.new("RGB", (180, 120), (242, 242, 238))
            draw = ImageDraw.Draw(image)
            for y in range(26, 92, 16):
                draw.rectangle((34, y, 142, y + 4), fill=(58, 58, 58))
            image.save(source)
            source_bytes = source.read_bytes()

            def broad_background_change(current: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                changed = current.copy()
                changed.paste((225, 225, 222), (0, 0, 82, current.height))
                return processing_module.BackgroundStainLighteningResult(
                    changed,
                    True,
                    "background stains lightened: stable isolated stains on light paper",
                    220.0,
                    235.0,
                    10.0,
                    0.45,
                    0.45,
                )

            def broad_faded_text_change(current: Image.Image) -> processing_module.FadedTextEnhancementResult:
                changed = current.copy()
                changed.paste((214, 214, 211), (82, 0, 98, current.height))
                return processing_module.FadedTextEnhancementResult(
                    changed,
                    True,
                    "faded text enhancement applied: stable low-contrast neutral text on light paper",
                    12.0,
                    0.09,
                    0.09,
                )

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            with mock.patch(
                "archive_scan_qc.processing._lighten_background_stains_conservative",
                side_effect=broad_background_change,
            ), mock.patch(
                "archive_scan_qc.processing._enhance_faded_text_conservative",
                side_effect=broad_faded_text_change,
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(lighten_background_stains=True, enhance_faded_text=True, workers=1),
                )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["background_stains_lightened"])
            self.assertFalse(record["faded_text_enhanced"])
            self.assertEqual(audit["combination_quality_guard_action"], "reverted_to_source")
            self.assertEqual(
                audit["combination_quality_guard_reason_code"],
                "combined_change_too_large_reverted",
            )
            self.assertIn("combination_quality_guard_reverted_to_source", record["operations"])
            with Image.open(process_dir / "images" / "private_high_risk_combo.png") as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            self.assertEqual(audit_summary["counts"]["combination_quality_guard_reverted_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["combination_quality_guard"]["reason_code_distribution"][
                    "combined_change_too_large_reverted"
                ],
                1,
            )
            for forbidden in (
                "private_high_risk_combo",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_multi_worker_retouch_manifest_order_stays_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            for index in range(6):
                image = Image.new("RGB", (60, 50), "white")
                image.putpixel((10 + index, 10), (0, 0, 0))
                image.save(input_dir / f"A001_{index + 1:04d}.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir, workers=3))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(trim_dark_border=True, despeckle=True, workers=3),
            )

            self.assertEqual([record["source_relative_path"] for record in manifest["files"]], [item["relative_path"] for item in report["files"]])
            self.assertEqual(manifest["summary"]["performance"]["mode"], "parallel")

    def test_processing_plan_covers_candidates_without_derivatives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            plan_dir = root / "plan"
            input_dir.mkdir()
            _synthetic_text_page().rotate(2.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white").save(
                input_dir / "A001_skew.png"
            )
            _synthetic_dark_border_page().save(input_dir / "A001_border.png")
            gutter = Image.new("RGB", (240, 180), (236, 236, 236))
            gutter_draw = ImageDraw.Draw(gutter)
            gutter_draw.rectangle((10, 0, 239, 179), fill=(248, 248, 248))
            gutter_draw.rectangle((36, 32, 204, 148), outline=(60, 60, 60), width=2)
            gutter.save(input_dir / "A001_light_gutter.png")
            Image.new("RGB", (240, 180), "white").save(input_dir / "A001_clean.png")
            (input_dir / "A001_broken.png").write_text("not an image", encoding="utf-8")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            report_path = write_reports(report, output_dir)["json"]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "processing-plan",
                        "--report",
                        str(report_path),
                        "--input",
                        str(input_dir),
                        "--out",
                        str(plan_dir),
                        "--deskew",
                        "--trim-dark-border",
                        "--scanner-gutter-trim",
                        "--auto-crop",
                        "--despeckle",
                    ]
                )

            plan = json.loads((plan_dir / "processing_plan.json").read_text(encoding="utf-8"))
            rows = {record["source_relative_path"]: record for record in plan["files"]}
            self.assertEqual(exit_code, 0)
            self.assertTrue((plan_dir / "processing_plan.csv").exists())
            self.assertFalse((plan_dir / "images").exists())
            self.assertIn("Derivative images written: no", stdout.getvalue())
            self.assertTrue(plan["privacy"]["contains_paths"])
            self.assertTrue(plan["privacy"]["contains_hashes"])
            self.assertFalse(plan["privacy"]["contains_image_content"])
            self.assertTrue(rows["A001_skew.png"]["deskew_candidate"])
            self.assertTrue(rows["A001_border.png"]["dark_border_trim_candidate"])
            self.assertTrue(rows["A001_light_gutter.png"]["scanner_gutter_trim_candidate"])
            self.assertFalse(rows["A001_clean.png"]["deskew_candidate"])
            self.assertFalse(rows["A001_clean.png"]["dark_border_trim_candidate"])
            self.assertEqual(rows["A001_broken.png"]["status"], "unopenable")
            self.assertEqual(plan["summary"]["scanner_gutter_trim_candidates"], 1)
            self.assertEqual(plan["summary"]["total_files"], 5)
            self.assertEqual(plan["summary"]["unopenable_files"], 1)

    def test_processing_plan_artifacts_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            _synthetic_text_page().save(input_dir / "A001_clean.png")

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            first = build_processing_plan(report, input_dir, ProcessingOptions(auto_crop=True, deskew=True))
            second = build_processing_plan(report, input_dir, ProcessingOptions(auto_crop=True, deskew=True))
            self.assertEqual(first, second)

    def test_cli_process_out_writes_processing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--process-out",
                        str(process_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((process_dir / "processing_manifest.json").exists())
            self.assertTrue((process_dir / "images" / "A001_0001.jpg").exists())
            output = stdout.getvalue()
            self.assertIn("Scan elapsed:", output)
            self.assertIn("Scan workers:", output)
            self.assertIn("Scan files/min:", output)
            self.assertIn("Processing elapsed:", output)
            self.assertIn("Processing workers:", output)
            self.assertIn("Processing files/min:", output)

    def test_production_runner_writes_derivatives_summary_and_progress_without_modifying_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            source = input_dir / "A001_0001.jpg"
            Image.new("RGB", (48, 36), "white").save(source, dpi=(300, 300))
            original_sha = _sha256_for_test(source)

            summary = run_production_folder(
                ProductionRunConfig(
                    input_dir=input_dir,
                    derivative_output_dir=derivatives_dir,
                    metadata_output_dir=metadata_dir,
                    project_id="project",
                    batch_id="batch",
                    workers=1,
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    despeckle=True,
                )
            )

            self.assertEqual(summary["schema_version"], "scan-qc.production-run.v1")
            self.assertEqual(summary["status"], "finished")
            self.assertEqual(summary["status_label_zh"], "已完成")
            self.assertTrue(summary["ready_for_operator_handoff"])
            self.assertIn("处理后图片已生成", summary["operator_summary"]["message_zh"])
            self.assertEqual(summary["operator_summary"]["total_source_images"], 1)
            self.assertEqual(summary["operator_summary"]["derivative_images_ready"], 1)
            self.assertFalse(summary["source_images_modified"])
            self.assertEqual(summary["options"]["processing_mode"], "standard")
            self.assertEqual(summary["options"]["processing_mode_label_zh"], "标准优化")
            self.assertTrue(summary["options"]["auto_crop"])
            self.assertTrue(summary["options"]["deskew"])
            self.assertTrue(summary["options"]["trim_dark_border"])
            self.assertTrue(summary["options"]["despeckle"])
            self.assertEqual(summary["options"]["despeckle_backend"], "fallback")
            self.assertEqual(_sha256_for_test(source), original_sha)
            self.assertTrue((derivatives_dir / "images" / "A001_0001.jpg").exists())
            self.assertTrue((derivatives_dir / "processing_manifest.json").exists())
            self.assertTrue((metadata_dir / "production_run_summary.json").exists())
            self.assertTrue((metadata_dir / "production_run_progress.json").exists())
            self.assertTrue((metadata_dir / "admin_reports" / "scan_qc_report.json").exists())
            progress = json.loads((metadata_dir / "production_run_progress.json").read_text(encoding="utf-8"))
            self.assertEqual(progress["state"], "finished")
            self.assertEqual(progress["state_label_zh"], "已完成")
            self.assertEqual(progress["steps"][0]["label"], "检查扫描图片")
            self.assertEqual(progress["completed_steps"], 3)
            expected_stages = [
                ("scan", "检查扫描图片", "completed"),
                ("process", "生成处理后图片", "completed"),
                ("summarize", "整理处理结果", "completed"),
            ]
            for timing_payload in (summary["stage_timings"], progress["stage_timings"]):
                self.assertEqual(timing_payload["schema_version"], "scan-qc.production-stage-timings.v1")
                self.assertTrue(timing_payload["aggregate_only"])
                self.assertEqual(
                    [(stage["id"], stage["label_zh"], stage["status"]) for stage in timing_payload["stages"]],
                    expected_stages,
                )
                for stage in timing_payload["stages"]:
                    self.assertIsInstance(stage["elapsed_seconds"], float)
                    self.assertGreaterEqual(stage["elapsed_seconds"], 0.0)

    def test_cli_production_run_is_operator_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "production-run",
                        "--input",
                        str(input_dir),
                        "--derivatives-out",
                        str(derivatives_dir),
                        "--metadata-out",
                        str(metadata_dir),
                        "--workers",
                        "1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("生产状态: 已完成 (finished)", output)
            self.assertIn("处理后图片文件夹:", output)
            self.assertIn("原图是否被修改: 否", output)
            saved = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["operator_summary"]["files_needing_attention"], 0)
            self.assertEqual(saved["operator_summary"]["message"], saved["operator_summary"]["message_zh"])

    def test_cli_production_rehearsal_generates_synthetic_workbench_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "rehearsal"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["production-rehearsal", "--root", str(root), "--workers", "1"])

            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("本机生产演练已生成。", output)
            self.assertIn("下一步:", output)
            self.assertIn("production-rehearsal --launch-workbench", output)
            self.assertIn("工作台会自动带入演练文件夹", output)
            self.assertIn("只使用合成图片", output)
            self.assertNotIn("JSON", output)
            self.assertNotIn("schema", output)
            self.assertNotIn("点击加载本机状态", output)
            self.assertNotIn("私有图片文件夹", output)

            input_dir = root / "synthetic_input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            self.assertEqual(len(list(input_dir.glob("SYNTHETIC_BATCH_*.png"))), 3)
            self.assertTrue((derivatives_dir / "images" / "SYNTHETIC_BATCH_0001_clean.png").exists())
            self.assertTrue((metadata_dir / "production_run_summary.json").exists())
            self.assertTrue((metadata_dir / "production_run_progress.json").exists())
            self.assertTrue((metadata_dir / "processing_review_package.json").exists())
            self.assertTrue((metadata_dir / "production_review_queue.json").exists())

            summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
            progress = json.loads((metadata_dir / "production_run_progress.json").read_text(encoding="utf-8"))
            queue = json.loads((metadata_dir / "production_review_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], "scan-qc.production-run.v1")
            self.assertEqual(progress["schema_version"], "scan-qc.production-run-progress.v1")
            self.assertEqual(queue["schema_version"], "scan-qc.production-review-queue.v1")
            self.assertEqual(summary["operator_summary"]["total_source_images"], 3)
            self.assertGreaterEqual(queue["summary"]["total_items"], 1)
            self.assertTrue(queue["summary"]["ready_for_operator_review"])
            self.assertTrue(queue["privacy"]["local_only"])
            self.assertFalse(queue["privacy"]["contains_image_bytes"])

    def test_local_production_workbench_server_prefills_rehearsal_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "rehearsal"
            rehearsal = run_production_rehearsal(
                ProductionRehearsalConfig(root_dir=root, workers=1)
            )

            server = make_server(
                "127.0.0.1",
                0,
                input_dir=Path(rehearsal["input_dir"]),
                derivatives_dir=Path(rehearsal["derivatives_dir"]),
                metadata_dir=Path(rehearsal["metadata_dir"]),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/status", timeout=5) as response:
                    status = json.loads(response.read().decode("utf-8"))

                self.assertTrue(status["configured"])
                self.assertEqual(status["folders"]["input"], rehearsal["input_dir"])
                self.assertEqual(status["folders"]["derivatives"], rehearsal["derivatives_dir"])
                self.assertEqual(status["folders"]["metadata"], rehearsal["metadata_dir"])
                self.assertEqual(status["summary"]["schema_version"], "scan-qc.production-run.v1")
                self.assertEqual(status["queue"]["schema_version"], "scan-qc.production-review-queue.v1")
                self.assertGreaterEqual(status["queue"]["summary"]["total_items"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_local_production_workbench_controller_runs_folder_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            input_dir.mkdir()
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            controller = WorkbenchController()
            configured = controller.configure(input_dir, derivatives_dir)
            self.assertTrue(configured["configured"])
            self.assertEqual(Path(configured["folders"]["metadata"]).name, DEFAULT_METADATA_DIRNAME)

            controller.start()
            deadline = time.time() + 10
            status = controller.status()
            while status["running"] and time.time() < deadline:
                time.sleep(0.05)
                status = controller.status()

            self.assertFalse(status["running"])
            self.assertIsNone(status["last_error_zh"])
            self.assertEqual(status["summary"]["status"], "finished")
            self.assertEqual(status["processing_mode"]["id"], DEFAULT_PROCESSING_MODE)
            self.assertEqual(status["summary"]["options"]["processing_mode"], DEFAULT_PROCESSING_MODE)
            self.assertTrue(status["summary"]["options"]["auto_crop"])
            self.assertTrue(status["summary"]["options"]["deskew"])
            self.assertTrue(status["summary"]["options"]["trim_dark_border"])
            self.assertTrue(status["summary"]["options"]["despeckle"])
            self.assertEqual(status["progress"]["state"], "finished")
            self.assertEqual(status["queue"]["schema_version"], "scan-qc.production-review-queue.v1")
            self.assertTrue((derivatives_dir / "images" / "A001_0001.jpg").exists())
            self.assertTrue((derivatives_dir / DEFAULT_METADATA_DIRNAME / "production_run_summary.json").exists())

    def test_local_production_workbench_processing_modes_handoff_existing_options(self) -> None:
        expected_modes = {
            "standard": {
                "label_zh": "标准优化",
                "auto_crop": True,
                "deskew": True,
                "trim_dark_border": True,
                "despeckle": True,
            },
            "light": {
                "label_zh": "轻度优化",
                "auto_crop": True,
                "deskew": False,
                "trim_dark_border": False,
                "despeckle": False,
            },
            "qc_only": {
                "label_zh": "只质检不修图",
                "auto_crop": False,
                "deskew": False,
                "trim_dark_border": False,
                "despeckle": False,
            },
        }
        for mode, expected in expected_modes.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "input"
                derivatives_dir = root / "derivatives"
                input_dir.mkdir()
                Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

                controller = WorkbenchController()
                configured = controller.configure(input_dir, derivatives_dir, processing_mode=mode)
                self.assertEqual(configured["processing_mode"]["id"], mode)
                self.assertEqual(configured["processing_mode"]["label_zh"], expected["label_zh"])

                with mock.patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                    run_mock.return_value = {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "artifacts": {},
                        "counts": {"total_files": 1, "processed_files": 1, "resumed_files": 0, "failed_files": 0, "retry_list_files": 0},
                    }
                    controller.start()
                    deadline = time.time() + 10
                    status = controller.status()
                    while status["running"] and time.time() < deadline:
                        time.sleep(0.05)
                        status = controller.status()

                self.assertFalse(status["running"])
                run_mock.assert_called_once()
                config = run_mock.call_args.args[0]
                self.assertEqual(config.processing_mode, mode)
                self.assertEqual(config.auto_crop, expected["auto_crop"])
                self.assertEqual(config.deskew, expected["deskew"])
                self.assertEqual(config.trim_dark_border, expected["trim_dark_border"])
                self.assertEqual(config.despeckle, expected["despeckle"])
                self.assertEqual(config.despeckle_backend, "fallback")
                self.assertTrue(config.resume_processing)
                self.assertTrue(config.reuse_scan_measurements)

    def test_local_production_workbench_rejects_unknown_processing_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            controller = WorkbenchController()

            with self.assertRaisesRegex(ValueError, "处理方式不正确"):
                controller.configure(input_dir, root / "derivatives", processing_mode="experimental")

            self.assertIsNone(controller.input_dir)

    def test_local_production_workbench_status_exposes_aggregate_recovery_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            metadata_dir.mkdir(parents=True)
            (metadata_dir / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "blocked",
                        "operator_summary": {
                            "total_source_images": 5,
                            "derivative_images_ready": 3,
                            "files_needing_attention": 2,
                        },
                        "counts": {
                            "total_files": 5,
                            "processed_files": 3,
                            "resumed_files": 0,
                            "failed_files": 2,
                            "retry_list_files": 2,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            status = controller.status()

            guidance = status["recovery_guidance"]
            self.assertTrue(guidance["aggregate_only"])
            self.assertEqual(guidance["kind"], "processing_failed_retryable")
            self.assertEqual(guidance["failed_files"], 2)
            self.assertEqual(guidance["retryable_files"], 2)
            self.assertEqual(guidance["derivative_images_ready"], 3)
            self.assertIn("磁盘空间", "".join(guidance["next_steps_zh"]))
            self.assertNotIn(str(input_dir), json.dumps(guidance, ensure_ascii=False))
            self.assertNotIn(".jpg", json.dumps(guidance, ensure_ascii=False))

    def test_local_production_workbench_status_is_conservative_when_retry_scope_is_unknown(self) -> None:
        private_values = [
            "/Users/private/archive/input",
            "Secret_Case_0001.tif",
            "f" * 64,
            "OCR: 张三身份证 110101199001010011",
            "data:image/png;base64",
            "Traceback File worker.py line 42",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            metadata_dir.mkdir(parents=True)
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            (metadata_dir / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "blocked",
                        "operator_summary": {"message_zh": " ".join(private_values)},
                        "counts": {
                            "total_files": 3,
                            "processed_files": 1,
                            "failed_files": "unknown",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            status = controller.status()

            guidance = status["recovery_guidance"]
            guidance_text = json.dumps(guidance, ensure_ascii=False, sort_keys=True)
            self.assertEqual(guidance["kind"], "processing_retry_scope_unknown")
            self.assertFalse(guidance["retry_scope_safe"])
            self.assertIn("不能安全判断本批应重试哪些图片", guidance["message_zh"])
            self.assertIn("不会误报完成", guidance["message_zh"])
            self.assertIn("不会编造重试数量", guidance["message_zh"])
            self.assertEqual(guidance["known_counts"], {"total_files": 3, "processed_files": 1})
            self.assertNotIn("no_remaining_work", guidance_text)
            for private_value in private_values:
                self.assertNotIn(private_value, guidance_text)
            self.assertNotIn(".tif", guidance_text)
            self.assertNotIn("sha256", guidance_text.lower())
            self.assertNotIn("ocr", guidance_text.lower())
            self.assertNotIn("traceback", guidance_text.lower())

    def test_local_production_workbench_status_sanitizes_private_error_text_from_summary(self) -> None:
        private_values = [
            "/Users/private/archive/secret-root",
            "Confidential_Case_0007.tif",
            "a" * 64,
            "PRIVATE_OCR: 张三身份证 110101199001010011",
            'Traceback File "worker.py", line 42, in run RuntimeError',
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            metadata_dir.mkdir(parents=True)
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            raw_private_message = " ".join(private_values)
            (metadata_dir / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "blocked",
                        "operator_summary": {
                            "message": raw_private_message,
                            "message_zh": raw_private_message,
                            "total_source_images": 1,
                            "openable_source_images": 1,
                            "derivative_images_ready": 0,
                            "files_needing_attention": 1,
                        },
                        "recovery_guidance": {
                            "schema_version": "scan-qc.local-recovery-guidance.v1",
                            "aggregate_only": True,
                            "kind": "processing_failed_admin",
                            "title_zh": f"RuntimeError {private_values[1]}",
                            "message_zh": raw_private_message,
                            "next_steps_zh": [raw_private_message],
                            "failed_files": 1,
                            "retryable_files": 0,
                            "derivative_images_ready": 0,
                            "total_files": 1,
                        },
                        "counts": {
                            "total_files": 1,
                            "openable_files": 1,
                            "processed_files": 0,
                            "resumed_files": 0,
                            "failed_files": 1,
                            "retry_list_files": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            status = controller.status()
            public_text = json.dumps(
                {
                    "last_error_zh": status["last_error_zh"],
                    "operator_summary": status["summary"]["operator_summary"],
                    "recovery_guidance": status["recovery_guidance"],
                },
                ensure_ascii=False,
            )

            for private_value in private_values:
                self.assertNotIn(private_value, public_text)
            self.assertIn("其他异常：本批次没有正常启动，请交管理员处理。", public_text)
            self.assertEqual(status["recovery_guidance"]["title_zh"], "处理没有正常完成")

    def test_local_production_workbench_status_sanitizes_top_level_operator_messages(self) -> None:
        private_values = [
            "/Volumes/Archive/SecretRoot",
            "Hidden_Batch_0099.png",
            "c" * 64,
            "OCR snippet: private row text",
            'Traceback File "private_worker.py", line 99, in run RuntimeError',
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            metadata_dir.mkdir(parents=True)
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            raw_private_message = " ".join(private_values)
            (metadata_dir / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "blocked",
                        "message": raw_private_message,
                        "message_zh": raw_private_message,
                        "operator_message_zh": raw_private_message,
                        "last_error_zh": raw_private_message,
                        "operator_summary": {
                            "total_source_images": 1,
                            "openable_source_images": 1,
                            "derivative_images_ready": 0,
                            "files_needing_attention": 1,
                        },
                        "counts": {
                            "total_files": 1,
                            "openable_files": 1,
                            "processed_files": 0,
                            "resumed_files": 0,
                            "failed_files": 1,
                            "retry_list_files": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            status = controller.status()
            public_text = json.dumps(status["summary"], ensure_ascii=False)

            for private_value in private_values:
                self.assertNotIn(private_value, public_text)
            self.assertEqual(status["summary"]["operator_message_zh"], "其他异常：本批次没有正常启动，请交管理员处理。")

    def test_local_production_workbench_runtime_error_keeps_admin_context_out_of_operator_status(self) -> None:
        private_values = [
            "/Users/private/archive/secret-root",
            "Confidential_Case_0007.tif",
            "b" * 64,
            "PRIVATE_OCR: 档案正文片段",
            "Traceback RuntimeError numpy stack",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            with mock.patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                run_mock.side_effect = RuntimeError(" ".join(private_values))
                controller.start()
                deadline = time.time() + 10
                status = controller.status()
                while status["running"] and time.time() < deadline:
                    time.sleep(0.05)
                    status = controller.status()

            public_text = json.dumps(
                {
                    "last_error_zh": status["last_error_zh"],
                    "recovery_guidance": status["recovery_guidance"],
                },
                ensure_ascii=False,
            )
            for private_value in private_values:
                self.assertNotIn(private_value, public_text)
            self.assertEqual(status["last_error_zh"], "其他异常：本批次没有正常启动，请交管理员处理。")
            maintenance_log = metadata_dir / "local_workbench_maintenance_errors.jsonl"
            self.assertTrue(maintenance_log.exists())
            maintenance_text = maintenance_log.read_text(encoding="utf-8")
            self.assertIn("RuntimeError", maintenance_text)
            self.assertIn("startup_or_processing_failed", maintenance_text)
            for private_value in private_values:
                self.assertNotIn(private_value, maintenance_text)

    def test_local_production_workbench_retry_reuses_saved_folder_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            metadata_dir.mkdir(parents=True)
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            (metadata_dir / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "blocked",
                        "counts": {
                            "total_files": 1,
                            "processed_files": 0,
                            "resumed_files": 0,
                            "failed_files": 1,
                            "retry_list_files": 1,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            with mock.patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                run_mock.return_value = {
                    "schema_version": "scan-qc.production-run.v1",
                    "status": "finished",
                    "artifacts": {},
                    "counts": {"total_files": 1, "processed_files": 1, "resumed_files": 0, "failed_files": 0, "retry_list_files": 0},
                }

                controller.retry()
                deadline = time.time() + 10
                status = controller.status()
                while status["running"] and time.time() < deadline:
                    time.sleep(0.05)
                    status = controller.status()

            self.assertFalse(status["running"])
            self.assertIsNone(status["last_error_zh"])
            run_mock.assert_called_once()
            config = run_mock.call_args.args[0]
            self.assertEqual(config.input_dir, input_dir.resolve())
            self.assertEqual(config.derivative_output_dir, derivatives_dir.resolve())
            self.assertEqual(config.metadata_output_dir, metadata_dir.resolve())
            self.assertTrue(config.resume_processing)
            self.assertTrue(config.reuse_scan_measurements)

    def test_local_production_workbench_retry_rejects_non_retryable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            metadata_dir.mkdir(parents=True)
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            (metadata_dir / "production_run_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "blocked",
                        "counts": {
                            "total_files": 1,
                            "processed_files": 0,
                            "failed_files": 1,
                            "retry_list_files": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            with mock.patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                with self.assertRaisesRegex(ValueError, "不能直接重试"):
                    controller.retry()
            run_mock.assert_not_called()

    def test_local_production_workbench_rejects_empty_configure_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            controller = WorkbenchController()

            with self.assertRaisesRegex(ValueError, "扫描原图文件夹|两个文件夹"):
                controller.configure(Path(""), root / "derivatives")
            with self.assertRaisesRegex(ValueError, "处理后输出文件夹|两个文件夹"):
                controller.configure(input_dir, Path(""))

            self.assertIsNone(controller.input_dir)
            self.assertFalse((Path.cwd() / DEFAULT_METADATA_DIRNAME).exists())

    def test_local_production_workbench_configure_rejects_unsafe_output_before_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_inside_input = input_dir / "derivatives"
            metadata_inside_input = input_dir / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()

            with self.assertRaisesRegex(ValueError, "处理后输出文件夹不能.*原图文件夹"):
                controller.configure(input_dir, output_inside_input)

            self.assertFalse(output_inside_input.exists())
            self.assertIsNone(controller.input_dir)

            safe_output = root / "derivatives"
            with self.assertRaisesRegex(ValueError, "本机状态文件夹不能放在扫描原图文件夹里面"):
                controller.configure(input_dir, safe_output, metadata_inside_input)

            self.assertFalse(safe_output.exists())
            self.assertFalse(metadata_inside_input.exists())
            self.assertIsNone(controller.input_dir)

    def test_local_production_workbench_configure_api_keeps_unsafe_folder_guidance_private_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            private_root = root / "Users" / "private" / "archive"
            input_dir = private_root / "Confidential_Batch_0099"
            output_inside_input = input_dir / "derivatives"
            metadata_inside_input = input_dir / "metadata"
            safe_output = private_root / "safe-output"
            input_dir.mkdir(parents=True)
            server = make_server("127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                cases = [
                    (
                        {
                            "input_dir": str(input_dir),
                            "derivatives_dir": str(output_inside_input),
                        },
                        "处理后输出文件夹不能和扫描原图文件夹相同，也不能放在原图文件夹里面。",
                    ),
                    (
                        {
                            "input_dir": str(input_dir),
                            "derivatives_dir": str(safe_output),
                            "metadata_dir": str(metadata_inside_input),
                        },
                        "本机状态文件夹不能放在扫描原图文件夹里面，处理没有启动。",
                    ),
                ]
                for payload, expected_error in cases:
                    with self.subTest(expected_error=expected_error):
                        request = urllib.request.Request(
                            f"{base_url}/api/configure",
                            data=json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with self.assertRaises(urllib.error.HTTPError) as raised:
                            urllib.request.urlopen(request, timeout=5)
                        self.assertEqual(raised.exception.code, 400)
                        response_payload = json.loads(raised.exception.read().decode("utf-8"))
                        raised.exception.close()
                        response_text = json.dumps(response_payload, ensure_ascii=False)

                        self.assertEqual(response_payload["error_zh"], expected_error)
                        self.assertNotIn(str(input_dir), response_text)
                        self.assertNotIn("Confidential_Batch_0099", response_text)
                        self.assertNotIn(str(output_inside_input), response_text)
                        self.assertNotIn(str(metadata_inside_input), response_text)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_local_production_workbench_pick_folder_api_returns_native_path_without_upload(self) -> None:
        server = make_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with mock.patch.object(local_workbench_module, "_pick_native_folder", return_value=r"C:\Users\PS\batch input") as picker:
                request = urllib.request.Request(
                    f"{base_url}/api/pick-folder",
                    data=json.dumps({"kind": "input"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))

            picker.assert_called_once_with("选择本批次扫描原图文件夹")
            self.assertEqual(payload["path"], r"C:\Users\PS\batch input")
            self.assertFalse(payload["cancelled"])
            self.assertIn("已选择文件夹", payload["message_zh"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_local_production_workbench_preflight_rejects_empty_input_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            input_dir.mkdir()

            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)

            with self.assertRaisesRegex(ValueError, "扫描原图文件夹里没有文件"):
                controller.start()

            status = controller.status()
            guidance = status["recovery_guidance"]
            self.assertFalse(status["running"])
            self.assertIsNone(status["summary"])
            self.assertEqual(guidance["kind"], "input_folder_empty")
            self.assertTrue(guidance["aggregate_only"])
            self.assertIn("放入原图文件夹", "".join(guidance["next_steps_zh"]))
            self.assertNotIn(str(input_dir), json.dumps(guidance, ensure_ascii=False))

    def test_local_production_workbench_preflight_rejects_unreadable_input_without_private_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            input_dir.mkdir()
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)

            original_mode = input_dir.stat().st_mode
            input_dir.chmod(0)
            try:
                with self.assertRaisesRegex(ValueError, "扫描原图文件夹现在不能读取"):
                    controller.start()

                status = controller.status()
                guidance = status["recovery_guidance"]
                self.assertFalse(status["running"])
                self.assertIsNone(status["summary"])
                self.assertEqual(guidance["kind"], "input_folder_unreadable")
                self.assertTrue(guidance["aggregate_only"])
                self.assertIn("读取权限", "".join(guidance["next_steps_zh"]))
                self.assertNotIn(str(input_dir), json.dumps(guidance, ensure_ascii=False))
            finally:
                input_dir.chmod(original_mode)

    def test_local_production_workbench_preflight_rejects_unusable_output_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            input_dir.mkdir()
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            shutil.rmtree(derivatives_dir)

            with self.assertRaisesRegex(ValueError, "处理后输出文件夹不存在"):
                controller.start()

            status = controller.status()
            guidance = status["recovery_guidance"]
            self.assertFalse(status["running"])
            self.assertIsNone(status["summary"])
            self.assertEqual(guidance["kind"], "output_folder_unusable")
            self.assertIn("可以写入", "".join(guidance["next_steps_zh"]))
            self.assertNotIn(str(derivatives_dir), json.dumps(guidance, ensure_ascii=False))

    def test_local_production_workbench_preflight_write_probe_does_not_overwrite_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            input_dir.mkdir()
            derivatives_dir.mkdir()
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            fixed_probe = derivatives_dir / ".scan_qc_preflight_write_test"
            fixed_probe.write_text("operator file\n", encoding="utf-8")

            controller = WorkbenchController()
            controller.configure(input_dir, derivatives_dir)
            with mock.patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                run_mock.return_value = {
                    "status": "finished",
                    "artifacts": {},
                    "counts": {"total_files": 1, "processed_files": 1, "resumed_files": 0},
                }
                controller.start()
                deadline = time.time() + 10
                status = controller.status()
                while status["running"] and time.time() < deadline:
                    time.sleep(0.05)
                    status = controller.status()
                self.assertFalse(status["running"])
                self.assertIsNone(status["last_error_zh"])

            self.assertEqual(fixed_probe.read_text(encoding="utf-8"), "operator file\n")

    def test_local_production_workbench_start_api_returns_preflight_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            input_dir.mkdir()
            server = make_server("127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                configure_request = urllib.request.Request(
                    f"{base_url}/api/configure",
                    data=json.dumps({"input_dir": str(input_dir), "derivatives_dir": str(derivatives_dir)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(configure_request, timeout=5):
                    pass

                start_request = urllib.request.Request(
                    f"{base_url}/api/start",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(start_request, timeout=5)
                self.assertEqual(raised.exception.code, 400)
                payload = json.loads(raised.exception.read().decode("utf-8"))
                raised.exception.close()

                self.assertEqual(payload["preflight_guidance"]["kind"], "input_folder_empty")
                self.assertEqual(payload["recovery_guidance"]["schema_version"], "scan-qc.local-folder-preflight.v1")
                self.assertIn("处理没有启动", payload["error_zh"])
                self.assertNotIn(str(input_dir), json.dumps(payload, ensure_ascii=False))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_local_production_workbench_preview_route_serves_only_local_id_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            metadata_dir.mkdir(parents=True)
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            (metadata_dir / "production_review_queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-review-queue.v1",
                        "items": [
                            {
                                "local_id": "PRQ000001",
                                "relative_path": "A001_0001.jpg",
                                "severity": "P1",
                                "reason_zh": "需要人工确认。",
                                "suggested_action": "pass",
                                "sensitivity": {"local_only": True},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            server = make_server("127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                configure_request = urllib.request.Request(
                    f"{base_url}/api/configure",
                    data=json.dumps({"input_dir": str(input_dir), "derivatives_dir": str(derivatives_dir)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(configure_request, timeout=5) as response:
                    configured = json.loads(response.read().decode("utf-8"))
                self.assertTrue(configured["configured"])

                with urllib.request.urlopen(f"{base_url}/api/preview/PRQ000001", timeout=5) as response:
                    body = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    preview_source = response.headers.get("X-Preview-Source", "")
                self.assertIn(content_type, {"image/jpeg", "image/jpg"})
                self.assertEqual(preview_source, "original_fallback")
                self.assertTrue(body.startswith(b"\xff\xd8"))

                with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                    status = json.loads(response.read().decode("utf-8"))
                self.assertEqual(status["queue"]["items"][0]["preview_source"], "original_fallback")

                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(f"{base_url}/api/preview/../A001_0001.jpg", timeout=5)
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_local_production_workbench_finish_route_saves_review_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            derivatives_dir = root / "derivatives"
            metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
            input_dir.mkdir()
            metadata_dir.mkdir(parents=True)
            Image.new("RGB", (48, 36), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            server = make_server("127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                configure_request = urllib.request.Request(
                    f"{base_url}/api/configure",
                    data=json.dumps({"input_dir": str(input_dir), "derivatives_dir": str(derivatives_dir)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(configure_request, timeout=5) as response:
                    configured = json.loads(response.read().decode("utf-8"))
                self.assertTrue(configured["configured"])

                decision_summary = {
                    "schema": "scan-qc-review-decisions.local.v1",
                    "source_type": "production_workbench",
                    "source_target_count": 2,
                    "generated_in_browser": True,
                    "privacy": {"summary_only": True},
                    "aggregate_counts": {
                        "review_completion": {
                            "total": 2,
                            "reviewed": 2,
                            "pending": 0,
                            "complete": True,
                        }
                    },
                    "review_counts": {
                        "pending": 0,
                        "accepted_issue": 1,
                        "false_positive": 1,
                        "fixed_externally": 0,
                        "needs_rescan": 0,
                        "blocked": 0,
                    },
                    "reviewed_targets": 2,
                    "decisions": [
                        {"scope": "production_review_queue", "local_id": "PRQ000001", "decision": "accepted_issue"},
                        {"scope": "production_review_queue", "local_id": "PRQ000002", "decision": "false_positive"},
                    ],
                }
                finish_request = urllib.request.Request(
                    f"{base_url}/api/finish-decisions",
                    data=json.dumps(decision_summary).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(finish_request, timeout=5) as response:
                    finished = json.loads(response.read().decode("utf-8"))

                self.assertTrue(finished["finished"])
                self.assertEqual(finished["decision_summary"]["total_decisions"], 2)
                saved_summary_path = metadata_dir / "scan-qc-review-decisions.summary.json"
                saved_verification_path = metadata_dir / "review_decision_verification_summary.json"
                self.assertTrue(saved_summary_path.exists())
                self.assertTrue(saved_verification_path.exists())
                saved_verification = json.loads(saved_verification_path.read_text(encoding="utf-8"))
                self.assertEqual(saved_verification["status"], "pass")
                self.assertEqual(saved_verification["privacy"]["status"], "pass")
                saved_raw = saved_summary_path.read_text(encoding="utf-8") + saved_verification_path.read_text(encoding="utf-8")
                self.assertNotIn("A001_0001.jpg", saved_raw)
                self.assertNotIn("sha256", saved_raw.lower())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_cli_production_workbench_rejects_non_loopback_host(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["production-workbench", "--host", "0.0.0.0", "--no-open"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("local-only", stderr.getvalue())

    def test_cli_rejects_invalid_workers_without_writing_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            for value in ["0", "-1", "abc"]:
                with self.assertRaises(SystemExit) as raised:
                    main(["--input", str(input_dir), "--out", str(output_dir), "--workers", value])
                self.assertEqual(raised.exception.code, 2)

            self.assertFalse(output_dir.exists())

    def test_cli_auto_crop_requires_process_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            with self.assertRaises(SystemExit) as raised:
                main(["--input", str(input_dir), "--out", str(output_dir), "--auto-crop"])

            self.assertEqual(raised.exception.code, 2)

    def test_cli_deskew_requires_process_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            with self.assertRaises(SystemExit) as raised:
                main(["--input", str(input_dir), "--out", str(output_dir), "--deskew"])

            self.assertEqual(raised.exception.code, 2)

    def test_cli_new_retouch_options_require_process_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            for option in [
                "--trim-dark-border",
                "--scanner-gutter-trim",
                "--despeckle",
                "--normalize-tones",
                "--lighten-edge-shadow",
                "--lighten-background-stains",
                "--lighten-fold-shadows",
                "--level-illumination-gradient",
                "--lighten-scanlines",
                "--enhance-faded-text",
                "--sharpen-text-edges",
            ]:
                with self.assertRaises(SystemExit) as raised:
                    main(["--input", str(input_dir), "--out", str(output_dir), option])
                self.assertEqual(raised.exception.code, 2)

    def test_preflight_success_writes_privacy_safe_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            manifest_csv = root / "manifest.csv"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path\nA001_0001.jpg\n", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "preflight",
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--process-out",
                        str(process_dir),
                        "--auto-crop",
                        "--workers",
                        "1",
                        "--project",
                        "p1",
                        "--batch",
                        "b1",
                        "--manifest-csv",
                        str(manifest_csv),
                    ]
                )

            report_path = output_dir / "preflight_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertTrue(report_path.exists())
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["input_summary"]["candidate_file_count"], 1)
            self.assertEqual(report["manifest"]["missing_count"], 0)
            self.assertEqual(report["manifest"]["unexpected_count"], 0)
            self.assertFalse(report["privacy"]["contains_file_list"])
            self.assertIn("Preflight status: pass", stdout.getvalue())

    def test_preflight_processing_flag_without_process_out_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["preflight", "--input", str(input_dir), "--out", str(output_dir), "--deskew"])

            report = json.loads((output_dir / "preflight_report.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(report["status"], "fail")
            self.assertIn("process_output_required", {error["code"] for error in report["errors"]})

    def test_preflight_invalid_rules_profile_writes_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            invalid_profile = root / "invalid.json"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "PRIVATE_PRESENT.jpg", dpi=(300, 300))
            invalid_profile.write_text('{"name": ""}', encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "preflight",
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_dir),
                        "--rules-profile",
                        str(invalid_profile),
                    ]
                )

            report_path = output_dir / "preflight_report.json"
            report_text = report_path.read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(exit_code, 1)
            self.assertTrue(report_path.exists())
            self.assertEqual(report["status"], "fail")
            self.assertFalse(report["configuration"]["rules_profile"]["loaded"])
            self.assertTrue(report["configuration"]["rules_profile"]["provided"])
            self.assertIn("rules_profile_invalid", {error["code"] for error in report["errors"]})
            self.assertNotIn("PRIVATE_PRESENT.jpg", report_text)
            self.assertNotIn(str(input_dir), report_text)
            self.assertNotIn("sha256", report_text.lower())
            self.assertFalse(report["privacy"]["contains_file_list"])
            self.assertFalse(report["privacy"]["contains_hashes"])
            self.assertFalse(report["privacy"]["contains_thumbnails"])
            self.assertFalse(report["privacy"]["contains_image_content"])

    def test_preflight_rejects_illegal_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            manifest_csv = root / "manifest.csv"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            manifest_csv.write_text(
                "relative_path\n"
                " \n"
                "/private/A001_0001.jpg\n"
                "../escape.jpg\n"
                "nested/../escape.jpg\n",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["preflight", "--input", str(input_dir), "--out", str(output_dir), "--manifest-csv", str(manifest_csv)])

            report = json.loads((output_dir / "preflight_report.json").read_text(encoding="utf-8"))
            codes = {error["code"] for error in report["errors"]}
            self.assertEqual(exit_code, 1)
            self.assertIn("manifest_empty_paths", codes)
            self.assertIn("manifest_absolute_paths", codes)
            self.assertIn("manifest_parent_escape_paths", codes)
            self.assertEqual(report["manifest"]["empty_path_count"], 1)
            self.assertEqual(report["manifest"]["absolute_path_count"], 1)
            self.assertEqual(report["manifest"]["parent_escape_count"], 2)

    def test_preflight_manifest_missing_unexpected_are_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            manifest_csv = root / "manifest.csv"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "PRIVATE_PRESENT.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path\nPRIVATE_MISSING.jpg\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["preflight", "--input", str(input_dir), "--out", str(output_dir), "--manifest-csv", str(manifest_csv)])

            report_text = (output_dir / "preflight_report.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(exit_code, 1)
            self.assertEqual(report["manifest"]["missing_count"], 1)
            self.assertEqual(report["manifest"]["unexpected_count"], 1)
            self.assertNotIn("PRIVATE_PRESENT.jpg", report_text)
            self.assertNotIn("PRIVATE_MISSING.jpg", report_text)
            self.assertNotIn("sha256", report_text.lower())
            self.assertFalse(report["privacy"]["contains_hashes"])
            self.assertFalse(report["privacy"]["contains_thumbnails"])

    def test_manifest_sequence_valid_manifest_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            manifest_csv = root / "manifest.csv"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (32, 24), "lightgray").save(input_dir / "A001_0002.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path,sequence\nA001_0001.jpg,1\nA001_0002.jpg,2\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["--input", str(input_dir), "--out", str(output_dir), "--manifest-csv", str(manifest_csv), "--workers", "1"])

            report = json.loads((output_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
            files_csv = (output_dir / "scan_qc_files.csv").read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["summary"]["manifest_sequence_entry_count"], 2)
            self.assertEqual(report["summary"]["manifest_sequence_invalid_count"], 0)
            self.assertEqual(report["summary"]["manifest_sequence_duplicate_count"], 0)
            self.assertEqual(report["summary"]["manifest_sequence_order_mismatch_count"], 0)
            self.assertEqual(report["files"][0]["manifest_sequence"], 1)
            self.assertIn("manifest_sequence", files_csv)

    def test_manifest_duplicate_sequence_reports_p0_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            manifest_csv = root / "manifest.csv"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (32, 24), "lightgray").save(input_dir / "A001_0002.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path,page_sequence\nA001_0001.jpg,1\nA001_0002.jpg,1\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["--input", str(input_dir), "--out", str(output_dir), "--manifest-csv", str(manifest_csv), "--workers", "1"])

            report = json.loads((output_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(report["summary"]["manifest_sequence_duplicate_count"], 1)
            self.assertEqual(_rule_count(report, "manifest_duplicate_sequence"), 2)
            self.assertEqual(report["rule_catalog"]["manifest_duplicate_sequence"]["default_severity"], "P0")

    def test_manifest_invalid_sequence_reports_p1_finding_and_preflight_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            preflight_dir = root / "preflight"
            manifest_csv = root / "manifest.csv"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            manifest_csv.write_text("relative_path,page_number\nA001_0001.jpg,front\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["--input", str(input_dir), "--out", str(output_dir), "--manifest-csv", str(manifest_csv), "--workers", "1"])
            with contextlib.redirect_stdout(io.StringIO()):
                preflight_exit = main(["preflight", "--input", str(input_dir), "--out", str(preflight_dir), "--manifest-csv", str(manifest_csv)])

            report = json.loads((output_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
            preflight = json.loads((preflight_dir / "preflight_report.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(preflight_exit, 1)
            self.assertEqual(report["summary"]["manifest_sequence_invalid_count"], 1)
            self.assertEqual(_rule_count(report, "manifest_invalid_sequence"), 1)
            self.assertEqual(preflight["manifest"]["sequence_invalid_count"], 1)
            self.assertIn("manifest_invalid_sequence_values", {error["code"] for error in preflight["errors"]})

    def test_manifest_sequence_order_mismatch_and_strict_gap_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            manifest_csv = root / "manifest.csv"
            input_dir.mkdir()
            Image.new("RGB", (32, 24), "white").save(input_dir / "A001_0001.jpg", dpi=(300, 300))
            Image.new("RGB", (32, 24), "lightgray").save(input_dir / "A001_0002.jpg", dpi=(300, 300))
            manifest_csv.write_text(
                "relative_path,expected_order,strict_sequence\n"
                "A001_0002.jpg,1,true\n"
                "A001_0001.jpg,3,true\n",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["--input", str(input_dir), "--out", str(output_dir), "--manifest-csv", str(manifest_csv), "--workers", "1"])

            report = json.loads((output_dir / "scan_qc_report.json").read_text(encoding="utf-8"))
            html = (output_dir / "scan_qc_report.html").read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["summary"]["manifest_sequence_gap_count"], 1)
            self.assertEqual(report["summary"]["manifest_sequence_order_mismatch_count"], 2)
            self.assertEqual(_rule_count(report, "manifest_sequence_gap"), 1)
            self.assertEqual(_rule_count(report, "manifest_order_mismatch"), 2)
            self.assertIn("Manifest Order Mismatches", html)

    def test_run_plan_two_batches_success_and_writes_aggregate_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan_path = root / "plan.csv"
            output_root = root / "project-out"
            batch_one = root / "PRIVATE_BATCH_ONE"
            batch_two = root / "PRIVATE_BATCH_TWO"
            batch_one.mkdir()
            batch_two.mkdir()
            batch_one_image = Image.new("RGB", (48, 36), "white")
            batch_one_image.putpixel((12, 12), (0, 0, 0))
            batch_one_image.save(batch_one / "SECRET_ONE.png", dpi=(300, 300))
            Image.new("RGB", (48, 36), "white").save(batch_two / "SECRET_TWO.png", dpi=(300, 300))
            plan_path.write_text(
                "batch_id,input_dir,report_dir,process_out,workers,auto_crop,despeckle,resume_processing\n"
                f"batch-one,{batch_one},reports-one,processed-one,1,true,true,false\n"
                f"batch-two,{batch_two},reports-two,processed-two,1,false,false,true\n",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["run-plan", "--plan-csv", str(plan_path), "--out", str(output_root), "--project", "project-x"])

            self.assertEqual(exit_code, 0)
            self.assertTrue((output_root / "reports-one" / "preflight_report.json").exists())
            self.assertTrue((output_root / "reports-one" / "scan_qc_report.json").exists())
            self.assertTrue((output_root / "processed-one" / "processing_manifest.json").exists())
            summary_path = output_root / "run_plan_summary.json"
            csv_path = output_root / "run_plan_summary.csv"
            self.assertTrue(summary_path.exists())
            self.assertTrue(csv_path.exists())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["project_id"], "project-x")
            self.assertEqual(summary["summary"]["total_batches"], 2)
            self.assertEqual(summary["summary"]["passed_batches"], 2)
            self.assertEqual(summary["summary"]["failed_batches"], 0)
            self.assertEqual(summary["summary"]["processing_failed_files"], 0)
            self.assertEqual(summary["batches"][1]["processing_resumed_files"], 0)
            despeckle_timing = summary["summary"]["processing_operation_timings"]["despeckle"]
            self.assertEqual(despeckle_timing["file_count"], 1)
            self.assertIn(despeckle_timing["backend_mode"], {"numpy", "fallback"})
            self.assertEqual(sum(despeckle_timing["backend_counts"].values()), 1)

    def test_run_plan_continue_on_error_records_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            good_input = root / "good"
            good_input.mkdir()
            Image.new("RGB", (48, 36), "white").save(good_input / "public_synthetic.png", dpi=(300, 300))
            missing_input = root / "missing"
            output_root = root / "project-out"
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "batches": [
                            {"batch_id": "bad-batch", "input_dir": str(missing_input), "report_dir": "bad-report"},
                            {"batch_id": "good-batch", "input_dir": str(good_input), "report_dir": "good-report"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["run-plan", "--plan-json", str(plan_path), "--out", str(output_root), "--continue-on-error"])

            self.assertEqual(exit_code, 1)
            summary = json.loads((output_root / "run_plan_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["summary"]["total_batches"], 2)
            self.assertEqual(summary["summary"]["passed_batches"], 1)
            self.assertEqual(summary["summary"]["failed_batches"], 1)
            self.assertEqual(summary["summary"]["failed_batch_ids"], ["bad-batch"])
            self.assertGreaterEqual(summary["summary"]["preflight_error_count"], 1)
            self.assertEqual(summary["batches"][0]["failure_stage"], "preflight")
            self.assertEqual(summary["batches"][1]["status"], "passed")

    def test_run_plan_stops_on_failure_without_continue_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            good_input = root / "good"
            good_input.mkdir()
            Image.new("RGB", (48, 36), "white").save(good_input / "page.png", dpi=(300, 300))
            output_root = root / "project-out"
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    [
                        {"batch_id": "bad-batch", "input_dir": str(root / "missing"), "report_dir": "bad-report"},
                        {"batch_id": "good-batch", "input_dir": str(good_input), "report_dir": "good-report"},
                    ]
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(["run-plan", "--plan-json", str(plan_path), "--out", str(output_root)])

            self.assertEqual(exit_code, 1)
            summary = json.loads((output_root / "run_plan_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["summary"]["total_batches"], 1)
            self.assertEqual(summary["summary"]["failed_batch_ids"], ["bad-batch"])
            self.assertFalse((output_root / "good-report").exists())

    def test_run_plan_summary_is_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private_input"
            input_dir.mkdir()
            private_name = "PRIVATE_CASE_ABC123.png"
            Image.new("RGB", (48, 36), "white").save(input_dir / private_name, dpi=(300, 300))
            output_root = root / "project-out"
            plan_path = root / "plan.csv"
            plan_path.write_text(f"batch_id,input_dir,report_dir\nsafe-batch,{input_dir},reports\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run-plan", "--plan-csv", str(plan_path), "--out", str(output_root)]), 0)

            raw = (output_root / "run_plan_summary.json").read_text(encoding="utf-8")
            report = json.loads((output_root / "reports" / "scan_qc_report.json").read_text(encoding="utf-8"))
            private_hash = report["files"][0]["sha256"]
            for forbidden in [private_name, str(input_dir), private_hash, "relative_path", "sha256", '"files": [', '"findings": [']:
                self.assertNotIn(forbidden, raw)
            self.assertTrue(json.loads(raw)["privacy"]["aggregate_only"])

    def test_run_plan_missing_and_invalid_fields_are_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_root = root / "out"
            missing_field_plan = root / "missing.csv"
            missing_field_plan.write_text("batch_id,report_dir\nbatch-one,reports\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(["run-plan", "--plan-csv", str(missing_field_plan), "--out", str(output_root)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("missing required field 'input_dir'", stderr.getvalue())

            input_dir = root / "input"
            input_dir.mkdir()
            invalid_plan = root / "invalid.csv"
            invalid_plan.write_text(f"batch_id,input_dir,workers\nbatch-one,{input_dir},0\n", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(["run-plan", "--plan-csv", str(invalid_plan), "--out", str(output_root)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("field 'workers' must be a positive integer", stderr.getvalue())

    def test_run_plan_resume_processing_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (48, 36), "white").save(input_dir / "page.png", dpi=(300, 300))
            output_root = root / "project-out"
            plan_path = root / "plan.csv"
            plan_path.write_text(
                f"batch_id,input_dir,report_dir,process_out,resume_processing,workers\nbatch-one,{input_dir},reports,processed,true,1\n",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run-plan", "--plan-csv", str(plan_path), "--out", str(output_root)]), 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run-plan", "--plan-csv", str(plan_path), "--out", str(output_root)]), 0)

            summary = json.loads((output_root / "run_plan_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["summary"]["processing_resumed_files"], 1)
            manifest = json.loads((output_root / "processed" / "processing_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["resume"]["enabled"])
            self.assertEqual(manifest["summary"]["resumed_files"], 1)

    def test_private_integration_summary_is_aggregate_only(self) -> None:
        private_integration = _load_private_integration_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            output_root = root / "private-output"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "sensitive_original_name.png", dpi=(300, 300))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = private_integration.main(
                    [
                        "--input",
                        str(input_dir),
                        "--out",
                        str(output_root),
                        "--workers",
                        "1",
                        "--process-images",
                        "--skip-benchmark",
                    ]
                )

            self.assertEqual(exit_code, 0)
            summary_path = output_root / "private_integration_summary.json"
            self.assertTrue(summary_path.exists())
            summary_text = summary_path.read_text(encoding="utf-8")
            stdout_text = stdout.getvalue()
            summary = json.loads(summary_text)

            self.assertEqual(summary["aggregate_counts"]["total_files"], 1)
            self.assertEqual(summary["aggregate_counts"]["openable_files"], 1)
            self.assertEqual(summary["aggregate_counts"]["processing_processed_files"], 1)
            self.assertTrue(summary["privacy_self_check"]["passed"])
            self.assertEqual(summary["privacy_self_check"]["violation_count"], 0)
            self.assertIn("processing_operation_timings", summary["throughput"])
            self.assertNotIn(str(input_dir), summary_text)
            self.assertNotIn(str(output_root), summary_text)
            self.assertNotIn("sensitive_original_name.png", summary_text)
            self.assertNotIn("sensitive_original_name.png", stdout_text)
            self.assertNotIn(str(input_dir), stdout_text)
            self.assertIn("Sensitive local evidence", summary["privacy"]["row_level_artifacts"])

    def test_private_integration_summary_scopes_repeated_benchmark_rule_counts(self) -> None:
        private_integration = _load_private_integration_module()
        args = private_integration.build_parser().parse_args(
            ["--input", "/tmp/private-input", "--out", "/tmp/private-output", "--process-images"]
        )
        run_plan_summary = _private_run_plan_summary(total_findings=22)
        benchmark_summary = _private_benchmark_summary(
            [
                _private_benchmark_run(1, scan_rate=10.0, processing_rate=5.0, rule_count=22),
                _private_benchmark_run(2, scan_rate=20.0, processing_rate=7.0, rule_count=22),
                _private_benchmark_run(3, scan_rate=15.0, processing_rate=6.0, rule_count=22),
            ],
            scan_recommendation=20.0,
            processing_recommendation=7.0,
        )

        summary = private_integration._public_summary(args, Path("/tmp/private-output"), run_plan_summary, benchmark_summary)

        self.assertEqual(summary["aggregate_counts"]["total_findings"], 22)
        self.assertNotIn("finding_rule_counts", summary["aggregate_counts"])
        self.assertEqual(summary["benchmark"]["finding_rule_counts_repeated_runs"], {"duplicate_file": 66})
        self.assertEqual(summary["benchmark"]["source"], "benchmark repeated worker runs")

    def test_private_integration_summary_uses_best_benchmark_throughput(self) -> None:
        private_integration = _load_private_integration_module()
        args = private_integration.build_parser().parse_args(["--input", "/tmp/private-input", "--out", "/tmp/private-output"])
        benchmark_summary = _private_benchmark_summary(
            [
                _private_benchmark_run(1, scan_rate=10.0, processing_rate=4.0),
                _private_benchmark_run(2, scan_rate=30.0, processing_rate=9.0),
            ],
            scan_recommendation=30.0,
            processing_recommendation=9.0,
        )

        summary = private_integration._public_summary(
            args,
            Path("/tmp/private-output"),
            _private_run_plan_summary(total_findings=0),
            benchmark_summary,
        )

        self.assertEqual(summary["throughput"]["benchmark_scan_files_per_minute"], 30.0)
        self.assertEqual(summary["throughput"]["benchmark_processing_files_per_minute"], 9.0)
        self.assertEqual(summary["throughput"]["benchmark_basis"], "best observed recommendation mean files/minute")
        self.assertIn("benchmark_processing_operation_timings", summary["throughput"])

    def test_private_integration_acceptance_matches_acceptance_summary_logic(self) -> None:
        private_integration = _load_private_integration_module()
        args = private_integration.build_parser().parse_args(["--input", "/tmp/private-input", "--out", "/tmp/private-output"])
        run_plan_summary = _private_run_plan_summary(total_findings=0, failed_batches=1)
        benchmark_summary = _private_benchmark_summary([_private_benchmark_run(1, scan_rate=10.0, processing_rate=None)])

        summary = private_integration._public_summary(args, Path("/tmp/private-output"), run_plan_summary, benchmark_summary)
        expected = build_acceptance_summary(run_plan_summary=run_plan_summary, benchmark_results=benchmark_summary)

        self.assertEqual(summary["acceptance"]["passed"], expected["pass"])
        self.assertEqual(summary["acceptance"]["status"], expected["status"])
        self.assertEqual(summary["acceptance"]["summary"]["blocking_items"], expected["blocking_items"])
        self.assertIn("review_summary was not provided", " ".join(summary["acceptance"]["summary"]["warnings"]))

    def test_private_integration_summary_privacy_self_check_allows_only_aggregate_fields(self) -> None:
        private_integration = _load_private_integration_module()
        args = private_integration.build_parser().parse_args(
            ["--input", "/private/source", "--out", "/private/output", "--process-images"]
        )
        summary = private_integration._public_summary(
            args,
            Path("/private/output"),
            _private_run_plan_summary(total_findings=0),
            _private_benchmark_summary([_private_benchmark_run(1, scan_rate=10.0, processing_rate=5.0)]),
        )

        leaks = private_integration.privacy_self_check(
            summary,
            forbidden_values={"private input directory": "/private/source", "private output root": "/private/output"},
        )
        summary_text = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(leaks, [])
        self.assertNotIn("/private/source", summary_text)
        self.assertNotIn("/private/output", summary_text)
        for forbidden in ["relative_path", "filename", "sha256", "thumbnail", "reviewer_notes"]:
            self.assertNotIn(f'"{forbidden}"', summary_text)

    def test_private_integration_redaction_self_check_rejects_sensitive_keys_and_values(self) -> None:
        private_integration = _load_private_integration_module()
        leaks = private_integration.privacy_self_check(
            {
                "summary": {"total_files": 1},
                "rows": [{"relative_path": "private/page.png", "ok": "safe"}],
                "note": "/private/input/private/page.png",
            },
            forbidden_values={"private input directory": "/private/input"},
        )

        self.assertTrue(any("relative_path" in leak for leak in leaks))
        self.assertTrue(any("private input directory" in leak for leak in leaks))


def _private_run_plan_summary(
    *,
    total_findings: int,
    failed_batches: int = 0,
    processing_failed_files: int = 0,
) -> dict[str, object]:
    return {
        "schema_version": "scan-qc.run-plan-summary.v1",
        "privacy": {"aggregate_only": True},
        "summary": {
            "total_files": 22,
            "openable_files": 22,
            "total_findings": total_findings,
            "p0_findings": 0,
            "p1_findings": 0,
            "p2_findings": total_findings,
            "processing_processed_files": 22,
            "processing_failed_files": processing_failed_files,
            "failed_batches": failed_batches,
            "preflight_error_count": 0,
            "scan_files_per_minute": 12.0,
            "scan_openable_files_per_minute": 12.0,
            "processing_files_per_minute": 8.0,
            "processing_operation_timings": {
                "deskew": {
                    "enabled": True,
                    "file_count": 22,
                    "elapsed_seconds": 2.0,
                    "files_per_minute": 660.0,
                    "average_seconds_per_file": 0.090909,
                }
            },
        },
        "batches": [{"workers": 1}],
    }


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


def _private_benchmark_summary(
    runs: list[dict[str, object]],
    *,
    scan_recommendation: float | None = None,
    processing_recommendation: float | None = None,
) -> dict[str, object]:
    recommendations: dict[str, object] = {"schema_version": "scan-qc.benchmark.recommendations.v1"}
    if scan_recommendation is not None:
        recommendations["scan_only"] = {"files_per_minute": scan_recommendation}
    if processing_recommendation is not None:
        recommendations["processing"] = {"files_per_minute": processing_recommendation}
    return {
        "schema_version": "scan-qc.benchmark.v1",
        "privacy": {"aggregate_only": True},
        "recommendations": recommendations,
        "runs": runs,
    }


def _private_benchmark_run(
    run_index: int,
    *,
    scan_rate: float,
    processing_rate: float | None,
    rule_count: int = 0,
) -> dict[str, object]:
    return {
        "run_index": run_index,
        "repeat_index": run_index,
        "requested_workers": run_index,
        "effective_workers": run_index,
        "scan_only": False,
        "finding_rule_counts": {"duplicate_file": rule_count} if rule_count else {},
        "scan": {"files_per_minute": scan_rate},
        "processing": {
            "enabled": processing_rate is not None,
            "failed_files": 0,
            "processed_files_per_minute": processing_rate,
            "effective_workers": run_index if processing_rate is not None else None,
            "operation_timings": {
                "deskew": {
                    "file_count": 22 if processing_rate is not None else 0,
                    "elapsed_seconds": 2.0 if processing_rate is not None else 0.0,
                    "files_per_minute": 660.0 if processing_rate is not None else 0.0,
                }
            },
        },
    }


def _synthetic_text_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(30, 30, 30), width=2)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    return image


def _synthetic_faded_text_page(
    *,
    background: int = 242,
    ink: int = 188,
    red_stamp: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (240, 180), (background, background, background))
    draw = ImageDraw.Draw(image)
    for y in range(36, 132, 18):
        draw.rectangle((42, y, 136, y + 3), fill=(ink, ink, ink))
        draw.rectangle((46, y + 8, 118, y + 10), fill=(ink + 4, ink + 4, ink + 4))
    if red_stamp:
        draw.ellipse((158, 46, 210, 98), outline=(180, 40, 35), width=3)
        draw.line((170, 72, 198, 72), fill=(180, 40, 35), width=2)
    return image


def _synthetic_blurred_text_page(
    *,
    red_stamp: bool = False,
    page_number: bool = False,
    header_footer: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for y in range(38, 132, 18):
        draw.rectangle((42, y, 164, y + 3), fill=(42, 42, 42))
        draw.rectangle((46, y + 8, 136, y + 10), fill=(58, 58, 58))
    if red_stamp:
        draw.ellipse((166, 48, 216, 98), outline=(180, 40, 35), width=3)
    if page_number:
        draw.text((180, 12), "12", fill=(58, 58, 58))
    if header_footer:
        draw.rectangle((48, 18, 170, 20), fill=(64, 64, 64))
        draw.rectangle((54, 158, 154, 160), fill=(64, 64, 64))
    return image.filter(ImageFilter.GaussianBlur(radius=0.8))


def _synthetic_dark_blurred_text_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    for y in range(38, 132, 18):
        draw.rectangle((42, y, 164, y + 3), fill=(70, 70, 70))
        draw.rectangle((46, y + 8, 136, y + 10), fill=(82, 82, 82))
    return image.filter(ImageFilter.GaussianBlur(radius=0.8))


def _synthetic_blurred_text_edge_risk_page() -> Image.Image:
    image = _synthetic_blurred_text_page()
    draw = ImageDraw.Draw(image)
    draw.line((0, 70, 30, 70), fill=(60, 60, 60), width=2)
    return image


def _synthetic_low_confidence_text_edge_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for y in range(38, 132, 18):
        draw.rectangle((42, y, 164, y + 3), fill=(218, 218, 218))
        draw.rectangle((46, y + 8, 136, y + 10), fill=(220, 220, 220))
    return image.filter(ImageFilter.GaussianBlur(radius=0.8))


def _synthetic_high_contrast_dense_text_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for y in range(35, 145, 14):
        draw.rectangle((20, y, 220, y + 4), fill=(20, 20, 20))
    return image


def _synthetic_tone_gray_text_page(
    *,
    background: tuple[int, int, int] = (188, 188, 188),
    foreground: tuple[int, int, int] = (92, 92, 92),
    red_stamp: bool = False,
    handwriting: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (240, 180), background)
    draw = ImageDraw.Draw(image)
    for y in (44, 68, 92, 116):
        draw.rectangle((34, y, 188, y + 5), fill=foreground)
        draw.rectangle((38, y + 10, 148, y + 13), fill=foreground)
    if red_stamp:
        draw.ellipse((164, 98, 220, 154), outline=(180, 28, 28), width=4)
        draw.line((176, 126, 208, 126), fill=(180, 28, 28), width=2)
    if handwriting:
        draw.line((30, 144, 92, 162), fill=(148, 132, 178), width=2)
        draw.line((92, 162, 154, 140), fill=(148, 132, 178), width=2)
    return image


def _synthetic_tone_normal_exposure_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    for y in (44, 68, 92, 116):
        draw.rectangle((34, y, 188, y + 5), fill=(35, 35, 35))
    return image


def _synthetic_tone_high_contrast_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (218, 218, 218))
    draw = ImageDraw.Draw(image)
    for y in (32, 52, 72, 92, 112, 132):
        draw.rectangle((26, y, 210, y + 6), fill=(42, 42, 42))
    return image


def _synthetic_tone_color_photo_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (188, 188, 188))
    draw = ImageDraw.Draw(image)
    for y in range(24, 156):
        draw.line((34, y, 206, y), fill=(86 + y % 80, 126 + y % 50, 170 + y % 40))
    return image


def _synthetic_tone_texture_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (206, 206, 202))
    draw = ImageDraw.Draw(image)
    for x in range(0, 240, 6):
        draw.line((x, 0, x, 179), fill=(198, 198, 194))
    return image


def _synthetic_tone_dark_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (112, 112, 112))
    draw = ImageDraw.Draw(image)
    for y in (44, 68, 92, 116):
        draw.rectangle((34, y, 188, y + 5), fill=(50, 50, 50))
    return image


def _synthetic_tone_overexposed_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    for y in (44, 68, 92, 116):
        draw.rectangle((34, y, 188, y + 5), fill=(226, 226, 226))
    return image


def _synthetic_tone_noisy_page() -> Image.Image:
    image = _synthetic_tone_gray_text_page()
    for x in range(0, image.width, 2):
        for y in range(0, image.height, 3):
            if (x * 37 + y * 19) % 17 == 0:
                image.putpixel((x, y), (70, 70, 70) if (x + y) % 2 else (220, 220, 220))
    return image


def _synthetic_tone_color_annotation_page() -> Image.Image:
    image = _synthetic_tone_gray_text_page()
    draw = ImageDraw.Draw(image)
    draw.arc((70, 126, 174, 170), 190, 350, fill=(145, 165, 200), width=2)
    return image


def _synthetic_tone_low_confidence_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (188, 188, 188))
    ImageDraw.Draw(image).rectangle((104, 88, 136, 91), fill=(92, 92, 92))
    return image


def _uniform_cast_page(
    *,
    background: tuple[int, int, int] = (246, 243, 232),
    red_stamp: bool = False,
    blue_annotation: bool = False,
    tiny_red_mark: bool = False,
    tiny_blue_mark: bool = False,
    handwriting: bool = False,
    photo: bool = False,
    chart: bool = False,
    edge_mark: bool = False,
    edge_tiny_red_mark: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (180, 140), background)
    draw = ImageDraw.Draw(image)
    if red_stamp:
        draw.ellipse((112, 72, 158, 118), outline=(190, 28, 28), width=4)
    if blue_annotation:
        draw.line((28, 36, 142, 44), fill=(42, 84, 190), width=3)
    if tiny_red_mark:
        draw.ellipse((120, 78, 129, 87), outline=(190, 28, 28), width=2)
    if tiny_blue_mark:
        draw.line((76, 72, 88, 76), fill=(42, 84, 190), width=2)
    if handwriting:
        draw.line((26, 92, 80, 110), fill=(64, 54, 50), width=2)
        draw.line((80, 110, 136, 88), fill=(64, 54, 50), width=2)
    if photo:
        for y in range(24, 104):
            draw.line((46, y, 136, y), fill=(70 + y % 90, 118 + y % 50, 168 + y % 44))
    if chart:
        for x in range(36, 146, 22):
            draw.line((x, 28, x, 112), fill=(70, 120, 190), width=2)
        for y in range(34, 112, 18):
            draw.line((32, y, 150, y), fill=(180, 70, 60), width=2)
    if edge_mark:
        draw.rectangle((0, 54, 12, 74), fill=(62, 48, 44))
    if edge_tiny_red_mark:
        draw.ellipse((2, 50, 15, 63), outline=(190, 28, 28), width=2)
    return image


def _sparse_text_uniform_cast_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (246, 243, 234))
    draw = ImageDraw.Draw(image)
    for y in (42, 66, 90):
        draw.rectangle((36, y, 128, y + 3), fill=(58, 58, 58))
    draw.rectangle((182, 24, 196, 30), fill=(72, 72, 72))
    draw.line((40, 132, 132, 132), fill=(82, 82, 82), width=2)
    return image


def _dense_text_uniform_cast_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (246, 243, 234))
    draw = ImageDraw.Draw(image)
    for y in range(20, 160, 10):
        draw.rectangle((20, y, 220, y + 4), fill=(58, 58, 58))
        draw.rectangle((28, y + 6, 190, y + 8), fill=(82, 82, 82))
    return image


def _synthetic_blurred_table_text_edge_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for y in range(35, 145, 22):
        draw.line((25, y, 215, y), fill=(55, 55, 55), width=2)
    for x in range(25, 216, 38):
        draw.line((x, 35, x, 145), fill=(55, 55, 55), width=2)
    draw.text((180, 18), "12", fill=(60, 60, 60))
    draw.line((48, 158, 116, 166), fill=(90, 90, 90), width=2)
    return image.filter(ImageFilter.GaussianBlur(radius=0.7))


def _synthetic_photo_like_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (232, 232, 232))
    draw = ImageDraw.Draw(image)
    for y in range(20, 160):
        shade = 150 + ((y * 7) % 82)
        draw.line((24, y, 216, y), fill=(shade, shade, shade))
    for x in range(34, 210, 12):
        draw.line((x, 26, min(216, x + 42), 154), fill=(120 + (x % 50), 120 + (x % 50), 120 + (x % 50)), width=2)
    return image


def _synthetic_texture_stain_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (238, 238, 238))
    draw = ImageDraw.Draw(image)
    draw.ellipse((44, 36, 184, 130), fill=(196, 196, 196))
    for x in range(18, 224, 10):
        for y in range(14, 166, 14):
            shade = 190 + ((x + y) % 28)
            draw.point((x, y), fill=(shade, shade, shade))
            draw.point((x + 1, y), fill=(shade, shade, shade))
    return image


def _synthetic_faded_text_edge_risk_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (242, 242, 242))
    draw = ImageDraw.Draw(image)
    draw.line((0, 38, 55, 38), fill=(198, 198, 198), width=3)
    draw.line((238, 0, 238, 179), fill=(196, 196, 196), width=2)
    for y in range(64, 132, 18):
        draw.rectangle((60, y, 130, y + 3), fill=(210, 210, 210))
    return image


def _synthetic_faded_text_table_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (242, 242, 242))
    draw = ImageDraw.Draw(image)
    for y in (42, 72, 102, 132):
        draw.line((26, y, 214, y), fill=(190, 190, 190), width=2)
    for x in (26, 88, 150, 214):
        draw.line((x, 42, x, 132), fill=(190, 190, 190), width=2)
    draw.text((180, 18), "12", fill=(60, 60, 60))
    draw.line((48, 150, 116, 158), fill=(120, 120, 120), width=2)
    return image


def _synthetic_background_stain_page(
    stain_color: tuple[int, int, int],
    *,
    red_annotation: bool = False,
    edge_mark: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (240, 180), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    draw.ellipse((140, 60, 205, 112), fill=stain_color)
    for y in range(42, 132, 20):
        draw.rectangle((28, y, 90, y + 4), fill=(35, 35, 35))
    draw.text((112, 24), "12", fill=(30, 30, 30))
    for y in (138, 150):
        draw.line((104, y, 198, y), fill=(40, 40, 40), width=1)
    for x in (128, 162):
        draw.line((x, 136, x, 154), fill=(40, 40, 40), width=1)
    draw.line((26, 142, 92, 154), fill=(55, 55, 55), width=2)
    if red_annotation:
        draw.ellipse((154, 30, 206, 82), outline=(180, 40, 35), width=3)
        draw.line((166, 56, 194, 56), fill=(180, 40, 35), width=2)
    if edge_mark:
        draw.rectangle((0, 74, 14, 102), fill=(58, 58, 58))
    return image


def _synthetic_bleed_through_page(variant: str) -> Image.Image:
    image = Image.new("RGB", (260, 180), (244, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.text((34, 36), "REAL", fill=(70, 70, 70))
    if variant == "ghost":
        mask = Image.new("L", image.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.text((124, 86), "321", fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(1.2))
        ghost = Image.new("RGB", image.size, (217, 217, 212))
        image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.65)))
    elif variant == "pale_ghost":
        mask = Image.new("L", image.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.text((124, 86), "321", fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(2.0))
        ghost = Image.new("RGB", image.size, (214, 214, 210))
        image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.65)))
    elif variant == "text":
        draw.text((124, 86), "321", fill=(215, 215, 210))
        draw.line((120, 112, 206, 112), fill=(215, 215, 210), width=1)
    elif variant == "page_number":
        draw.text((204, 4), "12", fill=(212, 212, 208))
    elif variant == "table":
        for x in range(120, 210, 20):
            draw.line((x, 80, x, 125), fill=(214, 214, 210), width=1)
        for y in range(80, 126, 15):
            draw.line((120, y, 210, y), fill=(214, 214, 210), width=1)
    elif variant == "stamp":
        draw.ellipse((120, 80, 188, 134), outline=(190, 80, 80), width=2)
    elif variant == "edge":
        draw.text((4, 86), "321", fill=(219, 219, 214))
    elif variant == "dense":
        for x in range(14, 246, 6):
            for y in range(14, 166, 6):
                shade = 184 + ((x * 7 + y * 11) % 36)
                draw.rectangle((x, y, x + 2, y + 2), fill=(shade, shade, shade))
    else:
        raise ValueError(f"unknown bleed-through variant: {variant}")
    return image


def _synthetic_background_multi_stain_page(*, variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (240, 180), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    for y in range(42, 132, 20):
        draw.rectangle((28, y, 90, y + 4), fill=(35, 35, 35))
    draw.text((112, 24), "12", fill=(30, 30, 30))
    for y in (138, 150):
        draw.line((104, y, 198, y), fill=(40, 40, 40), width=1)
    for x in (128, 162):
        draw.line((x, 136, x, 154), fill=(40, 40, 40), width=1)

    if variant == "safe":
        spots = (
            ((130, 42, 148, 58), (224, 224, 224)),
            ((176, 72, 198, 90), (230, 226, 205)),
            ((144, 110, 162, 126), (226, 226, 226)),
        )
    elif variant == "too_many":
        spots = tuple(
            ((118 + (index % 4) * 24, 42 + (index // 4) * 36, 132 + (index % 4) * 24, 54 + (index // 4) * 36), (224, 224, 224))
            for index in range(7)
        )
    elif variant == "large":
        spots = (((118, 48, 206, 70), (224, 224, 224)),)
    elif variant == "near_foreground":
        spots = (((90, 78, 112, 96), (224, 224, 224)),)
    elif variant == "edge":
        spots = (
            ((2, 76, 13, 92), (224, 224, 224)),
            ((176, 72, 198, 90), (224, 224, 224)),
        )
    else:
        raise ValueError(f"unknown multi-stain variant: {variant}")

    for box, color in spots:
        draw.ellipse(box, fill=color)
    return image


def _synthetic_dense_background_texture_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (232, 232, 232))
    draw = ImageDraw.Draw(image)
    for x in range(12, 228, 6):
        for y in range(10, 170, 6):
            shade = 92 + ((x * 5 + y * 7) % 80)
            draw.rectangle((x, y, x + 2, y + 2), fill=(shade, shade, shade))
    return image


def _synthetic_sparse_text_edge_preflight_skip_page() -> Image.Image:
    image = Image.new("RGB", (1200, 900), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for x, y in ((100, 100), (350, 100), (600, 100)):
        draw.rectangle((x, y, x + 160, y + 80), fill=(60, 60, 60))
    return image


def _synthetic_dense_faded_text_noop_page() -> Image.Image:
    image = Image.new("RGB", (720, 540), (238, 238, 238))
    draw = ImageDraw.Draw(image)
    for y in range(72, 468, 12):
        shade = 176 + ((y * 5) % 24)
        draw.rectangle((72, y, 648, y + 5), fill=(shade, shade, shade))
    for x in range(84, 636, 18):
        shade = 184 + ((x * 3) % 20)
        draw.line((x, 84, min(648, x + 84), 456), fill=(shade, shade, shade), width=3)
    return image


def _sha256_for_test(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _edge_energy(image: Image.Image) -> float:
    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(edges).mean[0])


def _synthetic_ink_text_page() -> Image.Image:
    image = Image.new("L", (240, 180), 0)
    draw = ImageDraw.Draw(image)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=255)
    return image


def _synthetic_shallow_stable_text_page(*, red_mark: bool = False) -> Image.Image:
    image = Image.new("RGB", (240, 320), (246, 246, 246))
    draw = ImageDraw.Draw(image)
    for y in range(50, 230, 30):
        draw.rectangle((45, y, 195, y + 3), fill=(50, 50, 50))
    if red_mark:
        draw.ellipse((164, 42, 210, 88), outline=(180, 38, 32), width=3)
        draw.line((176, 66, 198, 66), fill=(180, 38, 32), width=2)
    return image


def _synthetic_faint_segmented_text_page() -> Image.Image:
    image = Image.new("RGB", (420, 520), (248, 248, 244))
    draw = ImageDraw.Draw(image)
    ink = (232, 232, 232)
    for y in range(80, 390, 26):
        x = 60
        while x < 350:
            width = 4 + ((x + y) // 13) % 10
            draw.rectangle((x, y, min(350, x + width), y + 2), fill=ink)
            x += width + 8
    return image


def _synthetic_faint_ambiguous_text_page() -> Image.Image:
    base = Image.new("RGB", (420, 520), (248, 248, 244))
    upper = _synthetic_faint_segmented_text_page().crop((0, 0, 420, 250)).rotate(
        -0.8,
        resample=Image.Resampling.BICUBIC,
        expand=False,
        fillcolor=(248, 248, 244),
    )
    lower = _synthetic_faint_segmented_text_page().crop((0, 250, 420, 520)).rotate(
        1.0,
        resample=Image.Resampling.BICUBIC,
        expand=False,
        fillcolor=(248, 248, 244),
    )
    base.paste(upper, (0, 0))
    base.paste(lower, (0, 250))
    return base


def _faint_text_horizontal_alignment_score(image: Image.Image) -> float:
    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total_pixels = image.width * image.height
    raw_low = _histogram_percentile_for_test(histogram, total_pixels, 0.005)
    raw_high = _histogram_percentile_for_test(histogram, total_pixels, 0.995)
    threshold = max(0, raw_high - max(6, min(24, int(round((raw_high - raw_low) * 0.75)))))
    ink = grayscale.point(lambda value: 255 if value <= threshold else 0, mode="L")
    return _horizontal_projection_variance(ink)


def _histogram_percentile_for_test(histogram: list[int], total: int, percentile: float) -> int:
    target = total * percentile
    running = 0
    for value, count in enumerate(histogram):
        running += count
        if running >= target:
            return value
    return 255


def _synthetic_shallow_table_page() -> Image.Image:
    image = Image.new("RGB", (240, 320), (246, 246, 246))
    draw = ImageDraw.Draw(image)
    for y in range(48, 230, 30):
        draw.line((34, y, 206, y), fill=(50, 50, 50), width=2)
    for x in range(34, 207, 22):
        draw.line((x, 48, x, 228), fill=(50, 50, 50), width=2)
    return image


def _synthetic_edge_content_skew_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    draw.rectangle((0, 20, 22, 32), fill=(15, 15, 15))
    draw.rectangle((210, 146, 239, 160), fill=(15, 15, 15))
    draw.line((0, 75, 44, 75), fill=(15, 15, 15), width=3)
    draw.line((235, 0, 235, 179), fill=(40, 40, 40), width=3)
    return image.rotate(-2.5, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")


def _synthetic_sparse_edge_evidence_page() -> Image.Image:
    image = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(image)
    for x in (80, 320):
        draw.line((x, 45, x, 255), fill=(20, 20, 20), width=3)
    for y in (70, 230):
        draw.line((115, y, 285, y), fill=(25, 25, 25), width=2)
    return image


def _synthetic_inconsistent_skew_page() -> Image.Image:
    image = Image.new("RGB", (300, 220), "white")
    draw = ImageDraw.Draw(image)
    for y in range(45, 105, 16):
        draw.rectangle((50, y, 240, y + 4), fill=(20, 20, 20))
    overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for y in range(130, 190, 16):
        overlay_draw.rectangle((60, y, 250, y + 4), fill=(20, 20, 20, 255))
    overlay = overlay.rotate(5.0, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(255, 255, 255, 0))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    return image.rotate(-3.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")


def _synthetic_light_noise_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (252, 252, 252))
    draw = ImageDraw.Draw(image)
    for point in [(20, 20), (80, 60), (140, 100), (210, 150)]:
        draw.point(point, fill=(238, 238, 238))
    return image


def _synthetic_dark_border_page() -> Image.Image:
    image = _synthetic_text_page()
    draw = ImageDraw.Draw(image)
    for offset in range(5):
        draw.rectangle((offset, offset, image.width - 1 - offset, image.height - 1 - offset), outline=(8, 8, 8))
    return image


def _synthetic_edge_cutoff_page() -> Image.Image:
    image = _synthetic_text_page()
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 72, 14, 120), fill=(15, 15, 15))
    draw.rectangle((0, 126, 28, 131), fill=(15, 15, 15))
    return image


def _synthetic_scanline_page(orientation: str) -> Image.Image:
    image = _synthetic_text_page().resize((360, 270), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(image)
    if orientation == "horizontal":
        draw.rectangle((0, 134, image.width - 1, 139), fill=(0, 0, 0))
    else:
        draw.rectangle((178, 0, 183, image.height - 1), fill=(0, 0, 0))
    return image


def _synthetic_repair_scanline_page(
    orientation: str,
    *,
    table_line: bool = False,
    page_number: bool = False,
    red_stamp: bool = False,
    handwriting: bool = False,
    underline: bool = False,
    header_footer: bool = False,
    edge_archive_line: bool = False,
    scanline: bool = True,
) -> Image.Image:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (42, 64, 86):
        draw.rectangle((42, y, 158, y + 5), fill=(36, 36, 36))
    if orientation == "horizontal" and scanline:
        draw.rectangle((16, 132, 244, 133), fill=(236, 236, 232))
    elif orientation == "vertical" and scanline:
        draw.rectangle((212, 18, 213, 164), fill=(236, 236, 232))
    elif orientation not in {"horizontal", "vertical"}:
        raise ValueError(orientation)
    if table_line:
        draw.line((24, 126, 236, 126), fill=(45, 45, 45), width=2)
    if page_number:
        draw.rectangle((120, 166, 140, 174), fill=(35, 35, 35))
    if red_stamp:
        draw.ellipse((170, 84, 224, 138), outline=(180, 20, 20), width=4)
    if handwriting:
        draw.line((32, 128, 80, 150), fill=(55, 55, 55), width=2)
        draw.line((80, 150, 126, 126), fill=(55, 55, 55), width=2)
    if underline:
        draw.line((42, 93, 158, 93), fill=(42, 42, 42), width=2)
    if header_footer:
        draw.rectangle((26, 18, 232, 20), fill=(44, 44, 44))
        draw.rectangle((108, 160, 152, 168), fill=(36, 36, 36))
    if edge_archive_line:
        draw.rectangle((4, 112, 18, 154), fill=(60, 60, 60))
    return image


def _synthetic_broken_repair_scanline_page(orientation: str) -> Image.Image:
    image = _synthetic_repair_scanline_page(orientation, scanline=False)
    draw = ImageDraw.Draw(image)
    if orientation == "horizontal":
        for x0 in (18, 54, 92, 132, 172, 212):
            draw.rectangle((x0, 132, x0 + 15, 133), fill=(232, 232, 228))
    elif orientation == "vertical":
        for y0 in (20, 44, 70, 98, 126, 150):
            draw.rectangle((212, y0, 213, y0 + 9), fill=(232, 232, 228))
    else:
        raise ValueError(orientation)
    return image


def _synthetic_scanline_low_confidence_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    ImageDraw.Draw(image).rectangle((16, 132, 244, 133), fill=(222, 222, 218))
    return image


def _synthetic_edge_shadow_repair_page(
    *,
    edge_text: bool = False,
    page_number: bool = False,
    table_line: bool = False,
    red_stamp: bool = False,
    center_red_annotation: bool = False,
    handwriting: bool = False,
    binding_hole: bool = False,
    archive_line: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (260, 180), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    for x in range(14):
        shade = 190 + x * 3
        draw.line((x, 0, x, image.height - 1), fill=(shade, shade, shade))
    for y in (46, 68, 90):
        draw.rectangle((76, y, 184, y + 5), fill=(35, 35, 35))
    if edge_text:
        draw.rectangle((16, 74, 42, 78), fill=(28, 28, 28))
    if page_number:
        draw.rectangle((120, 162, 142, 172), fill=(32, 32, 32))
    if table_line:
        draw.line((2, 124, 236, 124), fill=(45, 45, 45), width=2)
    if red_stamp:
        draw.ellipse((174, 88, 230, 144), outline=(180, 28, 28), width=4)
    if center_red_annotation:
        draw.ellipse((108, 64, 152, 108), outline=(180, 28, 28), width=4)
    if handwriting:
        draw.line((18, 138, 72, 160), fill=(55, 55, 55), width=2)
        draw.line((72, 160, 124, 136), fill=(55, 55, 55), width=2)
    if binding_hole:
        draw.ellipse((2, 84, 10, 94), fill=(24, 24, 24))
    if archive_line:
        draw.rectangle((4, 112, 16, 154), fill=(58, 58, 58))
    return image


def _synthetic_edge_shadow_dense_texture_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (238, 238, 234))
    draw = ImageDraw.Draw(image)
    for x in range(14):
        shade = 188 + x * 3
        draw.line((x, 0, x, image.height - 1), fill=(shade, shade, shade))
    for x in range(18, 246, 6):
        for y in range(12, 168, 6):
            shade = 88 + ((x * 5 + y * 7) % 86)
            draw.rectangle((x, y, x + 2, y + 2), fill=(shade, shade, shade))
    return image


def _synthetic_edge_shadow_low_confidence_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for x in range(4):
        draw.line((x, 0, x, image.height - 1), fill=(232 + x, 232 + x, 228 + x))
    return image


def _synthetic_corner_shadow_page(
    *,
    page_number: bool = False,
    red_stamp: bool = False,
    handwriting: bool = False,
    table_line: bool = False,
    page_border: bool = False,
    color_mark: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (260, 180), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    for radius in range(62, 2, -2):
        shade = int(round(242 - (radius / 62) * 42))
        draw.pieslice((0, 0, radius * 2, radius * 2), 180, 270, fill=(shade, shade, shade))
    for y in (60, 82, 104):
        draw.rectangle((84, y, 188, y + 5), fill=(35, 35, 35))
    if page_number:
        draw.text((16, 14), "12", fill=(28, 28, 28))
    if red_stamp:
        draw.ellipse((12, 12, 56, 56), outline=(178, 28, 28), width=4)
    if handwriting:
        draw.line((12, 46, 34, 20), fill=(45, 45, 45), width=2)
        draw.line((34, 20, 58, 42), fill=(45, 45, 45), width=2)
    if table_line:
        draw.line((0, 42, 72, 42), fill=(44, 44, 44), width=1)
        draw.line((42, 0, 42, 72), fill=(44, 44, 44), width=1)
    if page_border:
        draw.rectangle((8, 8, 250, 172), outline=(45, 45, 45), width=2)
    if color_mark:
        draw.rectangle((16, 16, 42, 42), fill=(44, 116, 200))
    return image


def _synthetic_paired_soft_corner_vignette_page(
    corners: tuple[str, str],
    *,
    page_number: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (260, 180), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    for corner in corners:
        for radius in range(64, 2, -2):
            shade = int(round(242 - (radius / 64) * 22))
            if corner == "top_left":
                box = (0, 0, radius * 2, radius * 2)
                angles = (180, 270)
            elif corner == "top_right":
                box = (image.width - radius * 2, 0, image.width, radius * 2)
                angles = (270, 360)
            elif corner == "bottom_left":
                box = (0, image.height - radius * 2, radius * 2, image.height)
                angles = (90, 180)
            elif corner == "bottom_right":
                box = (image.width - radius * 2, image.height - radius * 2, image.width, image.height)
                angles = (0, 90)
            else:
                raise ValueError(f"unsupported corner: {corner}")
            draw.pieslice(box, *angles, fill=(shade, shade, shade))
    if page_number:
        draw.text((16, 14), "12", fill=(28, 28, 28))
    return image


def _synthetic_corner_dark_texture_page() -> Image.Image:
    image = _synthetic_corner_shadow_page()
    draw = ImageDraw.Draw(image)
    for x in range(4, 68, 7):
        for y in range(4, 68, 7):
            shade = 158 + ((x * 13 + y * 11) % 76)
            draw.rectangle((x, y, x + 3, y + 3), fill=(shade, shade, shade))
    return image


def _synthetic_dense_table_scanline_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for x in range(18, 246, 18):
        draw.line((x, 18, x, 162), fill=(48, 48, 48), width=1)
    for y in range(18, 162, 12):
        draw.line((18, y, 246, y), fill=(48, 48, 48), width=1)
    draw.rectangle((16, 132, 244, 133), fill=(222, 222, 218))
    return image


def _box_luma(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    return float(ImageStat.Stat(image.crop(box).convert("L")).mean[0])


def _corner_test_box(corner: str, size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    if corner == "top_left":
        return (0, 0, 54, 54)
    if corner == "top_right":
        return (width - 54, 0, width, 54)
    if corner == "bottom_left":
        return (0, height - 54, 54, height)
    if corner == "bottom_right":
        return (width - 54, height - 54, width, height)
    raise ValueError(f"unsupported corner: {corner}")


def _changed_ratio_for_test(before: Image.Image, after: Image.Image, box: tuple[int, int, int, int]) -> float:
    before_l = before.crop(box).convert("L")
    after_l = after.crop(box).convert("L")
    diff = ImageChops.difference(before_l, after_l)
    changed = sum(diff.point(lambda value: 255 if value > 8 else 0).histogram()[1:])
    return changed / max(1, before_l.width * before_l.height)


def _mean_channel_spread(image: Image.Image) -> float:
    means = ImageStat.Stat(image.convert("RGB")).mean
    return max(means) - min(means)


def _mean_luma_delta(before: Image.Image, after: Image.Image) -> float:
    before_mean = ImageStat.Stat(before.convert("L")).mean[0]
    after_mean = ImageStat.Stat(after.convert("L")).mean[0]
    return abs(before_mean - after_mean)


def _write_minimal_scan_report(
    path: Path,
    findings: list[dict[str, object]],
    *,
    files: list[dict[str, object]] | None = None,
    rules_profile: dict[str, object] | None = None,
) -> None:
    profile = rules_profile or {
        "name": "default",
        "version": "scan-qc.phase1.v1",
        "source": "builtin",
        "thresholds": {"min_dpi": 200, "name_pattern": None, "quality": {}},
        "rules": {},
    }
    payload = {
        "schema_version": "scan-qc.phase1.v1",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "summary": {"total_findings": len(findings)},
        "manifest": {"rules_profile": profile},
        "files": files or [],
        "findings": findings,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _sample_file(index: int, *, relative_path: str | None = None, sha256: str | None = None) -> dict[str, object]:
    path = relative_path or f"batch/page-{index:03d}.tif"
    return {
        "relative_path": path,
        "filename": Path(path).name,
        "manifest_order_index": index,
        "manifest_sequence": index + 1,
        "openable": True,
        "format": "TIFF",
        "width": 2400,
        "height": 3200,
        "dpi_x": 300,
        "dpi_y": 300,
        "color_mode": "RGB",
        "orientation_class": "portrait",
        "frame_count": 1,
        "sha256": sha256 or f"{index:064x}",
    }


def _rule_count(report: dict[str, object], rule: str) -> int:
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        return 0
    return sum(1 for finding in findings if isinstance(finding, dict) and finding.get("rule") == rule)


def _benchmark_run_stub(
    run_index: int,
    repeat_index: int,
    requested_workers: int,
    *,
    scan_rate: float,
    processing_rate: float,
) -> dict[str, object]:
    return {
        "run_index": run_index,
        "repeat_index": repeat_index,
        "requested_workers": requested_workers,
        "effective_workers": requested_workers,
        "worker_mode": "serial" if requested_workers == 1 else "parallel",
        "operations": {
            "deskew": True,
            "auto_crop": True,
            "trim_dark_border": True,
            "despeckle": True,
        },
        "scan_only": False,
        "total_files": 10,
        "openable_files": 10,
        "finding_severity_counts": {"P0": 0, "P1": 0, "P2": 0},
        "finding_rule_counts": {},
        "processing": {
            "enabled": True,
            "processed_files": 10,
            "failed_files": 0,
            "skipped_files": 0,
            "elapsed_seconds": 1.0,
            "processed_files_per_minute": processing_rate,
            "total_files_per_minute": processing_rate,
            "effective_workers": requested_workers,
            "worker_mode": "serial" if requested_workers == 1 else "parallel",
        },
        "scan": {
            "elapsed_seconds": 1.0,
            "files_per_minute": scan_rate,
            "openable_files_per_minute": scan_rate,
        },
    }


def _write_fake_provider(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "import json\n"
        "records = " + repr(records) + "\n"
        "for _ in __import__('sys').stdin:\n"
        "    pass\n"
        "for record in records:\n"
        "    print(json.dumps(record, ensure_ascii=False))\n",
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_public_safe_validation_index_fixtures(root: Path) -> None:
    _write_json(
        root / "frontend_workbench_validation.json",
        {
            "status": "pass",
            "counts": {"required_regions": 8},
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
            "error_count": 0,
            "errors": [],
        },
    )
    _write_json(root / "release_readiness_summary.json", _release_readiness_bundle_payload())
    _write_json(root / "release_candidate_summary.json", _release_candidate_bundle_payload())
    _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
    _write_json(root / "review_decision_verification_summary.json", _review_decision_verification_bundle_payload())
    _write_json(root / "final_production_handoff_summary.json", _final_handoff_bundle_payload())


def _write_artifact_readiness_required_fixtures(root: Path) -> None:
    _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
    handoff = _final_handoff_bundle_payload()
    handoff["generated_at"] = "2026-01-01T00:00:00+00:00"
    _write_json(root / "final_production_handoff_summary.json", handoff)
    _write_json(
        root / "public_safe_validation_index.json",
        {
            "schema_version": "scan-qc.public-safe-validation-index.v1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "status": "pass",
            "checks_passed": 4,
            "checks_failed": 0,
            "summary": {"artifacts_present": 4, "artifacts_failed": 0, "artifacts_missing": 0},
            "blocking_items": [],
            "privacy": {"aggregate_only": True, "redacts_private_values": True},
            "sensitive_values_omitted": True,
        },
    )
    _write_json(
        root / "workbench_public_summary.json",
        {
            "schema_version": "scan-qc.workbench-public-summary.v1",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "status": "pass",
            "ready": True,
            "checks_passed": 4,
            "checks_failed": 0,
            "blocking_items": [],
            "privacy": {"aggregate_only": True, "redacts_private_values": True},
            "sensitive_values_omitted": True,
        },
    )


def _documented_archive_scan_qc_commands(
    doc_path: Path,
    expected_commands: tuple[str, ...],
    replacements: dict[str, str],
) -> list[list[str]]:
    text = doc_path.read_text(encoding="utf-8")
    commands: list[list[str]] = []
    for match in re.finditer(r"^[ \t]*```bash\n(.*?)^[ \t]*```", text, re.DOTALL | re.MULTILINE):
        block = match.group(1).replace("\\\n", " ")
        if "archive-scan-qc" not in block:
            continue
        argv = shlex.split(block)
        start_indexes = [index for index, value in enumerate(argv) if value == "archive-scan-qc"]
        for start, end in zip(start_indexes, start_indexes[1:] + [len(argv)]):
            command = argv[start + 1:end]
            if not command or command[0] not in expected_commands:
                continue
            for index, value in enumerate(command):
                for placeholder, replacement in replacements.items():
                    if value.startswith(placeholder):
                        command[index] = replacement + value[len(placeholder):]
            commands.append(command)
    return commands


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


def _aggregate_evidence_bundle_payload(*, status: str, blocking_codes: list[str] | None = None) -> dict[str, object]:
    blockers = [
        {"artifact": "release_candidate_summary.json", "code": code}
        for code in (blocking_codes or [])
    ]
    return {
        "schema_version": "scan-qc.aggregate-evidence-bundle.v1",
        "status": status,
        "checks_passed": 6,
        "checks_failed": len(blockers),
        "blocking_items": blockers,
        "artifact_presence": {
            "release_candidate_summary.json": {"present": True, "required": True, "status": status},
            "release_readiness_summary.json": {"present": False, "required": False, "status": "optional_missing"},
            "review_decision_verification_summary.json": {
                "present": True,
                "required": False,
                "status": "pass",
                "checks_passed": 1,
                "checks_failed": 0,
                "blocking_count": 0,
                "warning_count": 0,
                "privacy_status": "pass",
            },
        },
        "privacy": {
            "aggregate_only": True,
            "private_indicators_found": False,
            "private_indicator_count": 0,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
        },
        "sensitive_values_omitted": True,
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


def _review_decision_export_fixture(
    decisions: tuple[str, ...] = ("accepted_issue", "false_positive", "fixed_externally"),
) -> dict[str, object]:
    decision_rows = [
        {"scope": "finding", "local_id": f"RID{index:04d}", "decision": decision}
        for index, decision in enumerate(decisions, start=1)
    ]
    counts = {decision: 0 for decision in ("pending", "accepted_issue", "false_positive", "fixed_externally", "needs_rescan", "blocked")}
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


def _final_handoff_bundle_payload() -> dict[str, object]:
    return {
        "schema_version": "scan-qc.final-production-handoff-summary.v1",
        "status": "pass",
        "ready_for_handoff": True,
        "checks_passed": 7,
        "checks_failed": 0,
        "blocking_item_count": 0,
        "blocking_items": [],
        "artifact_status_summary": {
            "aggregate_evidence_bundle_summary.json": {"present": True, "required": True, "status": "pass"},
            "release_candidate_summary.json": {"present": True, "required": False, "status": "pass"},
            "review_decision_verification_summary.json": {
                "present": True,
                "required": False,
                "status": "pass",
                "checks_passed": 1,
                "checks_failed": 0,
                "blocking_count": 0,
                "warning_count": 0,
                "privacy_status": "pass",
            },
        },
        "privacy": {
            "aggregate_only": True,
            "private_indicators_found": False,
            "private_indicator_count": 0,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
        },
        "sensitive_values_omitted": True,
    }


def _dark_pixel_count(image: Image.Image) -> int:
    grayscale = image.convert("L")
    return sum(grayscale.histogram()[:31])


if __name__ == "__main__":
    unittest.main()
