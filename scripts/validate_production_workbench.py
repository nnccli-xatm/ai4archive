"""Validate the ordinary-user Chinese production workbench prototype."""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = ROOT / "docs" / "production-workbench-prototype.html"
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
    "已暂停，可以继续",
    "有图片需要人工确认",
    "批次已完成",
    "处理失败",
    "通过",
    "需要重扫",
    "重新处理",
    "保留原貌痕迹",
    "跳过",
    "大图预览",
    "当前图片",
    "问题原因",
    "系统建议",
    "自动显示下一张待确认图片",
    "公开安全示意图",
    "本机真实预览",
    "输出位置",
    "原图不会被覆盖",
    "不读取目录内容",
    "不执行处理",
    "不显示本机私有路径",
    "加载本机状态",
    "选择状态示例",
    "选择本机状态文件",
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
    for fixture_name, expected_status in FIXTURE_STATES.items():
        fixture_dir = FIXTURE_ROOT / fixture_name
        summary_path = fixture_dir / "production_run_summary.json"
        progress_path = fixture_dir / "production_run_progress.json"
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

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Production workbench visible Chinese text check passed: {WORKBENCH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
