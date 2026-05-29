"""Sensitive local acceptance sampling export."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from math import ceil
from pathlib import Path
from typing import Any

ALLOWED_REVIEW_DECISIONS = ("pending", "accepted_issue", "false_positive", "fixed_externally", "needs_rescan", "blocked")


SAMPLING_JSON = "acceptance_sampling_review.json"
SAMPLING_CSV = "acceptance_sampling_review.csv"
SCHEMA_VERSION = "scan-qc.acceptance-sampling.v1"
DEFAULT_SAMPLE_RATIO = 0.05
MIN_SAMPLE_RATIO = 0.05
SENSITIVE_LOCAL_EVIDENCE = "sensitive_local_evidence"

CSV_FIELDS = [
    "sample_id",
    "selection_reason",
    "risk_tier",
    "relative_path",
    "filename",
    "manifest_order_index",
    "manifest_sequence",
    "openable",
    "format",
    "width",
    "height",
    "dpi_x",
    "dpi_y",
    "color_mode",
    "orientation_class",
    "frame_count",
    "sha256",
    "file_error",
    "finding_count",
    "highest_severity",
    "rules",
    "severities",
    "review_status",
    "reviewer_notes",
]

SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2}
RISK_TIER_BY_SEVERITY = {"P0": "p0", "P1": "p1", "P2": "p2"}
RISK_ESCALATION_P0_THRESHOLD = 0.05
RISK_ESCALATION_P0_SAMPLE_RATIO = 0.10
RISK_ESCALATION_P1_THRESHOLD = 0.15
RISK_ESCALATION_P1_SAMPLE_RATIO = 0.20


def _compute_effective_sample_ratio(report: dict[str, Any], base_ratio: float) -> float:
    files = report.get("files", [])
    if not isinstance(files, list) or not files:
        return base_ratio
    total = len(files)
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        return base_ratio
    p0_count = sum(1 for f in findings if isinstance(f, dict) and f.get("severity") == "P0")
    p1_count = sum(1 for f in findings if isinstance(f, dict) and f.get("severity") == "P1")
    p0_ratio = p0_count / total
    p1_ratio = p1_count / total
    if p0_ratio > RISK_ESCALATION_P0_THRESHOLD:
        return max(base_ratio, RISK_ESCALATION_P0_SAMPLE_RATIO)
    if p1_ratio > RISK_ESCALATION_P1_THRESHOLD:
        return max(base_ratio, RISK_ESCALATION_P1_SAMPLE_RATIO)
    return base_ratio


def write_acceptance_sampling_export(report_path: Path, output_dir: Path, *, sample_ratio: float = DEFAULT_SAMPLE_RATIO) -> tuple[Path, Path, dict[str, Any]]:
    report = _load_json_object(report_path, "Scan QC report")
    payload = build_acceptance_sampling_export(report, source_report=report_path, sample_ratio=sample_ratio)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / SAMPLING_JSON
    csv_path = output_dir / SAMPLING_CSV
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(payload["samples"])
    return json_path, csv_path, payload


def generate_sampling_checklist(
    report_path: Path,
    output_dir: Path,
    *,
    base_sample_ratio: float = DEFAULT_SAMPLE_RATIO,
) -> tuple[Path, dict[str, Any]]:
    report = _load_json_object(report_path, "Scan QC report")
    effective_ratio = _compute_effective_sample_ratio(report, base_sample_ratio)
    payload = build_acceptance_sampling_export(report, source_report=report_path, sample_ratio=effective_ratio)
    output_dir.mkdir(parents=True, exist_ok=True)
    checklist_path = output_dir / "sampling_checklist.csv"
    with checklist_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(payload["samples"])
    json_path = output_dir / SAMPLING_JSON
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return checklist_path, payload


def record_sampling_review(
    sampling_json_path: Path,
    reviews: list[dict[str, str]],
) -> dict[str, Any]:
    if not sampling_json_path.exists():
        raise FileNotFoundError(f"Sampling review file not found: {sampling_json_path}")
    payload = json.loads(sampling_json_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "samples" not in payload:
        raise ValueError("Invalid sampling review JSON structure.")
    samples = payload["samples"]
    review_map = {str(r.get("sample_id", "")): r for r in reviews if isinstance(r, dict)}
    updated = 0
    for sample in samples:
        sid = str(sample.get("sample_id", ""))
        if sid in review_map:
            review = review_map[sid]
            new_status = str(review.get("review_status", "")).strip().lower()
            if new_status in ALLOWED_REVIEW_DECISIONS:
                sample["review_status"] = new_status
                if review.get("reviewer_notes"):
                    sample["reviewer_notes"] = str(review["reviewer_notes"])
                updated += 1
    counts = _aggregate_counts_from_samples(samples, payload.get("selection", {}).get("sample_ratio", DEFAULT_SAMPLE_RATIO))
    payload["aggregate_sampling_counts"] = counts
    sampling_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"updated": updated, "total_samples": len(samples), "aggregate_sampling_counts": counts}


def _aggregate_counts_from_samples(samples: list[dict[str, Any]], sample_ratio: float) -> dict[str, Any]:
    target_count = min(len(samples), ceil(len(samples) / sample_ratio)) if samples and sample_ratio > 0 else 0
    reviewed = sum(1 for s in samples if str(s.get("review_status", "")).strip().lower() != "pending")
    pending = len(samples) - reviewed
    return {
        "schema_version": "scan-qc.acceptance-sampling-counts.v1",
        "privacy": {"aggregate_only": True},
        "total_records": len(samples),
        "target_sample_ratio": sample_ratio,
        "minimum_sample_ratio": MIN_SAMPLE_RATIO,
        "target_sample_count": target_count,
        "generated_sample_task_count": len(samples),
        "sample_task_target_met": len(samples) >= target_count,
        "reviewed_sample_count": reviewed,
        "pending_sample_count": pending,
        "sampling_target_met": reviewed >= target_count,
        "sampled_records": len(samples),
        "sample_ratio": sample_ratio,
    }


def build_acceptance_sampling_export(report: dict[str, Any], *, source_report: Path | None = None, sample_ratio: float = DEFAULT_SAMPLE_RATIO) -> dict[str, Any]:
    if sample_ratio < MIN_SAMPLE_RATIO:
        raise ValueError(f"sample ratio must be at least {MIN_SAMPLE_RATIO:.2%}.")
    files = report.get("files", [])
    findings = report.get("findings", [])
    if not isinstance(files, list):
        raise ValueError("Scan QC report field 'files' must be a list.")
    if not isinstance(findings, list):
        raise ValueError("Scan QC report field 'findings' must be a list.")

    finding_map = _findings_by_path(findings)
    candidates = [_candidate(index, item, finding_map) for index, item in enumerate(files)]
    sample_count = min(len(candidates), ceil(len(candidates) * sample_ratio)) if candidates else 0
    selected = _select_candidates(candidates, sample_count)
    rows = [_sample_row(index, candidate) for index, candidate in enumerate(selected, start=1)]
    aggregate_counts = _aggregate_counts(candidates, rows, sample_ratio)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(source_report) if source_report is not None else None,
        "sensitivity": SENSITIVE_LOCAL_EVIDENCE,
        "privacy": {
            "sensitive_local_evidence": True,
            "aggregate_only": False,
            "contains": [
                "source file identifiers",
                "source location strings",
                "content hashes",
                "row-level automated findings",
                "manual reviewer status fields",
            ],
            "omits": ["embedded images", "thumbnails", "recognized text", "OCR text", "image bytes", "base64 image content"],
        },
        "selection": {
            "deterministic": True,
            "sample_ratio": sample_ratio,
            "minimum_sample_ratio": MIN_SAMPLE_RATIO,
            "total_records": len(candidates),
            "sampled_records": len(rows),
            "selection_order": "risk tier P0/P1/P2/problematic first, then stable SHA-256 ordering",
        },
        "aggregate_sampling_counts": aggregate_counts,
        "samples": rows,
    }


def _select_candidates(candidates: list[dict[str, Any]], sample_count: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=_risk_sort_key):
        if candidate["risk_rank"] <= 3:
            selected.append(candidate)
            seen.add(candidate["stable_key"])
    if len(selected) < sample_count:
        for candidate in sorted(candidates, key=_stable_sort_key):
            if candidate["stable_key"] not in seen:
                selected.append(candidate)
                seen.add(candidate["stable_key"])
            if len(selected) >= sample_count:
                break
    return selected


def _candidate(index: int, item: Any, finding_map: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("Scan QC report files must be objects.")
    relative_path = str(item.get("relative_path") or "")
    findings = finding_map.get(relative_path, [])
    severities = sorted({str(finding.get("severity") or "") for finding in findings if finding.get("severity")}, key=lambda value: SEVERITY_RANK.get(value, 99))
    highest = severities[0] if severities else ""
    problematic = item.get("openable") is False or bool(item.get("error")) or bool(findings)
    risk_rank = SEVERITY_RANK.get(highest, 3 if problematic else 4)
    stable_key = _stable_key(item, index)
    return {
        "index": index,
        "file": item,
        "findings": findings,
        "severities": severities,
        "highest_severity": highest,
        "problematic": problematic,
        "risk_rank": risk_rank,
        "risk_tier": RISK_TIER_BY_SEVERITY.get(highest, "problematic" if problematic else "baseline"),
        "stable_key": stable_key,
    }


def _sample_row(index: int, candidate: dict[str, Any]) -> dict[str, Any]:
    file_record = candidate["file"]
    findings = candidate["findings"]
    rules = sorted({str(finding.get("rule") or "") for finding in findings if finding.get("rule")})
    severities = candidate["severities"]
    return {
        "sample_id": f"S{index:06d}",
        "selection_reason": _selection_reason(candidate),
        "risk_tier": candidate["risk_tier"],
        "relative_path": str(file_record.get("relative_path") or ""),
        "filename": str(file_record.get("filename") or ""),
        "manifest_order_index": file_record.get("manifest_order_index"),
        "manifest_sequence": file_record.get("manifest_sequence"),
        "openable": file_record.get("openable"),
        "format": file_record.get("format"),
        "width": file_record.get("width"),
        "height": file_record.get("height"),
        "dpi_x": file_record.get("dpi_x"),
        "dpi_y": file_record.get("dpi_y"),
        "color_mode": file_record.get("color_mode"),
        "orientation_class": file_record.get("orientation_class"),
        "frame_count": file_record.get("frame_count"),
        "sha256": str(file_record.get("sha256") or ""),
        "file_error": str(file_record.get("error") or ""),
        "finding_count": len(findings),
        "highest_severity": candidate["highest_severity"],
        "rules": ";".join(rules),
        "severities": ";".join(severities),
        "review_status": "pending",
        "reviewer_notes": "",
    }


def _aggregate_counts(candidates: list[dict[str, Any]], rows: list[dict[str, Any]], sample_ratio: float) -> dict[str, Any]:
    total_by_risk: dict[str, int] = {}
    sampled_by_risk: dict[str, int] = {}
    for candidate in candidates:
        total_by_risk[candidate["risk_tier"]] = total_by_risk.get(candidate["risk_tier"], 0) + 1
    for row in rows:
        tier = str(row["risk_tier"])
        sampled_by_risk[tier] = sampled_by_risk.get(tier, 0) + 1
    target_sample_count = min(len(candidates), ceil(len(candidates) * sample_ratio)) if candidates else 0
    reviewed_sample_count = sum(1 for row in rows if str(row.get("review_status") or "").strip().lower() != "pending")
    pending_sample_count = max(0, len(rows) - reviewed_sample_count)
    return {
        "schema_version": "scan-qc.acceptance-sampling-counts.v1",
        "privacy": {"aggregate_only": True},
        "design_reference": "archive-scan-qc-retouch-design.md section 11 manual sampling target",
        "input_total": len(candidates),
        "total_records": len(candidates),
        "target_sample_ratio": sample_ratio,
        "minimum_sample_ratio": MIN_SAMPLE_RATIO,
        "target_sample_count": target_sample_count,
        "generated_sample_task_count": len(rows),
        "sample_task_target_met": len(rows) >= target_sample_count,
        "reviewed_sample_count": reviewed_sample_count,
        "pending_sample_count": pending_sample_count,
        "sampling_target_met": reviewed_sample_count >= target_sample_count,
        "sampled_records": len(rows),
        "sample_ratio": sample_ratio,
        "effective_sample_ratio": round(len(rows) / len(candidates), 6) if candidates else 0.0,
        "total_by_risk_tier": dict(sorted(total_by_risk.items())),
        "sampled_by_risk_tier": dict(sorted(sampled_by_risk.items())),
    }


def _findings_by_path(findings: list[Any]) -> dict[str, list[dict[str, Any]]]:
    by_path: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Scan QC report findings must be objects.")
        by_path.setdefault(str(finding.get("relative_path") or ""), []).append(finding)
    return by_path


def _risk_sort_key(candidate: dict[str, Any]) -> tuple[int, str]:
    return (candidate["risk_rank"], candidate["stable_key"])


def _stable_sort_key(candidate: dict[str, Any]) -> tuple[str, int]:
    return (candidate["stable_key"], candidate["index"])


def _stable_key(file_record: dict[str, Any], index: int) -> str:
    basis = "|".join(str(file_record.get(field) or "") for field in ["sha256", "relative_path", "filename"])
    if not basis.strip("|"):
        basis = str(index)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _selection_reason(candidate: dict[str, Any]) -> str:
    highest = candidate["highest_severity"]
    if highest in RISK_TIER_BY_SEVERITY:
        return f"risk_weighted_{highest.lower()}_finding"
    if candidate["problematic"]:
        return "risk_weighted_problematic_record"
    return "deterministic_baseline_sample"


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON must be an object.")
    return payload
