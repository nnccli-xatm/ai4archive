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
    "public_safe_validation_index.json",
    "scan-qc.public-safe-validation-index.v1",
    "Public-safe validation index",
    "Public-Safe Validation Index",
    "Artifacts Present",
    "Artifacts Failed",
    "Artifacts Missing",
    "Unknown Inputs",
    "Validation Checks Passed",
    "Validation Checks Failed",
    "Validation Blocking Item Count",
    "Privacy Aggregate-only Status",
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
    "Validation codes",
    "private_field_omitted",
    "unsupported_decision_status",
    "unknown_review_target",
    "unsupported_field_omitted",
    "invalid_decision_entry",
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
    "Provider Capability Probe",
    "Provider Count",
    "Configured Provider Count",
    "Disabled Provider Count",
    "Providers Configured",
    "Visible GPU Count",
    "Visible Model Count",
    "Optional Package Visible Count",
    "Optional Package Missing Count",
    "Probe Privacy Status",
    "aggregateWarningCodes",
    "aggregateNestedStatusCounts",
    "aggregateWorkers",
    "aggregateProviderProbe",
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
    "provider capability probe": "aggregateProviderProbe",
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
    "Passing final production handoff",
    "Blocked final production handoff",
    "Disabled provider capability probe",
    "Passing public-safe validation index",
    "Blocked public-safe validation index",
    "Unsupported schema compatibility warning",
    "Privacy summary failing diagnostic",
    "Privacy summary missing diagnostic",
}

REQUIRED_PREVIEW_LIFECYCLE_STRINGS = {
    "clear function": "function clearPreviewState",
    "load function": "function loadPreviewFile",
    "create object URL": "URL.createObjectURL(file)",
    "clear revocation": "URL.revokeObjectURL(state.preview.objectUrl)",
    "replacement revocation": "if (state.preview.objectUrl)",
    "beforeunload revocation": 'window.addEventListener("beforeunload"',
    "export exclusion": "Preview is excluded from review-decision export JSON.",
    "local tab copy": "browser tab only",
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

PRIVATE_OUTPUT_PATTERNS = (
    re.compile(r"blob:[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"/Users/[A-Za-z0-9._/-]+"),
    re.compile(r"/private/[A-Za-z0-9._/-]+"),
    re.compile(r"\b[A-Za-z]:\\\\[^\s\"'<>]+"),
)


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
            "required_preview_lifecycle_strings": len(REQUIRED_PREVIEW_LIFECYCLE_STRINGS),
            "forbidden_pattern_checks": len(FORBIDDEN_PATTERNS),
            "forbidden_export_field_checks": len(FORBIDDEN_EXPORT_FIELDS),
            "forbidden_aggregate_payload_field_checks": len(FORBIDDEN_AGGREGATE_PAYLOAD_FIELDS),
            "forbidden_demo_fixture_field_checks": len(FORBIDDEN_DEMO_FIXTURE_FIELDS),
        },
        "fixture_groups": {
            "aggregate_executable_fixture_groups": 9,
            "demo_fixture_labels_required": len(REQUIRED_DEMO_FIXTURE_LABELS),
        },
        "coverage": {
            "aggregate_summary": False,
            "review_acceptance": False,
            "review_decision_import_export": False,
            "compatibility_diagnostics": False,
            "readiness_checklist": False,
            "demo_fixtures": False,
            "final_handoff_fixtures": False,
            "provider_capability_probe": False,
            "executable_fixtures": False,
            "preview_lifecycle": False,
        },
        "privacy": {
            "forbidden_pattern_checks_passed": False,
            "review_export_forbidden_field_checks_passed": False,
            "review_import_forbidden_field_checks_passed": False,
            "aggregate_payload_forbidden_field_checks_passed": False,
            "demo_fixture_forbidden_field_checks_passed": False,
            "preview_lifecycle_public_safe": False,
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
    summary["errors"].append({"code": code, "message": sanitize_public_message(message)})


def sanitize_public_message(message: str) -> str:
    safe = message
    for pattern in PRIVATE_OUTPUT_PATTERNS:
        safe = pattern.sub("[redacted-private-value]", safe)
    return safe


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
      }},
      "public_safe_validation_index.json": {{
        present: true,
        status: "pass",
        blocking_count: 0,
        warning_count: 0,
        privacy_status: "public-safe",
        generated_at: "2026-05-11T00:06:00Z"
      }}
    }}
  }};

  const finalHandoffPassFixture = {{
    schema_version: "scan-qc-final-production-handoff-summary.v1",
    generated_at: "2026-05-11T01:00:00Z",
    status: "pass",
    ready_for_handoff: true,
    checks_passed: 6,
    checks_failed: 0,
    blocking_item_count: 0,
    blocking_items: [],
    warnings: [],
    artifact_status_summary: {{
      "run_plan_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "review_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "acceptance_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "aggregate_evidence_bundle_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "release_candidate_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "final_production_handoff_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }}
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
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const finalHandoffBlockedFixture = {{
    schema_version: "scan-qc-final-production-handoff-summary.v1",
    generated_at: "2026-05-11T01:10:00Z",
    status: "fail",
    ready_for_handoff: false,
    checks_passed: 4,
    checks_failed: 2,
    blocking_item_count: 2,
    blocking_items: [
      {{ code: "aggregate_handoff_acceptance_blocker" }},
      {{ code: "aggregate_handoff_artifact_blocker" }}
    ],
    warnings: [{{ code: "aggregate_warning_handoff_recheck" }}],
    artifact_status_summary: {{
      "run_plan_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "review_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "acceptance_summary.json": {{ present: true, required: true, status: "fail", reported_status: "blocked", privacy_status: "public-safe" }},
      "aggregate_evidence_bundle_summary.json": {{ present: true, required: true, status: "pass", reported_status: "current", privacy_status: "public-safe" }},
      "release_candidate_summary.json": {{ present: true, required: true, status: "blocked", reported_status: "blocked", privacy_status: "public-safe" }},
      "final_production_handoff_summary.json": {{ present: true, required: true, status: "fail", reported_status: "blocked", privacy_status: "public-safe" }}
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
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    privacy_self_check: {{
      status: "passed",
      violation_count: 0
    }},
    sensitivity: "aggregate-only public summary"
  }};

  const validationIndexPassFixture = {{
    schema_version: "scan-qc.public-safe-validation-index.v1",
    generated_at: "2026-05-11T01:12:00Z",
    status: "pass",
    summary: {{
      known_artifacts: 5,
      artifacts_present: 5,
      artifacts_passed: 5,
      artifacts_failed: 0,
      artifacts_missing: 0,
      unknown_inputs: 0,
      checks_passed: 12,
      checks_failed: 0,
      blocking_item_count: 0
    }},
    checks_passed: 12,
    checks_failed: 0,
    artifact_presence: {{
      "frontend_workbench_validation.json": {{ present: true, category: "frontend_workbench_validation", status: "pass", reported_status: "pass" }},
      "release_readiness_summary.json": {{ present: true, category: "release_readiness", status: "pass", reported_status: "pass" }},
      "release_candidate_summary.json": {{ present: true, category: "release_candidate", status: "pass", reported_status: "pass" }},
      "aggregate_evidence_bundle_summary.json": {{ present: true, category: "aggregate_evidence_bundle", status: "pass", reported_status: "pass" }},
      "final_production_handoff_summary.json": {{ present: true, category: "final_production_handoff", status: "pass", reported_status: "pass" }}
    }},
    blocking_items: [],
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitive_values_omitted: true
  }};

  const validationIndexBlockedFixture = {{
    schema_version: "scan-qc.public-safe-validation-index.v1",
    generated_at: "2026-05-11T01:14:00Z",
    status: "fail",
    summary: {{
      known_artifacts: 5,
      artifacts_present: 3,
      artifacts_passed: 2,
      artifacts_failed: 1,
      artifacts_missing: 2,
      unknown_inputs: 1,
      checks_passed: 7,
      checks_failed: 4,
      blocking_item_count: 3
    }},
    checks_passed: 7,
    checks_failed: 4,
    artifact_presence: {{
      "frontend_workbench_validation.json": {{ present: true, category: "frontend_workbench_validation", status: "pass", reported_status: "pass" }},
      "release_readiness_summary.json": {{ present: true, category: "release_readiness", status: "fail", reported_status: "fail" }},
      "release_candidate_summary.json": {{ present: false, category: "release_candidate", status: "missing", reported_status: null }},
      "aggregate_evidence_bundle_summary.json": {{ present: true, category: "aggregate_evidence_bundle", status: "pass", reported_status: "pass" }},
      "final_production_handoff_summary.json": {{ present: false, category: "final_production_handoff", status: "missing", reported_status: null }}
    }},
    blocking_items: [
      {{ category: "release_readiness", code: "artifact_status_failed" }},
      {{ category: "release_candidate", code: "aggregate_artifact_missing" }},
      {{ category: "unknown", code: "unknown_public_safe_artifact" }}
    ],
    privacy: {{
      aggregate_only: true,
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
    }},
    sensitive_values_omitted: true
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

  const providerProbeFixture = {{
    schema_version: "scan-qc-provider-capability-probe-summary.v1",
    generated_at: "2026-05-11T01:20:00Z",
    status: "blocked",
    blocking_item_count: 0,
    blocking_items: [],
    warnings: [
      {{ code: "provider_probe_optional_packages_not_installed" }},
      {{ code: "provider_probe_gpu_not_visible" }}
    ],
    capability_probe_summary: {{
      provider_count: 3,
      configured_provider_count: 0,
      disabled_provider_count: 3,
      visible_gpu_count: 0,
      visible_model_count: 0,
      optional_package_visible_count: 1,
      optional_package_missing_count: 2,
      privacy_status: "public-safe",
      providers_configured: false
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
      redacts_private_values: true,
      private_indicators_found: false,
      private_indicator_count: 0
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
  assert(completeChecklistModel.artifactReadiness.rows.length === 7, "complete checklist fixture did not cover seven expected artifacts");
  state.model = completeChecklistModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("Public-Safe Artifact Readiness Checklist"), "complete checklist did not render checklist heading");
  assert(els.aggregateHandoff.innerHTML.includes("Ready for public handoff"), "complete checklist did not render ready summary");
  assert(els.aggregateHandoff.innerHTML.includes("final_production_handoff_summary.json"), "complete checklist did not render final handoff artifact");
  assert(!els.aggregateHandoff.innerHTML.includes("blob:aggregate-fixture"), "complete checklist rendered object URL state");

  const finalHandoffPassModel = inferArtifact(finalHandoffPassFixture);
  assert(finalHandoffPassModel.sourceType === "aggregate-handoff", "passing final handoff fixture did not load as aggregate handoff");
  assert(finalHandoffPassModel.aggregateHandoff.artifactType === "Final production handoff summary", "passing final handoff fixture did not classify as final handoff");
  assert(finalHandoffPassModel.aggregateHandoff.status === "pass", "passing final handoff status was not pass");
  assert(finalHandoffPassModel.aggregateHandoff.readyFlag === true, "passing final handoff ready flag was not true");
  assert(finalHandoffPassModel.aggregateHandoff.checksPassed === 6, "passing final handoff checks passed were not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.checksFailed === 0, "passing final handoff checks failed were not preserved");
  assert(finalHandoffPassModel.aggregateHandoff.blockingItemCount === 0, "passing final handoff blocking count was not zero");
  state.model = finalHandoffPassModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("Final production handoff summary"), "passing final handoff did not render final handoff type");
  assert(els.aggregateHandoff.innerHTML.includes("Ready for handoff"), "passing final handoff did not render ready flag label");
  assert(els.aggregateHandoff.innerHTML.includes("Checks Passed"), "passing final handoff did not render checks passed");
  assert(els.aggregateHandoff.innerHTML.includes("Checks Failed"), "passing final handoff did not render checks failed");
  assert(els.aggregateHandoff.innerHTML.includes("Artifact Presence And Status"), "passing final handoff did not render artifact status summary");
  assert(els.aggregateHandoff.innerHTML.includes("Privacy Status"), "passing final handoff did not render privacy status");

  const finalHandoffBlockedModel = inferArtifact(finalHandoffBlockedFixture);
  assert(finalHandoffBlockedModel.sourceType === "aggregate-handoff", "blocked final handoff fixture did not load as aggregate handoff");
  assert(finalHandoffBlockedModel.aggregateHandoff.artifactType === "Final production handoff summary", "blocked final handoff fixture did not classify as final handoff");
  assert(finalHandoffBlockedModel.aggregateHandoff.status === "fail", "blocked final handoff status was not fail");
  assert(finalHandoffBlockedModel.aggregateHandoff.readyFlag === false, "blocked final handoff ready flag was not false");
  assert(finalHandoffBlockedModel.aggregateHandoff.checksPassed === 4, "blocked final handoff checks passed were not preserved");
  assert(finalHandoffBlockedModel.aggregateHandoff.checksFailed === 2, "blocked final handoff checks failed were not preserved");
  assert(finalHandoffBlockedModel.aggregateHandoff.blockingItemCount === 2, "blocked final handoff blocking count was not preserved");
  assert(finalHandoffBlockedModel.aggregateHandoff.blockingCodes.includes("aggregate_handoff_acceptance_blocker"), "blocked final handoff blocker code was not preserved");
  assert(finalHandoffBlockedModel.artifactReadiness.ready === false, "blocked final handoff readiness was unexpectedly ready");
  state.model = finalHandoffBlockedModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_handoff_acceptance_blocker"), "blocked final handoff did not render blocker code");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_handoff_artifact_blocker"), "blocked final handoff did not render second blocker code");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_warning_handoff_recheck"), "blocked final handoff did not render warning code");
  assert(els.aggregateHandoff.innerHTML.includes("Not ready for public handoff"), "blocked final handoff did not render not-ready checklist summary");

  const validationIndexPassModel = inferArtifact(validationIndexPassFixture);
  assert(validationIndexPassModel.sourceType === "aggregate-handoff", "passing validation index fixture did not load as aggregate handoff");
  assert(validationIndexPassModel.aggregateHandoff.artifactType === "Public-safe validation index", "passing validation index fixture did not classify as validation index");
  assert(validationIndexPassModel.aggregateHandoff.status === "pass", "passing validation index status was not pass");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.artifactsPresent === 5, "passing validation index artifacts_present was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.artifactsFailed === 0, "passing validation index artifacts_failed was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.artifactsMissing === 0, "passing validation index artifacts_missing was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.unknownInputs === 0, "passing validation index unknown_inputs was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.checksPassed === 12, "passing validation index checks_passed was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.checksFailed === 0, "passing validation index checks_failed was not preserved");
  assert(validationIndexPassModel.aggregateHandoff.validationIndex.blockingItemCount === 0, "passing validation index blocking count was not preserved");
  state.model = validationIndexPassModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("Public-Safe Validation Index"), "passing validation index did not render index section");
  assert(els.aggregateHandoff.innerHTML.includes("Artifacts Present"), "passing validation index did not render artifacts_present");
  assert(els.aggregateHandoff.innerHTML.includes("Privacy Aggregate-only Status"), "passing validation index did not render aggregate-only privacy status");
  assert(els.aggregateHandoff.innerHTML.includes("frontend_workbench_validation.json"), "passing validation index did not render known public-safe filename");

  const validationIndexBlockedModel = inferArtifact(validationIndexBlockedFixture);
  assert(validationIndexBlockedModel.sourceType === "aggregate-handoff", "blocked validation index fixture did not load as aggregate handoff");
  assert(validationIndexBlockedModel.aggregateHandoff.artifactType === "Public-safe validation index", "blocked validation index fixture did not classify as validation index");
  assert(validationIndexBlockedModel.aggregateHandoff.status === "fail", "blocked validation index status was not fail");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.artifactsFailed === 1, "blocked validation index artifacts_failed was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.artifactsMissing === 2, "blocked validation index artifacts_missing was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.unknownInputs === 1, "blocked validation index unknown_inputs was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.checksFailed === 4, "blocked validation index checks_failed was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.validationIndex.blockingItemCount === 3, "blocked validation index blocking count was not preserved");
  assert(validationIndexBlockedModel.aggregateHandoff.blockingCodes.includes("artifact_status_failed"), "blocked validation index blocker code was not preserved");
  state.model = validationIndexBlockedModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_artifact_missing"), "blocked validation index did not render missing artifact blocker code");
  assert(els.aggregateHandoff.innerHTML.includes("unknown_public_safe_artifact"), "blocked validation index did not render unknown input blocker code");
  assert(!els.aggregateHandoff.innerHTML.includes("source_value"), "blocked validation index rendered source values");

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

  const providerProbeModel = inferArtifact(providerProbeFixture);
  assert(providerProbeModel.sourceType === "aggregate-handoff", "provider probe fixture did not load as aggregate handoff");
  assert(providerProbeModel.aggregateHandoff.artifactType === "Provider capability probe summary", "provider probe fixture did not classify as provider capability probe");
  assert(providerProbeModel.aggregateHandoff.status === "fail", "provider probe blocked status did not normalize to fail");
  assert(providerProbeModel.aggregateHandoff.providerProbe.providerCount === 3, "provider probe provider count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.configuredProviderCount === 0, "provider probe configured provider count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.providersConfigured === false, "provider probe configured flag was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.visibleGpuCount === 0, "provider probe GPU count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.visibleModelCount === 0, "provider probe model count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.optionalPackageVisibleCount === 1, "provider probe optional package visible count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.optionalPackageMissingCount === 2, "provider probe optional package missing count was not preserved");
  assert(providerProbeModel.aggregateHandoff.providerProbe.privacyStatus === "public-safe", "provider probe privacy status was not preserved");
  state.model = providerProbeModel;
  renderAggregateHandoff();
  assert(els.aggregateHandoff.innerHTML.includes("Provider Capability Probe"), "provider probe did not render probe section");
  assert(els.aggregateHandoff.innerHTML.includes("Configured Provider Count"), "provider probe did not render configured count");
  assert(els.aggregateHandoff.innerHTML.includes("Visible GPU Count"), "provider probe did not render GPU count");
  assert(els.aggregateHandoff.innerHTML.includes("Visible Model Count"), "provider probe did not render model count");
  assert(els.aggregateHandoff.innerHTML.includes("Optional Package Missing Count"), "provider probe did not render optional package count");
  assert(els.aggregateHandoff.innerHTML.includes("Probe Privacy Status"), "provider probe did not render privacy status");
  assert(els.aggregateHandoff.innerHTML.includes("provider_probe_gpu_not_visible"), "provider probe did not render warning code");

  assert(Array.isArray(DEMO_FIXTURES), "demo fixture gallery is not an array");
  assert(DEMO_FIXTURES.length >= 5, "demo fixture gallery does not cover at least five options");
  const demoLabels = DEMO_FIXTURES.map(item => item.label);
  [
    "Recognized passing review summary",
    "Passing acceptance summary",
    "Complete public-safe readiness checklist",
    "Passing final production handoff",
    "Blocked final production handoff",
    "Passing public-safe validation index",
    "Blocked public-safe validation index",
    "Disabled provider capability probe",
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
  loadDemoFixture("final-handoff-pass");
  assert(els.aggregateHandoff.innerHTML.includes("Final production handoff summary"), "demo load path did not render passing final handoff");
  assert(els.aggregateHandoff.innerHTML.includes("Ready for handoff"), "demo load path did not render passing final handoff ready flag");
  loadDemoFixture("final-handoff-blocked");
  assert(els.aggregateHandoff.innerHTML.includes("aggregate_handoff_acceptance_blocker"), "demo load path did not render blocked final handoff");
  assert(els.aggregateHandoff.innerHTML.includes("Not ready for public handoff"), "demo load path did not render blocked final handoff readiness");
  loadDemoFixture("validation-index-pass");
  assert(els.aggregateHandoff.innerHTML.includes("Public-safe validation index"), "demo load path did not render passing validation index type");
  assert(els.aggregateHandoff.innerHTML.includes("Artifacts Present"), "demo load path did not render passing validation index summary");
  loadDemoFixture("validation-index-blocked");
  assert(els.aggregateHandoff.innerHTML.includes("unknown_public_safe_artifact"), "demo load path did not render blocked validation index blocker");
  assert(els.aggregateHandoff.innerHTML.includes("Artifacts Missing"), "demo load path did not render blocked validation index missing count");
  loadDemoFixture("provider-capability-probe-disabled");
  assert(els.aggregateHandoff.innerHTML.includes("Provider capability probe summary"), "demo load path did not render provider probe type");
  assert(els.aggregateHandoff.innerHTML.includes("Provider Capability Probe"), "demo load path did not render provider probe section");
  assert(els.aggregateHandoff.innerHTML.includes("provider_probe_optional_packages_not_installed"), "demo load path did not render provider probe warning");
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


def validate_executable_preview_lifecycle(html: str) -> list[str]:
    script_match = re.search(r"<script>(?P<script>.*?)</script>", html, re.DOTALL)
    if not script_match:
        return ["missing executable workbench script"]

    runner = f"""
const vm = require("node:vm");
const workbenchScript = {script_match.group("script")!r};
const elements = new Map();
const createdUrls = [];
const revokedUrls = [];
const eventHandlers = {{}};

function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      value: "",
      innerHTML: "",
      textContent: "",
      className: "",
      disabled: false,
      files: [],
      dataset: {{}},
      classList: {{
        add() {{}},
        remove() {{}}
      }},
      addEventListener(type, handler) {{
        eventHandlers[id + ":" + type] = handler;
      }},
      querySelectorAll() {{
        return [];
      }},
      click() {{}}
    }});
  }}
  return elements.get(id);
}}

const context = {{
  assert,
  assertPublicSafe,
  console,
  createdUrls,
  revokedUrls,
  eventHandlers,
  Blob: function Blob() {{}},
  Date,
  Map,
  Number,
  Set,
  String,
  JSON,
  Array,
  URL: {{
    createObjectURL(file) {{
      const url = "blob:synthetic-preview-" + file.name + "-" + createdUrls.length;
      createdUrls.push(url);
      return url;
    }},
    revokeObjectURL(url) {{
      revokedUrls.push(url);
    }}
  }},
  document: {{
    getElementById: element,
    createElement: element
  }},
  window: {{
    addEventListener(type, handler) {{
      eventHandlers["window:" + type] = handler;
    }}
  }}
}};

function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}

function assertPublicSafe(value, label) {{
  const text = typeof value === "string" ? value : JSON.stringify(value);
  [
    "blob:synthetic-preview",
    "private_scan",
    "/Users/",
    "OCR_SECRET",
    "manifest_row",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ].forEach(token => assert(!text.includes(token), label + " leaked " + token));
}}

vm.createContext(context);
vm.runInContext(workbenchScript + `
  const firstFile = {{ name: "private_scan_alpha.tif", type: "image/tiff" }};
  const secondFile = {{ name: "private_scan_beta.png", type: "image/png" }};

  loadPreviewFile(firstFile);
  assert(state.preview.fileName === firstFile.name, "first preview filename was not tracked locally");
  assert(state.preview.objectUrl === "blob:synthetic-preview-private_scan_alpha.tif-0", "first object URL was not created");
  assert(createdUrls.length === 1, "first preview did not call createObjectURL once");
  assert(revokedUrls.length === 0, "first preview unexpectedly revoked a URL");
  assert(els.preview.innerHTML.includes("blob:synthetic-preview-private_scan_alpha.tif-0"), "preview image did not render object URL locally");

  state.model = {{
    sourceType: "scan-report",
    metrics: {{ totalBatches: 0, totalFindings: 0, p0: 0, p1: 0, p2: 0 }},
    batches: [],
    findings: []
  }};
  const exportWhilePreviewLoaded = JSON.stringify(buildReviewSummary());
  assertPublicSafe(exportWhilePreviewLoaded, "review export");

  state.model = inferArtifact(cloneDemoPayload(DEMO_FIXTURES.find(item => item.id === "recognized-review-pass").payload));
  renderAggregateHandoff();
  assertPublicSafe(els.aggregateHandoff.innerHTML, "aggregate handoff");

  loadDemoFixture("complete-readiness-checklist");
  assertPublicSafe(JSON.stringify(DEMO_FIXTURES), "demo fixtures");
  assertPublicSafe(els.aggregateHandoff.innerHTML, "demo fixture render");
  assertPublicSafe(els.status.textContent, "demo fixture status");

  loadPreviewFile(secondFile);
  assert(state.preview.fileName === secondFile.name, "replacement preview filename was not tracked locally");
  assert(state.preview.objectUrl === "blob:synthetic-preview-private_scan_beta.png-1", "replacement object URL was not created");
  assert(createdUrls.length === 2, "replacement preview did not call createObjectURL");
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_alpha.tif-0"), "replacement did not revoke first object URL");

  clearPreviewState();
  assert(state.preview.fileName === "", "clear did not reset preview filename");
  assert(state.preview.objectUrl === "", "clear did not reset preview object URL");
  assert(els.previewFile.value === "", "clear did not reset preview file input");
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_beta.png-1"), "clear did not revoke replacement object URL");
  assert(!els.preview.innerHTML.includes("blob:synthetic-preview"), "clear left object URL in preview markup");
  assert(!els.previewPrivacyCopy.innerHTML.includes("private_scan"), "clear left private filename in preview status");

  loadPreviewFile(firstFile);
  assert(typeof eventHandlers["window:beforeunload"] === "function", "beforeunload revocation handler was not registered");
  eventHandlers["window:beforeunload"]();
  assert(revokedUrls.includes("blob:synthetic-preview-private_scan_alpha.tif-2"), "beforeunload did not revoke active object URL");
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
        return ["Node.js is required for executable preview lifecycle checks but was not found on PATH"]
    finally:
        runner_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"node exited {completed.returncode}"
        return [f"executable preview lifecycle check failed: {detail}"]
    return []


def validate_executable_review_decision_import_export(html: str) -> list[str]:
    script_match = re.search(r"<script>(?P<script>.*?)</script>", html, re.DOTALL)
    if not script_match:
        return ["missing executable workbench script"]

    runner = f"""
const vm = require("node:vm");
const workbenchScript = {script_match.group("script")!r};
const elements = new Map();
const eventHandlers = {{}};

function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      value: "",
      innerHTML: "",
      textContent: "",
      className: "",
      disabled: false,
      files: [],
      dataset: {{}},
      classList: {{
        add(cls) {{ this.ownerClass = cls; }},
        remove() {{}}
      }},
      addEventListener(type, handler) {{
        eventHandlers[id + ":" + type] = handler;
      }},
      querySelectorAll() {{
        return [];
      }},
      click() {{}}
    }});
  }}
  return elements.get(id);
}}

const context = {{
  assert,
  assertPublicSafe,
  console,
  Blob: function Blob() {{}},
  Date,
  Map,
  Number,
  Object,
  Set,
  String,
  JSON,
  Array,
  URL: {{
    createObjectURL() {{ return "blob:synthetic-download"; }},
    revokeObjectURL() {{}}
  }},
  document: {{
    getElementById: element,
    createElement: element
  }},
  window: {{
    addEventListener(type, handler) {{
      eventHandlers["window:" + type] = handler;
    }}
  }}
}};

function assert(condition, message) {{
  if (!condition) throw new Error(message);
}}

function assertPublicSafe(value, label) {{
  const text = typeof value === "string" ? value : JSON.stringify(value);
  [
    "/Users/",
    "C:\\\\",
    "PRIVATE_OCR_TEXT",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "private_scan_alpha.tif",
    "blob:synthetic-preview"
  ].forEach(token => assert(!text.includes(token), label + " leaked " + token));
}}

vm.createContext(context);
vm.runInContext(workbenchScript + `
  const scanReport = {{
    schema_version: "scan-qc-report.v1",
    generated_at: "2026-05-11T00:00:00Z",
    project: {{ project_id: "synthetic-public-project" }},
    manifest: {{ batch_id: "synthetic-batch" }},
    summary: {{
      total_files: 3,
      openable_files: 3,
      total_findings: 2,
      p0_findings: 0,
      p1_findings: 1,
      p2_findings: 1
    }},
    findings: [
      {{ rule: "skew_detected", severity: "P1", source: "rules", confidence: "medium", message: "Synthetic skew prompt" }},
      {{ rule: "blur_detected", severity: "P2", source: "rules", confidence: "low", message: "Synthetic blur prompt" }}
    ]
  }};

  state.model = inferArtifact(scanReport);
  state.selectedBatchId = state.model.batches[0].id;
  render();

  assert(reviewTargets().length === 3, "synthetic scan report did not create expected review targets");
  state.decisions.set(decisionKey("batch", "B0001"), "accepted_issue");
  state.decisions.set(decisionKey("finding", "F0001"), "false_positive");
  state.decisions.set(decisionKey("finding", "F0002"), "needs_rescan");
  const exported = buildReviewSummary();
  assert(exported.schema === "scan-qc-review-decisions.local.v1", "review export schema changed");
  assert(exported.source_type === "scan-report", "review export source type was not preserved");
  assert(exported.source_target_count === 3, "review export target count was not preserved");
  assert(exported.review_counts.accepted_issue === 1, "review export accepted count was not preserved");
  assert(exported.review_counts.false_positive === 1, "review export false-positive count was not preserved");
  assert(exported.review_counts.needs_rescan === 1, "review export needs-rescan count was not preserved");
  assert(exported.decisions.every(item => Object.keys(item).sort().join(",") === "decision,local_id,scope"), "review export included unexpected decision fields");
  assertPublicSafe(exported, "review export summary");

  resetReviewState();
  renderReview();
  assert(getDecision("batch", "B0001") === "pending", "reset did not clear batch decision");
  applyReviewDecisionSummary(exported);
  assert(state.importStatus.imported === 3, "valid review summary did not import all decisions");
  assert(state.importStatus.skipped === 0, "valid review summary skipped entries");
  assert(getDecision("batch", "B0001") === "accepted_issue", "batch decision was not restored");
  assert(getDecision("finding", "F0001") === "false_positive", "finding decision was not restored");
  assert(getDecision("finding", "F0002") === "needs_rescan", "second finding decision was not restored");
  assert(els.reviewImportStatus.textContent.includes("Imported 3 review decisions; skipped 0."), "valid import status did not render aggregate counts");
  assertPublicSafe(els.reviewImportStatus.textContent, "valid import status");

  const invalidPayload = JSON.parse(JSON.stringify(exported));
  invalidPayload.decisions = [
    {{ scope: "batch", local_id: "B0001", decision: "fixed_externally" }},
    {{ scope: "finding", local_id: "F0001", decision: "false_positive", ocr_text: "PRIVATE_OCR_TEXT" }},
    {{ scope: "finding", local_id: "F0002", decision: "needs_rescan", hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }},
    {{ scope: "finding", local_id: "F9999", decision: "accepted_issue" }},
    {{ scope: "finding", local_id: "F0002", decision: "not_a_status" }},
    {{ scope: "finding", local_id: "F0002", decision: "accepted_issue", reviewer_notes: "synthetic note" }},
    {{ scope: "finding", local_id: "F0002", decision: "accepted_issue", extra_public_field: "ignored" }},
    "bad-entry"
  ];
  applyReviewDecisionSummary(invalidPayload);
  assert(state.importStatus.imported === 1, "invalid/private-bearing summary imported wrong count");
  assert(state.importStatus.skipped === 7, "invalid/private-bearing summary skipped wrong count");
  [
    "private_field_omitted=3",
    "unknown_review_target=1",
    "unsupported_decision_status=1",
    "unsupported_field_omitted=1",
    "invalid_decision_entry=1"
  ].forEach(code => assert(state.importStatus.validationCodes.includes(code), "missing aggregate validation code " + code));
  assert(els.reviewImportStatus.textContent.includes("Validation codes:"), "invalid import status did not render validation codes");
  assertPublicSafe(els.reviewImportStatus.textContent, "invalid import status");
  assertPublicSafe(state.exportSummary, "post-import export summary");
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
        return ["Node.js is required for executable review import/export checks but was not found on PATH"]
    finally:
        runner_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"node exited {completed.returncode}"
        return [f"executable review import/export check failed: {detail}"]
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
            add_error(summary, "forbidden_pattern_found", f"found forbidden {label}")
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
    demo_block = ""
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
    summary["coverage"]["final_handoff_fixtures"] = (
        summary["coverage"]["demo_fixtures"]
        and not any(
            error["code"] == "missing_demo_fixture_label"
            and (
                "Passing final production handoff" in error["message"]
                or "Blocked final production handoff" in error["message"]
            )
            for error in errors
        )
    )
    summary["coverage"]["provider_capability_probe"] = (
        summary["coverage"]["demo_fixtures"]
        and not any(
            error["code"] == "missing_demo_fixture_label"
            and "Disabled provider capability probe" in error["message"]
            for error in errors
        )
        and "scan-qc-provider-capability-probe-summary.v1" in demo_block
        and "capability_probe_summary" in demo_block
    )
    summary["privacy"]["demo_fixture_forbidden_field_checks_passed"] = not any(
        error["code"] in {"forbidden_demo_fixture_field", "demo_fixture_preview_state"} for error in errors
    )

    preview_start = html.find("function renderPreview")
    preview_end = html.find("async function loadFile", preview_start)
    if preview_start == -1 or preview_end == -1:
        add_error(summary, "missing_preview_lifecycle_block", "missing local preview lifecycle functions")
    else:
        preview_block = html[preview_start:preview_end]
        for label, required in sorted(REQUIRED_PREVIEW_LIFECYCLE_STRINGS.items()):
            search_area = html if label in {"beforeunload revocation", "local tab copy"} else preview_block
            if required not in search_area:
                add_error(
                    summary,
                    "missing_preview_lifecycle_string",
                    f"preview lifecycle missing {label}: {required!r}",
                )

    for error in validate_executable_preview_lifecycle(html):
        add_error(summary, "preview_lifecycle_failure", error)
    summary["coverage"]["preview_lifecycle"] = not any(
        error["code"] in {"missing_preview_lifecycle_block", "missing_preview_lifecycle_string", "preview_lifecycle_failure"}
        for error in errors
    )
    summary["privacy"]["preview_lifecycle_public_safe"] = summary["coverage"]["preview_lifecycle"]

    for error in validate_executable_review_decision_import_export(html):
        add_error(summary, "review_import_export_failure", error)
    summary["coverage"]["review_decision_import_export"] = not any(
        error["code"] == "review_import_export_failure" for error in errors
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
            "Provider Capability Probe",
            "Provider Count",
            "Configured Provider Count",
            "Visible GPU Count",
            "Visible Model Count",
            "Optional Package Missing Count",
            "Probe Privacy Status",
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

    with tempfile.TemporaryDirectory(prefix="frontend-workbench-json-self-test-") as temp_dir:
        unsafe_path = Path(temp_dir) / "unsafe-workbench.html"
        unsafe_path.write_text("<html><script>const leaked = '/Users/example/private_scan.tif blob:synthetic-preview-secret';</script></html>", encoding="utf-8")
        unsafe_failure = validate_workbench(unsafe_path)
    unsafe_serialized = json.dumps(unsafe_failure, sort_keys=True)
    if "blob:synthetic-preview-secret" in unsafe_serialized or "/Users/example/" in unsafe_serialized:
        print("JSON self-test failed: synthetic failure echoed private-looking details.", file=sys.stderr)
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
