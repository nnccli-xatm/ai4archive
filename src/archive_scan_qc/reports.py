"""Report writers for archive scan QC."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def write_reports(report: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "scan_qc_report.json"
    files_csv_path = output_dir / "scan_qc_files.csv"
    findings_csv_path = output_dir / "scan_qc_findings.csv"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    file_rows = report.get("files", [])
    file_fields = [
        "relative_path",
        "filename",
        "openable",
        "format",
        "width",
        "height",
        "dpi_x",
        "dpi_y",
        "color_mode",
        "file_size",
        "sha256",
        "error",
    ]
    with files_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=file_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(file_rows)

    finding_fields = ["relative_path", "rule", "severity", "message"]
    with findings_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=finding_fields)
        writer.writeheader()
        writer.writerows(report.get("findings", []))

    return {
        "json": json_path,
        "files_csv": files_csv_path,
        "findings_csv": findings_csv_path,
    }
