from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw

from archive_scan_qc.service_http import create_service_http_server
from archive_scan_qc.service_jobs import SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON


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
            self.assertEqual(created["state"], "created")
            self.assertEqual(status["state"], "created")
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertEqual(index["state_counts"], {"cancelled": 1})
            self.assertTrue((service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON).is_file())
            self.assertTrue(health_after["job_index_available"])
            _assert_public_text_omits(self, raw, str(root.resolve()), "输入目录", "私有页面001")

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


def _write_page(path: Path) -> None:
    image = Image.new("RGB", (220, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((32, 42, 188, 46), fill=(40, 40, 40))
    image.save(path, dpi=(300, 300))


def _assert_public_text_omits(testcase: unittest.TestCase, raw: str, *private_values: str) -> None:
    normalized = raw.replace("\\\\", "\\")
    for value in private_values:
        testcase.assertNotIn(value, raw)
        testcase.assertNotIn(value, normalized)


if __name__ == "__main__":
    unittest.main()
