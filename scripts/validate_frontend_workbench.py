"""Validate the static scan-QC frontend workbench prototype."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


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
    "Public-Safe Artifact Compatibility Diagnostics",
    "Compatibility diagnostic uses aggregate/public-safe fields only",
    "Recognized Artifact Type",
    "Schema Version",
    "Schema/Type Detection",
    "Generated Timestamp Presence",
    "Privacy Summary",
    "Diagnostic Blocking Count",
    "Diagnostic Warning Count",
    "Expected aggregate status fields present",
    "Expected aggregate status fields missing",
    "unsupported_public_safe_schema_version",
    "generated_timestamp_missing",
    "aggregate_status_fields_missing",
    "privacy_summary_missing",
    "privacy_summary_fail",
    "artifact_compatibility_pass",
    "SUPPORTED_PUBLIC_SAFE_SCHEMA_PREFIXES",
    "buildArtifactCompatibilityDiagnostics",
    "Public-Safe Artifact Readiness Checklist",
    "Top-level readiness",
    "Ready for public handoff",
    "Not ready for public handoff",
    "Expected Artifacts",
    "Missing Artifacts",
    "Privacy Blockers",
    "Stale Artifacts",
    "Present/Missing",
    "Generated Timestamp",
    "artifact_readiness_checklist",
    "public_safe_artifact_readiness",
    "EXPECTED_PUBLIC_SAFE_ARTIFACTS",
    "buildArtifactReadinessChecklist",
    "artifactReadinessPanel",
    "excludes local preview filename, preview content, and object URL state",
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
    "Public-Safe Demo Fixture Gallery",
    "Synthetic aggregate-only fixtures for browser validation.",
    "Demo fixtures exclude private filenames, paths, hashes, OCR text, thumbnails, image content, manifests, row-level findings, reviewer notes, derivative image references, and local preview state.",
    "demoFixtureSelect",
    "loadDemoFixtureButton",
    "DEMO_FIXTURES",
    "loadDemoFixture",
    "cloneDemoPayload",
    "Recognized passing review summary",
    "Passing acceptance summary",
    "Complete public-safe readiness checklist",
    "Unsupported schema compatibility warning",
    "Privacy summary failing diagnostic",
    "Privacy summary missing diagnostic",
    "Loaded public-safe demo fixture",
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
    "compatibility diagnostics": "buildArtifactCompatibilityDiagnostics",
}

REQUIRED_CHECKLIST_FIELDS = {
    "expected artifacts": "EXPECTED_PUBLIC_SAFE_ARTIFACTS",
    "synthetic checklist input": "artifact_readiness_checklist",
    "alternate checklist input": "public_safe_artifact_readiness",
    "readiness model": "buildArtifactReadinessChecklist",
    "ready summary": "Ready for public handoff",
    "not-ready summary": "Not ready for public handoff",
    "privacy status": "privacyStatus",
    "generated timestamp": "generatedAt",
    "blocking count": "blockingCount",
    "warning count": "warningCount",
    "stale count": "staleCount",
}

REQUIRED_COMPATIBILITY_FIELDS = {
    "recognized artifact type": "Recognized Artifact Type",
    "schema version": "Schema Version",
    "schema/type detection": "Schema/Type Detection",
    "generated timestamp presence": "Generated Timestamp Presence",
    "privacy summary": "Privacy Summary",
    "blocking diagnostic count": "Diagnostic Blocking Count",
    "warning diagnostic count": "Diagnostic Warning Count",
    "expected fields present": "Expected aggregate status fields present",
    "expected fields missing": "Expected aggregate status fields missing",
    "unsupported schema warning": "unsupported_public_safe_schema_version",
    "privacy missing diagnostic": "privacy_summary_missing",
    "privacy failing diagnostic": "privacy_summary_fail",
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

REQUIRED_DEMO_FIXTURE_LABELS = {
    "Recognized passing review summary",
    "Passing acceptance summary",
    "Complete public-safe readiness checklist",
    "Unsupported schema compatibility warning",
    "Privacy summary failing diagnostic",
    "Privacy summary missing diagnostic",
}

FORBIDDEN_DEMO_FIXTURE_FIELDS = {
    "absolute_path",
    "derivative_image",
    "filename",
    "hash",
    "image_bytes",
    "image_content",
    "manifest",
    "ocr_text",
    "path",
    "preview_filename",
    "preview_object_url",
    "reviewer_notes",
    "sha256",
    "thumbnail",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a deterministic public-safe JSON validation summary to stdout.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Write a deterministic public-safe JSON validation summary to this path.",
    )
    parser.add_argument(
        "--self-test-json",
        action="store_true",
        help="Run focused self-tests for JSON success and synthetic failure output.",
    )
    parser.add_argument(
        "--workbench",
        type=Path,
        default=WORKBENCH,
        help=argparse.SUPPRESS,
    )
    return parser


def new_summary(workbench: Path) -> dict[str, Any]:
    return {
        "status": "fail",
        "validated_html_path": safe_workbench_path(workbench),
        "counts": {
            "required_regions": len(REQUIRED_REGIONS),
            "required_strings": len(REQUIRED_STRINGS),
            "required_aggregate_fields": len(REQUIRED_AGGREGATE_FIELDS),
            "required_checklist_fields": len(REQUIRED_CHECKLIST_FIELDS),
            "required_compatibility_fields": len(REQUIRED_COMPATIBILITY_FIELDS),
            "required_demo_fixture_labels": len(REQUIRED_DEMO_FIXTURE_LABELS),
            "forbidden_pattern_checks": len(FORBIDDEN_PATTERNS),
            "forbidden_export_field_checks": len(FORBIDDEN_EXPORT_FIELDS),
            "forbidden_aggregate_payload_field_checks": len(FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS),
            "forbidden_demo_fixture_field_checks": len(FORBIDDEN_DEMO_FIXTURE_FIELDS),
        },
        "fixture_groups": {
            "aggregate_executable_fixture_groups": 6,
            "demo_fixture_labels_required": len(REQUIRED_DEMO_FIXTURE_LABELS),
        },
        "coverage": {
            "aggregate_summary": False,
            "review_acceptance": False,
            "compatibility_diagnostics": False,
            "readiness_checklist": False,
            "demo_fixtures": False,
            "executable_fixtures": False,
        },
        "privacy": {
            "forbidden_pattern_checks_passed": False,
            "review_export_forbidden_field_checks_passed": False,
            "review_import_forbidden_field_checks_passed": False,
            "aggregate_payload_forbidden_field_checks_passed": False,
            "demo_fixture_forbidden_field_checks_passed": False,
            "forbidden_field_check_count": (
                len(FORBIDDEN_EXPORT_FIELDS) * 2
                + len(FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS)
                + len(FORBIDDEN_DEMO_FIXTURE_FIELDS)
            ),
        },
        "error_count": 0,
        "errors": [],
    }


def safe_workbench_path(workbench: Path) -> str:
    try:
        return str(workbench.resolve().relative_to(ROOT))
    except ValueError:
        return workbench.name


def add_error(summary: dict[str, Any], code: str, message: str) -> None:
    summary["errors"].append({"code": code, "message": message})


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
    blocking_item_count: 0,
    blocking_items: [],
    warnings: [],
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

  const completeChecklistFixture = {{
    schema_version: "scan-qc-artifact-readiness-checklist.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "pass",
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitivity: "aggregate-only public summary",
    artifact_readiness_checklist: {{
      "run_plan_summary.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:00:00Z"
      }},
      "review_summary.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:01:00Z"
      }},
      "acceptance_summary.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:02:00Z"
      }},
      "aggregate_evidence_bundle_summary.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:03:00Z"
      }},
      "release_candidate_summary.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:04:00Z"
      }},
      "final_production_handoff_summary.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:05:00Z"
      }}
    }}
  }};

  const missingChecklistFixture = {{
    schema_version: "scan-qc-artifact-readiness-checklist.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "fail",
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitivity: "aggregate-only public summary",
    artifact_readiness_checklist: [
      {{
        artifact: "run_plan_summary.json",
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 1,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:00:00Z"
      }},
      {{
        artifact: "review_summary.json",
        present: true,
        status: "stale",
        blocking_count: 1,
        warning_count: 2,
        privacy_status: "public-safe",
        generated_at: "2026-05-10T00:00:00Z"
      }},
      {{
        artifact: "acceptance_summary.json",
        present: false,
        status: "missing",
        blocking_count: 1,
        warning_count: 0,
        privacy_status: "not evaluated"
      }}
    ]
  }};

  const unsupportedSchemaFixture = {{
    schema_version: "scan-qc-artifact-readiness-checklist.v99",
    generated_at: "2026-05-11T00:00:00Z",
    status: "pass",
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitivity: "aggregate-only public summary",
    artifact_readiness_checklist: {{
      "run_plan_summary.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:00:00Z"
      }}
    }}
  }};

  const failingPrivacyFixture = {{
    schema_version: "scan-qc-acceptance-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "fail",
    acceptance_passed: false,
    blocking_item_count: 1,
    blocking_items: [{{ code: "aggregate_privacy_hold" }}],
    warnings: [{{ code: "aggregate_warning_review_backlog" }}],
    privacy: {{
      aggregate_only: true,
      redacts_private_values: false,
      private_indicators_found: true,
      private_indicator_count: 1,
      contains_paths: true
    }},
    privacy_self_check: {{
      status: "failed",
      violation_count: 1
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const missingPrivacyFixture = {{
    schema_version: "scan-qc-review-summary.v1",
    generated_at: "2026-05-11T00:00:00Z",
    status: "pass",
    status_counts: {{
      accepted_issue: 1
    }}
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
  assert(els.aggregateHandoff.innerHTML.includes("Public-Safe Artifact Compatibility Diagnostics"), "review fixture did not render compatibility diagnostics");
  assert(els.aggregateHandoff.innerHTML.includes("artifact_compatibility_pass"), "review fixture did not render compatibility pass code");
  assert(els.aggregateHandoff.innerHTML.includes("Schema/Type Detection"), "review fixture did not render schema/type detection");

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
  assert(els.aggregateHandoff.innerHTML.includes("Privacy Summary"), "acceptance fixture did not render privacy summary diagnostic");

  const completeChecklistModel = inferArtifact(completeChecklistFixture);
  assert(completeChecklistModel.sourceType === "aggregate-handoff", "complete checklist fixture did not load as aggregate handoff");
  assert(completeChecklistModel.artifactReadiness.ready === true, "complete checklist fixture was not ready");
  assert(completeChecklistModel.artifactReadiness.missingCount === 0, "complete checklist fixture reported missing artifacts");
  assert(completeChecklistModel.artifactReadiness.rows.length === 6, "complete checklist fixture did not cover six expected artifacts");
  state.model = completeChecklistModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("Public-Safe Artifact Readiness Checklist"), "complete checklist did not render checklist heading");
  assert(els.aggregateHandoff.innerHTML.includes("Ready for public handoff"), "complete checklist did not render ready summary");
  assert(els.aggregateHandoff.innerHTML.includes("final_production_handoff_summary.json"), "complete checklist did not render final handoff artifact");
  assert(!els.aggregateHandoff.innerHTML.includes("blob:aggregate-fixture"), "complete checklist rendered object URL state");

  const missingChecklistModel = inferArtifact(missingChecklistFixture);
  assert(missingChecklistModel.artifactReadiness.ready === false, "missing checklist fixture was unexpectedly ready");
  assert(missingChecklistModel.artifactReadiness.missingCount >= 1, "missing checklist fixture did not count missing artifacts");
  assert(missingChecklistModel.artifactReadiness.blockingCount === 2, "missing checklist fixture did not preserve blocking counts");
  assert(missingChecklistModel.artifactReadiness.warningCount === 3, "missing checklist fixture did not preserve warning counts");
  assert(missingChecklistModel.artifactReadiness.staleCount === 1, "missing checklist fixture did not count stale artifact");
  state.model = missingChecklistModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("Not ready for public handoff"), "missing checklist did not render not-ready summary");
  assert(els.aggregateHandoff.innerHTML.includes("missing"), "missing checklist did not render missing status");
  assert(els.aggregateHandoff.innerHTML.includes("stale"), "missing checklist did not render stale status");

  const unsupportedSchemaModel = inferArtifact(unsupportedSchemaFixture);
  assert(unsupportedSchemaModel.aggregateHandoff.artifactType === "Public-safe artifact readiness checklist", "unsupported schema fixture did not classify by public-safe artifact type");
  assert(unsupportedSchemaModel.artifactCompatibility.schemaRecognized === false, "unsupported schema fixture was unexpectedly recognized");
  assert(unsupportedSchemaModel.artifactCompatibility.warningCount >= 1, "unsupported schema fixture did not produce warning count");
  state.model = unsupportedSchemaModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("unsupported_public_safe_schema_version"), "unsupported schema fixture did not render unsupported schema warning");
  assert(els.aggregateHandoff.innerHTML.includes("unsupported or unknown"), "unsupported schema fixture did not render unknown schema wording");

  const failingPrivacyModel = inferArtifact(failingPrivacyFixture);
  assert(failingPrivacyModel.artifactCompatibility.privacySummaryStatus === "fail", "failing privacy fixture did not produce privacy fail status");
  assert(failingPrivacyModel.artifactCompatibility.blockingCount >= 1, "failing privacy fixture did not produce blocking count");
  state.model = failingPrivacyModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("privacy_summary_fail"), "failing privacy fixture did not render privacy failure diagnostic");
  assert(els.aggregateHandoff.innerHTML.includes("Diagnostic Blocking Count"), "failing privacy fixture did not render diagnostic blocking count");

  const missingPrivacyModel = inferArtifact(missingPrivacyFixture);
  assert(missingPrivacyModel.artifactCompatibility.privacySummaryStatus === "missing", "missing privacy fixture did not produce privacy missing status");
  state.model = missingPrivacyModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("privacy_summary_missing"), "missing privacy fixture did not render privacy missing diagnostic");

  assert(Array.isArray(DEMO_FIXTURES), "demo fixture gallery is not an array");
  assert(DEMO_FIXTURES.length >= 5, "demo fixture gallery does not cover at least five options");
  const demoLabels = DEMO_FIXTURES.map(item => item.label);
  [
    "Recognized passing review summary",
    "Passing acceptance summary",
    "Complete public-safe readiness checklist",
    "Unsupported schema compatibility warning",
    "Privacy summary failing diagnostic",
    "Privacy summary missing diagnostic"
  ].forEach(label => assert(demoLabels.includes(label), "missing demo fixture label: " + label));

  DEMO_FIXTURES.forEach(item => {{
    assert(item && item.id && item.label && item.payload, "demo fixture is missing id, label, or payload");
    const serialized = JSON.stringify(item.payload);
    [
      "filename",
      "path",
      "hash",
      "sha256",
      "ocr_text",
      "thumbnail",
      "image_content",
      "image_bytes",
      "manifest",
      "reviewer_notes",
      "derivative_image",
      "preview_filename",
      "preview_object_url"
    ].forEach(field => assert(!serialized.includes(String.fromCharCode(34) + field + String.fromCharCode(34)), "demo fixture " + item.id + " includes forbidden field " + field));
    const model = inferArtifact(cloneDemoPayload(item.payload));
    assert(model.sourceType === "aggregate-handoff", "demo fixture " + item.id + " did not load through aggregate inference");
    state.model = model;
    renderAggregateHandoff();
    assert(els.aggregateHandoff.innerHTML.includes("Public-Safe Artifact Compatibility Diagnostics"), "demo fixture " + item.id + " did not render diagnostics");
    assert(els.aggregateHandoff.innerHTML.includes("Aggregate-only Status"), "demo fixture " + item.id + " did not render aggregate summary");
  }});

  loadDemoFixture("recognized-review-pass");
  assert(els.status.textContent.includes("Loaded public-safe demo fixture: Recognized passing review summary."), "demo load button path did not report selected fixture");
  assert(els.aggregateHandoff.innerHTML.includes("Review summary"), "demo load path did not render review summary");
  loadDemoFixture("complete-readiness-checklist");
  assert(els.aggregateHandoff.innerHTML.includes("Public-Safe Artifact Readiness Checklist"), "demo load path did not render readiness checklist");
  loadDemoFixture("unsupported-schema-warning");
  assert(els.aggregateHandoff.innerHTML.includes("unsupported_public_safe_schema_version"), "demo load path did not render unsupported schema diagnostic");
  loadDemoFixture("privacy-diagnostic-fail");
  assert(els.aggregateHandoff.innerHTML.includes("privacy_summary_fail"), "demo load path did not render failing privacy diagnostic");
  loadDemoFixture("privacy-diagnostic-missing");
  assert(els.aggregateHandoff.innerHTML.includes("privacy_summary_missing"), "demo load path did not render missing privacy diagnostic");
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


def validate_workbench(workbench: Path = WORKBENCH) -> dict[str, Any]:
    summary = new_summary(workbench)
    errors = summary["errors"]

    if not workbench.exists():
        add_error(summary, "missing_workbench", f"Missing workbench: {safe_workbench_path(workbench)}")
        return finalize_summary(summary)

    html = workbench.read_text(encoding="utf-8")

    for region in sorted(REQUIRED_REGIONS):
        if f'data-region="{region}"' not in html:
            add_error(summary, "missing_required_region", f"missing data-region={region!r}")

    for required in sorted(REQUIRED_STRINGS):
        if required not in html:
            add_error(summary, "missing_required_string", f"missing required string {required!r}")

    summary["coverage"]["review_acceptance"] = not any(
        error["code"] == "missing_required_string"
        and (
            "review_summary.json" in error["message"]
            or "acceptance_summary.json" in error["message"]
            or "Human Review Decisions" in error["message"]
        )
        for error in errors
    )

    for label, pattern in FORBIDDEN_PATTERNS.items():
        match = pattern.search(html)
        if match:
            add_error(summary, "forbidden_pattern_found", f"found forbidden {label}: {match.group(0)!r}")
    summary["privacy"]["forbidden_pattern_checks_passed"] = not any(
        error["code"] == "forbidden_pattern_found" for error in errors
    )

    export_start = html.find('schema: "scan-qc-review-decisions.local.v1"')
    if export_start == -1:
        add_error(summary, "missing_review_export_builder", "missing privacy-safe review export builder")
    else:
        export_block = html[export_start : html.find("function resetReviewState", export_start)]
        for field in sorted(FORBIDDEN_EXPORT_FIELDS):
            if re.search(rf"\b{re.escape(field)}\b\s*:", export_block):
                add_error(
                    summary,
                    "forbidden_review_export_field",
                    f"review export includes forbidden field {field!r}",
                )
    summary["privacy"]["review_export_forbidden_field_checks_passed"] = not any(
        error["code"] in {"missing_review_export_builder", "forbidden_review_export_field"} for error in errors
    )

    import_start = html.find("function parseReviewDecisionSummary")
    if import_start == -1:
        add_error(summary, "missing_review_import_parser", "missing privacy-safe review import parser")
    else:
        import_block = html[import_start : html.find("function clearPreviewState", import_start)]
        for field in sorted(FORBIDDEN_EXPORT_FIELDS):
            if re.search(rf"\b{re.escape(field)}\b\s*:", import_block):
                add_error(
                    summary,
                    "forbidden_review_import_field",
                    f"review import reads forbidden field {field!r}",
                )
    summary["privacy"]["review_import_forbidden_field_checks_passed"] = not any(
        error["code"] in {"missing_review_import_parser", "forbidden_review_import_field"} for error in errors
    )

    aggregate_start = html.find("function buildAggregateHandoffModel")
    aggregate_end = html.find("function normalizeStatus", aggregate_start)
    if aggregate_start == -1 or aggregate_end == -1:
        add_error(summary, "missing_aggregate_model_builder", "missing aggregate summary model builder")
    else:
        aggregate_block = html[aggregate_start:aggregate_end]
        for label, required in sorted(REQUIRED_AGGREGATE_FIELDS.items()):
            if required not in aggregate_block:
                add_error(
                    summary,
                    "missing_aggregate_field",
                    f"aggregate summary builder missing {label}: {required!r}",
                )
        for label, required in sorted(REQUIRED_CHECKLIST_FIELDS.items()):
            if required not in aggregate_block:
                add_error(
                    summary,
                    "missing_readiness_field",
                    f"artifact readiness checklist builder missing {label}: {required!r}",
                )
        for label, required in sorted(REQUIRED_COMPATIBILITY_FIELDS.items()):
            if required not in html:
                add_error(
                    summary,
                    "missing_compatibility_field",
                    f"artifact compatibility diagnostics missing {label}: {required!r}",
                )
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
                add_error(
                    summary,
                    "missing_aggregate_fragment",
                    f"aggregate summary builder missing {label}: {fragment!r}",
                )
        for label, field in sorted(FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS.items()):
            pattern = rf"\bpayload\.{re.escape(field)}\b|\bpayload\[['\"]{re.escape(field)}['\"]\]"
            if re.search(pattern, aggregate_block):
                add_error(
                    summary,
                    "forbidden_aggregate_payload_field",
                    f"aggregate summary builder reads forbidden {label} field {field!r}",
                )
    summary["coverage"]["aggregate_summary"] = not any(
        error["code"] in {"missing_aggregate_model_builder", "missing_aggregate_field", "missing_aggregate_fragment"}
        for error in errors
    )
    summary["coverage"]["compatibility_diagnostics"] = not any(
        error["code"] == "missing_compatibility_field" for error in errors
    )
    summary["coverage"]["readiness_checklist"] = not any(
        error["code"] == "missing_readiness_field" for error in errors
    )
    summary["privacy"]["aggregate_payload_forbidden_field_checks_passed"] = not any(
        error["code"] == "forbidden_aggregate_payload_field" for error in errors
    )

    demo_start = html.find("const DEMO_FIXTURES = [")
    demo_end = html.find("const els = {", demo_start)
    if demo_start == -1 or demo_end == -1:
        add_error(summary, "missing_demo_fixture_gallery", "missing public-safe demo fixture gallery data")
    else:
        demo_block = html[demo_start:demo_end]
        for label in sorted(REQUIRED_DEMO_FIXTURE_LABELS):
            if label not in demo_block:
                add_error(summary, "missing_demo_fixture_label", f"demo fixture gallery missing label {label!r}")
        for field in sorted(FORBIDDEN_DEMO_FIXTURE_FIELDS):
            if re.search(rf"['\"]{re.escape(field)}['\"]\s*:", demo_block):
                add_error(
                    summary,
                    "forbidden_demo_fixture_field",
                    f"demo fixture gallery includes forbidden field {field!r}",
                )
        if "URL.createObjectURL" in demo_block or "preview" in demo_block.lower():
            add_error(
                summary,
                "demo_fixture_preview_state",
                "demo fixture gallery must not include local preview object URL or filename state",
            )
    summary["coverage"]["demo_fixtures"] = not any(
        error["code"] in {"missing_demo_fixture_gallery", "missing_demo_fixture_label"} for error in errors
    )
    summary["privacy"]["demo_fixture_forbidden_field_checks_passed"] = not any(
        error["code"] in {"forbidden_demo_fixture_field", "demo_fixture_preview_state"} for error in errors
    )

    render_start = html.find("function renderAggregateHandoff")
    render_end = html.find("function workerRange", render_start)
    if render_start == -1 or render_end == -1:
        add_error(summary, "missing_aggregate_renderer", "missing aggregate summary renderer")
    else:
        render_block = html[render_start:render_end]
        expected_labels = {
            "Acceptance Passed",
            "Blocking And Warning Codes",
            "Blocking Count",
            "Generated Timestamp",
            "Missing Artifacts",
            "Omitted private evidence",
            "Present/Missing",
            "Privacy Status",
            "Public-Safe Artifact Readiness Checklist",
            "Processing Workers",
            "Public-Safe Artifact Compatibility Diagnostics",
            "Recognized Artifact Type",
            "Review Status Counts",
            "Rule Counts",
            "Rule Status Counts",
            "Scan Workers",
            "Schema Version",
            "Schema/Type Detection",
        }
        for label in sorted(expected_labels):
            if label not in render_block:
                add_error(summary, "missing_renderer_label", f"aggregate summary renderer missing label {label!r}")

    if "http://" in html or "https://" in html:
        add_error(summary, "external_network_url", "workbench should not depend on external network URLs")

    for error in validate_executable_aggregate_fixtures(html):
        add_error(summary, "executable_fixture_failure", error)
    summary["coverage"]["executable_fixtures"] = not any(
        error["code"] == "executable_fixture_failure" for error in errors
    )

    return finalize_summary(summary)


def finalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    summary["error_count"] = len(summary["errors"])
    summary["status"] = "pass" if summary["error_count"] == 0 else "fail"
    return summary


def emit_json_summary(summary: dict[str, Any], json_out: Path | None) -> None:
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if json_out is None:
        print(text, end="")
        return
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(text, encoding="utf-8")


def run_json_self_tests() -> int:
    success = validate_workbench(WORKBENCH)
    if success["status"] != "pass":
        print("JSON self-test failed: expected success status for current workbench.", file=sys.stderr)
        return 1
    if success["error_count"] != 0 or success["errors"]:
        print("JSON self-test failed: success summary included errors.", file=sys.stderr)
        return 1
    required_success_keys = {
        "status",
        "validated_html_path",
        "counts",
        "fixture_groups",
        "coverage",
        "privacy",
        "error_count",
        "errors",
    }
    if set(success) != required_success_keys:
        print("JSON self-test failed: success summary keys changed.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="frontend-workbench-json-self-test-") as temp_dir:
        failure_path = Path(temp_dir) / "missing-workbench.html"
        failure = validate_workbench(failure_path)
    if failure["status"] != "fail" or failure["error_count"] != 1:
        print("JSON self-test failed: expected one synthetic failure.", file=sys.stderr)
        return 1
    if failure["errors"] != [{"code": "missing_workbench", "message": "Missing workbench: missing-workbench.html"}]:
        print("JSON self-test failed: synthetic failure error shape changed.", file=sys.stderr)
        return 1

    json.loads(json.dumps(success, sort_keys=True))
    json.loads(json.dumps(failure, sort_keys=True))
    print("JSON summary self-tests passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_test_json:
        return run_json_self_tests()

    summary = validate_workbench(args.workbench)

    if args.json or args.json_out is not None:
        emit_json_summary(summary, args.json_out)

    if summary["errors"]:
        print("Frontend workbench validation failed:", file=sys.stderr)
        for error in summary["errors"]:
            print(f"- {error['message']}", file=sys.stderr)
        return 1

    if not args.json:
        print(f"Validated {summary['validated_html_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
