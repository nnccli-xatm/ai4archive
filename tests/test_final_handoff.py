from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.final_handoff import build_final_handoff_summary
from archive_scan_qc.review_decisions import build_review_decision_verification_summary

from test_evidence_bundle import (
    _deep_inspection_candidate_bundle_payload,
    _release_candidate_bundle_payload,
    _review_decision_export_fixture,
    _review_decision_verification_bundle_payload,
    _write_json,
)
from test_release_summaries import (
    _load_release_candidate_module,
    _release_candidate_acceptance,
    _release_candidate_baseline,
    _release_candidate_readiness,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


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


class FinalHandoffTests(unittest.TestCase):
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
        self.assertEqual(digest["cleanup_quality_warnings_zh"], [])
        self.assertEqual(digest["cleanup_quality_warning_codes"], [])
        self.assertIn("release_candidate_summary", digest["reused_aggregate_fields"])
        self.assertNotIn("page_0001", raw)
        self.assertNotIn("/Users/private/archive", raw)

    def test_final_handoff_summary_promotes_chinese_cleanup_quality_warning_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release_payload = _release_candidate_bundle_payload()
            release_payload["warning_items"] = [
                {
                    "code": "full_chain_cleanup_low_improved_ratio",
                    "title_zh": "清理改善比例偏低",
                    "message_zh": "全链路清理在聚合结果中的改善比例偏低，请在导出前复核清理参数与抽样结果。",
                    "next_step_zh": "检查清理参数并抽检代表性处理结果；如偏差持续，请补充聚合证据后重跑验收。",
                    "observed": {"ratios": {"improved_ratio": 0.35}},
                },
                {
                    "code": "full_chain_cleanup_high_reverted_ratio",
                    "title_zh": "清理回退比例偏高",
                    "message_zh": "全链路清理在聚合结果中的回退比例偏高，请在导出前复核清理参数与抽样结果。",
                    "next_step_zh": "检查清理参数并抽检代表性处理结果；如偏差持续，请补充聚合证据后重跑验收。",
                    "observed": {"ratios": {"reverted_ratio": 0.3}},
                },
            ]
            _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
            _write_json(root / "release_candidate_summary.json", release_payload)

            summary = build_final_handoff_summary(root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False)

        digest = summary["handoff_blocker_summary_zh"]
        self.assertEqual(
            digest["cleanup_quality_warning_codes"],
            ["full_chain_cleanup_low_improved_ratio", "full_chain_cleanup_high_reverted_ratio"],
        )
        self.assertEqual(len(digest["cleanup_quality_warnings_zh"]), 2)
        self.assertEqual(digest["cleanup_quality_warnings_zh"][0]["code"], "full_chain_cleanup_low_improved_ratio")
        self.assertIn("清理改善比例偏低", digest["cleanup_quality_warnings_zh"][0]["title_zh"])
        self.assertIn("warning_items", digest["reused_aggregate_fields"])
        self.assertNotIn("page_0001", raw)
        self.assertNotIn("/Users/private/archive", raw)
        self.assertNotIn("observed", raw)

    def test_final_handoff_summary_promotes_cleanup_warnings_from_release_candidate_generated_from_acceptance(self) -> None:
        module = _load_release_candidate_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            acceptance = _release_candidate_acceptance(status="pass")
            acceptance["warning_items"] = [
                {
                    "code": "full_chain_cleanup_high_reverted_ratio",
                    "title_zh": "清理回退比例偏高",
                    "message_zh": "全链路清理在聚合结果中的回退比例偏高，请在导出前复核清理参数与抽样结果。",
                    "next_step_zh": "检查清理参数并抽检代表性处理结果；如偏差持续，请补充聚合证据后重跑验收。",
                    "observed": {"ratios": {"reverted_ratio": 0.3}},
                }
            ]
            release_payload = module.build_release_candidate_summary(
                aggregate_baseline_summary=_release_candidate_baseline(),
                acceptance_summary=acceptance,
                release_readiness_summary=_release_candidate_readiness(status="pass"),
                cleanup_requested=True,
                generated_at="2026-01-01T00:00:00+00:00",
            )
            _write_json(root / "aggregate_evidence_bundle_summary.json", _aggregate_evidence_bundle_payload(status="pass"))
            _write_json(root / "release_candidate_summary.json", release_payload)

            summary = build_final_handoff_summary(root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False)

        digest = summary["handoff_blocker_summary_zh"]
        self.assertEqual(digest["cleanup_quality_warning_codes"], ["full_chain_cleanup_high_reverted_ratio"])
        self.assertEqual(digest["cleanup_quality_warnings_zh"][0]["code"], "full_chain_cleanup_high_reverted_ratio")
        self.assertNotIn("\"ratios\"", raw)
        self.assertNotIn("0.3", raw)

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


if __name__ == "__main__":
    unittest.main()
