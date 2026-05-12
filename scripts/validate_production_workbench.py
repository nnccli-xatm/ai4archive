"""Validate the ordinary-user Chinese production workbench prototype."""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = ROOT / "docs" / "production-workbench-prototype.html"

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
    "输出位置",
    "原图不会被覆盖",
    "不读取目录内容",
    "不执行处理",
    "不显示本机私有路径",
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

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Production workbench visible Chinese text check passed: {WORKBENCH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
