"""Aggregate-only production acceptance gate summary."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACCEPTANCE_JSON = "acceptance_summary.json"
SCHEMA_VERSION = "scan-qc.acceptance-summary.v1"
EVIDENCE_TYPES = (
    "run_plan_summary",
    "review_summary",
    "processing_audit_summary",
    "benchmark_results",
    "aggregate_baseline_summary",
)


def write_acceptance_summary(
    *,
    output_path: Path,
    run_plan_summary_path: Path | None = None,
    review_summary_path: Path | None = None,
    processing_audit_summary_path: Path | None = None,
    benchmark_results_path: Path | None = None,
    aggregate_baseline_summary_path: Path | None = None,
    min_scan_files_per_minute: float | None = None,
    min_processing_files_per_minute: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    payload = build_acceptance_summary(
        run_plan_summary=_load_optional_json(run_plan_summary_path, "run_plan_summary"),
        review_summary=_load_optional_json(review_summary_path, "review_summary"),
        processing_audit_summary=_load_optional_json(processing_audit_summary_path, "processing_audit_summary"),
        benchmark_results=_load_optional_json(benchmark_results_path, "benchmark_results"),
        aggregate_baseline_summary=_load_optional_json(aggregate_baseline_summary_path, "aggregate_baseline_summary"),
        min_scan_files_per_minute=min_scan_files_per_minute,
        min_processing_files_per_minute=min_processing_files_per_minute,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path, payload


def build_acceptance_summary(
    *,
    run_plan_summary: dict[str, Any] | None = None,
    review_summary: dict[str, Any] | None = None,
    processing_audit_summary: dict[str, Any] | None = None,
    benchmark_results: dict[str, Any] | None = None,
    aggregate_baseline_summary: dict[str, Any] | None = None,
    min_scan_files_per_minute: float | None = None,
    min_processing_files_per_minute: float | None = None,
) -> dict[str, Any]:
    supplied = {
        "run_plan_summary": run_plan_summary,
        "review_summary": review_summary,
        "processing_audit_summary": processing_audit_summary,
        "benchmark_results": benchmark_results,
        "aggregate_baseline_summary": aggregate_baseline_summary,
    }
    if not any(payload is not None for payload in supplied.values()):
        raise ValueError("At least one aggregate evidence input is required.")

    warnings: list[str] = []
    blocking_items: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}

    for evidence_type in EVIDENCE_TYPES:
        payload = supplied[evidence_type]
        evidence[evidence_type] = {
            "provided": payload is not None,
            "schema_version": _safe_text(payload.get("schema_version")) if payload else None,
            "aggregate_only": _aggregate_only(payload) if payload else None,
        }
        if payload is None:
            warnings.append(f"{evidence_type} was not provided; related gate checks were not evaluated.")
        elif not _aggregate_only(payload):
            blocking_items.append(
                {
                    "code": f"{evidence_type}_not_aggregate_only",
                    "message": f"{evidence_type} does not declare aggregate-only privacy.",
                }
            )

    remaining_p0 = _int_from(review_summary, "remaining_p0")
    remaining_p1 = _int_from(review_summary, "remaining_p1")
    failed_batches = _run_plan_failed_batches(run_plan_summary)
    processing_failed_files = _processing_failed_files(
        run_plan_summary,
        processing_audit_summary,
        benchmark_results,
        aggregate_baseline_summary,
    )
    throughput = _throughput_summary(run_plan_summary, processing_audit_summary, benchmark_results, aggregate_baseline_summary)
    workers = _worker_summary(run_plan_summary, processing_audit_summary, benchmark_results, aggregate_baseline_summary)
    human_review = _human_review_summary(review_summary)
    privacy_self_check = _privacy_self_check_summary(aggregate_baseline_summary)
    cleanup = _cleanup_summary(aggregate_baseline_summary)

    _block_if_positive(blocking_items, "remaining_p0", remaining_p0, "Remaining P0 findings must be zero.")
    _block_if_positive(blocking_items, "remaining_p1", remaining_p1, "Remaining P1 findings must be zero.")
    _block_if_positive(blocking_items, "failed_batches", failed_batches, "Failed batch count must be zero.")
    _block_if_positive(
        blocking_items,
        "processing_failed_files",
        processing_failed_files,
        "Processing failed file count must be zero.",
    )
    if privacy_self_check["provided"] and not privacy_self_check["passed"]:
        blocking_items.append(
            {
                "code": "privacy_self_check_failed",
                "message": "Aggregate baseline privacy self-check must pass.",
                "observed": privacy_self_check["status"],
                "threshold": "pass",
            }
        )
    if cleanup["provided"]:
        if not cleanup["enabled"]:
            blocking_items.append(
                {
                    "code": "cleanup_retention_not_enabled",
                    "message": "Aggregate baseline cleanup must be enabled for release evidence.",
                    "observed": False,
                    "threshold": True,
                }
            )
        elif not cleanup["retained_public_summary_only"]:
            blocking_items.append(
                {
                    "code": "cleanup_retention_failed",
                    "message": "Aggregate baseline cleanup must retain only the public aggregate summary.",
                    "observed": cleanup,
                    "threshold": {"enabled": True, "retained_public_summary_only": True},
                }
            )

    if min_scan_files_per_minute is not None:
        observed = throughput["scan_files_per_minute"]["best_observed"]
        if observed is None:
            warnings.append("Minimum scan throughput was configured but no scan throughput evidence was available.")
        elif observed < min_scan_files_per_minute:
            blocking_items.append(
                {
                    "code": "scan_throughput_below_threshold",
                    "message": "Observed scan throughput is below the configured minimum.",
                    "observed": observed,
                    "threshold": min_scan_files_per_minute,
                }
            )
    if min_processing_files_per_minute is not None:
        observed = throughput["processing_files_per_minute"]["best_observed"]
        if observed is None:
            warnings.append("Minimum processing throughput was configured but no processing throughput evidence was available.")
        elif observed < min_processing_files_per_minute:
            blocking_items.append(
                {
                    "code": "processing_throughput_below_threshold",
                    "message": "Observed processing throughput is below the configured minimum.",
                    "observed": observed,
                    "threshold": min_processing_files_per_minute,
                }
            )

    passed = not blocking_items
    closure_gate_summary = _closure_gate_summary(
        remaining_p0=remaining_p0,
        remaining_p1=remaining_p1,
        human_review=human_review,
        can_complete_delivery=passed,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if passed else "fail",
        "pass": passed,
        "privacy": {
            "aggregate_only": True,
            "omits": [
                "source file identifiers",
                "source location strings",
                "content hashes",
                "thumbnails",
                "row-level findings",
                "human reviewer free text",
                "recognized text",
                "image content",
            ],
        },
        "evidence": evidence,
        "thresholds": {
            "remaining_p0_max": 0,
            "remaining_p1_max": 0,
            "failed_batches_max": 0,
            "processing_failed_files_max": 0,
            "min_scan_files_per_minute": min_scan_files_per_minute,
            "min_processing_files_per_minute": min_processing_files_per_minute,
            "privacy_self_check": "pass when aggregate_baseline_summary is provided",
            "cleanup_retention": "only aggregate_baseline_summary.json retained when aggregate_baseline_summary is provided",
        },
        "blocking_items": blocking_items,
        "warnings": warnings,
        "remaining": {
            "p0": remaining_p0,
            "p1": remaining_p1,
        },
        "closure_gate_summary": closure_gate_summary,
        "failed_batches": failed_batches,
        "processing_failed_files": processing_failed_files,
        "throughput": throughput,
        "workers": workers,
        "human_review": human_review,
        "privacy_self_check": privacy_self_check,
        "cleanup": cleanup,
        "recommended_next_steps": _recommended_next_steps(passed, blocking_items, warnings),
    }


def _load_optional_json(path: Path | None, label: str) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"{label} JSON could not be read.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return payload


def _aggregate_only(payload: dict[str, Any]) -> bool:
    privacy = payload.get("privacy")
    if isinstance(privacy, dict):
        return privacy.get("aggregate_only") is True
    sensitivity = str(payload.get("sensitivity", "")).lower()
    return "aggregate-only" in sensitivity or "aggregate only" in sensitivity


def _safe_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _int_from(payload: dict[str, Any] | None, key: str) -> int | None:
    if payload is None or key not in payload:
        return None
    return _coerce_int(payload.get(key))


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _run_plan_failed_batches(run_plan_summary: dict[str, Any] | None) -> int | None:
    if not run_plan_summary:
        return None
    summary = run_plan_summary.get("summary")
    if not isinstance(summary, dict):
        return None
    return _coerce_int(summary.get("failed_batches"))


def _processing_failed_files(
    run_plan_summary: dict[str, Any] | None,
    processing_audit_summary: dict[str, Any] | None,
    benchmark_results: dict[str, Any] | None,
    aggregate_baseline_summary: dict[str, Any] | None,
) -> int | None:
    values: list[int] = []
    run_summary = run_plan_summary.get("summary") if run_plan_summary else None
    if isinstance(run_summary, dict):
        value = _coerce_int(run_summary.get("processing_failed_files"))
        if value is not None:
            values.append(value)
    counts = processing_audit_summary.get("counts") if processing_audit_summary else None
    if isinstance(counts, dict):
        value = _coerce_int(counts.get("failed_files"))
        if value is not None:
            values.append(value)
    for run in _benchmark_runs(benchmark_results):
        processing = run.get("processing")
        if isinstance(processing, dict):
            value = _coerce_int(processing.get("failed_files"))
            if value is not None:
                values.append(value)
    baseline_counts = aggregate_baseline_summary.get("aggregate_counts") if aggregate_baseline_summary else None
    if isinstance(baseline_counts, dict):
        value = _coerce_int(baseline_counts.get("processing_failed_files"))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _throughput_summary(
    run_plan_summary: dict[str, Any] | None,
    processing_audit_summary: dict[str, Any] | None,
    benchmark_results: dict[str, Any] | None,
    aggregate_baseline_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    scan_values: list[float] = []
    processing_values: list[float] = []
    run_summary = run_plan_summary.get("summary") if run_plan_summary else None
    if isinstance(run_summary, dict):
        _append_float(scan_values, run_summary.get("scan_files_per_minute"))
        _append_float(processing_values, run_summary.get("processing_files_per_minute"))
    throughput = processing_audit_summary.get("throughput") if processing_audit_summary else None
    if isinstance(throughput, dict):
        _append_float(processing_values, throughput.get("processed_files_per_minute"))
    for run in _benchmark_runs(benchmark_results):
        scan = run.get("scan")
        if isinstance(scan, dict):
            _append_float(scan_values, scan.get("files_per_minute"))
        processing = run.get("processing")
        if isinstance(processing, dict):
            _append_float(processing_values, processing.get("processed_files_per_minute"))
    baseline_timings = aggregate_baseline_summary.get("stage_timings") if aggregate_baseline_summary else None
    if isinstance(baseline_timings, dict):
        scan = baseline_timings.get("scan")
        if isinstance(scan, dict):
            _append_float(scan_values, scan.get("files_per_minute"))
        processing = baseline_timings.get("processing")
        if isinstance(processing, dict):
            _append_float(processing_values, processing.get("processed_files_per_minute"))
    return {
        "scan_files_per_minute": _metric_summary(scan_values),
        "processing_files_per_minute": _metric_summary(processing_values),
    }


def _append_float(values: list[float], value: Any) -> None:
    parsed = _coerce_float(value)
    if parsed is not None:
        values.append(parsed)


def _metric_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"provided": False, "best_observed": None, "lowest_observed": None}
    return {
        "provided": True,
        "best_observed": round(max(values), 6),
        "lowest_observed": round(min(values), 6),
    }


def _worker_summary(
    run_plan_summary: dict[str, Any] | None,
    processing_audit_summary: dict[str, Any] | None,
    benchmark_results: dict[str, Any] | None,
    aggregate_baseline_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    scan_workers: list[int] = []
    processing_workers: list[int] = []
    for batch in run_plan_summary.get("batches", []) if run_plan_summary else []:
        if not isinstance(batch, dict):
            continue
        value = _coerce_int(batch.get("workers"))
        if value is not None:
            scan_workers.append(value)
    workers = processing_audit_summary.get("workers") if processing_audit_summary else None
    if isinstance(workers, dict):
        value = _coerce_int(workers.get("effective_workers"))
        if value is not None:
            processing_workers.append(value)
    for run in _benchmark_runs(benchmark_results):
        value = _coerce_int(run.get("effective_workers"))
        if value is not None:
            scan_workers.append(value)
        processing = run.get("processing")
        if isinstance(processing, dict):
            value = _coerce_int(processing.get("effective_workers"))
            if value is not None:
                processing_workers.append(value)
    baseline_workers = aggregate_baseline_summary.get("worker_settings") if aggregate_baseline_summary else None
    if isinstance(baseline_workers, dict):
        value = _coerce_int(baseline_workers.get("requested_workers"))
        if value is not None:
            scan_workers.append(value)
            processing_workers.append(value)
    return {
        "scan_effective_workers": _worker_range(scan_workers),
        "processing_effective_workers": _worker_range(processing_workers),
    }


def _worker_range(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"provided": False, "min": None, "max": None}
    return {"provided": True, "min": min(values), "max": max(values)}


def _human_review_summary(review_summary: dict[str, Any] | None) -> dict[str, Any]:
    if not review_summary:
        return {
            "provided": False,
            "acceptance_passed": None,
            "total_findings": None,
            "remaining_p0": None,
            "remaining_p1": None,
            "manually_handled_count": None,
            "status_counts": {},
            "closure_gate_summary": None,
        }
    status_counts = review_summary.get("status_counts")
    if not isinstance(status_counts, dict):
        status_counts = {}
    closure = review_summary.get("closure_gate_summary")
    return {
        "provided": True,
        "acceptance_passed": review_summary.get("acceptance_passed") is True,
        "total_findings": _coerce_int(review_summary.get("total_findings")),
        "remaining_p0": _coerce_int(review_summary.get("remaining_p0")),
        "remaining_p1": _coerce_int(review_summary.get("remaining_p1")),
        "manually_handled_count": _coerce_int(review_summary.get("manually_handled_count")),
        "status_counts": {str(key): _coerce_int(value) or 0 for key, value in sorted(status_counts.items())},
        "closure_gate_summary": closure if isinstance(closure, dict) else None,
    }


def _closure_gate_summary(
    *,
    remaining_p0: int | None,
    remaining_p1: int | None,
    human_review: dict[str, Any],
    can_complete_delivery: bool,
) -> dict[str, Any]:
    open_p0 = remaining_p0 if remaining_p0 is not None else 0
    open_p1 = remaining_p1 if remaining_p1 is not None else 0
    handled = _coerce_int(human_review.get("manually_handled_count")) if human_review.get("provided") else None
    return {
        "open_p0_count": open_p0,
        "open_p1_count": open_p1,
        "manually_handled_count": handled,
        "can_complete_delivery": can_complete_delivery,
        "operator_message_zh": (
            "P0/P1 问题已经有处理结论，可以进入验收。"
            if can_complete_delivery
            else "还有需要重扫/重新处理的图片，先处理后再完成导出。"
        ),
    }


def _benchmark_runs(benchmark_results: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not benchmark_results:
        return []
    runs = benchmark_results.get("runs")
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict)]


def _privacy_self_check_summary(aggregate_baseline_summary: dict[str, Any] | None) -> dict[str, Any]:
    if not aggregate_baseline_summary:
        return {"provided": False, "passed": None, "status": None, "violation_count": None}
    self_check = aggregate_baseline_summary.get("privacy_self_check")
    if not isinstance(self_check, dict):
        return {"provided": False, "passed": None, "status": None, "violation_count": None}
    status = _safe_text(self_check.get("status"))
    passed = self_check.get("passed") is True or status == "pass"
    return {
        "provided": True,
        "passed": passed,
        "status": status,
        "violation_count": _coerce_int(self_check.get("violation_count")),
    }


def _cleanup_summary(aggregate_baseline_summary: dict[str, Any] | None) -> dict[str, Any]:
    if not aggregate_baseline_summary:
        return {"provided": False, "enabled": None, "retained_public_summary_only": None}
    cleanup = aggregate_baseline_summary.get("cleanup")
    if not isinstance(cleanup, dict):
        return {"provided": False, "enabled": None, "retained_public_summary_only": None}
    removed = cleanup.get("removed_artifacts")
    preserved = cleanup.get("preserved_artifacts")
    retained_summary = cleanup.get("retained_public_summary")
    enabled = cleanup.get("enabled") is True
    retained_public_summary_only = (
        enabled
        and isinstance(removed, list)
        and len(removed) > 0
        and isinstance(preserved, list)
        and len(preserved) == 0
        and retained_summary == "aggregate_baseline_summary.json"
    )
    return {
        "provided": True,
        "enabled": enabled,
        "retained_public_summary_only": retained_public_summary_only,
        "removed_artifact_count": len(removed) if isinstance(removed, list) else None,
        "preserved_artifact_count": len(preserved) if isinstance(preserved, list) else None,
        "retained_public_summary": _safe_text(retained_summary),
    }


def _block_if_positive(blocking_items: list[dict[str, Any]], code: str, value: int | None, message: str) -> None:
    if value is not None and value > 0:
        blocking_items.append({"code": code, "message": message, "observed": value, "threshold": 0})


def _recommended_next_steps(
    passed: bool,
    blocking_items: list[dict[str, Any]],
    warnings: list[str],
) -> list[str]:
    if passed:
        steps = ["Proceed with batch delivery approval after local sign-off."]
        if warnings:
            steps.append("Review warnings and attach any omitted aggregate evidence before final release if required.")
        return steps
    codes = {item["code"] for item in blocking_items}
    steps: list[str] = []
    if "remaining_p0" in codes or "remaining_p1" in codes:
        steps.append("Resolve or disposition remaining P0/P1 review findings, then regenerate review_summary.json.")
    if "failed_batches" in codes:
        steps.append("Rerun failed batches and regenerate run_plan_summary.json.")
    if "processing_failed_files" in codes:
        steps.append("Retry derivative processing failures and regenerate processing audit evidence.")
    if "scan_throughput_below_threshold" in codes or "processing_throughput_below_threshold" in codes:
        steps.append("Tune worker count or capacity, rerun aggregate benchmark or baseline evidence, and repeat acceptance.")
    if "privacy_self_check_failed" in codes:
        steps.append("Regenerate aggregate baseline evidence after removing private fields from the public summary.")
    if "cleanup_retention_not_enabled" in codes or "cleanup_retention_failed" in codes:
        steps.append("Rerun the aggregate baseline with cleanup enabled and verify only aggregate_baseline_summary.json remains.")
    if any(code.endswith("_not_aggregate_only") for code in codes):
        steps.append("Replace non-aggregate evidence with approved aggregate-only summaries.")
    return steps or ["Review blocking items, regenerate aggregate evidence, and rerun acceptance."]
