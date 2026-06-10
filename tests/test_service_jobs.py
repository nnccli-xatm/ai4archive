from __future__ import annotations

import hashlib
import json
import time
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from archive_scan_qc import service_jobs as service_jobs_module
from archive_scan_qc.service_jobs import (
    SERVICE_JOB_MAX_WORKERS,
    SERVICE_JOB_MAX_ACTIVE_WORKERS,
    SERVICE_JOB_MAX_TMP_BYTES,
    SERVICE_JOB_MIN_FREE_SPACE_BYTES,
    SERVICE_JOB_EVENT_LOG_JSON,
    SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION,
    SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON,
    SERVICE_JOB_INDEX_RECOVERY_ISSUES_SCHEMA_VERSION,
    SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
    SERVICE_JOB_PUBLIC_SUMMARY_JSON,
    SERVICE_JOB_RECORD_JSON,
    ServiceJobConfig,
    InvalidServiceJobIdError,
    cancel_service_job,
    create_service_job,
    recover_service_job,
    recover_service_jobs,
    retry_service_job,
    run_service_job,
    start_service_job_async,
)
from archive_scan_qc.processing_quality_summary import PROCESSING_QUALITY_SUMMARY_JSON
from archive_scan_qc.rule_templates import save_service_rule_template


class ServiceJobBoundaryTests(unittest.TestCase):
    def test_create_job_writes_isolated_private_record_and_public_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-boundary-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            summary = create_service_job(
                ServiceJobConfig(
                    input_dir=input_dir,
                    service_root=service_root,
                    rule_template="dat-31-2017-standard",
                    workers=1,
                ),
                job_id="job-testboundary001",
            )
            job_root = service_root / "jobs" / "job-testboundary001"
            record = json.loads((job_root / SERVICE_JOB_RECORD_JSON).read_text(encoding="utf-8"))
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["job_id"], "job-testboundary001")
            self.assertEqual(summary["state"], "created")
            self.assertTrue(summary["public_safe"])
            self.assertTrue(summary["isolation"]["metadata_isolated"])
            self.assertTrue(summary["isolation"]["derivatives_isolated"])
            self.assertTrue(summary["isolation"]["tmp_isolated"])
            self.assertTrue(summary["isolation"]["review_isolated"])
            self.assertTrue(summary["isolation"]["log_isolated"])
            self.assertEqual(summary["events"]["schema_version"], "scan-qc.service-job-event-log.v1")
            self.assertTrue(summary["events"]["provided"])
            self.assertEqual(summary["events"]["event_count"], 1)
            self.assertEqual(summary["events"]["latest_event_type"], "job_created")
            self.assertEqual(summary["events"]["latest_state"], "created")
            self.assertFalse(summary["events"]["path_returned"])
            self.assertFalse(summary["events"]["privacy"]["contains_event_rows"])
            self.assertEqual(summary["resource_limits"]["max_workers_per_job"], SERVICE_JOB_MAX_WORKERS)
            self.assertEqual(summary["resource_limits"]["max_active_workers"], SERVICE_JOB_MAX_ACTIVE_WORKERS)
            self.assertEqual(summary["resource_limits"]["min_free_space_bytes"], SERVICE_JOB_MIN_FREE_SPACE_BYTES)
            self.assertEqual(summary["resource_limits"]["max_tmp_bytes_per_job"], SERVICE_JOB_MAX_TMP_BYTES)
            self.assertEqual(summary["resource_limits"]["workers_requested"], 1)
            self.assertEqual(Path(record["paths"]["metadata_dir"]).parent, job_root)
            self.assertEqual(Path(record["paths"]["derivatives_dir"]).parent, job_root)
            self.assertEqual(Path(record["paths"]["tmp_dir"]).parent, job_root)
            self.assertEqual(Path(record["paths"]["review_dir"]).parent, job_root)
            self.assertTrue((job_root / "review").is_dir())
            self.assertTrue((job_root / "logs" / SERVICE_JOB_EVENT_LOG_JSON).is_file())
            event_log = json.loads((job_root / "logs" / SERVICE_JOB_EVENT_LOG_JSON).read_text(encoding="utf-8"))
            self.assertTrue(event_log["local_only"])
            self.assertFalse(event_log["public_safe"])
            self.assertEqual(event_log["event_count"], 1)
            self.assertEqual(event_log["events"][0]["event_type"], "job_created")
            self.assertEqual(record["resource_limits"]["max_workers_per_job"], SERVICE_JOB_MAX_WORKERS)
            self.assertEqual(record["resource_limits"]["max_active_workers"], SERVICE_JOB_MAX_ACTIVE_WORKERS)
            self.assertEqual(record["resource_limits"]["min_free_space_bytes"], SERVICE_JOB_MIN_FREE_SPACE_BYTES)
            self.assertEqual(record["resource_limits"]["max_tmp_bytes_per_job"], SERVICE_JOB_MAX_TMP_BYTES)
            self.assertEqual(record["resource_limits"]["workers_requested"], 1)
            self.assertEqual(record["paths"]["input_dir"], str(input_dir.resolve()))
            _assert_public_text_omits(
                self,
                public_raw,
                str(root.resolve()),
                "private_page_001",
                "sha256",
                SERVICE_JOB_RECORD_JSON,
                SERVICE_JOB_EVENT_LOG_JSON,
            )

    def test_create_rejects_overlapping_service_root_and_input_dir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-overlap-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            service_root = input_dir / "service-root"
            input_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "must not overlap"):
                create_service_job(ServiceJobConfig(input_dir=input_dir, service_root=service_root))

    def test_create_rejects_invalid_job_id_without_job_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-invalid-id-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            with self.assertRaisesRegex(InvalidServiceJobIdError, "Invalid service job id"):
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                    job_id="../job-escape001",
                )

            jobs_dir = service_root / "jobs"
            self.assertTrue(jobs_dir.is_dir())
            self.assertEqual(list(jobs_dir.iterdir()), [])

    def test_create_rejects_invalid_worker_limits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-workers-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            with self.assertRaisesRegex(ValueError, "positive integer"):
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=root / "service-root-low", workers=0),
                    job_id="job-testworkers001",
                )
            with self.assertRaisesRegex(ValueError, "per-job limit"):
                create_service_job(
                    ServiceJobConfig(
                        input_dir=input_dir,
                        service_root=root / "service-root-high",
                        workers=SERVICE_JOB_MAX_WORKERS + 1,
                    ),
                    job_id="job-testworkers002",
                )

    def test_create_rejects_service_root_below_free_space_minimum(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-disk-quota-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            with (
                mock.patch("archive_scan_qc.service_jobs.SERVICE_JOB_MIN_FREE_SPACE_BYTES", 10**30),
                self.assertRaisesRegex(RuntimeError, "free space"),
            ):
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                    job_id="job-testdiskquota001",
                )

            self.assertFalse((service_root / "jobs" / "job-testdiskquota001").exists())

    def test_run_job_rejects_tmp_quota_before_marking_running(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-tmp-quota-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            with mock.patch("archive_scan_qc.service_jobs.SERVICE_JOB_MAX_TMP_BYTES", 4):
                created = create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                    job_id="job-testtmpquota001",
                )
            job_root = service_root / "jobs" / "job-testtmpquota001"
            (job_root / "tmp" / "overflow.bin").write_bytes(b"overflow")

            with self.assertRaisesRegex(RuntimeError, "temporary directory quota"):
                run_service_job(service_root, "job-testtmpquota001")
            status = recover_service_job(service_root, "job-testtmpquota001")

            self.assertEqual(created["resource_limits"]["max_tmp_bytes_per_job"], 4)
            self.assertEqual(status["state"], "created")

    def test_service_job_runs_service_managed_custom_template_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-custom-template-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            save_service_rule_template(
                service_root=service_root,
                template_id="custom-readable001",
                template_draft={
                    "name": "saved-custom-template",
                    "min_dpi": 300,
                    "dpi_purpose": "print",
                    "processing_defaults": {
                        "auto_crop": True,
                        "deskew": True,
                        "normalize_tones": True,
                        "reuse_scan_measurements": True,
                    },
                    "quality_thresholds": {"dark_mean_threshold": 38.0},
                    "rules": {"quality_too_dark": {"enabled": True, "severity": "P1"}},
                },
            )

            created = create_service_job(
                ServiceJobConfig(
                    input_dir=input_dir,
                    service_root=service_root,
                    rule_template="custom-readable001",
                    workers=1,
                ),
                job_id="job-testcustomtemplate001",
            )
            summary = run_service_job(service_root, "job-testcustomtemplate001")
            job_root = service_root / "jobs" / "job-testcustomtemplate001"
            record = json.loads((job_root / SERVICE_JOB_RECORD_JSON).read_text(encoding="utf-8"))
            processing_manifest = json.loads(
                (job_root / "derivatives" / "processing_manifest.json").read_text(encoding="utf-8")
            )
            raw = json.dumps({"created": created, "summary": summary}, ensure_ascii=False)

            self.assertEqual(created["template"]["rule_template_id"], "custom-readable001")
            self.assertEqual(created["template"]["base_rule_template_id"], "custom")
            self.assertEqual(created["template"]["processing_profile"], "standard")
            self.assertEqual(summary["template"]["rule_template_id"], "custom-readable001")
            self.assertEqual(summary["template"]["base_rule_template_id"], "custom")
            self.assertEqual(summary["template"]["processing_profile"], "standard")
            self.assertEqual(record["template_snapshot"]["service_template_id"], "custom-readable001")
            self.assertEqual(record["template_snapshot"]["processing_profile"], "standard")
            self.assertTrue(record["template_snapshot"]["processing_defaults"]["normalize_tones"])
            self.assertIsInstance(record["template_snapshot"]["custom_template_draft"], dict)
            self.assertEqual(processing_manifest["rule_template"]["id"], "custom")
            self.assertEqual(summary["state"], "finished")
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001")

    def test_run_job_writes_terminal_public_summary_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-run-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            source_path = input_dir / "private_page_001.png"
            _write_page(source_path)
            source_sha_before = _sha256_for_test(source_path)
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testrun001",
            )

            summary = run_service_job(service_root, "job-testrun001")
            job_root = service_root / "jobs" / "job-testrun001"
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")
            processing_manifest = json.loads(
                (job_root / "derivatives" / "processing_manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(summary["recovery"]["status"], "terminal_summary_recovered")
            self.assertTrue(summary["events"]["provided"])
            self.assertGreaterEqual(summary["events"]["event_count"], 3)
            self.assertEqual(summary["events"]["latest_event_type"], "state_changed")
            self.assertEqual(summary["events"]["latest_state"], "finished")
            self.assertFalse(summary["events"]["privacy"]["contains_event_rows"])
            self.assertEqual(summary["counts"]["total_files"], 1)
            self.assertEqual(summary["counts"]["processed_files"], 1)
            self.assertEqual(summary["counts"]["resumed_files"], 0)
            self.assertEqual(summary["counts"]["reused_files"], 0)
            self.assertEqual(summary["counts"]["reprocessed_files"], 0)
            self.assertEqual(summary["counts"]["retry_list_files"], 0)
            self.assertFalse(summary["retry"]["provided"])
            self.assertEqual(summary["retry"]["status"], "not_started")
            self.assertTrue(summary["quality"]["provided"])
            self.assertEqual(summary["quality"]["status"], "pass")
            self.assertEqual(summary["quality"]["processed_files"], 1)
            _assert_public_quality_summary(self, summary["quality"])
            _assert_public_timing_summary(self, summary["timings"], expected_processed_files=1)
            _assert_public_source_integrity(self, summary["source_integrity"], checked_files=1)
            self.assertFalse(summary["source_images_modified"])
            self.assertEqual(_sha256_for_test(source_path), source_sha_before)
            self.assertEqual(processing_manifest["rule_template"]["id"], "dat-31-2017-standard")
            self.assertEqual(
                processing_manifest["rule_template"]["processing_defaults"]["reuse_scan_measurements"],
                True,
            )
            self.assertTrue((job_root / "metadata" / "production_run_summary.json").is_file())
            self.assertTrue((job_root / "derivatives" / "processing_manifest.json").is_file())
            self.assertTrue((job_root / "derivatives" / PROCESSING_QUALITY_SUMMARY_JSON).is_file())
            self.assertTrue((job_root / "review" / "processing_review_package.json").is_file())
            self.assertTrue((job_root / "review" / "processing_review_package.html").is_file())
            self.assertTrue((job_root / "review" / "production_review_queue.json").is_file())
            self.assertTrue((job_root / "logs" / SERVICE_JOB_EVENT_LOG_JSON).is_file())
            event_log = json.loads((job_root / "logs" / SERVICE_JOB_EVENT_LOG_JSON).read_text(encoding="utf-8"))
            self.assertGreaterEqual(event_log["event_count"], 3)
            self.assertTrue(event_log["privacy"]["contains_event_rows"])
            index_summary = recover_service_jobs(service_root)
            index_quality = index_summary["quality"]
            self.assertEqual(index_quality["schema_version"], SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION)
            self.assertTrue(index_quality["provided"])
            self.assertEqual(index_quality["job_count"], 1)
            self.assertEqual(index_quality["provided_job_count"], 1)
            self.assertEqual(index_quality["not_provided_job_count"], 0)
            self.assertEqual(index_quality["processed_files"], 1)
            self.assertEqual(index_quality["failed_files"], 0)
            self.assertEqual(index_quality["guardrail_failed_files"], 0)
            self.assertEqual(index_quality["jobs_with_blocking_codes"], 0)
            self.assertEqual(index_quality["status_counts"], {"pass": 1})
            self.assertEqual(index_quality["quality_signal_status_counts"], {summary["quality"]["quality_signal_status"]: 1})
            self.assertEqual(index_quality["blocking_code_counts"], {})
            self.assertFalse(index_quality["privacy"]["contains_paths"])
            self.assertFalse(index_quality["privacy"]["contains_quality_rows"])
            index_source_integrity = index_summary["source_integrity"]
            self.assertEqual(
                index_source_integrity["schema_version"],
                SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
            )
            self.assertTrue(index_source_integrity["provided"])
            self.assertEqual(index_source_integrity["status"], "pass")
            self.assertEqual(index_source_integrity["job_count"], 1)
            self.assertEqual(index_source_integrity["provided_job_count"], 1)
            self.assertEqual(index_source_integrity["checked_files"], 1)
            self.assertEqual(index_source_integrity["unchanged_files"], 1)
            self.assertEqual(index_source_integrity["modified_files"], 0)
            self.assertEqual(index_source_integrity["missing_files"], 0)
            self.assertEqual(index_source_integrity["added_files"], 0)
            self.assertFalse(index_source_integrity["source_images_modified"])
            self.assertFalse(index_source_integrity["source_tree_changed"])
            self.assertFalse(index_source_integrity["privacy"]["contains_paths"])
            self.assertFalse(index_source_integrity["privacy"]["contains_hashes"])
            self.assertFalse(index_source_integrity["privacy"]["contains_file_lists"])
            review_package = json.loads(
                (job_root / "review" / "processing_review_package.json").read_text(encoding="utf-8")
            )
            review_queue = json.loads(
                (job_root / "review" / "production_review_queue.json").read_text(encoding="utf-8")
            )
            self.assertTrue(review_package["privacy"]["local_only"])
            self.assertTrue(review_queue["privacy"]["local_only"])
            self.assertTrue(summary["local_review"]["provided"])
            self.assertTrue(summary["local_review"]["processing_review_package_written"])
            self.assertTrue(summary["local_review"]["production_review_queue_written"])
            self.assertIsInstance(summary["local_review"]["review_item_count"], int)
            self.assertIn("readability_improvement", summary["local_review"]["processing_review_group_counts"])
            self.assertIsInstance(
                summary["local_review"]["processing_review_group_counts"]["readability_improvement"],
                int,
            )
            self.assertFalse(summary["local_review"]["privacy"]["contains_paths"])
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")
            self.assertFalse(summary["private_paths_exposed"])

    def test_recover_index_preserves_print_clean_quality_evidence_without_quality_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-print-clean-index-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private input"
            service_root = root / "service root"
            input_dir.mkdir()
            source_path = input_dir / "private_page_001.png"
            _write_print_clean_background_stain_page(source_path)
            source_sha_before = _sha256_for_test(source_path)
            create_service_job(
                ServiceJobConfig(
                    input_dir=input_dir,
                    service_root=service_root,
                    rule_template="print-clean-v1",
                    workers=1,
                ),
                job_id="job-printcleanindex001",
            )

            summary = run_service_job(service_root, "job-printcleanindex001")
            recovered = recover_service_job(service_root, "job-printcleanindex001")
            index_summary = recover_service_jobs(service_root)
            job_root = service_root / "jobs" / "job-printcleanindex001"
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")
            index_raw = (service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            quality_metrics = recovered["quality"]["quality_metrics"]
            index_quality = index_summary["quality"]

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(recovered["state"], "finished")
            self.assertEqual(recovered["template"]["rule_template_id"], "print-clean-v1")
            self.assertEqual(recovered["template"]["processing_profile"], "print_clean")
            self.assertEqual(recovered["quality"]["status"], "pass")
            self.assertEqual(recovered["quality"]["quality_signal_status"], "measured_with_changes")
            self.assertEqual(recovered["quality"]["background_cleanup_changed_files"], 1)
            self.assertEqual(recovered["quality"]["guardrail_failed_files"], 0)
            self.assertGreaterEqual(quality_metrics["background_stains_delta"]["max"], 6.0)
            self.assertGreater(quality_metrics["background_stains_changed_pixel_ratio"]["max"], 0.02)
            self.assertLessEqual(quality_metrics["background_stains_changed_pixel_ratio"]["max"], 0.05)
            self.assertEqual(index_quality["schema_version"], SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION)
            self.assertTrue(index_quality["provided"])
            self.assertEqual(index_quality["status_counts"], {"pass": 1})
            self.assertEqual(index_quality["quality_signal_status_counts"], {"measured_with_changes": 1})
            self.assertEqual(index_quality["processed_files"], 1)
            self.assertEqual(index_quality["guardrail_failed_files"], 0)
            self.assertNotIn("quality_metrics", index_quality)
            self.assertFalse(index_quality["privacy"]["contains_paths"])
            self.assertFalse(index_quality["privacy"]["contains_job_ids"])
            self.assertFalse(index_quality["privacy"]["contains_quality_rows"])
            self.assertEqual(_sha256_for_test(source_path), source_sha_before)
            _assert_public_source_integrity(self, recovered["source_integrity"], checked_files=1)
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private input", "private_page_001")
            _assert_public_text_omits(
                self,
                index_raw,
                str(root.resolve()),
                "private input",
                "service root",
                "private_page_001",
            )

    def test_retry_job_reuses_existing_derivative_after_failed_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-retry-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            source_path = input_dir / "private_page_001.png"
            _write_page(source_path)
            source_sha_before = _sha256_for_test(source_path)
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testretry001",
            )
            run_service_job(service_root, "job-testretry001")
            job_root = service_root / "jobs" / "job-testretry001"
            _force_service_job_state(job_root, "failed", recovery_status="forced_failed_for_retry_test")

            summary = retry_service_job(service_root, "job-testretry001")
            processing_manifest = json.loads(
                (job_root / "derivatives" / "processing_manifest.json").read_text(encoding="utf-8")
            )
            production_summary = json.loads(
                (job_root / "metadata" / "production_run_summary.json").read_text(encoding="utf-8")
            )
            record = json.loads((job_root / SERVICE_JOB_RECORD_JSON).read_text(encoding="utf-8"))
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(summary["counts"]["resumed_files"], 1)
            self.assertEqual(summary["counts"]["reused_files"], 1)
            self.assertEqual(summary["counts"]["reprocessed_files"], 0)
            self.assertEqual(summary["counts"]["retry_list_files"], 0)
            self.assertTrue(summary["retry"]["provided"])
            self.assertEqual(summary["retry"]["status"], "completed")
            self.assertEqual(summary["retry"]["attempt"], 1)
            self.assertTrue(summary["retry"]["resume_processing"])
            self.assertTrue(summary["retry"]["reuse_existing_derivatives"])
            self.assertFalse(summary["retry"]["privacy"]["contains_paths"])
            self.assertFalse(summary["retry"]["privacy"]["contains_retry_file_list"])
            self.assertEqual(record["retry_count"], 1)
            self.assertEqual(record["retry"]["status"], "completed")
            self.assertTrue(record["retry"]["resume_processing"])
            self.assertTrue(record["retry"]["reuse_existing_derivatives"])
            self.assertEqual(processing_manifest["summary"]["resumed_files"], 1)
            self.assertEqual(processing_manifest["summary"]["existing_derivative_reused_files"], 1)
            self.assertEqual(production_summary["counts"]["resumed_files"], 1)
            self.assertEqual(production_summary["local_reuse_summary"]["reused_files"], 1)
            self.assertEqual(_sha256_for_test(source_path), source_sha_before)
            _assert_public_source_integrity(self, summary["source_integrity"], checked_files=1)
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_retry_job_rejects_non_retryable_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-retry-reject-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testretryreject001",
            )

            with self.assertRaisesRegex(RuntimeError, "retryable state"):
                retry_service_job(service_root, "job-testretryreject001")
            summary = recover_service_job(service_root, "job-testretryreject001")

            self.assertEqual(summary["state"], "created")

    def test_run_job_preserves_chinese_source_hash_with_public_integrity_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-source-integrity-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "输入 目录"
            service_root = root / "service-root"
            input_dir.mkdir()
            source_path = input_dir / "私有 页面001.png"
            _write_page(source_path)
            source_sha_before = _sha256_for_test(source_path)
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testsource001",
            )

            summary = run_service_job(service_root, "job-testsource001")
            public_raw = (
                service_root / "jobs" / "job-testsource001" / SERVICE_JOB_PUBLIC_SUMMARY_JSON
            ).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(_sha256_for_test(source_path), source_sha_before)
            _assert_public_source_integrity(self, summary["source_integrity"], checked_files=1)
            self.assertFalse(summary["source_images_modified"])
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "输入 目录", "私有 页面001")

    def test_recover_legacy_checkpoint_derives_public_processing_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-legacy-profile-recover-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(
                    input_dir=input_dir,
                    service_root=service_root,
                    rule_template="print-clean-v1",
                    workers=1,
                ),
                job_id="job-testlegacyprofile001",
            )
            job_root = service_root / "jobs" / "job-testlegacyprofile001"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["template_snapshot"].pop("processing_profile", None)
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            summary = recover_service_job(service_root, "job-testlegacyprofile001")
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "created")
            self.assertEqual(summary["template"]["rule_template_id"], "print-clean-v1")
            self.assertEqual(summary["template"]["processing_profile"], "print_clean")
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_start_job_async_returns_running_then_terminal_public_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-async-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testasync001",
            )

            running = start_service_job_async(service_root, "job-testasync001")
            terminal = _wait_for_terminal_summary(
                self,
                lambda: recover_service_job(service_root, "job-testasync001"),
            )
            _wait_for_async_job_inactive(self, service_root, "job-testasync001")
            job_root = service_root / "jobs" / "job-testasync001"
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(running["state"], "running")
            self.assertEqual(running["recovery"]["status"], "async_running")
            self.assertEqual(terminal["state"], "finished")
            self.assertTrue(terminal["quality"]["provided"])
            self.assertEqual(terminal["quality"]["status"], "pass")
            self.assertEqual(terminal["counts"]["processed_files"], 1)
            _assert_public_quality_summary(self, terminal["quality"])
            _assert_public_timing_summary(self, terminal["timings"], expected_processed_files=1)
            self.assertTrue(terminal["local_review"]["provided"])
            self.assertTrue(terminal["local_review"]["processing_review_package_html_written"])
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_two_async_jobs_keep_state_outputs_templates_and_reviews_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-concurrent-isolation-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            specs = {
                "job-testconcurrent001": {
                    "input_dir": root / "\u8f93\u5165-alpha",
                    "source_name": "\u79c1\u6709_alpha_001.png",
                    "project_id": "project-alpha",
                    "batch_id": "batch-alpha",
                    "rule_template": "print-clean-v1",
                    "processing_mode": "standard",
                    "processing_profile": "print_clean",
                },
                "job-testconcurrent002": {
                    "input_dir": root / "\u8f93\u5165-beta",
                    "source_name": "\u79c1\u6709_beta_001.png",
                    "project_id": "project-beta",
                    "batch_id": "batch-beta",
                    "rule_template": "dat-31-2017-standard",
                    "processing_mode": "light",
                    "processing_profile": "standard",
                },
            }
            source_hashes: dict[str, str] = {}
            for job_id, spec in specs.items():
                input_dir = spec["input_dir"]
                input_dir.mkdir()
                source_path = input_dir / spec["source_name"]
                _write_page(source_path)
                source_hashes[job_id] = _sha256_for_test(source_path)
                create_service_job(
                    ServiceJobConfig(
                        input_dir=input_dir,
                        service_root=service_root,
                        project_id=spec["project_id"],
                        batch_id=spec["batch_id"],
                        rule_template=spec["rule_template"],
                        processing_mode=spec["processing_mode"],
                        workers=1,
                    ),
                    job_id=job_id,
                )

            barrier = threading.Barrier(2)
            runner_entries: list[dict[str, Any]] = []
            runner_lock = threading.Lock()
            original_runner = service_jobs_module.run_production_folder

            def _barrier_runner(config):  # type: ignore[no-untyped-def]
                with runner_lock:
                    runner_entries.append(
                        {
                            "project_id": config.project_id,
                            "batch_id": config.batch_id,
                            "metadata_dir": str(config.metadata_output_dir.resolve()),
                            "derivatives_dir": str(config.derivative_output_dir.resolve()),
                        }
                    )
                barrier.wait(timeout=5)
                return original_runner(config)

            with mock.patch("archive_scan_qc.service_jobs.run_production_folder", side_effect=_barrier_runner):
                running_a = start_service_job_async(service_root, "job-testconcurrent001")
                running_b = start_service_job_async(service_root, "job-testconcurrent002")
                summaries = {
                    "job-testconcurrent001": _wait_for_terminal_summary(
                        self,
                        lambda: recover_service_job(service_root, "job-testconcurrent001"),
                    ),
                    "job-testconcurrent002": _wait_for_terminal_summary(
                        self,
                        lambda: recover_service_job(service_root, "job-testconcurrent002"),
                    ),
                }

            self.assertEqual(running_a["state"], "running")
            self.assertEqual(running_b["state"], "running")
            self.assertEqual(len(runner_entries), 2)
            self.assertEqual({entry["project_id"] for entry in runner_entries}, {"project-alpha", "project-beta"})
            self.assertEqual({entry["batch_id"] for entry in runner_entries}, {"batch-alpha", "batch-beta"})
            self.assertEqual(len({entry["metadata_dir"] for entry in runner_entries}), 2)
            self.assertEqual(len({entry["derivatives_dir"] for entry in runner_entries}), 2)

            for job_id, spec in specs.items():
                other_job_ids = set(specs) - {job_id}
                other_sources = [specs[other_job_id]["source_name"] for other_job_id in other_job_ids]
                job_root = service_root / "jobs" / job_id
                record = json.loads((job_root / SERVICE_JOB_RECORD_JSON).read_text(encoding="utf-8"))
                public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")
                production_summary = json.loads(
                    (job_root / "metadata" / "production_run_summary.json").read_text(encoding="utf-8")
                )
                processing_manifest_path = job_root / "derivatives" / "processing_manifest.json"
                processing_manifest = json.loads(processing_manifest_path.read_text(encoding="utf-8"))
                manifest_raw = processing_manifest_path.read_text(encoding="utf-8")

                summary = summaries[job_id]
                self.assertEqual(summary["state"], "finished")
                self.assertEqual(summary["template"]["rule_template_id"], spec["rule_template"])
                self.assertEqual(summary["template"]["processing_mode"], spec["processing_mode"])
                self.assertEqual(summary["template"]["processing_profile"], spec["processing_profile"])
                self.assertEqual(summary["counts"]["processed_files"], 1)
                self.assertTrue(summary["quality"]["provided"])
                self.assertTrue(summary["local_review"]["provided"])
                self.assertTrue(summary["local_review"]["production_review_queue_written"])
                _assert_public_source_integrity(self, summary["source_integrity"], checked_files=1)
                self.assertFalse(summary["source_images_modified"])
                self.assertEqual(_sha256_for_test(spec["input_dir"] / spec["source_name"]), source_hashes[job_id])

                self.assertEqual(record["template_snapshot"]["rule_template"]["id"], spec["rule_template"])
                self.assertEqual(record["template_snapshot"]["processing_mode"], spec["processing_mode"])
                self.assertEqual(record["template_snapshot"]["processing_profile"], spec["processing_profile"])
                self.assertEqual(record["project"]["project_id"], spec["project_id"])
                self.assertEqual(record["project"]["batch_id"], spec["batch_id"])
                self.assertEqual(processing_manifest["rule_template"]["id"], spec["rule_template"])
                self.assertEqual(processing_manifest["options"]["processing_profile"], spec["processing_profile"])
                self.assertEqual(production_summary["rule_template"]["id"], spec["rule_template"])
                self.assertEqual(production_summary["operator_summary"]["processing_mode"], spec["processing_mode"])
                self.assertEqual(
                    production_summary["operator_summary"]["processing_profile"],
                    spec["processing_profile"],
                )
                if spec["processing_mode"] == "standard":
                    self.assertTrue(processing_manifest["options"]["lighten_scanlines"])
                    self.assertTrue(processing_manifest["options"]["enhance_faded_text"])
                else:
                    self.assertTrue(processing_manifest["options"]["auto_crop"])
                    self.assertFalse(processing_manifest["options"]["deskew"])

                _assert_job_record_paths_inside_job_root(self, record, job_root)
                _assert_local_review_artifacts_inside_job_root(self, record, job_root)
                self.assertIn(spec["source_name"], manifest_raw)
                _assert_public_text_omits(self, public_raw, str(root.resolve()), spec["source_name"], *other_sources)
                for other_job_id in other_job_ids:
                    self.assertNotIn(other_job_id, public_raw)
                    self.assertNotIn(other_job_id, manifest_raw)
                for other_source in other_sources:
                    self.assertNotIn(other_source, manifest_raw)

    def test_recover_regenerates_missing_terminal_local_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-review-recover-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testreviewrecover001",
            )
            run_service_job(service_root, "job-testreviewrecover001")
            job_root = service_root / "jobs" / "job-testreviewrecover001"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record.pop("local_review", None)
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            for path in (job_root / "review").glob("*"):
                if path.is_file():
                    path.unlink()

            summary = recover_service_job(service_root, "job-testreviewrecover001")
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(summary["recovery"]["status"], "terminal_summary_recovered")
            self.assertTrue((job_root / "review" / "processing_review_package.json").is_file())
            self.assertTrue((job_root / "review" / "production_review_queue.json").is_file())
            self.assertTrue(summary["local_review"]["provided"])
            self.assertIn("readability_improvement", summary["local_review"]["processing_review_group_counts"])
            _assert_public_timing_summary(self, summary["timings"], expected_processed_files=1)
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_start_job_async_enforces_active_job_limit_before_marking_running(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-async-limit-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            for index in (1, 2):
                input_dir = root / f"private-source-{index}"
                input_dir.mkdir()
                _write_page(input_dir / f"private_page_{index:03d}.png")
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                    job_id=f"job-testasynclimit00{index}",
                )
            started = threading.Event()
            release = threading.Event()

            def _blocking_runner(config):  # type: ignore[no-untyped-def]
                started.set()
                release.wait(timeout=5)

            with (
                mock.patch("archive_scan_qc.service_jobs.SERVICE_JOB_MAX_ACTIVE_JOBS", 1),
                mock.patch("archive_scan_qc.service_jobs.run_production_folder", side_effect=_blocking_runner),
            ):
                running = start_service_job_async(service_root, "job-testasynclimit001")
                self.assertTrue(started.wait(timeout=5))
                with self.assertRaisesRegex(RuntimeError, "active job limit"):
                    start_service_job_async(service_root, "job-testasynclimit002")
                second_status = recover_service_job(service_root, "job-testasynclimit002")
                release.set()
                _wait_for_async_job_inactive(self, service_root, "job-testasynclimit001")
                terminal = recover_service_job(service_root, "job-testasynclimit001")
                self.assertIn(terminal["state"], {"needs_recovery", "failed", "finished"})

            self.assertEqual(running["state"], "running")
            self.assertEqual(second_status["state"], "created")

    def test_start_job_async_enforces_active_worker_limit_before_marking_running(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-async-worker-limit-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            for index, workers in ((1, 2), (2, 1)):
                input_dir = root / f"private-source-workers-{index}"
                input_dir.mkdir()
                _write_page(input_dir / f"private_page_{index:03d}.png")
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=workers),
                    job_id=f"job-testworkerlimit00{index}",
                )
            started = threading.Event()
            release = threading.Event()

            def _blocking_runner(config):  # type: ignore[no-untyped-def]
                started.set()
                release.wait(timeout=5)

            with (
                mock.patch("archive_scan_qc.service_jobs.SERVICE_JOB_MAX_ACTIVE_WORKERS", 2),
                mock.patch("archive_scan_qc.service_jobs.run_production_folder", side_effect=_blocking_runner),
            ):
                running = start_service_job_async(service_root, "job-testworkerlimit001")
                self.assertTrue(started.wait(timeout=5))
                with self.assertRaisesRegex(RuntimeError, "active worker limit"):
                    start_service_job_async(service_root, "job-testworkerlimit002")
                second_status = recover_service_job(service_root, "job-testworkerlimit002")
                release.set()
                _wait_for_async_job_inactive(self, service_root, "job-testworkerlimit001")
                terminal = recover_service_job(service_root, "job-testworkerlimit001")
                self.assertIn(terminal["state"], {"needs_recovery", "failed", "finished"})

            self.assertEqual(running["state"], "running")
            self.assertEqual(running["resource_limits"]["max_active_workers"], SERVICE_JOB_MAX_ACTIVE_WORKERS)
            self.assertEqual(second_status["state"], "created")

    def test_cancel_job_marks_terminal_public_summary_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-cancel-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testcancel001",
            )

            summary = cancel_service_job(service_root, "job-testcancel001")
            job_root = service_root / "jobs" / "job-testcancel001"
            record = json.loads((job_root / SERVICE_JOB_RECORD_JSON).read_text(encoding="utf-8"))
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "cancelled")
            self.assertEqual(summary["state_label_zh"], "已取消")
            self.assertEqual(summary["recovery"]["status"], "cancelled_by_service_request")
            self.assertEqual(record["state"], "cancelled")
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")
            with self.assertRaisesRegex(RuntimeError, "already terminal"):
                run_service_job(service_root, "job-testcancel001")

    def test_cancel_running_async_job_remains_cancelled_after_worker_finishes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-cancel-running-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testrunningcancel001",
            )

            started = threading.Event()
            release = threading.Event()

            def slow_production_run(config: Any) -> dict[str, Any]:
                started.set()
                self.assertTrue(release.wait(timeout=5), "test runner was not released")
                return {
                    "schema_version": "scan-qc.production-run-summary.v1",
                    "state": "finished",
                    "status": "finished",
                    "counts": {
                        "total_files": 1,
                        "processed_files": 1,
                        "failed_files": 0,
                        "remaining_files": 0,
                    },
                    "artifacts": {},
                }

            with mock.patch.object(service_jobs_module, "run_production_folder", side_effect=slow_production_run):
                running = start_service_job_async(service_root, "job-testrunningcancel001")
                self.assertTrue(started.wait(timeout=5))
                cancelled = cancel_service_job(service_root, "job-testrunningcancel001")
                release.set()

                deadline = time.monotonic() + 10
                terminal = cancelled
                while time.monotonic() < deadline:
                    terminal = recover_service_job(service_root, "job-testrunningcancel001")
                    if terminal["state"] == "cancelled" and terminal["source_integrity"]["provided"]:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f"cancelled async job did not publish source integrity: {terminal}")

            job_root = service_root / "jobs" / "job-testrunningcancel001"
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")
            record = json.loads((job_root / SERVICE_JOB_RECORD_JSON).read_text(encoding="utf-8"))

            self.assertEqual(running["state"], "running")
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertEqual(terminal["state"], "cancelled")
            self.assertEqual(terminal["recovery"]["status"], "cancelled_by_service_request")
            self.assertEqual(record["state"], "cancelled")
            self.assertTrue(terminal["source_integrity"]["provided"])
            self.assertEqual(terminal["source_integrity"]["checked_files"], 1)
            self.assertFalse(terminal["source_images_modified"])
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_recover_marks_stale_running_progress_without_leaking_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-recover-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testrecover001",
            )
            job_root = service_root / "jobs" / "job-testrecover001"
            (job_root / "metadata" / "production_run_progress.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run-progress.v1",
                        "state": "running",
                        "aggregate_processing": {
                            "aggregate_only": True,
                            "total_images": 8,
                            "processed_images": 3,
                            "remaining_images": 5,
                        },
                    }
                ),
                encoding="utf-8",
            )

            summary = recover_service_job(service_root, "job-testrecover001")
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "needs_recovery")
            self.assertEqual(
                summary["recovery"]["status"],
                "running_progress_requires_resume_after_service_restart",
            )
            self.assertEqual(summary["counts"]["total_files"], 8)
            self.assertEqual(summary["counts"]["processed_files"], 3)
            self.assertEqual(summary["counts"]["remaining_files"], 5)
            _assert_public_progress_timing_summary(self, summary["timings"], expected_processed_files=3)
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_recover_marks_running_record_without_progress_as_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-record-recover-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testrecord001",
            )
            job_root = service_root / "jobs" / "job-testrecord001"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["state"] = "running"
            record["recovery"]["status"] = "running"
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            summary = recover_service_job(service_root, "job-testrecord001")
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "needs_recovery")
            self.assertEqual(
                summary["recovery"]["status"],
                "running_record_requires_resume_after_service_restart",
            )
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_recover_marks_success_record_without_summary_as_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-missing-summary-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testmissingsummary001",
            )
            job_root = service_root / "jobs" / "job-testmissingsummary001"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["state"] = "finished"
            record["recovery"]["status"] = "forced_finished_without_summary"
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            summary = recover_service_job(service_root, "job-testmissingsummary001")
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "needs_recovery")
            self.assertEqual(summary["recovery"]["status"], "terminal_state_missing_production_summary")
            self.assertEqual(summary["quality"]["status"], "not_available")
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_recover_marks_success_progress_without_summary_as_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-progress-missing-summary-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testprogresssummary001",
            )
            job_root = service_root / "jobs" / "job-testprogresssummary001"
            (job_root / "metadata" / "production_run_progress.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run-progress.v1",
                        "state": "finished",
                        "aggregate_processing": {
                            "aggregate_only": True,
                            "total_images": 1,
                            "processed_images": 1,
                            "remaining_images": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            summary = recover_service_job(service_root, "job-testprogresssummary001")
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "needs_recovery")
            self.assertEqual(summary["recovery"]["status"], "terminal_state_missing_production_summary")
            self.assertEqual(summary["counts"]["processed_files"], 1)
            self.assertEqual(summary["quality"]["status"], "not_available")
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_recover_rejects_tampered_record_paths_outside_job_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-tamper-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testtamper001",
            )
            job_root = service_root / "jobs" / "job-testtamper001"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["paths"]["metadata_dir"] = str(root / "escaped-metadata")
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "escapes"):
                recover_service_job(service_root, "job-testtamper001")

    def test_recover_rejects_tampered_input_dir_inside_service_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-input-tamper-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testinput001",
            )
            job_root = service_root / "jobs" / "job-testinput001"
            nested_input = job_root / "metadata"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["paths"]["input_dir"] = str(nested_input)
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "input directory overlaps"):
                recover_service_job(service_root, "job-testinput001")

    def test_recover_service_jobs_indexes_multiple_jobs_without_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-index-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            for index in (1, 2):
                input_dir = root / f"private-source-{index}"
                input_dir.mkdir()
                _write_page(input_dir / f"private_page_{index:03d}.png")
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                    job_id=f"job-testindex00{index}",
                )

            summary = recover_service_jobs(service_root)
            index_path = service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON
            file_summary = json.loads(index_path.read_text(encoding="utf-8"))
            raw = json.dumps(summary, ensure_ascii=False)
            file_raw = json.dumps(file_summary, ensure_ascii=False)

            self.assertEqual(summary["job_count"], 2)
            self.assertEqual(summary["skipped_job_count"], 0)
            self.assertEqual(summary["state_counts"], {"created": 2})
            self.assertEqual(summary["quality"]["schema_version"], SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION)
            self.assertFalse(summary["quality"]["provided"])
            self.assertEqual(summary["quality"]["status"], "not_available")
            self.assertEqual(summary["quality"]["job_count"], 2)
            self.assertEqual(summary["quality"]["provided_job_count"], 0)
            self.assertEqual(summary["quality"]["not_provided_job_count"], 2)
            self.assertEqual(summary["quality"]["status_counts"], {"not_available": 2})
            self.assertEqual(summary["quality"]["quality_signal_status_counts"], {"not_available": 2})
            self.assertEqual(summary["quality"]["blocking_code_counts"], {})
            self.assertFalse(summary["quality"]["privacy"]["contains_paths"])
            self.assertFalse(summary["quality"]["privacy"]["contains_quality_rows"])
            self.assertEqual(
                summary["source_integrity"]["schema_version"],
                SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
            )
            self.assertFalse(summary["source_integrity"]["provided"])
            self.assertEqual(summary["source_integrity"]["status"], "not_available")
            self.assertEqual(summary["source_integrity"]["job_count"], 2)
            self.assertEqual(summary["source_integrity"]["provided_job_count"], 0)
            self.assertEqual(summary["source_integrity"]["not_provided_job_count"], 2)
            self.assertEqual(summary["source_integrity"]["checked_files"], 0)
            self.assertEqual(summary["source_integrity"]["status_counts"], {"not_available": 2})
            self.assertFalse(summary["source_integrity"]["source_images_modified"])
            self.assertFalse(summary["source_integrity"]["source_tree_changed"])
            self.assertFalse(summary["source_integrity"]["privacy"]["contains_paths"])
            self.assertFalse(summary["source_integrity"]["privacy"]["contains_file_lists"])
            self.assertEqual(
                summary["recovery_issues"]["schema_version"],
                SERVICE_JOB_INDEX_RECOVERY_ISSUES_SCHEMA_VERSION,
            )
            self.assertEqual(summary["recovery_issues"]["status"], "clear")
            self.assertEqual(summary["recovery_issues"]["issue_count"], 0)
            self.assertEqual(file_summary["schema_version"], "scan-qc.service-job-index-public-summary.v1")
            self.assertEqual(file_summary["job_count"], 2)
            self.assertEqual(file_summary["skipped_job_count"], 0)
            self.assertEqual(file_summary["state_counts"], {"created": 2})
            self.assertEqual(file_summary["quality"]["status_counts"], {"not_available": 2})
            self.assertEqual(file_summary["source_integrity"]["status_counts"], {"not_available": 2})
            self.assertEqual(file_summary["recovery_issues"]["status"], "clear")
            self.assertEqual({job["job_id"] for job in summary["jobs"]}, {"job-testindex001", "job-testindex002"})
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_")
            _assert_public_text_omits(self, file_raw, str(root.resolve()), "private_page_")

    def test_recover_service_jobs_reports_public_safe_skipped_invalid_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-index-issues-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            for index in (1, 2):
                input_dir = root / f"private-source-{index}"
                input_dir.mkdir()
                _write_page(input_dir / f"private_page_{index:03d}.png")
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                    job_id=f"job-testindexissue00{index}",
                )
            bad_job_root = service_root / "jobs" / "job-testindexissue002"
            record_path = bad_job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["paths"]["metadata_dir"] = str(root / "escaped-metadata")
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            summary = recover_service_jobs(service_root)
            file_summary = json.loads((service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8"))
            raw = json.dumps(summary, ensure_ascii=False)
            file_raw = json.dumps(file_summary, ensure_ascii=False)

            self.assertEqual(summary["job_count"], 1)
            self.assertEqual(summary["skipped_job_count"], 1)
            self.assertEqual(summary["state_counts"], {"created": 1})
            self.assertEqual({job["job_id"] for job in summary["jobs"]}, {"job-testindexissue001"})
            self.assertEqual(
                summary["recovery_issues"]["schema_version"],
                SERVICE_JOB_INDEX_RECOVERY_ISSUES_SCHEMA_VERSION,
            )
            self.assertEqual(summary["recovery_issues"]["status"], "issues_found")
            self.assertEqual(summary["recovery_issues"]["issue_count"], 1)
            self.assertEqual(summary["recovery_issues"]["skipped_job_count"], 1)
            self.assertEqual(summary["recovery_issues"]["by_code"], {"invalid_checkpoint": 1})
            self.assertFalse(summary["recovery_issues"]["privacy"]["contains_paths"])
            self.assertFalse(summary["recovery_issues"]["privacy"]["contains_exception_messages"])
            self.assertEqual(file_summary["skipped_job_count"], 1)
            self.assertEqual(file_summary["recovery_issues"]["by_code"], {"invalid_checkpoint": 1})
            _assert_public_text_omits(
                self,
                raw,
                str(root.resolve()),
                "private_page_",
                "escaped-metadata",
                "job-testindexissue002",
            )
            _assert_public_text_omits(
                self,
                file_raw,
                str(root.resolve()),
                "private_page_",
                "escaped-metadata",
                "job-testindexissue002",
            )

    def test_recover_invalid_checkpoint_json_stays_public_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-invalid-json-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            input_dir = root / "private-source"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testinvalidjson001",
            )
            record_path = service_root / "jobs" / "job-testinvalidjson001" / SERVICE_JOB_RECORD_JSON
            record_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "checkpoint JSON"):
                recover_service_job(service_root, "job-testinvalidjson001")
            summary = recover_service_jobs(service_root)
            file_summary = json.loads((service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8"))
            raw = json.dumps(summary, ensure_ascii=False)

            self.assertEqual(summary["job_count"], 0)
            self.assertEqual(summary["skipped_job_count"], 1)
            self.assertEqual(summary["recovery_issues"]["by_code"], {"invalid_checkpoint_json": 1})
            self.assertEqual(file_summary["recovery_issues"]["by_code"], {"invalid_checkpoint_json": 1})
            self.assertFalse(summary["recovery_issues"]["privacy"]["contains_paths"])
            self.assertFalse(summary["recovery_issues"]["privacy"]["contains_exception_messages"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001", "job-testinvalidjson001")


def _write_page(path: Path) -> None:
    image = Image.new("RGB", (360, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 28, 332, 452), outline=(220, 220, 220), width=2)
    for index in range(6):
        y = 95 + index * 42
        draw.line((72, y, 288, y), fill=(35, 35, 35), width=2)
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


def _wait_for_terminal_summary(testcase: unittest.TestCase, read_summary) -> dict:  # type: ignore[no-untyped-def]
    return _wait_for_state(
        testcase,
        read_summary,
        {"finished", "needs_review", "failed", "interrupted", "cancelled"},
    )


def _wait_for_state(testcase: unittest.TestCase, read_summary, states: set[str]) -> dict:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 10
    last_summary = None
    while time.monotonic() < deadline:
        try:
            last_summary = read_summary()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_summary = {"transient_read_error": type(exc).__name__}
            time.sleep(0.05)
            continue
        if last_summary.get("state") in states:
            return last_summary
        time.sleep(0.05)
    testcase.fail(f"service job did not reach one of {sorted(states)}: {last_summary}")


def _wait_for_async_job_inactive(testcase: unittest.TestCase, service_root: Path, job_id: str) -> None:
    key = service_jobs_module._async_job_key_from_parts(service_root, job_id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with service_jobs_module._ASYNC_JOB_LOCK:
            active = key in service_jobs_module._ASYNC_JOB_KEYS
        if not active:
            return
        time.sleep(0.05)
    testcase.fail(f"service async job did not unregister: {job_id}")


def _assert_public_text_omits(testcase: unittest.TestCase, raw: str, *private_values: str) -> None:
    normalized = raw.replace("\\\\", "\\")
    for value in private_values:
        testcase.assertNotIn(value, raw)
        testcase.assertNotIn(value, normalized)


def _force_service_job_state(job_root: Path, state: str, *, recovery_status: str) -> None:
    record_path = job_root / SERVICE_JOB_RECORD_JSON
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["state"] = state
    record["recovery"] = {
        **record.get("recovery", {}),
        "status": recovery_status,
        "resume_supported": True,
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _assert_public_quality_summary(testcase: unittest.TestCase, quality: dict) -> None:
    testcase.assertTrue(quality["provided"])
    testcase.assertEqual(quality["blocking_codes"], [])
    testcase.assertEqual(quality["processing_warning_files"], 0)
    testcase.assertEqual(quality["retry_list_files"], 0)
    testcase.assertEqual(quality["guardrail_failed_files"], 0)
    testcase.assertIn(quality["quality_signal_status"], {"measured_with_changes", "measured_no_quality_operations"})
    for field in (
        "any_quality_operation_changed_files",
        "geometry_changed_files",
        "background_cleanup_changed_files",
        "text_enhancement_changed_files",
        "defect_cleanup_changed_files",
    ):
        testcase.assertIsInstance(quality[field], int)
    testcase.assertEqual(
        set(quality["quality_operations_applied"]),
        {"geometry", "background_cleanup", "text_enhancement", "defect_cleanup"},
    )
    testcase.assertIn("quality_metrics", quality)
    for metric_id in (
        "brightness_delta",
        "contrast_delta",
        "max_trim_margin_ratio",
        "scanner_gutter_max_trim_margin_ratio",
        "tone_changed_pixel_ratio",
        "background_stains_changed_pixel_ratio",
        "tone_background_delta",
        "processed_output_brightness_increase",
    ):
        testcase.assertIn(metric_id, quality["quality_metrics"])
        metric = quality["quality_metrics"][metric_id]
        testcase.assertIsInstance(metric["count"], int)
        testcase.assertIn("average", metric)
        testcase.assertIn("max", metric)
    testcase.assertTrue(quality["guardrails"]["enabled"])
    testcase.assertEqual(quality["guardrails"]["warning_files"], 0)
    testcase.assertEqual(quality["guardrails"]["failed_files"], 0)
    testcase.assertEqual(quality["guardrails"]["failure_reasons"], {})


def _assert_public_timing_summary(
    testcase: unittest.TestCase,
    timings: dict,
    *,
    expected_processed_files: int,
) -> None:
    testcase.assertEqual(timings["schema_version"], "scan-qc.service-job-public-timings.v1")
    testcase.assertTrue(timings["provided"])
    testcase.assertEqual(timings["status"], "available")
    testcase.assertTrue(timings["aggregate_only"])
    testcase.assertTrue(timings["public_safe"])
    testcase.assertEqual(
        {stage["id"] for stage in timings["stage_timings"]["stages"]},
        {"scan", "process", "summarize"},
    )
    testcase.assertEqual(timings["aggregate_processing"]["processed_images"], expected_processed_files)
    testcase.assertIn("deskew", timings["operation_timings"])
    testcase.assertIn("despeckle", timings["operation_timings"])
    testcase.assertEqual(timings["operation_count"], len(timings["operation_timings"]))
    for operation in timings["operation_timings"].values():
        testcase.assertIn("enabled", operation)
        testcase.assertIsInstance(operation["file_count"], int)
        testcase.assertIsInstance(operation["elapsed_seconds"], float)
        testcase.assertIn("average_seconds_per_file", operation)
        testcase.assertIn("files_per_minute", operation)
    testcase.assertFalse(timings["privacy"]["contains_paths"])
    testcase.assertFalse(timings["privacy"]["contains_filenames"])


def _assert_public_progress_timing_summary(
    testcase: unittest.TestCase,
    timings: dict,
    *,
    expected_processed_files: int,
) -> None:
    testcase.assertTrue(timings["provided"])
    testcase.assertEqual(timings["aggregate_processing"]["processed_images"], expected_processed_files)
    testcase.assertEqual(timings["operation_timings"], {})
    testcase.assertFalse(timings["privacy"]["contains_paths"])


def _assert_job_record_paths_inside_job_root(
    testcase: unittest.TestCase,
    record: dict,
    job_root: Path,
) -> None:
    paths = record["paths"]
    resolved_job_root = job_root.resolve()
    for key, dirname in {
        "metadata_dir": "metadata",
        "derivatives_dir": "derivatives",
        "tmp_dir": "tmp",
        "checkpoint_dir": "checkpoints",
        "review_dir": "review",
        "log_dir": "logs",
    }.items():
        testcase.assertEqual(Path(paths[key]).resolve(), resolved_job_root / dirname)


def _assert_local_review_artifacts_inside_job_root(
    testcase: unittest.TestCase,
    record: dict,
    job_root: Path,
) -> None:
    review = record["local_review"]
    review_dir = job_root.resolve() / "review"
    testcase.assertEqual(Path(review["review_dir"]).resolve(), review_dir)
    artifacts = review["artifacts"]
    for artifact_path in artifacts.values():
        resolved = Path(artifact_path).resolve()
        testcase.assertEqual(resolved.parent, review_dir)
        testcase.assertTrue(resolved.is_file())


def _assert_public_source_integrity(
    testcase: unittest.TestCase,
    source_integrity: dict,
    *,
    checked_files: int,
) -> None:
    testcase.assertEqual(source_integrity["schema_version"], "scan-qc.service-job-source-integrity.v1")
    testcase.assertTrue(source_integrity["provided"])
    testcase.assertEqual(source_integrity["status"], "pass")
    testcase.assertTrue(source_integrity["aggregate_only"])
    testcase.assertTrue(source_integrity["public_safe"])
    testcase.assertEqual(source_integrity["checked_files"], checked_files)
    testcase.assertEqual(source_integrity["unchanged_files"], checked_files)
    testcase.assertEqual(source_integrity["modified_files"], 0)
    testcase.assertEqual(source_integrity["missing_files"], 0)
    testcase.assertEqual(source_integrity["added_files"], 0)
    testcase.assertFalse(source_integrity["source_images_modified"])
    testcase.assertFalse(source_integrity["source_tree_changed"])
    testcase.assertFalse(source_integrity["hashes_recorded_in_public_summary"])
    testcase.assertFalse(source_integrity["privacy"]["contains_paths"])
    testcase.assertFalse(source_integrity["privacy"]["contains_filenames"])
    testcase.assertFalse(source_integrity["privacy"]["contains_hashes"])


def _sha256_for_test(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
