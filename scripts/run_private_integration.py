#!/usr/bin/env python3
"""Local-only private sample integration runner.

This script is an aggregate-only public entry point for running real private
image directories inside an internal environment. Row-level reports remain in
the caller-provided output root and must not be uploaded.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from archive_scan_qc.acceptance import build_acceptance_summary  # noqa: E402
from archive_scan_qc.benchmark import _environment  # noqa: E402
from archive_scan_qc.benchmark import run_benchmark  # noqa: E402
from archive_scan_qc.processing import _load_numpy  # noqa: E402
from archive_scan_qc.run_plan import PlanBatch, RunPlan, run_plan  # noqa: E402


SUMMARY_JSON = "private_integration_summary.json"
FORBIDDEN_KEYS = {
    "source_path",
    "relative_path",
    "sha256",
    "filename",
    "thumbnail",
    "reviewer_notes",
}
@dataclass(frozen=True)
class PrivateIntegrationResult:
    summary: dict[str, Any]
    summary_path: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_private_integration.py",
        description="Run local private scan QC integration and emit only aggregate public summary fields.",
    )
    parser.add_argument(
        "--input",
        default=os.environ.get("PRIVATE_INTEGRATION_INPUT"),
        type=Path,
        help="Private image input directory. May also be set with PRIVATE_INTEGRATION_INPUT.",
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("PRIVATE_INTEGRATION_OUT"),
        type=Path,
        help="Private output root. May also be set with PRIVATE_INTEGRATION_OUT.",
    )
    parser.add_argument("--project", default="private-integration", help="Non-sensitive project identifier.")
    parser.add_argument("--batch", default="private-batch", help="Non-sensitive batch identifier.")
    parser.add_argument("--workers", default=1, type=_positive_int, help="Local worker count.")
    parser.add_argument(
        "--process-images",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable derivative image processing in the private output root.",
    )
    parser.add_argument("--auto-crop", action="store_true", help="Enable conservative auto-crop during processing.")
    parser.add_argument("--deskew", action="store_true", help="Enable conservative deskew during processing.")
    parser.add_argument("--trim-dark-border", action="store_true", help="Enable conservative dark-border trim.")
    parser.add_argument("--despeckle", action="store_true", help="Enable conservative despeckle.")
    parser.add_argument("--normalize-tones", action="store_true", help="Enable conservative gray/dark page tone normalization.")
    parser.add_argument("--lighten-edge-shadow", action="store_true", help="Enable conservative narrow edge-shadow lightening.")
    parser.add_argument("--lighten-background-stains", action="store_true", help="Enable conservative light background stain lightening.")
    parser.add_argument(
        "--despeckle-backend",
        choices=("fallback", "numpy"),
        default="fallback",
        help="Despeckle processing backend. Defaults to conservative fallback; numpy is opt-in.",
    )
    parser.add_argument("--resume-processing", action="store_true", help="Resume derivative processing.")
    parser.add_argument(
        "--reuse-scan-measurements",
        action="store_true",
        help="Reuse complete scan-stage processing measurements when available.",
    )
    parser.add_argument("--manifest-csv", default=None, type=Path, help="Optional private manifest CSV.")
    parser.add_argument("--rules-profile", default=None, type=Path, help="Optional private rules profile JSON.")
    parser.add_argument("--min-dpi", default=None, type=int, help="Optional minimum DPI override.")
    parser.add_argument("--name-pattern", default=None, help="Optional filename-stem regex.")
    parser.add_argument(
        "--benchmark-workers-list",
        default=None,
        help="Optional comma-separated worker counts for aggregate benchmark. Defaults to --workers.",
    )
    parser.add_argument("--benchmark-repeats", default=1, type=_positive_int, help="Benchmark repeat count.")
    parser.add_argument(
        "--skip-benchmark",
        action="store_true",
        help="Skip benchmark stage when only functional integration is needed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_private_integration(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"run_private_integration.py: error: {exc}\n")

    counts = result.summary["aggregate_counts"]
    throughput = result.summary["throughput"]
    acceptance = result.summary["acceptance"]
    print(f"Private integration output directory: {result.summary['output_dir_name']}")
    print(f"Total files: {counts['total_files']}")
    print(f"Openable files: {counts['openable_files']}")
    print(f"Findings: {counts['total_findings']} (P0={counts['p0_findings']}, P1={counts['p1_findings']}, P2={counts['p2_findings']})")
    print(f"Processed files: {counts['processing_processed_files']}")
    print(f"Failed batches: {counts['failed_batches']}")
    print(f"Scan files/min: {throughput['scan_files_per_minute']:.2f}")
    print(f"Processing files/min: {throughput['processing_files_per_minute']:.2f}")
    print(f"Acceptance status: {acceptance['status']}")
    print(f"Public aggregate summary: {SUMMARY_JSON}")
    return 0 if acceptance["passed"] else 1


def run_private_integration(args: argparse.Namespace) -> PrivateIntegrationResult:
    input_dir = _required_path(args.input, "--input or PRIVATE_INTEGRATION_INPUT")
    output_root = _required_path(args.out, "--out or PRIVATE_INTEGRATION_OUT")
    if not input_dir.exists() or not input_dir.is_dir():
        raise ValueError("Private input directory must exist and be a directory.")
    output_root.mkdir(parents=True, exist_ok=True)

    report_dir = output_root / "scan-reports"
    process_out = output_root / "processed-images" if args.process_images else None
    despeckle_backend = getattr(args, "despeckle_backend", "fallback")

    plan = RunPlan(
        project_id=args.project,
        batches=[
            PlanBatch(
                batch_id=args.batch,
                input_dir=input_dir,
                report_dir=report_dir,
                process_out=process_out,
                manifest_csv=args.manifest_csv,
                rules_profile=args.rules_profile,
                workers=args.workers,
                min_dpi=args.min_dpi,
                name_pattern=args.name_pattern,
                auto_crop=args.auto_crop,
                deskew=args.deskew,
                trim_dark_border=args.trim_dark_border,
                despeckle=args.despeckle,
                normalize_tones=getattr(args, "normalize_tones", False),
                lighten_edge_shadow=getattr(args, "lighten_edge_shadow", False),
                lighten_background_stains=getattr(args, "lighten_background_stains", False),
                despeckle_backend=despeckle_backend,
                resume_processing=args.resume_processing,
                reuse_scan_measurements=args.reuse_scan_measurements,
            )
        ],
    )
    run_plan_summary = run_plan(plan, output_root / "run-plan", continue_on_error=True)

    benchmark_summary = None
    if not args.skip_benchmark:
        benchmark_summary = run_benchmark(_benchmark_args(args, input_dir, output_root))

    summary = _public_summary(args, output_root, run_plan_summary, benchmark_summary)
    leaks = privacy_self_check(summary, forbidden_values=_forbidden_values(args, input_dir, output_root))
    summary["privacy_self_check"]["passed"] = not leaks
    summary["privacy_self_check"]["violation_count"] = len(leaks)
    summary["privacy_self_check"]["violations"] = leaks
    if leaks:
        summary["acceptance"]["passed"] = False
        summary["acceptance"]["status"] = "failed_privacy_self_check"

    leaks_after_update = privacy_self_check(summary, forbidden_values=_forbidden_values(args, input_dir, output_root))
    if leaks_after_update:
        raise ValueError("Privacy self-check found sensitive fields in public summary: " + ", ".join(leaks_after_update))

    summary_path = output_root / SUMMARY_JSON
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return PrivateIntegrationResult(summary=summary, summary_path=summary_path)


def privacy_self_check(payload: Any, *, forbidden_values: dict[str, str]) -> list[str]:
    violations: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text in FORBIDDEN_KEYS:
                    violations.append(f"forbidden key: {child_path}")
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
        elif isinstance(value, str) and value:
            for label, forbidden in forbidden_values.items():
                if forbidden and forbidden in value:
                    violations.append(f"forbidden {label} value at {path}")

    visit(payload, "")
    return violations


def _public_summary(
    args: argparse.Namespace,
    output_root: Path,
    run_plan_summary: dict[str, Any],
    benchmark_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    run_counts = run_plan_summary["summary"]
    benchmark_runs = benchmark_summary.get("runs", []) if benchmark_summary else []
    finding_rule_counts: dict[str, int] = {}
    for run in benchmark_runs:
        for rule, count in run.get("finding_rule_counts", {}).items():
            finding_rule_counts[rule] = finding_rule_counts.get(rule, 0) + int(count)
    benchmark_scan_throughput = _benchmark_recommended_throughput(benchmark_summary, "scan_only")
    benchmark_processing_throughput = _benchmark_recommended_throughput(benchmark_summary, "processing")
    processing_operation_timings = run_counts.get("processing_operation_timings", {})
    benchmark_operation_timings = _benchmark_operation_timings(benchmark_summary)
    despeckle_backend = _despeckle_backend_capability(args, processing_operation_timings)
    warning_items = _aggregate_warning_items(despeckle_backend)

    failed_batches = int(run_counts["failed_batches"])
    processing_failures = int(run_counts["processing_failed_files"])
    acceptance_summary = build_acceptance_summary(
        run_plan_summary=run_plan_summary,
        benchmark_results=benchmark_summary,
    )

    return {
        "schema_version": "scan-qc.private-integration-summary.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir_name": output_root.name,
        "privacy": {
            "aggregate_only": True,
            "row_level_artifacts": "Sensitive local evidence remains under the private output root; do not upload.",
            "omits": [
                "source paths",
                "source relative paths",
                "filenames",
                "content hashes",
                "thumbnails",
                "reviewer notes",
                "row-level findings",
                "image content",
            ],
        },
        "configuration": {
            "processing_enabled": bool(args.process_images),
            "workers": int(args.workers),
            "benchmark_enabled": benchmark_summary is not None,
            "benchmark_run_count": len(benchmark_runs),
            "despeckle_backend_requested": despeckle_backend["requested_backend"],
            "reuse_scan_measurements": bool(getattr(args, "reuse_scan_measurements", False)),
            "lighten_edge_shadow": bool(getattr(args, "lighten_edge_shadow", False)),
            "lighten_background_stains": bool(getattr(args, "lighten_background_stains", False)),
        },
        "despeckle_backend": despeckle_backend,
        "warning_item_count": len(warning_items),
        "warning_counts_by_code": _counts_by_code(warning_items),
        "warning_items": warning_items,
        "environment": _environment(),
        "aggregate_counts": {
            "total_files": int(run_counts["total_files"]),
            "openable_files": int(run_counts["openable_files"]),
            "total_findings": int(run_counts["total_findings"]),
            "p0_findings": int(run_counts["p0_findings"]),
            "p1_findings": int(run_counts["p1_findings"]),
            "p2_findings": int(run_counts["p2_findings"]),
            "processing_processed_files": int(run_counts["processing_processed_files"]),
            "processing_failed_files": int(run_counts["processing_failed_files"]),
            "processing_resumed_files": int(run_counts.get("processing_resumed_files", 0)),
            "processing_duplicate_reused_files": int(run_counts.get("processing_duplicate_reused_files", 0)),
            "processing_existing_derivative_reused_files": int(
                run_counts.get("processing_existing_derivative_reused_files", 0)
            ),
            "processing_scan_measurement_reused_files": int(
                run_counts.get("processing_scan_measurement_reused_files", 0)
            ),
            "failed_batches": failed_batches,
            "preflight_errors": int(run_counts["preflight_error_count"]),
        },
        "benchmark": {
            "source": "benchmark repeated worker runs",
            "run_count": len(benchmark_runs),
            "finding_rule_counts_repeated_runs": dict(sorted(finding_rule_counts.items())),
            "worker_sweep": _benchmark_worker_sweep(benchmark_summary),
        },
        "throughput": {
            "scan_elapsed_seconds": float(run_counts.get("scan_elapsed_seconds", 0.0)),
            "scan_files_per_minute": float(run_counts["scan_files_per_minute"]),
            "scan_openable_files_per_minute": float(run_counts["scan_openable_files_per_minute"]),
            "processing_elapsed_seconds": float(run_counts.get("processing_elapsed_seconds", 0.0)),
            "processing_files_per_minute": float(run_counts["processing_files_per_minute"]),
            "benchmark_scan_files_per_minute": benchmark_scan_throughput,
            "benchmark_processing_files_per_minute": benchmark_processing_throughput,
            "benchmark_basis": "best observed recommendation mean files/minute",
            "processing_operation_timings": processing_operation_timings if isinstance(processing_operation_timings, dict) else {},
            "benchmark_processing_operation_timings": benchmark_operation_timings,
        },
        "acceptance": {
            "passed": bool(acceptance_summary["pass"]),
            "status": acceptance_summary["status"],
            "source": "archive_scan_qc.acceptance.build_acceptance_summary",
            "summary": acceptance_summary,
        },
        "optional_steps": {
            "review_summary": "not_run",
            "acceptance_summary": "generated_from_available_aggregate_evidence",
        },
        "privacy_self_check": {
            "passed": False,
            "violation_count": None,
            "violations": [],
        },
    }


def _despeckle_backend_capability(args: argparse.Namespace, processing_operation_timings: Any) -> dict[str, Any]:
    requested_backend = getattr(args, "despeckle_backend", "fallback")
    numpy_available = _load_numpy() is not None
    despeckle_enabled = bool(getattr(args, "despeckle", False))
    processing_enabled = bool(getattr(args, "process_images", False))
    timing = (
        processing_operation_timings.get("despeckle", {})
        if isinstance(processing_operation_timings, dict)
        else {}
    )
    backend_counts = _backend_counts(timing.get("backend_counts") if isinstance(timing, dict) else None)
    effective_backend_mode = timing.get("backend_mode") if isinstance(timing, dict) else None
    if effective_backend_mode not in {"numpy", "fallback", "mixed", "disabled", "not_applicable", "unknown"}:
        if not processing_enabled or not despeckle_enabled:
            effective_backend_mode = "disabled"
        else:
            effective_backend_mode = "unknown"
    fallback_count = int(backend_counts["fallback"])
    processed_backend_count = int(sum(backend_counts.values()))
    requested_numpy_fallback_count = fallback_count if requested_backend == "numpy" else 0
    warning_codes: list[str] = []
    if requested_backend == "numpy" and despeckle_enabled and processing_enabled and not numpy_available:
        warning_codes.append("despeckle_numpy_unavailable_fallback")
    if (
        requested_backend == "numpy"
        and despeckle_enabled
        and processing_enabled
        and processed_backend_count > 0
        and backend_counts["numpy"] == 0
        and fallback_count == processed_backend_count
    ):
        warning_codes.append("despeckle_numpy_requested_all_fallback")
    return {
        "requested_backend": requested_backend,
        "effective_backend_mode": effective_backend_mode,
        "numpy_available": numpy_available,
        "backend_counts": backend_counts,
        "fallback_count": fallback_count,
        "requested_numpy_fallback_count": requested_numpy_fallback_count,
        "warning_codes": warning_codes,
    }


def _backend_counts(value: Any) -> dict[str, int]:
    result = {"numpy": 0, "fallback": 0, "not_applicable": 0, "unknown": 0}
    if not isinstance(value, dict):
        return result
    for key in result:
        count = value.get(key)
        if isinstance(count, int) and count >= 0:
            result[key] = count
    return result


def _aggregate_warning_items(despeckle_backend: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"code": code, "source": "despeckle_backend"}
        for code in despeckle_backend.get("warning_codes", [])
        if isinstance(code, str)
    ]


def _counts_by_code(items: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        code = item.get("code")
        if isinstance(code, str) and code:
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def _benchmark_worker_sweep(benchmark_summary: dict[str, Any] | None) -> dict[str, Any]:
    if not benchmark_summary:
        return {
            "enabled": False,
            "operation_timing_presence": False,
            "workers": [],
            "recommendation": None,
        }

    runs = [run for run in benchmark_summary.get("runs", []) if isinstance(run, dict)]
    workers = _benchmark_worker_points(runs)
    recommendation = _benchmark_worker_recommendation(workers)
    return {
        "enabled": True,
        "operation_timing_presence": any(point["processing"]["operation_timing_presence"] for point in workers),
        "workers": workers,
        "recommendation": recommendation,
    }


def _benchmark_worker_points(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    first_seen: dict[int, int] = {}
    for position, run in enumerate(runs):
        try:
            requested_workers = int(run["requested_workers"])
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault(requested_workers, []).append(run)
        first_seen.setdefault(requested_workers, position)

    points: list[dict[str, Any]] = []
    for requested_workers, worker_runs in grouped.items():
        scan_rates = _numeric_values(run.get("scan", {}).get("files_per_minute") for run in worker_runs)
        processing_rates = _numeric_values(
            run.get("processing", {}).get("processed_files_per_minute") for run in worker_runs
        )
        processing_failures = sum(
            int(run.get("processing", {}).get("failed_files", 0))
            for run in worker_runs
            if isinstance(run.get("processing"), dict)
        )
        operation_timings = [
            run.get("processing", {}).get("operation_timings")
            for run in worker_runs
            if isinstance(run.get("processing"), dict)
        ]
        operation_timing_presence = any(_has_operation_timing(timing) for timing in operation_timings)
        points.append(
            {
                "requested_workers": requested_workers,
                "run_count": len(worker_runs),
                "scan": {
                    "files_per_minute": _mean_or_none(scan_rates),
                },
                "processing": {
                    "processed_files_per_minute": _mean_or_none(processing_rates),
                    "failed_files": processing_failures,
                    "operation_timing_presence": operation_timing_presence,
                },
            }
        )
    return sorted(points, key=lambda point: (first_seen[point["requested_workers"]], point["requested_workers"]))


def _benchmark_worker_recommendation(workers: list[dict[str, Any]]) -> dict[str, Any] | None:
    processing_points = [
        point
        for point in workers
        if point["processing"]["processed_files_per_minute"] is not None
        and int(point["processing"]["failed_files"]) == 0
    ]
    metric = "processing_processed_files_per_minute"
    candidate_points = processing_points
    if not candidate_points:
        candidate_points = [point for point in workers if point["scan"]["files_per_minute"] is not None]
        metric = "scan_files_per_minute"
    if not candidate_points:
        return None

    metric_path = ("processing", "processed_files_per_minute") if metric.startswith("processing") else ("scan", "files_per_minute")
    best_rate = max(float(point[metric_path[0]][metric_path[1]]) for point in candidate_points)
    threshold = round(best_rate * 0.90, 6)
    recommended = min(
        (point for point in candidate_points if float(point[metric_path[0]][metric_path[1]]) >= threshold),
        key=lambda point: int(point["requested_workers"]),
    )
    return {
        "requested_workers": int(recommended["requested_workers"]),
        "metric": metric,
        "files_per_minute": float(recommended[metric_path[0]][metric_path[1]]),
        "best_observed_files_per_minute": round(best_rate, 2),
        "conservative_threshold_ratio": 0.90,
        "basis": (
            "Select the lowest worker count within 90% of the best observed aggregate throughput "
            "with zero processing failures when processing evidence is available; otherwise use scan throughput. "
            "This favors hardware headroom over assuming the maximum-throughput worker count is always best."
        ),
    }


def _numeric_values(values: Any) -> list[float]:
    parsed: list[float] = []
    for value in values:
        numeric = _optional_float(value)
        if numeric is not None:
            parsed.append(numeric)
    return parsed


def _mean_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _has_operation_timing(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for timing in value.values():
        if isinstance(timing, dict) and (
            isinstance(timing.get("elapsed_seconds"), int | float) or isinstance(timing.get("file_count"), int)
        ):
            return True
    return False


def _benchmark_operation_timings(benchmark_summary: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not benchmark_summary:
        return {}
    operation_names = [
        "auto_crop",
        "deskew",
        "trim_dark_border",
        "despeckle",
        "normalize_tones",
        "lighten_edge_shadow",
        "lighten_background_stains",
    ]
    totals: dict[str, dict[str, Any]] = {}
    for operation in operation_names:
        elapsed_seconds = 0.0
        file_count = 0
        enabled = False
        for run in benchmark_summary.get("runs", []):
            if not isinstance(run, dict):
                continue
            operations = run.get("operations")
            if isinstance(operations, dict):
                enabled = enabled or operations.get(operation) is True
            processing = run.get("processing")
            if not isinstance(processing, dict):
                continue
            operation_timings = processing.get("operation_timings")
            if not isinstance(operation_timings, dict):
                continue
            timing = operation_timings.get(operation)
            if not isinstance(timing, dict):
                continue
            if isinstance(timing.get("elapsed_seconds"), int | float):
                elapsed_seconds += float(timing["elapsed_seconds"])
            if isinstance(timing.get("file_count"), int):
                file_count += int(timing["file_count"])
        elapsed_seconds = round(elapsed_seconds, 6)
        totals[operation] = {
            "enabled": enabled,
            "file_count": file_count,
            "elapsed_seconds": elapsed_seconds,
            "files_per_minute": _files_per_minute(file_count, elapsed_seconds),
            "average_seconds_per_file": round(elapsed_seconds / file_count, 6) if file_count else None,
        }
        if operation == "deskew":
            totals[operation].update(_benchmark_deskew_audit_timings(benchmark_summary))
    return totals


def _benchmark_deskew_audit_timings(benchmark_summary: dict[str, Any]) -> dict[str, int]:
    fields = (
        "reused_scan_measurement_files",
        "safe_skip_files",
        "projection_detection_files",
        "fallback_detection_files",
    )
    totals = {field: 0 for field in fields}
    for run in benchmark_summary.get("runs", []):
        if not isinstance(run, dict):
            continue
        processing = run.get("processing")
        if not isinstance(processing, dict):
            continue
        operation_timings = processing.get("operation_timings")
        if not isinstance(operation_timings, dict):
            continue
        timing = operation_timings.get("deskew")
        if not isinstance(timing, dict):
            continue
        for field in fields:
            value = timing.get(field)
            if isinstance(value, int):
                totals[field] += value
    return totals


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)


def _benchmark_recommended_throughput(benchmark_summary: dict[str, Any] | None, operation: str) -> float | None:
    if not benchmark_summary:
        return None
    recommendations = benchmark_summary.get("recommendations")
    if not isinstance(recommendations, dict):
        return _benchmark_best_observed_throughput(benchmark_summary, operation)
    recommendation = recommendations.get(operation)
    if not isinstance(recommendation, dict):
        return _benchmark_best_observed_throughput(benchmark_summary, operation)
    return _optional_float(recommendation.get("files_per_minute"))


def _benchmark_best_observed_throughput(benchmark_summary: dict[str, Any], operation: str) -> float | None:
    values: list[float] = []
    for run in benchmark_summary.get("runs", []):
        if not isinstance(run, dict):
            continue
        if operation == "scan_only":
            scan = run.get("scan")
            if isinstance(scan, dict):
                _append_optional_float(values, scan.get("files_per_minute"))
        elif operation == "processing":
            processing = run.get("processing")
            if isinstance(processing, dict):
                _append_optional_float(values, processing.get("processed_files_per_minute"))
    return max(values) if values else None


def _append_optional_float(values: list[float], value: Any) -> None:
    parsed = _optional_float(value)
    if parsed is not None:
        values.append(parsed)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _benchmark_args(args: argparse.Namespace, input_dir: Path, output_root: Path) -> argparse.Namespace:
    workers_list = _workers_list(args.benchmark_workers_list) if args.benchmark_workers_list else [args.workers]
    despeckle_backend = getattr(args, "despeckle_backend", "fallback")
    return argparse.Namespace(
        input=input_dir,
        out=output_root / "benchmark",
        process_out=(output_root / "benchmark-processed") if args.process_images else None,
        project=args.project,
        batch=args.batch,
        workers_list=workers_list,
        repeats=args.benchmark_repeats,
        scan_only=not args.process_images,
        auto_crop=args.auto_crop,
        deskew=args.deskew,
        trim_dark_border=args.trim_dark_border,
        despeckle=args.despeckle,
        normalize_tones=getattr(args, "normalize_tones", False),
        lighten_edge_shadow=getattr(args, "lighten_edge_shadow", False),
        lighten_background_stains=getattr(args, "lighten_background_stains", False),
        despeckle_backend=despeckle_backend,
        min_dpi=args.min_dpi,
        name_pattern=args.name_pattern,
        manifest_csv=args.manifest_csv,
        rules_profile=args.rules_profile,
        reuse_scan_measurements=getattr(args, "reuse_scan_measurements", False),
    )


def _required_path(value: Path | str | None, label: str) -> Path:
    if value is None or str(value).strip() == "":
        raise ValueError(f"Missing required {label}.")
    return Path(value).expanduser().resolve()


def _forbidden_values(args: argparse.Namespace, input_dir: Path, output_root: Path) -> dict[str, str]:
    values = {
        "private input directory": str(input_dir),
        "private output root": str(output_root),
    }
    if args.manifest_csv:
        values["private manifest path"] = str(Path(args.manifest_csv).expanduser().resolve())
    if args.rules_profile:
        values["private rules profile path"] = str(Path(args.rules_profile).expanduser().resolve())
    return {label: value for label, value in values.items() if value and value not in {"/", "."}}


def _positive_int(value: str | int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer.") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer.")
    return parsed


def _workers_list(value: str) -> list[int]:
    workers = [_positive_int(item.strip()) for item in value.split(",") if item.strip()]
    if not workers:
        raise ValueError("Benchmark workers list must contain at least one positive integer.")
    return workers


if __name__ == "__main__":
    raise SystemExit(main())
