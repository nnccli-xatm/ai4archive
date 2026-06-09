"""Public capability contract for stable scan QC surfaces.

This module is intentionally static and public-safe. It does not inspect local
files, run image processing, probe hardware, or execute providers.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from ._version import __version__
from .processing_quality_summary import PROCESSING_QUALITY_SUMMARY_JSON
from .rule_templates import RULE_TEMPLATE_CATALOG_JSON, RULE_TEMPLATE_DRY_RUN_JSON


PUBLIC_CAPABILITY_CONTRACT_JSON = "public_capability_contract.json"
SCHEMA_VERSION = "scan-qc.public-capability-contract.v1"


def build_public_capability_contract(generated_at: str | None = None) -> dict[str, Any]:
    """Return the current public-safe capability and schema contract."""

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": "pass",
        "package": {
            "name": "ai4archive",
            "cli": "archive-scan-qc",
            "version": __version__,
        },
        "privacy": {
            "aggregate_only": True,
            "public_safe": True,
            "reads_source_images": False,
            "runs_image_processing": False,
            "runs_provider_commands": False,
            "contains_file_list": False,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_image_content": False,
            "contains_environment_values": False,
        },
        "stability_levels": [
            {
                "id": "stable_public_cli",
                "meaning": "Supported command-line surface with regression coverage and documented outputs.",
            },
            {
                "id": "stable_sensitive_local",
                "meaning": "Supported local artifact or command, but output can include row-level private evidence.",
            },
            {
                "id": "stable_public_safe_aggregate",
                "meaning": "Supported aggregate artifact intended for sharing after local policy review.",
            },
            {
                "id": "prototype_or_validation",
                "meaning": "Supported for rehearsal, smoke, validation, or local operator workflow checks.",
            },
            {
                "id": "internal_or_experimental",
                "meaning": "Implemented or scaffolded internally, but not part of the stable public CLI contract.",
            },
        ],
        "public_cli": {
            "stable_commands": [
                _command(
                    "archive-scan-qc",
                    "Scan an image directory and write local QC reports; optionally writes derivative images with --process-out.",
                    "scan-qc.phase1.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc preflight",
                    "Validate batch configuration and manifest risk before scanning or derivative processing.",
                    "scan-qc.preflight.v1",
                ),
                _command(
                    "archive-scan-qc production-run",
                    "Run a single production batch with progress, reports, derivative processing, and summary artifacts.",
                    "scan-qc.production-run.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc run-plan",
                    "Run multiple configured batches and write aggregate run-plan status.",
                    "scan-qc.run-plan-summary.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc benchmark",
                    "Run aggregate-only local performance benchmarks across worker counts.",
                    "scan-qc.benchmark.v1",
                ),
                _command(
                    "archive-scan-qc capability-probe",
                    "Report optional local GPU/model/provider package visibility without inference.",
                    "scan-qc.capability-probe.v1",
                ),
                _command(
                    "archive-scan-qc image-processing-capability-smoke",
                    "Run a public-safe synthetic scan and derivative-processing smoke check.",
                    "scan-qc.image-processing-capability-smoke.v1",
                ),
                _command(
                    "archive-scan-qc public-capability-contract",
                    "Write this public-safe capability and schema boundary contract.",
                    SCHEMA_VERSION,
                ),
            ],
            "sensitive_local_commands": [
                _command(
                    "archive-scan-qc processing-plan",
                    "Plan derivative processing candidates from a scan report without writing derivative images.",
                    "scan-qc.processing-plan.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc processing-review-package",
                    "Build a local JSON/HTML review package from processing_manifest.json.",
                    "scan-qc.processing-review.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc review-export",
                    "Export row-level reviewer templates from scan_qc_report.json.",
                    "scan-qc.review-template.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc acceptance-sampling-export",
                    "Export sensitive local acceptance sampling tasks from scan_qc_report.json.",
                    "scan-qc.acceptance-sampling.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc production-review-queue",
                    "Write a local operator review queue from QC and processing evidence.",
                    "scan-qc.production-review-queue.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc rework-action-list",
                    "Write a local operator rework list from findings and processing audit evidence.",
                    "scan-qc.rework-action-list.v1",
                    sensitive_outputs=True,
                ),
                _command(
                    "archive-scan-qc delivery-manifest",
                    "Write local handoff manifests for selected evidence artifacts.",
                    "scan-qc.delivery-handoff-manifest.v1",
                    sensitive_outputs=True,
                ),
            ],
            "public_safe_aggregate_commands": [
                _command(
                    "archive-scan-qc review-summary",
                    "Reduce a filled local review template to aggregate counts.",
                    "scan-qc.review-summary.v1",
                ),
                _command(
                    "archive-scan-qc review-decisions-verify",
                    "Verify browser-exported aggregate review decisions.",
                    "scan-qc.review-decision-verification-summary.v1",
                ),
                _command(
                    "archive-scan-qc calibrate-rules",
                    "Write aggregate rule-calibration recommendations from reports and review summaries.",
                    "scan-qc.rules-calibration.v1",
                ),
                _command(
                    "archive-scan-qc acceptance-summary",
                    "Write aggregate production acceptance gate status.",
                    "scan-qc.acceptance-summary.v1",
                ),
                _command(
                    "archive-scan-qc evidence-bundle-verify",
                    "Verify aggregate release/handoff evidence without row-level reads.",
                    "scan-qc.aggregate-evidence-bundle.v1",
                ),
                _command(
                    "archive-scan-qc final-handoff-summary",
                    "Write aggregate final production handoff status.",
                    "scan-qc.final-production-handoff-summary.v1",
                ),
                _command(
                    "archive-scan-qc public-safe-validation-index",
                    "Index approved aggregate validation artifacts.",
                    "scan-qc.public-safe-validation-index.v1",
                ),
                _command(
                    "archive-scan-qc artifact-readiness-checklist",
                    "Check aggregate/public-safe artifact readiness.",
                    "scan-qc-artifact-readiness-checklist.v1",
                ),
                _command(
                    "archive-scan-qc workbench-summary",
                    "Build public-safe aggregate workbench input.",
                    "scan-qc.workbench-public-summary.v1",
                ),
                _command(
                    "archive-scan-qc deep-inspection-provider-probe",
                    "Validate optional deep-inspection provider metadata without inference.",
                    "scan-qc.deep-inspection-provider.v1",
                ),
                _command(
                    "archive-scan-qc deep-inspection-candidate-summary",
                    "Summarize already-detected candidate counts for future optional deep inspection.",
                    "scan-qc.deep-inspection-candidates.v1",
                ),
                _command(
                    "archive-scan-qc rule-template-catalog",
                    "Write public-safe built-in rule template metadata.",
                    "scan-qc.rule-template-catalog.v1",
                ),
                _command(
                    "archive-scan-qc rule-template-dry-run",
                    "Write a public-safe aggregate dry-run plan for a rule template without derivative images.",
                    "scan-qc.rule-template-dry-run.v1",
                ),
            ],
            "prototype_or_validation_commands": [
                _command(
                    "archive-scan-qc production-rehearsal",
                    "Generate synthetic rehearsal artifacts for local operator workflow validation.",
                    "scan-qc.production-rehearsal.v1",
                ),
                _command(
                    "archive-scan-qc production-workbench",
                    "Launch the local operator workbench prototype.",
                    "scan-qc.local-production-workbench.v1",
                    sensitive_outputs=True,
                ),
            ],
        },
        "output_artifacts": [
            _artifact("preflight_report.json", "scan-qc.preflight.v1", "stable_public_safe_aggregate"),
            _artifact("scan_qc_report.json", "scan-qc.phase1.v1", "stable_sensitive_local"),
            _artifact("scan_qc_report.html", "scan-qc.phase1.v1", "stable_sensitive_local"),
            _artifact("scan_qc_files.csv", "scan-qc.phase1.v1", "stable_sensitive_local"),
            _artifact("scan_qc_findings.csv", "scan-qc.phase1.v1", "stable_sensitive_local"),
            _artifact("review_template.json", "scan-qc.review-template.v1", "stable_sensitive_local"),
            _artifact("review_template.csv", "scan-qc.review-template.v1", "stable_sensitive_local"),
            _artifact("acceptance_sampling_review.json", "scan-qc.acceptance-sampling.v1", "stable_sensitive_local"),
            _artifact("acceptance_sampling_review.csv", "scan-qc.acceptance-sampling.v1", "stable_sensitive_local"),
            _artifact("processing_manifest.json", "scan-qc.processing.v1", "stable_sensitive_local"),
            _artifact("processing_retry_manifest.json", "scan-qc.processing.retry.v1", "stable_sensitive_local"),
            _artifact("processing_review_package.json", "scan-qc.processing-review.v1", "stable_sensitive_local"),
            _artifact("processing_review_package.html", "scan-qc.processing-review.v1", "stable_sensitive_local"),
            _artifact("production_review_queue.json", "scan-qc.production-review-queue.v1", "stable_sensitive_local"),
            _artifact("rework_action_list.json", "scan-qc.rework-action-list.v1", "stable_sensitive_local"),
            _artifact("rework_action_list.csv", "scan-qc.rework-action-list.v1", "stable_sensitive_local"),
            _artifact("delivery_handoff_manifest.json", "scan-qc.delivery-handoff-manifest.v1", "stable_sensitive_local"),
            _artifact("delivery_handoff_manifest.csv", "scan-qc.delivery-handoff-manifest.v1", "stable_sensitive_local"),
            _artifact("production_run_progress.json", "scan-qc.production-run-progress.v1", "stable_sensitive_local"),
            _artifact("production_run_summary.json", "scan-qc.production-run.v1", "stable_sensitive_local"),
            _artifact("processing_audit_summary.json", "scan-qc.processing.audit.v1", "stable_public_safe_aggregate"),
            _artifact(PROCESSING_QUALITY_SUMMARY_JSON, "scan-qc.processing-quality-summary.v1", "stable_public_safe_aggregate"),
            _artifact("run_plan_summary.json", "scan-qc.run-plan-summary.v1", "stable_public_safe_aggregate"),
            _artifact("benchmark_results.json", "scan-qc.benchmark.v1", "stable_public_safe_aggregate"),
            _artifact("capability_probe.json", "scan-qc.capability-probe.v1", "stable_public_safe_aggregate"),
            _artifact(
                "image_processing_capability_smoke.json",
                "scan-qc.image-processing-capability-smoke.v1",
                "stable_public_safe_aggregate",
            ),
            _artifact("deep_inspection_provider_probe.json", "scan-qc.deep-inspection-provider.v1", "stable_public_safe_aggregate"),
            _artifact("deep_inspection_candidate_summary.json", "scan-qc.deep-inspection-candidates.v1", "stable_public_safe_aggregate"),
            _artifact("review_summary.json", "scan-qc.review-summary.v1", "stable_public_safe_aggregate"),
            _artifact(
                "review_decision_verification_summary.json",
                "scan-qc.review-decision-verification-summary.v1",
                "stable_public_safe_aggregate",
            ),
            _artifact("rules_calibration_summary.json", "scan-qc.rules-calibration.v1", "stable_public_safe_aggregate"),
            _artifact("acceptance_summary.json", "scan-qc.acceptance-summary.v1", "stable_public_safe_aggregate"),
            _artifact("aggregate_evidence_bundle_summary.json", "scan-qc.aggregate-evidence-bundle.v1", "stable_public_safe_aggregate"),
            _artifact("final_production_handoff_summary.json", "scan-qc.final-production-handoff-summary.v1", "stable_public_safe_aggregate"),
            _artifact("public_safe_validation_index.json", "scan-qc.public-safe-validation-index.v1", "stable_public_safe_aggregate"),
            _artifact("artifact_readiness_checklist.json", "scan-qc-artifact-readiness-checklist.v1", "stable_public_safe_aggregate"),
            _artifact("workbench_public_summary.json", "scan-qc.workbench-public-summary.v1", "stable_public_safe_aggregate"),
            _artifact(RULE_TEMPLATE_CATALOG_JSON, "scan-qc.rule-template-catalog.v1", "stable_public_safe_aggregate"),
            _artifact(RULE_TEMPLATE_DRY_RUN_JSON, "scan-qc.rule-template-dry-run.v1", "stable_public_safe_aggregate"),
            _artifact("service_job_public_summary.json", "scan-qc.service-job-public-summary.v1", "prototype_or_validation"),
            _artifact(
                "service_job_index_public_summary.json",
                "scan-qc.service-job-index-public-summary.v1",
                "prototype_or_validation",
            ),
            _artifact(PUBLIC_CAPABILITY_CONTRACT_JSON, SCHEMA_VERSION, "stable_public_safe_aggregate"),
        ],
        "processing_contract": {
            "source_images_modified": False,
            "derivative_processing_requires_process_out": True,
            "stable_operations": [
                "exif_transpose",
                "convert_non_l_or_rgb_to_rgb",
                "deskew",
                "trim_dark_border",
                "scanner_gutter_trim",
                "auto_crop",
                "despeckle",
                "normalize_tones",
                "normalize_paper_color_cast",
                "lighten_edge_shadow",
                "lighten_corner_shadows",
                "lighten_background_stains",
                "lighten_fold_shadows",
                "level_illumination_gradient",
                "clean_bleed_through",
                "lighten_scanlines",
                "enhance_faded_text",
                "sharpen_text_edges",
                "resume_processing",
                "reuse_scan_measurements",
            ],
            "stable_public_backends": [
                {
                    "id": "pillow_cpu_baseline",
                    "surface": "default",
                    "status": "stable_public_cli",
                    "required_dependency": "Pillow",
                },
                {
                    "id": "despeckle_fallback",
                    "surface": "--despeckle-backend fallback",
                    "status": "stable_public_cli",
                    "required_dependency": "Pillow",
                },
                {
                    "id": "despeckle_numpy",
                    "surface": "--despeckle-backend numpy",
                    "status": "stable_public_cli_optional",
                    "required_dependency": "numpy",
                    "fallback": "despeckle_fallback",
                },
            ],
            "internal_or_experimental_backends": [
                {
                    "id": "despeckle_opencv",
                    "surface": "ProcessingOptions.despeckle_backend='opencv'",
                    "public_cli": False,
                    "status": "internal_or_experimental",
                    "reason": "Implemented and unit-tested as an internal optional path, but not exposed by the stable CLI or run-plan contract.",
                },
                {
                    "id": "libvips_image_io",
                    "surface": "ProcessingOptions.image_io_backend='vips'",
                    "public_cli": False,
                    "status": "internal_or_experimental",
                    "reason": "Helper-level read/write path exists, but production CLI does not expose an image IO backend switch.",
                },
                {
                    "id": "gpu_model_providers",
                    "surface": "capability-probe and offline analysis-provider protocol only",
                    "public_cli": False,
                    "status": "internal_or_experimental",
                    "reason": "Probe and provider protocol are stable; built-in GPU/model inference is not part of current processing semantics.",
                },
            ],
            "explicitly_out_of_scope": [
                "in_place_source_image_modification",
                "cloud_or_network_image_upload",
                "built_in_ocr",
                "built_in_layout_or_restoration_model_inference",
                "dibco_specific_binarization_optimization",
                "generative_image_repair",
            ],
        },
    }


def write_public_capability_contract(report: dict[str, Any], output_path: Path) -> Path:
    path = output_path / PUBLIC_CAPABILITY_CONTRACT_JSON if output_path.suffix == "" else output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _command(
    command: str,
    purpose: str,
    primary_schema: str,
    *,
    sensitive_outputs: bool = False,
) -> dict[str, Any]:
    return {
        "command": command,
        "purpose": purpose,
        "primary_schema": primary_schema,
        "sensitive_outputs": sensitive_outputs,
    }


def _artifact(name: str, schema_version: str, stability: str) -> dict[str, str]:
    return {
        "name": name,
        "schema_version": schema_version,
        "stability": stability,
    }
