"""Focused smoke guard for the Chinese operator-first production loop."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = ROOT / "docs" / "production-workbench-prototype.html"

PRIVATE_TERMS = {
    "/Users/",
    "/private/",
    "relative_path",
    "source_path",
    "thumbnail",
    "sha256",
    "hash",
    "ocr_text",
    "preview_url",
    "original_path",
    "processed_path",
}


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_no_private_terms(payload: Any, label: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    leaked = sorted(term for term in PRIVATE_TERMS if term.lower() in text.lower())
    _assert(not leaked, f"{label} includes forbidden private terms: {', '.join(leaked)}")


def _index(text: str, token: str) -> int:
    idx = text.find(token)
    _assert(idx >= 0, f"missing required Chinese operator token: {token}")
    return idx


def run_smoke() -> dict[str, Any]:
    html = WORKBENCH.read_text(encoding="utf-8")

    header_index = _index(html, "<h1>本地生产工作台</h1>")
    operator_intro_index = _index(html, "面向扫描加工人员")
    setup_order_index = _index(html, 'aria-label="开始处理顺序"')
    choose_folder_index = _index(html, "填写原图文件夹")
    start_index = _index(html, "开始处理")
    review_index = _index(html, "待确认")
    completion_index = _index(html, "完成并导出结果")
    next_batch_index = _index(html, "准备下一批")
    maintenance_index = _index(html, "<summary>维护入口</summary>")
    maintenance_note_index = _index(html, "这不是正常加工步骤。")
    admin_link_index = _index(html, "管理维护工作台")

    _assert(header_index < operator_intro_index < setup_order_index, "operator-first header sequence changed")
    _assert(choose_folder_index < start_index < review_index < completion_index < next_batch_index, "closed-loop Chinese operator flow order changed")
    _assert(maintenance_index > start_index, "maintenance entry appears before production-start controls")
    _assert(maintenance_note_index > maintenance_index, "maintenance warning placement changed")
    _assert(admin_link_index > header_index, "operator screen header should remain primary")

    for forbidden in [
        "统计优先",
        "先看统计",
        "管理员首页",
        "管理仪表盘",
    ]:
        _assert(forbidden not in html, f"operator-first screen regressed to admin/statistics-first copy: {forbidden}")

    public_evidence = {
        "status": "pass",
        "entry_header_zh": "本地生产工作台",
        "operator_flow_zh": "选择扫描图片文件夹 -> 开始处理 -> 查看/处理待复核图片 -> 完成导出 -> 准备下一批",
        "maintenance_marked_non_primary": maintenance_note_index > maintenance_index > start_index,
        "operator_first_entry": header_index < setup_order_index,
        "admin_dashboard_first": False,
    }
    _assert_no_private_terms(public_evidence, "operator loop smoke evidence")
    return public_evidence


def main() -> int:
    try:
        evidence = run_smoke()
    except AssertionError as exc:
        print(f"Production workbench operator-loop smoke failed: {exc}", file=sys.stderr)
        return 1
    print("Production workbench operator-loop smoke passed")
    print(
        "operator_first_entry={operator_first_entry} maintenance_non_primary={maintenance_marked_non_primary} "
        "admin_dashboard_first={admin_dashboard_first}".format(**evidence)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
