from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from archive_scan_qc.analysis_provider import _prepare_command
from archive_scan_qc.cli import main
from archive_scan_qc.reports import write_reports
from archive_scan_qc.rule_registry import validate_provider_rule_id
from archive_scan_qc.scanner import ScanConfig, scan_batch


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PROVIDER_EXAMPLE = REPO_ROOT / "examples" / "local_analysis_provider.py"


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


class AnalysisProviderTests(unittest.TestCase):
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

        self.assertEqual(
            argv,
            [
                sys.executable,
                str(REPO_ROOT / "examples" / "local_analysis_provider.py"),
                "--flag",
            ],
        )

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


if __name__ == "__main__":
    unittest.main()
