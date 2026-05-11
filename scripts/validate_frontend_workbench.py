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
    "aggregate-handoff",
    "review-decisions",
    "batch-list",
    "findings-list",
    "batch-detail",
    "image-preview",
}

REQUIRED_STRINGS = {
    "run_plan_summary.json",
    "scan_qc_report.json",
    "review_summary.json",
    "acceptance_summary.json",
    "aggregate_evidence_bundle_summary.json",
    "final_production_handoff_summary.json",
    "release_candidate_summary.json",
    "JSON.parse",
    "type=\"file\"",
    "No artifact loaded",
    "Could not load JSON",
    "Preview placeholder",
    "Local image preview scaffold",
    "Selected preview filename: none",
    "Clear Preview",
    "Preview is excluded from review-decision export JSON.",
    "URL.createObjectURL",
    "URL.revokeObjectURL",
    "beforeunload",
    "preview filename",
    "preview object URL",
    "Human Review Decisions",
    "Import privacy-safe summary",
    "reviewImportFile",
    "reviewImportStatus",
    "No review-decision summary imported.",
    "Imports restore only scope, synthetic/local ID, and decision status",
    "Imported",
    "skipped",
    "Last import",
    "Schema mismatch: expected scan-qc-review-decisions.local.v1.",
    "Source mismatch: summary source_type does not match the loaded artifact.",
    "Target count mismatch",
    "Load a scan, run-plan, or aggregate handoff artifact before importing decisions.",
    "accepted_issue",
    "false_positive",
    "fixed_externally",
    "needs_rescan",
    "scan-qc-review-decisions.local.v1",
    "Prepare Privacy-Safe Summary",
    "generated_in_browser",
    "source_target_count",
    "parseReviewDecisionSummary",
    "applyReviewDecisionSummary",
    "importReviewDecisionFile",
    "scope",
    "local_id",
    "decision",
    "Aggregate Artifact Summary",
    "Public-safe aggregate summaries only after local policy review",
    "Review summary",
    "Acceptance summary",
    "review-summary",
    "acceptance-summary",
    "Aggregate-only Status",
    "Artifact Type",
    "Remaining P0",
    "Remaining P1",
    "Review Status Counts",
    "Severity Status Counts",
    "No severity/status counts were present.",
    "Rule Counts",
    "Rule Status Counts",
    "Acceptance Passed",
    "Blocking And Warning Codes",
    "Blocking codes",
    "Warning codes",
    "Warning Count",
    "Scan Workers",
    "Processing Workers",
    "aggregateWarningCodes",
    "aggregateNestedStatusCounts",
    "aggregateWorkers",
    "aggregatePrivacyOmissions",
    "privacyOmits",
    "Privacy self-check status",
    "Privacy self-check violations",
    "Aggregate-only status from locally reviewed public-safe summary artifacts.",
    "Artifact Presence And Status",
    "Privacy Status",
    "Sensitivity",
    "Omitted private evidence",
    "Ready for handoff",
    "Ready for release candidate",
    "Blocking Items",
    "Checks Passed",
    "Checks Failed",
    "Scan Throughput",
    "Processing Throughput",
    "contains_paths",
    "contains_filenames",
    "contains_hashes",
    "contains_ocr_text",
    "contains_thumbnails",
    "contains_image_content",
    "contains_row_level_findings",
    "private filenames, paths, hashes, OCR text, thumbnails, image content, row-level findings, reviewer notes, manifests, or derivative image references",
}

FORBIDDEN_PATTERNS = {
    "absolute unix user path": re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    "windows drive path": re.compile(r"\b[A-Za-z]:\\\\"),
    "sha256-like hash": re.compile(r"\b[a-fA-F0-9]{64}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "openai key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "generic bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE),
    "embedded image data": re.compile(r"\bdata:image/", re.IGNORECASE),
    "remote image url": re.compile(r"https?://[^\s\"']+\.(?:jpg|jpeg|png|tif|tiff|webp)\b", re.IGNORECASE),
    "local file image url": re.compile(r"\bfile://[^\s\"']+", re.IGNORECASE),
    "base64 image field": re.compile(r"\bimage_(?:data|base64|bytes)\b", re.IGNORECASE),
    "ocr text field": re.compile(r"\bocr_text\b", re.IGNORECASE),
    "private image filename": re.compile(r"\b[\w.-]+\.(?:jpg|jpeg|png|tif|tiff|webp)\b", re.IGNORECASE),
}

FORBIDDEN_EXPORT_FIELDS = {
    "ocr_text",
    "hash",
    "sha256",
    "thumbnail",
    "absolute_path",
    "image_bytes",
    "manifest",
    "reviewer_notes",
    "derivative_image",
}

REQUIRED_AGGREGATE_FIELDS = {
    "acceptance pass/fail": "acceptancePassed",
    "artifact type": "aggregateArtifactType",
    "blocking codes": "blockingCodes",
    "blocking item count": "blockingItemCount",
    "privacy/sensitivity": "aggregatePrivacy",
    "remaining p0": "remainingP0",
    "remaining p1": "remainingP1",
    "review status counts": "reviewStatusCounts",
    "rule counts": "ruleCounts",
    "rule status counts": "ruleStatusCounts",
    "severity status counts": "severityStatusCounts",
    "throughput": "aggregateThroughput",
    "warning codes": "warningCodes",
    "warning count": "warningCount",
    "workers": "aggregateWorkers",
}

FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS = {
    "content hash": "hash",
    "derivative image": "derivative_image",
    "image content": "image_content",
    "manifest rows": "manifest",
    "ocr text": "ocr_text",
    "private filename": "filename",
    "private path": "path",
    "reviewer notes": "reviewer_notes",
    "row-level findings": "findings",
    "thumbnail": "thumbnail",
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

    export_start = html.find('schema: "scan-qc-review-decisions.local.v1"')
    if export_start == -1:
        errors.append("missing privacy-safe review export builder")
    else:
        export_block = html[export_start : html.find("function resetReviewState", export_start)]
        for field in sorted(FORBIDDEN_EXPORT_FIELDS):
            if re.search(rf"\b{re.escape(field)}\b\s*:", export_block):
                errors.append(f"review export includes forbidden field {field!r}")

    import_start = html.find("function parseReviewDecisionSummary")
    if import_start == -1:
        errors.append("missing privacy-safe review import parser")
    else:
        import_block = html[import_start : html.find("function clearPreviewState", import_start)]
        for field in sorted(FORBIDDEN_EXPORT_FIELDS):
            if re.search(rf"\b{re.escape(field)}\b\s*:", import_block):
                errors.append(f"review import reads forbidden field {field!r}")

    aggregate_start = html.find("function buildAggregateHandoffModel")
    aggregate_end = html.find("function normalizeStatus", aggregate_start)
    if aggregate_start == -1 or aggregate_end == -1:
        errors.append("missing aggregate summary model builder")
    else:
        aggregate_block = html[aggregate_start:aggregate_end]
        for label, required in sorted(REQUIRED_AGGREGATE_FIELDS.items()):
            if required not in aggregate_block:
                errors.append(f"aggregate summary builder missing {label}: {required!r}")
        for label, field in sorted(FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS.items()):
            pattern = rf"\bpayload\.{re.escape(field)}\b|\bpayload\[['\"]{re.escape(field)}['\"]\]"
            if re.search(pattern, aggregate_block):
                errors.append(f"aggregate summary builder reads forbidden {label} field {field!r}")

    render_start = html.find("function renderAggregateHandoff")
    render_end = html.find("function workerRange", render_start)
    if render_start == -1 or render_end == -1:
        errors.append("missing aggregate summary renderer")
    else:
        render_block = html[render_start:render_end]
        expected_labels = {
            "Acceptance Passed",
            "Blocking And Warning Codes",
            "Omitted private evidence",
            "Privacy Status",
            "Processing Workers",
            "Review Status Counts",
            "Rule Counts",
            "Rule Status Counts",
            "Scan Workers",
        }
        for label in sorted(expected_labels):
            if label not in render_block:
                errors.append(f"aggregate summary renderer missing label {label!r}")

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
