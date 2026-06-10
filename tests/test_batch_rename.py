from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from archive_scan_qc.batch_rename import (
    BATCH_RENAME_APPLY_JSON,
    BATCH_RENAME_PLAN_JSON,
    BATCH_RENAME_ROLLBACK_JSON,
    apply_batch_rename_plan,
    write_batch_rename_plan,
)
from archive_scan_qc.cli import main


class BatchRenameTests(unittest.TestCase):
    def test_plan_and_apply_write_logs_and_rollback_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="batch-rename-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            out_dir = root / "out"
            input_dir.mkdir()
            (input_dir / "A001.jpg").write_text("page 1", encoding="utf-8")
            (input_dir / "nested").mkdir()
            (input_dir / "nested" / "A002.jpg").write_text("page 2", encoding="utf-8")
            mapping = root / "mapping.csv"
            mapping.write_text(
                "source_relative_path,new_relative_path\n"
                "A001.jpg,renamed/B001.jpg\n"
                "nested/A002.jpg,renamed/B002.jpg\n",
                encoding="utf-8",
            )

            plan_json, plan_csv, plan_xlsx, plan = write_batch_rename_plan(input_dir, mapping, out_dir)
            apply_json, apply_csv, apply_xlsx, rollback_json, applied = apply_batch_rename_plan(plan_json)

            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["summary"]["ready_count"], 2)
            self.assertTrue(plan_csv.is_file())
            self.assertTrue(plan_xlsx.is_file())
            self.assertFalse((input_dir / "A001.jpg").exists())
            self.assertTrue((input_dir / "renamed" / "B001.jpg").is_file())
            self.assertEqual(applied["summary"]["applied_count"], 2)
            self.assertTrue(apply_csv.is_file())
            self.assertTrue(apply_xlsx.is_file())
            rollback = json.loads(rollback_json.read_text(encoding="utf-8"))
            self.assertEqual(rollback["schema_version"], "scan-qc.batch-rename-rollback.v1")
            self.assertEqual(rollback["rows"][0]["source_relative_path"], "renamed/B001.jpg")
            self.assertEqual(rollback["rows"][0]["target_relative_path"], "A001.jpg")
            with ZipFile(plan_xlsx) as archive:
                self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
            self.assertEqual(plan_json.name, BATCH_RENAME_PLAN_JSON)
            self.assertEqual(apply_json.name, BATCH_RENAME_APPLY_JSON)
            self.assertEqual(rollback_json.name, BATCH_RENAME_ROLLBACK_JSON)

    def test_plan_blocks_duplicate_target_existing_target_and_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="batch-rename-blocked-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            out_dir = root / "out"
            input_dir.mkdir()
            (input_dir / "A001.jpg").write_text("page 1", encoding="utf-8")
            (input_dir / "A002.jpg").write_text("page 2", encoding="utf-8")
            (input_dir / "exists.jpg").write_text("existing", encoding="utf-8")
            mapping = root / "mapping.csv"
            mapping.write_text(
                "source_relative_path,target_relative_path\n"
                "A001.jpg,duplicate.jpg\n"
                "A002.jpg,duplicate.jpg\n"
                "../outside.jpg,new.jpg\n"
                "missing.jpg,exists.jpg\n",
                encoding="utf-8",
            )

            plan_json, _plan_csv, _plan_xlsx, plan = write_batch_rename_plan(input_dir, mapping, out_dir)

            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(plan["summary"]["blocking_count"], 4)
            self.assertIn("duplicate_target", {row["reason_code"] for row in plan["rows"]})
            self.assertIn("unsafe_path", {row["reason_code"] for row in plan["rows"]})
            self.assertIn("source_missing", {row["reason_code"] for row in plan["rows"]})
            with self.assertRaisesRegex(ValueError, "blocked"):
                apply_batch_rename_plan(plan_json)
            self.assertTrue((input_dir / "A001.jpg").is_file())
            self.assertTrue((input_dir / "A002.jpg").is_file())

    def test_cli_plan_returns_nonzero_for_blocked_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="batch-rename-cli-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            out_dir = root / "out"
            input_dir.mkdir()
            mapping = root / "mapping.csv"
            mapping.write_text("source_relative_path,new_relative_path\nmissing.jpg,new.jpg\n", encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "batch-rename-plan",
                        "--input",
                        str(input_dir),
                        "--mapping-csv",
                        str(mapping),
                        "--out",
                        str(out_dir),
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("Rows blocked: 1", stdout.getvalue())
            self.assertTrue((out_dir / BATCH_RENAME_PLAN_JSON).is_file())


if __name__ == "__main__":
    unittest.main()
