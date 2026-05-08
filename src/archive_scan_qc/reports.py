"""Report writers for archive scan QC."""

from __future__ import annotations

import csv
from html import escape
import json
from pathlib import Path
from typing import Any


def write_reports(report: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "scan_qc_report.json"
    html_path = output_dir / "scan_qc_report.html"
    files_csv_path = output_dir / "scan_qc_files.csv"
    findings_csv_path = output_dir / "scan_qc_findings.csv"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(_render_html_report(report), encoding="utf-8")

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
        "orientation_class",
        "aspect_ratio",
        "exif_orientation",
        "exif_orientation_requires_transpose",
        "quality_brightness_mean",
        "quality_contrast_stddev",
        "quality_sharpness_laplacian_var",
        "quality_dark_pixel_ratio",
        "quality_foreground_coverage",
        "quality_edge_coverage",
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
        "html": html_path,
        "files_csv": files_csv_path,
        "findings_csv": findings_csv_path,
    }


def _render_html_report(report: dict[str, Any]) -> str:
    schema_version = report.get("schema_version")
    generated_at = report.get("generated_at")
    project = report.get("project", {})
    manifest = report.get("manifest", {})
    summary = report.get("summary", {})
    dependency_notes = report.get("dependency_notes", [])
    files = report.get("files", [])
    findings = report.get("findings", [])
    report_json = json.dumps(report, ensure_ascii=False, indent=2)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Scan QC Report - {_text(manifest.get("batch_id"))}</title>
  <style>
    :root {{
      color-scheme: light;
      --border: #d7dde5;
      --bg: #f6f8fb;
      --text: #18202b;
      --muted: #5d6b7c;
      --p0: #b42318;
      --p1: #b54708;
      --p2: #175cd3;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 32px 24px 48px;
    }}
    h1, h2 {{
      margin: 0;
      line-height: 1.2;
    }}
    h1 {{
      font-size: 30px;
    }}
    h2 {{
      margin-top: 32px;
      margin-bottom: 12px;
      font-size: 20px;
    }}
    .meta, .cards {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-top: 18px;
    }}
    .item, .card {{
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
    }}
    .label {{
      color: var(--muted);
      display: block;
      font-size: 12px;
      text-transform: uppercase;
    }}
    .value {{
      display: block;
      font-size: 16px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }}
    .card .value {{
      font-size: 28px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      padding: 9px 10px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}
    th {{
      background: #eef2f7;
      color: #344054;
      font-size: 12px;
      white-space: nowrap;
    }}
    tr:last-child td {{
      border-bottom: 0;
    }}
    .badge {{
      border-radius: 999px;
      color: #fff;
      display: inline-block;
      font-size: 12px;
      font-weight: 700;
      min-width: 28px;
      padding: 2px 8px;
      text-align: center;
    }}
    .p0 {{ background: var(--p0); }}
    .p1 {{ background: var(--p1); }}
    .p2 {{ background: var(--p2); }}
    .empty {{
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--muted);
      padding: 16px;
    }}
    .table-wrap {{
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow-x: auto;
    }}
    .table-wrap table {{
      border: 0;
      border-radius: 0;
      min-width: 760px;
    }}
    .notes {{
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      margin: 0;
      padding: 12px 16px 12px 28px;
    }}
    .notes li + li {{
      margin-top: 8px;
    }}
    details {{
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
    }}
    summary {{
      cursor: pointer;
      font-weight: 650;
    }}
    pre {{
      background: #101828;
      border-radius: 6px;
      color: #f2f4f7;
      font-size: 12px;
      line-height: 1.5;
      margin: 12px 0 0;
      max-height: 520px;
      overflow: auto;
      padding: 12px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Scan QC Report</h1>
    <section class="meta" aria-label="Report metadata">
      <div class="item"><span class="label">Schema Version</span><span class="value">{_text(schema_version)}</span></div>
      <div class="item"><span class="label">Generated At</span><span class="value">{_text(generated_at)}</span></div>
    </section>
    <h2>Project</h2>
    {_key_value_table(project)}
    <h2>Batch Manifest</h2>
    <section class="meta" aria-label="Batch manifest">
      {_manifest_items(manifest)}
    </section>
    <section class="cards" aria-label="Summary">
      {_summary_cards(summary)}
    </section>
    <h2>Summary Details</h2>
    {_key_value_table(summary)}
    <h2>Dependency Notes</h2>
    {_notes_list(dependency_notes)}
    <h2>Files</h2>
    {_files_table(files)}
    <h2>Findings</h2>
    {_findings_table(findings)}
    <h2>Complete Report Data</h2>
    <details open>
      <summary>Full embedded JSON</summary>
      <pre id="complete-report-json">{_text(report_json)}</pre>
    </details>
    <script id="scan-qc-report-data" type="application/json">{_script_json(report_json)}</script>
  </main>
</body>
</html>
"""


def _manifest_items(manifest: dict[str, Any]) -> str:
    fields = [
        ("Project", "project_id"),
        ("Batch", "batch_id"),
        ("Input Directory", "input_dir"),
        ("Output Directory", "output_dir"),
        ("Manifest Used", "manifest_used"),
        ("Manifest CSV", "manifest_csv"),
        ("Rule Version", "rule_version"),
        ("Generated At", "generated_at"),
    ]
    return "\n".join(
        f'<div class="item"><span class="label">{label}</span><span class="value">{_text(manifest.get(key))}</span></div>'
        for label, key in fields
    )


def _summary_cards(summary: dict[str, Any]) -> str:
    fields = [
        ("Total Files", "total_files"),
        ("Openable", "openable_files"),
        ("Total Findings", "total_findings"),
        ("P0", "p0_findings"),
        ("P1", "p1_findings"),
        ("P2", "p2_findings"),
        ("Blank Page Findings", "blank_page_findings"),
        ("Manifest Entries", "manifest_entry_count"),
        ("Manifest Missing", "manifest_missing_count"),
        ("Manifest Unexpected", "manifest_unexpected_count"),
        ("Manifest Duplicates", "manifest_duplicate_count"),
        ("Skipped Total", "skipped_total_count"),
        ("Skipped Files", "skipped_file_count"),
        ("Skipped Directories", "skipped_directory_count"),
    ]
    return "\n".join(
        f'<div class="card"><span class="label">{label}</span><span class="value">{_text(summary.get(key, 0))}</span></div>'
        for label, key in fields
    )


def _files_table(files: list[dict[str, Any]]) -> str:
    if not files:
        return '<div class="empty">No files scanned.</div>'
    fields = [
        "relative_path",
        "filename",
        "extension",
        "openable",
        "format",
        "width",
        "height",
        "dpi_x",
        "dpi_y",
        "color_mode",
        "orientation_class",
        "aspect_ratio",
        "exif_orientation",
        "exif_orientation_requires_transpose",
        "quality_brightness_mean",
        "quality_contrast_stddev",
        "quality_sharpness_laplacian_var",
        "quality_dark_pixel_ratio",
        "quality_foreground_coverage",
        "quality_edge_coverage",
        "file_size",
        "sha256",
        "error",
    ]
    rows = []
    for item in files:
        rows.append(
            "<tr>"
            + "".join(f"<td>{_text(item.get(field))}</td>" for field in fields)
            + "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + "".join(f"<th>{_text(_label_from_key(field))}</th>" for field in fields)
        + "</tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table></div>"
    )


def _findings_table(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return '<div class="empty">No findings.</div>'
    rows = []
    for item in findings:
        severity = str(item.get("severity", "")).lower()
        badge_class = severity if severity in {"p0", "p1", "p2"} else ""
        rows.append(
            "<tr>"
            f'<td><span class="badge {badge_class}">{_text(item.get("severity"))}</span></td>'
            f"<td>{_text(item.get('relative_path'))}</td>"
            f"<td>{_text(item.get('rule'))}</td>"
            f"<td>{_text(item.get('message'))}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Severity</th><th>Path</th><th>Rule</th><th>Message</th></tr></thead><tbody>'
        + "\n".join(rows)
        + "</tbody></table></div>"
    )


def _key_value_table(data: dict[str, Any]) -> str:
    if not data:
        return '<div class="empty">No data.</div>'
    rows = []
    for key, value in data.items():
        rows.append(f"<tr><th>{_text(_label_from_key(key))}</th><td>{_format_value(value)}</td></tr>")
    return '<div class="table-wrap"><table><tbody>' + "\n".join(rows) + "</tbody></table></div>"


def _notes_list(notes: list[Any]) -> str:
    if not notes:
        return '<div class="empty">No dependency notes.</div>'
    return '<ul class="notes">' + "\n".join(f"<li>{_text(note)}</li>" for note in notes) + "</ul>"


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return f"<pre>{_text(json.dumps(value, ensure_ascii=False, indent=2))}</pre>"
    return _text(value)


def _label_from_key(key: str) -> str:
    labels = {
        "dpi_x": "DPI X",
        "dpi_y": "DPI Y",
        "exif_orientation": "EXIF Orientation",
        "exif_orientation_requires_transpose": "EXIF Transpose Signal",
        "sha256": "SHA256",
    }
    if key in labels:
        return labels[key]
    return key.replace("_", " ").title()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return escape(str(value), quote=True)


def _script_json(value: str) -> str:
    return value.replace("</", "<\\/")
