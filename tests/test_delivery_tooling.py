from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ISSUE_DRIVER_PATH = REPO_ROOT / "scripts" / "frontend_issue_driver.py"
ISSUE_PLAN_PATH = REPO_ROOT / "scripts" / "generate_issue_plan.py"
OFFLINE_DEPENDENCY_CHECK_PATH = REPO_ROOT / "scripts" / "check_offline_dependencies.py"


def _load_script_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_issue_plan_module():
    return _load_script_module("generate_issue_plan", ISSUE_PLAN_PATH)


def _load_frontend_issue_driver_module():
    return _load_script_module("frontend_issue_driver", FRONTEND_ISSUE_DRIVER_PATH)


def _load_offline_dependency_check_module():
    return _load_script_module("check_offline_dependencies", OFFLINE_DEPENDENCY_CHECK_PATH)


class DeliveryToolingTests(unittest.TestCase):
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
            for name in (
                "ai4archive-0.1.0-py3-none-any.whl",
                "Pillow-10.4.0-cp312-cp312-linux_x86_64.whl",
                "setuptools-69.0.0-py3-none-any.whl",
            ):
                (wheelhouse / name).touch()

            with mock.patch.object(module, "_distribution_version", return_value="10.4.0"), mock.patch.object(
                module, "_importable", return_value=True
            ):
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

            with mock.patch.object(module, "_distribution_version", return_value="10.4.0"), mock.patch.object(
                module, "_importable", return_value=True
            ):
                failure_code, failure_lines = module.check_dependencies(wheelhouse=wheelhouse)
                warning_code, warning_lines = module.check_dependencies(
                    wheelhouse=wheelhouse, wheelhouse_warning_only=True
                )

        failure_output = "\n".join(failure_lines)
        warning_output = "\n".join(warning_lines)
        self.assertEqual(failure_code, 1, failure_output)
        self.assertNotIn("wheelhouse-package: ai4archive", failure_output)
        self.assertIn("wheelhouse-package: setuptools wheels=0 status=missing", failure_output)
        self.assertEqual(warning_code, 0, warning_output)
        self.assertIn("result: pass", warning_output)


if __name__ == "__main__":
    unittest.main()
