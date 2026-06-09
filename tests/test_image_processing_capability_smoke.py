from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.image_processing_capability_smoke import (
    IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON,
    SCHEMA_VERSION,
    run_image_processing_capability_smoke,
)


class ImageProcessingCapabilitySmokeTests(unittest.TestCase):
    def test_smoke_runs_real_processing_and_writes_public_safe_aggregate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-processing-capability-smoke-test-") as temp_dir:
            out_dir = Path(temp_dir) / "out"
            path, payload = run_image_processing_capability_smoke(
                output_path=out_dir,
                generated_at="2026-06-09T00:00:00+00:00",
            )
            raw = path.read_text(encoding="utf-8") if path else ""

            self.assertEqual(path, out_dir / IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON)
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["blocking_codes"], [])
            self.assertTrue(payload["processing_run"]["image_processing_run"])
            self.assertFalse(payload["processing_run"]["source_images_modified"])
            self.assertFalse(payload["privacy"]["private_inputs_read"])
            self.assertFalse(payload["privacy"]["contains_paths"])
            self.assertFalse(payload["privacy"]["contains_filenames"])
            self.assertFalse(payload["privacy"]["contains_hashes"])
            self.assertEqual(payload["counts"]["synthetic_fixture_count"], 5)
            self.assertEqual(payload["counts"]["processed_files"], 5)
            self.assertEqual(payload["counts"]["failed_files"], 0)
            self.assertEqual(payload["counts"]["retry_list_files"], 0)
            self.assertEqual(payload["counts"]["guardrail_failed_files"], 0)
            self.assertGreaterEqual(payload["operation_counts"]["despeckled_files"], 1)
            self.assertGreater(payload["backend_summary"]["despeckle_backend_modes"]["fallback"], 0)

            self.assertNotIn(temp_dir, raw)
            self.assertNotIn("synthetic_fixture_001", raw)
            self.assertNotIn("source_relative_path", raw)
            self.assertNotIn("output_sha256", raw)

    def test_cli_writes_smoke_summary_without_private_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-processing-capability-smoke-cli-") as temp_dir:
            out_dir = Path(temp_dir) / "smoke"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["image-processing-capability-smoke", "--out", str(out_dir), "--workers", "1"])
            payload = json.loads((out_dir / IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON).read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["status"], "pass")
        self.assertIn("Image processing run: yes", stdout.getvalue())
        self.assertIn("Private images read: no", stdout.getvalue())
        self.assertIn("Provider commands run: no", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
