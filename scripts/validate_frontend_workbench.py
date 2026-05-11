"""Validate the static scan-QC frontend workbench prototype."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
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


def validate_executable_aggregate_fixtures(html: str) -> list[str]:
    script_match = re.search(r"<script>(?P<script>.*?)</script>", html, re.DOTALL)
    if not script_match:
        return ["missing executable workbench script"]

    runner = f"""
const vm = require("node:vm");
const workbenchScript = {script_match.group("script")!r};
const elements = new Map();

function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      value: "",
      innerHTML: "",
      textContent: "",
      className: "",
      files: [],
      classList: {{
        add() {{}},
        remove() {{}}
      }},
      addEventListener() {{}},
      click() {{}}
    }});
  }}
  return elements.get(id);
}}

const context = {{
  assert,
  countFor,
  console,
  Blob: function Blob() {{}},
  URL: {{
    createObjectURL() {{ return "blob:aggregate-fixture"; }},
    revokeObjectURL() {{}}
  }},
  document: {{
    getElementById: element,
    createElement: element
  }},
  window: {{
    addEventListener() {{}}
  }}
}};

function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}

function countFor(rows, name) {{
  const row = rows.find(item => item.name === name);
  return row ? row.count : undefined;
}}

vm.createContext(context);
vm.runInContext(workbenchScript + `
  const reviewFixture = {{
    schema_version: "scan-qc-review-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "pass",
    remaining_p0: 0,
    remaining_p1: 1,
    total_findings: 8,
    status_counts: {{
      accepted_issue: 2,
      false_positive: 5,
      needs_rescan: 1
    }},
    severity_status_counts: {{
      P0: {{ accepted_issue: 0, false_positive: 0 }},
      P1: {{ accepted_issue: 1, false_positive: 1 }},
      P2: {{ accepted_issue: 1, false_positive: 4, needs_rescan: 1 }}
    }},
    rule_counts: {{
      skew_detected: 3,
      blur_detected: 5
    }},
    rule_status_counts: {{
      skew_detected: {{ false_positive: 3 }},
      blur_detected: {{ accepted_issue: 2, false_positive: 2, needs_rescan: 1 }}
    }},
    privacy: {{
      aggregate_only: true,
      omits: [
        "source location strings",
        "source file identifiers",
        "content hashes",
        "recognized text",
        "thumbnails",
        "image content",
        "row-level findings"
      ],
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const acceptanceFixture = {{
    schema_version: "scan-qc-acceptance-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    acceptance_passed: true,
    pass: true,
    blocking_item_count: 0,
    blocking_items: [],
    warnings: [
      {{ code: "aggregate_warning_review_backlog" }},
      {{ code: "aggregate_warning_policy_hold" }}
    ],
    human_review: {{
      remaining_p0: 0,
      remaining_p1: 2,
      total_findings: 9,
      status_counts: {{
        accepted_issue: 4,
        false_positive: 5
      }}
    }},
    privacy: {{
      aggregate_only: true,
      omits: [
        "source location strings",
        "source file identifiers",
        "content hashes",
        "recognized text",
        "thumbnails",
        "image content",
        "row-level findings"
      ],
      contains_paths: false,
      contains_filenames: false,
      contains_hashes: false,
      contains_ocr_text: false,
      contains_thumbnails: false,
      contains_image_content: false,
      contains_row_level_findings: false,
      redacts_private_values: true
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const reviewModel = inferArtifact(reviewFixture);
  assert(reviewModel.sourceType === "aggregate-handoff", "review fixture did not load as aggregate handoff");
  assert(reviewModel.aggregateHandoff.artifactType === "Review summary", "review fixture did not classify as Review summary");
  assert(reviewModel.aggregateHandoff.status === "pass", "review fixture status was not pass");
  assert(countFor(reviewModel.aggregateHandoff.reviewStatusCounts, "false_positive") === 5, "review fixture status counts were not preserved");
  state.model = reviewModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("Review summary"), "review fixture did not render Review summary");
  assert(els.aggregateHandoff.innerHTML.includes("Review Status Counts"), "review fixture did not render review status counts");

  const acceptanceModel = inferArtifact(acceptanceFixture);
  assert(acceptanceModel.sourceType === "aggregate-handoff", "acceptance fixture did not load as aggregate handoff");
  assert(acceptanceModel.aggregateHandoff.artifactType === "Acceptance summary", "acceptance fixture did not classify as Acceptance summary");
  assert(acceptanceModel.aggregateHandoff.status === "pass", "acceptance fixture aggregate-only status was not pass");
  assert(acceptanceModel.aggregateHandoff.acceptancePassed === true, "acceptance fixture did not preserve acceptance_passed/pass");
  assert(acceptanceModel.aggregateHandoff.blockingItemCount === 0, "acceptance fixture blocking count was not aggregate-only zero");
  assert(acceptanceModel.aggregateHandoff.remainingP0 === 0, "acceptance fixture remaining P0 was not aggregate-only zero");
  assert(acceptanceModel.aggregateHandoff.remainingP1 === 2, "acceptance fixture remaining P1 was not preserved");
  assert(acceptanceModel.aggregateHandoff.warningCount === 2, "acceptance fixture warning count was not preserved");
  assert(acceptanceModel.aggregateHandoff.privacy.aggregateOnly === true, "acceptance fixture privacy was not aggregate-only");
  assert(acceptanceModel.aggregateHandoff.privacy.containsPaths === false, "acceptance fixture reported private paths");
  assert(acceptanceModel.aggregateHandoff.privacy.containsFilenames === false, "acceptance fixture reported private filenames");
  state.model = acceptanceModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("Acceptance summary"), "acceptance fixture did not render Acceptance summary");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_warning_review_backlog"), "acceptance fixture did not render warning code");
  assert(els.aggregateHandoff.innerHTML.includes("Aggregate-only Status"), "acceptance fixture did not render aggregate-only status");
  assert(els.aggregateHandoff.innerHTML.includes("Acceptance Passed"), "acceptance fixture did not render acceptance status");
`, context);
"""

    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(runner)
        runner_path = Path(handle.name)

    try:
        completed = subprocess.run(
            ["node", str(runner_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return ["Node.js is required for executable aggregate fixture checks but was not found on PATH"]
    finally:
        runner_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"node exited {completed.returncode}"
        return [f"executable aggregate fixture check failed: {detail}"]
    return []


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
        required_fragments = {
            "review summary schema classification": 'schema.includes("review-summary")',
            "review summary status-count classification": "payload.status_counts",
            "acceptance summary schema classification": 'schema.includes("acceptance-summary")',
            "acceptance summary pass classification": "payload.pass",
            "acceptance human-review remaining p0": "payload.human_review && payload.human_review.remaining_p0",
            "acceptance human-review remaining p1": "payload.human_review && payload.human_review.remaining_p1",
        }
        for label, fragment in sorted(required_fragments.items()):
            if fragment not in aggregate_block:
                errors.append(f"aggregate summary builder missing {label}: {fragment!r}")
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

    errors.extend(validate_executable_aggregate_fixtures(html))

    if errors:
        print("Frontend workbench validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Validated {WORKBENCH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
