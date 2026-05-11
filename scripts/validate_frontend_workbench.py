"""Validate the static scan-QC frontend workbench prototype."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = ROOT / "docs" / "frontend-workbench-prototype.html"

REQUIRED_REGIONS = {
    "artifact-loader",
    "overview-metrics",
    "workflow-steps",
    "batch-list",
    "findings-list",
    "batch-detail",
    "image-preview",
}

REQUIRED_STRINGS = {
    "run_plan_summary.json",
    "scan_qc_report.json",
    "JSON.parse",
    "type=\"file\"",
    "No artifact loaded",
    "Could not load JSON",
    "Preview placeholder",
}

FORBIDDEN_PATTERNS = {
    "absolute unix user path": re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    "windows drive path": re.compile(r"\b[A-Za-z]:\\\\"),
    "sha256-like hash": re.compile(r"\b[a-fA-F0-9]{64}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "openai key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "private image filename": re.compile(r"\b[\w.-]+\.(?:jpg|jpeg|png|tif|tiff|webp)\b", re.IGNORECASE),
}


def main() -> int:
    if not WORKBENCH.exists():
        print(f"Missing workbench: {WORKBENCH}", file=sys.stderr)
        return 1

    html = WORKBENCH.read_text(encoding="utf-8")
    errors: list[str] = []

    for region in sorted(REQUIRED_REGIONS):
        if f'data-region="{region}"' not in html:
            errors.append(f"missing data-region={region!r}")

    for required in sorted(REQUIRED_STRINGS):
        if required not in html:
            errors.append(f"missing required string {required!r}")

    for label, pattern in FORBIDDEN_PATTERNS.items():
        match = pattern.search(html)
        if match:
            errors.append(f"found forbidden {label}: {match.group(0)!r}")

    if "http://" in html or "https://" in html:
        errors.append("workbench should not depend on external network URLs")

    if errors:
        print("Frontend workbench validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Validated {WORKBENCH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
