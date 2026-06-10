from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.private_validation import build_private_validation_aggregate

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "docs" / "fixtures" / "private-validation-aggregate"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class PrivateValidationAggregateTests(unittest.TestCase):
    def test_private_validation_aggregate_groups_metrics_without_private_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="private-validation-") as temp_dir:
            root = Path(temp_dir)
            _write_json(
                root / "local-result-1.json",
                _private_validation_payload(
                    group_id="low_contrast_text",
                    status="pass",
                    counts={"total_files": 3, "processed_files": 3, "quality_gain_files": 3},
                    metrics={"text_contrast_delta": {"count": 3, "average": 0.25, "max": 0.4}},
                    risk_codes=["quality_gain_measured"],
                    quality_signal_status="measured_with_changes",
                ),
            )
            _write_json(
                root / "local-result-2.json",
                _private_validation_payload(
                    group_id="mixed_content_guardrail",
                    status="fail",
                    counts={"total_files": 2, "processed_files": 2, "guardrail_failed_files": 1},
                    metrics={"color_mean_abs_delta": {"count": 2, "average": 0.02, "max": 0.03}},
                    risk_codes=["color_shift_risk"],
                    quality_signal_status="measured_no_quality_operations",
                ),
            )

            summary = build_private_validation_aggregate(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["schema_version"], "scan-qc.private-validation-aggregate.v1")
        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["validation_inputs"]["provided_count"], 2)
        self.assertEqual(summary["validation_inputs"]["raw_sensitive_payload_count"], 2)
        self.assertEqual(summary["group_count"], 2)
        groups = {group["group_id"]: group for group in summary["group_summaries"]}
        self.assertEqual(groups["low_contrast_text"]["counts"]["quality_gain_items"], 3)
        self.assertEqual(
            groups["low_contrast_text"]["quality_signal_status_counts"],
            [{"status": "measured_with_changes", "count": 1}],
        )
        text_metrics = {metric["metric_id"]: metric for metric in groups["low_contrast_text"]["metric_summary"]}
        self.assertEqual(text_metrics["text_contrast_delta"]["count"], 3)
        self.assertEqual(text_metrics["text_contrast_delta"]["average"], 0.25)
        self.assertEqual(text_metrics["text_contrast_delta"]["max"], 0.4)
        self.assertEqual(groups["mixed_content_guardrail"]["failed_validation_inputs"], 1)
        self.assertEqual(
            groups["mixed_content_guardrail"]["quality_signal_status_counts"],
            [{"status": "measured_no_quality_operations", "count": 1}],
        )
        self.assertEqual(
            summary["quality_signal_status_counts"],
            [
                {"status": "measured_no_quality_operations", "count": 1},
                {"status": "measured_with_changes", "count": 1},
            ],
        )
        risk_codes = {item["risk_code"]: item["count"] for item in summary["risk_code_counts"]}
        self.assertEqual(risk_codes["color_shift_risk"], 1)
        self.assertIn("validation_group_failed", summary["blocking_codes"])
        self.assertTrue(summary["privacy"]["aggregate_only"])
        for forbidden in (
            "D:\\private\\客户001.png",
            "客户001.png",
            "abcdef1234567890abcdef1234567890",
            "private OCR text",
            "preview_object_url",
        ):
            self.assertNotIn(forbidden, raw)

    def test_private_validation_aggregate_omits_unsafe_group_and_risk_labels(self) -> None:
        with tempfile.TemporaryDirectory(prefix="private-validation-") as temp_dir:
            root = Path(temp_dir)
            _write_json(
                root / "local-result.json",
                _private_validation_payload(
                    group_id="private_scan_001.png",
                    status="pass",
                    risk_codes=["D:/private/private_scan_001.png"],
                ),
            )

            summary = build_private_validation_aggregate(input_dir=root, generated_at="2026-01-01T00:00:00+00:00")
            raw = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["group_summaries"][0]["group_id"], "unclassified")
        risk_codes = {item["risk_code"]: item["count"] for item in summary["risk_code_counts"]}
        self.assertEqual(risk_codes["unsafe_group_label_omitted"], 1)
        self.assertEqual(risk_codes["unsafe_risk_code_omitted"], 1)
        self.assertNotIn("private_scan_001.png", raw)
        self.assertNotIn("D:/private", raw)

    def test_private_validation_aggregate_cli_writes_public_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="private-validation-cli-") as temp_dir:
            root = Path(temp_dir)
            input_path = root / "local-result.json"
            output_path = root / "private_validation_aggregate_summary.json"
            _write_json(input_path, _private_validation_payload(group_id="low_contrast_text", status="pass"))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "private-validation-aggregate",
                        "--file",
                        str(input_path),
                        "--out",
                        str(output_path),
                    ]
                )
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["group_count"], 1)
        self.assertIn("Private validation aggregate summary:", stdout.getvalue())

    def test_private_validation_aggregate_fixture_provides_release_gate_shape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="private-validation-fixture-") as temp_dir:
            output_path = Path(temp_dir) / "private_validation_aggregate_summary.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "private-validation-aggregate",
                        "--input-dir",
                        str(FIXTURE_DIR),
                        "--out",
                        str(output_path),
                    ]
                )
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["schema_version"], "scan-qc.private-validation-aggregate.v1")
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["validation_inputs"]["provided_count"], 2)
        self.assertEqual(payload["validation_inputs"]["raw_sensitive_payload_count"], 0)
        self.assertEqual(payload["group_count"], 2)
        groups = {group["group_id"]: group for group in payload["group_summaries"]}
        self.assertEqual(groups["text_clean_readability"]["counts"]["quality_gain_items"], 3)
        self.assertEqual(groups["photo_mixed_guardrail"]["counts"]["overprocessing_risk_items"], 0)
        text_metrics = {metric["metric_id"]: metric for metric in groups["text_clean_readability"]["metric_summary"]}
        self.assertEqual(text_metrics["text_contrast_delta"]["average"], 0.31)
        self.assertIn("Private validation aggregate summary:", stdout.getvalue())
        for forbidden in ("D:\\", "/Users/", "客户", "abcdef123456", "private OCR text", "blob:http"):
            self.assertNotIn(forbidden, raw)


def _private_validation_payload(
    *,
    group_id: str,
    status: str,
    counts: dict[str, int] | None = None,
    metrics: dict[str, dict[str, float]] | None = None,
    risk_codes: list[str] | None = None,
    quality_signal_status: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "scan-qc.private-validation-result.local.v1",
        "public_group_id": group_id,
        "status": status,
        "counts": counts or {"total_files": 1, "processed_files": 1},
        "quality_metrics": metrics or {"text_contrast_delta": {"count": 1, "average": 0.2, "max": 0.2}},
        "risk_codes": risk_codes or [],
        "local_rows": [
            {
                "absolute_path": "D:\\private\\客户001.png",
                "filename": "客户001.png",
                "sha256": "abcdef1234567890abcdef1234567890",
                "ocr_text": "private OCR text",
                "preview_object_url": "blob:http://localhost/private-preview",
            }
        ],
        "privacy": {"aggregate_only": False, "contains_paths": True},
    }
    if quality_signal_status is not None:
        payload["quality_signal"] = {"status": quality_signal_status}
    return payload


if __name__ == "__main__":
    unittest.main()
