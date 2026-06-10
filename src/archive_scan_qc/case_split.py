"""Local case-file split planning and apply utilities."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import shutil
from typing import Any
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from .scanner import SUPPORTED_EXTENSIONS


CASE_SPLIT_PLAN_JSON = "case_split_plan.json"
CASE_SPLIT_PLAN_CSV = "case_split_plan.csv"
CASE_SPLIT_PLAN_XLSX = "case_split_plan.xlsx"
CASE_SPLIT_APPLY_JSON = "case_split_apply.json"
CASE_SPLIT_APPLY_CSV = "case_split_apply.csv"
CASE_SPLIT_APPLY_XLSX = "case_split_apply.xlsx"
CASE_SPLIT_ROLLBACK_JSON = "case_split_rollback.json"
CASE_SPLIT_PLAN_SCHEMA_VERSION = "scan-qc.case-split-plan.v1"
CASE_SPLIT_APPLY_SCHEMA_VERSION = "scan-qc.case-split-apply.v1"
CASE_SPLIT_ROLLBACK_SCHEMA_VERSION = "scan-qc.case-split-rollback.v1"

_CASE_COLUMNS = ("case_name", "case", "file_title", "件名", "案卷题名")
_START_COLUMNS = ("start_page", "start", "from_page", "起始页", "起页")
_END_COLUMNS = ("end_page", "end", "to_page", "终止页", "止页")
_LOG_COLUMNS = (
    "row_number",
    "case_name",
    "start_page",
    "end_page",
    "status",
    "reason_code",
    "source_count",
    "copied_count",
    "target_relative_dir",
)


@dataclass(frozen=True)
class _CaseRow:
    row_number: int
    case_name: str
    start_page: str
    end_page: str


def write_case_split_plan(input_dir: Path, case_map: Path, target_dir: Path, out_dir: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    input_root = input_dir.resolve()
    target_root = target_dir.resolve()
    if not input_root.is_dir():
        raise FileNotFoundError(f"Case split input directory does not exist: {input_root}")
    if input_root == target_root or input_root in target_root.parents or target_root in input_root.parents:
        raise ValueError("Case split input and target directories must not overlap.")
    source_files = _source_files(input_root)
    case_rows = _read_case_map(case_map)
    planned = _plan_rows(source_files, target_root, case_rows)
    payload = _plan_payload(input_root, target_root, case_map.resolve(), len(source_files), planned)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / CASE_SPLIT_PLAN_JSON
    csv_path = out_dir / CASE_SPLIT_PLAN_CSV
    xlsx_path = out_dir / CASE_SPLIT_PLAN_XLSX
    _write_json(json_path, payload)
    _write_log_csv(csv_path, planned)
    _write_log_xlsx(xlsx_path, planned, sheet_name="case_split_plan")
    return json_path, csv_path, xlsx_path, payload


def apply_case_split_plan(plan_json: Path, out_dir: Path | None = None) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    plan = _read_json(plan_json)
    if plan.get("schema_version") != CASE_SPLIT_PLAN_SCHEMA_VERSION:
        raise ValueError("Case split plan schema is not supported.")
    input_root = Path(str(plan.get("input_root", ""))).resolve()
    target_root = Path(str(plan.get("target_root", ""))).resolve()
    if not input_root.is_dir():
        raise FileNotFoundError("Case split input root is unavailable.")
    source_files = _source_files(input_root)
    rows = _rows_from_plan(plan)
    current_plan = _plan_rows(source_files, target_root, rows)
    blockers = [row for row in current_plan if row["status"] != "ready"]
    output_dir = out_dir or plan_json.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    if blockers:
        payload = _apply_payload(input_root, target_root, current_plan, status="blocked", blocking_count=len(blockers))
        json_path, csv_path, xlsx_path = _write_apply_outputs(output_dir, payload, current_plan)
        rollback_path = output_dir / CASE_SPLIT_ROLLBACK_JSON
        _write_json(rollback_path, _rollback_payload(input_root, target_root, []))
        raise ValueError(f"Case split plan is blocked by {len(blockers)} row(s). Apply log: {json_path}")

    applied_rows: list[dict[str, Any]] = []
    copied_targets: list[str] = []
    for row in current_plan:
        target_dir = target_root / row["target_relative_dir"]
        target_dir.mkdir(parents=True, exist_ok=True)
        copied_count = 0
        for source_relative in row["source_relative_paths"]:
            source = input_root / source_relative
            target = target_dir / Path(source_relative).name
            shutil.copy2(source, target)
            copied_targets.append(target.relative_to(target_root).as_posix())
            copied_count += 1
        applied = dict(row)
        applied["status"] = "applied"
        applied["copied_count"] = copied_count
        applied_rows.append(applied)

    payload = _apply_payload(input_root, target_root, applied_rows, status="applied", blocking_count=0)
    json_path, csv_path, xlsx_path = _write_apply_outputs(output_dir, payload, applied_rows)
    rollback_path = output_dir / CASE_SPLIT_ROLLBACK_JSON
    _write_json(rollback_path, _rollback_payload(input_root, target_root, copied_targets))
    return json_path, csv_path, xlsx_path, rollback_path, payload


def _source_files(input_root: Path) -> list[Path]:
    return sorted(
        (
            path.relative_to(input_root)
            for path in input_root.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda path: path.as_posix().casefold(),
    )


def _read_case_map(case_map: Path) -> list[_CaseRow]:
    if not case_map.is_file():
        raise FileNotFoundError(f"Case split map does not exist: {case_map}")
    if case_map.suffix.lower() == ".xlsx":
        records = _read_xlsx_records(case_map)
    else:
        records = _read_csv_records(case_map)
    case_column = _first_column(records["fieldnames"], _CASE_COLUMNS)
    start_column = _first_column(records["fieldnames"], _START_COLUMNS)
    end_column = _first_column(records["fieldnames"], _END_COLUMNS)
    if case_column is None or start_column is None or end_column is None:
        raise ValueError("Case split map must include case_name, start_page, and end_page columns.")
    return [
        _CaseRow(
            row_number=row_number,
            case_name=str(row.get(case_column) or ""),
            start_page=str(row.get(start_column) or ""),
            end_page=str(row.get(end_column) or ""),
        )
        for row_number, row in records["rows"]
    ]


def _read_csv_records(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [(row_number, row) for row_number, row in enumerate(reader, start=2)]
        return {"fieldnames": reader.fieldnames or [], "rows": rows}


def _read_xlsx_records(path: Path) -> dict[str, Any]:
    with ZipFile(path) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml")
        shared_strings = _read_shared_strings(archive)
    root = ET.fromstring(sheet_xml)
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    table: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", namespace):
        values: list[str] = []
        for cell in row.findall("x:c", namespace):
            values.append(_cell_text(cell, shared_strings, namespace))
        table.append(values)
    if not table:
        return {"fieldnames": [], "rows": []}
    fieldnames = table[0]
    rows: list[tuple[int, dict[str, str]]] = []
    for row_number, values in enumerate(table[1:], start=2):
        rows.append((row_number, {field: values[index] if index < len(values) else "" for index, field in enumerate(fieldnames)}))
    return {"fieldnames": fieldnames, "rows": rows}


def _read_shared_strings(archive: ZipFile) -> list[str]:
    try:
        xml = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml)
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values = []
    for item in root.findall("x:si", namespace):
        values.append("".join(text.text or "" for text in item.findall(".//x:t", namespace)))
    return values


def _cell_text(cell: ET.Element, shared_strings: list[str], namespace: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//x:t", namespace))
    value = cell.find("x:v", namespace)
    text = value.text if value is not None and value.text is not None else ""
    if cell_type == "s":
        try:
            return shared_strings[int(text)]
        except (IndexError, ValueError):
            return ""
    return text


def _first_column(fieldnames: list[str] | None, candidates: tuple[str, ...]) -> str | None:
    if not fieldnames:
        return None
    by_lower = {field.lower(): field for field in fieldnames}
    for candidate in candidates:
        if candidate in by_lower:
            return by_lower[candidate]
    return None


def _plan_rows(source_files: list[Path], target_root: Path, rows: list[_CaseRow]) -> list[dict[str, Any]]:
    used_pages: dict[int, int] = {}
    parsed: list[tuple[_CaseRow, str | None, int | None, int | None, str | None]] = []
    case_counts: dict[str, int] = {}
    for row in rows:
        case_dir, case_error = _safe_case_dir(row.case_name)
        start_page, start_error = _positive_int(row.start_page, "invalid_start_page")
        end_page, end_error = _positive_int(row.end_page, "invalid_end_page")
        reason = case_error or start_error or end_error
        if reason is None and start_page is not None and end_page is not None:
            if start_page > end_page:
                reason = "start_after_end"
            elif end_page > len(source_files):
                reason = "page_range_out_of_bounds"
        if case_dir is not None:
            case_counts[_case_key(case_dir)] = case_counts.get(_case_key(case_dir), 0) + 1
        parsed.append((row, case_dir, start_page, end_page, reason))

    planned: list[dict[str, Any]] = []
    for row, case_dir, start_page, end_page, reason in parsed:
        source_paths: list[str] = []
        target_exists = False
        if reason is None and case_dir is not None and start_page is not None and end_page is not None:
            if case_counts.get(_case_key(case_dir), 0) > 1:
                reason = "duplicate_case_name"
            else:
                for page_number in range(start_page, end_page + 1):
                    if page_number in used_pages:
                        reason = "overlapping_page_range"
                        break
                if reason is None:
                    source_paths = [source_files[index - 1].as_posix() for index in range(start_page, end_page + 1)]
                    for source in source_paths:
                        target = target_root / case_dir / Path(source).name
                        if target.exists():
                            target_exists = True
                            reason = "target_exists"
                            break
                    if reason is None:
                        for page_number in range(start_page, end_page + 1):
                            used_pages[page_number] = row.row_number
        planned.append(
            {
                "row_number": row.row_number,
                "case_name": row.case_name.strip(),
                "start_page": start_page,
                "end_page": end_page,
                "status": "ready" if reason is None else "blocked",
                "reason_code": reason,
                "source_count": len(source_paths),
                "copied_count": 0,
                "target_relative_dir": case_dir or "",
                "target_exists": target_exists,
                "source_relative_paths": source_paths,
            }
        )
    return planned


def _safe_case_dir(value: str) -> tuple[str | None, str | None]:
    text = value.replace("\\", "/").strip()
    if not text:
        return None, "empty_case_name"
    relative = Path(text)
    if relative.is_absolute() or relative.drive:
        return None, "absolute_case_path"
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None, "unsafe_case_path"
    return relative.as_posix(), None


def _positive_int(value: str, code: str) -> tuple[int | None, str | None]:
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return None, code
    if parsed < 1:
        return None, code
    return parsed, None


def _case_key(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _plan_payload(
    input_root: Path,
    target_root: Path,
    case_map: Path,
    total_source_files: int,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    blocking_count = sum(1 for row in rows if row["status"] != "ready")
    return {
        "schema_version": CASE_SPLIT_PLAN_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": "ready" if blocking_count == 0 else "blocked",
        "local_only": True,
        "public_safe": False,
        "input_root": str(input_root),
        "target_root": str(target_root),
        "case_map": str(case_map),
        "summary": {
            "row_count": len(rows),
            "ready_count": len(rows) - blocking_count,
            "blocking_count": blocking_count,
            "total_source_files": total_source_files,
            "planned_copy_count": sum(row["source_count"] for row in rows if row["status"] == "ready"),
            "applies_changes": False,
        },
        "rows": rows,
    }


def _apply_payload(
    input_root: Path,
    target_root: Path,
    rows: list[dict[str, Any]],
    *,
    status: str,
    blocking_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": CASE_SPLIT_APPLY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": status,
        "local_only": True,
        "public_safe": False,
        "input_root": str(input_root),
        "target_root": str(target_root),
        "summary": {
            "row_count": len(rows),
            "applied_case_count": sum(1 for row in rows if row["status"] == "applied"),
            "copied_file_count": sum(int(row.get("copied_count") or 0) for row in rows),
            "blocking_count": blocking_count,
            "applies_changes": status == "applied",
        },
        "rows": rows,
    }


def _rollback_payload(input_root: Path, target_root: Path, copied_targets: list[str]) -> dict[str, Any]:
    return {
        "schema_version": CASE_SPLIT_ROLLBACK_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "local_only": True,
        "public_safe": False,
        "input_root": str(input_root),
        "target_root": str(target_root),
        "summary": {"target_file_count": len(copied_targets), "automatic_rollback_performed": False},
        "target_relative_paths": copied_targets,
    }


def _rows_from_plan(plan: dict[str, Any]) -> list[_CaseRow]:
    rows = plan.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Case split plan rows are missing.")
    parsed: list[_CaseRow] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError("Case split plan row is invalid.")
        parsed.append(
            _CaseRow(
                row_number=int(row.get("row_number") or index),
                case_name=str(row.get("case_name") or ""),
                start_page=str(row.get("start_page") or ""),
                end_page=str(row.get("end_page") or ""),
            )
        )
    return parsed


def _write_apply_outputs(out_dir: Path, payload: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    json_path = out_dir / CASE_SPLIT_APPLY_JSON
    csv_path = out_dir / CASE_SPLIT_APPLY_CSV
    xlsx_path = out_dir / CASE_SPLIT_APPLY_XLSX
    _write_json(json_path, payload)
    _write_log_csv(csv_path, rows)
    _write_log_xlsx(xlsx_path, rows, sheet_name="case_split_apply")
    return json_path, csv_path, xlsx_path


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_log_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_LOG_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_log_xlsx(path: Path, rows: list[dict[str, Any]], *, sheet_name: str) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml(sheet_name))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml())
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(rows))


def _worksheet_xml(rows: list[dict[str, Any]]) -> str:
    xml_rows = [_xlsx_row(1, list(_LOG_COLUMNS))]
    for index, row in enumerate(rows, start=2):
        xml_rows.append(_xlsx_row(index, [row.get(column, "") for column in _LOG_COLUMNS]))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>"
    )


def _xlsx_row(index: int, values: list[Any]) -> str:
    cells = []
    for offset, value in enumerate(values):
        reference = f"{_xlsx_column(offset)}{index}"
        cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value), quote=False)}</t></is></c>')
    return f'<row r="{index}">{"".join(cells)}</row>'


def _xlsx_column(offset: int) -> str:
    letters = ""
    value = offset + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _workbook_xml(sheet_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )


def _content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )


def _workbook_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
