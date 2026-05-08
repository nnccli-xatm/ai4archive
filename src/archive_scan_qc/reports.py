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
    manifest = report.get("manifest", {})
    summary = report.get("summary", {})
    files = report.get("files", [])
    findings = report.get("findings", [])

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
  </style>
</head>
<body>
  <main>
    <h1>Scan QC Report</h1>
    <section class="meta" aria-label="Batch manifest">
      {_manifest_items(manifest)}
    </section>
    <section class="cards" aria-label="Summary">
      {_summary_cards(summary)}
    </section>
    <h2>Files</h2>
    {_files_table(files)}
    <h2>Findings</h2>
    {_findings_table(findings)}
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
    rows = []
    for item in files:
        rows.append(
            "<tr>"
            f"<td>{_text(item.get('relative_path'))}</td>"
            f"<td>{_text(item.get('openable'))}</td>"
            f"<td>{_text(item.get('format'))}</td>"
            f"<td>{_text(item.get('width'))} x {_text(item.get('height'))}</td>"
            f"<td>{_text(item.get('dpi_x'))} x {_text(item.get('dpi_y'))}</td>"
            f"<td>{_text(item.get('color_mode'))}</td>"
            f"<td>{_text(item.get('orientation_class'))}</td>"
            f"<td>{_text(item.get('aspect_ratio'))}</td>"
            f"<td>{_text(item.get('exif_orientation'))}</td>"
            f"<td>{_text(item.get('exif_orientation_requires_transpose'))}</td>"
            f"<td>{_text(item.get('quality_brightness_mean'))}</td>"
            f"<td>{_text(item.get('quality_contrast_stddev'))}</td>"
            f"<td>{_text(item.get('quality_sharpness_laplacian_var'))}</td>"
            f"<td>{_text(item.get('quality_dark_pixel_ratio'))}</td>"
            f"<td>{_text(item.get('quality_foreground_coverage'))}</td>"
            f"<td>{_text(item.get('quality_edge_coverage'))}</td>"
            f"<td>{_text(item.get('file_size'))}</td>"
            f"<td>{_text(item.get('error'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Path</th><th>Openable</th><th>Format</th><th>Dimensions</th>"
        "<th>DPI</th><th>Color</th><th>Orientation</th><th>Aspect Ratio</th>"
        "<th>EXIF Orientation</th><th>EXIF Transpose Signal</th><th>Brightness Mean</th><th>Contrast Stddev</th>"
        "<th>Sharpness Laplacian Var</th><th>Dark Pixel Ratio</th><th>Foreground Coverage</th>"
        "<th>Edge Coverage</th><th>Bytes</th><th>Error</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
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
        "<table><thead><tr><th>Severity</th><th>Path</th><th>Rule</th><th>Message</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return escape(str(value), quote=True)
