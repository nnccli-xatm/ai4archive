from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.reports import build_review_summary
from archive_scan_qc.rules import load_rules_profile


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


class RulesCalibrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
