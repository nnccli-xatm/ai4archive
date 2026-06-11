from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.public_capability_contract import (
    PUBLIC_CAPABILITY_CONTRACT_JSON,
    SCHEMA_VERSION,
    build_public_capability_contract,
)


class PublicCapabilityContractTests(unittest.TestCase):
    def test_contract_declares_stable_cli_and_schema_boundary(self) -> None:
        contract = build_public_capability_contract(generated_at="2026-06-09T00:00:00+00:00")

        self.assertEqual(contract["schema_version"], SCHEMA_VERSION)
        self.assertEqual(contract["status"], "pass")
        self.assertTrue(contract["privacy"]["public_safe"])
        self.assertFalse(contract["privacy"]["reads_source_images"])
        self.assertFalse(contract["privacy"]["runs_provider_commands"])

        stable_commands = {item["command"] for item in contract["public_cli"]["stable_commands"]}
        self.assertIn("archive-scan-qc production-run", stable_commands)
        self.assertIn("archive-scan-qc run-plan", stable_commands)
        self.assertIn("archive-scan-qc preflight", stable_commands)
        self.assertIn("archive-scan-qc image-processing-capability-smoke", stable_commands)
        self.assertIn("archive-scan-qc public-capability-contract", stable_commands)
        public_safe_commands = {item["command"] for item in contract["public_cli"]["public_safe_aggregate_commands"]}
        self.assertIn("archive-scan-qc rule-template-catalog", public_safe_commands)
        self.assertIn("archive-scan-qc rule-template-dry-run", public_safe_commands)
        self.assertIn("archive-scan-qc private-validation-aggregate", public_safe_commands)
        sensitive_commands = {item["command"] for item in contract["public_cli"]["sensitive_local_commands"]}
        self.assertIn("archive-scan-qc batch-rename-plan", sensitive_commands)
        self.assertIn("archive-scan-qc batch-rename-apply", sensitive_commands)
        self.assertIn("archive-scan-qc case-split-plan", sensitive_commands)
        self.assertIn("archive-scan-qc case-split-apply", sensitive_commands)
        prototype_commands = {item["command"] for item in contract["public_cli"]["prototype_or_validation_commands"]}
        self.assertIn("archive-scan-qc service-api", prototype_commands)

        artifacts = {item["name"]: item for item in contract["output_artifacts"]}
        self.assertEqual(artifacts["scan_qc_report.json"]["stability"], "stable_sensitive_local")
        self.assertEqual(artifacts["production_run_summary.json"]["stability"], "stable_sensitive_local")
        self.assertEqual(artifacts["production_run_progress.json"]["schema_version"], "scan-qc.production-run-progress.v1")
        self.assertEqual(artifacts["processing_retry_manifest.json"]["schema_version"], "scan-qc.processing.retry.v1")
        self.assertEqual(artifacts["processing_audit_summary.json"]["schema_version"], "scan-qc.processing.audit.v1")
        self.assertEqual(
            artifacts["processing_quality_summary.json"]["schema_version"],
            "scan-qc.processing-quality-summary.v1",
        )
        self.assertEqual(artifacts["processing_quality_summary.json"]["stability"], "stable_public_safe_aggregate")
        self.assertEqual(artifacts["rule_template_catalog.json"]["schema_version"], "scan-qc.rule-template-catalog.v1")
        self.assertEqual(artifacts["rule_template_dry_run.json"]["schema_version"], "scan-qc.rule-template-dry-run.v1")
        self.assertEqual(artifacts["rule_template_catalog.json"]["stability"], "stable_public_safe_aggregate")
        self.assertEqual(artifacts["rule_template_dry_run.json"]["stability"], "stable_public_safe_aggregate")
        self.assertEqual(artifacts["batch_rename_plan.json"]["schema_version"], "scan-qc.batch-rename-plan.v1")
        self.assertEqual(artifacts["batch_rename_plan.json"]["stability"], "stable_sensitive_local")
        self.assertEqual(artifacts["batch_rename_rollback.json"]["schema_version"], "scan-qc.batch-rename-rollback.v1")
        self.assertEqual(artifacts["batch_rename_rollback.json"]["stability"], "stable_sensitive_local")
        self.assertEqual(artifacts["case_split_plan.json"]["schema_version"], "scan-qc.case-split-plan.v1")
        self.assertEqual(artifacts["case_split_plan.json"]["stability"], "stable_sensitive_local")
        self.assertEqual(artifacts["case_split_rollback.json"]["schema_version"], "scan-qc.case-split-rollback.v1")
        self.assertEqual(artifacts["case_split_rollback.json"]["stability"], "stable_sensitive_local")
        self.assertEqual(artifacts["artifact_readiness_checklist.json"]["schema_version"], "scan-qc-artifact-readiness-checklist.v1")
        self.assertEqual(
            artifacts["service_job_public_summary.json"]["schema_version"],
            "scan-qc.service-job-public-summary.v1",
        )
        self.assertEqual(artifacts["service_job_public_summary.json"]["stability"], "prototype_or_validation")
        self.assertEqual(
            artifacts["service_job_index_public_summary.json"]["schema_version"],
            "scan-qc.service-job-index-public-summary.v1",
        )
        self.assertEqual(artifacts["service_job_index_public_summary.json"]["stability"], "prototype_or_validation")
        nested_schemas = {item["name"]: item for item in contract["nested_public_schemas"]}
        self.assertEqual(
            nested_schemas["service_job_public_summary.timings"]["schema_version"],
            "scan-qc.service-job-public-timings.v1",
        )
        self.assertEqual(
            nested_schemas["service_job_public_summary.timings"]["stability"],
            "prototype_or_validation",
        )
        self.assertEqual(
            nested_schemas["service_job_public_summary.source_integrity"]["schema_version"],
            "scan-qc.service-job-source-integrity.v1",
        )
        self.assertEqual(
            nested_schemas["service_job_public_summary.source_integrity"]["stability"],
            "prototype_or_validation",
        )
        self.assertEqual(
            nested_schemas["service_job_index_public_summary.recovery_issues"]["schema_version"],
            "scan-qc.service-job-index-recovery-issues.v1",
        )
        self.assertEqual(
            nested_schemas["service_job_index_public_summary.recovery_issues"]["stability"],
            "prototype_or_validation",
        )
        self.assertEqual(
            artifacts["image_processing_capability_smoke.json"]["schema_version"],
            "scan-qc.image-processing-capability-smoke.v1",
        )
        self.assertEqual(artifacts["public_capability_contract.json"]["stability"], "stable_public_safe_aggregate")
        self.assertEqual(
            artifacts["private_validation_aggregate_summary.json"]["schema_version"],
            "scan-qc.private-validation-aggregate.v1",
        )
        self.assertEqual(
            artifacts["private_validation_aggregate_summary.json"]["stability"],
            "stable_public_safe_aggregate",
        )

    def test_contract_separates_public_and_experimental_processing_backends(self) -> None:
        contract = build_public_capability_contract(generated_at="2026-06-09T00:00:00+00:00")
        processing = contract["processing_contract"]

        stable_backend_ids = {item["id"] for item in processing["stable_public_backends"]}
        self.assertIn("pillow_cpu_baseline", stable_backend_ids)
        self.assertIn("despeckle_fallback", stable_backend_ids)
        self.assertIn("despeckle_numpy", stable_backend_ids)

        experimental = {item["id"]: item for item in processing["internal_or_experimental_backends"]}
        self.assertFalse(experimental["despeckle_opencv"]["public_cli"])
        self.assertFalse(experimental["libvips_image_io"]["public_cli"])
        self.assertIn("built_in_ocr", processing["explicitly_out_of_scope"])
        self.assertFalse(processing["source_images_modified"])

    def test_contract_declares_service_quality_public_boundaries(self) -> None:
        contract = build_public_capability_contract(generated_at="2026-06-09T00:00:00+00:00")
        service = contract["service_contract"]

        self.assertEqual(service["status"], "prototype_or_validation")
        self.assertFalse(service["source_images_modified"])
        self.assertTrue(service["job_state_isolation"]["private_checkpoint_contains_local_paths"])
        self.assertFalse(service["job_state_isolation"]["public_surfaces_contain_local_paths"])

        surfaces = {item["id"]: item for item in service["public_surfaces"]}
        job_summary = surfaces["service_job_public_summary"]
        self.assertEqual(job_summary["schema_version"], "scan-qc.service-job-public-summary.v1")
        self.assertTrue(job_summary["may_include_job_id"])
        self.assertTrue(job_summary["may_include_quality_metrics"])
        self.assertIn("print_clean", job_summary["allowed_processing_profiles"])
        self.assertIn("ocr_preprocess_opencv_local", job_summary["allowed_processing_profiles"])
        self.assertIn("ocr_preprocess_sauvola_wolf", job_summary["allowed_processing_profiles"])
        self.assertIn("ocr_preprocess_stroke_bg", job_summary["allowed_processing_profiles"])
        self.assertIn("processing_profile:print_clean", job_summary["allowed_print_clean_context"])
        self.assertIn(
            "processing_profile:ocr_preprocess_opencv_local",
            job_summary["allowed_print_clean_context"],
        )
        self.assertIn(
            "processing_profile:ocr_preprocess_sauvola_wolf",
            job_summary["allowed_print_clean_context"],
        )
        self.assertIn(
            "processing_profile:ocr_preprocess_stroke_bg",
            job_summary["allowed_print_clean_context"],
        )
        self.assertIn("background_stains_delta", job_summary["allowed_print_clean_context"])
        self.assertIn("changed_pixel_ratio", job_summary["allowed_print_clean_context"])
        self.assertIn("whitelisted_quality_metrics", job_summary["allowed_quality_context"])
        self.assertIn("local_paths", job_summary["forbidden_content"])
        self.assertIn("filenames", job_summary["forbidden_content"])
        self.assertIn("hashes", job_summary["forbidden_content"])
        self.assertIn("local_review_items", job_summary["forbidden_content"])

        for aggregate_id in ("service_job_index_quality", "service_production_session_quality"):
            aggregate = surfaces[aggregate_id]
            self.assertFalse(aggregate["may_include_job_id"])
            self.assertFalse(aggregate["may_include_quality_metrics"])
            self.assertFalse(aggregate["may_include_processing_profile"])
            self.assertIn("quality_signal_status_counts", aggregate["allowed_quality_context"])
            self.assertIn("aggregate_file_counts", aggregate["allowed_quality_context"])
            self.assertIn("blocking_code_counts", aggregate["allowed_quality_context"])
            self.assertIn("job_ids", aggregate["forbidden_content"])
            self.assertIn("quality_rows", aggregate["forbidden_content"])
            self.assertIn("quality_metrics", aggregate["forbidden_content"])
            self.assertIn("processing_profiles", aggregate["forbidden_content"])

        finish_export = surfaces["production_finish_export"]
        self.assertEqual(finish_export["scope"], "POST /api/production/finish-export export readiness gate")
        self.assertTrue(finish_export["may_include_job_id"])
        self.assertIn("review_item_count", finish_export["allowed_review_gate_context"])
        self.assertIn("latest_completion_status", finish_export["allowed_review_gate_context"])
        self.assertIn("operator_review_required", finish_export["allowed_review_gate_blocking_codes"])
        self.assertIn("operator_review_incomplete", finish_export["allowed_review_gate_blocking_codes"])
        self.assertIn("local_ids", finish_export["forbidden_content"])
        self.assertIn("review_rows", finish_export["forbidden_content"])
        self.assertIn("raw_decision_rows", finish_export["forbidden_content"])

    def test_public_capability_contract_cli_writes_public_safe_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="public-capability-contract-") as temp_dir:
            out_dir = Path(temp_dir) / "contract"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["public-capability-contract", "--out", str(out_dir)])

            payload_path = out_dir / PUBLIC_CAPABILITY_CONTRACT_JSON
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            raw = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(exit_code, 0)
        self.assertIn("Image processing run: no", stdout.getvalue())
        self.assertIn("Tracked artifacts:", stdout.getvalue())
        self.assertIn("Public-safe aggregate artifacts:", stdout.getvalue())
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertTrue(payload["privacy"]["aggregate_only"])
        self.assertFalse(payload["privacy"]["contains_paths"])
        self.assertNotIn(temp_dir, raw)


if __name__ == "__main__":
    unittest.main()
