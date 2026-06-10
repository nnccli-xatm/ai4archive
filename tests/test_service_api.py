from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from archive_scan_qc.service_api import (
    cancel_job_response,
    create_job_response,
    get_job_local_preview_response,
    get_job_local_review_artifact_response,
    get_job_review_history_response,
    get_job_response,
    get_rule_template_response,
    list_rule_templates_response,
    production_finish_export_response,
    production_progress_response,
    production_review_actions_response,
    production_review_history_response,
    production_review_queue_response,
    production_session_response,
    production_setup_response,
    production_start_response,
    recover_jobs_response,
    run_job_response,
    save_rule_template_response,
    service_capabilities,
    service_health,
    start_job_response,
    validate_rule_template_response,
)
from archive_scan_qc.rule_templates import RuleTemplateNotFoundError
from archive_scan_qc.service_jobs import (
    SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON,
    SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION,
    SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
    SERVICE_JOB_RECORD_JSON,
    ServiceJobNotFoundError,
)


class ServiceApiCoreTests(unittest.TestCase):
    def test_health_and_capabilities_are_public_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-health-") as temp_dir:
            root = Path(temp_dir) / "service-root"

            health = service_health(service_root=root)
            capabilities = service_capabilities()
            raw = json.dumps({"health": health, "capabilities": capabilities}, ensure_ascii=False)

            self.assertEqual(health["schema_version"], "scan-qc.service-api.v1")
            self.assertEqual(health["status"], "pass")
            self.assertTrue(health["service_root_configured"])
            self.assertFalse(health["job_index_available"])
            self.assertIn(
                ("POST", "/api/jobs"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/rule-templates"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/rule-templates/{template_id}"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/rule-templates"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("PUT", "/api/rule-templates/{template_id}"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/jobs/{job_id}/run"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/jobs/{job_id}/start"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/jobs/{job_id}/retry"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/jobs/{job_id}/local-review/{artifact_id}"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/jobs/{job_id}/local-preview/{local_id}"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/jobs/{job_id}/review-history"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/production/session"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/production/setup"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/production/start"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/production/progress"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/production/review-queue"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/production/review-history"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/production/preview"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/production/review-actions"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/production/finish-export"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertEqual(capabilities["schemas"]["rule_template_catalog"], "scan-qc.rule-template-catalog.v1")
            self.assertEqual(capabilities["schemas"]["rule_template_dry_run"], "scan-qc.rule-template-dry-run.v1")
            self.assertEqual(
                capabilities["schemas"]["rule_template_custom_validation"],
                "scan-qc.rule-template-custom-validation.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_rule_template_write"],
                "scan-qc.service-rule-template-write.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_job_local_review_artifact"],
                "scan-qc.service-job-local-review-artifact.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_job_local_preview"],
                "scan-qc.service-job-local-preview.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["production_session"],
                "scan-qc.service-production-session.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_job_review_actions"],
                "scan-qc.service-job-review-actions.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_job_review_history"],
                "scan-qc.service-job-review-history.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_job_event_log"],
                "scan-qc.service-job-event-log.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_job_index_quality"],
                "scan-qc.service-job-index-quality.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_job_index_recovery_issues"],
                "scan-qc.service-job-index-recovery-issues.v1",
            )
            self.assertEqual(
                capabilities["schemas"]["service_job_index_source_integrity"],
                "scan-qc.service-job-index-source-integrity.v1",
            )
            self.assertGreaterEqual(capabilities["resource_limits"]["max_active_async_jobs"], 1)
            self.assertGreaterEqual(capabilities["resource_limits"]["max_active_workers"], 1)
            self.assertGreaterEqual(capabilities["resource_limits"]["min_free_space_bytes"], 1)
            self.assertGreaterEqual(capabilities["resource_limits"]["max_tmp_bytes_per_job"], 1)
            self.assertTrue(capabilities["privacy"]["public_safe"])
            self.assertNotIn(str(root), raw)

    def test_rule_template_responses_are_public_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-templates-") as temp_dir:
            root = Path(temp_dir)

            catalog = list_rule_templates_response()
            detail = get_rule_template_response(template_id="text-clean-readable-v1")
            raw = json.dumps({"catalog": catalog, "detail": detail}, ensure_ascii=False)

            self.assertEqual(catalog["schema_version"], "scan-qc.rule-template-catalog.v1")
            self.assertEqual(detail["schema_version"], "scan-qc.rule-template-dry-run.v1")
            self.assertIn("text-clean-readable-v1", {template["id"] for template in catalog["templates"]})
            self.assertEqual(detail["template"]["id"], "text-clean-readable-v1")
            self.assertFalse(detail["derivative_images_written"])
            self.assertIn("text_clean_requires_pure_text_batch_confirmation", detail["risk_codes"])
            self.assertTrue(detail["privacy"]["public_safe"])
            self.assertFalse(detail["privacy"]["contains_paths"])
            _assert_public_text_omits(self, raw, str(root.resolve()))

    def test_rule_template_validation_accepts_inline_custom_draft_without_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-template-validate-") as temp_dir:
            root = Path(temp_dir)
            payload = validate_rule_template_response(
                {
                    "template": {
                        "name": "local-custom-template",
                        "min_dpi": 300,
                        "dpi_purpose": "print",
                        "name_pattern": str(root / "private-name-pattern"),
                        "quality_thresholds": {
                            "dark_mean_threshold": 40.0,
                            "despeckle_max_pixel_change_ratio": 0.005,
                        },
                        "rules": {
                            "dpi_missing": {"enabled": False, "severity": "P2"},
                            "quality_too_dark": {"enabled": True, "severity": "P1"},
                        },
                    }
                }
            )
            raw = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(payload["schema_version"], "scan-qc.rule-template-custom-validation.v1")
            self.assertEqual(payload["status"], "pass")
            self.assertTrue(payload["valid"])
            self.assertFalse(payload["derivative_images_written"])
            self.assertEqual(payload["template"]["id"], "custom")
            self.assertEqual(payload["validation"]["rule_count"], 2)
            self.assertEqual(payload["validation"]["disabled_rule_count"], 1)
            self.assertEqual(payload["validation"]["severity_override_count"], 2)
            self.assertIn("custom_template_requires_local_review_before_production", payload["risk_codes"])
            self.assertTrue(payload["privacy"]["public_safe"])
            self.assertFalse(payload["privacy"]["contains_paths"])
            self.assertFalse(payload["privacy"]["contains_row_level_evidence"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private-name-pattern")

    def test_rule_template_save_and_replace_are_service_managed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-template-save-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            draft = {
                "name": "saved-template",
                "min_dpi": 300,
                "dpi_purpose": "print",
                "name_pattern": str(root / "private-name-pattern"),
                "processing_defaults": {
                    "auto_crop": True,
                    "deskew": True,
                    "normalize_tones": True,
                    "reuse_scan_measurements": True,
                },
                "quality_thresholds": {"dark_mean_threshold": 38.0},
                "rules": {"quality_too_dark": {"enabled": True, "severity": "P1"}},
            }

            created = save_rule_template_response(
                {"template_id": "custom-readable001", "template": draft},
                service_root=service_root,
            )
            catalog = list_rule_templates_response(service_root=service_root)
            detail = get_rule_template_response(service_root=service_root, template_id="custom-readable001")
            updated = save_rule_template_response(
                {"template": {**draft, "processing_defaults": {"auto_crop": True}}},
                service_root=service_root,
                template_id="custom-readable001",
                replace_existing=True,
            )
            raw = json.dumps({"created": created, "catalog": catalog, "detail": detail, "updated": updated}, ensure_ascii=False)

            self.assertEqual(created["schema_version"], "scan-qc.service-rule-template-write.v1")
            self.assertEqual(created["action"], "created")
            self.assertEqual(created["template"]["id"], "custom-readable001")
            self.assertTrue(created["template"]["service_managed"])
            self.assertTrue(created["template"]["processing_defaults"]["normalize_tones"])
            self.assertEqual(detail["schema_version"], "scan-qc.service-rule-template-detail.v1")
            self.assertEqual(detail["template"]["id"], "custom-readable001")
            self.assertIn("normalize_tones", {item["operation"] for item in detail["planned_operations"]})
            self.assertIn("custom-readable001", {item["id"] for item in catalog["templates"]})
            self.assertEqual(updated["action"], "updated")
            self.assertFalse(updated["storage"]["path_returned"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private-name-pattern")

    def test_missing_service_rule_template_raises_not_found_without_job_dirs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-template-missing-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            with self.assertRaises(RuleTemplateNotFoundError):
                get_rule_template_response(service_root=service_root, template_id="custom-missing001")
            with self.assertRaises(RuleTemplateNotFoundError):
                create_job_response(
                    {
                        "input_dir": str(input_dir),
                        "service_root": str(service_root),
                        "rule_template": "custom-missing001",
                        "workers": 1,
                    },
                    job_id="job-missingtemplate001",
                )

            self.assertFalse((service_root / "jobs" / "job-missingtemplate001").exists())

    def test_job_create_status_cancel_and_index_responses_stay_public_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-job-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "输入目录"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "私有页面001.png")

            create_summary = create_job_response(
                {
                    "input_dir": str(input_dir),
                    "service_root": str(service_root),
                    "project_id": "project-zh",
                    "batch_id": "batch-zh",
                    "rule_template": "text-clean-print",
                    "workers": 1,
                },
                job_id="job-testapi001",
            )
            status_summary = get_job_response(service_root=service_root, job_id="job-testapi001")
            not_ready_finish_export = production_finish_export_response(
                service_root=service_root,
                job_id="job-testapi001",
            )
            cancel_summary = cancel_job_response(service_root=service_root, job_id="job-testapi001")
            cancelled_finish_export = production_finish_export_response(
                service_root=service_root,
                job_id="job-testapi001",
            )
            index_summary = recover_jobs_response(service_root=service_root)
            health = service_health(service_root=service_root)
            raw = json.dumps(
                {
                    "create": create_summary,
                    "status": status_summary,
                    "not_ready_finish_export": not_ready_finish_export,
                    "cancel": cancel_summary,
                    "cancelled_finish_export": cancelled_finish_export,
                    "index": index_summary,
                    "health": health,
                },
                ensure_ascii=False,
            )

            self.assertEqual(create_summary["state"], "created")
            self.assertEqual(create_summary["template"]["rule_template_id"], "text-clean-print")
            self.assertEqual(create_summary["template"]["processing_profile"], "standard")
            self.assertEqual(status_summary["state"], "created")
            self.assertEqual(status_summary["template"]["processing_profile"], "standard")
            self.assertFalse(not_ready_finish_export["finish_export"]["terminal"])
            self.assertFalse(not_ready_finish_export["finish_export"]["retryable"])
            self.assertFalse(not_ready_finish_export["finish_export"]["ready_for_export"])
            self.assertFalse(not_ready_finish_export["finish_export"]["requires_review"])
            self.assertEqual(not_ready_finish_export["finish_export"]["state"], "created")
            self.assertEqual(not_ready_finish_export["finish_export"]["blocking_codes"], ["job_not_terminal"])
            self.assertEqual(cancel_summary["state"], "cancelled")
            self.assertTrue(cancelled_finish_export["finish_export"]["terminal"])
            self.assertFalse(cancelled_finish_export["finish_export"]["retryable"])
            self.assertFalse(cancelled_finish_export["finish_export"]["ready_for_export"])
            self.assertFalse(cancelled_finish_export["finish_export"]["requires_review"])
            self.assertEqual(cancelled_finish_export["finish_export"]["state"], "cancelled")
            self.assertEqual(cancelled_finish_export["finish_export"]["blocking_codes"], ["job_cancelled"])
            self.assertEqual(index_summary["state_counts"], {"cancelled": 1})
            self.assertEqual(index_summary["skipped_job_count"], 0)
            self.assertEqual(index_summary["recovery_issues"]["status"], "clear")
            self.assertTrue((service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).is_file())
            self.assertTrue(health["job_index_available"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "输入目录", "私有页面001")

    def test_finish_export_marks_recovered_job_retryable_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-finish-recovery-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_job_response(
                {
                    "input_dir": str(input_dir),
                    "service_root": str(service_root),
                    "rule_template": "dat-31-2017-standard",
                    "workers": 1,
                },
                job_id="job-finishrecover001",
            )
            job_root = service_root / "jobs" / "job-finishrecover001"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["state"] = "finished"
            record["recovery"]["status"] = "forced_finished_without_summary"
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            finish_export = production_finish_export_response(
                service_root=service_root,
                job_id="job-finishrecover001",
            )
            raw = json.dumps(finish_export, ensure_ascii=False)

            self.assertEqual(finish_export["view"], "finish_export")
            self.assertEqual(finish_export["job"]["state"], "needs_recovery")
            self.assertEqual(
                finish_export["job"]["recovery"]["status"],
                "terminal_state_missing_production_summary",
            )
            self.assertFalse(finish_export["finish_export"]["terminal"])
            self.assertTrue(finish_export["finish_export"]["retryable"])
            self.assertFalse(finish_export["finish_export"]["ready_for_export"])
            self.assertFalse(finish_export["finish_export"]["requires_review"])
            self.assertEqual(finish_export["finish_export"]["state"], "needs_recovery")
            self.assertEqual(finish_export["finish_export"]["blocking_codes"], ["job_needs_recovery"])
            self.assertTrue(finish_export["privacy"]["public_safe"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001")

    def test_job_run_response_returns_quality_summary_without_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-run-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_job_response(
                {
                    "input_dir": str(input_dir),
                    "service_root": str(service_root),
                    "rule_template": "dat-31-2017-standard",
                    "workers": 1,
                },
                job_id="job-testapirun001",
            )

            summary = run_job_response(service_root=service_root, job_id="job-testapirun001")
            status = get_job_response(service_root=service_root, job_id="job-testapirun001")
            local_review = get_job_local_review_artifact_response(
                service_root=service_root,
                job_id="job-testapirun001",
                artifact_id="processing-review-package",
            )
            raw = json.dumps({"summary": summary, "status": status}, ensure_ascii=False)
            local_raw = json.dumps(local_review, ensure_ascii=False)

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(status["state"], "finished")
            self.assertEqual(local_review["schema_version"], "scan-qc.service-job-local-review-artifact.v1")
            self.assertEqual(local_review["artifact_id"], "processing-review-package")
            self.assertEqual(local_review["payload"]["schema_version"], "scan-qc.processing-review.v1")
            self.assertTrue(local_review["local_only"])
            self.assertTrue(local_review["sensitive"])
            self.assertFalse(local_review["public_safe"])
            self.assertTrue(local_review["privacy"]["contains_paths"])
            self.assertNotIn("artifact_path", local_raw)
            self.assertIn("private_page_001", local_raw)
            self.assertEqual(summary["counts"]["resumed_files"], 0)
            self.assertEqual(summary["counts"]["reused_files"], 0)
            self.assertEqual(summary["counts"]["retry_list_files"], 0)
            self.assertFalse(summary["retry"]["provided"])
            self.assertTrue(summary["quality"]["provided"])
            self.assertEqual(summary["quality"]["status"], "pass")
            self.assertEqual(summary["quality"]["processed_files"], 1)
            self.assertEqual(summary["quality"]["blocking_codes"], [])
            self.assertEqual(summary["quality"]["quality_signal_status"], "measured_no_quality_operations")
            self.assertIn("background_cleanup", summary["quality"]["quality_operations_applied"])
            self.assertTrue(summary["quality"]["guardrails"]["enabled"])
            self.assertEqual(summary["quality"]["guardrails"]["failed_files"], 0)
            _assert_public_timing_summary(self, summary["timings"], expected_processed_files=1)
            _assert_public_source_integrity(self, summary["source_integrity"], checked_files=1)
            self.assertFalse(summary["source_images_modified"])
            self.assertTrue(summary["local_review"]["provided"])
            self.assertTrue(summary["local_review"]["production_review_queue_written"])
            self.assertFalse(summary["local_review"]["privacy"]["contains_paths"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001")

            with self.assertRaisesRegex(ValueError, "Unsupported local review artifact"):
                get_job_local_review_artifact_response(
                    service_root=service_root,
                    job_id="job-testapirun001",
                    artifact_id="processing-review-package.json",
                )

    def test_job_run_response_handles_nested_unicode_space_paths_without_public_leak(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-unicode-path-") as temp_dir:
            root = Path(temp_dir)
            input_dir = (
                root
                / "中文 输入 目录"
                / "nested folder with spaces"
                / "long-but-ci-safe-segment-0001-0002-0003"
            )
            service_root = root / "服务 root with spaces"
            input_dir.mkdir(parents=True)
            source_path = input_dir / "私有 页面 001.png"
            _write_page(source_path)
            source_sha_before = _sha256_for_test(source_path)
            create_job_response(
                {
                    "input_dir": str(input_dir),
                    "service_root": str(service_root),
                    "rule_template": "dat-31-2017-standard",
                    "workers": 1,
                },
                job_id="job-testapiunicode001",
            )

            summary = run_job_response(service_root=service_root, job_id="job-testapiunicode001")
            status = get_job_response(service_root=service_root, job_id="job-testapiunicode001")
            session = production_session_response(service_root=service_root)
            raw = json.dumps({"summary": summary, "status": status, "session": session}, ensure_ascii=False)

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(status["state"], "finished")
            self.assertEqual(session["session"]["state_counts"], {"finished": 1})
            self.assertNotIn("jobs", session["session"])
            index_quality = session["session"]["quality"]
            self.assertEqual(index_quality["schema_version"], SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION)
            self.assertTrue(index_quality["provided"])
            self.assertEqual(index_quality["status_counts"], {"pass": 1})
            self.assertEqual(index_quality["provided_job_count"], 1)
            self.assertFalse(index_quality["privacy"]["contains_paths"])
            self.assertFalse(index_quality["privacy"]["contains_quality_rows"])
            index_source_integrity = session["session"]["source_integrity"]
            self.assertEqual(
                index_source_integrity["schema_version"],
                SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
            )
            self.assertTrue(index_source_integrity["provided"])
            self.assertEqual(index_source_integrity["status"], "pass")
            self.assertEqual(index_source_integrity["checked_files"], 1)
            self.assertEqual(index_source_integrity["unchanged_files"], 1)
            self.assertFalse(index_source_integrity["source_images_modified"])
            self.assertFalse(index_source_integrity["privacy"]["contains_paths"])
            self.assertFalse(index_source_integrity["privacy"]["contains_hashes"])
            self.assertFalse(index_source_integrity["privacy"]["contains_file_lists"])
            self.assertEqual(_sha256_for_test(source_path), source_sha_before)
            self.assertFalse(summary["source_images_modified"])
            _assert_public_source_integrity(self, summary["source_integrity"], checked_files=1)
            self.assertTrue(summary["quality"]["provided"])
            self.assertFalse(summary["quality"]["blocking_codes"])
            _assert_public_text_omits(
                self,
                raw,
                str(root.resolve()),
                "中文 输入 目录",
                "nested folder with spaces",
                "long-but-ci-safe-segment",
                "服务 root with spaces",
                "私有 页面 001",
            )

    def test_job_run_response_exposes_print_clean_quality_evidence_without_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-print-clean-quality-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private input"
            service_root = root / "service root"
            input_dir.mkdir()
            source_path = input_dir / "private_page_001.png"
            _write_print_clean_background_stain_page(source_path)
            source_sha_before = _sha256_for_test(source_path)
            create_job_response(
                {
                    "input_dir": str(input_dir),
                    "service_root": str(service_root),
                    "rule_template": "print-clean-v1",
                    "workers": 1,
                },
                job_id="job-printcleanquality001",
            )

            summary = run_job_response(service_root=service_root, job_id="job-printcleanquality001")
            status = get_job_response(service_root=service_root, job_id="job-printcleanquality001")
            session = production_session_response(service_root=service_root)
            raw = json.dumps({"summary": summary, "status": status, "session": session}, ensure_ascii=False)

            quality_metrics = summary["quality"]["quality_metrics"]
            session_quality = session["session"]["quality"]

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(status["state"], "finished")
            self.assertEqual(summary["template"]["rule_template_id"], "print-clean-v1")
            self.assertEqual(summary["template"]["processing_profile"], "print_clean")
            self.assertEqual(status["template"]["processing_profile"], "print_clean")
            self.assertTrue(summary["quality"]["provided"])
            self.assertEqual(summary["quality"]["status"], "pass")
            self.assertEqual(summary["quality"]["quality_signal_status"], "measured_with_changes")
            self.assertEqual(summary["quality"]["processed_files"], 1)
            self.assertEqual(summary["quality"]["background_cleanup_changed_files"], 1)
            self.assertEqual(summary["quality"]["guardrail_failed_files"], 0)
            self.assertGreaterEqual(quality_metrics["background_stains_delta"]["max"], 6.0)
            self.assertGreater(quality_metrics["background_stains_changed_pixel_ratio"]["max"], 0.02)
            self.assertLessEqual(quality_metrics["background_stains_changed_pixel_ratio"]["max"], 0.05)
            self.assertEqual(session_quality["status_counts"], {"pass": 1})
            self.assertEqual(session_quality["quality_signal_status_counts"], {"measured_with_changes": 1})
            self.assertNotIn("quality_metrics", session_quality)
            self.assertFalse(session_quality["privacy"]["contains_paths"])
            self.assertFalse(session_quality["privacy"]["contains_job_ids"])
            self.assertFalse(session_quality["privacy"]["contains_quality_rows"])
            self.assertEqual(_sha256_for_test(source_path), source_sha_before)
            self.assertFalse(summary["source_images_modified"])
            _assert_public_source_integrity(self, summary["source_integrity"], checked_files=1)
            _assert_public_text_omits(
                self,
                raw,
                str(root.resolve()),
                "private input",
                "service root",
                "private_page_001",
            )

    def test_job_start_response_returns_running_then_terminal_without_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-start-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_job_response(
                {
                    "input_dir": str(input_dir),
                    "service_root": str(service_root),
                    "rule_template": "dat-31-2017-standard",
                    "workers": 1,
                },
                job_id="job-testapistart001",
            )

            running = start_job_response(service_root=service_root, job_id="job-testapistart001")
            terminal = _wait_for_terminal_summary(
                self,
                lambda: get_job_response(service_root=service_root, job_id="job-testapistart001"),
            )
            raw = json.dumps({"running": running, "terminal": terminal}, ensure_ascii=False)

            self.assertEqual(running["state"], "running")
            self.assertEqual(running["recovery"]["status"], "async_running")
            self.assertEqual(terminal["state"], "finished")
            self.assertTrue(terminal["quality"]["provided"])
            self.assertEqual(terminal["quality"]["processed_files"], 1)
            self.assertIn("text_enhancement", terminal["quality"]["quality_operations_applied"])
            self.assertEqual(terminal["quality"]["guardrails"]["failure_reasons"], {})
            _assert_public_timing_summary(self, terminal["timings"], expected_processed_files=1)
            self.assertTrue(terminal["local_review"]["processing_review_package_written"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001")

    def test_production_facade_uses_public_safe_job_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-production-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            initial_session = production_session_response(service_root=service_root)
            setup = production_setup_response(
                {
                    "input_dir": str(input_dir),
                    "service_root": str(service_root),
                    "rule_template": "dat-31-2017-standard",
                    "workers": 1,
                },
                job_id="job-productionapi001",
            )
            running = production_start_response(service_root=service_root, job_id="job-productionapi001")
            terminal = _wait_for_terminal_summary(
                self,
                lambda: production_progress_response(service_root=service_root, job_id="job-productionapi001")["job"],
            )
            review_queue = production_review_queue_response(service_root=service_root, job_id="job-productionapi001")
            _ensure_preview_queue_item(service_root, "job-productionapi001", "private_page_001.png")
            preview = get_job_local_preview_response(
                service_root=service_root,
                job_id="job-productionapi001",
                local_id="PRQ000001",
                source="original",
            )
            review_actions = production_review_actions_response(
                {
                    "job_id": "job-productionapi001",
                    "review_decisions": _review_decision_summary(("accepted_issue",)),
                },
                service_root=service_root,
            )
            job_review_history = get_job_review_history_response(
                service_root=service_root,
                job_id="job-productionapi001",
            )
            production_review_history = production_review_history_response(
                service_root=service_root,
                job_id="job-productionapi001",
            )
            progress_after_actions = production_progress_response(
                service_root=service_root,
                job_id="job-productionapi001",
            )
            finish_export = production_finish_export_response(service_root=service_root, job_id="job-productionapi001")
            final_session = production_session_response(service_root=service_root)
            raw = json.dumps(
                {
                    "initial_session": initial_session,
                    "setup": setup,
                    "running": running,
                    "terminal": terminal,
                    "review_queue": review_queue,
                    "review_actions": review_actions,
                    "job_review_history": job_review_history,
                    "production_review_history": production_review_history,
                    "progress_after_actions": progress_after_actions,
                    "finish_export": finish_export,
                    "final_session": final_session,
                },
                ensure_ascii=False,
            )

            self.assertEqual(initial_session["schema_version"], "scan-qc.service-production-session.v1")
            self.assertEqual(initial_session["view"], "session")
            self.assertEqual(initial_session["session"]["quality"]["status"], "not_available")
            self.assertEqual(initial_session["session"]["source_integrity"]["status"], "not_available")
            self.assertEqual(setup["view"], "setup")
            self.assertEqual(setup["job"]["state"], "created")
            self.assertEqual(running["view"], "start")
            self.assertEqual(running["job"]["state"], "running")
            self.assertEqual(terminal["state"], "finished")
            self.assertEqual(review_queue["view"], "review_queue")
            self.assertTrue(review_queue["review_queue"]["available"])
            self.assertEqual(review_queue["review_queue"]["local_review_artifact_id"], "production-review-queue")
            self.assertEqual(preview["schema_version"], "scan-qc.service-job-local-preview.v1")
            self.assertEqual(preview["source"], "original")
            self.assertTrue(preview["local_only"])
            self.assertFalse(preview["public_safe"])
            self.assertTrue(Path(preview["path"]).is_file())
            self.assertEqual(review_actions["view"], "review_actions")
            self.assertTrue(review_actions["review_actions"]["saved"])
            self.assertEqual(review_actions["review_actions"]["verification"]["status"], "pass")
            self.assertEqual(review_actions["review_actions"]["decision_summary"]["total_decisions"], 1)
            self.assertTrue(review_actions["review_actions"]["review_history_written"])
            self.assertEqual(review_actions["review_actions"]["history"]["entry_count"], 1)
            self.assertEqual(review_actions["review_actions"]["history"]["latest_verification_status"], "pass")
            self.assertEqual(job_review_history["schema_version"], "scan-qc.service-job-review-history.v1")
            self.assertTrue(job_review_history["provided"])
            self.assertEqual(job_review_history["status"], "available")
            self.assertEqual(job_review_history["review_history"]["entry_count"], 1)
            self.assertEqual(job_review_history["review_history"]["latest_verification_status"], "pass")
            self.assertFalse(job_review_history["storage"]["path_returned"])
            self.assertFalse(job_review_history["review_history"]["privacy"]["contains_review_rows"])
            self.assertFalse(job_review_history["review_history"]["privacy"]["contains_local_ids"])
            self.assertEqual(production_review_history["view"], "review_history")
            self.assertEqual(production_review_history["review_history"]["review_history"]["entry_count"], 1)
            self.assertTrue(production_review_history["privacy"]["public_safe"])
            self.assertEqual(progress_after_actions["job"]["review_actions"]["history"]["entry_count"], 1)
            self.assertNotIn("PRQ000001", raw)
            self.assertTrue(
                (service_root / "jobs" / "job-productionapi001" / "review" / "scan-qc-review-decisions.summary.json").is_file()
            )
            self.assertTrue(
                (
                    service_root
                    / "jobs"
                    / "job-productionapi001"
                    / "review"
                    / "review_decision_verification_summary.json"
                ).is_file()
            )
            review_history_path = service_root / "jobs" / "job-productionapi001" / "review" / "service_job_review_history.json"
            self.assertTrue(review_history_path.is_file())
            review_history = json.loads(review_history_path.read_text(encoding="utf-8"))
            self.assertEqual(review_history["schema_version"], "scan-qc.service-job-review-history.v1")
            self.assertEqual(review_history["entry_count"], 1)
            self.assertIn("PRQ000001", json.dumps(review_history, ensure_ascii=False))
            self.assertEqual(finish_export["view"], "finish_export")
            self.assertTrue(finish_export["finish_export"]["terminal"])
            self.assertTrue(finish_export["finish_export"]["ready_for_export"])
            self.assertEqual(final_session["session"]["job_count"], 1)
            self.assertEqual(final_session["session"]["state_counts"], {"finished": 1})
            self.assertEqual(final_session["session"]["recovery_issues"]["status"], "clear")
            self.assertNotIn("jobs", final_session["session"])
            final_quality = final_session["session"]["quality"]
            self.assertEqual(final_quality["schema_version"], SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION)
            self.assertTrue(final_quality["provided"])
            self.assertEqual(final_quality["provided_job_count"], 1)
            self.assertEqual(final_quality["processed_files"], 1)
            self.assertEqual(final_quality["status_counts"], {"pass": 1})
            self.assertFalse(final_quality["privacy"]["contains_paths"])
            self.assertFalse(final_quality["privacy"]["contains_job_ids"])
            self.assertFalse(final_quality["privacy"]["contains_quality_rows"])
            final_source_integrity = final_session["session"]["source_integrity"]
            self.assertEqual(
                final_source_integrity["schema_version"],
                SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
            )
            self.assertTrue(final_source_integrity["provided"])
            self.assertEqual(final_source_integrity["status"], "pass")
            self.assertEqual(final_source_integrity["checked_files"], 1)
            self.assertEqual(final_source_integrity["unchanged_files"], 1)
            self.assertEqual(final_source_integrity["modified_files"], 0)
            self.assertFalse(final_source_integrity["source_images_modified"])
            self.assertFalse(final_source_integrity["source_tree_changed"])
            self.assertFalse(final_source_integrity["privacy"]["contains_paths"])
            self.assertFalse(final_source_integrity["privacy"]["contains_hashes"])
            self.assertFalse(final_source_integrity["privacy"]["contains_file_lists"])
            self.assertTrue(finish_export["privacy"]["public_safe"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001")

    def test_create_job_response_requires_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "input_dir"):
            create_job_response({"service_root": "service-root"})

    def test_missing_job_response_raises_explicit_not_found(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-api-missing-job-") as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ServiceJobNotFoundError):
                get_job_response(service_root=root / "service-root", job_id="job-missing001")


def _write_page(path: Path) -> None:
    image = Image.new("RGB", (220, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((32, 42, 188, 46), fill=(40, 40, 40))
    image.save(path, dpi=(300, 300))


def _write_print_clean_background_stain_page(path: Path) -> None:
    image = Image.new("RGB", (260, 190), (242, 242, 236))
    draw = ImageDraw.Draw(image)
    for y in (54, 82, 110):
        draw.rectangle((42, y, 142, y + 4), fill=(42, 42, 42))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((178, 58, 222, 102), fill=150)
    mask = mask.filter(ImageFilter.GaussianBlur(7))
    image = Image.composite(Image.new("RGB", image.size, (224, 220, 196)), image, mask)
    image.save(path, dpi=(300, 300))


def _sha256_for_test(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _review_decision_summary(decisions: tuple[str, ...]) -> dict[str, object]:
    counts = {
        "pending": 0,
        "accepted_issue": 0,
        "false_positive": 0,
        "fixed_externally": 0,
        "needs_rescan": 0,
        "blocked": 0,
    }
    rows = []
    for index, decision in enumerate(decisions, start=1):
        counts[decision] += 1
        rows.append({"scope": "production_review_queue", "local_id": f"PRQ{index:06d}", "decision": decision})
    pending = counts["pending"]
    reviewed = len(rows) - pending
    return {
        "schema": "scan-qc-review-decisions.local.v1",
        "source_type": "production_review_queue",
        "source_target_count": len(rows),
        "generated_in_browser": False,
        "privacy": {"local_only": True},
        "review_counts": counts,
        "aggregate_counts": {
            "p0_pending": 0 if pending == 0 else pending,
            "p1_pending": 0,
            "review_completion": {
                "total": len(rows),
                "pending": pending,
                "reviewed": reviewed,
                "complete": pending == 0,
            },
        },
        "reviewed_targets": reviewed,
        "decisions": rows,
    }


def _ensure_preview_queue_item(service_root: Path, job_id: str, relative_path: str) -> None:
    queue_path = service_root / "jobs" / job_id / "review" / "production_review_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    items = queue.get("items")
    if not isinstance(items, list):
        items = []
        queue["items"] = items
    if not any(isinstance(item, dict) and item.get("local_id") == "PRQ000001" for item in items):
        items.insert(
            0,
            {
                "local_id": "PRQ000001",
                "relative_path": relative_path,
                "severity": "P2",
                "source_category": "processing_failure",
                "suggested_action": "reprocess",
            },
        )
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _wait_for_terminal_summary(testcase: unittest.TestCase, read_summary) -> dict:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 10
    last_summary = None
    while time.monotonic() < deadline:
        last_summary = read_summary()
        if last_summary.get("state") in {"finished", "needs_review", "failed", "interrupted", "cancelled"}:
            return last_summary
        time.sleep(0.05)
    testcase.fail(f"service job did not reach a terminal state: {last_summary}")


def _assert_public_text_omits(testcase: unittest.TestCase, raw: str, *private_values: str) -> None:
    normalized = raw.replace("\\\\", "\\")
    for value in private_values:
        testcase.assertNotIn(value, raw)
        testcase.assertNotIn(value, normalized)


def _assert_public_timing_summary(
    testcase: unittest.TestCase,
    timings: dict,
    *,
    expected_processed_files: int,
) -> None:
    testcase.assertEqual(timings["schema_version"], "scan-qc.service-job-public-timings.v1")
    testcase.assertTrue(timings["provided"])
    testcase.assertEqual(timings["aggregate_processing"]["processed_images"], expected_processed_files)
    testcase.assertIn("scan", {stage["id"] for stage in timings["stage_timings"]["stages"]})
    testcase.assertIn("auto_crop", timings["operation_timings"])
    testcase.assertEqual(timings["operation_count"], len(timings["operation_timings"]))
    testcase.assertFalse(timings["privacy"]["contains_paths"])


def _assert_public_source_integrity(
    testcase: unittest.TestCase,
    source_integrity: dict,
    *,
    checked_files: int,
) -> None:
    testcase.assertEqual(source_integrity["schema_version"], "scan-qc.service-job-source-integrity.v1")
    testcase.assertTrue(source_integrity["provided"])
    testcase.assertEqual(source_integrity["checked_files"], checked_files)
    testcase.assertEqual(source_integrity["modified_files"], 0)
    testcase.assertFalse(source_integrity["privacy"]["contains_paths"])
    testcase.assertFalse(source_integrity["privacy"]["contains_hashes"])


if __name__ == "__main__":
    unittest.main()
