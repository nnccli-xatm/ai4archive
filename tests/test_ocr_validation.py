from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.ocr_validation import (
    OCR_PREPROCESSING_OCR_VALIDATION_JSON,
    build_ocr_provider_probe,
)
from archive_scan_qc.private_validation import build_private_validation_aggregate


class OcrValidationTests(unittest.TestCase):
    def test_ocr_provider_probe_disabled_is_public_safe(self) -> None:
        payload = build_ocr_provider_probe()
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        self.assertEqual(payload["schema_version"], "scan-qc.ocr-provider-probe.v1")
        self.assertEqual(payload["status"], "disabled")
        self.assertFalse(payload["can_run_ocr"])
        self.assertTrue(payload["privacy"]["public_safe"])
        for forbidden in ("D:\\", "/Users/", "private OCR text", ".png", "sha256"):
            self.assertNotIn(forbidden, raw)

    def test_ocr_preprocessing_validation_disabled_provider_writes_public_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ocr-validation-cli-") as temp_dir:
            output_dir = Path(temp_dir) / "out"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["ocr-preprocessing-ocr-validation", "--out", str(output_dir)])
            payload = json.loads((output_dir / OCR_PREPROCESSING_OCR_VALIDATION_JSON).read_text(encoding="utf-8"))
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["schema_version"], "scan-qc.ocr-preprocessing-ocr-validation.v1")
        self.assertEqual(payload["status"], "pass")
        self.assertFalse(payload["ocr_text_metrics"]["available"])
        self.assertEqual(payload["provider_probe"]["status"], "disabled")
        self.assertEqual(payload["processing_counts"]["ocr_preprocessed_files"], 3)
        self.assertEqual(payload["processing_counts"]["ocr_binary_created_files"], 3)
        self.assertTrue(payload["privacy"]["public_safe"])
        self.assertIn("OCR preprocessing validation summary:", stdout.getvalue())
        for forbidden in ("ocr_synthetic_01.png", "ARCHIVE QUALITY TEST", "D:\\", "/Users/", "sha256"):
            self.assertNotIn(forbidden, raw)

    def test_ocr_preprocessing_validation_require_metric_fails_when_provider_disabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ocr-validation-require-") as temp_dir:
            output_dir = Path(temp_dir) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "ocr-preprocessing-ocr-validation",
                        "--out",
                        str(output_dir),
                        "--require-ocr-metric",
                    ]
                )
            payload = json.loads((output_dir / OCR_PREPROCESSING_OCR_VALIDATION_JSON).read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "fail")
        self.assertIn("ocr_metric_unavailable", payload["blocking_codes"])

    def test_private_validation_aggregate_accepts_ocr_metric_ids_without_ocr_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ocr-private-validation-") as temp_dir:
            root = Path(temp_dir)
            (root / "ocr-result.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.private-validation-result.local.v1",
                        "public_group_id": "ocr_synthetic_quality",
                        "status": "pass",
                        "counts": {"total_files": 3, "processed_files": 3, "quality_gain_files": 3},
                        "quality_metrics": {
                            "cer_relative_reduction": {"count": 3, "average": 0.42, "max": 0.5},
                            "wer_relative_reduction": {"count": 3, "average": 0.25, "max": 0.33},
                        },
                        "risk_codes": ["ocr_quality_gate_passed"],
                    }
                ),
                encoding="utf-8",
            )
            summary = build_private_validation_aggregate(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            metrics = {
                metric["metric_id"]: metric for metric in summary["group_summaries"][0]["metric_summary"]
            }
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(metrics["cer_relative_reduction"]["average"], 0.42)
        self.assertEqual(metrics["wer_relative_reduction"]["average"], 0.25)
        self.assertNotIn("private OCR text", raw)


if __name__ == "__main__":
    unittest.main()
