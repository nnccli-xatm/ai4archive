"""Validate the ordinary-user Chinese production workbench prototype."""

from __future__ import annotations

import json
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archive_scan_qc.review_decisions import build_review_decision_verification_summary
from archive_scan_qc.local_workbench import (
    COMPLETION_NOTE_TXT,
    PRODUCTION_RUN_SUMMARY_JSON,
    REVIEW_DECISION_SUMMARY_JSON,
    WorkbenchController,
)
from archive_scan_qc.review_decisions import REVIEW_DECISION_VERIFICATION_JSON

WORKBENCH = ROOT / "docs" / "production-workbench-prototype.html"
CLI = ROOT / "src" / "archive_scan_qc" / "cli.py"
LOCAL_WORKBENCH = ROOT / "src" / "archive_scan_qc" / "local_workbench.py"
FIXTURE_ROOT = ROOT / "docs" / "fixtures"
FIXTURE_STATES = {
    "production-run-running": "running",
    "production-run-needs-review": "needs_review",
    "production-run-finished": "finished",
    "production-run-retryable": "blocked",
    "production-run-blocked": "blocked",
    "production-run-empty": "finished",
}

REQUIRED_TEXT = {
    "本地生产工作台",
    "管理维护工作台",
    "原图文件夹",
    "输出文件夹",
    "请填写原图文件夹",
    "请填写输出文件夹",
    "开始处理",
    "本批预检结果",
    "可处理图片数量",
    "批次正在运行，请等待",
    "阶段：",
    "检查扫描图片",
    "已处理",
    "待处理",
    "聚合处理速度",
    "张/分钟",
    "预计还需等待",
    "暂不能估算剩余时间，继续等待处理进度更新",
    "生成处理后图片",
    "整理处理结果",
    "正在统计图片数量",
    "不能更改文件夹和处理方式",
    "处理中，请等待",
    "批次正在运行，不能重复开始处理。",
    "失败",
    "正在检查文件夹",
    "正在读取图片",
    "正在检查质量",
    "正在生成处理后图片",
    "等待人工确认",
    "可以完成",
    "可以开始处理",
    "正在处理",
    "有图片需要人工确认",
    "没有需要人工确认",
    "待完成",
    "批次已完成",
    "处理失败",
    "确认通过",
    "退回重扫",
    "重新处理图片",
    "确认保留原貌",
    "大图查看",
    "当前图片",
    "当前第",
    "已确认",
    "待确认",
    "图片查看",
    "处理后图片不可用，正在显示原图",
    "正在对比原图和处理后图片",
    "对比查看",
    "查看原图",
    "查看处理后图片",
    "当前查看",
    "正在查看原图",
    "正在查看处理后图片",
    "本张没有原图，可查看处理后图片",
    "本张没有处理后图片，可查看原图",
    "本张暂时没有可查看图片",
    "处理后图片",
    "原图",
    "问题原因",
    "重点查看",
    "等待处理开始后显示查看重点",
    "系统建议",
    "怎么选",
    "图片可以继续使用",
    "点击后会保存决定并看下一张",
    "交回扫描工位补扫",
    "重新生成处理后图片",
    "不让自动优化覆盖原貌",
    "选择任一决定后，会保存当前图片的决定和备注",
    "当前决定",
    "建议：重新处理图片",
    "建议：退回重扫",
    "建议：确认保留原貌",
    "建议：确认通过",
    "另存复核结果",
    "完成并导出结果",
    "确认完成本批",
    "暂不能完成本批",
    "完成前还要等待",
    "还有待确认项",
    "返回看图确认",
    "输出图片数量不足",
    "继续等待",
    "重试本批次",
    "返回继续检查",
    "请确认本批已经检查完",
    "所有待确认图片都已确认",
    "还需确认",
    "上一张已确认图片",
    "下一张待确认图片",
    "张图片没有决定，暂不能完成",
    "本批没有需要人工确认的图片",
    "保存位置",
    "接下来",
    "处理后图片已保存到输出文件夹",
    "复核结果和交接说明已保存到本机状态文件夹",
    "处理后图片已准备好",
    "本批次是否完成",
    "人工处理",
    "交管理员处理",
    "查看处理后图片",
    "如果仍有异常或不能交接，请交管理员处理",
    "需要继续加工时",
    "准备下一批",
    "待决定",
    "记录当前图片",
    "已记录",
    "自动显示下一张待确认图片",
    "撤销当前决定",
    "请重新选择",
    "复核人员",
    "填写姓名或工号",
    "本张备注",
    "可填写中文备注，留空也可以",
    "公开安全示意图",
    "本机真实图片",
    "本批次交接结果",
    "原图不会被覆盖",
    "不读取目录内容",
    "不执行处理",
    "不显示本机私有路径",
    "本机处理状态",
    "已自动保存",
    "已恢复上次进度",
    "保存文件夹",
    "填写原图文件夹",
    "填写输出文件夹",
    "从系统选择原图文件夹",
    "从系统选择输出文件夹",
    "处理方式",
    "标准优化",
    "只质检不修图",
    "轻度优化",
    "推荐用于正常批量生产",
    "兼顾批量图片质量和处理效率",
    "用于担心过度处理的批次",
    "只做质量检查",
    "不会生成处理后优化图片",
    "输出结果",
    "当前处理方式：标准优化",
    "本机入口",
    "已预先填写演练文件夹",
    "维护入口",
    "选择维护示例",
    "这不是正常加工步骤",
    "填写本机真实文件夹位置",
    "请先填写原图文件夹和输出文件夹",
    "需留意文件",
    "原图总数",
    "需要管理员处理",
    "文件夹还没有准备好",
    "处理没有全部完成",
    "本批次有图片没有处理完，可以先检查文件夹后重试本批次。",
    "本批次没有处理完，当前不能直接重试。",
    "没有剩余处理任务",
    "磁盘空间",
    "可重试",
    "检查扫描原图文件夹和输出文件夹是否选对",
    "重试本批次",
    "请交管理员处理",
    "不要反复点击开始处理",
    "返回重新选择文件夹",
    "原图文件夹是空的",
    "没有可处理的图片",
    "常见图片格式",
    "没有需要人工确认",
    "文件夹位置不对",
    "本批预检未通过",
    "文件夹可以开始处理",
    "可处理图片",
    "输出文件夹可以写入",
    "简短中文状态",
}

FORBIDDEN_VISIBLE_TERMS = {
    "JSON",
    "schema",
    "CLI",
    "hash",
    "OCR",
    "sha256",
    "row-level",
    "private path",
    "raw evidence",
    "模拟失败",
    "production_run_summary",
    "production_run_progress",
}

PRIVATE_FIXTURE_TERMS = {
    "PRIVATE",
    "PUERSAI",
    "relative_path",
    "sha256",
    "hash",
    "OCR",
    "/Users/",
    "\\\\",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
}

PRIVATE_COMPLETION_EXPORT_TERMS = {
    "relative_path",
    "source_path",
    "thumbnail",
    "sha256",
    "hash",
    "OCR",
    "ocr_text",
    "preview_url",
    "original_path",
    "processed_path",
}

CONTRACT_DECISION_MAP = {
    "pass": "false_positive",
    "rescan": "needs_rescan",
    "reprocess": "fixed_externally",
    "keep_original_trace": "false_positive",
}


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0:
            self.parts.append(data)


def visible_text(html: str) -> str:
    parser = VisibleTextParser()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def review_decision_contract_fixture(queue: dict[str, object]) -> dict[str, object]:
    items = queue.get("items")
    if not isinstance(items, list):
        items = []
    operator_decisions = ["pass", "rescan", "reprocess", "keep_original_trace"]
    rows = []
    counts = {
        "pending": 0,
        "accepted_issue": 0,
        "false_positive": 0,
        "fixed_externally": 0,
        "needs_rescan": 0,
        "blocked": 0,
    }
    severities = {"P0": 0, "P1": 0, "P2": 0}
    for index, item in enumerate(items):
        decision = CONTRACT_DECISION_MAP[operator_decisions[index % len(operator_decisions)]]
        counts[decision] += 1
        if isinstance(item, dict) and item.get("severity") in severities:
            severities[str(item["severity"])] += 1
        rows.append(
            {
                "scope": "production_review_queue",
                "local_id": item.get("local_id") if isinstance(item, dict) else f"PRQ{index + 1:06d}",
                "decision": decision,
            }
        )
    return {
        "schema": "scan-qc-review-decisions.local.v1",
        "source_type": "production_workbench",
        "source_target_count": len(rows),
        "generated_in_browser": True,
        "privacy": {"summary_only": True},
        "aggregate_counts": {
            "total_batches": 1,
            "total_findings": len(rows),
            "p0": severities["P0"],
            "p1": severities["P1"],
            "p2": severities["P2"],
            "p0_pending": 0,
            "p1_pending": 0,
            "review_completion": {
                "total": len(rows),
                "reviewed": len(rows),
                "pending": 0,
                "complete": True,
                "counts": counts,
            },
        },
        "review_counts": counts,
        "reviewed_targets": len(rows),
        "decisions": rows,
    }


def js_function_body(html: str, name: str) -> str | None:
    marker = f"function {name}("
    start = html.find(marker)
    if start == -1:
        return None
    body_start = html.find("{", start)
    if body_start == -1:
        return None
    depth = 0
    for index in range(body_start, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[body_start + 1 : index]
    return None


def require_js_tokens(errors: list[str], label: str, body: str | None, tokens: list[str]) -> None:
    if body is None:
        errors.append(f"missing preview fit-control contract function: {label}")
        return
    for token in tokens:
        if token not in body:
            errors.append(f"preview fit-control contract missing token in {label}: {token}")


def validate_preview_fit_controls(html: str, errors: list[str]) -> None:
    """Keep static coverage focused on aggregate preview control behavior."""

    require_js_tokens(
        errors,
        "updatePreviewControls",
        js_function_body(html, "updatePreviewControls"),
        [
            "const disabled = !hasImage;",
            "els.fitPreviewButton.disabled = disabled;",
            "els.zoomOutButton.disabled = disabled || state.previewZoom <= 0.5;",
            "els.zoomInButton.disabled = disabled || state.previewZoom >= 3;",
            'els.zoomState.textContent = "查看：暂无图片";',
            'els.zoomState.textContent = "查看：适合窗口";',
            "els.zoomState.textContent = `查看：${previewZoomPercent()}`;",
        ],
    )
    require_js_tokens(
        errors,
        "applyImageView",
        js_function_body(html, "applyImageView"),
        [
            'const images = Array.from(els.previewFrame.querySelectorAll("img"));',
            "updatePreviewControls(false);",
            'image.classList.toggle("zoomed", !state.previewFit);',
            'image.style.width = state.previewFit ? "100%" : `${Math.round(100 * state.previewZoom)}%`;',
            'image.style.height = state.previewFit ? "100%" : "auto";',
            "updatePreviewControls(true);",
        ],
    )
    require_js_tokens(
        errors,
        "setPreviewFit",
        js_function_body(html, "setPreviewFit"),
        [
            "state.previewFit = true;",
            "state.previewZoom = 1;",
            "applyImageView();",
        ],
    )
    require_js_tokens(
        errors,
        "setPreviewZoom",
        js_function_body(html, "setPreviewZoom"),
        [
            "state.previewFit = false;",
            "state.previewZoom = Math.max(0.5, Math.min(3, Number(nextZoom) || 1));",
            "applyImageView();",
        ],
    )
    require_js_tokens(
        errors,
        "renderPreview",
        js_function_body(html, "renderPreview"),
        [
            'els.previewFrame.classList.toggle("comparison-shell", canCompare && state.comparisonMode === "side_by_side");',
            "applyImageView();",
            "updatePreviewControls(false);",
        ],
    )
    for handler_token in [
        'els.fitPreviewButton.addEventListener("click", setPreviewFit);',
        'els.zoomOutButton.addEventListener("click", () => setPreviewZoom(state.previewZoom - 0.25));',
        'els.zoomInButton.addEventListener("click", () => setPreviewZoom(state.previewZoom + 0.25));',
        'els.resetPreviewButton.addEventListener("click", resetPreviewView);',
    ]:
        if handler_token not in html:
            errors.append(f"preview fit-control contract missing event handler: {handler_token}")
    render_body = js_function_body(html, "renderPreview") or ""
    if render_body.count("applyImageView();") < 2:
        errors.append("preview fit-control contract must reapply image view after single and comparison preview renders")


def validate_preview_visibility_layout(html: str, errors: list[str]) -> None:
    """Guard against comparison preview clipping or overlap regressions."""

    for required_css_token in [
        ".preview-controls {\n      position: relative;\n      z-index: 2;",
        ".preview-zone {\n      display: grid;\n      grid-template-rows: auto 1fr;\n      gap: 12px;\n      align-items: start;\n      justify-items: center;\n      min-height: 620px;",
        "      overflow: auto;\n      background:",
        ".preview-frame {\n      position: relative;\n      z-index: 1;\n      width: min(100%, 520px);\n      min-height: 420px;",
        ".preview-frame.compact {\n      width: 100%;\n      min-height: 340px;",
        ".preview-frame.comparison-shell {\n      width: min(100%, 980px);\n      min-height: 520px;",
        "      .preview-zone {\n        padding: 14px;\n        min-height: 520px;",
        "      .preview-frame {\n        min-height: 360px;",
        "      .preview-frame.compact {\n        min-height: 300px;",
        "      .preview-frame.comparison-shell {\n        min-height: 480px;",
    ]:
        if required_css_token not in html:
            errors.append(f"preview visibility layout contract missing CSS token: {required_css_token}")

    render_body = js_function_body(html, "renderPreview") or ""
    for required_render_token in [
        'els.previewFrame.classList.toggle("comparison-shell", canCompare && state.comparisonMode === "side_by_side");',
        '<div class="preview-comparison" aria-label="原图和处理后图片对比">',
        '<div class="comparison-title">原图</div>',
        '<div class="comparison-title">处理后图片</div>',
        '<div class="preview-fallback">正在对比查看。看完后在右侧选择处理决定。</div>',
    ]:
        if required_render_token not in render_body:
            errors.append(f"preview visibility layout contract missing render token: {required_render_token}")


def validate_completion_export_smoke(html: str, errors: list[str]) -> None:
    """Exercise the local completion export path with synthetic aggregate-only data."""

    for required_token in [
        'const payload = await apiPost("/api/finish-decisions", decisionArtifact());',
        "applyCompletionPanel(payload.completion_panel);",
        'state.status = "complete";',
        "state.progress = 100;",
        'state.operatorMessage = payload.message_zh || "完成并导出结果。";',
        "处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹",
        'id="completionReuseMessage"',
        "function localReuseMessage(panel)",
        "state.completionReuseMessage = localReuseMessage(panel);",
        'els.completionReuseMessage.classList.toggle("hidden", !state.completionReuseMessage);',
        "Number.isFinite(Number(panel.processed_output_images))",
        "state.outputSummary = `已准备 ${state.readyImages} 张处理后图片`;",
        "Number.isFinite(Number(panel.needs_rescan_images))",
        "Number.isFinite(Number(panel.needs_reprocess_images))",
        "if (panel.decision_summary_saved || panel.verification_summary_saved) state.decisionSaveSummary = DECISION_SAVE_LABEL;",
        "if (panel.completion_note_saved) state.completionNoteSummary = COMPLETION_NOTE_LABEL;",
        'id="finishBlocker"',
        "function finishBlockerFromError(error)",
        "blocking_reasons_zh",
        "完成前还要等待",
        "还有待确认项",
        "输出图片数量不足",
        "已经记录的复核决定会保留",
        "当前不会显示为本批已完成",
        "finishBlockerReviewButton",
        "finishBlockerRetryButton",
    ]:
        if required_token not in html:
            errors.append(f"missing completion export smoke token: {required_token}")

    queue = {
        "items": [
            {"local_id": "PRQ-SMOKE-001", "severity": "P1"},
            {"local_id": "PRQ-SMOKE-002", "severity": "P2"},
            {"local_id": "PRQ-SMOKE-003", "severity": "P2"},
        ]
    }
    summary = review_decision_contract_fixture(queue)
    summary["operator_name"] = "复核员"
    summary["operator_decisions"] = [
        {
            "scope": "production_review_queue",
            "local_id": "PRQ-SMOKE-001",
            "decision": "rescan",
            "decided_at": "2026-05-14T03:00:00.000Z",
            "note_zh": "画面需要补扫。",
        },
        {
            "scope": "production_review_queue",
            "local_id": "PRQ-SMOKE-002",
            "decision": "reprocess",
            "decided_at": "2026-05-14T03:01:00.000Z",
            "note_zh": "",
        },
        {
            "scope": "production_review_queue",
            "local_id": "PRQ-SMOKE-003",
            "decision": "pass",
            "decided_at": "2026-05-14T03:02:00.000Z",
            "note_zh": "",
        },
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="production-completion-smoke-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)
            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "operator_summary": {"derivative_images_ready": 7},
                        "counts": {"processed_files": 7, "resumed_files": 0},
                        "local_reuse_summary": {
                            "schema_version": "scan-qc.local-processing-reuse-summary.v1",
                            "aggregate_only": True,
                            "total_files": 8,
                            "reused_files": 2,
                            "reprocessed_files": 5,
                            "failed_files": 0,
                            "remaining_files": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = controller.save_review_decisions(summary)

            expected_message = "本批已完成：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。"
            if result.get("finished") is not True:
                errors.append("completion export smoke did not finish")
            if result.get("message_zh") != expected_message:
                errors.append("completion export smoke returned unexpected final Chinese message")
            decision_summary = result.get("decision_summary")
            if not isinstance(decision_summary, dict) or decision_summary.get("completion_status") != "complete":
                errors.append("completion export smoke did not return complete decision summary")
            elif decision_summary.get("closure_gate_summary", {}).get("can_complete_delivery") is not True:
                errors.append("completion export smoke did not return deliverable closure gate summary")
            panel = result.get("completion_panel")
            if not isinstance(panel, dict):
                errors.append("completion export smoke did not return completion panel")
                return
            expected_panel_values = {
                "title_zh": "本批已完成",
                "message_zh": "处理后图片已准备好。请检查输出文件夹后再交接。",
                "completion_status_zh": "本批已完成",
                "manual_work_zh": "没有待人工处理图片",
                "admin_handoff_zh": "不需要",
            }
            for key, expected in expected_panel_values.items():
                if panel.get(key) != expected:
                    errors.append(f"completion panel has unexpected {key}")
            if panel.get("processed_output_images") != 7:
                errors.append("completion panel missing processed output handoff count")
            if panel.get("total_source_images") != 0:
                errors.append("completion panel should expose aggregate original count with a stable zero fallback")
            if panel.get("needs_rescan_images") != 1:
                errors.append("completion panel missing rescan handoff count")
            if panel.get("needs_reprocess_images") != 1:
                errors.append("completion panel missing reprocess handoff count")
            expected_reuse = {
                "schema_version": "scan-qc.local-processing-reuse-summary.v1",
                "aggregate_only": True,
                "total_files": 8,
                "reused_files": 2,
                "reprocessed_files": 5,
                "failed_files": 0,
                "remaining_files": 0,
                "next_action_zh": "无需整批重跑，检查输出文件夹后交接。",
                "message_zh": "本批共 8 张：已复用 2 张，实际重新处理 5 张，仍失败 0 张，剩余待处理 0 张。无需整批重跑，检查输出文件夹后交接。",
            }
            if panel.get("local_reuse_summary") != expected_reuse:
                errors.append("completion panel missing aggregate local reuse handoff counts")
            for path_name in [REVIEW_DECISION_SUMMARY_JSON, REVIEW_DECISION_VERIFICATION_JSON, COMPLETION_NOTE_TXT]:
                if not (metadata_dir / path_name).exists():
                    errors.append(f"completion export smoke missing local artifact: {path_name}")

            saved_summary = json.loads((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).read_text(encoding="utf-8"))
            saved_summary_text = json.dumps(saved_summary, ensure_ascii=False, sort_keys=True)
            leaked_summary_terms = sorted(
                term for term in PRIVATE_COMPLETION_EXPORT_TERMS if term.lower() in saved_summary_text.lower()
            )
            if leaked_summary_terms:
                errors.append(f"completion decision summary includes forbidden private terms: {leaked_summary_terms}")
            if saved_summary.get("privacy", {}).get("summary_only") is not True:
                errors.append("completion decision summary is not marked summary-only")

            public_panel_text = json.dumps(
                {
                    "title_zh": panel.get("title_zh"),
                    "message_zh": panel.get("message_zh"),
                    "completion_status_zh": panel.get("completion_status_zh"),
                    "manual_work_zh": panel.get("manual_work_zh"),
                    "admin_handoff_zh": panel.get("admin_handoff_zh"),
                    "processed_output_images": panel.get("processed_output_images"),
                    "needs_rescan_images": panel.get("needs_rescan_images"),
                    "needs_reprocess_images": panel.get("needs_reprocess_images"),
                    "local_reuse_summary": panel.get("local_reuse_summary"),
                    "next_steps_zh": panel.get("next_steps_zh"),
                    "checklist_zh": panel.get("checklist_zh"),
                    "processing_mode": panel.get("processing_mode"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for private_value in [
                str(input_dir),
                str(output_dir),
                str(metadata_dir),
                REVIEW_DECISION_SUMMARY_JSON,
                REVIEW_DECISION_VERIFICATION_JSON,
                COMPLETION_NOTE_TXT,
            ]:
                if private_value in public_panel_text:
                    errors.append("completion panel public guidance exposes a local path or artifact filename")
            if any(key in panel for key in ["derivatives_dir", "metadata_dir", "decision_summary_path", "verification_summary_path", "completion_note_path"]):
                errors.append("completion panel exposes local artifact paths instead of aggregate saved flags")
            if panel.get("decision_summary_saved") is not True or panel.get("verification_summary_saved") is not True:
                errors.append("completion panel missing saved verifier flags")
            if panel.get("completion_note_saved") is not True:
                errors.append("completion panel missing saved handoff note flag")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"completion export smoke failed: {exc}")


def main() -> int:
    html = WORKBENCH.read_text(encoding="utf-8")
    text = visible_text(html)
    missing = sorted(item for item in REQUIRED_TEXT if item not in html)
    visible_forbidden = sorted(term for term in FORBIDDEN_VISIBLE_TERMS if term.lower() in text.lower())
    visible_ascii_words = sorted(set(re.findall(r"\b[A-Za-z]{4,}\b", text)))

    errors: list[str] = []
    if missing:
        errors.append(f"missing required Chinese text: {missing}")
    if visible_forbidden:
        errors.append(f"forbidden operator-visible terms: {visible_forbidden}")
    allowed_ascii: set[str] = set()
    unexpected_ascii = [word for word in visible_ascii_words if word not in allowed_ascii]
    if unexpected_ascii:
        errors.append(f"unexpected visible ASCII words: {unexpected_ascii}")
    if 'lang="zh-CN"' not in html:
        errors.append("missing zh-CN document language")
    for forbidden_upload_token in [
        'type="file"',
        "webkitdirectory",
        "directory multiple",
        "inputFolder",
        "outputFolder",
        "summaryFile",
        "浏览器辅助确认",
        "选择本机状态",
        "上传",
    ]:
        if forbidden_upload_token in html:
            errors.append(f"operator workbench still exposes upload/file-picker flow: {forbidden_upload_token}")
    if "applyRunStatus" not in html:
        errors.append("missing production-run status loader")
    if "operator_summary" not in html:
        errors.append("missing operator summary mapping")
    for stage_timing_token in [
        'id="stageTimingText"',
        'id="aggregateProcessingText"',
        'id="stageTimingAdviceText"',
        "function deriveAggregateProcessingLabel(summary, progress)",
        "function aggregateProcessingAvailable(aggregate)",
        "function formatRemainingWait(secondsValue)",
        "aggregate.aggregate_only === true",
        "aggregate.unavailable_reason",
        "聚合处理速度：",
        "预计还需等待",
        "少于 1 分钟",
        "暂不能估算剩余时间，继续等待处理进度更新。",
        'els.aggregateProcessingText.classList.toggle("hidden", !state.aggregateProcessingLabel);',
        "function deriveStageTimingLabel(summary, progress)",
        "function deriveStageTimingAdvice(summary, progress)",
        "source.stage_timings",
        "timings.aggregate_only !== true",
        "elapsed_seconds",
        "主要耗时在生成处理后图片，请继续等待；如长时间没有变化再交管理员处理。",
        '["completed", "finished", "running"].includes(stage.status)',
        'els.stageTimingText.classList.toggle("hidden", !state.stageTimingLabel);',
        'els.stageTimingAdviceText.classList.toggle("hidden", !state.stageTimingAdvice);',
        "^[\\u4e00-\\u9fff\\s]{2,18}$",
    ]:
        if stage_timing_token not in html:
            errors.append(f"missing aggregate stage timing display token: {stage_timing_token}")
    running_preflight_start = html.find("function runningPreflightSummary(summary)")
    running_preflight_end = html.find("function makeReviewItems(count)", running_preflight_start)
    if running_preflight_start == -1 or running_preflight_end == -1:
        errors.append("missing running preflight plan implementation")
    else:
        running_preflight_body = html[running_preflight_start:running_preflight_end]
        for required_token in [
            "state.folderReadiness && state.folderReadiness.preflight_processing_summary",
            "summary && summary.preflight_processing_summary",
            "preflight.aggregate_only !== true",
            "preflight.retry_scope_safe !== true || rawState === \"unknown\"",
            "Object.prototype.hasOwnProperty.call(preflight, key) && preflight[key] !== null",
            "开始前判断：本批共 ${total} 张，预计可复用处理后输出 ${reusable} 张，预计需要新处理或补处理 ${needsProcessing} 张。",
            "开始前预检摘要暂不能安全用于判断可复用输出。",
            "开始前不能安全判断哪些输出可复用。",
            "当前聚合进度会继续显示已处理、剩余和预计等待",
        ]:
            if required_token not in running_preflight_body:
                errors.append(f"running preflight plan missing token: {required_token}")
        for forbidden_token in [
            "relative_path",
            "source_path",
            "source_sha256",
            "output_sha256",
            "sha256",
            "file_name",
            "filename",
            "ocr_text",
            "OCR",
            "thumbnail",
            "<img",
            "exception",
            "traceback",
            "stack",
        ]:
            if forbidden_token.lower() in running_preflight_body.lower():
                errors.append(f"running preflight plan includes private/debug token: {forbidden_token}")
    for removed_primary_token in [
        'id="pauseButton"',
        'id="resumeButton"',
        "已暂停，可以继续",
        'setStatus("paused")',
        ".status-loader",
    ]:
        if removed_primary_token in html:
            errors.append(f"removed or demoted control still present in primary workbench: {removed_primary_token}")
    for simplified_flow_token in [
        'class="workbench-message" id="loadStatus"',
        '<details class="maintenance-loader">',
        "按下面顺序操作：填原图、填输出、保存文件夹、开始处理。",
        "管理员排查、演练或查看本机状态时使用；这不是正常加工步骤。",
        "请填写本机真实文件夹位置",
    ]:
        if simplified_flow_token not in html:
            errors.append(f"missing simplified operator-flow token: {simplified_flow_token}")
    save_folders_match = re.search(r"async function saveFolders\(\) \{(?P<body>.*?)\n    \}", html, re.S)
    if not save_folders_match:
        errors.append("missing saveFolders implementation")
    else:
        save_folders_body = save_folders_match.group("body")
        for required_token in [
            'input_dir: els.inputPath.value.trim()',
            'derivatives_dir: els.outputPath.value.trim()',
            'processing_mode: selectedProcessingMode()',
            'state.status = "ready";',
            "preflightSummaryMessage(state.folderReadiness)",
        ]:
            if required_token not in save_folders_body:
                errors.append(f"saved-folder configure flow missing token: {required_token}")
        success_copy_index = save_folders_body.find("preflightSummaryMessage(state.folderReadiness)")
        render_after_success_index = save_folders_body.find("render();", success_copy_index)
        if success_copy_index == -1 or render_after_success_index == -1:
            errors.append("saved-folder configure flow does not render after successful save")
    preflight_summary_start = html.find("function preflightSummaryMessage(readiness)")
    preflight_summary_end = html.find("function blockedStartMessage()", preflight_summary_start)
    if preflight_summary_start == -1 or preflight_summary_end == -1:
        errors.append("missing preflight summary message implementation")
    else:
        preflight_summary_body = html[preflight_summary_start:preflight_summary_end]
        for required_token in [
            "function preflightProcessingSummaryMessage(summary)",
            "readiness.preflight_processing_summary",
            "summary.aggregate_only !== true",
            "summary.retry_scope_safe === true",
            "summary.retry_scope_safe === false || rawState === \"unknown\"",
            "本批共 ${total} 张：可复用处理后输出 ${reusable} 张，需要新处理或补处理 ${needsProcessing} 张。",
            "开始前不能安全判断哪些输出可复用。",
            "系统会保守核对并补齐需要处理的输出",
        ]:
            if required_token not in preflight_summary_body:
                errors.append(f"preflight processing summary missing token: {required_token}")
        for forbidden_token in [
            "relative_path",
            "source_sha256",
            "output_sha256",
            "ocr_text",
            "thumbnail",
            "exception",
            "traceback",
            "stack",
        ]:
            if forbidden_token in preflight_summary_body:
                errors.append(f"preflight processing summary includes private/debug token: {forbidden_token}")
    prepare_next_batch_start = html.find("async function prepareNextBatch()")
    prepare_next_batch_end = html.find("els.resetButton.addEventListener", prepare_next_batch_start)
    if prepare_next_batch_start == -1 or prepare_next_batch_end == -1:
        errors.append("missing prepare-next-batch reset flow")
    else:
        prepare_next_batch_body = html[prepare_next_batch_start:prepare_next_batch_end]
        for required_token in [
            'state.status = "choose_input";',
            "state.readyImages = 0;",
            "state.totalImages = 0;",
            "state.attentionFiles = 0;",
            "state.reviewItems = [];",
            "state.queueLoaded = false;",
            "state.decisions = {};",
            "state.finishConfirmationVisible = false;",
            "state.recoveryGuidance = null;",
            "state.folderReadiness = null;",
            'state.completionTitle = "等待完成本批";',
            'state.completionMessage = "完成并导出结果后，这里会显示本批交接清单。";',
            'state.completionStatusFact = "未完成";',
            "state.completionSteps = INITIAL_COMPLETION_STEPS.slice();",
            "const reusableOutputPath = state.lastServerOutputPath && els.outputPath.value.trim() === state.lastServerOutputPath",
            "state.outputChosen = Boolean(reusableOutputPath);",
            "els.inputPath.value = \"\";",
            "els.outputPath.value = reusableOutputPath;",
            "state.lastServerInputPath = \"\";",
            "state.lastServerOutputPath = reusableOutputPath;",
            "必须重新选择新一批扫描原图文件夹，不要混用批次。",
            "已沿用上次保存的输出文件夹提示；如本批要换位置，请重新选择输出文件夹。",
            "没有可安全沿用的输出文件夹提示，请重新选择输出文件夹。",
            "请重新选择新一批扫描原图文件夹；输出文件夹已保留上次保存的位置提示。",
        ]:
            if required_token not in prepare_next_batch_body:
                errors.append(f"prepare-next-batch reset missing token: {required_token}")
        for forbidden_token in [
            "state.outputChosen = false;",
            "els.outputPath.value = \"\";",
            "state.lastServerOutputPath = \"\";",
            'state.attentionFiles = 3;',
            'state.completionTitle = "本批已完成";',
            'state.completionStatusFact = "本批已完成";',
            "state.completionSteps = DEFAULT_COMPLETION_STEPS.slice();",
        ]:
            if forbidden_token in prepare_next_batch_body:
                errors.append(f"prepare-next-batch reset keeps stale completed batch token: {forbidden_token}")
    for required_reset_prompt_token in [
        "NEXT_BATCH_STATUS_TEXT",
        "请重新选择扫描原图文件夹",
        "输出文件夹可沿用上次保存的位置",
    ]:
        if required_reset_prompt_token not in html:
            errors.append(f"missing next-batch operator prompt token: {required_reset_prompt_token}")
    cli = CLI.read_text(encoding="utf-8")
    local_workbench = LOCAL_WORKBENCH.read_text(encoding="utf-8")
    for required_entrypoint_token in [
        "production-workbench",
        "local_workbench_main",
        "--launch-workbench",
        "--input-dir",
        "--derivatives-dir",
    ]:
        if required_entrypoint_token not in cli:
            errors.append(f"missing local workbench CLI entrypoint token: {required_entrypoint_token}")
    for required_server_token in [
        "ThreadingHTTPServer",
        "127.0.0.1",
        "run_production_folder",
        "write_production_review_queue",
        "/api/start",
        "/api/status",
        "/api/pick-folder",
        "/api/finish-decisions",
        "/api/save-draft-decisions",
        "/api/open-output-folder",
        "/api/preview/",
        "build_review_decision_verification_summary",
        "_required_path",
        "_pick_operator_folder",
        "_pick_native_folder",
        "_open_operator_folder",
        "_batch_has_completed",
        "PREVIEW_IMAGE_SUFFIXES",
        "preview_source",
        "preview_sources",
        "original_fallback",
        "comparison",
        "X-Preview-Source",
        "_is_loopback_client",
        "local-only",
        "已预先填写演练文件夹",
        "metadata_dir",
        "DEFAULT_PROCESSING_MODE",
        "PROCESSING_MODE_OPTIONS",
        "processing_mode",
    ]:
        if required_server_token not in local_workbench:
            errors.append(f"missing local workbench server token: {required_server_token}")
    if 'Path(str(payload.get("input_dir", "")))' in local_workbench or 'Path(str(payload.get("derivatives_dir", "")))' in local_workbench:
        errors.append("configure API converts possibly empty folder value to Path before validation")
    for fixture_name, expected_status in FIXTURE_STATES.items():
        fixture_dir = FIXTURE_ROOT / fixture_name
        summary_path = fixture_dir / "production_run_summary.json"
        progress_path = fixture_dir / "production_run_progress.json"
        queue_path = fixture_dir / "production_review_queue.json"
        if fixture_name not in html:
            errors.append(f"fixture not referenced by workbench: {fixture_name}")
        if not summary_path.exists() or not progress_path.exists():
            errors.append(f"missing production-run fixture pair: {fixture_name}")
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"invalid fixture JSON for {fixture_name}: {exc}")
            continue
        if summary.get("schema_version") != "scan-qc.production-run.v1":
            errors.append(f"unexpected summary schema for {fixture_name}")
        if progress.get("schema_version") != "scan-qc.production-run-progress.v1":
            errors.append(f"unexpected progress schema for {fixture_name}")
        if summary.get("status") != expected_status:
            errors.append(f"unexpected summary status for {fixture_name}: {summary.get('status')}")
        aggregate = progress.get("aggregate_processing") or summary.get("aggregate_processing")
        if fixture_name in {"production-run-running", "production-run-finished"}:
            if not isinstance(aggregate, dict):
                errors.append(f"missing aggregate processing fixture payload: {fixture_name}")
            else:
                for key in [
                    "schema_version",
                    "aggregate_only",
                    "total_images",
                    "processed_images",
                    "remaining_images",
                    "images_per_minute",
                    "estimated_remaining_seconds",
                    "unavailable_reason",
                ]:
                    if key not in aggregate:
                        errors.append(f"missing aggregate processing field for {fixture_name}: {key}")
                if aggregate.get("schema_version") != "scan-qc.aggregate-processing-rate.v1":
                    errors.append(f"unexpected aggregate processing schema for {fixture_name}")
                if aggregate.get("aggregate_only") is not True:
                    errors.append(f"aggregate processing is not aggregate-only for {fixture_name}")
                if fixture_name == "production-run-running":
                    if aggregate.get("remaining_images", 0) <= 0 or aggregate.get("images_per_minute", 0) <= 0:
                        errors.append("running aggregate processing fixture does not prove remaining speed estimate")
                    if aggregate.get("estimated_remaining_seconds", 0) <= 0:
                        errors.append("running aggregate processing fixture does not prove positive wait estimate")
                    preflight = summary.get("preflight_processing_summary")
                    if not isinstance(preflight, dict):
                        errors.append("running fixture missing preflight processing plan summary")
                    else:
                        expected_preflight = {
                            "schema_version": "scan-qc.preflight-processing-summary.v1",
                            "aggregate_only": True,
                            "state": "current_batch",
                            "retry_scope_safe": True,
                            "total_files": 120,
                            "reusable_files": 36,
                            "needs_processing_files": 84,
                        }
                        if preflight != expected_preflight:
                            errors.append("running fixture preflight processing plan summary changed unexpectedly")
                if fixture_name == "production-run-finished":
                    if aggregate.get("remaining_images") != 0 or aggregate.get("estimated_remaining_seconds") != 0.0:
                        errors.append("finished aggregate processing fixture should have no remaining wait")
        operator = summary.get("operator_summary")
        if not isinstance(operator, dict):
            errors.append(f"missing operator summary for {fixture_name}")
            continue
        for key in [
            "message",
            "message_zh",
            "total_source_images",
            "openable_source_images",
            "derivative_images_ready",
            "files_needing_attention",
        ]:
            if key not in operator:
                errors.append(f"missing operator field for {fixture_name}: {key}")
        guidance = summary.get("recovery_guidance")
        if expected_status in {"blocked", "finished"} and not isinstance(guidance, dict):
            errors.append(f"missing aggregate recovery guidance for {fixture_name}")
        if isinstance(guidance, dict):
            if guidance.get("aggregate_only") is not True:
                errors.append(f"recovery guidance is not aggregate-only for {fixture_name}")
            for key in ["kind", "title_zh", "message_zh", "next_steps_zh", "failed_files", "derivative_images_ready"]:
                if key not in guidance:
                    errors.append(f"missing recovery guidance field for {fixture_name}: {key}")
            if not re.search(r"[\u4e00-\u9fff]", str(guidance.get("message_zh", ""))):
                errors.append(f"recovery guidance message is not Chinese for {fixture_name}")
        for demo_only_key in ["output_folder_summary", "review_queue_state"]:
            if demo_only_key in operator:
                errors.append(f"fixture uses non-production operator field for {fixture_name}: {demo_only_key}")
        if "completed_items" in progress or "total_items" in progress:
            errors.append(f"progress fixture uses non-production top-level item counts: {fixture_name}")
        raw_fixture = summary_path.read_text(encoding="utf-8") + progress_path.read_text(encoding="utf-8")
        leaked_terms = sorted(term for term in PRIVATE_FIXTURE_TERMS if term.lower() in raw_fixture.lower())
        if leaked_terms:
            errors.append(f"private or row-level fixture terms in {fixture_name}: {leaked_terms}")
        if expected_status == "needs_review":
            if not queue_path.exists():
                errors.append(f"missing production review queue fixture: {fixture_name}")
            else:
                try:
                    queue = json.loads(queue_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    errors.append(f"invalid production review queue JSON for {fixture_name}: {exc}")
                    continue
                if queue.get("schema_version") != "scan-qc.production-review-queue.v1":
                    errors.append(f"unexpected review queue schema for {fixture_name}")
                items = queue.get("items")
                if not isinstance(items, list) or not items:
                    errors.append(f"review queue fixture has no items: {fixture_name}")
                else:
                    for index, item in enumerate(items, start=1):
                        if not isinstance(item, dict):
                            errors.append(f"review queue item is not an object: {fixture_name} #{index}")
                            continue
                        for key in ["local_id", "reason_zh", "focus_hints_zh", "suggested_action", "severity"]:
                            if key not in item:
                                errors.append(f"missing review queue item field for {fixture_name} #{index}: {key}")
                        if not re.search(r"[\u4e00-\u9fff]", str(item.get("reason_zh", ""))):
                            errors.append(f"review queue reason is not Chinese for {fixture_name} #{index}")
                        focus_hints = item.get("focus_hints_zh")
                        if not isinstance(focus_hints, list) or not focus_hints:
                            errors.append(f"review queue focus hints missing for {fixture_name} #{index}")
                        else:
                            for hint in focus_hints:
                                hint_text = str(hint)
                                if not re.search(r"[\u4e00-\u9fff]", hint_text):
                                    errors.append(f"review queue focus hint is not Chinese for {fixture_name} #{index}")
                                for forbidden_term in ["JSON", "schema", "P0", "P1", "P2", "OCR", "hash", "sha256"]:
                                    if forbidden_term.lower() in hint_text.lower():
                                        errors.append(f"review queue focus hint includes technical term for {fixture_name} #{index}: {forbidden_term}")
                        sensitivity = item.get("sensitivity")
                        if not isinstance(sensitivity, dict) or sensitivity.get("local_only") is not True:
                            errors.append(f"review queue item is not marked local-only for {fixture_name} #{index}")
                    if len(items) != operator.get("files_needing_attention"):
                        errors.append(f"review queue item count does not match operator pending count for {fixture_name}")
                raw_queue = queue_path.read_text(encoding="utf-8").lower()
                for forbidden in ["data:image", "base64,", "source_sha256", "output_sha256"]:
                    if forbidden in raw_queue:
                        errors.append(f"review queue fixture includes forbidden private payload marker: {forbidden}")
                decision_summary = build_review_decision_verification_summary(review_decision_contract_fixture(queue))
                if decision_summary.get("status") != "pass":
                    errors.append(f"production workbench decision export contract does not verify for {fixture_name}: {decision_summary.get('blocking_counts_by_code')}")

    for required_script_token in [
        "production_review_queue.json",
        "applyReviewQueue",
        "decisionArtifact",
        "restoreDraftDecisions",
        "draftDecisionArtifact",
        "operator_decisions",
        "operator_name",
        "operatorName",
        "decisionNote",
        "decided_at",
        "note_zh",
        "stampDecision",
        "updateCurrentNote",
        "scheduleDraftSave",
        "saveDraftDecisions",
        "finishBatch",
        "reviewDecisionShortcuts",
        "recordReviewDecision",
        "handleReviewDecisionShortcut",
        "elementAcceptsTextInput",
        "inReviewShortcutScope",
        'document.addEventListener("keydown", handleReviewDecisionShortcut)',
        'recordReviewDecision(button.dataset.decision)',
        'recordReviewDecision(decision, "keyboard")',
        'aria-keyshortcuts="1"',
        'aria-keyshortcuts="2"',
        'aria-keyshortcuts="3"',
        'aria-keyshortcuts="4"',
        'id="decisionDesk"',
        'tabindex="0" aria-label="图片对比查看"',
        "pickFolder",
        "/api/pick-folder",
        "pickInputButton",
        "pickOutputButton",
        "contractDecisionMap",
        "previewSourceLabel",
        "originalPreviewUrl",
        "processedPreviewUrl",
        "preview-comparison",
        "reviewPositionText",
        "previewSourceText",
        "currentFocusHints",
        "focus_hints_zh",
        "previousReviewedButton",
        "nextPendingButton",
        "clearDecisionButton",
        "上一张已确认",
        "下一张待确认",
        "撤销当前决定",
        "previousReviewedIndex",
        "nextPendingIndex",
        "moveToNextPending",
        "currentDecisionLabel",
        "currentRecommendation",
        "currentDecisionStatus",
        "recommendationLabels",
        "recommendationLabel",
        "recommended-choice",
        "已记录：",
        "已自动显示下一张待确认图片",
        "已撤销当前决定",
        "fitPreviewButton",
        "zoomOutButton",
        "zoomInButton",
        "resetPreviewButton",
        "zoomState",
        "适合窗口",
        "缩小",
        "放大",
        "还原",
        "查看：适合窗口",
        "查看：暂无图片",
        "setPreviewFit",
        "setPreviewZoom",
        "resetPreviewView",
        "applyImageView",
        "scan-qc-review-decisions.summary.json",
        "scan-qc-review-decisions.local.v1",
        "source_target_count",
        "review_counts",
        "review_completion",
        "/api/finish-decisions",
        "/api/save-draft-decisions",
        "/api/preview/",
        "scan-qc-review-decisions.draft.json",
        "已自动保存",
        "已恢复上次进度",
        "本批次是否完成",
        "人工处理",
        "交管理员处理",
        "请填写本机真实文件夹位置",
        "静态打开不会启动处理",
        "recovery_guidance",
        "renderRecoveryGuidance",
        "guidanceFromSummary",
        "folder_setup_missing",
        "folder_path_invalid",
        "empty_input_folder",
        "no_supported_images",
        "processing_failed_retryable",
        "processing_failed_admin",
        "retryButton",
        "retryLocalRun",
        "/api/retry",
        "重试本批次",
        "系统会继续使用当前文件夹",
        "请交管理员处理，不要反复点击开始处理",
        "如果文件夹选错了，请返回重新选择文件夹",
        "no_remaining_work",
        "completion_panel",
        "ready_to_finish",
        "canFinishWithoutReview",
        "completion_note_saved",
        "completion_status_zh",
        "manual_work_zh",
        "admin_handoff_zh",
        "completionStatusFact",
        "manualWorkFact",
        "adminHandoffFact",
        "openOutputFolderButton",
        "openOutputFolderStatus",
        "openOutputFolderAvailable",
        "openOutputFolder",
        "/api/open-output-folder",
        "打开输出文件夹",
        "输出文件夹没有打开。请重新选择输出文件夹，或联系管理员处理。",
        "nextBatchButton",
        "prepareNextBatch",
        "renderCompletionPanel",
        "applyCompletionPanel",
        "finishConfirmPanel",
        "showFinishConfirmation",
        "returnToReviewFromFinishConfirmation",
        "renderFinishConfirmation",
        "confirmFinishButton",
        "returnReviewButton",
        "processingModeInputs",
        "selectedProcessingMode",
        "setProcessingMode",
        "processingModeLabels",
        "当前处理方式",
    ]:
        if required_script_token not in html:
            errors.append(f"missing review queue workflow script token: {required_script_token}")
    validate_preview_fit_controls(html, errors)
    validate_preview_visibility_layout(html, errors)
    validate_completion_export_smoke(html, errors)

    for old_finish_copy in ["导出复核决定", "完成导出", "把处理后图片交给验收或移交流程"]:
        if old_finish_copy in text:
            errors.append(f"old operator finish/export copy still visible: {old_finish_copy}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Production workbench visible Chinese text check passed: {WORKBENCH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
