"""Minimal local HTTP transport for the service API core."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .rule_templates import RuleTemplateNotFoundError
from .service_api import (
    cancel_job_response,
    create_job_response,
    get_job_local_review_artifact_response,
    get_job_local_preview_response,
    get_rule_template_response,
    get_job_response,
    list_rule_templates_response,
    production_finish_export_response,
    production_progress_response,
    production_review_actions_response,
    production_review_queue_response,
    production_session_response,
    production_setup_response,
    production_start_response,
    recover_jobs_response,
    retry_job_response,
    run_job_response,
    save_rule_template_response,
    service_api_privacy,
    service_capabilities,
    service_health,
    start_job_response,
    validate_rule_template_response,
)
from .service_jobs import ServiceJobNotFoundError


SERVICE_API_ERROR_SCHEMA_VERSION = "scan-qc.service-api-error.v1"
MAX_JSON_BODY_BYTES = 64 * 1024


class ServiceHttpError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ServiceApiRequestHandler(BaseHTTPRequestHandler):
    server_version = "AI4ArchiveServiceAPI/0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            path = _normalized_path(self.path)
            if path == "/api/health":
                self._send_json(200, service_health(service_root=self._service_root))
                return
            if path == "/api/capabilities":
                self._send_json(200, service_capabilities())
                return
            if path == "/api/rule-templates":
                self._send_json(200, list_rule_templates_response(service_root=self._service_root))
                return
            if path == "/api/jobs":
                self._send_json(200, recover_jobs_response(service_root=self._service_root))
                return
            if path == "/api/production/session":
                self._send_json(200, production_session_response(service_root=self._service_root))
                return
            if path == "/api/production/progress":
                self._send_json(
                    200,
                    production_progress_response(
                        service_root=self._service_root,
                        job_id=_required_query_arg(self.path, "job_id"),
                    ),
                )
                return
            if path == "/api/production/review-queue":
                self._send_json(
                    200,
                    production_review_queue_response(
                        service_root=self._service_root,
                        job_id=_required_query_arg(self.path, "job_id"),
                    ),
                )
                return
            if path == "/api/production/preview":
                self._send_local_preview(
                    get_job_local_preview_response(
                        service_root=self._service_root,
                        job_id=_required_query_arg(self.path, "job_id"),
                        local_id=_required_query_arg(self.path, "local_id"),
                        source=_optional_query_arg(self.path, "source"),
                    )
                )
                return

            segments = _path_segments(path)
            if len(segments) == 3 and segments[:2] == ["api", "rule-templates"]:
                self._send_json(200, get_rule_template_response(service_root=self._service_root, template_id=segments[2]))
                return
            if len(segments) == 3 and segments[:2] == ["api", "jobs"]:
                self._send_json(200, get_job_response(service_root=self._service_root, job_id=segments[2]))
                return
            if len(segments) == 5 and segments[:2] == ["api", "jobs"] and segments[3] == "local-review":
                self._send_json(
                    200,
                    get_job_local_review_artifact_response(
                        service_root=self._service_root,
                        job_id=segments[2],
                        artifact_id=segments[4],
                    ),
                )
                return
            if len(segments) == 5 and segments[:2] == ["api", "jobs"] and segments[3] == "local-preview":
                self._send_local_preview(
                    get_job_local_preview_response(
                        service_root=self._service_root,
                        job_id=segments[2],
                        local_id=segments[4],
                        source=_optional_query_arg(self.path, "source"),
                    )
                )
                return
            raise ServiceHttpError(404, "not_found", "Endpoint not found.")
        except Exception as exc:  # pragma: no cover - covered through _send_exception branches
            self._send_exception(exc)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            path = _normalized_path(self.path)
            segments = _path_segments(path)
            if path == "/api/rule-templates/validate":
                self._send_json(200, validate_rule_template_response(self._read_json_body()))
                return
            if path == "/api/rule-templates":
                payload = self._read_json_body()
                if "service_root" in payload:
                    raise ServiceHttpError(
                        400,
                        "service_root_managed_by_server",
                        "Service root is configured by the server.",
                    )
                self._send_json(201, save_rule_template_response(payload, service_root=self._service_root))
                return
            if path == "/api/production/setup":
                payload = self._read_json_body()
                if "service_root" in payload:
                    raise ServiceHttpError(
                        400,
                        "service_root_managed_by_server",
                        "Service root is configured by the server.",
                    )
                job_id = payload.get("job_id")
                if job_id is not None and not isinstance(job_id, str):
                    raise ServiceHttpError(400, "invalid_job_id", "Job id must be a string.")
                request = {**payload, "service_root": str(self._service_root)}
                self._send_json(201, production_setup_response(request, job_id=job_id))
                return
            if path == "/api/production/start":
                payload = self._read_json_body()
                if "service_root" in payload:
                    raise ServiceHttpError(
                        400,
                        "service_root_managed_by_server",
                        "Service root is configured by the server.",
                    )
                self._send_json(
                    202,
                    production_start_response(
                        service_root=self._service_root,
                        job_id=_required_payload_arg(payload, "job_id"),
                    ),
                )
                return
            if path == "/api/production/finish-export":
                payload = self._read_json_body()
                if "service_root" in payload:
                    raise ServiceHttpError(
                        400,
                        "service_root_managed_by_server",
                        "Service root is configured by the server.",
                    )
                self._send_json(
                    200,
                    production_finish_export_response(
                        service_root=self._service_root,
                        job_id=_required_payload_arg(payload, "job_id"),
                    ),
                )
                return
            if path == "/api/production/review-actions":
                payload = self._read_json_body()
                if "service_root" in payload:
                    raise ServiceHttpError(
                        400,
                        "service_root_managed_by_server",
                        "Service root is configured by the server.",
                    )
                self._send_json(
                    200,
                    production_review_actions_response(
                        payload,
                        service_root=self._service_root,
                    ),
                )
                return
            if path == "/api/jobs":
                payload = self._read_json_body()
                if "service_root" in payload:
                    raise ServiceHttpError(
                        400,
                        "service_root_managed_by_server",
                        "Service root is configured by the server.",
                    )
                job_id = payload.get("job_id")
                if job_id is not None and not isinstance(job_id, str):
                    raise ServiceHttpError(400, "invalid_job_id", "Job id must be a string.")
                request = {**payload, "service_root": str(self._service_root)}
                self._send_json(201, create_job_response(request, job_id=job_id))
                return

            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "cancel":
                self._send_json(200, cancel_job_response(service_root=self._service_root, job_id=segments[2]))
                return
            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "run":
                self._send_json(200, run_job_response(service_root=self._service_root, job_id=segments[2]))
                return
            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "start":
                self._send_json(202, start_job_response(service_root=self._service_root, job_id=segments[2]))
                return
            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "retry":
                self._send_json(200, retry_job_response(service_root=self._service_root, job_id=segments[2]))
                return
            raise ServiceHttpError(404, "not_found", "Endpoint not found.")
        except Exception as exc:  # pragma: no cover - covered through _send_exception branches
            self._send_exception(exc)

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            path = _normalized_path(self.path)
            segments = _path_segments(path)
            if len(segments) == 3 and segments[:2] == ["api", "rule-templates"]:
                payload = self._read_json_body()
                if "service_root" in payload:
                    raise ServiceHttpError(
                        400,
                        "service_root_managed_by_server",
                        "Service root is configured by the server.",
                    )
                self._send_json(
                    200,
                    save_rule_template_response(
                        payload,
                        service_root=self._service_root,
                        template_id=segments[2],
                        replace_existing=True,
                    ),
                )
                return
            raise ServiceHttpError(404, "not_found", "Endpoint not found.")
        except Exception as exc:  # pragma: no cover - covered through _send_exception branches
            self._send_exception(exc)

    @property
    def _service_root(self) -> Path:
        return Path(getattr(self.server, "service_root")).resolve()

    def _read_json_body(self) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ServiceHttpError(400, "invalid_content_length", "Content-Length must be an integer.") from exc
        if length > MAX_JSON_BODY_BYTES:
            raise ServiceHttpError(413, "request_too_large", "JSON request body is too large.")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceHttpError(400, "invalid_json", "Request body must be UTF-8 JSON.") from exc
        if not isinstance(payload, dict):
            raise ServiceHttpError(400, "invalid_json_object", "Request body must be a JSON object.")
        return payload

    def _send_exception(self, exc: Exception) -> None:
        if isinstance(exc, ServiceHttpError):
            self._send_error_json(exc.status, exc.code, exc.message)
            return
        if isinstance(exc, FileExistsError):
            if "rule template" in str(exc).lower():
                self._send_error_json(409, "rule_template_already_exists", "Rule template already exists.")
                return
            self._send_error_json(409, "job_already_exists", "Service job already exists.")
            return
        if isinstance(exc, ServiceJobNotFoundError):
            self._send_error_json(404, "job_not_found", "Service job does not exist.")
            return
        if isinstance(exc, RuleTemplateNotFoundError):
            self._send_error_json(404, "rule_template_not_found", "Rule template does not exist.")
            return
        if isinstance(exc, FileNotFoundError):
            self._send_error_json(400, "input_dir_missing", "Input directory does not exist or is not authorized.")
            return
        if isinstance(exc, ValueError):
            self._send_error_json(400, "invalid_request", "Request failed validation.")
            return
        if isinstance(exc, RuntimeError):
            self._send_error_json(409, "invalid_job_state", "Service job state does not allow this operation.")
            return
        self._send_error_json(500, "internal_error", "Service API request failed.")

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        self._send_json(
            status,
            {
                "schema_version": SERVICE_API_ERROR_SCHEMA_VERSION,
                "status": "error",
                "error": {
                    "code": code,
                    "message": message,
                },
                "public_safe": True,
                "private_paths_exposed": False,
                "privacy": service_api_privacy(),
            },
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_local_preview(self, preview: dict[str, Any]) -> None:
        path = preview.get("path")
        if not isinstance(path, Path):
            raise ServiceHttpError(500, "internal_error", "Local preview could not be resolved.")
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-AI4-Local-Only", "true")
        self.send_header("X-AI4-Preview-Source", str(preview.get("source") or "unknown"))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return


def create_service_http_server(
    *,
    service_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    _require_loopback_host(host)
    server = ThreadingHTTPServer((host, port), ServiceApiRequestHandler)
    server.service_root = service_root.resolve()  # type: ignore[attr-defined]
    return server


def serve_service_api(*, service_root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_service_http_server(service_root=service_root, host=host, port=port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _required_query_arg(raw_path: str, key: str) -> str:
    values = parse_qs(urlsplit(raw_path).query, keep_blank_values=False).get(key)
    if not values or not values[-1]:
        raise ServiceHttpError(400, "missing_request_field", f"Missing query field: {key}.")
    return values[-1]


def _optional_query_arg(raw_path: str, key: str) -> str | None:
    values = parse_qs(urlsplit(raw_path).query, keep_blank_values=False).get(key)
    return values[-1] if values and values[-1] else None


def _required_payload_arg(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ServiceHttpError(400, "missing_request_field", f"Missing request field: {key}.")
    return value


def _normalized_path(raw_path: str) -> str:
    path = urlsplit(raw_path).path
    if path != "/":
        path = path.rstrip("/")
    return path or "/"


def _path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _require_loopback_host(host: str) -> None:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return
    try:
        if ip_address(normalized).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("Service API host must be a loopback address.")
