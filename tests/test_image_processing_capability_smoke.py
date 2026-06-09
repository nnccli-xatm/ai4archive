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
from archive_scan_qc.processing_quality_summary import (
    PROCESSING_QUALITY_SUMMARY_JSON,
    SCHEMA_VERSION as QUALITY_SCHEMA_VERSION,
)

EXPECTED_SYNTHETIC_FIXTURES = 11


class ImageProcessingCapabilitySmokeTests(unittest.TestCase):
    def test_smoke_runs_real_processing_and_writes_public_safe_aggregate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-processing-capability-smoke-test-") as temp_dir:
            out_dir = Path(temp_dir) / "out"
            path, payload, quality_path = run_image_processing_capability_smoke(
                output_path=out_dir,
                generated_at="2026-06-09T00:00:00+00:00",
            )
            raw = path.read_text(encoding="utf-8") if path else ""
            quality_payload = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path else {}

            self.assertEqual(path, out_dir / IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON)
            self.assertEqual(quality_path, out_dir / PROCESSING_QUALITY_SUMMARY_JSON)
            self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["blocking_codes"], [])
            self.assertTrue(payload["processing_run"]["image_processing_run"])
            self.assertFalse(payload["processing_run"]["source_images_modified"])
            self.assertFalse(payload["privacy"]["private_inputs_read"])
            self.assertFalse(payload["privacy"]["contains_paths"])
            self.assertFalse(payload["privacy"]["contains_filenames"])
            self.assertFalse(payload["privacy"]["contains_hashes"])
            self.assertEqual(payload["counts"]["synthetic_fixture_count"], EXPECTED_SYNTHETIC_FIXTURES)
            self.assertEqual(payload["counts"]["processed_files"], EXPECTED_SYNTHETIC_FIXTURES)
            self.assertEqual(payload["counts"]["failed_files"], 0)
            self.assertEqual(payload["counts"]["retry_list_files"], 0)
            self.assertEqual(payload["counts"]["guardrail_failed_files"], 0)
            self.assertGreaterEqual(payload["operation_counts"]["despeckled_files"], 1)
            self.assertGreaterEqual(payload["operation_counts"]["tone_normalized_files"], 1)
            self.assertGreater(payload["backend_summary"]["despeckle_backend_modes"]["fallback"], 0)
            self.assertEqual(payload["quality_baseline"]["schema_version"], QUALITY_SCHEMA_VERSION)
            self.assertEqual(payload["quality_baseline"]["status"], "pass")
            self.assertEqual(payload["quality_baseline"], quality_payload)
            self.assertTrue(quality_payload["privacy"]["public_safe"])
            self.assertFalse(quality_payload["privacy"]["contains_paths"])
            self.assertFalse(quality_payload["privacy"]["contains_hashes"])
            self.assertFalse(quality_payload["privacy"]["contains_image_content"])
            self.assertEqual(quality_payload["fixture_context"]["fixture_count"], EXPECTED_SYNTHETIC_FIXTURES)
            self.assertIn("mixed_photo_stamp_table_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertIn("bleed_through_page", quality_payload["fixture_context"]["fixture_groups"])
            self.assertEqual(quality_payload["counts"]["processed_files"], EXPECTED_SYNTHETIC_FIXTURES)
            self.assertEqual(quality_payload["counts"]["failed_files"], 0)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["tone_background_delta"]["max"], 6)
            self.assertGreaterEqual(quality_payload["quality_metrics"]["tone_contrast_delta"]["max"], 40)
            self.assertGreaterEqual(
                quality_payload["quality_signal"]["any_quality_operation_changed_files"],
                1,
            )

            self.assertNotIn(temp_dir, raw)
            self.assertNotIn("synthetic_fixture_001", raw)
            self.assertNotIn("source_relative_path", raw)
            self.assertNotIn("output_sha256", raw)
            self.assertNotIn(temp_dir, json.dumps(quality_payload, ensure_ascii=False))
            self.assertNotIn("synthetic_fixture_001", json.dumps(quality_payload, ensure_ascii=False))

    def test_cli_writes_smoke_summary_without_private_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-processing-capability-smoke-cli-") as temp_dir:
            out_dir = Path(temp_dir) / "smoke"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["image-processing-capability-smoke", "--out", str(out_dir), "--workers", "1"])
            payload = json.loads((out_dir / IMAGE_PROCESSING_CAPABILITY_SMOKE_JSON).read_text(encoding="utf-8"))
            quality_payload = json.loads((out_dir / PROCESSING_QUALITY_SUMMARY_JSON).read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(quality_payload["schema_version"], QUALITY_SCHEMA_VERSION)
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(quality_payload["status"], "pass")
        self.assertIn("Processing quality summary:", stdout.getvalue())
        self.assertIn("Quality baseline status: pass", stdout.getvalue())
        self.assertIn("Image processing run: yes", stdout.getvalue())
        self.assertIn("Private images read: no", stdout.getvalue())
        self.assertIn("Provider commands run: no", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
