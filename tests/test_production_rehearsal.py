from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.local_workbench import DEFAULT_METADATA_DIRNAME


class ProductionRehearsalTests(unittest.TestCase):
    def test_cli_production_rehearsal_generates_synthetic_workbench_artifacts(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "rehearsal"

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(["production-rehearsal", "--root", str(root), "--workers", "1"])

                self.assertEqual(exit_code, 0)
                output = stdout.getvalue()
                self.assertIn("本机生产演练已生成。", output)
                self.assertIn("下一步:", output)
                self.assertIn("production-rehearsal --launch-workbench", output)
                self.assertIn("工作台会自动带入演练文件夹", output)
                self.assertIn("只使用合成图片", output)
                self.assertNotIn("JSON", output)
                self.assertNotIn("schema", output)
                self.assertNotIn("点击加载本机状态", output)
                self.assertNotIn("私有图片文件夹", output)

                input_dir = root / "synthetic_input"
                derivatives_dir = root / "derivatives"
                metadata_dir = derivatives_dir / DEFAULT_METADATA_DIRNAME
                self.assertEqual(len(list(input_dir.glob("SYNTHETIC_BATCH_*.png"))), 3)
                self.assertTrue((derivatives_dir / "images" / "SYNTHETIC_BATCH_0001_clean.png").exists())
                self.assertTrue((metadata_dir / "production_run_summary.json").exists())
                self.assertTrue((metadata_dir / "production_run_progress.json").exists())
                self.assertTrue((metadata_dir / "processing_review_package.json").exists())
                self.assertTrue((metadata_dir / "production_review_queue.json").exists())

                summary = json.loads((metadata_dir / "production_run_summary.json").read_text(encoding="utf-8"))
                progress = json.loads((metadata_dir / "production_run_progress.json").read_text(encoding="utf-8"))
                queue = json.loads((metadata_dir / "production_review_queue.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["schema_version"], "scan-qc.production-run.v1")
                self.assertEqual(progress["schema_version"], "scan-qc.production-run-progress.v1")
                self.assertEqual(queue["schema_version"], "scan-qc.production-review-queue.v1")
                self.assertEqual(summary["operator_summary"]["total_source_images"], 3)
                self.assertGreaterEqual(queue["summary"]["total_items"], 1)
                self.assertTrue(queue["summary"]["ready_for_operator_review"])
                self.assertTrue(queue["privacy"]["local_only"])
                self.assertFalse(queue["privacy"]["contains_image_bytes"])


if __name__ == "__main__":
    unittest.main()
