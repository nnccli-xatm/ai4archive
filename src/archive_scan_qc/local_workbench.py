"""Loopback-only operator workbench server for local production runs."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
from typing import Any
from urllib.parse import unquote, urlparse
import webbrowser

from .processing_review import REVIEW_JSON as PROCESSING_REVIEW_JSON, write_processing_review_package
from .production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON, write_production_review_queue
from .production_runner import (
    PRODUCTION_RUN_PROGRESS_JSON,
    PRODUCTION_RUN_SUMMARY_JSON,
    ProductionRunConfig,
    run_production_folder,
)
from .review_decisions import (
    REVIEW_DECISION_VERIFICATION_JSON,
    build_review_decision_verification_summary,
)


ROOT = Path(__file__).resolve().parents[2]
WORKBENCH_HTML = ROOT / "docs" / "production-workbench-prototype.html"
DOCS_DIR = ROOT / "docs"
DEFAULT_METADATA_DIRNAME = "_production_workbench"
SERVER_SCHEMA = "scan-qc.local-production-workbench.v1"
REVIEW_DECISION_SUMMARY_JSON = "scan-qc-review-decisions.summary.json"
REVIEW_DECISION_DRAFT_JSON = "scan-qc-review-decisions.draft.json"
PREVIEW_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


class WorkbenchController:
    """Small state holder shared by HTTP requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.input_dir: Path | None = None
        self.derivatives_dir: Path | None = None
        self.metadata_dir: Path | None = None
        self.last_error: str | None = None

    def configure(self, input_dir: Path, derivatives_dir: Path, metadata_dir: Path | None = None) -> dict[str, Any]:
        if str(input_dir).strip() in {"", "."}:
            raise ValueError("请填写扫描原图文件夹。")
        if str(derivatives_dir).strip() in {"", "."}:
            raise ValueError("请填写处理后输出文件夹。")
        if metadata_dir is not None and str(metadata_dir).strip() in {"", "."}:
            raise ValueError("请填写本机状态文件夹，或留空使用默认位置。")
        input_path = input_dir.expanduser().resolve()
        output_path = derivatives_dir.expanduser().resolve()
        metadata_path = (metadata_dir.expanduser().resolve() if metadata_dir else output_path / DEFAULT_METADATA_DIRNAME)
        if not input_path.exists() or not input_path.is_dir():
            raise ValueError("扫描原图文件夹不存在。")
        output_path.mkdir(parents=True, exist_ok=True)
        metadata_path.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.input_dir = input_path
            self.derivatives_dir = output_path
            self.metadata_dir = metadata_path
            self.last_error = None
        return self.status()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("当前批次正在处理。")
            if self.input_dir is None or self.derivatives_dir is None or self.metadata_dir is None:
                raise ValueError("请先填写并保存两个文件夹位置。")
            self.last_error = None
            self._thread = threading.Thread(target=self._run_once, name="production-workbench-run", daemon=True)
            self._thread.start()
        return self.status()

    def preview_path(self, local_id: str) -> Path:
        safe_id = local_id.strip()
        if not safe_id:
            raise ValueError("预览请求缺少复核编号。")
        with self._lock:
            input_dir = self.input_dir
            derivatives_dir = self.derivatives_dir
            metadata_dir = self.metadata_dir
        if input_dir is None or derivatives_dir is None or metadata_dir is None:
            raise ValueError("请先保存文件夹并生成复核队列。")
        queue = _read_json(metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON)
        items = queue.get("items") if isinstance(queue, dict) else None
        if not isinstance(items, list):
            raise ValueError("尚未生成可预览的复核队列。")
        item = next((entry for entry in items if isinstance(entry, dict) and entry.get("local_id") == safe_id), None)
        if not item:
            raise ValueError("未找到这条复核记录。")
        relative_path = _safe_relative_path(str(item.get("relative_path") or ""))
        candidates = [
            derivatives_dir / "images" / relative_path,
            derivatives_dir / relative_path,
            input_dir / relative_path,
        ]
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if resolved.suffix.lower() in PREVIEW_IMAGE_SUFFIXES and resolved.is_file() and (
                _is_relative_to(resolved, input_dir) or _is_relative_to(resolved, derivatives_dir)
            ):
                return resolved
        raise ValueError("未找到这条复核记录对应的本机预览图。")

    def save_review_decisions(self, summary: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            derivatives_dir = self.derivatives_dir
            metadata_dir = self.metadata_dir
        if derivatives_dir is None or metadata_dir is None:
            raise ValueError("请先保存文件夹并生成复核队列。")
        verification = build_review_decision_verification_summary(summary)
        if verification.get("status") != "pass":
            raise ValueError("复核决定还不能完成，请检查是否还有待处理图片。")

        metadata_dir.mkdir(parents=True, exist_ok=True)
        summary_path = metadata_dir / REVIEW_DECISION_SUMMARY_JSON
        verification_path = metadata_dir / REVIEW_DECISION_VERIFICATION_JSON
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        verification_path.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "schema_version": SERVER_SCHEMA,
            "finished": True,
            "message_zh": "批次已完成，复核决定已保存。",
            "folders": {
                "derivatives": str(derivatives_dir),
                "metadata": str(metadata_dir),
            },
            "saved": {
                "decision_summary": str(summary_path),
                "verification_summary": str(verification_path),
            },
            "decision_summary": verification.get("decision_summary"),
        }

    def save_draft_review_decisions(self, summary: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            metadata_dir = self.metadata_dir
        if metadata_dir is None:
            raise ValueError("请先保存文件夹并生成复核队列。")
        verification = build_review_decision_verification_summary(summary)
        if verification.get("status") != "pass":
            raise ValueError("复核进度暂不能保存，请重新选择。")

        metadata_dir.mkdir(parents=True, exist_ok=True)
        draft_path = metadata_dir / REVIEW_DECISION_DRAFT_JSON
        draft_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {
            "schema_version": SERVER_SCHEMA,
            "saved": True,
            "message_zh": "已自动保存",
            "draft_decisions": summary,
            "decision_summary": verification.get("decision_summary"),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            input_dir = str(self.input_dir) if self.input_dir else None
            derivatives_dir = str(self.derivatives_dir) if self.derivatives_dir else None
            metadata_dir = str(self.metadata_dir) if self.metadata_dir else None
            last_error = self.last_error
        summary = _read_json(Path(metadata_dir) / PRODUCTION_RUN_SUMMARY_JSON) if metadata_dir else None
        progress = _read_json(Path(metadata_dir) / PRODUCTION_RUN_PROGRESS_JSON) if metadata_dir else None
        queue = _read_json(Path(metadata_dir) / PRODUCTION_REVIEW_QUEUE_JSON) if metadata_dir else None
        draft_decisions = _read_json(Path(metadata_dir) / REVIEW_DECISION_DRAFT_JSON) if metadata_dir else None
        return {
            "schema_version": SERVER_SCHEMA,
            "running": running,
            "configured": bool(input_dir and derivatives_dir and metadata_dir),
            "last_error_zh": last_error,
            "folders": {
                "input": input_dir,
                "derivatives": derivatives_dir,
                "metadata": metadata_dir,
            },
            "summary": summary,
            "progress": progress,
            "queue": queue,
            "draft_decisions": draft_decisions,
        }

    def _run_once(self) -> None:
        try:
            with self._lock:
                assert self.input_dir is not None
                assert self.derivatives_dir is not None
                assert self.metadata_dir is not None
                config = ProductionRunConfig(
                    input_dir=self.input_dir,
                    derivative_output_dir=self.derivatives_dir,
                    metadata_output_dir=self.metadata_dir,
                    auto_crop=True,
                    deskew=True,
                    resume_processing=True,
                    reuse_scan_measurements=True,
                )
            summary = run_production_folder(config)
            self._write_review_queue(summary)
        except Exception as exc:  # pragma: no cover - exercised through status in integration use.
            with self._lock:
                self.last_error = f"本机处理失败：{exc}"

    def _write_review_queue(self, summary: dict[str, Any]) -> None:
        with self._lock:
            metadata_dir = self.metadata_dir
            derivatives_dir = self.derivatives_dir
        if metadata_dir is None or derivatives_dir is None:
            return
        artifacts = summary.get("artifacts") if isinstance(summary, dict) else {}
        scan_report = (
            Path(str(artifacts.get("admin_scan_report")))
            if isinstance(artifacts, dict) and artifacts.get("admin_scan_report")
            else None
        )
        processing_manifest = derivatives_dir / "processing_manifest.json"
        processing_package = metadata_dir / PROCESSING_REVIEW_JSON
        if processing_manifest.exists():
            write_processing_review_package(processing_manifest, metadata_dir)
        write_production_review_queue(
            metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON,
            scan_qc_report_path=scan_report if scan_report and scan_report.exists() else None,
            processing_review_package_path=processing_package if processing_package.exists() else None,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc production-workbench",
        description="在本机回环地址启动中文生产工作台，并协调本地处理产物。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="只建议使用 127.0.0.1。")
    parser.add_argument("--port", default=8765, type=int, help="本机端口。")
    parser.add_argument("--no-open", action="store_true", help="只启动服务，不自动打开浏览器。")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("production-workbench is local-only; use 127.0.0.1, localhost, or ::1.")
    server = make_server(args.host, args.port)
    host_for_url = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{host_for_url}:{server.server_port}/"
    print(f"本地生产工作台: {url}")
    print("按 Ctrl+C 停止。")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止本地生产工作台。")
    finally:
        server.server_close()
    return 0


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    controller = WorkbenchController()

    class Handler(WorkbenchRequestHandler):
        workbench_controller = controller

    return ThreadingHTTPServer((host, port), Handler)


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    workbench_controller: WorkbenchController

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        parsed = urlparse(self.path)
        if not _is_loopback_client(self.client_address[0]):
            self._send_json({"error_zh": "本机预览只允许回环地址访问。"}, HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/api/status":
            self._send_json(self.workbench_controller.status())
            return
        if parsed.path.startswith("/api/preview/"):
            self._serve_preview(unquote(parsed.path.removeprefix("/api/preview/")))
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        try:
            payload = self._read_payload()
            if self.path == "/api/configure":
                result = self.workbench_controller.configure(
                    _required_path(payload, "input_dir", "扫描原图文件夹"),
                    _required_path(payload, "derivatives_dir", "处理后输出文件夹"),
                    _optional_path(payload, "metadata_dir", "本机状态文件夹"),
                )
            elif self.path == "/api/start":
                result = self.workbench_controller.start()
            elif self.path == "/api/finish-decisions":
                result = self.workbench_controller.save_review_decisions(payload)
            elif self.path == "/api/save-draft-decisions":
                result = self.workbench_controller.save_draft_review_decisions(payload)
            else:
                self._send_json({"error_zh": "未知请求。"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(result)
        except ValueError as exc:
            self._send_json({"error_zh": str(exc)}, HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self._send_json({"error_zh": "请求内容无法读取。"}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _serve_static(self, raw_path: str) -> None:
        if raw_path in {"", "/"}:
            path = WORKBENCH_HTML
        else:
            candidate = (DOCS_DIR / unquote(raw_path.lstrip("/"))).resolve()
            if DOCS_DIR.resolve() not in candidate.parents and candidate != DOCS_DIR.resolve():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path = candidate
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8" if path.suffix == ".html" else "application/json; charset=utf-8"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_preview(self, local_id: str) -> None:
        try:
            path = self.workbench_controller.preview_path(local_id)
        except ValueError as exc:
            self._send_json({"error_zh": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容格式不正确。")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _required_path(payload: dict[str, Any], key: str, label_zh: str) -> Path:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"请填写{label_zh}。")
    return Path(value.strip())


def _optional_path(payload: dict[str, Any], key: str, label_zh: str) -> Path | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"请填写{label_zh}，或留空使用默认位置。")
    return Path(value.strip())


def _safe_relative_path(value: str) -> Path:
    stripped = value.strip()
    candidate = Path(stripped)
    if not stripped or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("复核记录预览路径不安全。")
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def _is_loopback_client(host: str) -> bool:
    return host in {"127.0.0.1", "::1"} or host.startswith("127.")
