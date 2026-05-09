#!/usr/bin/env python3
"""Generate one-task issue drafts from the scan-QC implementation plan."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "scan-qc.issue-plan.v1"
PLAN_SOURCE = "archive-scan-qc-retouch-design.md#94-生产级性能优化计划"
LINEAR_PROJECT = "AI4Archive scan-QC production optimization"
PUERSAI_BASELINE = {
    "sample": "fixed private 149-image set on puersai-hpc",
    "scan_files_per_minute": 137.93,
    "processing_files_per_minute": 111.61,
    "deskew_seconds": 122.636509,
    "despeckle_seconds": 108.368089,
}


@dataclass(frozen=True)
class IssueDraft:
    slug: str
    priority: str
    title: str
    goal: str
    scope: list[str]
    acceptance: list[str]
    validation: list[str]
    labels: list[str]


ISSUES: tuple[IssueDraft, ...] = (
    IssueDraft(
        slug="stabilize-aggregate-baseline",
        priority="P0",
        title="Stabilize puersai-hpc aggregate baseline and cleanup evidence",
        goal=(
            "Keep the aggregate baseline runner, timing fields, privacy self-check, "
            "and cleanup behavior stable before deeper performance changes."
        ),
        scope=[
            "Review aggregate_baseline_summary.json schema compatibility.",
            "Keep phase timings, throughput, worker settings, and cleanup results aggregate-only.",
            "Add regression coverage for cleanup retaining only public aggregate evidence.",
        ],
        acceptance=[
            "A single command produces comparable aggregate baseline JSON on puersai-hpc.",
            "Privacy self-check passes and public output contains no filenames, paths, hashes, thumbnails, OCR text, or row-level records.",
            "Cleanup mode deletes generated images and reports while preserving the input sample set.",
        ],
        validation=[
            "Run unit tests for aggregate baseline and private integration summaries locally.",
            "On puersai-hpc, run scripts/run_aggregate_baseline.py with the fixed 149-image private set and --cleanup-artifacts.",
            "Publish only aggregate_baseline_summary.json metrics in the PR or issue comment.",
        ],
        labels=["performance", "privacy", "puersai-hpc"],
    ),
    IssueDraft(
        slug="remove-repeat-decode-write",
        priority="P0",
        title="Reduce repeated image decode and low-value derivative writes",
        goal=(
            "Find and remove remaining redundant image reads or derivative rewrites in "
            "scan, processing-plan, and processing paths without weakening auditability."
        ),
        scope=[
            "Trace image open/read calls across scan, dry-run processing-plan, and processing execution.",
            "Reuse metadata or processing decisions where source content is unchanged.",
            "Avoid rewriting successful derivatives when resume-processing can prove they are current.",
        ],
        acceptance=[
            "Processing failures remain zero on the private validation sample.",
            "Processing throughput is measurably better than 111.61 files/minute or the no-gain reason is documented.",
            "Processing audit output still explains operations, skipped files, failures, and resume behavior.",
        ],
        validation=[
            "Run targeted processing and run-plan unit tests locally.",
            "On puersai-hpc, run the fixed 20-image private sample first with before/after aggregate summaries.",
            "If the 20-image sample passes, run the fixed 149-image baseline and compare aggregate throughput.",
        ],
        labels=["performance", "processing", "puersai-hpc"],
    ),
    IssueDraft(
        slug="vectorize-deskew-despeckle",
        priority="P0",
        title="Vectorize deskew and despeckle processing hotspots",
        goal=(
            "Replace the Pillow/Python-loop hotspots in deskew scoring and isolated "
            "speckle handling with a vectorized backend while keeping the existing safe fallback."
        ),
        scope=[
            "Profile deskew candidate scoring and despeckle candidate selection on the private sample.",
            "Implement an optional OpenCV/NumPy path for BOX projection scoring and speckle filtering.",
            "Keep the current Pillow implementation as a fallback for unsupported environments.",
        ],
        acceptance=[
            "Quality finding counts match the current baseline or every difference is explained.",
            "Processing throughput improves against the 111.61 files/minute baseline.",
            "Fallback mode remains available and covered by tests.",
        ],
        validation=[
            "Run local unit tests for processing metrics, deskew, despeckle, and fallback behavior.",
            "On puersai-hpc, pass the fixed 20-image sample before running the 149-image sample.",
            "For the 149-image run, compare operation timings against deskew 122.636509s and despeckle 108.368089s.",
        ],
        labels=["performance", "image-processing", "puersai-hpc"],
    ),
    IssueDraft(
        slug="libvips-streaming-io",
        priority="P1",
        title="Prototype libvips streaming IO for large image processing",
        goal=(
            "Evaluate libvips for large-image reads, crops, thumbnails, and derivative "
            "writes to reduce memory pressure and full-image copies."
        ),
        scope=[
            "Add an optional libvips backend behind an explicit capability check.",
            "Limit the first pass to IO-heavy operations where output equivalence can be verified.",
            "Document installation constraints for Linux and Windows targets.",
        ],
        acceptance=[
            "Large-image memory high-water mark drops or the no-gain reason is documented.",
            "Output audit fields remain stable and original images are never overwritten.",
            "Systems without libvips use the existing Pillow path without failing baseline QC.",
        ],
        validation=[
            "Run local unit tests with libvips unavailable to prove fallback behavior.",
            "On puersai-hpc, run the 20-image private sample and capture aggregate memory and throughput notes.",
            "Run the 149-image private sample only after fallback and privacy checks pass.",
        ],
        labels=["performance", "io", "optional-backend"],
    ),
    IssueDraft(
        slug="adaptive-worker-scheduling",
        priority="P1",
        title="Add adaptive worker recommendation for scan and processing",
        goal=(
            "Use CPU, memory, image-size, and benchmark evidence to recommend worker "
            "counts for scan and derivative processing without regressing manual overrides."
        ),
        scope=[
            "Separate scan, processing, and report-write worker decisions in aggregate evidence.",
            "Use benchmark worker sweeps as the primary recommendation source.",
            "Expose the recommendation in aggregate-only summaries and docs.",
        ],
        acceptance=[
            "1/2/4/8 worker benchmark on puersai-hpc yields explicit scan and processing recommendations.",
            "Manual --workers behavior remains unchanged.",
            "No source paths or row-level records are included in recommendation output.",
        ],
        validation=[
            "Run benchmark and run-plan unit tests locally.",
            "On puersai-hpc, run benchmark sweeps with the fixed private sample.",
            "Compare recommended workers against aggregate throughput and note any diminishing returns.",
        ],
        labels=["performance", "scheduling", "puersai-hpc"],
    ),
    IssueDraft(
        slug="gpu-model-provider-pilot",
        priority="P1",
        title="Pilot optional GPU/model provider for deep QC signals",
        goal=(
            "Add a non-blocking provider path for GPU-assisted blur, defect, layout, "
            "or truncation signals while preserving the CPU-only baseline."
        ),
        scope=[
            "Probe ONNX Runtime CUDA or PaddleOCR GPU availability without requiring it.",
            "Record aggregate provider readiness, GPU visibility, and inference throughput.",
            "Keep provider failures isolated from base scan-QC rules unless explicitly configured.",
        ],
        acceptance=[
            "GPU availability is reported when present and CPU fallback is automatic when absent.",
            "Base scan QC passes without model packages installed.",
            "Provider evidence is aggregate-only in public summaries.",
        ],
        validation=[
            "Run local capability-probe and provider-failure unit tests.",
            "On puersai-hpc, run capability-probe and a private sample provider smoke test if hardware is available.",
            "Publish only aggregate readiness and throughput fields.",
        ],
        labels=["gpu", "model-provider", "optional-backend"],
    ),
    IssueDraft(
        slug="offline-package-sbom",
        priority="P2",
        title="Prepare offline package, wheelhouse, SBOM, and license evidence",
        goal=(
            "Make release validation reproducible on puersai-hpc and fresh offline "
            "machines with pinned runtime dependencies and shareable supply-chain evidence."
        ),
        scope=[
            "Document wheelhouse build and install flow for supported Python versions.",
            "Generate SBOM and license summaries for runtime and optional backends.",
            "Keep private validation paths out of release artifacts.",
        ],
        acceptance=[
            "A fresh offline install can run release validation and aggregate private baseline validation.",
            "SBOM and license evidence are present for required packages.",
            "Release notes identify optional backend dependencies separately from the base package.",
        ],
        validation=[
            "Run offline dependency checks locally against a synthetic wheelhouse.",
            "On puersai-hpc, install from the prepared wheelhouse and run release validation.",
            "Run the aggregate baseline wrapper after install and publish only aggregate pass/fail evidence.",
        ],
        labels=["release", "offline", "supply-chain"],
    ),
)


def build_issue_plan() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project": LINEAR_PROJECT,
        "plan_source": PLAN_SOURCE,
        "privacy": {
            "public_output": True,
            "contains_credentials": False,
            "contains_private_paths": False,
            "credential_handling": "Do not store puersai-hpc credentials in repository files, generated plans, PRs, or Linear comments.",
        },
        "baseline": PUERSAI_BASELINE,
        "issues": [_issue_to_dict(issue) for issue in ISSUES],
    }


def write_issue_plan(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = build_issue_plan()
    json_path = output_dir / "scan_qc_issue_plan.json"
    markdown_path = output_dir / "scan_qc_issue_plan.md"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(plan), encoding="utf-8")
    return json_path, markdown_path


def _issue_to_dict(issue: IssueDraft) -> dict[str, Any]:
    return {
        "slug": issue.slug,
        "priority": issue.priority,
        "title": issue.title,
        "goal": issue.goal,
        "scope": issue.scope,
        "acceptance_criteria": issue.acceptance,
        "test_plan": {
            "target_machine": "puersai-hpc",
            "administrator_account": "ps",
            "credentials": "Use the approved secret channel; do not write credentials into issue bodies, PRs, or repository files.",
            "steps": issue.validation,
            "privacy_rules": [
                "Do not upload real images, filenames, paths, hashes, thumbnails, OCR text, or row-level findings.",
                "After validation, delete generated private images and reports unless they are retained in an approved private evidence store.",
                "Public comments may include only aggregate counts, timings, throughput, pass/fail status, and explained regressions.",
            ],
        },
        "labels": issue.labels,
    }


def _markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Scan-QC Linear Issue Plan",
        "",
        f"Source: `{plan['plan_source']}`",
        "",
        "Public sharing boundary: generated drafts are aggregate-only and do not contain credentials or private sample paths.",
        "",
        "Baseline reference:",
        "",
        f"- Sample: {plan['baseline']['sample']}",
        f"- Scan throughput: {plan['baseline']['scan_files_per_minute']} files/minute",
        f"- Processing throughput: {plan['baseline']['processing_files_per_minute']} files/minute",
        f"- Deskew timing: {plan['baseline']['deskew_seconds']} seconds",
        f"- Despeckle timing: {plan['baseline']['despeckle_seconds']} seconds",
        "",
    ]
    for index, issue in enumerate(plan["issues"], start=1):
        lines.extend(
            [
                f"## {index}. [{issue['priority']}] {issue['title']}",
                "",
                f"Slug: `{issue['slug']}`",
                "",
                issue["goal"],
                "",
                "Scope:",
                *[f"- {item}" for item in issue["scope"]],
                "",
                "Acceptance criteria:",
                *[f"- {item}" for item in issue["acceptance_criteria"]],
                "",
                "Test plan:",
                f"- Target machine: `{issue['test_plan']['target_machine']}`",
                f"- Administrator account: `{issue['test_plan']['administrator_account']}`",
                f"- Credentials: {issue['test_plan']['credentials']}",
                *[f"- {item}" for item in issue["test_plan"]["steps"]],
                "",
                "Privacy rules:",
                *[f"- {item}" for item in issue["test_plan"]["privacy_rules"]],
                "",
                f"Labels: {', '.join(issue['labels'])}",
                "",
            ]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate scan-QC Linear issue drafts from the production optimization plan.")
    parser.add_argument("--out", default=Path("generated/issue-plan"), type=Path, help="Output directory for JSON and Markdown drafts.")
    args = parser.parse_args(argv)
    json_path, markdown_path = write_issue_plan(args.out)
    print(f"Issue plan JSON: {json_path}")
    print(f"Issue plan Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
