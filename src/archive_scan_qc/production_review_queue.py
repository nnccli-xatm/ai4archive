"""Local-only production review queue for operator-facing scan decisions."""

from __future__ import annotations

from collections import Counter
import json
import re
from pathlib import Path
from typing import Any


PRODUCTION_REVIEW_QUEUE_JSON = "production_review_queue.json"
SCHEMA_VERSION = "scan-qc.production-review-queue.v1"
SENSITIVITY_NOTICE = (
    "LOCAL-ONLY PRODUCTION REVIEW QUEUE. Contains row-level local paths and operator decision context; "
    "keep inside the approved local production environment and do not publish as public evidence."
)
OPERATOR_ACTIONS = ("pass", "rescan", "reprocess", "keep_original_trace", "skip")
SOURCE_CATEGORIES = ("scan_qc", "processing_failure", "guardrail_warning", "rework_action")
_SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "info": 4, "": 5}


def write_production_review_queue(
    output_path: Path,
    *,
    scan_qc_report_path: Path | None = None,
    processing_review_package_path: Path | None = None,
    rework_action_list_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    queue = build_production_review_queue(
        scan_qc_report=_load_json_object(scan_qc_report_path, "scan QC report") if scan_qc_report_path else None,
        scan_qc_report_source=str(scan_qc_report_path) if scan_qc_report_path else None,
        processing_review_package=(
            _load_json_object(processing_review_package_path, "processing review package")
            if processing_review_package_path
            else None
        ),
        processing_review_package_source=str(processing_review_package_path) if processing_review_package_path else None,
        rework_action_list=_load_json_object(rework_action_list_path, "rework action list") if rework_action_list_path else None,
        rework_action_list_source=str(rework_action_list_path) if rework_action_list_path else None,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path, queue


def build_production_review_queue(
    *,
    scan_qc_report: dict[str, Any] | None = None,
    scan_qc_report_source: str | None = None,
    processing_review_package: dict[str, Any] | None = None,
    processing_review_package_source: str | None = None,
    rework_action_list: dict[str, Any] | None = None,
    rework_action_list_source: str | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if scan_qc_report is not None:
        items.extend(_scan_qc_items(scan_qc_report))
    if processing_review_package is not None:
        items.extend(_processing_review_items(processing_review_package))
    if rework_action_list is not None:
        items.extend(_rework_action_items(rework_action_list))

    items = [_with_local_id(index, item) for index, item in enumerate(sorted(items, key=_sort_key), start=1)]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _generated_at(scan_qc_report, processing_review_package, rework_action_list),
        "sensitivity": SENSITIVITY_NOTICE,
        "privacy": {
            "local_only": True,
            "aggregate_only": False,
            "contains_row_level_paths": True,
            "contains_operator_messages": True,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_image_bytes": False,
            "contains_base64": False,
            "contains_ocr_text": False,
            "contains_public_evidence": False,
        },
        "allowed_operator_actions": list(OPERATOR_ACTIONS),
        "source_categories": list(SOURCE_CATEGORIES),
        "sources": {
            "scan_qc_report": scan_qc_report_source,
            "processing_review_package": processing_review_package_source,
            "rework_action_list": rework_action_list_source,
        },
        "project": _project(scan_qc_report, processing_review_package, rework_action_list),
        "summary": _summary(items),
        "items": items,
    }


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return payload


def _scan_qc_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            raise ValueError("Scan QC report findings must be objects.")
        severity = _severity(finding.get("severity"))
        if severity == "P0":
            action = "rescan"
        elif severity == "P1":
            action = "reprocess" if _looks_processable(finding) else "keep_original_trace"
        elif severity == "P2":
            action = "pass"
        else:
            action = "keep_original_trace"
        rule = _text(finding.get("rule"))
        message = _text(finding.get("message"))
        operator_note = _scan_operator_note_zh(rule, message)
        items.append(
            _item(
                source_category="scan_qc",
                source_ref=rule,
                relative_path=_text(finding.get("relative_path")),
                severity=severity or "P2",
                suggested_action=action,
                reason_zh=_scan_reason_zh(severity, rule, operator_note),
                operator_note=operator_note,
            )
        )
    return items


def _processing_review_items(package: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for record in package.get("files", []):
        if not isinstance(record, dict):
            raise ValueError("Processing review package files must be objects.")
        path = _text(record.get("source_relative_path") or record.get("relative_path"))
        status = _text(record.get("status"))
        warnings = [_text(value) for value in record.get("processing_warnings") or [] if _text(value)]
        guardrails = [_text(value) for value in record.get("guardrail_failures") or [] if _text(value)]
        if status == "failed":
            reason = _text(record.get("failure_reason") or record.get("error") or "processing failed")
            operator_note = _processing_failure_note_zh(reason)
            items.append(
                _item(
                    source_category="processing_failure",
                    source_ref="processing_failed",
                    relative_path=path,
                    severity="P0",
                    suggested_action="reprocess",
                    reason_zh=f"处理失败：{operator_note}。请重新处理，若源图无法打开则转为重扫。",
                    operator_note=operator_note,
                )
            )
        for warning in warnings + guardrails:
            operator_note = _processing_guardrail_note_zh(warning)
            items.append(
                _item(
                    source_category="guardrail_warning",
                    source_ref="processing_guardrail",
                    relative_path=path,
                    severity="P1",
                    suggested_action="keep_original_trace",
                    reason_zh=f"处理保护线提示：{operator_note}。请人工确认成品，必要时保留原始轨迹。",
                    operator_note=operator_note,
                )
            )
    return items


def _rework_action_items(action_list: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for action in action_list.get("actions", []):
        if not isinstance(action, dict):
            raise ValueError("Rework action list actions must be objects.")
        action_type = _text(action.get("action_type"))
        suggested_action = _operator_action_for_rework(action_type)
        severity = _severity(action.get("priority")) or "P2"
        messages = [_text(finding.get("message")) for finding in action.get("findings", []) if isinstance(finding, dict)]
        retry_messages = [
            _text(record.get("failure_reason") or record.get("error"))
            for record in action.get("processing_retry_evidence", [])
            if isinstance(record, dict)
        ]
        note = _rework_operator_note_zh(action_type, messages, retry_messages)
        items.append(
            _item(
                source_category="rework_action",
                source_ref=action_type,
                relative_path=_text(action.get("relative_path")),
                severity=severity,
                suggested_action=suggested_action,
                reason_zh=_rework_reason_zh(action_type, note),
                operator_note=note,
            )
        )
    return items


def _item(
    *,
    source_category: str,
    source_ref: str,
    relative_path: str,
    severity: str,
    suggested_action: str,
    reason_zh: str,
    operator_note: str,
) -> dict[str, Any]:
    if suggested_action not in OPERATOR_ACTIONS:
        raise ValueError(f"Unsupported operator action: {suggested_action}")
    return {
        "local_id": "",
        "relative_path": relative_path,
        "severity": severity,
        "source_category": source_category,
        "source_ref": source_ref,
        "reason_zh": reason_zh,
        "focus_hints_zh": _focus_hints_zh(source_category, source_ref, suggested_action, operator_note),
        "suggested_action": suggested_action,
        "operator_note": operator_note,
        "sensitivity": {
            "local_only": True,
            "contains_image_bytes": False,
            "contains_thumbnail": False,
            "contains_hash": False,
            "contains_ocr_text": False,
        },
    }


def _with_local_id(index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {"local_id": f"PRQ{index:06d}", **{key: value for key, value in item.items() if key != "local_id"}}


def _sort_key(item: dict[str, Any]) -> tuple[int, str, str, str, str]:
    return (
        _SEVERITY_RANK.get(_text(item.get("severity")), 9),
        _text(item.get("relative_path")),
        _text(item.get("source_category")),
        _text(item.get("source_ref")),
        _text(item.get("operator_note")),
    )


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_severity = Counter(_text(item.get("severity")) for item in items)
    by_category = Counter(_text(item.get("source_category")) for item in items)
    by_action = Counter(_text(item.get("suggested_action")) for item in items)
    return {
        "total_items": len(items),
        "items_by_severity": {severity: by_severity.get(severity, 0) for severity in ["P0", "P1", "P2", "P3", "info"]},
        "items_by_source_category": {category: by_category.get(category, 0) for category in SOURCE_CATEGORIES},
        "items_by_suggested_action": {action: by_action.get(action, 0) for action in OPERATOR_ACTIONS},
        "ready_for_operator_review": bool(items),
    }


def _generated_at(*payloads: dict[str, Any] | None) -> str | None:
    values = [_text(payload.get("generated_at")) for payload in payloads if isinstance(payload, dict) and _text(payload.get("generated_at"))]
    return sorted(values)[-1] if values else None


def _project(*payloads: dict[str, Any] | None) -> dict[str, Any]:
    for payload in payloads:
        if isinstance(payload, dict) and isinstance(payload.get("project"), dict):
            return payload["project"]
    return {}


def _looks_processable(finding: dict[str, Any]) -> bool:
    haystack = " ".join([_text(finding.get("rule")), _text(finding.get("message"))]).lower()
    return any(token in haystack for token in ["quality", "dpi", "format", "color", "blur", "dark", "crop", "deskew"])


def _operator_action_for_rework(action_type: str) -> str:
    return {
        "rescan_required": "rescan",
        "reprocess_candidate": "reprocess",
        "manual_review": "keep_original_trace",
        "duplicate_manifest_correction": "skip",
        "processing_retry": "reprocess",
        "informational_follow_up": "pass",
    }.get(action_type, "keep_original_trace")


def _scan_reason_zh(severity: str, rule: str, message: str) -> str:
    if severity == "P0":
        return f"扫描质检发现阻断问题：{message or rule}。请优先重扫或剔除不可用源图。"
    if severity == "P1":
        return f"扫描质检发现需处理问题：{message or rule}。请决定重处理或保留原始轨迹。"
    return f"扫描质检提示：{message or rule}。确认无影响后可通过。"


def _scan_operator_note_zh(rule: str, message: str) -> str:
    rule_notes = {
        "openability": "源图无法打开",
        "unsupported_format": "文件格式不在本批次支持范围内",
        "dpi_minimum": "图片扫描分辨率低于最低要求",
        "dpi_missing": "图片缺少水平或垂直扫描分辨率信息",
        "dimensions": "图片宽度或高度信息缺失",
        "multi_page_image_container": "图片容器包含多页或多帧，请确认交付规范",
        "name_pattern": "文件名不符合本批次命名规则",
        "quality_near_blank_page": "页面疑似空白或内容过少，请确认是否漏扫",
        "quality_too_dark": "页面亮度偏暗，请确认是否需要重扫或重处理",
        "quality_too_bright": "页面亮度偏高且对比度偏低，请确认成像质量",
        "quality_low_contrast": "页面对比度偏低，请确认文字或图像是否清晰",
        "quality_suspected_blur": "文字边缘疑似模糊，请确认是否需要重扫",
        "quality_skew_candidate": "页面疑似轻微倾斜，请确认是否需要纠偏处理",
        "quality_dark_border_candidate": "页面边缘疑似存在黑边，请确认是否需要裁边处理",
        "quality_scanline_candidate": "页面疑似存在扫描线或条纹，请复核源图",
        "quality_content_edge_cutoff_candidate": "页面边缘疑似有正文、印章或页码被裁切，请复核源图",
        "duplicate_name": "同一文件夹内存在重名文件，请修正清单或文件名",
        "duplicate_file": "发现重复文件，请确认是否需要剔除",
        "batch_format_consistency": "同批可打开图片格式不一致，请确认批次配置",
        "batch_color_mode_consistency": "同批可打开图片颜色模式不一致，请确认批次配置",
        "batch_dpi_consistency": "同批可打开图片扫描分辨率不一致，请确认扫描参数",
        "batch_orientation_consistency": "同批图片方向不一致，请确认页面方向或清单顺序",
    }
    note = rule_notes.get(rule)
    if note:
        return _append_first_number(note, message)
    return "质检规则触发，请人工复核"


def _focus_hints_zh(source_category: str, source_ref: str, suggested_action: str, operator_note: str) -> list[str]:
    hints_by_ref = {
        "openability": ["看图片能否正常打开", "打不开就按重扫处理"],
        "openable": ["看图片能否正常打开", "打不开就按重扫处理"],
        "unsupported_format": ["看图片是否能正常预览", "确认是否需要换成常用图片格式"],
        "dpi_minimum": ["看文字和细节是否清楚", "不清楚就重扫"],
        "dpi_missing": ["看文字和细节是否清楚", "确认扫描设置是否一致"],
        "dimensions": ["看图片是否完整显示", "确认画面有没有异常缺失"],
        "multi_page_image_container": ["看是否夹带多页画面", "确认是否需要拆成单页"],
        "name_pattern": ["看页面是否放在正确批次", "确认顺序和命名是否需要修正"],
        "quality_near_blank_page": ["看页面是否真的空白", "留意是否漏扫正文"],
        "quality_too_dark": ["看页面是否偏暗", "确认文字和印章是否可读"],
        "quality_too_bright": ["看页面是否过亮", "确认浅色文字是否丢失"],
        "quality_low_contrast": ["看文字和背景是否分得清", "确认细节是否可读"],
        "quality_suspected_blur": ["看文字边缘是否模糊", "不清楚就重扫"],
        "quality_skew_candidate": ["看页面是否倾斜", "确认是否需要重新处理"],
        "quality_dark_border_candidate": ["看页面四边是否有黑边", "确认裁边后不能切到内容"],
        "quality_scanline_candidate": ["看页面是否有横线或条纹", "确认是否影响阅读"],
        "quality_content_edge_cutoff_candidate": ["看页面边缘内容是否被切掉", "留意页码、印章和边注"],
        "duplicate_name": ["看是否重复放入同一页", "确认是否缺页或顺序错误"],
        "duplicate_file": ["看是否重复扫描同一页", "确认是否少了相邻页面"],
        "batch_format_consistency": ["看本页是否属于当前批次", "确认图片能正常预览"],
        "batch_color_mode_consistency": ["看颜色是否和同批明显不同", "确认是否影响阅读"],
        "batch_dpi_consistency": ["看清晰度是否和同批明显不同", "确认是否需要重扫"],
        "batch_orientation_consistency": ["看页面方向是否正确", "确认前后页顺序是否正常"],
        "processing_failed": ["看原图是否能打开", "能打开就重新处理，打不开就重扫"],
        "processing_guardrail": ["对比原图和处理后图片", "留意内容是否被裁掉或变暗变亮"],
        "rescan_required": ["看原图质量是否足够", "确认是否需要重新扫描"],
        "reprocess_candidate": ["看原图是否可用", "确认处理后图片是否需要重新生成"],
        "manual_review": ["看画面是否完整可读", "确认是否需要人工处理"],
        "duplicate_manifest_correction": ["看是否重复或缺页", "确认页面顺序是否要修正"],
        "processing_retry": ["看原图是否能打开", "确认是否重新处理"],
        "informational_follow_up": ["看提示问题是否属实", "确认后再通过"],
    }
    hints = list(hints_by_ref.get(source_ref, []))
    note = operator_note
    if not hints:
        if "无法打开" in note:
            hints = ["看图片能否正常打开", "打不开就按重扫处理"]
        elif "重复" in note:
            hints = ["看是否重复扫描同一页", "确认是否少了相邻页面"]
        elif "顺序" in note:
            hints = ["看前后页顺序是否正常", "确认是否缺页或重复"]
        elif "黑边" in note or "裁" in note:
            hints = ["看页面四边是否异常", "确认内容没有被切掉"]
        elif "倾斜" in note:
            hints = ["看页面是否倾斜", "确认是否需要重新处理"]
        elif "偏暗" in note or "亮度" in note:
            hints = ["看页面明暗是否合适", "确认文字和印章是否可读"]
        elif "模糊" in note or "清晰" in note:
            hints = ["看文字边缘是否清楚", "不清楚就重扫"]
        else:
            hints = ["看画面是否完整可读", "确认后再选择处理决定"]
    if suggested_action == "rescan":
        hints.append("重点判断是否需要重扫")
    elif suggested_action == "reprocess":
        hints.append("重点判断是否需要重新处理")
    elif suggested_action == "keep_original_trace":
        hints.append("重点判断是否保留原貌")
    return list(dict.fromkeys(hints))[:3]


def _processing_failure_note_zh(reason: str) -> str:
    normalized = reason.lower()
    if "openable" in normalized or "cannot open" in normalized or "could not be opened" in normalized:
        return "源图无法打开"
    if "guardrail" in normalized:
        return "处理保护线触发导致处理失败"
    if "missing relative_path" in normalized:
        return "处理清单缺少相对路径"
    if "duplicate derivative source output is missing" in normalized:
        return "重复源图的派生成品缺失"
    if "preflight failed" in normalized:
        return "预检未通过"
    return "处理失败，请重新处理并复核源图"


def _processing_guardrail_note_zh(warning: str) -> str:
    normalized = warning.lower()
    if "pixel_change_ratio" in normalized:
        return "处理后像素变化比例超过复核阈值"
    if "crop_ratio" in normalized or "crop ratio" in normalized:
        return "裁切比例超过保护线"
    if "brightness_delta" in normalized:
        return "处理前后亮度变化超过保护线"
    if "contrast_delta" in normalized:
        return "处理前后对比度变化超过保护线"
    if "deskew" in normalized:
        return "纠偏处理结果需要人工复核"
    if "guardrail" in normalized:
        return "处理保护线触发"
    return "处理保护线触发，请人工复核成品"


def _rework_operator_note_zh(action_type: str, messages: list[str], retry_messages: list[str]) -> str:
    notes = [_rework_message_note_zh(message) for message in messages + retry_messages if message]
    notes = [note for note in notes if note]
    if notes:
        return "；".join(dict.fromkeys(notes))
    return {
        "rescan_required": "需要重扫",
        "reprocess_candidate": "建议重新处理",
        "manual_review": "需要人工复核",
        "duplicate_manifest_correction": "清单或顺序需要修正",
        "processing_retry": "需要重新处理",
        "informational_follow_up": "提示信息需要跟进",
    }.get(action_type, "需要人工确认")


def _rework_message_note_zh(message: str) -> str:
    normalized = message.lower()
    if "duplicate" in normalized:
        return "疑似重复记录需要修正"
    if "sequence" in normalized or "order" in normalized:
        return "页面顺序需要人工确认"
    if "openable" in normalized or "cannot open" in normalized or "could not be opened" in normalized:
        return "源图无法打开"
    if "guardrail" in normalized:
        return "处理保护线触发"
    if "filename" in normalized or "name" in normalized:
        return "文件名需要人工确认"
    return "返工信息需要人工确认"


def _rework_reason_zh(action_type: str, note: str) -> str:
    prefix = {
        "rescan_required": "返工清单要求重扫",
        "reprocess_candidate": "返工清单建议重处理",
        "manual_review": "返工清单要求人工复核",
        "duplicate_manifest_correction": "返工清单提示清单或顺序修正",
        "processing_retry": "返工清单要求处理重试",
        "informational_follow_up": "返工清单提示信息跟进",
    }.get(action_type, "返工清单要求人工确认")
    return f"{prefix}：{note or '需要人工确认'}。"


def _severity(value: Any) -> str:
    text = _text(value).upper()
    return text if text in {"P0", "P1", "P2", "P3"} else _text(value)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _append_first_number(text: str, source: str) -> str:
    match = re.search(r"\d+(?:\.\d+)?", source)
    return f"{text}（参考值 {match.group(0)}）" if match else text
