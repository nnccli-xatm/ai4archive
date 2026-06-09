from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from archive_scan_qc.capability_probe import CapabilityProbeConfig, run_capability_probe
from archive_scan_qc.cli import main


class CapabilityProbeTests(unittest.TestCase):
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

    def test_capability_probe_inference_readiness_reports_cpu_fallback(self) -> None:
            def missing_package(module_name: str) -> bool:
                return False

            def missing_nvidia(*args, **kwargs):
                raise FileNotFoundError

            report = run_capability_probe(
                package_available=missing_package,
                command_runner=missing_nvidia,
                environ={},
            )
            ir = report["inference_readiness"]
            self.assertFalse(ir["onnxruntime_available"])
            self.assertEqual(ir["inference_backend"], "cpu_only")
            self.assertTrue(ir["cpu_fallback_guaranteed"])
            self.assertEqual(ir["onnxruntime_providers"], [])


if __name__ == "__main__":
    unittest.main()
