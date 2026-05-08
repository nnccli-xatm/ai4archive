"""Aggregate-only rule calibration summaries for scan QC reports."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .reports import REVIEW_STATUSES, build_review_summary, _load_review_rows
from .rule_registry import RULE_REGISTRY


CALIBRATION_JSON = "rules_calibration_summary.json"
THRESHOLD_RULES = {
    "dpi_minimum": ("min_dpi",),
    "name_pattern": ("name_pattern",),
    "quality_too_dark": ("quality", "dark_mean_threshold"),
    "quality_too_bright": ("quality", "bright_mean_threshold"),
    "quality_low_contrast": ("quality", "low_contrast_stddev_threshold"),
    "quality_suspected_blur": ("quality", "blur_laplacian_variance_threshold"),
    "quality_near_blank_page": ("quality", "blank_brightness_min"),
    "batch_orientation_consistency": None,
}
MIN_REVIEWED_FINDINGS_FOR_DIRECTION = 5


def write_rules_calibration_summary(
    report_paths: list[Path],
    output_path: Path,
    *,
    review_summary_paths: list[Path] | None = None,
    review_paths: list[Path] | None = None,
    suggested_profile_path: Path | None = None,
) -> tuple[Path, Path | None]:
    summary = build_rules_calibration_summary(
        report_paths,
        review_summary_paths=review_summary_paths or [],
        review_paths=review_paths or [],
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    written_profile = None
    if suggested_profile_path is not None:
        suggested = build_suggested_profile(summary)
        suggested_profile_path.parent.mkdir(parents=True, exist_ok=True)
        suggested_profile_path.write_text(json.dumps(suggested, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written_profile = suggested_profile_path
    return output_path, written_profile


def build_rules_calibration_summary(
    report_paths: list[Path],
    *,
    review_summary_paths: list[Path] | None = None,
    review_paths: list[Path] | None = None,
) -> dict[str, Any]:
    if not report_paths:
        raise ValueError("At least one --report path is required.")

    reports = [_load_report(path) for path in report_paths]
    review_summaries = [_load_review_summary(path) for path in review_summary_paths or []]
    review_summaries.extend(build_review_summary(_load_review_rows(path)) for path in review_paths or [])

    rule_counts: dict[str, int] = {}
    severity_counts_by_rule: dict[str, dict[str, int]] = {}
    overall_severity_counts: dict[str, int] = {}
    for report in reports:
        findings = report.get("findings", [])
        if not isinstance(findings, list):
            raise ValueError("Scan QC report field 'findings' must be a list.")
        for finding in findings:
            if not isinstance(finding, dict):
                raise ValueError("Scan QC report findings must be objects.")
            rule = _required_text(finding, "rule", "Scan QC report finding")
            severity = _required_text(finding, "severity", "Scan QC report finding")
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
            rule_severity_counts = severity_counts_by_rule.setdefault(rule, {})
            rule_severity_counts[severity] = rule_severity_counts.get(severity, 0) + 1
            overall_severity_counts[severity] = overall_severity_counts.get(severity, 0) + 1

    review_status_by_rule = _merged_rule_status_counts(review_summaries)
    rules_profile = _profile_summary(reports)
    rules = {}
    for rule in sorted(set(RULE_REGISTRY) | set(rule_counts) | set(review_status_by_rule)):
        trigger_count = rule_counts.get(rule, 0)
        severity_distribution = _ordered_counts(severity_counts_by_rule.get(rule, {}), ["P0", "P1", "P2"])
        status_distribution = _ordered_counts(review_status_by_rule.get(rule, {}), sorted(REVIEW_STATUSES))
        rules[rule] = {
            "title": RULE_REGISTRY[rule].title if rule in RULE_REGISTRY else rule,
            "trigger_count": trigger_count,
            "severity_distribution": severity_distribution,
            "review_status_distribution": status_distribution if any(status_distribution.values()) else None,
            "threshold_fields": _threshold_fields(rule),
            "recommendation": _recommendation(rule, trigger_count, status_distribution),
        }

    return {
        "schema_version": "scan-qc.rules-calibration.v1",
        "generated_at": _utc_now(),
        "sensitivity": (
            "Aggregate-only calibration summary. Safe fields omit file paths, filenames, hashes, "
            "OCR text, image content, thumbnails, row-level messages, and reviewer notes. "
            "The source scan_qc_report.json and review templates remain sensitive local evidence."
        ),
        "privacy": {
            "aggregate_only": True,
            "source_reports_sensitive": True,
            "omits": [
                "file paths",
                "filenames",
                "hashes",
                "reviewer notes",
                "OCR text",
                "image content",
                "thumbnails",
                "row-level messages",
            ],
        },
        "inputs": {
            "scan_qc_report_count": len(reports),
            "review_summary_count": len(review_summaries),
        },
        "totals": {
            "findings": sum(rule_counts.values()),
            "severity_distribution": _ordered_counts(overall_severity_counts, ["P0", "P1", "P2"]),
        },
        "rules_profile": rules_profile,
        "rules": rules,
        "approval": {
            "auto_applies_profile_changes": False,
            "required_next_step": "Human approval is required before editing or deploying a production rules profile.",
        },
    }


def build_suggested_profile(summary: dict[str, Any]) -> dict[str, Any]:
    current = deepcopy(summary.get("rules_profile", {}).get("current") or {})
    thresholds = current.get("thresholds", {}) if isinstance(current.get("thresholds", {}), dict) else {}
    profile = {
        "name": current.get("name", "rules-profile"),
        "version": current.get("version", "unknown"),
        "min_dpi": thresholds.get("min_dpi"),
        "name_pattern": thresholds.get("name_pattern"),
        "quality_thresholds": thresholds.get("quality", {}),
        "rules": current.get("rules", {}),
    }
    profile["name"] = f"{profile.get('name', 'rules-profile')}-suggested"
    profile["version"] = f"{profile.get('version', 'unknown')}-draft"
    profile["draft"] = True
    profile["suggested"] = True
    profile["schema_version"] = "scan-qc.rules-profile.suggested.v1"
    profile["generated_at"] = summary.get("generated_at")
    profile["basis"] = {
        "calibration_schema_version": summary.get("schema_version"),
        "aggregate_only": True,
        "does_not_modify_original_profile": True,
    }
    profile["calibration_recommendations"] = {
        rule: data.get("recommendation")
        for rule, data in sorted((summary.get("rules") or {}).items())
        if data.get("recommendation", {}).get("action") != "keep"
    }
    return profile


def _load_report(path: Path) -> dict[str, Any]:
    payload = _load_json_object(path, "Scan QC report")
    if payload.get("schema_version") != "scan-qc.phase1.v1":
        raise ValueError(f"Scan QC report has unsupported schema_version: {payload.get('schema_version')!r}.")
    return payload


def _load_review_summary(path: Path) -> dict[str, Any]:
    payload = _load_json_object(path, "Review summary")
    if payload.get("schema_version") != "scan-qc.review-summary.v1":
        raise ValueError(f"Review summary has unsupported schema_version: {payload.get('schema_version')!r}.")
    if not isinstance(payload.get("rule_status_counts", {}), dict):
        raise ValueError("Review summary field 'rule_status_counts' must be an object.")
    return payload


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON must be an object.")
    return payload


def _merged_rule_status_counts(review_summaries: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for summary in review_summaries:
        for rule, counts in summary.get("rule_status_counts", {}).items():
            if not isinstance(counts, dict):
                raise ValueError("Review summary field 'rule_status_counts' values must be objects.")
            target = merged.setdefault(str(rule), {status: 0 for status in sorted(REVIEW_STATUSES)})
            for status, count in counts.items():
                if status not in REVIEW_STATUSES:
                    raise ValueError(f"Review summary contains invalid review status '{status}'.")
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError("Review summary status counts must be non-negative integers.")
                target[status] += count
    return merged


def _profile_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
    profiles = []
    for report in reports:
        manifest = report.get("manifest", {})
        if isinstance(manifest, dict) and isinstance(manifest.get("rules_profile"), dict):
            profile = deepcopy(manifest["rules_profile"])
            profile.pop("source", None)
            profiles.append(profile)
    current = deepcopy(profiles[0]) if profiles else {}
    return {
        "current": current,
        "input_profile_count": len(profiles),
        "mixed_profiles": any(profile != current for profile in profiles[1:]),
        "fields": {
            "name": current.get("name"),
            "version": current.get("version"),
            "thresholds": current.get("thresholds", {}),
            "rules": current.get("rules", {}),
        },
    }


def _recommendation(rule: str, trigger_count: int, statuses: dict[str, int]) -> dict[str, Any]:
    reviewed_count = sum(statuses.values())
    if trigger_count == 0:
        action = "keep"
        rationale = "No triggers in the supplied aggregate reports."
    elif reviewed_count < MIN_REVIEWED_FINDINGS_FOR_DIRECTION:
        action = "need_more_samples"
        rationale = "Too few reviewed findings for a threshold direction."
    else:
        false_positive_ratio = statuses.get("false_positive", 0) / reviewed_count
        accepted_ratio = (statuses.get("accepted", 0) + statuses.get("needs_rescan", 0)) / reviewed_count
        if false_positive_ratio >= 0.70:
            action = "loosen"
            rationale = "Most reviewed findings for this rule were marked false_positive."
        elif accepted_ratio >= 0.80 and rule in THRESHOLD_RULES:
            action = "tighten"
            rationale = "Most reviewed findings were accepted or need rescan; consider a stricter project threshold after approval."
        else:
            action = "keep"
            rationale = "Reviewed dispositions do not justify a conservative threshold change."
    return {
        "action": action,
        "confidence": "low" if action == "need_more_samples" else "medium",
        "sample_floor": MIN_REVIEWED_FINDINGS_FOR_DIRECTION,
        "reviewed_findings": reviewed_count,
        "rationale": rationale,
        "auto_apply": False,
    }


def _threshold_fields(rule: str) -> list[str]:
    fields = THRESHOLD_RULES.get(rule)
    if fields is None:
        return []
    return [".".join(fields)]


def _ordered_counts(counts: dict[str, int], keys: list[str]) -> dict[str, int]:
    ordered = {key: int(counts.get(key, 0)) for key in keys}
    for key in sorted(counts):
        if key not in ordered:
            ordered[key] = int(counts[key])
    return ordered


def _required_text(payload: dict[str, Any], field: str, label: str) -> str:
    value = payload.get(field)
    if value is None or str(value) == "":
        raise ValueError(f"{label} missing required field '{field}'.")
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
