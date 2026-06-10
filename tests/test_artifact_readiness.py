from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.artifact_readiness import build_artifact_readiness_checklist
from archive_scan_qc.cli import main
from archive_scan_qc.workbench_summary import build_workbench_public_summary

from test_evidence_bundle import _deep_inspection_candidate_bundle_payload, _write_json
from test_final_handoff import _aggregate_evidence_bundle_payload
from test_validation_index import _final_handoff_bundle_payload, _image_processing_capability_smoke_payload


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


class ArtifactReadinessTests(unittest.TestCase):
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

    def test_artifact_readiness_checklist_propagates_image_smoke_blocker(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _write_artifact_readiness_required_fixtures(root)
                _write_json(
                    root / "image_processing_capability_smoke.json",
                    _image_processing_capability_smoke_payload(
                        status="fail",
                        blocking_codes=["text_edge_energy_not_improved"],
                    ),
                )

                summary = build_artifact_readiness_checklist(
                    evidence_dir=root,
                    generated_at="2026-01-01T00:00:00+00:00",
                )
                raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

            self.assertEqual(summary["status"], "fail")
            self.assertFalse(summary["ready"])
            row = summary["artifact_readiness_checklist"]["image_processing_capability_smoke.json"]
            self.assertTrue(row["present"])
            self.assertEqual(row["status"], "fail")
            self.assertIn("text_edge_energy_not_improved", summary["blocking_counts_by_code"])
            self.assertNotIn(str(root), raw)

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


if __name__ == "__main__":
    unittest.main()
