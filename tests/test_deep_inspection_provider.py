from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.deep_inspection_provider import (
    DeepInspectionProviderConfigError,
    build_deep_inspection_provider_probe,
    parse_deep_inspection_provider_config,
)


class DeepInspectionProviderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
