from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw

from archive_scan_qc.service_http import create_service_http_server
from archive_scan_qc.service_jobs import (
    SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON,
    SERVICE_JOB_INDEX_QUALITY_SCHEMA_VERSION,
    SERVICE_JOB_INDEX_SOURCE_INTEGRITY_SCHEMA_VERSION,
    SERVICE_JOB_RECORD_JSON,
)


class ServiceHttpTransportTests(unittest.TestCase):
    def test_http_health_create_status_cancel_and_index_are_public_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-job-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "输入目录"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "私有页面001.png")

            with _running_server(service_root) as base_url:
                health_status, health = _json_request(base_url, "GET", "/api/health")
                capabilities_status, capabilities = _json_request(base_url, "GET", "/api/capabilities")
                create_status, created = _json_request(
                    base_url,
                    "POST",
                    "/api/jobs",
                    {
                        "job_id": "job-testhttp001",
                        "input_dir": str(input_dir),
                        "project_id": "project-http",
                        "batch_id": "batch-http",
                        "rule_template": "text-clean-print",
                        "workers": 1,
                    },
                )
                status_status, status = _json_request(base_url, "GET", "/api/jobs/job-testhttp001")
                cancel_status, cancelled = _json_request(base_url, "POST", "/api/jobs/job-testhttp001/cancel")
                index_status, index = _json_request(base_url, "GET", "/api/jobs")
                health_after_status, health_after = _json_request(base_url, "GET", "/api/health")

            raw = json.dumps(
                {
                    "health": health,
                    "capabilities": capabilities,
                    "created": created,
                    "status": status,
                    "cancelled": cancelled,
                    "index": index,
                    "health_after": health_after,
                },
                ensure_ascii=False,
            )

            self.assertEqual(health_status, 200)
            self.assertEqual(capabilities_status, 200)
            self.assertEqual(create_status, 201)
            self.assertEqual(status_status, 200)
            self.assertEqual(cancel_status, 200)
            self.assertEqual(index_status, 200)
            self.assertEqual(health_after_status, 200)
            self.assertEqual(health["schema_version"], "scan-qc.service-api.v1")
            self.assertFalse(health["job_index_available"])
            self.assertIn(
                ("GET", "/api/jobs"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("GET", "/api/rule-templates"),
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
            self.assertEqual(
                capabilities["schemas"]["service_job_index_recovery_issues"],
                "scan-qc.service-job-index-recovery-issues.v1",
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
            self.assertGreaterEqual(capabilities["resource_limits"]["max_active_workers"], 1)
            self.assertGreaterEqual(capabilities["resource_limits"]["min_free_space_bytes"], 1)
            self.assertGreaterEqual(capabilities["resource_limits"]["max_tmp_bytes_per_job"], 1)
            self.assertEqual(created["state"], "created")
            self.assertEqual(status["state"], "created")
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertEqual(index["state_counts"], {"cancelled": 1})
            self.assertEqual(index["skipped_job_count"], 0)
            self.assertEqual(index["recovery_issues"]["status"], "clear")
            self.assertTrue((service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).is_file())
            self.assertTrue(health_after["job_index_available"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "输入目录", "私有页面001")

    def test_http_run_job_writes_quality_summary_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-run-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            with _running_server(service_root) as base_url:
                _json_request(
                    base_url,
                    "POST",
                    "/api/jobs",
                    {
                        "job_id": "job-testhttprun001",
                        "input_dir": str(input_dir),
                        "rule_template": "dat-31-2017-standard",
                        "workers": 1,
                    },
                )
                run_status, run_summary = _json_request(base_url, "POST", "/api/jobs/job-testhttprun001/run")
                _force_service_job_state(
                    service_root / "jobs" / "job-testhttprun001",
                    "failed",
                    recovery_status="forced_failed_for_http_retry_test",
                )
                retry_status, retry_summary = _json_request(base_url, "POST", "/api/jobs/job-testhttprun001/retry")
                local_status, local_review = _json_request(
                    base_url,
                    "GET",
                    "/api/jobs/job-testhttprun001/local-review/production-review-queue",
                )
                invalid_artifact_status, invalid_artifact = _json_request(
                    base_url,
                    "GET",
                    "/api/jobs/job-testhttprun001/local-review/production_review_queue.json",
                )
                escaped_review_path = root / "escaped-review.json"
                escaped_review_path.write_text(
                    json.dumps({"schema_version": "private-review.v1"}, ensure_ascii=False),
                    encoding="utf-8",
                )
                record_path = service_root / "jobs" / "job-testhttprun001" / SERVICE_JOB_RECORD_JSON
                record = json.loads(record_path.read_text(encoding="utf-8"))
                record["local_review"]["artifacts"]["production_review_queue"] = str(escaped_review_path)
                record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                tampered_artifact_status, tampered_artifact = _json_request(
                    base_url,
                    "GET",
                    "/api/jobs/job-testhttprun001/local-review/production-review-queue",
                )
                status_status, status_summary = _json_request(base_url, "GET", "/api/jobs/job-testhttprun001")
            raw = json.dumps(
                {
                    "run": run_summary,
                    "retry": retry_summary,
                    "status": status_summary,
                    "tampered_artifact": tampered_artifact,
                },
                ensure_ascii=False,
            )
            local_raw = json.dumps(local_review, ensure_ascii=False)

            self.assertEqual(run_status, 200)
            self.assertEqual(retry_status, 200)
            self.assertEqual(local_status, 200)
            self.assertEqual(invalid_artifact_status, 400)
            self.assertEqual(tampered_artifact_status, 400)
            self.assertEqual(status_status, 200)
            self.assertEqual(local_review["schema_version"], "scan-qc.service-job-local-review-artifact.v1")
            self.assertEqual(local_review["artifact_id"], "production-review-queue")
            self.assertEqual(local_review["payload"]["schema_version"], "scan-qc.production-review-queue.v1")
            self.assertTrue(local_review["local_only"])
            self.assertTrue(local_review["sensitive"])
            self.assertFalse(local_review["public_safe"])
            self.assertTrue(local_review["privacy"]["contains_paths"])
            self.assertIn("private-input", local_raw)
            self.assertEqual(invalid_artifact["error"]["code"], "invalid_request")
            self.assertEqual(tampered_artifact["error"]["code"], "invalid_request")
            self.assertEqual(run_summary["state"], "finished")
            self.assertEqual(retry_summary["state"], "finished")
            self.assertEqual(status_summary["state"], "finished")
            self.assertEqual(run_summary["counts"]["resumed_files"], 0)
            self.assertEqual(run_summary["counts"]["reused_files"], 0)
            self.assertEqual(retry_summary["counts"]["resumed_files"], 1)
            self.assertEqual(retry_summary["counts"]["reused_files"], 1)
            self.assertEqual(retry_summary["counts"]["retry_list_files"], 0)
            self.assertFalse(run_summary["retry"]["provided"])
            self.assertTrue(retry_summary["retry"]["provided"])
            self.assertEqual(retry_summary["retry"]["status"], "completed")
            self.assertEqual(retry_summary["retry"]["attempt"], 1)
            self.assertFalse(retry_summary["retry"]["privacy"]["contains_paths"])
            self.assertTrue(run_summary["quality"]["provided"])
            self.assertEqual(run_summary["quality"]["status"], "pass")
            self.assertEqual(run_summary["quality"]["processed_files"], 1)
            self.assertEqual(run_summary["quality"]["blocking_codes"], [])
            self.assertEqual(run_summary["quality"]["quality_signal_status"], "measured_no_quality_operations")
            self.assertIn("defect_cleanup", run_summary["quality"]["quality_operations_applied"])
            self.assertTrue(run_summary["quality"]["guardrails"]["enabled"])
            self.assertEqual(run_summary["quality"]["guardrails"]["failed_files"], 0)
            _assert_public_timing_summary(self, run_summary["timings"], expected_processed_files=1)
            _assert_public_source_integrity(self, run_summary["source_integrity"], checked_files=1)
            self.assertFalse(run_summary["source_images_modified"])
            self.assertTrue(run_summary["local_review"]["provided"])
            self.assertTrue(run_summary["local_review"]["production_review_queue_written"])
            self.assertFalse(run_summary["local_review"]["privacy"]["contains_paths"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001", "escaped-review")

    def test_http_start_job_returns_running_then_terminal_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-start-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            with _running_server(service_root) as base_url:
                _json_request(
                    base_url,
                    "POST",
                    "/api/jobs",
                    {
                        "job_id": "job-testhttpstart001",
                        "input_dir": str(input_dir),
                        "rule_template": "dat-31-2017-standard",
                        "workers": 1,
                    },
                )
                start_status, running = _json_request(base_url, "POST", "/api/jobs/job-testhttpstart001/start")
                terminal = _wait_for_terminal_http(
                    self,
                    lambda: _json_request(base_url, "GET", "/api/jobs/job-testhttpstart001")[1],
                )
            raw = json.dumps({"running": running, "terminal": terminal}, ensure_ascii=False)

            self.assertEqual(start_status, 202)
            self.assertEqual(running["state"], "running")
            self.assertEqual(running["recovery"]["status"], "async_running")
            self.assertEqual(terminal["state"], "finished")
            self.assertTrue(terminal["quality"]["provided"])
            self.assertEqual(terminal["quality"]["processed_files"], 1)
            self.assertIn("geometry", terminal["quality"]["quality_operations_applied"])
            self.assertEqual(terminal["quality"]["guardrails"]["failure_reasons"], {})
            _assert_public_timing_summary(self, terminal["timings"], expected_processed_files=1)
            self.assertTrue(terminal["local_review"]["processing_review_package_written"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001")

    def test_http_production_facade_wraps_job_boundary_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-production-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            with _running_server(service_root) as base_url:
                session_status, session = _json_request(base_url, "GET", "/api/production/session")
                setup_status, setup = _json_request(
                    base_url,
                    "POST",
                    "/api/production/setup",
                    {
                        "job_id": "job-productionhttp001",
                        "input_dir": str(input_dir),
                        "rule_template": "dat-31-2017-standard",
                        "workers": 1,
                    },
                )
                start_status, running = _json_request(
                    base_url,
                    "POST",
                    "/api/production/start",
                    {"job_id": "job-productionhttp001"},
                )
                terminal = _wait_for_terminal_http(
                    self,
                    lambda: _json_request(
                        base_url,
                        "GET",
                        "/api/production/progress?job_id=job-productionhttp001",
                    )[1]["job"],
                )
                queue_status, review_queue = _json_request(
                    base_url,
                    "GET",
                    "/api/production/review-queue?job_id=job-productionhttp001",
                )
                _ensure_preview_queue_item(service_root, "job-productionhttp001", "private_page_001.png")
                job_preview_status, job_preview_headers, job_preview_body = _raw_request(
                    base_url,
                    "GET",
                    "/api/jobs/job-productionhttp001/local-preview/PRQ000001?source=original",
                )
                production_preview_status, production_preview_headers, production_preview_body = _raw_request(
                    base_url,
                    "GET",
                    "/api/production/preview?job_id=job-productionhttp001&local_id=PRQ000001&source=original",
                )
                _ensure_preview_queue_item(
                    service_root,
                    "job-productionhttp001",
                    "../private_page_001.png",
                    local_id="PRQ999999",
                )
                unsafe_preview_status, unsafe_preview = _json_request(
                    base_url,
                    "GET",
                    "/api/production/preview?job_id=job-productionhttp001&local_id=PRQ999999&source=original",
                )
                actions_status, review_actions = _json_request(
                    base_url,
                    "POST",
                    "/api/production/review-actions",
                    {
                        "job_id": "job-productionhttp001",
                        "review_decisions": _review_decision_summary(("accepted_issue",)),
                    },
                )
                job_review_history_status, job_review_history = _json_request(
                    base_url,
                    "GET",
                    "/api/jobs/job-productionhttp001/review-history",
                )
                production_review_history_status, production_review_history = _json_request(
                    base_url,
                    "GET",
                    "/api/production/review-history?job_id=job-productionhttp001",
                )
                post_actions_progress_status, post_actions_progress = _json_request(
                    base_url,
                    "GET",
                    "/api/production/progress?job_id=job-productionhttp001",
                )
                finish_status, finish_export = _json_request(
                    base_url,
                    "POST",
                    "/api/production/finish-export",
                    {"job_id": "job-productionhttp001"},
                )
                final_session_status, final_session = _json_request(base_url, "GET", "/api/production/session")
                missing_job_id_status, missing_job_id = _json_request(
                    base_url,
                    "GET",
                    "/api/production/progress",
                )
                invalid_preview_status, invalid_preview = _json_request(
                    base_url,
                    "GET",
                    "/api/production/preview?job_id=job-productionhttp001&local_id=PRQ000001&source=bad",
                )
                managed_root_status, managed_root = _json_request(
                    base_url,
                    "POST",
                    "/api/production/setup",
                    {
                        "service_root": str(root / "client-root"),
                        "input_dir": str(input_dir),
                    },
                )
            raw = json.dumps(
                {
                    "session": session,
                    "setup": setup,
                    "running": running,
                    "terminal": terminal,
                    "review_queue": review_queue,
                    "unsafe_preview": unsafe_preview,
                    "review_actions": review_actions,
                    "job_review_history": job_review_history,
                    "production_review_history": production_review_history,
                    "post_actions_progress": post_actions_progress,
                    "finish_export": finish_export,
                    "final_session": final_session,
                    "missing_job_id": missing_job_id,
                    "invalid_preview": invalid_preview,
                    "managed_root": managed_root,
                },
                ensure_ascii=False,
            )

            self.assertEqual(session_status, 200)
            self.assertEqual(setup_status, 201)
            self.assertEqual(start_status, 202)
            self.assertEqual(queue_status, 200)
            self.assertEqual(job_preview_status, 200)
            self.assertEqual(production_preview_status, 200)
            self.assertEqual(unsafe_preview_status, 400)
            self.assertEqual(actions_status, 200)
            self.assertEqual(job_review_history_status, 200)
            self.assertEqual(production_review_history_status, 200)
            self.assertEqual(post_actions_progress_status, 200)
            self.assertEqual(finish_status, 200)
            self.assertEqual(final_session_status, 200)
            self.assertEqual(missing_job_id_status, 400)
            self.assertEqual(invalid_preview_status, 400)
            self.assertEqual(managed_root_status, 400)
            self.assertEqual(session["schema_version"], "scan-qc.service-production-session.v1")
            self.assertEqual(session["view"], "session")
            self.assertEqual(session["session"]["quality"]["status"], "not_available")
            self.assertEqual(session["session"]["source_integrity"]["status"], "not_available")
            self.assertEqual(session["session"]["recovery_issues"]["status"], "clear")
            self.assertEqual(final_session["session"]["job_count"], 1)
            self.assertEqual(final_session["session"]["state_counts"], {"finished": 1})
            self.assertEqual(final_session["session"]["recovery_issues"]["status"], "clear")
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
            self.assertEqual(setup["view"], "setup")
            self.assertEqual(setup["job"]["state"], "created")
            self.assertEqual(running["view"], "start")
            self.assertEqual(running["job"]["state"], "running")
            self.assertEqual(terminal["state"], "finished")
            self.assertEqual(review_queue["view"], "review_queue")
            self.assertTrue(review_queue["review_queue"]["available"])
            self.assertEqual(review_queue["review_queue"]["local_review_artifact_id"], "production-review-queue")
            self.assertEqual(job_preview_headers.get("X-AI4-Local-Only"), "true")
            self.assertEqual(job_preview_headers.get("X-AI4-Preview-Source"), "original")
            self.assertIn("image/png", job_preview_headers.get("Content-Type", ""))
            self.assertNotIn("Content-Disposition", job_preview_headers)
            self.assertGreater(len(job_preview_body), 20)
            self.assertEqual(production_preview_headers.get("X-AI4-Preview-Source"), "original")
            self.assertEqual(job_preview_body, production_preview_body)
            self.assertEqual(unsafe_preview["error"]["code"], "invalid_request")
            self.assertEqual(review_actions["view"], "review_actions")
            self.assertTrue(review_actions["review_actions"]["saved"])
            self.assertEqual(review_actions["review_actions"]["verification"]["status"], "pass")
            self.assertEqual(review_actions["review_actions"]["decision_summary"]["total_decisions"], 1)
            self.assertTrue(review_actions["review_actions"]["review_history_written"])
            self.assertEqual(review_actions["review_actions"]["history"]["entry_count"], 1)
            self.assertEqual(review_actions["review_actions"]["history"]["latest_verification_status"], "pass")
            self.assertEqual(job_review_history["schema_version"], "scan-qc.service-job-review-history.v1")
            self.assertTrue(job_review_history["provided"])
            self.assertEqual(job_review_history["review_history"]["entry_count"], 1)
            self.assertEqual(job_review_history["review_history"]["latest_verification_status"], "pass")
            self.assertFalse(job_review_history["review_history"]["privacy"]["contains_review_rows"])
            self.assertFalse(job_review_history["review_history"]["privacy"]["contains_local_ids"])
            self.assertEqual(production_review_history["view"], "review_history")
            self.assertEqual(production_review_history["review_history"]["review_history"]["entry_count"], 1)
            self.assertTrue(production_review_history["privacy"]["public_safe"])
            self.assertEqual(post_actions_progress["job"]["review_actions"]["history"]["entry_count"], 1)
            self.assertNotIn("PRQ000001", raw)
            review_history_path = service_root / "jobs" / "job-productionhttp001" / "review" / "service_job_review_history.json"
            self.assertTrue(review_history_path.is_file())
            review_history = json.loads(review_history_path.read_text(encoding="utf-8"))
            self.assertEqual(review_history["entry_count"], 1)
            self.assertIn("PRQ000001", json.dumps(review_history, ensure_ascii=False))
            self.assertEqual(finish_export["view"], "finish_export")
            self.assertTrue(finish_export["finish_export"]["ready_for_export"])
            self.assertEqual(missing_job_id["error"]["code"], "missing_request_field")
            self.assertEqual(invalid_preview["error"]["code"], "invalid_request")
            self.assertEqual(managed_root["error"]["code"], "service_root_managed_by_server")
            self.assertFalse(review_queue["privacy"]["contains_paths"])
            self.assertNotIn("../", raw)
            _assert_public_text_omits(self, raw, str(root.resolve()), "private-input", "private_page_001", "client-root")

    def test_http_rejects_client_managed_service_root_without_echoing_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-reject-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            with _running_server(service_root) as base_url:
                status, payload = _json_request(
                    base_url,
                    "POST",
                    "/api/jobs",
                    {
                        "service_root": str(root / "client-root"),
                        "input_dir": str(root / "missing-input"),
                    },
                )
            raw = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(status, 400)
            self.assertEqual(payload["schema_version"], "scan-qc.service-api-error.v1")
            self.assertEqual(payload["error"]["code"], "service_root_managed_by_server")
            self.assertTrue(payload["public_safe"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "client-root", "missing-input")

    def test_http_missing_input_dir_error_is_public_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-missing-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            with _running_server(service_root) as base_url:
                status, payload = _json_request(
                    base_url,
                    "POST",
                    "/api/jobs",
                    {
                        "input_dir": str(root / "missing-input"),
                    },
                )
            raw = json.dumps(payload, ensure_ascii=False)

            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "input_dir_missing")
            self.assertFalse(payload["private_paths_exposed"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "missing-input")

    def test_http_missing_job_error_is_public_safe_not_found(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-missing-job-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            with _running_server(service_root) as base_url:
                status, payload = _json_request(base_url, "GET", "/api/jobs/job-missing001")
                review_status, review_payload = _json_request(
                    base_url,
                    "GET",
                    "/api/jobs/job-missing001/local-review/processing-review-package",
                )
            raw = json.dumps({"status": payload, "review": review_payload}, ensure_ascii=False)

            self.assertEqual(status, 404)
            self.assertEqual(review_status, 404)
            self.assertEqual(payload["error"]["code"], "job_not_found")
            self.assertEqual(review_payload["error"]["code"], "job_not_found")
            self.assertFalse(payload["private_paths_exposed"])
            self.assertFalse(review_payload["private_paths_exposed"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "job-missing001")

    def test_http_server_rejects_non_loopback_bind_host_for_local_only_api(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-host-") as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "loopback"):
                create_service_http_server(service_root=root / "service-root", host="0.0.0.0", port=0)

    def test_http_rule_template_catalog_and_detail_are_public_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-http-templates-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            with _running_server(service_root) as base_url:
                catalog_status, catalog = _json_request(base_url, "GET", "/api/rule-templates")
                detail_status, detail = _json_request(
                    base_url,
                    "GET",
                    "/api/rule-templates/text-clean-readable-v1",
                )
                validate_status, validation = _json_request(
                    base_url,
                    "POST",
                    "/api/rule-templates/validate",
                    {
                        "template": {
                            "name": "http-custom-template",
                            "min_dpi": 300,
                            "dpi_purpose": "print",
                            "name_pattern": str(root / "private-pattern"),
                            "rules": {
                                "dpi_missing": {"enabled": False, "severity": "P2"},
                            },
                        }
                    },
                )
                save_status, saved = _json_request(
                    base_url,
                    "POST",
                    "/api/rule-templates",
                    {
                        "template_id": "custom-http001",
                        "template": {
                            "name": "http-custom-template",
                            "min_dpi": 300,
                            "dpi_purpose": "print",
                            "name_pattern": str(root / "private-template-pattern"),
                            "processing_defaults": {"auto_crop": True, "normalize_tones": True},
                            "rules": {
                                "dpi_missing": {"enabled": False, "severity": "P2"},
                            },
                        }
                    },
                )
                duplicate_status, duplicate_error = _json_request(
                    base_url,
                    "POST",
                    "/api/rule-templates",
                    {
                        "template_id": "custom-http001",
                        "template": {"name": "duplicate-template"},
                    },
                )
                saved_detail_status, saved_detail = _json_request(
                    base_url,
                    "GET",
                    "/api/rule-templates/custom-http001",
                )
                update_status, updated = _json_request(
                    base_url,
                    "PUT",
                    "/api/rule-templates/custom-http001",
                    {
                        "template": {
                            "name": "http-custom-template-updated",
                            "processing_defaults": {"auto_crop": True},
                        }
                    },
                )
                managed_root_status, managed_root_error = _json_request(
                    base_url,
                    "POST",
                    "/api/rule-templates",
                    {
                        "service_root": str(root / "client-root"),
                        "template_id": "custom-http002",
                        "template": {"name": "bad-template"},
                    },
                )
                catalog_after_status, catalog_after = _json_request(base_url, "GET", "/api/rule-templates")
                custom_status, custom_error = _json_request(base_url, "GET", "/api/rule-templates/custom")
                missing_detail_status, missing_detail_error = _json_request(
                    base_url,
                    "GET",
                    "/api/rule-templates/custom-missing001",
                )
                missing_job_status, missing_job_error = _json_request(
                    base_url,
                    "POST",
                    "/api/jobs",
                    {
                        "job_id": "job-missingtemplate001",
                        "input_dir": str(input_dir),
                        "rule_template": "custom-missing001",
                        "workers": 1,
                    },
                )
            raw = json.dumps(
                {
                    "catalog": catalog,
                    "detail": detail,
                    "validation": validation,
                    "saved": saved,
                    "saved_detail": saved_detail,
                    "updated": updated,
                    "catalog_after": catalog_after,
                    "duplicate_error": duplicate_error,
                    "managed_root_error": managed_root_error,
                    "custom_error": custom_error,
                    "missing_detail_error": missing_detail_error,
                    "missing_job_error": missing_job_error,
                },
                ensure_ascii=False,
            )

            self.assertEqual(catalog_status, 200)
            self.assertEqual(detail_status, 200)
            self.assertEqual(validate_status, 200)
            self.assertEqual(save_status, 201)
            self.assertEqual(duplicate_status, 409)
            self.assertEqual(saved_detail_status, 200)
            self.assertEqual(update_status, 200)
            self.assertEqual(managed_root_status, 400)
            self.assertEqual(catalog_after_status, 200)
            self.assertEqual(custom_status, 400)
            self.assertEqual(missing_detail_status, 404)
            self.assertEqual(missing_job_status, 404)
            self.assertEqual(catalog["schema_version"], "scan-qc.rule-template-catalog.v1")
            self.assertEqual(detail["schema_version"], "scan-qc.rule-template-dry-run.v1")
            self.assertEqual(validation["schema_version"], "scan-qc.rule-template-custom-validation.v1")
            self.assertEqual(saved["schema_version"], "scan-qc.service-rule-template-write.v1")
            self.assertEqual(saved["template"]["id"], "custom-http001")
            self.assertTrue(saved["template"]["processing_defaults"]["normalize_tones"])
            self.assertEqual(saved_detail["schema_version"], "scan-qc.service-rule-template-detail.v1")
            self.assertEqual(updated["action"], "updated")
            self.assertEqual(duplicate_error["error"]["code"], "rule_template_already_exists")
            self.assertEqual(managed_root_error["error"]["code"], "service_root_managed_by_server")
            self.assertTrue(validation["valid"])
            self.assertEqual(validation["template"]["id"], "custom")
            self.assertEqual(validation["validation"]["rule_count"], 1)
            self.assertFalse(validation["privacy"]["contains_paths"])
            self.assertIn("print-clean-v1", {template["id"] for template in catalog["templates"]})
            self.assertIn("custom-http001", {template["id"] for template in catalog_after["templates"]})
            self.assertEqual(detail["template"]["id"], "text-clean-readable-v1")
            self.assertIn("text_clean_requires_pure_text_batch_confirmation", detail["risk_codes"])
            self.assertFalse(detail["derivative_images_written"])
            self.assertEqual(custom_error["schema_version"], "scan-qc.service-api-error.v1")
            self.assertEqual(custom_error["error"]["code"], "invalid_request")
            self.assertFalse(custom_error["private_paths_exposed"])
            self.assertEqual(missing_detail_error["error"]["code"], "rule_template_not_found")
            self.assertEqual(missing_job_error["error"]["code"], "rule_template_not_found")
            self.assertFalse((service_root / "jobs" / "job-missingtemplate001").exists())
            _assert_public_text_omits(
                self,
                raw,
                str(root.resolve()),
                "private-pattern",
                "private-template-pattern",
                "private-input",
                "private_page_001",
            )


class _running_server:
    def __init__(self, service_root: Path) -> None:
        self._server = create_service_http_server(service_root=service_root, port=0)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _json_request(base_url: str, method: str, path: str, payload: dict[str, object] | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _raw_request(base_url: str, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    request = Request(f"{base_url}{path}", method=method)
    with urlopen(request, timeout=5) as response:
        return response.status, dict(response.headers.items()), response.read()


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


def _write_page(path: Path) -> None:
    image = Image.new("RGB", (220, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((32, 42, 188, 46), fill=(40, 40, 40))
    image.save(path, dpi=(300, 300))


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


def _ensure_preview_queue_item(
    service_root: Path,
    job_id: str,
    relative_path: str,
    *,
    local_id: str = "PRQ000001",
) -> None:
    queue_path = service_root / "jobs" / job_id / "review" / "production_review_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    items = queue.get("items")
    if not isinstance(items, list):
        items = []
        queue["items"] = items
    if not any(isinstance(item, dict) and item.get("local_id") == local_id for item in items):
        items.insert(
            0,
            {
                "local_id": local_id,
                "relative_path": relative_path,
                "severity": "P2",
                "source_category": "processing_failure",
                "suggested_action": "reprocess",
            },
        )
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _wait_for_terminal_http(testcase: unittest.TestCase, read_summary) -> dict:  # type: ignore[no-untyped-def]
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
    testcase.assertIn("process", {stage["id"] for stage in timings["stage_timings"]["stages"]})
    testcase.assertIn("deskew", timings["operation_timings"])
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
