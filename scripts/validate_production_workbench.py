"""Validate the ordinary-user Chinese production workbench prototype."""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archive_scan_qc.review_decisions import build_review_decision_verification_summary

WORKBENCH = ROOT / "docs" / "production-workbench-prototype.html"
CLI = ROOT / "src" / "archive_scan_qc" / "cli.py"
LOCAL_WORKBENCH = ROOT / "src" / "archive_scan_qc" / "local_workbench.py"
FIXTURE_ROOT = ROOT / "docs" / "fixtures"
FIXTURE_STATES = {
    "production-run-running": "running",
    "production-run-needs-review": "needs_review",
    "production-run-finished": "finished",
    "production-run-blocked": "blocked",
}

REQUIRED_TEXT = {
    "本地生产工作台",
    "管理维护工作台",
    "选择扫描原图文件夹",
    "选择处理后输出文件夹",
    "可以开始处理",
    "正在处理",
    "有图片需要人工确认",
    "批次已完成",
    "处理失败",
    "通过",
    "需要返工",
    "管理员处理",
    "保留原貌痕迹",
    "大图预览",
    "当前图片",
    "问题原因",
    "系统建议",
    "导出复核决定",
    "待决定",
    "自动显示下一张待确认图片",
    "公开安全示意图",
    "本机真实预览",
    "输出位置",
    "原图不会被覆盖",
    "不读取目录内容",
    "不执行处理",
    "不显示本机私有路径",
    "本机处理状态",
    "已自动保存",
    "已恢复上次进度",
    "保存文件夹",
    "本机入口",
    "维护入口",
    "选择维护示例",
    "选择本机状态",
    "请先填写两个本机文件夹位置",
    "需留意文件",
    "原图总数",
    "需要管理员处理",
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

CONTRACT_DECISION_MAP = {
    "pass": "false_positive",
    "needs_rework": "needs_rescan",
    "admin_handling": "blocked",
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
    operator_decisions = ["pass", "needs_rework", "admin_handling", "keep_original_trace"]
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
            "review_completion": {
                "total": len(rows),
                "reviewed": len(rows),
                "pending": 0,
                "complete": len(rows) > 0,
                "counts": counts,
            },
        },
        "review_counts": counts,
        "reviewed_targets": len(rows),
        "decisions": rows,
    }


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
    if "webkitdirectory" not in html:
        errors.append("missing local folder picker controls")
    if "applyRunStatus" not in html:
        errors.append("missing production-run status loader")
    if "operator_summary" not in html:
        errors.append("missing operator summary mapping")
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
        "只有管理员排查或演练时使用，正常加工不需要打开。",
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
            'state.status = "ready";',
            'els.loadStatus.textContent = "文件夹已保存，可以开始处理。";',
        ]:
            if required_token not in save_folders_body:
                errors.append(f"saved-folder configure flow missing token: {required_token}")
        success_copy_index = save_folders_body.find('els.loadStatus.textContent = "文件夹已保存，可以开始处理。";')
        render_after_success_index = save_folders_body.find("render();", success_copy_index)
        if success_copy_index == -1 or render_after_success_index == -1:
            errors.append("saved-folder configure flow does not render after successful save")
    cli = CLI.read_text(encoding="utf-8")
    local_workbench = LOCAL_WORKBENCH.read_text(encoding="utf-8")
    for required_entrypoint_token in [
        "production-workbench",
        "local_workbench_main",
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
        "/api/finish-decisions",
        "/api/save-draft-decisions",
        "/api/preview/",
        "build_review_decision_verification_summary",
        "_required_path",
        "PREVIEW_IMAGE_SUFFIXES",
        "_is_loopback_client",
        "local-only",
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
                        for key in ["local_id", "reason_zh", "suggested_action", "severity"]:
                            if key not in item:
                                errors.append(f"missing review queue item field for {fixture_name} #{index}: {key}")
                        if not re.search(r"[\u4e00-\u9fff]", str(item.get("reason_zh", ""))):
                            errors.append(f"review queue reason is not Chinese for {fixture_name} #{index}")
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
        "scheduleDraftSave",
        "saveDraftDecisions",
        "finishBatch",
        "contractDecisionMap",
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
        "复核保存",
        "浏览器选择只用于提示",
        "静态打开不会启动处理",
    ]:
        if required_script_token not in html:
            errors.append(f"missing review queue workflow script token: {required_script_token}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Production workbench visible Chinese text check passed: {WORKBENCH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
