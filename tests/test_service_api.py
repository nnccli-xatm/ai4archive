from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.service_api import (
    cancel_job_response,
    create_job_response,
    get_job_response,
    get_rule_template_response,
    list_rule_templates_response,
    recover_jobs_response,
    run_job_response,
    service_capabilities,
    service_health,
    start_job_response,
)
from archive_scan_qc.service_jobs import SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON


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
                ("POST", "/api/jobs/{job_id}/run"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertIn(
                ("POST", "/api/jobs/{job_id}/start"),
                {(item["method"], item["path"]) for item in capabilities["endpoints"]},
            )
            self.assertEqual(capabilities["schemas"]["rule_template_catalog"], "scan-qc.rule-template-catalog.v1")
            self.assertEqual(capabilities["schemas"]["rule_template_dry_run"], "scan-qc.rule-template-dry-run.v1")
            self.assertGreaterEqual(capabilities["resource_limits"]["max_active_async_jobs"], 1)
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
            cancel_summary = cancel_job_response(service_root=service_root, job_id="job-testapi001")
            index_summary = recover_jobs_response(service_root=service_root)
            health = service_health(service_root=service_root)
            raw = json.dumps(
                {
                    "create": create_summary,
                    "status": status_summary,
                    "cancel": cancel_summary,
                    "index": index_summary,
                    "health": health,
                },
                ensure_ascii=False,
            )

            self.assertEqual(create_summary["state"], "created")
            self.assertEqual(create_summary["template"]["rule_template_id"], "text-clean-print")
            self.assertEqual(status_summary["state"], "created")
            self.assertEqual(cancel_summary["state"], "cancelled")
            self.assertEqual(index_summary["state_counts"], {"cancelled": 1})
            self.assertTrue((service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).is_file())
            self.assertTrue(health["job_index_available"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "输入目录", "私有页面001")

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
            raw = json.dumps({"summary": summary, "status": status}, ensure_ascii=False)

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(status["state"], "finished")
            self.assertTrue(summary["quality"]["provided"])
            self.assertEqual(summary["quality"]["status"], "pass")
            self.assertEqual(summary["quality"]["processed_files"], 1)
            self.assertEqual(summary["quality"]["blocking_codes"], [])
            self.assertIn("background_cleanup", summary["quality"]["quality_operations_applied"])
            self.assertTrue(summary["quality"]["guardrails"]["enabled"])
            self.assertEqual(summary["quality"]["guardrails"]["failed_files"], 0)
            self.assertTrue(summary["local_review"]["provided"])
            self.assertTrue(summary["local_review"]["production_review_queue_written"])
            self.assertFalse(summary["local_review"]["privacy"]["contains_paths"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001")

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
            self.assertTrue(terminal["local_review"]["processing_review_package_written"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_001")

    def test_create_job_response_requires_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "input_dir"):
            create_job_response({"service_root": "service-root"})


def _write_page(path: Path) -> None:
    image = Image.new("RGB", (220, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((32, 42, 188, 46), fill=(40, 40, 40))
    image.save(path, dpi=(300, 300))


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


if __name__ == "__main__":
    unittest.main()
