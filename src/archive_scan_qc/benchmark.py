"""Privacy-safe aggregate benchmark runner for archive scan QC."""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import sys
from typing import Any, Iterable

from PIL import Image

from .processing import ProcessingOptions, process_images
from .rules import RulesProfileError, load_rules_profile
from .scanner import ScanConfig, scan_batch


BENCHMARK_JSON = "benchmark_results.json"
BENCHMARK_CSV = "benchmark_results.csv"
DIMINISHING_RETURNS_THRESHOLD_RATIO = 0.10
COMPARISON_PLAN_VERSION = "scan-qc.performance-comparison-plan.v1"
PROCESSING_OPERATION_TIMING_NAMES = (
    "auto_crop",
    "deskew",
    "trim_dark_border",
    "scanner_gutter_trim",
    "despeckle",
    "normalize_tones",
    "normalize_paper_color_cast",
    "lighten_edge_shadow",
    "lighten_corner_shadows",
    "lighten_background_stains",
    "lighten_fold_shadows",
    "level_illumination_gradient",
    "clean_bleed_through",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
)
PROCESSING_OPERATION_TIMING_REQUIRED_FIELDS = (
    "enabled",
    "file_count",
    "elapsed_seconds",
    "files_per_minute",
    "average_seconds_per_file",
)
PROCESSING_OPERATION_TIMING_DIAGNOSTIC_SECONDS_PER_FILE = {
    "deskew": 0.15,
    "trim_dark_border": 0.15,
    "scanner_gutter_trim": 0.15,
    "auto_crop": 0.2,
    "despeckle": 0.25,
    "normalize_tones": 0.25,
    "normalize_paper_color_cast": 0.25,
    "lighten_edge_shadow": 0.25,
    "lighten_corner_shadows": 0.25,
    "lighten_background_stains": 0.35,
    "lighten_fold_shadows": 0.35,
    "level_illumination_gradient": 0.35,
    "clean_bleed_through": 0.35,
    "lighten_scanlines": 0.35,
    "enhance_faded_text": 0.35,
    "sharpen_text_edges": 0.35,
}


def positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be a positive integer.") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"{label} must be a positive integer.")
    return parsed


def workers_list(value: str) -> list[int]:
    if not value.strip():
        raise argparse.ArgumentTypeError("--workers-list must contain at least one positive integer.")
    workers: list[int] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            raise argparse.ArgumentTypeError("--workers-list must be a comma-separated list of positive integers.")
        workers.append(positive_int(item, "--workers-list"))
    return workers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive-scan-qc benchmark",
        description="Run privacy-safe aggregate scan QC benchmarks.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Image directory to benchmark.")
    parser.add_argument("--out", required=True, type=Path, help="Benchmark aggregate output directory.")
    parser.add_argument("--process-out", default=None, type=Path, help="Optional derivative-image output root.")
    parser.add_argument("--project", default="benchmark-project", help="Project identifier used internally for scanning.")
    parser.add_argument("--batch", default="benchmark-batch", help="Batch identifier used internally for scanning.")
    parser.add_argument("--workers-list", required=True, type=workers_list, help="Comma-separated worker counts, e.g. 1,2,4.")
    parser.add_argument("--repeats", default=1, type=lambda value: positive_int(value, "--repeats"))
    parser.add_argument("--scan-only", action="store_true", help="Only run scan checks; do not write derivative images.")
    parser.add_argument("--auto-crop", action="store_true", help="Enable conservative auto-crop during processing.")
    parser.add_argument("--deskew", action="store_true", help="Enable conservative deskew during processing.")
    parser.add_argument("--trim-dark-border", action="store_true", help="Enable conservative dark-border trim during processing.")
    parser.add_argument("--scanner-gutter-trim", action="store_true", help="Enable conservative light scanner gutter trim during processing.")
    parser.add_argument("--despeckle", action="store_true", help="Enable conservative despeckle during processing.")
    parser.add_argument("--normalize-tones", action="store_true", help="Enable conservative gray/dark page tone normalization.")
    parser.add_argument(
        "--normalize-paper-color-cast",
        action="store_true",
        help="Enable conservative mild uniform scanner color-cast normalization.",
    )
    parser.add_argument("--lighten-edge-shadow", action="store_true", help="Enable conservative narrow edge-shadow lightening.")
    parser.add_argument("--lighten-corner-shadows", action="store_true", help="Enable conservative smooth corner-shadow cleanup.")
    parser.add_argument("--lighten-background-stains", action="store_true", help="Enable conservative light background stain lightening.")
    parser.add_argument("--lighten-fold-shadows", action="store_true", help="Enable conservative narrow fold-shadow cleanup.")
    parser.add_argument("--level-illumination-gradient", action="store_true", help="Enable conservative smooth paper illumination-gradient leveling.")
    parser.add_argument("--clean-bleed-through", action="store_true", help="Enable conservative faint reverse-side ghost cleanup.")
    parser.add_argument("--lighten-scanlines", action="store_true", help="Enable conservative low-contrast scanline lightening.")
    parser.add_argument("--enhance-faded-text", action="store_true", help="Enable conservative low-contrast faded text enhancement.")
    parser.add_argument("--sharpen-text-edges", action="store_true", help="Enable conservative blurred text edge sharpening.")
    parser.add_argument(
        "--reuse-scan-measurements",
        action="store_true",
        help="Reuse complete scan-stage processing measurements when available.",
    )
    parser.add_argument(
        "--despeckle-backend",
        choices=("fallback", "numpy"),
        default="fallback",
        help="Despeckle processing backend. Defaults to conservative fallback; numpy is opt-in.",
    )
    parser.add_argument("--min-dpi", default=None, type=int, help="Minimum acceptable DPI.")
    parser.add_argument("--name-pattern", default=None, help="Optional filename-stem regex.")
    parser.add_argument("--manifest-csv", default=None, type=Path, help="Optional manifest CSV with relative_path.")
    parser.add_argument("--rules-profile", default=None, type=Path, help="Optional local JSON rules profile.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run_benchmark(args)
    except (FileNotFoundError, NotADirectoryError, ValueError, RulesProfileError, OSError) as exc:
        parser.exit(1, f"archive-scan-qc benchmark: error: {exc}\n")
    return 0


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.out.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    process_root = (args.process_out or (output_dir / "processed")).resolve()

    rules_profile = load_rules_profile(args.rules_profile) if args.rules_profile else None
    if rules_profile and args.min_dpi is not None:
        rules_profile = replace(rules_profile, min_dpi=args.min_dpi)
    if rules_profile and args.name_pattern is not None:
        rules_profile = replace(rules_profile, name_pattern=args.name_pattern)

    started_at = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, Any]] = []
    despeckle_backend = getattr(args, "despeckle_backend", "fallback")
    payload = _payload(started_at, results)
    _write_results(payload, output_dir)

    run_index = 0
    for repeat_index in range(1, args.repeats + 1):
        for workers in args.workers_list:
            run_index += 1
            scan_out = output_dir / ".benchmark_work" / f"run-{run_index:03d}-scan"
            report = scan_batch(
                ScanConfig(
                    project_id=args.project,
                    batch_id=args.batch,
                    input_dir=args.input,
                    output_dir=scan_out,
                    min_dpi=args.min_dpi if args.min_dpi is not None else 200,
                    name_pattern=args.name_pattern,
                    manifest_csv=args.manifest_csv,
                    rules_profile=rules_profile,
                    workers=workers,
                )
            )
            processing_manifest = None
            if not args.scan_only:
                process_dir = process_root / f"run-{run_index:03d}-workers-{workers}"
                processing_manifest = process_images(
                    report,
                    args.input,
                    process_dir,
                    ProcessingOptions(
                        auto_crop=args.auto_crop,
                        deskew=args.deskew,
                        trim_dark_border=args.trim_dark_border,
                        scanner_gutter_trim=args.scanner_gutter_trim,
                        despeckle=args.despeckle,
                        normalize_tones=args.normalize_tones,
                        normalize_paper_color_cast=args.normalize_paper_color_cast,
                        lighten_edge_shadow=args.lighten_edge_shadow,
                        lighten_corner_shadows=args.lighten_corner_shadows,
                        lighten_background_stains=args.lighten_background_stains,
                        lighten_fold_shadows=getattr(args, "lighten_fold_shadows", False),
                        level_illumination_gradient=getattr(args, "level_illumination_gradient", False),
                        clean_bleed_through=args.clean_bleed_through,
                        lighten_scanlines=args.lighten_scanlines,
                        enhance_faded_text=args.enhance_faded_text,
                        sharpen_text_edges=getattr(args, "sharpen_text_edges", False),
                        despeckle_backend=despeckle_backend,
                        reuse_scan_measurements=getattr(args, "reuse_scan_measurements", False),
                        workers=workers,
                    ),
                )
                _remove_sensitive_processing_manifest(process_dir)

            results.append(_aggregate_run(run_index, repeat_index, workers, args, report, processing_manifest))
            payload = _payload(started_at, results)
            _write_results(payload, output_dir)

    shutil.rmtree(output_dir / ".benchmark_work", ignore_errors=True)
    payload = _payload(started_at, results, finished_at=datetime.now(timezone.utc).isoformat())
    _write_results(payload, output_dir)
    _print_recommendation_summary(payload["recommendations"])
    return payload


def _payload(started_at: str, results: list[dict[str, Any]], finished_at: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.benchmark.v1",
        "generated_at": finished_at or datetime.now(timezone.utc).isoformat(),
        "started_at": started_at,
        "finished_at": finished_at,
        "privacy": {
            "aggregate_only": True,
            "omits": [
                "source identifiers",
                "content hashes",
                "row-level quality metrics",
                "row-level manifests",
                "image_content",
                "preview images",
            ],
        },
        "environment": _environment(),
        "comparison_plan": _comparison_plan(results),
        "recommendations": _recommendations(results),
        "runs": results,
    }


def _comparison_plan(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": COMPARISON_PLAN_VERSION,
        "goal": (
            "Compare production paths by operator wait reduction, review burden, safe automatic processing "
            "quality, failure risk, and resource visibility."
        ),
        "privacy_boundary": {
            "synthetic_or_local_public_safe_inputs": True,
            "private_puersai_policy": (
                "Private puersai validation is a later orchestrator-only run. Do not publish private source "
                "images, paths, names, hashes, previews, OCR text, derivative images, or row-level results; "
                "publish aggregate JSON/CSV fields only."
            ),
        },
        "metrics": [
            "processed_images_per_minute",
            "scan_images_per_minute",
            "failures",
            "review_needed_counts_by_severity_and_rule",
            "memory_cpu_gpu_visibility",
            "quality_difference_summary",
        ],
        "paths": _comparison_paths(),
        "production_decision": _production_decision(runs),
    }


def _comparison_paths() -> list[dict[str, Any]]:
    return [
        {
            "id": "pillow_cpu_baseline",
            "label": "Current Python/Pillow CPU baseline",
            "status": "measured_by_default",
            "run_hint": "archive-scan-qc benchmark --despeckle-backend fallback",
            "quality_signal": "Reference aggregate finding counts and processing failures.",
            "next_action_rule": "Keep as baseline unless another path improves throughput without higher failures or review counts.",
        },
        {
            "id": "numpy_vectorized_hotspots",
            "label": "OpenCV/NumPy-style vectorized hotspots",
            "status": "optional_local_measurement",
            "run_hint": "archive-scan-qc benchmark --despeckle --despeckle-backend numpy",
            "quality_signal": "Compare operation timings, failures, and finding deltas against the Pillow baseline.",
            "next_action_rule": "Prioritize if hotspot timings fall materially and aggregate quality/failure counts stay stable.",
        },
        {
            "id": "libvips_streaming_io",
            "label": "libvips streaming IO/output path",
            "status": "candidate_not_required_for_baseline",
            "run_hint": "Use the same synthetic/local corpus and record libvips output timing in the same summary schema.",
            "quality_signal": "Compare output-path throughput, memory pressure, and derivative write failures.",
            "next_action_rule": "Prioritize if processing is IO-bound or memory pressure is the observed bottleneck.",
        },
        {
            "id": "gpu_model_providers",
            "label": "Optional GPU/model provider paths: ONNX Runtime or PaddleOCR",
            "status": "candidate_not_required_for_baseline",
            "run_hint": "Run only where provider dependencies are installed; baseline operation must not require GPU.",
            "quality_signal": "Compare review-needed counts, provider failures, GPU visibility, and safe-retouch confidence.",
            "next_action_rule": "Prioritize only when reduced manual review burden offsets setup and failure risk.",
        },
    ]


def _production_decision(runs: list[dict[str, Any]]) -> dict[str, Any]:
    recommendations = _recommendations(runs)
    scan = recommendations["scan_only"]
    processing = recommendations["processing"]
    if not runs:
        return {
            "worth_implementing_next": None,
            "reason": "No benchmark runs have completed yet.",
        }
    if processing:
        return {
            "worth_implementing_next": "processing_worker_tuning",
            "reason": (
                f"Best measured processing throughput is {processing['files_per_minute']} processed images/min "
                f"at requested workers={processing['best_requested_workers']}; compare candidate paths against this."
            ),
        }
    if scan:
        return {
            "worth_implementing_next": "scan_worker_tuning",
            "reason": (
                f"Best measured scan throughput is {scan['files_per_minute']} images/min at requested "
                f"workers={scan['best_requested_workers']}; processing candidates still need a measured run."
            ),
        }
    return {
        "worth_implementing_next": None,
        "reason": "Runs completed but did not expose comparable throughput metrics.",
    }


def _recommendations(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.benchmark.recommendations.v1",
        "generated_from_runs": len(runs),
        "basis": (
            "Recommendations are calculated from this benchmark run's aggregate throughput only. "
            "Choose the worker count with the highest mean files/minute, then review diminishing-return notes "
            "alongside local CPU, memory, and I/O observations."
        ),
        "diminishing_returns_threshold_ratio": DIMINISHING_RETURNS_THRESHOLD_RATIO,
        "scan_only": _operation_recommendation(
            runs,
            operation="scan-only",
            metric_path=("scan", "files_per_minute"),
            effective_workers_path=("effective_workers",),
            metric_name="scan_files_per_minute",
        ),
        "processing": _operation_recommendation(
            [run for run in runs if not run["scan_only"] and run["processing"]["enabled"]],
            operation="processing",
            metric_path=("processing", "processed_files_per_minute"),
            effective_workers_path=("processing", "effective_workers"),
            metric_name="processing_processed_files_per_minute",
        ),
    }


def _operation_recommendation(
    runs: list[dict[str, Any]],
    *,
    operation: str,
    metric_path: tuple[str, str],
    effective_workers_path: tuple[str, ...],
    metric_name: str,
) -> dict[str, Any] | None:
    points = _worker_points(runs, metric_path, effective_workers_path)
    if not points:
        return None

    best = max(points, key=lambda point: (point["files_per_minute"], -point["requested_workers"]))
    diminishing_notes = _diminishing_return_notes(points, operation)
    notes = []
    if diminishing_notes:
        notes.extend(diminishing_notes)
    else:
        notes.append(
            f"No adjacent worker step was below the {DIMINISHING_RETURNS_THRESHOLD_RATIO:.0%} "
            "diminishing-return threshold."
        )

    return {
        "operation": operation,
        "metric": metric_name,
        "best_requested_workers": best["requested_workers"],
        "best_effective_workers": best["effective_workers"],
        "files_per_minute": best["files_per_minute"],
        "run_count": sum(point["run_count"] for point in points),
        "selection_basis": (
            f"Highest mean {metric_name} among requested worker counts in this benchmark payload."
        ),
        "diminishing_returns": bool(diminishing_notes),
        "notes": notes,
        "workers": points,
    }


def _worker_points(
    runs: list[dict[str, Any]], metric_path: tuple[str, str], effective_workers_path: tuple[str, ...]
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    first_seen: dict[int, int] = {}
    for position, run in enumerate(runs):
        metric_value = run[metric_path[0]][metric_path[1]]
        if metric_value is None:
            continue
        requested_workers = int(run["requested_workers"])
        grouped.setdefault(requested_workers, []).append(run)
        first_seen.setdefault(requested_workers, position)

    points = []
    for requested_workers, worker_runs in grouped.items():
        throughputs = [float(run[metric_path[0]][metric_path[1]]) for run in worker_runs]
        effective_workers = [float(_nested_value(run, effective_workers_path)) for run in worker_runs]
        points.append(
            {
                "requested_workers": requested_workers,
                "effective_workers": _rounded_mean(effective_workers),
                "files_per_minute": round(sum(throughputs) / len(throughputs), 2),
                "run_count": len(worker_runs),
                "first_run_index": min(int(run["run_index"]) for run in worker_runs),
            }
        )
    return sorted(points, key=lambda point: (first_seen[point["requested_workers"]], point["requested_workers"]))


def _nested_value(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        value = value[key]
    return value


def _rounded_mean(values: list[float]) -> int | float:
    mean = round(sum(values) / len(values), 2)
    if mean.is_integer():
        return int(mean)
    return mean


def _diminishing_return_notes(points: list[dict[str, Any]], operation: str) -> list[str]:
    notes = []
    for previous, current in zip(points, points[1:]):
        previous_rate = previous["files_per_minute"]
        current_rate = current["files_per_minute"]
        if previous_rate <= 0:
            continue
        gain_ratio = (current_rate - previous_rate) / previous_rate
        if gain_ratio < DIMINISHING_RETURNS_THRESHOLD_RATIO:
            notes.append(
                f"{operation}: requested workers {previous['requested_workers']} -> "
                f"{current['requested_workers']} improved mean throughput by {gain_ratio:.1%}; "
                "verify CPU, memory, and disk I/O before selecting the higher setting."
            )
    return notes


def _environment() -> dict[str, Any]:
    memory_total_bytes = None
    if hasattr(os, "sysconf"):
        try:
            memory_total_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (OSError, ValueError):
            memory_total_bytes = None
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "pillow_version": Image.__version__,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": memory_total_bytes,
        "gpu": None,
        "executable": Path(sys.executable).name,
    }


def _aggregate_run(
    run_index: int,
    repeat_index: int,
    requested_workers: int,
    args: argparse.Namespace,
    report: dict[str, Any],
    processing_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = report["summary"]
    scan_performance = summary["performance"]
    processing_summary = processing_manifest["summary"] if processing_manifest else None
    processing_performance = processing_summary["performance"] if processing_summary else None
    return {
        "run_index": run_index,
        "repeat_index": repeat_index,
        "requested_workers": requested_workers,
        "effective_workers": scan_performance["effective_workers"],
        "worker_mode": scan_performance["mode"],
        "operations": _operations(args),
        "scan_only": args.scan_only,
        "total_files": summary["total_files"],
        "openable_files": summary["openable_files"],
        "finding_severity_counts": {
            "P0": summary["p0_findings"],
            "P1": summary["p1_findings"],
            "P2": summary["p2_findings"],
        },
        "finding_rule_counts": _rule_counts(report.get("findings", [])),
        "processing": {
            "enabled": processing_manifest is not None,
            "processed_files": processing_summary["processed_files"] if processing_summary else 0,
            "failed_files": processing_summary["failed_files"] if processing_summary else 0,
            "skipped_files": processing_summary["skipped_files"] if processing_summary else 0,
            "elapsed_seconds": processing_performance["elapsed_seconds"] if processing_performance else None,
            "processed_files_per_minute": (
                processing_performance["processed_files_per_minute"] if processing_performance else None
            ),
            "total_files_per_minute": processing_performance["total_files_per_minute"] if processing_performance else None,
            "effective_workers": processing_performance["effective_workers"] if processing_performance else None,
            "worker_mode": processing_performance["mode"] if processing_performance else None,
            "operation_timings": _processing_operation_timings(processing_manifest),
            "scan_measurement_reuse": _processing_scan_measurement_reuse(processing_manifest),
            "quality_regression": _processing_quality_regression(processing_manifest),
        },
        "scan": {
            "elapsed_seconds": scan_performance["elapsed_seconds"],
            "files_per_minute": scan_performance["files_per_minute"],
            "openable_files_per_minute": scan_performance["openable_files_per_minute"],
        },
    }


def _processing_operation_timings(processing_manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not processing_manifest:
        return {}
    summary_path = processing_manifest.get("summary")
    if isinstance(summary_path, dict):
        performance = summary_path.get("performance")
        if isinstance(performance, dict):
            timings = performance.get("operation_timings")
            if isinstance(timings, dict):
                return timings
    records = processing_manifest.get("files")
    if not isinstance(records, list):
        return {}
    timings: dict[str, Any] = {}
    for operation in PROCESSING_OPERATION_TIMING_NAMES:
        values = [
            float(record["operation_timings"][operation]["elapsed_seconds"])
            for record in records
            if isinstance(record, dict)
            and isinstance(record.get("operation_timings"), dict)
            and isinstance(record["operation_timings"].get(operation), dict)
            and isinstance(record["operation_timings"][operation].get("elapsed_seconds"), int | float)
        ]
        elapsed_seconds = round(sum(values), 6)
        timings[operation] = {
            "enabled": bool(values),
            "file_count": len(values),
            "elapsed_seconds": elapsed_seconds,
            "files_per_minute": _files_per_minute(len(values), elapsed_seconds),
            "average_seconds_per_file": round(elapsed_seconds / len(values), 6) if values else None,
        }
    return timings


def _processing_scan_measurement_reuse(processing_manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not processing_manifest:
        return {}
    summary = processing_manifest.get("summary")
    if isinstance(summary, dict):
        performance = summary.get("performance")
        if isinstance(performance, dict) and isinstance(performance.get("scan_measurement_reuse"), dict):
            return performance["scan_measurement_reuse"]
        reuse = summary.get("scan_measurement_reuse")
        if isinstance(reuse, dict):
            return reuse
    return {}


def _processing_quality_regression(processing_manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not processing_manifest:
        return _empty_quality_regression_summary("processing_not_enabled")
    records = processing_manifest.get("files")
    summary = processing_manifest.get("summary")
    if not isinstance(records, list) or not isinstance(summary, dict):
        return _empty_quality_regression_summary("processing_manifest_unavailable")

    audit_records = [
        record["processing_audit"]
        for record in records
        if isinstance(record, dict) and isinstance(record.get("processing_audit"), dict)
    ]
    guardrail_failed_files = sum(1 for audit in audit_records if audit.get("guardrail_failures"))
    processing_warning_files = sum(
        1 for record in records if isinstance(record, dict) and bool(record.get("processing_warnings"))
    )
    operation_timings = _processing_operation_timings(processing_manifest)
    timing_integrity = _operation_timing_integrity(operation_timings)
    timing_budget = _operation_timing_budget(operation_timings, _operation_timing_budget_config(summary))
    thresholds = _processing_quality_thresholds()
    algorithm_metrics = _repair_algorithm_metrics(audit_records, operation_timings)
    threshold_violations = _quality_threshold_violations(algorithm_metrics, thresholds)
    failed_files = _coerce_int(summary.get("failed_files"))
    status = (
        "pass"
        if failed_files == 0
        and guardrail_failed_files == 0
        and not threshold_violations
        and timing_integrity["status"] == "pass"
        and timing_budget["status"] != "failed"
        else "failed"
    )

    local_content_guard = _local_content_change_guard_summary(audit_records)
    processed_output_guard = _processed_output_safety_guard_summary(audit_records)
    return {
        "schema_version": "scan-qc.processing.quality-regression.v1",
        "aggregate_only": True,
        "status": status,
        "counts": {
            "total_files": _coerce_int(summary.get("total_files")),
            "processed_files": _coerce_int(summary.get("processed_files")),
            "failed_files": failed_files,
            "skipped_files": _coerce_int(summary.get("skipped_files")),
            "processing_warning_files": processing_warning_files,
            "guardrail_failed_files": guardrail_failed_files,
            "enhancement_changed_files": _enhancement_changed_files(audit_records),
            "cumulative_change_guard_checked_files": sum(
                1 for audit in audit_records if audit.get("cumulative_change_guard_checked") is True
            ),
            "cumulative_change_guard_reverted_files": sum(
                1 for audit in audit_records if audit.get("cumulative_change_guard_reverted") is True
            ),
            "local_content_change_guard_checked_files": sum(
                1 for audit in audit_records if audit.get("local_content_change_guard_checked") is True
            ),
            "local_content_change_guard_skipped_files": local_content_guard["skipped_files"],
            "local_content_change_guard_reverted_files": sum(
                1 for audit in audit_records if audit.get("local_content_change_guard_reverted") is True
            ),
            "processed_output_safety_guard_checked_files": processed_output_guard["checked_files"],
            "processed_output_safety_guard_reverted_files": processed_output_guard["reverted_files"],
            "processed_output_washout_guard_reverted_files": processed_output_guard["washout_reverted_files"],
            "processed_output_clipping_guard_reverted_files": processed_output_guard["clipping_reverted_files"],
            "processed_output_foreground_loss_guard_reverted_files": processed_output_guard[
                "foreground_loss_reverted_files"
            ],
        },
        "local_content_change_guard": local_content_guard,
        "processed_output_safety_guard": processed_output_guard,
        "thresholds": thresholds,
        "algorithm_metrics": algorithm_metrics,
        "operation_timing_integrity": timing_integrity,
        "operation_timing_budget": timing_budget,
        "slow_operations": _slow_operation_summary(operation_timings),
        "threshold_violations": threshold_violations,
        "privacy": {
            "aggregate_only": True,
            "contains_file_list": False,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_ocr": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_row_level_evidence": False,
        },
    }


def _empty_quality_regression_summary(reason: str) -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.processing.quality-regression.v1",
        "aggregate_only": True,
        "status": "not_applicable",
        "reason": reason,
        "counts": {
            "total_files": 0,
            "processed_files": 0,
            "failed_files": 0,
            "skipped_files": 0,
            "processing_warning_files": 0,
            "guardrail_failed_files": 0,
            "enhancement_changed_files": 0,
            "cumulative_change_guard_checked_files": 0,
            "cumulative_change_guard_reverted_files": 0,
            "local_content_change_guard_checked_files": 0,
            "local_content_change_guard_skipped_files": 0,
            "local_content_change_guard_reverted_files": 0,
            "processed_output_safety_guard_checked_files": 0,
            "processed_output_safety_guard_reverted_files": 0,
            "processed_output_washout_guard_reverted_files": 0,
            "processed_output_clipping_guard_reverted_files": 0,
            "processed_output_foreground_loss_guard_reverted_files": 0,
        },
        "local_content_change_guard": _local_content_change_guard_summary([]),
        "processed_output_safety_guard": _processed_output_safety_guard_summary([]),
        "thresholds": _processing_quality_thresholds(),
        "algorithm_metrics": _repair_algorithm_metrics([], {}),
        "operation_timing_integrity": _empty_operation_timing_integrity(reason),
        "operation_timing_budget": _empty_operation_timing_budget(reason),
        "slow_operations": [],
        "threshold_violations": [],
        "privacy": {
            "aggregate_only": True,
            "contains_file_list": False,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_ocr": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_row_level_evidence": False,
        },
    }


def _operation_timing_integrity(operation_timings: dict[str, Any]) -> dict[str, Any]:
    missing_operations: list[str] = []
    incomplete_operations: list[dict[str, Any]] = []
    for operation in PROCESSING_OPERATION_TIMING_NAMES:
        timing = operation_timings.get(operation)
        if not isinstance(timing, dict):
            missing_operations.append(operation)
            continue
        missing_fields = [field for field in PROCESSING_OPERATION_TIMING_REQUIRED_FIELDS if field not in timing]
        if missing_fields:
            incomplete_operations.append({"operation": operation, "missing_fields": missing_fields})
    timing_missing = bool(missing_operations or incomplete_operations)
    return {
        "aggregate_only": True,
        "status": "missing" if timing_missing else "pass",
        "missing_code": "missing_or_incomplete_processing_operation_timings" if timing_missing else None,
        "missing_operations": missing_operations,
        "incomplete_operations": incomplete_operations,
    }


def _empty_operation_timing_integrity(reason: str) -> dict[str, Any]:
    return {
        "aggregate_only": True,
        "status": "not_applicable",
        "missing_code": reason,
        "missing_operations": [],
        "incomplete_operations": [],
    }


def _operation_timing_budget_config(summary: dict[str, Any]) -> dict[str, Any]:
    performance = summary.get("performance")
    if not isinstance(performance, dict):
        return {
            "mode": "diagnostic",
            "source": "diagnostic_defaults",
            "budgets_seconds_per_file": dict(PROCESSING_OPERATION_TIMING_DIAGNOSTIC_SECONDS_PER_FILE),
        }
    config = performance.get("operation_timing_budget")
    if not isinstance(config, dict):
        return {
            "mode": "diagnostic",
            "source": "diagnostic_defaults",
            "budgets_seconds_per_file": dict(PROCESSING_OPERATION_TIMING_DIAGNOSTIC_SECONDS_PER_FILE),
        }
    mode = "blocking" if config.get("mode") == "blocking" or config.get("blocking") is True else "diagnostic"
    budgets = dict(PROCESSING_OPERATION_TIMING_DIAGNOSTIC_SECONDS_PER_FILE)
    configured_budgets = config.get("budgets_seconds_per_file")
    if isinstance(configured_budgets, dict):
        for operation, value in configured_budgets.items():
            if operation in PROCESSING_OPERATION_TIMING_NAMES:
                budget = _coerce_float(value)
                if budget is not None and budget > 0:
                    budgets[operation] = budget
    return {
        "mode": mode,
        "source": "calibrated" if mode == "blocking" else "diagnostic_defaults",
        "budgets_seconds_per_file": budgets,
    }


def _operation_timing_budget(operation_timings: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    budgets = config["budgets_seconds_per_file"]
    over_budget: list[dict[str, Any]] = []
    for operation in PROCESSING_OPERATION_TIMING_NAMES:
        timing = operation_timings.get(operation)
        if not isinstance(timing, dict) or not timing.get("enabled", False):
            continue
        budget = budgets[operation]
        average_seconds_per_file = _coerce_float(timing.get("average_seconds_per_file"))
        file_count = _coerce_int(timing.get("file_count"))
        elapsed_seconds = _coerce_float(timing.get("elapsed_seconds"))
        if average_seconds_per_file is None and isinstance(elapsed_seconds, int | float) and file_count > 0:
            average_seconds_per_file = round(elapsed_seconds / file_count, 6)
        if isinstance(average_seconds_per_file, int | float) and average_seconds_per_file > budget:
            over_budget.append(
                {
                    "operation": operation,
                    "average_seconds_per_file": round(float(average_seconds_per_file), 6),
                    "budget_seconds_per_file": budget,
                    "file_count": file_count,
                }
            )
    blocking = config["mode"] == "blocking"
    failed = blocking and bool(over_budget)
    return {
        "aggregate_only": True,
        "status": "failed" if failed else "pass",
        "mode": config["mode"],
        "budget_source": config["source"],
        "blocker_code": "processing_operation_timing_budget_exceeded" if failed else None,
        "diagnostic_code": (
            "processing_operation_timing_budget_diagnostic" if over_budget and not failed else None
        ),
        "budgets_seconds_per_file": dict(budgets),
        "over_budget_operations": over_budget,
    }


def _empty_operation_timing_budget(reason: str) -> dict[str, Any]:
    return {
        "aggregate_only": True,
        "status": "not_applicable",
        "mode": "not_applicable",
        "budget_source": reason,
        "blocker_code": reason,
        "diagnostic_code": None,
        "budgets_seconds_per_file": dict(PROCESSING_OPERATION_TIMING_DIAGNOSTIC_SECONDS_PER_FILE),
        "over_budget_operations": [],
    }


def _slow_operation_summary(operation_timings: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for operation in PROCESSING_OPERATION_TIMING_NAMES:
        timing = operation_timings.get(operation)
        if not isinstance(timing, dict):
            continue
        file_count = _coerce_int(timing.get("file_count"))
        elapsed_seconds = _coerce_float(timing.get("elapsed_seconds"))
        average_seconds_per_file = _coerce_float(timing.get("average_seconds_per_file"))
        if average_seconds_per_file is None and isinstance(elapsed_seconds, int | float) and file_count > 0:
            average_seconds_per_file = round(elapsed_seconds / file_count, 6)
        summaries.append(
            {
                "operation": operation,
                "enabled": bool(timing.get("enabled", False)),
                "file_count": file_count,
                "elapsed_seconds": elapsed_seconds,
                "average_seconds_per_file": average_seconds_per_file,
                "files_per_minute": _coerce_float(timing.get("files_per_minute")),
            }
        )
    summaries.sort(
        key=lambda item: (
            item["average_seconds_per_file"] if isinstance(item["average_seconds_per_file"], int | float) else -1,
            item["elapsed_seconds"] if isinstance(item["elapsed_seconds"], int | float) else -1,
            item["operation"],
        ),
        reverse=True,
    )
    return summaries[:limit]


def _local_content_change_guard_summary(audit_records: list[dict[str, Any]]) -> dict[str, Any]:
    checked_files = sum(1 for audit in audit_records if audit.get("local_content_change_guard_checked") is True)
    reverted_files = sum(1 for audit in audit_records if audit.get("local_content_change_guard_reverted") is True)
    warning_files = sum(
        1
        for audit in audit_records
        if audit.get("local_content_change_guard_action") in {"reverted_to_source", "warn_review"}
    )
    return {
        "aggregate_only": True,
        "checked_files": checked_files,
        "skipped_files": max(0, len(audit_records) - checked_files),
        "reverted_files": reverted_files,
        "warning_files": warning_files,
        "reason_distribution": _reason_counts(
            reason
            for audit in audit_records
            for reason in audit.get("local_content_change_guard_reasons", [])
            if isinstance(reason, str)
        ),
    }


def _processed_output_safety_guard_summary(audit_records: list[dict[str, Any]]) -> dict[str, Any]:
    checked_files = sum(1 for audit in audit_records if audit.get("processed_output_safety_guard_checked") is True)
    reverted_files = sum(1 for audit in audit_records if audit.get("processed_output_safety_guard_reverted") is True)
    warning_files = sum(
        1
        for audit in audit_records
        if audit.get("processed_output_safety_guard_action") in {"reverted_to_source", "warn_review"}
    )
    reason_lists = [
        audit.get("processed_output_safety_guard_reasons", [])
        for audit in audit_records
        if isinstance(audit.get("processed_output_safety_guard_reasons"), list)
    ]
    return {
        "aggregate_only": True,
        "checked_files": checked_files,
        "skipped_files": max(0, len(audit_records) - checked_files),
        "reverted_files": reverted_files,
        "warning_files": warning_files,
        "washout_reverted_files": sum(
            1
            for reasons in reason_lists
            if "near_white_saturation" in reasons or "bright_page_washout" in reasons
        ),
        "clipping_reverted_files": sum(1 for reasons in reason_lists if "highlight_clipping" in reasons),
        "foreground_loss_reverted_files": sum(1 for reasons in reason_lists if "dark_foreground_loss" in reasons),
        "reason_code_distribution": _reason_counts(
            audit.get("processed_output_safety_guard_reason_code")
            for audit in audit_records
            if isinstance(audit.get("processed_output_safety_guard_reason_code"), str)
        ),
        "reason_distribution": _reason_counts(
            reason
            for reasons in reason_lists
            for reason in reasons
            if isinstance(reason, str)
        ),
    }


def _processing_quality_thresholds() -> dict[str, float]:
    defaults = ProcessingOptions()
    return {
        "max_size_change_ratio": defaults.audit_max_size_change_ratio,
        "max_pixel_change_ratio": defaults.audit_max_pixel_change_ratio,
        "max_brightness_delta": defaults.audit_max_brightness_delta,
        "max_contrast_delta": defaults.audit_max_contrast_delta,
        "max_crop_ratio": defaults.audit_max_crop_ratio,
        "max_trim_margin_ratio": defaults.audit_max_trim_margin_ratio,
        "max_despeckle_pixel_ratio": defaults.audit_max_despeckle_pixel_ratio,
        "max_cumulative_change_score": defaults.audit_max_cumulative_change_score,
        "max_cumulative_pixel_change_ratio": defaults.audit_max_cumulative_pixel_change_ratio,
        "max_cumulative_brightness_delta": defaults.audit_max_cumulative_brightness_delta,
        "max_cumulative_contrast_delta": defaults.audit_max_cumulative_contrast_delta,
        "max_cumulative_crop_ratio": defaults.audit_max_cumulative_crop_ratio,
        "max_cumulative_candidate_pixel_ratio": defaults.audit_max_cumulative_candidate_pixel_ratio,
        "max_local_content_changed_ratio": defaults.audit_max_local_content_changed_ratio,
        "max_local_content_tile_changed_ratio": defaults.audit_max_local_content_tile_changed_ratio,
        "max_edge_content_changed_ratio": defaults.audit_max_edge_content_changed_ratio,
        "max_processed_near_white_ratio": defaults.audit_max_processed_near_white_ratio,
        "max_processed_near_white_delta": defaults.audit_max_processed_near_white_delta,
        "max_processed_highlight_clip_ratio": defaults.audit_max_processed_highlight_clip_ratio,
        "max_processed_highlight_clip_delta": defaults.audit_max_processed_highlight_clip_delta,
        "max_processed_bright_page_delta": defaults.audit_max_processed_bright_page_delta,
        "max_processed_dark_pixel_loss_ratio": defaults.audit_max_processed_dark_pixel_loss_ratio,
        "max_processed_dark_pixel_lift_ratio": defaults.audit_max_processed_dark_pixel_lift_ratio,
        "max_processed_full_page_change_ratio": defaults.audit_max_processed_full_page_change_ratio,
        "max_deskew_degrees": defaults.deskew_max_degrees,
        "max_tone_background_delta": defaults.audit_max_brightness_delta,
        "max_tone_contrast_delta": defaults.audit_max_contrast_delta,
        "max_tone_changed_pixel_ratio": 1.0,
        "max_paper_color_cast_delta": 12.0,
        "max_paper_color_cast_brightness_delta": 4.0,
        "max_paper_color_cast_changed_pixel_ratio": 1.0,
        "max_paper_color_cast_candidate_pixel_ratio": 1.0,
        "max_edge_shadow_changed_pixel_ratio": 0.08,
        "max_corner_shadows_changed_pixel_ratio": 0.06,
        "max_corner_shadows_candidate_pixel_ratio": 0.10,
        "max_background_stains_changed_pixel_ratio": 0.08,
        "max_background_stains_candidate_pixel_ratio": 1.0,
        "max_fold_shadows_changed_pixel_ratio": 0.075,
        "max_fold_shadows_candidate_pixel_ratio": 0.12,
        "max_illumination_gradient_changed_pixel_ratio": 1.0,
        "max_illumination_gradient_candidate_pixel_ratio": 1.0,
        "max_bleed_through_changed_pixel_ratio": 0.045,
        "max_bleed_through_candidate_pixel_ratio": 0.065,
        "max_scanlines_changed_pixel_ratio": 0.08,
        "max_scanlines_candidate_pixel_ratio": 1.0,
        "max_faded_text_changed_pixel_ratio": 0.10,
        "max_faded_text_candidate_pixel_ratio": 0.18,
        "max_text_edges_changed_pixel_ratio": 0.08,
        "max_text_edges_candidate_pixel_ratio": 0.12,
    }


def _repair_algorithm_metrics(
    audit_records: list[dict[str, Any]], operation_timings: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    return {
        "deskew": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="deskew",
            changed_flag="deskewed",
            metrics={"abs_angle_degrees": "deskew_abs_angle_degrees"},
        ),
        "trim_dark_border": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="trim_dark_border",
            changed_flag="dark_border_trimmed",
            metrics={"max_trim_margin_ratio": "max_trim_margin_ratio"},
        ),
        "scanner_gutter_trim": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="scanner_gutter_trim",
            changed_flag="scanner_gutter_trimmed",
            metrics={"max_trim_margin_ratio": "scanner_gutter_max_trim_margin_ratio"},
        ),
        "auto_crop": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="auto_crop",
            changed_flag="cropped",
            metrics={"crop_ratio": "crop_ratio"},
        ),
        "despeckle": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="despeckle",
            changed_flag="despeckled",
            metrics={"pixel_ratio": "despeckle_pixel_ratio"},
        ),
        "normalize_tones": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="normalize_tones",
            changed_flag="tone_normalized",
            metrics={
                "background_delta": "tone_background_delta",
                "contrast_delta": "tone_contrast_delta",
                "changed_pixel_ratio": "tone_changed_pixel_ratio",
            },
        ),
        "normalize_paper_color_cast": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="normalize_paper_color_cast",
            changed_flag="paper_color_cast_normalized",
            metrics={
                "delta": "paper_color_cast_delta",
                "brightness_delta": "paper_color_cast_brightness_delta",
                "changed_pixel_ratio": "paper_color_cast_changed_pixel_ratio",
                "candidate_pixel_ratio": "paper_color_cast_candidate_pixel_ratio",
            },
        ),
        "lighten_edge_shadow": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="lighten_edge_shadow",
            changed_flag="edge_shadow_lightened",
            metrics={"delta": "edge_shadow_delta", "changed_pixel_ratio": "edge_shadow_changed_pixel_ratio"},
        ),
        "lighten_corner_shadows": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="lighten_corner_shadows",
            changed_flag="corner_shadows_lightened",
            metrics={
                "delta": "corner_shadows_delta",
                "changed_pixel_ratio": "corner_shadows_changed_pixel_ratio",
                "candidate_pixel_ratio": "corner_shadows_candidate_pixel_ratio",
            },
        ),
        "lighten_background_stains": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="lighten_background_stains",
            changed_flag="background_stains_lightened",
            metrics={
                "delta": "background_stains_delta",
                "changed_pixel_ratio": "background_stains_changed_pixel_ratio",
                "candidate_pixel_ratio": "background_stains_candidate_pixel_ratio",
            },
        ),
        "lighten_fold_shadows": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="lighten_fold_shadows",
            changed_flag="fold_shadows_lightened",
            metrics={
                "delta": "fold_shadows_delta",
                "changed_pixel_ratio": "fold_shadows_changed_pixel_ratio",
                "candidate_pixel_ratio": "fold_shadows_candidate_pixel_ratio",
            },
        ),
        "level_illumination_gradient": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="level_illumination_gradient",
            changed_flag="illumination_gradient_levelled",
            metrics={
                "correction_delta": "illumination_gradient_correction_delta",
                "changed_pixel_ratio": "illumination_gradient_changed_pixel_ratio",
                "candidate_pixel_ratio": "illumination_gradient_candidate_pixel_ratio",
            },
        ),
        "clean_bleed_through": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="clean_bleed_through",
            changed_flag="bleed_through_cleaned",
            metrics={
                "delta": "bleed_through_delta",
                "changed_pixel_ratio": "bleed_through_changed_pixel_ratio",
                "candidate_pixel_ratio": "bleed_through_candidate_pixel_ratio",
            },
        ),
        "lighten_scanlines": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="lighten_scanlines",
            changed_flag="scanlines_lightened",
            metrics={
                "delta": "scanlines_delta",
                "changed_pixel_ratio": "scanlines_changed_pixel_ratio",
                "candidate_pixel_ratio": "scanlines_candidate_pixel_ratio",
            },
        ),
        "enhance_faded_text": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="enhance_faded_text",
            changed_flag="faded_text_enhanced",
            metrics={
                "delta": "faded_text_delta",
                "changed_pixel_ratio": "faded_text_changed_pixel_ratio",
                "candidate_pixel_ratio": "faded_text_candidate_pixel_ratio",
            },
        ),
        "sharpen_text_edges": _algorithm_summary(
            audit_records,
            operation_timings,
            operation="sharpen_text_edges",
            changed_flag="text_edges_sharpened",
            metrics={
                "delta": "text_edges_delta",
                "changed_pixel_ratio": "text_edges_changed_pixel_ratio",
                "candidate_pixel_ratio": "text_edges_candidate_pixel_ratio",
            },
        ),
    }


def _algorithm_summary(
    audit_records: list[dict[str, Any]],
    operation_timings: dict[str, Any],
    *,
    operation: str,
    changed_flag: str,
    metrics: dict[str, str],
) -> dict[str, Any]:
    timing = operation_timings.get(operation)
    if not isinstance(timing, dict):
        timing = {}
    return {
        "enabled": bool(timing.get("enabled", False)),
        "changed_files": sum(1 for audit in audit_records if audit.get(changed_flag) is True),
        "file_count": _coerce_int(timing.get("file_count")),
        "elapsed_seconds": _coerce_float(timing.get("elapsed_seconds")),
        "files_per_minute": _coerce_float(timing.get("files_per_minute")),
        "metrics": {label: _aggregate_metric(audit_records, key) for label, key in metrics.items()},
    }


def _quality_threshold_violations(
    algorithm_metrics: dict[str, dict[str, Any]], thresholds: dict[str, float]
) -> list[dict[str, Any]]:
    checks = {
        ("deskew", "abs_angle_degrees"): "max_deskew_degrees",
        ("trim_dark_border", "max_trim_margin_ratio"): "max_trim_margin_ratio",
        ("scanner_gutter_trim", "max_trim_margin_ratio"): "max_trim_margin_ratio",
        ("auto_crop", "crop_ratio"): "max_crop_ratio",
        ("despeckle", "pixel_ratio"): "max_despeckle_pixel_ratio",
        ("normalize_tones", "background_delta"): "max_tone_background_delta",
        ("normalize_tones", "contrast_delta"): "max_tone_contrast_delta",
        ("normalize_tones", "changed_pixel_ratio"): "max_tone_changed_pixel_ratio",
        ("normalize_paper_color_cast", "delta"): "max_paper_color_cast_delta",
        ("normalize_paper_color_cast", "brightness_delta"): "max_paper_color_cast_brightness_delta",
        ("normalize_paper_color_cast", "changed_pixel_ratio"): "max_paper_color_cast_changed_pixel_ratio",
        ("normalize_paper_color_cast", "candidate_pixel_ratio"): "max_paper_color_cast_candidate_pixel_ratio",
        ("lighten_edge_shadow", "changed_pixel_ratio"): "max_edge_shadow_changed_pixel_ratio",
        ("lighten_corner_shadows", "changed_pixel_ratio"): "max_corner_shadows_changed_pixel_ratio",
        ("lighten_corner_shadows", "candidate_pixel_ratio"): "max_corner_shadows_candidate_pixel_ratio",
        ("lighten_background_stains", "changed_pixel_ratio"): "max_background_stains_changed_pixel_ratio",
        ("lighten_background_stains", "candidate_pixel_ratio"): "max_background_stains_candidate_pixel_ratio",
        ("lighten_fold_shadows", "changed_pixel_ratio"): "max_fold_shadows_changed_pixel_ratio",
        ("lighten_fold_shadows", "candidate_pixel_ratio"): "max_fold_shadows_candidate_pixel_ratio",
        ("level_illumination_gradient", "changed_pixel_ratio"): "max_illumination_gradient_changed_pixel_ratio",
        ("level_illumination_gradient", "candidate_pixel_ratio"): "max_illumination_gradient_candidate_pixel_ratio",
        ("clean_bleed_through", "changed_pixel_ratio"): "max_bleed_through_changed_pixel_ratio",
        ("clean_bleed_through", "candidate_pixel_ratio"): "max_bleed_through_candidate_pixel_ratio",
        ("lighten_scanlines", "changed_pixel_ratio"): "max_scanlines_changed_pixel_ratio",
        ("lighten_scanlines", "candidate_pixel_ratio"): "max_scanlines_candidate_pixel_ratio",
        ("enhance_faded_text", "changed_pixel_ratio"): "max_faded_text_changed_pixel_ratio",
        ("enhance_faded_text", "candidate_pixel_ratio"): "max_faded_text_candidate_pixel_ratio",
        ("sharpen_text_edges", "changed_pixel_ratio"): "max_text_edges_changed_pixel_ratio",
        ("sharpen_text_edges", "candidate_pixel_ratio"): "max_text_edges_candidate_pixel_ratio",
    }
    violations: list[dict[str, Any]] = []
    for (operation, metric_name), threshold_name in checks.items():
        metric = algorithm_metrics.get(operation, {}).get("metrics", {}).get(metric_name, {})
        observed = metric.get("max") if isinstance(metric, dict) else None
        threshold = thresholds[threshold_name]
        if isinstance(observed, int | float) and observed > threshold:
            violations.append(
                {
                    "operation": operation,
                    "metric": metric_name,
                    "max": round(float(observed), 6),
                    "threshold": threshold,
                }
            )
    return violations


def _enhancement_changed_files(audit_records: list[dict[str, Any]]) -> int:
    enhancement_flags = (
        "tone_normalized",
        "edge_shadow_lightened",
        "corner_shadows_lightened",
        "background_stains_lightened",
        "fold_shadows_lightened",
        "illumination_gradient_levelled",
        "bleed_through_cleaned",
        "scanlines_lightened",
        "faded_text_enhanced",
        "text_edges_sharpened",
    )
    return sum(1 for audit in audit_records if any(audit.get(flag) is True for flag in enhancement_flags))


def _reason_counts(reasons: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _coerce_int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _coerce_float(value: Any) -> float | None:
    return round(float(value), 6) if isinstance(value, int | float) else None


def _aggregate_metric(records: list[dict[str, Any]], key: str) -> dict[str, float | int | None]:
    values = [float(record[key]) for record in records if isinstance(record.get(key), int | float)]
    if not values:
        return {"count": 0, "average": None, "max": None}
    return {"count": len(values), "average": round(sum(values) / len(values), 6), "max": round(max(values), 6)}


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)


def _operations(args: argparse.Namespace) -> dict[str, bool | str]:
    return {
        "deskew": args.deskew,
        "auto_crop": args.auto_crop,
        "trim_dark_border": args.trim_dark_border,
        "scanner_gutter_trim": getattr(args, "scanner_gutter_trim", False),
        "despeckle": args.despeckle,
        "normalize_tones": getattr(args, "normalize_tones", False),
        "normalize_paper_color_cast": getattr(args, "normalize_paper_color_cast", False),
        "lighten_edge_shadow": getattr(args, "lighten_edge_shadow", False),
        "lighten_corner_shadows": getattr(args, "lighten_corner_shadows", False),
        "lighten_background_stains": getattr(args, "lighten_background_stains", False),
        "lighten_fold_shadows": getattr(args, "lighten_fold_shadows", False),
        "level_illumination_gradient": getattr(args, "level_illumination_gradient", False),
        "clean_bleed_through": getattr(args, "clean_bleed_through", False),
        "lighten_scanlines": getattr(args, "lighten_scanlines", False),
        "enhance_faded_text": getattr(args, "enhance_faded_text", False),
        "sharpen_text_edges": getattr(args, "sharpen_text_edges", False),
        "despeckle_backend": getattr(args, "despeckle_backend", "fallback"),
        "reuse_scan_measurements": getattr(args, "reuse_scan_measurements", False),
    }


def _rule_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        rule = str(finding.get("rule", "unknown"))
        counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items()))


def _write_results(payload: dict[str, Any], output_dir: Path) -> None:
    (output_dir / BENCHMARK_JSON).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / BENCHMARK_CSV).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_csv_fields())
        writer.writeheader()
        for run in payload["runs"]:
            writer.writerow(_csv_row(run, payload["environment"]))


def _csv_fields() -> list[str]:
    return [
        "run_index",
        "repeat_index",
        "requested_workers",
        "effective_workers",
        "worker_mode",
        "scan_only",
        "deskew",
        "auto_crop",
        "trim_dark_border",
        "scanner_gutter_trim",
        "despeckle",
        "normalize_tones",
        "normalize_paper_color_cast",
        "lighten_edge_shadow",
        "lighten_corner_shadows",
        "lighten_background_stains",
        "lighten_fold_shadows",
        "level_illumination_gradient",
        "clean_bleed_through",
        "lighten_scanlines",
        "enhance_faded_text",
        "sharpen_text_edges",
        "reuse_scan_measurements",
        "total_files",
        "openable_files",
        "p0_findings",
        "p1_findings",
        "p2_findings",
        "finding_rule_counts_json",
        "processed_files",
        "processing_failed_files",
        "processing_skipped_files",
        "processing_scan_measurement_reused_files",
        "processing_guardrail_failed_files",
        "processing_quality_status",
        "processing_enhancement_changed_files",
        "scan_elapsed_seconds",
        "scan_files_per_minute",
        "scan_openable_files_per_minute",
        "processing_elapsed_seconds",
        "processing_processed_files_per_minute",
        "processing_total_files_per_minute",
        "python_version",
        "platform",
        "cpu_count",
        "memory_total_bytes",
        "gpu",
    ]


def _csv_row(run: dict[str, Any], environment: dict[str, Any]) -> dict[str, Any]:
    operations = run["operations"]
    processing = run["processing"]
    quality = processing.get("quality_regression", {})
    quality_counts = quality.get("counts", {}) if isinstance(quality, dict) else {}
    scan = run["scan"]
    severities = run["finding_severity_counts"]
    return {
        "run_index": run["run_index"],
        "repeat_index": run["repeat_index"],
        "requested_workers": run["requested_workers"],
        "effective_workers": run["effective_workers"],
        "worker_mode": run["worker_mode"],
        "scan_only": run["scan_only"],
        "deskew": operations["deskew"],
        "auto_crop": operations["auto_crop"],
        "trim_dark_border": operations["trim_dark_border"],
        "scanner_gutter_trim": operations.get("scanner_gutter_trim", False),
        "despeckle": operations["despeckle"],
        "normalize_tones": operations["normalize_tones"],
        "normalize_paper_color_cast": operations.get("normalize_paper_color_cast", False),
        "lighten_edge_shadow": operations.get("lighten_edge_shadow", False),
        "lighten_corner_shadows": operations.get("lighten_corner_shadows", False),
        "lighten_background_stains": operations.get("lighten_background_stains", False),
        "lighten_fold_shadows": operations.get("lighten_fold_shadows", False),
        "level_illumination_gradient": operations.get("level_illumination_gradient", False),
        "clean_bleed_through": operations.get("clean_bleed_through", False),
        "lighten_scanlines": operations.get("lighten_scanlines", False),
        "enhance_faded_text": operations.get("enhance_faded_text", False),
        "sharpen_text_edges": operations.get("sharpen_text_edges", False),
        "reuse_scan_measurements": operations.get("reuse_scan_measurements", False),
        "total_files": run["total_files"],
        "openable_files": run["openable_files"],
        "p0_findings": severities["P0"],
        "p1_findings": severities["P1"],
        "p2_findings": severities["P2"],
        "finding_rule_counts_json": json.dumps(run["finding_rule_counts"], sort_keys=True),
        "processed_files": processing["processed_files"],
        "processing_failed_files": processing["failed_files"],
        "processing_skipped_files": processing["skipped_files"],
        "processing_scan_measurement_reused_files": processing.get("scan_measurement_reuse", {}).get(
            "files_with_any_reuse", 0
        ),
        "processing_guardrail_failed_files": quality_counts.get("guardrail_failed_files", 0),
        "processing_quality_status": quality.get("status") if isinstance(quality, dict) else None,
        "processing_enhancement_changed_files": quality_counts.get("enhancement_changed_files", 0),
        "scan_elapsed_seconds": scan["elapsed_seconds"],
        "scan_files_per_minute": scan["files_per_minute"],
        "scan_openable_files_per_minute": scan["openable_files_per_minute"],
        "processing_elapsed_seconds": processing["elapsed_seconds"],
        "processing_processed_files_per_minute": processing["processed_files_per_minute"],
        "processing_total_files_per_minute": processing["total_files_per_minute"],
        "python_version": environment["python_version"],
        "platform": environment["platform"],
        "cpu_count": environment["cpu_count"],
        "memory_total_bytes": environment["memory_total_bytes"],
        "gpu": environment["gpu"],
    }


def _print_recommendation_summary(recommendations: dict[str, Any]) -> None:
    scan = recommendations["scan_only"]
    if scan:
        print(
            "Benchmark recommendation: "
            f"scan workers={scan['best_requested_workers']} "
            f"(effective {scan['best_effective_workers']}), "
            f"{scan['files_per_minute']} files/min"
        )
    processing = recommendations["processing"]
    if processing:
        print(
            "Benchmark recommendation: "
            f"processing workers={processing['best_requested_workers']} "
            f"(effective {processing['best_effective_workers']}), "
            f"{processing['files_per_minute']} files/min"
        )
    if (scan and scan["diminishing_returns"]) or (processing and processing["diminishing_returns"]):
        print("Benchmark note: diminishing returns detected; see benchmark_results.json recommendations.")


def _remove_sensitive_processing_manifest(process_dir: Path) -> None:
    for filename in ["processing_manifest.json", "processing_retry_manifest.json"]:
        manifest_path = process_dir / filename
        if manifest_path.exists():
            manifest_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
