"""Sensitive local rework action list generation."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


ACTION_TYPES = [
    "rescan_required",
    "reprocess_candidate",
    "manual_review",
    "duplicate_manifest_correction",
    "processing_retry",
    "informational_follow_up",
]

CSV_FIELDS = [
    "action_id",
    "action_type",
    "priority",
    "relative_path",
    "finding_count",
    "processing_retry_count",
    "rules",
    "severities",
    "messages",
    "source_sha256",
    "processing_status",
    "processing_failure_reason",
]


def write_rework_action_list(
    report_path: Path,
    output_path: Path,
    *,
    processing_audit_summary_path: Path | None = None,
    processing_retry_manifest_path: Path | None = None,
    csv_path: Path | None = None,
) -> tuple[Path, Path | None, dict[str, Any]]:
    report = _load_json_object(report_path, "Scan QC report")
    audit_summary = (
        _load_json_object(processing_audit_summary_path, "Processing audit summary")
        if processing_audit_summary_path is not None
        else None
    )
    retry_manifest = (
        _load_json_object(processing_retry_manifest_path, "Processing retry manifest")
        if processing_retry_manifest_path is not None
        else None
    )
    payload = build_rework_action_list(
        report,
        source_report=str(report_path),
        processing_audit_summary=audit_summary,
        processing_audit_summary_source=str(processing_audit_summary_path) if processing_audit_summary_path else None,
        processing_retry_manifest=retry_manifest,
        processing_retry_manifest_source=str(processing_retry_manifest_path) if processing_retry_manifest_path else None,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written_csv = None
    if csv_path is not None:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(_csv_rows(payload["actions"]))
        written_csv = csv_path
    return output_path, written_csv, payload


def build_rework_action_list(
    report: dict[str, Any],
    *,
    source_report: str | None = None,
    processing_audit_summary: dict[str, Any] | None = None,
    processing_audit_summary_source: str | None = None,
    processing_retry_manifest: dict[str, Any] | None = None,
    processing_retry_manifest_source: str | None = None,
) -> dict[str, Any]:
    findings_by_path: dict[str, list[dict[str, Any]]] = {}
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            raise ValueError("Scan QC report findings must be objects.")
        relative_path = str(finding.get("relative_path") or "")
        findings_by_path.setdefault(relative_path, []).append(finding)

    retry_by_path: dict[str, list[dict[str, Any]]] = {}
    if processing_retry_manifest is not None:
        for record in processing_retry_manifest.get("files", []):
            if not isinstance(record, dict):
                raise ValueError("Processing retry manifest files must be objects.")
            relative_path = str(record.get("source_relative_path") or record.get("relative_path") or "")
            retry_by_path.setdefault(relative_path, []).append(record)

    action_inputs: dict[tuple[str, str, str], dict[str, Any]] = {}
    for relative_path, findings in findings_by_path.items():
        action_type = _action_type_for_findings(findings)
        _merge_action_input(action_inputs, relative_path, action_type, findings=findings)
    for relative_path, records in retry_by_path.items():
        _merge_action_input(action_inputs, relative_path, "processing_retry", retry_records=records)

    actions = [_action_from_input(index, key, value) for index, (key, value) in enumerate(sorted(action_inputs.items()), start=1)]
    summary = _summary(actions, report, processing_audit_summary, processing_retry_manifest)
    return {
        "schema_version": "scan-qc.rework-action-list.v1",
        "generated_at": _utc_now(),
        "sensitivity": (
            "LOCAL-ONLY SENSITIVE EVIDENCE. Contains row-level paths, hashes, messages, and processing errors; "
            "do not upload to public systems."
        ),
        "privacy": {
            "local_only": True,
            "contains_row_level_paths": True,
            "contains_hashes": True,
            "contains_messages": True,
            "contains_thumbnails": False,
            "contains_image_content": False,
        },
        "sources": {
            "scan_qc_report": source_report,
            "processing_audit_summary": processing_audit_summary_source,
            "processing_retry_manifest": processing_retry_manifest_source,
        },
        "project": report.get("project", {}),
        "summary": summary,
        "action_types": ACTION_TYPES,
        "actions": actions,
    }


def _merge_action_input(
    action_inputs: dict[tuple[str, str, str], dict[str, Any]],
    relative_path: str,
    action_type: str,
    *,
    findings: list[dict[str, Any]] | None = None,
    retry_records: list[dict[str, Any]] | None = None,
) -> None:
    key = (_priority_for_action(action_type, findings or []), action_type, relative_path)
    item = action_inputs.setdefault(key, {"relative_path": relative_path, "action_type": action_type, "findings": [], "retry_records": []})
    item["findings"].extend(findings or [])
    item["retry_records"].extend(retry_records or [])


def _action_from_input(index: int, key: tuple[str, str, str], item: dict[str, Any]) -> dict[str, Any]:
    priority, action_type, relative_path = key
    findings = sorted(item["findings"], key=lambda finding: (_text(finding.get("severity")), _text(finding.get("rule")), _text(finding.get("message"))))
    retries = sorted(item["retry_records"], key=lambda record: (_text(record.get("failure_reason")), _text(record.get("error"))))
    return {
        "action_id": f"RA{index:06d}",
        "action_type": action_type,
        "priority": priority,
        "relative_path": relative_path,
        "finding_count": len(findings),
        "processing_retry_count": len(retries),
        "rules": sorted({_text(finding.get("rule")) for finding in findings if _text(finding.get("rule"))}),
        "severities": sorted({_text(finding.get("severity")) for finding in findings if _text(finding.get("severity"))}),
        "findings": [_finding_evidence(finding) for finding in findings],
        "processing_retry_evidence": [_retry_evidence(record) for record in retries],
    }


def _action_type_for_findings(findings: list[dict[str, Any]]) -> str:
    rules = " ".join(_text(finding.get("rule")).lower() for finding in findings)
    severities = {_text(finding.get("severity")) for finding in findings}
    if any(token in rules for token in ["duplicate", "manifest", "sequence", "missing", "unexpected", "order"]):
        return "duplicate_manifest_correction"
    if "P0" in severities:
        return "rescan_required"
    if "P1" in severities and any(token in rules for token in ["quality", "dpi", "format", "color", "openable"]):
        return "reprocess_candidate"
    if "P1" in severities:
        return "manual_review"
    if "P2" in severities:
        return "informational_follow_up"
    return "manual_review"


def _priority_for_action(action_type: str, findings: list[dict[str, Any]]) -> str:
    if action_type in {"rescan_required", "processing_retry"}:
        return "P0"
    severities = {_text(finding.get("severity")) for finding in findings}
    if "P0" in severities:
        return "P0"
    if "P1" in severities:
        return "P1"
    if "P2" in severities:
        return "P2"
    return "P2"


def _finding_evidence(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule": _text(finding.get("rule")),
        "severity": _text(finding.get("severity")),
        "source": _text(finding.get("source")),
        "confidence": finding.get("confidence"),
        "message": _text(finding.get("message")),
    }


def _retry_evidence(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_relative_path": _text(record.get("source_relative_path") or record.get("relative_path")),
        "source_sha256": record.get("source_sha256"),
        "status": _text(record.get("status")),
        "failure_reason": _text(record.get("failure_reason")),
        "error": _text(record.get("error")),
        "processing_warnings": list(record.get("processing_warnings") or []),
    }


def _summary(
    actions: list[dict[str, Any]],
    report: dict[str, Any],
    audit_summary: dict[str, Any] | None,
    retry_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "total_actions": len(actions),
        "actions_by_type": {action_type: sum(1 for action in actions if action["action_type"] == action_type) for action_type in ACTION_TYPES},
        "actions_by_priority": {
            priority: sum(1 for action in actions if action["priority"] == priority)
            for priority in ["P0", "P1", "P2"]
        },
        "source_findings": {
            "total_findings": report.get("summary", {}).get("total_findings", len(report.get("findings", []))),
            "p0_findings": report.get("summary", {}).get("p0_findings"),
            "p1_findings": report.get("summary", {}).get("p1_findings"),
            "p2_findings": report.get("summary", {}).get("p2_findings"),
        },
        "processing": {
            "audit_counts": audit_summary.get("counts") if audit_summary else None,
            "retry_list_files": (retry_manifest.get("summary", {}) if retry_manifest else {}).get("retry_list_files"),
        },
    }


def _csv_rows(actions: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for action in actions:
        retry = action["processing_retry_evidence"][0] if action["processing_retry_evidence"] else {}
        rows.append(
            {
                "action_id": action["action_id"],
                "action_type": action["action_type"],
                "priority": action["priority"],
                "relative_path": action["relative_path"],
                "finding_count": str(action["finding_count"]),
                "processing_retry_count": str(action["processing_retry_count"]),
                "rules": ";".join(action["rules"]),
                "severities": ";".join(action["severities"]),
                "messages": " | ".join(finding["message"] for finding in action["findings"]),
                "source_sha256": _text(retry.get("source_sha256")),
                "processing_status": _text(retry.get("status")),
                "processing_failure_reason": _text(retry.get("failure_reason") or retry.get("error")),
            }
        )
    return rows


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON must be an object.")
    return payload


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
