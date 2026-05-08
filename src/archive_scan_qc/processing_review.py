"""Sensitive local review package for derivative processing manifests."""

from __future__ import annotations

from collections import Counter
from html import escape
import json
from pathlib import Path
from typing import Any


REVIEW_JSON = "processing_review_package.json"
REVIEW_HTML = "processing_review_package.html"
SENSITIVITY_NOTICE = (
    "Sensitive local processing review package. Contains row-level source and derivative paths, "
    "per-file operation decisions, warnings, and failure details. Keep inside the approved local "
    "operator environment; do not use as public aggregate evidence."
)
GROUP_DEFINITIONS = {
    "deskewed": "Deskewed",
    "dark_border_trimmed": "Dark Border Trimmed",
    "cropped": "Cropped",
    "despeckled": "Despeckled",
    "failed": "Failed",
    "guardrail_warnings": "Guardrail Warnings",
}


def write_processing_review_package(manifest_path: Path, out_dir: Path) -> tuple[Path, Path]:
    manifest = _load_manifest(manifest_path)
    package = build_processing_review_package(manifest, manifest_path, out_dir=out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / REVIEW_JSON
    html_path = out_dir / REVIEW_HTML
    json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(_render_html(package), encoding="utf-8")
    return json_path, html_path


def build_processing_review_package(manifest: dict[str, Any], manifest_path: Path, out_dir: Path | None = None) -> dict[str, Any]:
    files = [_review_record(record, manifest_path.parent, out_dir) for record in manifest.get("files", []) if isinstance(record, dict)]
    groups = {name: [record for record in files if _record_in_group(record, name)] for name in GROUP_DEFINITIONS}
    return {
        "schema_version": "scan-qc.processing-review.v1",
        "generated_at": manifest.get("generated_at"),
        "source_processing_manifest": manifest_path.name,
        "sensitivity": SENSITIVITY_NOTICE,
        "privacy": {
            "local_only": True,
            "aggregate_only": False,
            "contains_row_level_paths": True,
            "contains_hashes": True,
            "contains_image_links": True,
            "embeds_image_data": False,
            "public_evidence": False,
        },
        "project": manifest.get("project", {}),
        "summary": _summary(manifest, files),
        "operations": manifest.get("operations", []),
        "groups": {
            name: {
                "label": GROUP_DEFINITIONS[name],
                "count": len(records),
                "records": records,
            }
            for name, records in groups.items()
        },
        "files": files,
    }


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("processing manifest must be a JSON object")
    if not isinstance(payload.get("files"), list):
        raise ValueError("processing manifest must include a files array")
    return payload


def _review_record(record: dict[str, Any], manifest_dir: Path, out_dir: Path | None) -> dict[str, Any]:
    audit = record.get("processing_audit") if isinstance(record.get("processing_audit"), dict) else {}
    output_relative_path = record.get("output_relative_path")
    return {
        "source_relative_path": record.get("source_relative_path"),
        "output_relative_path": output_relative_path,
        "before_href": None,
        "after_href": _safe_output_href(output_relative_path, manifest_dir, out_dir),
        "status": record.get("status"),
        "resumed": bool(record.get("resumed")),
        "reprocessed": bool(record.get("reprocessed")),
        "deskewed": bool(record.get("deskewed")),
        "deskew_reason": record.get("deskew_reason"),
        "skew_angle_degrees": record.get("skew_angle_degrees"),
        "skew_confidence": record.get("skew_confidence"),
        "dark_border_trimmed": bool(record.get("dark_border_trimmed")),
        "dark_border_reason": record.get("dark_border_reason"),
        "dark_border_bbox": record.get("dark_border_bbox"),
        "cropped": bool(record.get("cropped")),
        "crop_bbox": record.get("crop_bbox"),
        "despeckled": bool(record.get("despeckled")),
        "despeckle_reason": record.get("despeckle_reason"),
        "despeckle_pixels_changed": record.get("despeckle_pixels_changed"),
        "processing_warnings": record.get("processing_warnings", []),
        "guardrail_failures": audit.get("guardrail_failures", []),
        "failure_reason": record.get("failure_reason"),
        "error": record.get("error"),
        "operations": record.get("operations", []),
        "audit": {
            "size_change_ratio": audit.get("size_change_ratio"),
            "pixel_change_ratio": audit.get("pixel_change_ratio"),
            "brightness_delta": audit.get("brightness_delta"),
            "contrast_delta": audit.get("contrast_delta"),
            "crop_ratio": audit.get("crop_ratio"),
            "max_trim_margin_ratio": audit.get("max_trim_margin_ratio"),
            "despeckle_pixel_ratio": audit.get("despeckle_pixel_ratio"),
        },
        "source_sha256": record.get("source_sha256"),
        "output_sha256": record.get("output_sha256"),
    }


def _safe_output_href(value: Any, manifest_dir: Path, out_dir: Path | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    if out_dir is None:
        return path.as_posix()
    target = manifest_dir / path
    try:
        target.resolve().relative_to(manifest_dir.resolve())
    except ValueError:
        return None
    return _relative_href(target, out_dir)


def _relative_href(target: Path, base: Path) -> str:
    try:
        return target.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        import os

        return Path(os.path.relpath(target.resolve(), base.resolve())).as_posix()


def _record_in_group(record: dict[str, Any], group: str) -> bool:
    if group == "failed":
        return record.get("status") == "failed"
    if group == "guardrail_warnings":
        return bool(record.get("processing_warnings")) or bool(record.get("guardrail_failures"))
    return bool(record.get(group))


def _summary(manifest: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(str(record.get("status")) for record in files)
    return {
        "total_files": len(files),
        "processed_files": status_counts.get("processed", 0),
        "resumed_files": status_counts.get("resumed", 0),
        "failed_files": status_counts.get("failed", 0),
        "skipped_files": status_counts.get("skipped", 0),
        "status_counts": dict(sorted(status_counts.items())),
        "deskewed_files": sum(1 for record in files if record.get("deskewed")),
        "dark_border_trimmed_files": sum(1 for record in files if record.get("dark_border_trimmed")),
        "cropped_files": sum(1 for record in files if record.get("cropped")),
        "despeckled_files": sum(1 for record in files if record.get("despeckled")),
        "guardrail_warning_files": sum(
            1 for record in files if record.get("processing_warnings") or record.get("guardrail_failures")
        ),
        "source_manifest_summary": manifest.get("summary", {}),
    }


def _render_html(package: dict[str, Any]) -> str:
    rows = "\n".join(_render_row(record) for record in package["files"])
    group_links = "\n".join(
        f"<li><a href=\"#{escape(name)}\">{escape(group['label'])}</a>: {group['count']}</li>"
        for name, group in package["groups"].items()
    )
    group_sections = "\n".join(_render_group(name, group) for name, group in package["groups"].items())
    data = json.dumps(package, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Processing Review Package</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; color: #1f2933; }}
    .notice {{ border: 2px solid #9f580a; background: #fff7ed; padding: 12px; font-weight: 700; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px; font-size: 13px; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 6px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f0f4f8; }}
    code {{ white-space: pre-wrap; }}
    .pill {{ display: inline-block; padding: 2px 6px; border: 1px solid #bcccdc; border-radius: 999px; margin: 1px; }}
  </style>
</head>
<body>
  <h1>Processing Review Package</h1>
  <p class="notice">{escape(package["sensitivity"])}</p>
  <h2>Summary</h2>
  {_render_summary(package["summary"])}
  <h2>Review Groups</h2>
  <ul>{group_links}</ul>
  {group_sections}
  <h2>All Records</h2>
  <table>
    <thead><tr><th>Source</th><th>Output</th><th>Status</th><th>Decisions</th><th>Warnings / Failure</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <h2>Complete Local Package Data</h2>
  <script id="processing-review-data" type="application/json">{data}</script>
</body>
</html>
"""


def _render_summary(summary: dict[str, Any]) -> str:
    keys = [
        "total_files",
        "processed_files",
        "resumed_files",
        "failed_files",
        "deskewed_files",
        "dark_border_trimmed_files",
        "cropped_files",
        "despeckled_files",
        "guardrail_warning_files",
    ]
    items = "".join(f"<li>{escape(key.replace('_', ' ').title())}: {escape(str(summary.get(key, 0)))}</li>" for key in keys)
    return f"<ul>{items}</ul>"


def _render_group(name: str, group: dict[str, Any]) -> str:
    rows = "\n".join(_render_row(record) for record in group["records"])
    if not rows:
        rows = '<tr><td colspan="5">No records in this group.</td></tr>'
    return (
        f'<h2 id="{escape(name)}">{escape(group["label"])}</h2>'
        "<table><thead><tr><th>Source</th><th>Output</th><th>Status</th>"
        f"<th>Decisions</th><th>Warnings / Failure</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _render_row(record: dict[str, Any]) -> str:
    source = _link_or_code(record.get("source_relative_path"), record.get("before_href"))
    output = _link_or_code(record.get("output_relative_path"), record.get("after_href"))
    decisions = [
        f"deskewed={record.get('deskewed')}",
        f"dark_border_trimmed={record.get('dark_border_trimmed')}",
        f"cropped={record.get('cropped')}",
        f"despeckled={record.get('despeckled')}",
    ]
    warnings = list(record.get("processing_warnings") or []) + list(record.get("guardrail_failures") or [])
    if record.get("failure_reason"):
        warnings.append(str(record["failure_reason"]))
    warning_text = "; ".join(str(item) for item in warnings)
    decision_html = "".join(f'<span class="pill">{escape(item)}</span>' for item in decisions)
    return (
        "<tr>"
        f"<td>{source}</td>"
        f"<td>{output}</td>"
        f"<td>{escape(str(record.get('status')))}</td>"
        f"<td>{decision_html}</td>"
        f"<td>{escape(warning_text)}</td>"
        "</tr>"
    )


def _link_or_code(label: Any, href: Any) -> str:
    label_text = "" if label is None else str(label)
    if isinstance(href, str) and href:
        return f'<a href="{escape(href, quote=True)}"><code>{escape(label_text)}</code></a>'
    return f"<code>{escape(label_text)}</code>"
