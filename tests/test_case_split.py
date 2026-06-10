from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from archive_scan_qc.case_split import (
    CASE_SPLIT_APPLY_JSON,
    CASE_SPLIT_PLAN_JSON,
    CASE_SPLIT_ROLLBACK_JSON,
    apply_case_split_plan,
    write_case_split_plan,
)
from archive_scan_qc.cli import main


class CaseSplitTests(unittest.TestCase):
    def test_plan_and_apply_copy_page_ranges_to_case_folders(self) -> None:
        with tempfile.TemporaryDirectory(prefix="case-split-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            target_dir = root / "target"
            out_dir = root / "out"
            input_dir.mkdir()
            for index in range(1, 5):
                (input_dir / f"{index:04d}.jpg").write_text(f"page {index}", encoding="utf-8")
            case_map = root / "cases.csv"
            case_map.write_text(
                "case_name,start_page,end_page\n"
                "case-001,1,2\n"
                "case-002,3,4\n",
                encoding="utf-8",
            )

            plan_json, plan_csv, plan_xlsx, plan = write_case_split_plan(input_dir, case_map, target_dir, out_dir)
            apply_json, apply_csv, apply_xlsx, rollback_json, applied = apply_case_split_plan(plan_json)

            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["summary"]["planned_copy_count"], 4)
            self.assertTrue(plan_csv.is_file())
            self.assertTrue(plan_xlsx.is_file())
            self.assertTrue((target_dir / "case-001" / "0001.jpg").is_file())
            self.assertTrue((target_dir / "case-001" / "0002.jpg").is_file())
            self.assertTrue((target_dir / "case-002" / "0004.jpg").is_file())
            self.assertEqual(applied["summary"]["applied_case_count"], 2)
            self.assertEqual(applied["summary"]["copied_file_count"], 4)
            self.assertTrue(apply_csv.is_file())
            self.assertTrue(apply_xlsx.is_file())
            rollback = json.loads(rollback_json.read_text(encoding="utf-8"))
            self.assertEqual(rollback["schema_version"], "scan-qc.case-split-rollback.v1")
            self.assertEqual(len(rollback["target_relative_paths"]), 4)
            with ZipFile(plan_xlsx) as archive:
                self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
            self.assertEqual(plan_json.name, CASE_SPLIT_PLAN_JSON)
            self.assertEqual(apply_json.name, CASE_SPLIT_APPLY_JSON)
            self.assertEqual(rollback_json.name, CASE_SPLIT_ROLLBACK_JSON)

    def test_plan_reads_basic_xlsx_case_map(self) -> None:
        with tempfile.TemporaryDirectory(prefix="case-split-xlsx-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            target_dir = root / "target"
            out_dir = root / "out"
            input_dir.mkdir()
            for index in range(1, 3):
                (input_dir / f"{index:04d}.jpg").write_text(f"page {index}", encoding="utf-8")
            case_map = root / "cases.xlsx"
            _write_inline_xlsx(case_map, [["case_name", "start_page", "end_page"], ["case-x", "1", "2"]])

            _plan_json, _plan_csv, _plan_xlsx, plan = write_case_split_plan(input_dir, case_map, target_dir, out_dir)

            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["summary"]["ready_count"], 1)
            self.assertEqual(plan["rows"][0]["target_relative_dir"], "case-x")

    def test_plan_blocks_overlap_bounds_target_and_unsafe_case(self) -> None:
        with tempfile.TemporaryDirectory(prefix="case-split-blocked-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            target_dir = root / "target"
            out_dir = root / "out"
            input_dir.mkdir()
            for index in range(1, 4):
                (input_dir / f"{index:04d}.jpg").write_text(f"page {index}", encoding="utf-8")
            (target_dir / "case-existing").mkdir(parents=True)
            (target_dir / "case-existing" / "0003.jpg").write_text("existing", encoding="utf-8")
            case_map = root / "cases.csv"
            case_map.write_text(
                "case_name,start_page,end_page\n"
                "case-a,1,2\n"
                "case-b,2,3\n"
                "../bad,1,1\n"
                "case-out,4,5\n"
                "case-existing,3,3\n",
                encoding="utf-8",
            )

            plan_json, _plan_csv, _plan_xlsx, plan = write_case_split_plan(input_dir, case_map, target_dir, out_dir)

            self.assertEqual(plan["status"], "blocked")
            reasons = {row["reason_code"] for row in plan["rows"]}
            self.assertIn("overlapping_page_range", reasons)
            self.assertIn("unsafe_case_path", reasons)
            self.assertIn("page_range_out_of_bounds", reasons)
            self.assertIn("target_exists", reasons)
            with self.assertRaisesRegex(ValueError, "blocked"):
                apply_case_split_plan(plan_json)
            self.assertFalse((target_dir / "case-a" / "0001.jpg").exists())

    def test_cli_plan_returns_nonzero_for_blocked_case_split(self) -> None:
        with tempfile.TemporaryDirectory(prefix="case-split-cli-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            target_dir = root / "target"
            out_dir = root / "out"
            input_dir.mkdir()
            (input_dir / "0001.jpg").write_text("page 1", encoding="utf-8")
            case_map = root / "cases.csv"
            case_map.write_text("case_name,start_page,end_page\ncase-a,1,2\n", encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "case-split-plan",
                        "--input",
                        str(input_dir),
                        "--case-map",
                        str(case_map),
                        "--target",
                        str(target_dir),
                        "--out",
                        str(out_dir),
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("Cases blocked: 1", stdout.getvalue())
            self.assertTrue((out_dir / CASE_SPLIT_PLAN_JSON).is_file())


def _write_inline_xlsx(path: Path, rows: list[list[str]]) -> None:
    sheet_rows = []
    for row_index, values in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(values):
            reference = f"{chr(65 + column_index)}{row_index}"
            cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="cases" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


if __name__ == "__main__":
    unittest.main()
