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
from typing import Any

from PIL import Image

from .processing import ProcessingOptions, process_images
from .rules import RulesProfileError, load_rules_profile
from .scanner import ScanConfig, scan_batch


BENCHMARK_JSON = "benchmark_results.json"
BENCHMARK_CSV = "benchmark_results.csv"
DIMINISHING_RETURNS_THRESHOLD_RATIO = 0.10
COMPARISON_PLAN_VERSION = "scan-qc.performance-comparison-plan.v1"


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
    parser.add_argument("--despeckle", action="store_true", help="Enable conservative despeckle during processing.")
    parser.add_argument("--normalize-tones", action="store_true", help="Enable conservative gray/dark page tone normalization.")
    parser.add_argument("--lighten-edge-shadow", action="store_true", help="Enable conservative narrow edge-shadow lightening.")
    parser.add_argument("--lighten-background-stains", action="store_true", help="Enable conservative light background stain lightening.")
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
                        despeckle=args.despeckle,
                        normalize_tones=args.normalize_tones,
                        lighten_edge_shadow=args.lighten_edge_shadow,
                        lighten_background_stains=args.lighten_background_stains,
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
    operation_names = [
        "auto_crop",
        "deskew",
        "trim_dark_border",
        "despeckle",
        "normalize_tones",
        "lighten_edge_shadow",
        "lighten_background_stains",
    ]
    timings: dict[str, Any] = {}
    for operation in operation_names:
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
            "file_count": len(values),
            "elapsed_seconds": elapsed_seconds,
            "files_per_minute": _files_per_minute(len(values), elapsed_seconds),
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


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)


def _operations(args: argparse.Namespace) -> dict[str, bool | str]:
    return {
        "deskew": args.deskew,
        "auto_crop": args.auto_crop,
        "trim_dark_border": args.trim_dark_border,
        "despeckle": args.despeckle,
        "normalize_tones": getattr(args, "normalize_tones", False),
        "lighten_edge_shadow": getattr(args, "lighten_edge_shadow", False),
        "lighten_background_stains": getattr(args, "lighten_background_stains", False),
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
        "despeckle",
        "normalize_tones",
        "lighten_edge_shadow",
        "lighten_background_stains",
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
        "despeckle": operations["despeckle"],
        "normalize_tones": operations["normalize_tones"],
        "lighten_edge_shadow": operations.get("lighten_edge_shadow", False),
        "lighten_background_stains": operations.get("lighten_background_stains", False),
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
