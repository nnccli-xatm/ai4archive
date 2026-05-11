"""Aggregate-only dry-run summary for optional deep-inspection candidates."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .deep_inspection_provider import (
    DeepInspectionProviderConfig,
    load_deep_inspection_provider_config,
)


DEEP_INSPECTION_CANDIDATE_SUMMARY_JSON = "deep_inspection_candidate_summary.json"
DEEP_INSPECTION_CANDIDATE_SCHEMA_VERSION = "scan-qc.deep-inspection-candidates.v1"
PRIVATE_OUTPUT_STATUS = "aggregate_public_safe"
SEVERITIES = ("P0", "P1", "P2", "unknown")


def build_deep_inspection_candidate_summary(
    *,
    scan_report: dict[str, Any] | None = None,
    processing_review_package: dict[str, Any] | None = None,
    provider_probe: dict[str, Any] | None = None,
    provider_config: DeepInspectionProviderConfig | None = None,
) -> dict[str, Any]:
    candidates_by_reason: Counter[str] = Counter()
    candidates_by_severity: Counter[str] = Counter({severity: 0 for severity in SEVERITIES})
    checks_passed: list[str] = []
    checks_failed: list[str] = []
    candidate_total = 0

    if scan_report is not None:
        checks_passed.append("scan_report_loaded")
        candidate_total += _add_scan_report_candidates(scan_report, candidates_by_reason, candidates_by_severity)
    else:
        checks_failed.append("scan_report_not_provided")

    if processing_review_package is not None:
        checks_passed.append("processing_review_package_loaded")
        candidate_total += _add_processing_review_candidates(processing_review_package, candidates_by_reason)

    provider_configured, provider_count, provider_status = _provider_status(provider_probe, provider_config)
    candidates_by_reason[f"provider_eligibility:{provider_status}"] += 0
    checks_passed.append("provider_eligibility_summarized")

    status = "pass" if candidate_total > 0 else "no_candidates"
    if "scan_report_not_provided" in checks_failed and processing_review_package is None:
        status = "no_inputs"

    return {
        "schema_version": DEEP_INSPECTION_CANDIDATE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "candidate_total": candidate_total,
        "candidates_by_reason": dict(sorted(candidates_by_reason.items())),
        "candidates_by_severity": dict(sorted(candidates_by_severity.items())),
        "provider_configured": provider_configured,
        "provider_count": provider_count,
        "checks_passed": sorted(checks_passed),
        "checks_failed": sorted(checks_failed),
        "privacy_status": PRIVATE_OUTPUT_STATUS,
        "privacy": {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_row_level_findings": False,
            "contains_reviewer_notes": False,
            "contains_manifests": False,
            "contains_derivative_image_references": False,
            "contains_source_roots": False,
            "network_calls": False,
        },
        "no_inference_run": True,
        "dry_run_only": True,
    }


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return payload


def load_deep_inspection_candidate_inputs(
    *,
    scan_report_path: Path | None,
    processing_review_package_path: Path | None,
    provider_probe_path: Path | None,
    provider_config_path: Path | None,
) -> dict[str, Any]:
    provider_config = load_deep_inspection_provider_config(provider_config_path) if provider_config_path else None
    return {
        "scan_report": load_json_object(scan_report_path, "scan report") if scan_report_path else None,
        "processing_review_package": (
            load_json_object(processing_review_package_path, "processing review package")
            if processing_review_package_path
            else None
        ),
        "provider_probe": load_json_object(provider_probe_path, "provider probe") if provider_probe_path else None,
        "provider_config": provider_config,
    }


def write_deep_inspection_candidate_summary(summary: dict[str, Any], output_path: Path) -> Path:
    path = (
        output_path / DEEP_INSPECTION_CANDIDATE_SUMMARY_JSON
        if output_path.is_dir() or output_path.suffix == ""
        else output_path
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _add_scan_report_candidates(
    scan_report: dict[str, Any],
    candidates_by_reason: Counter[str],
    candidates_by_severity: Counter[str],
) -> int:
    candidate_count = 0
    for finding in scan_report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        candidate_count += 1
        severity = str(finding.get("severity") or "unknown")
        if severity not in SEVERITIES:
            severity = "unknown"
        rule_bucket = _rule_bucket(finding.get("rule"))
        source_bucket = _source_bucket(finding.get("source"))
        uncertainty_bucket = _uncertainty_bucket(finding.get("confidence"))
        candidates_by_severity[severity] += 1
        candidates_by_reason[f"severity:{severity}"] += 1
        candidates_by_reason[f"rule_bucket:{rule_bucket}"] += 1
        candidates_by_reason[f"source:{source_bucket}"] += 1
        candidates_by_reason[f"uncertainty:{uncertainty_bucket}"] += 1
    return candidate_count


def _add_processing_review_candidates(processing_review_package: dict[str, Any], candidates_by_reason: Counter[str]) -> int:
    candidate_count = 0
    counted_failed = 0
    counted_guardrail_warnings = 0
    summary = processing_review_package.get("summary")
    if isinstance(summary, dict):
        for status, count in _safe_counts(summary.get("status_counts")).items():
            if status in {"needs_review", "warning"}:
                candidates_by_reason[f"processing_review_status:{status}"] += count
                candidate_count += count
        for key, reason in (
            ("failed_files", "processing_review_status:failed"),
            ("guardrail_warning_files", "processing_review_uncertainty:guardrail_warning"),
        ):
            count = _safe_int(summary.get(key))
            if count:
                candidates_by_reason[reason] += count
                candidate_count += count
                if key == "failed_files":
                    counted_failed += count
                else:
                    counted_guardrail_warnings += count
    groups = processing_review_package.get("groups")
    if isinstance(groups, dict):
        counted_by_group = {
            "failed": counted_failed,
            "guardrail_warnings": counted_guardrail_warnings,
        }
        for group_name in ("failed", "guardrail_warnings"):
            group = groups.get(group_name)
            if isinstance(group, dict):
                count = _safe_int(group.get("count"))
                incremental_count = max(0, count - counted_by_group[group_name])
                if incremental_count:
                    candidates_by_reason[f"processing_review_group:{group_name}"] += incremental_count
                    candidate_count += incremental_count
    return candidate_count


def _provider_status(
    provider_probe: dict[str, Any] | None,
    provider_config: DeepInspectionProviderConfig | None,
) -> tuple[bool, int, str]:
    if provider_probe is not None:
        configured = bool(provider_probe.get("configured", provider_probe.get("provider_configured", False)))
        provider_count = _safe_int(provider_probe.get("provider_count"))
        return configured, provider_count, "configured" if configured else "not_configured"
    if provider_config is not None:
        configured = provider_config.enabled and bool(provider_config.providers)
        provider_count = len(provider_config.providers) if provider_config.enabled else 0
        return configured, provider_count, "configured" if configured else "not_configured"
    return False, 0, "unknown"


def _rule_bucket(rule: Any) -> str:
    value = str(rule or "unknown").lower()
    for prefix in ("openability", "manifest", "quality", "name", "provider", "processing"):
        if value.startswith(prefix):
            return prefix
    return "other"


def _source_bucket(source: Any) -> str:
    value = str(source or "unknown").lower()
    return value if value in {"scanner", "manifest", "provider", "processing", "unknown"} else "other"


def _uncertainty_bucket(confidence: Any) -> str:
    if isinstance(confidence, (int, float)):
        if confidence < 0.75:
            return "low_confidence"
        if confidence < 0.95:
            return "medium_confidence"
        return "high_confidence"
    return "unknown_confidence"


def _safe_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): count for key, raw in value.items() if (count := _safe_int(raw)) > 0}


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    return 0
