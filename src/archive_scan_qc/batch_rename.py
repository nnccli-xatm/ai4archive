"""Local batch rename planning and apply utilities.

This tool intentionally handles path-bearing local evidence. Its manifests are
not public-safe artifacts.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile


BATCH_RENAME_PLAN_JSON = "batch_rename_plan.json"
BATCH_RENAME_PLAN_CSV = "batch_rename_plan.csv"
BATCH_RENAME_PLAN_XLSX = "batch_rename_plan.xlsx"
BATCH_RENAME_APPLY_JSON = "batch_rename_apply.json"
BATCH_RENAME_APPLY_CSV = "batch_rename_apply.csv"
BATCH_RENAME_APPLY_XLSX = "batch_rename_apply.xlsx"
BATCH_RENAME_ROLLBACK_JSON = "batch_rename_rollback.json"
BATCH_RENAME_PLAN_SCHEMA_VERSION = "scan-qc.batch-rename-plan.v1"
BATCH_RENAME_APPLY_SCHEMA_VERSION = "scan-qc.batch-rename-apply.v1"
BATCH_RENAME_ROLLBACK_SCHEMA_VERSION = "scan-qc.batch-rename-rollback.v1"

_SOURCE_COLUMNS = ("source_relative_path", "source_path", "source", "current_relative_path")
_TARGET_COLUMNS = ("target_relative_path", "new_relative_path", "target_path", "target", "destination_relative_path")
_LOG_COLUMNS = (
    "row_number",
    "source_relative_path",
    "target_relative_path",
    "status",
    "reason_code",
    "applied",
    "source_exists",
    "target_exists",
)


@dataclass(frozen=True)
class _RenameRow:
    row_number: int
    source_relative_path: str
    target_relative_path: str


def write_batch_rename_plan(input_dir: Path, mapping_csv: Path, out_dir: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Write a dry-run batch rename plan with conflict detection."""

    input_root = input_dir.resolve()
    if not input_root.is_dir():
        raise FileNotFoundError(f"Batch rename input directory does not exist: {input_root}")
    rows = _read_mapping_csv(mapping_csv)
    planned = _plan_rows(input_root, rows)
    payload = _plan_payload(input_root, mapping_csv.resolve(), planned)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / BATCH_RENAME_PLAN_JSON
    csv_path = out_dir / BATCH_RENAME_PLAN_CSV
    xlsx_path = out_dir / BATCH_RENAME_PLAN_XLSX
    _write_json(json_path, payload)
    _write_log_csv(csv_path, planned)
    _write_log_xlsx(xlsx_path, planned, sheet_name="rename_plan")
    return json_path, csv_path, xlsx_path, payload


def apply_batch_rename_plan(plan_json: Path, out_dir: Path | None = None) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    """Apply a previously written plan and write apply plus rollback manifests."""

    plan = _read_json(plan_json)
    if plan.get("schema_version") != BATCH_RENAME_PLAN_SCHEMA_VERSION:
        raise ValueError("Batch rename plan schema is not supported.")
    input_root = Path(str(plan.get("input_root", ""))).resolve()
    if not input_root.is_dir():
        raise FileNotFoundError("Batch rename input root is unavailable.")
    rows = _rows_from_plan(plan)
    current_plan = _plan_rows(input_root, rows)
    blockers = [row for row in current_plan if row["status"] != "ready"]
    if blockers:
        payload = _apply_payload(input_root, current_plan, status="blocked", blocking_count=len(blockers))
        output_dir = out_dir or plan_json.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path, csv_path, xlsx_path = _write_apply_outputs(output_dir, payload, current_plan)
        rollback_path = output_dir / BATCH_RENAME_ROLLBACK_JSON
        _write_json(rollback_path, _rollback_payload(input_root, []))
        raise ValueError(f"Batch rename plan is blocked by {len(blockers)} row(s). Apply log: {json_path}")

    applied_rows: list[dict[str, Any]] = []
    for row in current_plan:
        source = input_root / row["source_relative_path"]
        target = input_root / row["target_relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        applied = dict(row)
        applied["status"] = "applied"
        applied["applied"] = True
        applied_rows.append(applied)

    output_dir = out_dir or plan_json.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = _apply_payload(input_root, applied_rows, status="applied", blocking_count=0)
    json_path, csv_path, xlsx_path = _write_apply_outputs(output_dir, payload, applied_rows)
    rollback_path = output_dir / BATCH_RENAME_ROLLBACK_JSON
    _write_json(rollback_path, _rollback_payload(input_root, applied_rows))
    return json_path, csv_path, xlsx_path, rollback_path, payload


def _read_mapping_csv(mapping_csv: Path) -> list[_RenameRow]:
    if not mapping_csv.is_file():
        raise FileNotFoundError(f"Batch rename mapping CSV does not exist: {mapping_csv}")
    with mapping_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        source_column = _first_column(reader.fieldnames, _SOURCE_COLUMNS)
        target_column = _first_column(reader.fieldnames, _TARGET_COLUMNS)
        if source_column is None or target_column is None:
            raise ValueError(
                "Batch rename CSV must include source_relative_path and target_relative_path/new_relative_path columns."
            )
        rows: list[_RenameRow] = []
        for row_number, row in enumerate(reader, start=2):
            rows.append(
                _RenameRow(
                    row_number=row_number,
                    source_relative_path=str(row.get(source_column) or ""),
                    target_relative_path=str(row.get(target_column) or ""),
                )
            )
    return rows


def _first_column(fieldnames: list[str] | None, candidates: tuple[str, ...]) -> str | None:
    if not fieldnames:
        return None
    by_lower = {field.lower(): field for field in fieldnames}
    for candidate in candidates:
        if candidate in by_lower:
            return by_lower[candidate]
    return None


def _plan_rows(input_root: Path, rows: list[_RenameRow]) -> list[dict[str, Any]]:
    source_counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    normalized_rows: list[tuple[_RenameRow, str | None, str | None, str | None]] = []
    for row in rows:
        source, source_error = _safe_relative_text(row.source_relative_path)
        target, target_error = _safe_relative_text(row.target_relative_path)
        normalized_rows.append((row, source, target, source_error or target_error))
        if source is not None:
            source_counts[_case_key(source)] = source_counts.get(_case_key(source), 0) + 1
        if target is not None:
            target_counts[_case_key(target)] = target_counts.get(_case_key(target), 0) + 1

    planned: list[dict[str, Any]] = []
    for row, source, target, path_error in normalized_rows:
        reason_code = path_error
        source_exists = False
        target_exists = False
        if reason_code is None and source is not None and target is not None:
            source_path = (input_root / source).resolve()
            target_path = (input_root / target).resolve()
            _require_within(source_path, input_root)
            _require_within(target_path, input_root)
            source_exists = source_path.is_file()
            target_exists = target_path.exists()
            if _case_key(source) == _case_key(target):
                reason_code = "source_target_same"
            elif source_counts.get(_case_key(source), 0) > 1:
                reason_code = "duplicate_source"
            elif target_counts.get(_case_key(target), 0) > 1:
                reason_code = "duplicate_target"
            elif not source_exists:
                reason_code = "source_missing"
            elif target_exists:
                reason_code = "target_exists"
        status = "ready" if reason_code is None else "blocked"
        planned.append(
            {
                "row_number": row.row_number,
                "source_relative_path": source or row.source_relative_path.strip(),
                "target_relative_path": target or row.target_relative_path.strip(),
                "status": status,
                "reason_code": reason_code,
                "applied": False,
                "source_exists": source_exists,
                "target_exists": target_exists,
            }
        )
    return planned


def _safe_relative_text(value: str) -> tuple[str | None, str | None]:
    text = value.replace("\\", "/").strip()
    if not text:
        return None, "empty_path"
    relative = Path(text)
    if relative.is_absolute() or relative.drive:
        return None, "absolute_path"
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None, "unsafe_path"
    return relative.as_posix(), None


def _require_within(path: Path, root: Path) -> None:
    if path != root and root not in path.parents:
        raise ValueError("Batch rename path escapes input root.")


def _case_key(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _plan_payload(input_root: Path, mapping_csv: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    blocking_count = sum(1 for row in rows if row["status"] != "ready")
    return {
        "schema_version": BATCH_RENAME_PLAN_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": "ready" if blocking_count == 0 else "blocked",
        "local_only": True,
        "public_safe": False,
        "input_root": str(input_root),
        "mapping_csv": str(mapping_csv),
        "summary": {
            "row_count": len(rows),
            "ready_count": len(rows) - blocking_count,
            "blocking_count": blocking_count,
            "applies_changes": False,
        },
        "rows": rows,
    }


def _apply_payload(input_root: Path, rows: list[dict[str, Any]], *, status: str, blocking_count: int) -> dict[str, Any]:
    return {
        "schema_version": BATCH_RENAME_APPLY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "status": status,
        "local_only": True,
        "public_safe": False,
        "input_root": str(input_root),
        "summary": {
            "row_count": len(rows),
            "applied_count": sum(1 for row in rows if row.get("applied")),
            "blocking_count": blocking_count,
            "applies_changes": status == "applied",
        },
        "rows": rows,
    }


def _rollback_payload(input_root: Path, applied_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rollback_rows = [
        {
            "row_number": row["row_number"],
            "source_relative_path": row["target_relative_path"],
            "target_relative_path": row["source_relative_path"],
            "status": "ready_for_manual_rollback",
        }
        for row in applied_rows
    ]
    return {
        "schema_version": BATCH_RENAME_ROLLBACK_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "local_only": True,
        "public_safe": False,
        "input_root": str(input_root),
        "summary": {"row_count": len(rollback_rows), "automatic_rollback_performed": False},
        "rows": rollback_rows,
    }


def _rows_from_plan(plan: dict[str, Any]) -> list[_RenameRow]:
    plan_rows = plan.get("rows")
    if not isinstance(plan_rows, list):
        raise ValueError("Batch rename plan rows are missing.")
    rows: list[_RenameRow] = []
    for index, row in enumerate(plan_rows, start=1):
        if not isinstance(row, dict):
            raise ValueError("Batch rename plan row is invalid.")
        rows.append(
            _RenameRow(
                row_number=int(row.get("row_number") or index),
                source_relative_path=str(row.get("source_relative_path") or ""),
                target_relative_path=str(row.get("target_relative_path") or ""),
            )
        )
    return rows


def _write_apply_outputs(
    out_dir: Path, payload: dict[str, Any], rows: list[dict[str, Any]]
) -> tuple[Path, Path, Path]:
    json_path = out_dir / BATCH_RENAME_APPLY_JSON
    csv_path = out_dir / BATCH_RENAME_APPLY_CSV
    xlsx_path = out_dir / BATCH_RENAME_APPLY_XLSX
    _write_json(json_path, payload)
    _write_log_csv(csv_path, rows)
    _write_log_xlsx(xlsx_path, rows, sheet_name="rename_apply")
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
    sheet = _worksheet_xml(rows)
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml())
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


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
        cells.append(
            f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value), quote=False)}</t></is></c>'
        )
    return f'<row r="{index}">{"".join(cells)}</row>'


def _xlsx_column(offset: int) -> str:
    letters = ""
    value = offset + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


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
