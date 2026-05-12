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
COMPLETION_NOTE_TXT = "本批次完成交接说明.txt"
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

    def preview_path(self, local_id: str) -> tuple[Path, str]:
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
        for candidate, source in _preview_candidates(input_dir, derivatives_dir, relative_path):
            if _valid_preview_path(candidate, input_dir, derivatives_dir):
                return candidate.expanduser().resolve(), source
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
        completion_note_path = metadata_dir / COMPLETION_NOTE_TXT
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        verification_path.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        decision_summary = verification.get("decision_summary") if isinstance(verification.get("decision_summary"), dict) else {}
        total_decisions = int(decision_summary.get("total_decisions") or 0)
        pending_decisions = int(decision_summary.get("pending") or 0)
        reviewed_decisions = max(0, total_decisions - pending_decisions)
        completion_note_path.write_text(
            "\n".join(
                [
                    "本批次完成交接说明",
                    f"处理后图片文件夹：{derivatives_dir}",
                    f"复核结果保存位置：{summary_path}",
                    f"复核校验保存位置：{verification_path}",
                    f"本机状态文件夹：{metadata_dir}",
                    f"复核总数：{total_decisions}",
                    f"已确认：{reviewed_decisions}",
                    f"待决定：{pending_decisions}",
                    "交接事项：把处理后图片交给验收或移交流程。",
                    "下一批：在工作台点击准备下一批，重新选择扫描原图文件夹和处理后输出文件夹。",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return {
            "schema_version": SERVER_SCHEMA,
            "finished": True,
            "message_zh": "完成并导出结果：处理后图片和复核结果已保存。",
            "folders": {
                "derivatives": str(derivatives_dir),
                "metadata": str(metadata_dir),
            },
            "saved": {
                "decision_summary": str(summary_path),
                "verification_summary": str(verification_path),
                "completion_note": str(completion_note_path),
            },
            "completion_panel": {
                "title_zh": "完成并导出结果",
                "message_zh": "本批次已完成。处理后图片在输出文件夹，复核结果已保存到本机状态文件夹。",
                "total_review_items": total_decisions,
                "reviewed_items": reviewed_decisions,
                "pending_items": pending_decisions,
                "derivatives_dir": str(derivatives_dir),
                "metadata_dir": str(metadata_dir),
                "decision_summary_path": str(summary_path),
                "verification_summary_path": str(verification_path),
                "completion_note_path": str(completion_note_path),
                "next_steps_zh": [
                    "到处理后输出文件夹检查图片数量和文件是否齐全。",
                    "把处理后图片交给验收或移交流程。",
                    "点击准备下一批，重新选择新的扫描原图文件夹和输出文件夹。",
                ],
            },
            "decision_summary": decision_summary,
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
        queue = self._queue_with_preview_sources(Path(metadata_dir)) if metadata_dir else None
        draft_decisions = _read_json(Path(metadata_dir) / REVIEW_DECISION_DRAFT_JSON) if metadata_dir else None
        recovery_guidance = _status_recovery_guidance(
            configured=bool(input_dir and derivatives_dir and metadata_dir),
            running=running,
            summary=summary,
            progress=progress,
            last_error=last_error,
        )
        return {
            "schema_version": SERVER_SCHEMA,
            "running": running,
            "configured": bool(input_dir and derivatives_dir and metadata_dir),
            "last_error_zh": last_error,
            "recovery_guidance": recovery_guidance,
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

    def _queue_with_preview_sources(self, metadata_dir: Path) -> dict[str, Any] | None:
        queue = _read_json(metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON)
        if not isinstance(queue, dict):
            return None
        with self._lock:
            input_dir = self.input_dir
            derivatives_dir = self.derivatives_dir
        items = queue.get("items")
        if input_dir is None or derivatives_dir is None or not isinstance(items, list):
            return queue
        enriched = dict(queue)
        enriched_items = []
        for item in items:
            if not isinstance(item, dict):
                enriched_items.append(item)
                continue
            enriched_item = dict(item)
            enriched_item["preview_source"] = _preview_source_for_item(item, input_dir, derivatives_dir)
            enriched_items.append(enriched_item)
        enriched["items"] = enriched_items
        return enriched

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
    parser.add_argument("--input-dir", default=None, type=Path, help="预先填写扫描原图文件夹。")
    parser.add_argument("--derivatives-dir", default=None, type=Path, help="预先填写处理后输出文件夹。")
    parser.add_argument("--metadata-dir", default=None, type=Path, help="预先填写本机状态文件夹；默认使用输出文件夹下的状态文件夹。")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("production-workbench is local-only; use 127.0.0.1, localhost, or ::1.")
    if bool(args.input_dir) != bool(args.derivatives_dir):
        parser.error("请同时提供 --input-dir 和 --derivatives-dir，或都不提供。")
    try:
        server = make_server(
            args.host,
            args.port,
            input_dir=args.input_dir,
            derivatives_dir=args.derivatives_dir,
            metadata_dir=args.metadata_dir,
        )
    except ValueError as exc:
        parser.error(str(exc))
    host_for_url = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{host_for_url}:{server.server_port}/"
    print(f"本地生产工作台: {url}")
    if args.input_dir and args.derivatives_dir:
        print("已预先填写演练文件夹，打开后可直接查看批次状态。")
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


def make_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    input_dir: Path | None = None,
    derivatives_dir: Path | None = None,
    metadata_dir: Path | None = None,
) -> ThreadingHTTPServer:
    controller = WorkbenchController()
    if input_dir is not None or derivatives_dir is not None or metadata_dir is not None:
        if input_dir is None or derivatives_dir is None:
            raise ValueError("请同时提供扫描原图文件夹和处理后输出文件夹。")
        controller.configure(input_dir, derivatives_dir, metadata_dir)

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
            path, source = self.workbench_controller.preview_path(local_id)
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
        self.send_header("X-Preview-Source", source)
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


def _status_recovery_guidance(
    *,
    configured: bool,
    running: bool,
    summary: dict[str, Any] | None,
    progress: dict[str, Any] | None,
    last_error: str | None,
) -> dict[str, Any]:
    base = {
        "schema_version": "scan-qc.local-recovery-guidance.v1",
        "aggregate_only": True,
        "failed_files": 0,
        "retryable_files": 0,
        "derivative_images_ready": 0,
        "total_files": 0,
    }
    if not configured:
        return {
            **base,
            "kind": "folder_setup_missing",
            "title_zh": "文件夹还没有准备好",
            "message_zh": "请先填写扫描原图文件夹和处理后输出文件夹。",
            "next_steps_zh": [
                "确认扫描原图文件夹存在，里面是本批次原图。",
                "确认处理后输出文件夹可以写入，磁盘空间足够。",
                "保存文件夹后再开始处理。",
            ],
        }
    if last_error:
        return {
            **base,
            "kind": "processing_failed_admin",
            "title_zh": "本机处理启动失败",
            "message_zh": "处理没有正常完成，请检查文件夹、磁盘空间和图片是否能打开。",
            "next_steps_zh": [
                "确认两个文件夹位置没有填错。",
                "确认磁盘空间足够，图片文件可以正常打开。",
                "重新开始处理；如果仍失败，请交管理员查看本机状态文件夹。",
            ],
        }
    if isinstance(summary, dict):
        guidance = summary.get("recovery_guidance")
        if isinstance(guidance, dict):
            return guidance
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        failed_files = int(counts.get("failed_files") or 0)
        retryable_files = int(counts.get("retry_list_files") or 0)
        derivative_images_ready = int(counts.get("processed_files") or 0) + int(counts.get("resumed_files") or 0)
        total_files = int(counts.get("total_files") or 0)
        aggregate = {
            **base,
            "failed_files": failed_files,
            "retryable_files": retryable_files,
            "derivative_images_ready": derivative_images_ready,
            "total_files": total_files,
        }
        openable_files = int(counts.get("openable_files") or 0)
        if total_files == 0:
            return {
                **aggregate,
                "kind": "empty_input_folder",
                "title_zh": "原图文件夹是空的",
                "message_zh": "这个扫描原图文件夹里没有发现可处理文件。",
                "next_steps_zh": [
                    "确认是否选到了本批次真正的扫描原图文件夹。",
                    "如果还没有扫描图片，请先完成扫描或把图片放入原图文件夹。",
                    "放好图片后，重新保存文件夹并开始处理。",
                ],
            }
        if summary.get("status") == "blocked" or failed_files:
            return {
                **aggregate,
                "kind": "processing_failed_retryable" if retryable_files else "processing_failed_admin",
                "title_zh": "处理没有全部完成",
                "message_zh": "有文件处理失败。请检查文件夹、磁盘空间和图片是否能打开。",
                "next_steps_zh": [
                    "确认扫描原图文件夹和处理后输出文件夹选对。",
                    "检查磁盘空间是否足够，原图是否能正常打开。",
                    "重新开始处理；如果仍失败，请交管理员查看本机状态文件夹。",
                ],
            }
        if openable_files == 0:
            return {
                **aggregate,
                "kind": "no_supported_images",
                "title_zh": "没有可处理的图片",
                "message_zh": "文件夹里没有找到当前支持处理的图片，或图片无法正常打开。",
                "next_steps_zh": [
                    "确认选对了扫描原图文件夹。",
                    "确认原图是常见图片格式，并且能用本机图片查看器打开。",
                    "如果文件格式不对，请重新导出为支持的图片格式后再处理。",
                ],
            }
        if summary.get("status") == "finished":
            return {
                **aggregate,
                "kind": "no_remaining_work",
                "title_zh": "没有剩余处理任务",
                "message_zh": "本批次没有需要人工确认的图片，处理后图片已经准备好。",
                "next_steps_zh": [
                    "确认处理后图片数量正常。",
                    "把处理后图片交给验收或移交流程。",
                    "点击准备下一批，重新选择新的扫描原图文件夹和输出文件夹。",
                ],
            }
    state = progress.get("state") if isinstance(progress, dict) else None
    return {
        **base,
        "kind": "processing_running" if running or state == "running" else "ready_to_start",
        "title_zh": "正在处理" if running or state == "running" else "可以开始处理",
        "message_zh": "本机正在生成处理后图片，请稍候。" if running or state == "running" else "文件夹已保存，可以开始处理。",
        "next_steps_zh": ["等待处理完成后查看结果。"] if running or state == "running" else ["点击开始处理。"],
    }


def _preview_candidates(input_dir: Path, derivatives_dir: Path, relative_path: Path) -> list[tuple[Path, str]]:
    return [
        (derivatives_dir / "images" / relative_path, "processed"),
        (derivatives_dir / relative_path, "processed"),
        (input_dir / relative_path, "original_fallback"),
    ]


def _valid_preview_path(candidate: Path, input_dir: Path, derivatives_dir: Path) -> bool:
    resolved = candidate.expanduser().resolve()
    return resolved.suffix.lower() in PREVIEW_IMAGE_SUFFIXES and resolved.is_file() and (
        _is_relative_to(resolved, input_dir) or _is_relative_to(resolved, derivatives_dir)
    )


def _preview_source_for_item(item: dict[str, Any], input_dir: Path, derivatives_dir: Path) -> str:
    try:
        relative_path = _safe_relative_path(str(item.get("relative_path") or ""))
    except ValueError:
        return "unavailable"
    for candidate, source in _preview_candidates(input_dir, derivatives_dir, relative_path):
        if _valid_preview_path(candidate, input_dir, derivatives_dir):
            return source
    return "unavailable"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def _is_loopback_client(host: str) -> bool:
    return host in {"127.0.0.1", "::1"} or host.startswith("127.")
