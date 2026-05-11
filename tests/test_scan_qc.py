from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from archive_scan_qc import __version__
from archive_scan_qc.acceptance import build_acceptance_summary
from archive_scan_qc.benchmark import _recommendations
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
from archive_scan_qc.processing import (
    ProcessingOptions,
    _despeckle_candidate_points,
    _deskew_candidate_scores,
    _horizontal_projection_variance,
    process_images,
)
from archive_scan_qc.processing_plan import build_processing_plan
from archive_scan_qc.processing_review import build_processing_review_package
from archive_scan_qc.reports import build_review_summary, write_reports, write_review_export, write_review_summary
from archive_scan_qc.rework import build_rework_action_list, write_rework_action_list
from archive_scan_qc.rule_registry import RULE_REGISTRY, validate_provider_rule_id
from archive_scan_qc.rules import RulesProfileError, load_rules_profile
from archive_scan_qc.sampling import build_acceptance_sampling_export
from archive_scan_qc.scanner import ScanConfig, scan_batch
from archive_scan_qc.validation_index import build_public_safe_validation_index


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


def _load_frontend_issue_driver_module():
    spec = importlib.util.spec_from_file_location("frontend_issue_driver", FRONTEND_ISSUE_DRIVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load frontend_issue_driver.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

            summary = build_evidence_bundle_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertGreater(summary["checks_passed"], 0)
        self.assertEqual(summary["checks_failed"], 0)
        self.assertFalse(summary["privacy"]["private_indicators_found"])

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

            summary = build_final_handoff_summary(root, generated_at="2026-01-01T00:00:00+00:00")

        self.assertEqual(summary["status"], "pass")
        self.assertTrue(summary["ready_for_handoff"])
        self.assertEqual(summary["blocking_item_count"], 0)
        self.assertEqual(summary["artifact_status_summary"]["aggregate_evidence_bundle_summary.json"]["status"], "pass")
        self.assertTrue(summary["privacy"]["aggregate_only"])

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
        self.assertEqual(summary["summary"]["artifacts_present"], 5)
        self.assertEqual(summary["summary"]["artifacts_failed"], 0)
        self.assertEqual(summary["artifact_presence"]["frontend_workbench_validation.json"]["status"], "pass")
        self.assertEqual(summary["artifact_presence"]["final_production_handoff_summary.json"]["status"], "pass")
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
                ]
            )
            payload = module.run_aggregate_baseline(args)

            json_path = output_dir / "aggregate_baseline_summary.json"
            self.assertTrue(json_path.exists())
            self.assertEqual(payload["schema_version"], "scan-qc.aggregate-baseline.v1")
            self.assertEqual(payload["target_environment"]["validation_target"], "puersai-hpc")
            self.assertFalse(payload["target_environment"]["gpu_acceleration_used"])
            self.assertEqual(payload["worker_settings"]["requested_workers"], 1)
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
        self.assertGreaterEqual(payload["aggregate_sampling_counts"]["effective_sample_ratio"], 0.05)
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
            self.assertIn("image bytes", payload["privacy"]["omits"])
            self.assertIn(private_path, csv_text)
            self.assertIn(private_hash, csv_text)
            self.assertNotIn("private message", json_path.read_text(encoding="utf-8"))

    def test_acceptance_sampling_rejects_ratio_below_five_percent(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 5.00%"):
            build_acceptance_sampling_export({"files": [_sample_file(1)], "findings": []}, sample_ratio=0.01)

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
            self.assertEqual(audit["metrics"]["pixel_change_ratio"]["count"], 2)

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
            self.assertTrue((process_dir / duplicate["output_relative_path"]).exists())

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

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            self.assertFalse(records["A001_0001.png"]["deskewed"])
            self.assertFalse(records["A001_0002.png"]["deskewed"])
            self.assertIn(records["A001_0001.png"]["deskew_reason"], {"blank page", "low contrast"})
            self.assertEqual(records["A001_0002.png"]["deskew_reason"], "low contrast")

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
            self.assertEqual(record["original_size"], [80, 60])
            self.assertEqual(record["output_size"], [60, 44])
            self.assertGreater(record["processing_audit"]["crop_ratio"], 0.0)
            self.assertEqual(record["processing_warnings"], [])
            self.assertEqual(processed_size, (60, 44))
            self.assertIn("auto_crop_conservative", record["operations"])

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
            self.assertIn("dark_border_trim_disabled", record["operations"])
            self.assertIn("despeckle_disabled", record["operations"])

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

    def test_despeckle_candidate_points_prefilter_isolated_speckles_only(self) -> None:
        mask = Image.new("L", (80, 60), 0)
        draw = ImageDraw.Draw(mask)
        draw.line((10, 30, 70, 30), fill=255, width=2)
        draw.rectangle((15, 12, 20, 17), fill=255)
        for point in [(5, 5), (20, 8), (74, 12), (40, 50)]:
            mask.putpixel(point, 255)

        self.assertEqual(
            sorted(_despeckle_candidate_points(mask)),
            [(5, 5), (20, 8), (40, 50), (74, 12)],
        )

    def test_despeckle_candidate_points_dense_content_fast_path(self) -> None:
        mask = Image.new("L", (120, 80), 0)
        draw = ImageDraw.Draw(mask)
        for y in range(12, 70, 9):
            draw.rectangle((12, y, 108, y + 2), fill=255)
        for x in range(18, 108, 12):
            draw.line((x, 10, x, 72), fill=255, width=3)

        self.assertEqual(_despeckle_candidate_points(mask), [])

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
            self.assertEqual(record["despeckle_reason"], "no isolated dark pixels found")

    def test_despeckle_preserves_clustered_dark_marks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "A001_0001.png"
            image = Image.new("RGB", (80, 60), "white")
            for point in [(20, 20), (21, 20), (20, 21), (21, 21)]:
                image.putpixel(point, (0, 0, 0))
            image.save(source)

            report = scan_batch(ScanConfig("p1", "b1", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))

            with Image.open(process_dir / "images" / "A001_0001.png") as processed:
                grayscale = processed.convert("L")
                for point in [(20, 20), (21, 20), (20, 21), (21, 21)]:
                    self.assertLessEqual(grayscale.getpixel(point), 5)
            record = manifest["files"][0]
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["despeckle_pixels_changed"], 0)
            self.assertEqual(record["despeckle_reason"], "no isolated dark pixels found")

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
            self.assertFalse(rows["A001_clean.png"]["deskew_candidate"])
            self.assertFalse(rows["A001_clean.png"]["dark_border_trim_candidate"])
            self.assertEqual(rows["A001_broken.png"]["status"], "unopenable")
            self.assertEqual(plan["summary"]["total_files"], 4)
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

            for option in ["--trim-dark-border", "--despeckle"]:
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
            Image.new("RGB", (48, 36), "white").save(batch_one / "SECRET_ONE.png", dpi=(300, 300))
            Image.new("RGB", (48, 36), "white").save(batch_two / "SECRET_TWO.png", dpi=(300, 300))
            plan_path.write_text(
                "batch_id,input_dir,report_dir,process_out,workers,auto_crop,resume_processing\n"
                f"batch-one,{batch_one},reports-one,processed-one,1,true,false\n"
                f"batch-two,{batch_two},reports-two,processed-two,1,false,true\n",
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


def _synthetic_ink_text_page() -> Image.Image:
    image = Image.new("L", (240, 180), 0)
    draw = ImageDraw.Draw(image)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=255)
    return image


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
    _write_json(
        root / "final_production_handoff_summary.json",
        {
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
        },
    )


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
        "blocking_items": [],
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
