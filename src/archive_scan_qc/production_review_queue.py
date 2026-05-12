"""Local-only production review queue for operator-facing scan decisions."""

from __future__ import annotations

from collections import Counter
import json
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
        items.append(
            _item(
                source_category="scan_qc",
                source_ref=rule,
                relative_path=_text(finding.get("relative_path")),
                severity=severity or "P2",
                suggested_action=action,
                reason_zh=_scan_reason_zh(severity, rule, message),
                operator_note=message,
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
            items.append(
                _item(
                    source_category="processing_failure",
                    source_ref="processing_failed",
                    relative_path=path,
                    severity="P0",
                    suggested_action="reprocess",
                    reason_zh=f"处理失败：{reason}。请重新处理，若源图无法打开则转为重扫。",
                    operator_note=reason,
                )
            )
        for warning in warnings + guardrails:
            items.append(
                _item(
                    source_category="guardrail_warning",
                    source_ref="processing_guardrail",
                    relative_path=path,
                    severity="P1",
                    suggested_action="keep_original_trace",
                    reason_zh=f"处理保护线提示：{warning}。请人工确认成品，必要时保留原始轨迹。",
                    operator_note=warning,
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
        note = "; ".join(value for value in messages + retry_messages if value)
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


def _rework_reason_zh(action_type: str, note: str) -> str:
    prefix = {
        "rescan_required": "返工清单要求重扫",
        "reprocess_candidate": "返工清单建议重处理",
        "manual_review": "返工清单要求人工复核",
        "duplicate_manifest_correction": "返工清单提示清单或顺序修正",
        "processing_retry": "返工清单要求处理重试",
        "informational_follow_up": "返工清单提示信息跟进",
    }.get(action_type, "返工清单要求人工确认")
    return f"{prefix}：{note or action_type}。"


def _severity(value: Any) -> str:
    text = _text(value).upper()
    return text if text in {"P0", "P1", "P2", "P3"} else _text(value)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
