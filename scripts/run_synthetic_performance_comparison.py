#!/usr/bin/env python3
"""Run a public-safe synthetic performance comparison for scan QC paths."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from PIL import Image, ImageDraw


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SUMMARY_JSON = "synthetic_performance_comparison.json"
FULL_CHAIN_VARIANT_ID = "full_conservative_repair_chain"
FULL_CHAIN_SYNTHETIC_BUDGET_SECONDS_PER_FILE = 5.0
REGRESSION_SIGNAL_OPERATIONS = (
    "deskew",
    "trim_dark_border",
    "scanner_gutter_trim",
    "auto_crop",
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
DESPECKLE_BACKEND_MODES = ("numpy", "fallback", "not_applicable", "unknown")
FULL_CHAIN_REPAIR_ARGS = (
    "--auto-crop",
    "--deskew",
    "--trim-dark-border",
    "--scanner-gutter-trim",
    "--despeckle",
    "--normalize-tones",
    "--normalize-paper-color-cast",
    "--lighten-edge-shadow",
    "--lighten-corner-shadows",
    "--lighten-background-stains",
    "--lighten-fold-shadows",
    "--level-illumination-gradient",
    "--clean-bleed-through",
    "--lighten-scanlines",
    "--enhance-faded-text",
    "--sharpen-text-edges",
    "--despeckle-backend",
    "fallback",
    "--reuse-scan-measurements",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_synthetic_performance_comparison.py",
        description="Create synthetic images and run public-safe benchmark variants.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output directory for synthetic data and summaries.")
    parser.add_argument("--image-count", default=8, type=_positive_int)
    parser.add_argument("--workers-list", default="1,2", help="Comma-separated worker counts for comparable runs.")
    parser.add_argument("--repeats", default=1, type=_positive_int)
    parser.add_argument("--width", default=640, type=_positive_int)
    parser.add_argument("--height", default=900, type=_positive_int)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = run_synthetic_comparison(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"run_synthetic_performance_comparison.py: error: {exc}\n")
    print(f"Synthetic comparison summary: {args.out.resolve() / SUMMARY_JSON}")
    decision = summary["production_decision"]
    print(f"Production decision: {decision['worth_implementing_next'] or 'none'} - {decision['reason']}")
    guard = summary["full_chain_regression_guard"]
    print(f"Full-chain regression guard: {guard['status']} - {guard['message']}")
    return 1 if guard["status"] == "failed" else 0


def run_synthetic_comparison(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.out.resolve()
    input_dir = output_dir / "synthetic-input"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_synthetic_images(input_dir, args.image_count, args.width, args.height)

    variants = [
        {
            "id": "pillow_cpu_baseline",
            "label": "Current Python/Pillow CPU baseline",
            "benchmark_args": [
                "--auto-crop",
                "--deskew",
                "--trim-dark-border",
                "--scanner-gutter-trim",
                "--despeckle",
                "--despeckle-backend",
                "fallback",
                "--reuse-scan-measurements",
                "--lighten-fold-shadows",
            ],
        },
        {
            "id": "numpy_vectorized_hotspots",
            "label": "NumPy vectorized despeckle hotspot when available",
            "benchmark_args": [
                "--auto-crop",
                "--deskew",
                "--trim-dark-border",
                "--scanner-gutter-trim",
                "--despeckle",
                "--despeckle-backend",
                "numpy",
                "--reuse-scan-measurements",
                "--lighten-fold-shadows",
            ],
        },
        {
            "id": FULL_CHAIN_VARIANT_ID,
            "label": "Full conservative repair chain budget guard",
            "benchmark_args": list(FULL_CHAIN_REPAIR_ARGS),
        },
    ]
    variant_summaries = []
    for variant in variants:
        benchmark_dir = output_dir / variant["id"]
        process_dir = benchmark_dir / "processed"
        command = [
            sys.executable,
            "-m",
            "archive_scan_qc.cli",
            "benchmark",
            "--input",
            str(input_dir),
            "--out",
            str(benchmark_dir),
            "--process-out",
            str(process_dir),
            "--workers-list",
            args.workers_list,
            "--repeats",
            str(args.repeats),
            *variant["benchmark_args"],
        ]
        subprocess.run(command, check=True, cwd=REPO_ROOT, env=_env())
        benchmark = json.loads((benchmark_dir / "benchmark_results.json").read_text(encoding="utf-8"))
        variant_summaries.append(_variant_summary(variant, benchmark))

    full_chain_guard = _full_chain_regression_guard(variant_summaries)
    summary = {
        "schema_version": "scan-qc.synthetic-performance-comparison.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "public_safe": True,
            "input_source": "synthetic images generated by this script",
            "private_puersai_images_used": False,
        },
        "synthetic_input": {
            "image_count": args.image_count,
            "width": args.width,
            "height": args.height,
        },
        "variants": variant_summaries,
        "comparison_plan": variant_summaries[0]["comparison_plan"] if variant_summaries else None,
        "full_chain_regression_guard": full_chain_guard,
        "production_decision": _production_decision(variant_summaries),
    }
    (output_dir / SUMMARY_JSON).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer.") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer.")
    return parsed


def _env() -> dict[str, str]:
    env = dict(**__import__("os").environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    return env


def _write_synthetic_images(input_dir: Path, image_count: int, width: int, height: int) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for stale in input_dir.glob("*.png"):
        stale.unlink()
    for index in range(1, image_count + 1):
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        margin = 42 + (index % 5)
        draw.rectangle((margin, margin, width - margin, height - margin), outline=(35, 35, 35), width=3)
        for line_index in range(12):
            y = margin + 42 + line_index * 44
            draw.line((margin + 28, y, width - margin - 28, y + (index % 3) - 1), fill=(55, 55, 55), width=2)
        for speckle_index in range(80):
            x = (speckle_index * 37 + index * 19) % width
            y = (speckle_index * 53 + index * 23) % height
            image.putpixel((x, y), (0, 0, 0))
        image.save(input_dir / f"synthetic_page_{index:03d}.png", dpi=(300, 300))


def _variant_summary(variant: dict[str, Any], benchmark: dict[str, Any]) -> dict[str, Any]:
    processing_recommendation = benchmark["recommendations"].get("processing")
    best_processing_rate = processing_recommendation["files_per_minute"] if processing_recommendation else None
    latest_run = benchmark["runs"][-1] if benchmark["runs"] else {}
    latest_processing = latest_run.get("processing", {}) if isinstance(latest_run, dict) else {}
    return {
        "id": variant["id"],
        "label": variant["label"],
        "benchmark_schema_version": benchmark["schema_version"],
        "run_count": len(benchmark["runs"]),
        "best_processing_images_per_minute": best_processing_rate,
        "best_requested_workers": (
            processing_recommendation["best_requested_workers"] if processing_recommendation else None
        ),
        "failures": latest_processing.get("failed_files") if isinstance(latest_processing, dict) else None,
        "review_needed_counts": latest_run.get("finding_severity_counts", {}),
        "operation_timing_regression_signal": _operation_timing_regression_signal(benchmark),
        "full_chain_budget_signal": _full_chain_budget_signal(variant, benchmark),
        "full_chain_quality_guard_signal": _full_chain_quality_guard_signal(variant, benchmark),
        "quality_difference_summary": (
            "Synthetic aggregate comparison only; inspect deltas in failures and review-needed counts before "
            "using private orchestrator evidence."
        ),
        "environment": benchmark["environment"],
        "comparison_plan": benchmark["comparison_plan"],
    }


def _full_chain_regression_guard(variants: list[dict[str, Any]]) -> dict[str, Any]:
    full_chain = next((variant for variant in variants if variant.get("id") == FULL_CHAIN_VARIANT_ID), None)
    if not isinstance(full_chain, dict):
        return _failed_guard(
            "missing_full_chain_variant",
            "Full conservative repair chain variant is missing from the synthetic comparison.",
        )

    timing_signal = full_chain.get("operation_timing_regression_signal")
    if not isinstance(timing_signal, dict):
        return _failed_guard("missing_operation_timing_signal", "Full-chain operation timing signal is missing.")

    missing_operations = timing_signal.get("missing_operations")
    if isinstance(missing_operations, list) and missing_operations:
        return _failed_guard(
            "missing_full_chain_operation_timing",
            "Full-chain synthetic guard is missing required operation timing signals.",
            missing_operations=missing_operations,
        )

    operations = timing_signal.get("operations")
    disabled_operations: list[str] = []
    if isinstance(operations, dict):
        disabled_operations = [
            operation
            for operation in REGRESSION_SIGNAL_OPERATIONS
            if not isinstance(operations.get(operation), dict) or operations[operation].get("enabled") is not True
        ]
    if disabled_operations:
        return _failed_guard(
            "full_chain_operation_not_enabled",
            "Full-chain synthetic guard did not enable every required repair operation.",
            disabled_operations=disabled_operations,
        )

    budget_signal = full_chain.get("full_chain_budget_signal")
    if not isinstance(budget_signal, dict):
        return _failed_guard("missing_full_chain_budget_signal", "Full-chain budget signal is missing.")
    if budget_signal.get("status") == "failed":
        return _failed_guard(
            "full_chain_processing_budget_exceeded",
            "Full-chain synthetic processing exceeded the aggregate seconds-per-file budget.",
            budget_signal=budget_signal,
        )

    quality_signal = full_chain.get("full_chain_quality_guard_signal")
    if not isinstance(quality_signal, dict):
        return _failed_guard("missing_full_chain_quality_guard_signal", "Full-chain aggregate quality signal is missing.")
    if quality_signal.get("status") == "failed":
        return _failed_guard(
            quality_signal.get("code", "full_chain_quality_guard_signal_failed"),
            "Full-chain aggregate quality evidence failed synthetic guard requirements.",
            quality_guard_signal=quality_signal,
        )

    return {
        "schema_version": "scan-qc.synthetic-full-chain-regression-guard.v1",
        "aggregate_only": True,
        "status": "pass",
        "message": "Full conservative chain timing, quality evidence, and synthetic budget signals are present.",
        "required_operations": list(REGRESSION_SIGNAL_OPERATIONS),
        "budget_signal": budget_signal,
        "quality_guard_signal": quality_signal,
        "privacy": _privacy_flags(),
    }


def _failed_guard(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "schema_version": "scan-qc.synthetic-full-chain-regression-guard.v1",
        "aggregate_only": True,
        "status": "failed",
        "code": code,
        "message": message,
        "required_operations": list(REGRESSION_SIGNAL_OPERATIONS),
        **details,
        "privacy": _privacy_flags(),
    }


def _full_chain_budget_signal(variant: dict[str, Any], benchmark: dict[str, Any]) -> dict[str, Any] | None:
    if variant.get("id") != FULL_CHAIN_VARIANT_ID:
        return None
    runs = benchmark.get("runs")
    if not isinstance(runs, list):
        runs = []

    run_summaries: list[dict[str, Any]] = []
    over_budget_runs: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        processing = run.get("processing")
        if not isinstance(processing, dict):
            continue
        elapsed_seconds = _coerce_float(processing.get("elapsed_seconds"))
        processed_files = _coerce_int(processing.get("processed_files"))
        if elapsed_seconds is None or processed_files <= 0:
            continue
        average_seconds_per_file = round(elapsed_seconds / processed_files, 6)
        run_summary = {
            "run_index": _coerce_int(run.get("run_index")),
            "requested_workers": _coerce_int(run.get("requested_workers")),
            "processed_files": processed_files,
            "elapsed_seconds": round(elapsed_seconds, 6),
            "average_seconds_per_file": average_seconds_per_file,
        }
        run_summaries.append(run_summary)
        if average_seconds_per_file > FULL_CHAIN_SYNTHETIC_BUDGET_SECONDS_PER_FILE:
            over_budget_runs.append(run_summary)

    missing_signal = not run_summaries
    status = "failed" if missing_signal or over_budget_runs else "pass"
    return {
        "schema_version": "scan-qc.synthetic-full-chain-budget-signal.v1",
        "aggregate_only": True,
        "status": status,
        "code": (
            "missing_full_chain_processing_budget_signal"
            if missing_signal
            else "full_chain_processing_budget_exceeded"
            if over_budget_runs
            else None
        ),
        "budget_seconds_per_file": FULL_CHAIN_SYNTHETIC_BUDGET_SECONDS_PER_FILE,
        "run_count": len(run_summaries),
        "max_average_seconds_per_file": (
            max(run["average_seconds_per_file"] for run in run_summaries) if run_summaries else None
        ),
        "over_budget_runs": over_budget_runs,
        "privacy": _privacy_flags(),
    }


def _full_chain_quality_guard_signal(variant: dict[str, Any], benchmark: dict[str, Any]) -> dict[str, Any] | None:
    if variant.get("id") != FULL_CHAIN_VARIANT_ID:
        return None
    runs = benchmark.get("runs")
    if not isinstance(runs, list):
        runs = []

    required_guard_sections = (
        "local_content_change_guard",
        "combination_quality_guard",
        "processed_output_safety_guard",
        "guardrail_metric_signal",
        "algorithm_metrics",
    )
    run_summaries: list[dict[str, Any]] = []
    failed_quality_runs: list[dict[str, Any]] = []
    missing_sections: dict[str, int] = {section: 0 for section in required_guard_sections}
    for run in runs:
        if not isinstance(run, dict):
            continue
        processing = run.get("processing")
        if not isinstance(processing, dict):
            continue
        quality = processing.get("quality_regression")
        if not isinstance(quality, dict):
            continue

        missing = [section for section in required_guard_sections if not isinstance(quality.get(section), dict)]
        for section in missing:
            missing_sections[section] += 1
        run_summary = {
            "run_index": _coerce_int(run.get("run_index")),
            "requested_workers": _coerce_int(run.get("requested_workers")),
            "status": quality.get("status"),
            "counts": _quality_guard_counts(quality.get("counts")),
            "local_content_change_guard": quality.get("local_content_change_guard", {}),
            "combination_quality_guard": quality.get("combination_quality_guard", {}),
            "processed_output_safety_guard": quality.get("processed_output_safety_guard", {}),
            "guardrail_metric_signal": quality.get("guardrail_metric_signal", {}),
            "algorithm_metrics": quality.get("algorithm_metrics", {}),
            "threshold_violations": quality.get("threshold_violations", []),
        }
        run_summaries.append(run_summary)
        if quality.get("status") != "pass" or missing:
            failed_quality_runs.append(
                {
                    "run_index": run_summary["run_index"],
                    "status": quality.get("status"),
                    "missing_sections": missing,
                    "threshold_violation_count": len(quality.get("threshold_violations", []))
                    if isinstance(quality.get("threshold_violations"), list)
                    else 0,
                }
            )

    missing_signal = not run_summaries
    active_missing_sections = {section: count for section, count in missing_sections.items() if count}
    status = "failed" if missing_signal or active_missing_sections or failed_quality_runs else "pass"
    return {
        "schema_version": "scan-qc.synthetic-full-chain-quality-guard-signal.v1",
        "aggregate_only": True,
        "status": status,
        "code": (
            "missing_full_chain_quality_guard_signal"
            if missing_signal
            else "missing_full_chain_quality_guard_sections"
            if active_missing_sections
            else "full_chain_quality_regression_failed"
            if failed_quality_runs
            else None
        ),
        "run_count": len(run_summaries),
        "failed_quality_runs": failed_quality_runs,
        "missing_sections": active_missing_sections,
        "runs": run_summaries,
        "privacy": _privacy_flags(),
    }


def _quality_guard_counts(counts: Any) -> dict[str, int]:
    if not isinstance(counts, dict):
        counts = {}
    keys = (
        "processed_files",
        "failed_files",
        "guardrail_failed_files",
        "cumulative_change_guard_checked_files",
        "cumulative_change_guard_reverted_files",
        "local_content_change_guard_checked_files",
        "local_content_change_guard_reverted_files",
        "combination_quality_guard_checked_files",
        "combination_quality_guard_reverted_files",
        "processed_output_safety_guard_checked_files",
        "processed_output_safety_guard_reverted_files",
        "processed_output_foreground_weakening_guard_reverted_files",
    )
    return {key: _coerce_int(counts.get(key)) for key in keys}


def _operation_timing_regression_signal(benchmark: dict[str, Any]) -> dict[str, Any]:
    runs = benchmark.get("runs")
    if not isinstance(runs, list):
        runs = []

    operations = {
        operation: _aggregate_operation_timing(runs, operation) for operation in REGRESSION_SIGNAL_OPERATIONS
    }
    missing_operations = [
        operation for operation, timing in operations.items() if not timing["signal_available"]
    ]
    return {
        "schema_version": "scan-qc.synthetic-operation-timing-regression-signal.v1",
        "source": "benchmark_results.runs.processing.operation_timings",
        "aggregate_only": True,
        "required_operations": list(REGRESSION_SIGNAL_OPERATIONS),
        "signal_available": not missing_operations,
        "missing_operations": missing_operations,
        "operations": operations,
        "privacy": {
            "contains_file_names": False,
            "contains_paths": False,
            "contains_hashes": False,
            "contains_thumbnails": False,
            "contains_ocr_text": False,
            "contains_row_level_evidence": False,
            "contains_image_content": False,
        },
    }


def _aggregate_operation_timing(runs: list[Any], operation: str) -> dict[str, Any]:
    elapsed_seconds = 0.0
    file_count = 0
    run_count = 0
    enabled_values: list[bool] = []
    reused_scan_measurement_files = 0
    deskew_safe_skip_files = 0
    deskew_projection_detection_files = 0
    deskew_fallback_detection_files = 0
    deskew_safe_skip_reason_code_distribution: dict[str, int] = {}
    backend_counts = {mode: 0 for mode in DESPECKLE_BACKEND_MODES}

    for run in runs:
        if not isinstance(run, dict):
            continue
        processing = run.get("processing")
        if not isinstance(processing, dict):
            continue
        operation_timings = processing.get("operation_timings")
        if not isinstance(operation_timings, dict):
            continue
        timing = operation_timings.get(operation)
        if not isinstance(timing, dict):
            continue
        run_count += 1
        if isinstance(timing.get("enabled"), bool):
            enabled_values.append(timing["enabled"])
        elapsed = timing.get("elapsed_seconds")
        if isinstance(elapsed, int | float):
            elapsed_seconds += float(elapsed)
        count = timing.get("file_count")
        if isinstance(count, int):
            file_count += count
        reused = timing.get("reused_scan_measurement_files")
        if isinstance(reused, int):
            reused_scan_measurement_files += reused
        if operation == "deskew":
            safe_skip = timing.get("safe_skip_files")
            if isinstance(safe_skip, int):
                deskew_safe_skip_files += safe_skip
            projection = timing.get("projection_detection_files")
            if isinstance(projection, int):
                deskew_projection_detection_files += projection
            fallback = timing.get("fallback_detection_files")
            if isinstance(fallback, int):
                deskew_fallback_detection_files += fallback
            reason_codes = timing.get("safe_skip_reason_code_distribution")
            if isinstance(reason_codes, dict):
                for reason_code, count in reason_codes.items():
                    if isinstance(reason_code, str) and isinstance(count, int):
                        deskew_safe_skip_reason_code_distribution[reason_code] = (
                            deskew_safe_skip_reason_code_distribution.get(reason_code, 0) + count
                        )
        if operation == "despeckle":
            source_counts = timing.get("backend_counts")
            if isinstance(source_counts, dict):
                for mode in DESPECKLE_BACKEND_MODES:
                    value = source_counts.get(mode)
                    if isinstance(value, int):
                        backend_counts[mode] += value

    if run_count == 0:
        missing = {
            "signal_available": False,
            "missing_reason": "missing_from_benchmark_processing_operation_timings",
            "run_count": 0,
            "file_count": 0,
            "elapsed_seconds": 0.0,
            "files_per_minute": 0.0,
            "average_seconds_per_file": None,
            "reused_scan_measurement_files": 0,
        }
        if operation == "deskew":
            missing.update(
                {
                    "safe_skip_files": 0,
                    "projection_detection_files": 0,
                    "fallback_detection_files": 0,
                    "safe_skip_reason_code_distribution": {},
                }
            )
        return missing

    elapsed_seconds = round(elapsed_seconds, 6)
    summary = {
        "signal_available": True,
        "enabled": any(enabled_values) if enabled_values else True,
        "run_count": run_count,
        "file_count": file_count,
        "elapsed_seconds": elapsed_seconds,
        "files_per_minute": _files_per_minute(file_count, elapsed_seconds),
        "average_seconds_per_file": round(elapsed_seconds / file_count, 6) if file_count else None,
        "reused_scan_measurement_files": reused_scan_measurement_files,
    }
    if operation == "deskew":
        summary.update(
            {
                "safe_skip_files": deskew_safe_skip_files,
                "projection_detection_files": deskew_projection_detection_files,
                "fallback_detection_files": deskew_fallback_detection_files,
                "safe_skip_reason_code_distribution": deskew_safe_skip_reason_code_distribution,
            }
        )
    if operation == "despeckle":
        active_modes = [mode for mode in DESPECKLE_BACKEND_MODES if backend_counts[mode]]
        if len(active_modes) == 1:
            backend_mode = active_modes[0]
        elif active_modes:
            backend_mode = "mixed"
        else:
            backend_mode = "unknown"
        summary.update(
            {
                "backend_mode": backend_mode,
                "numpy_available": backend_counts["numpy"] > 0,
                "backend_counts": backend_counts,
            }
        )
    return summary


def _files_per_minute(file_count: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    return round((file_count / elapsed_seconds) * 60, 2)


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _privacy_flags() -> dict[str, bool]:
    return {
        "contains_file_names": False,
        "contains_paths": False,
        "contains_hashes": False,
        "contains_thumbnails": False,
        "contains_ocr_text": False,
        "contains_row_level_evidence": False,
        "contains_image_content": False,
    }


def _production_decision(variants: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [
        variant
        for variant in variants
        if variant["id"] != FULL_CHAIN_VARIANT_ID and variant["best_processing_images_per_minute"] is not None
    ]
    if not measured:
        return {"worth_implementing_next": None, "reason": "No processing variants produced throughput metrics."}
    best = max(measured, key=lambda variant: variant["best_processing_images_per_minute"])
    baseline = next((variant for variant in measured if variant["id"] == "pillow_cpu_baseline"), None)
    if baseline and best["id"] != baseline["id"]:
        gain = best["best_processing_images_per_minute"] - baseline["best_processing_images_per_minute"]
        return {
            "worth_implementing_next": best["id"] if gain > 0 else None,
            "reason": (
                f"{best['label']} led synthetic processing throughput by {gain:.2f} images/min; "
                "confirm with private aggregate validation before implementation."
            ),
        }
    return {
        "worth_implementing_next": "pillow_cpu_baseline_worker_tuning",
        "reason": "The baseline was the best synthetic result; use worker tuning before adding new dependencies.",
    }


if __name__ == "__main__":
    raise SystemExit(main())
