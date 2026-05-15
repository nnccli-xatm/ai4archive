#!/usr/bin/env python3
"""One-command aggregate production validation wrapper."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for path in [SRC_DIR, SCRIPTS_DIR]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from archive_scan_qc.acceptance import ACCEPTANCE_JSON, write_acceptance_summary  # noqa: E402
from run_aggregate_baseline import BASELINE_JSON, build_parser as build_baseline_parser, run_aggregate_baseline  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_production_validation.py",
        description=(
            "Run aggregate baseline cleanup and the aggregate acceptance gate in one "
            "privacy-safe operator command."
        ),
    )
    parser.add_argument("--input", required=True, type=Path, help="Private image input directory.")
    parser.add_argument("--out", required=True, type=Path, help="Private output root for aggregate evidence.")
    parser.add_argument("--workers", default=4, type=_positive_int, help="Requested worker count.")
    parser.add_argument(
        "--benchmark-workers-list",
        default=None,
        help="Optional comma-separated worker counts. Defaults to --workers in the baseline runner.",
    )
    parser.add_argument("--benchmark-repeats", default=1, type=_positive_int)
    parser.add_argument("--project", default="aggregate-production-validation", help="Non-sensitive project identifier.")
    parser.add_argument("--batch", default="aggregate-production-validation", help="Non-sensitive batch identifier.")
    parser.add_argument("--label", default="puersai-hpc", help="Non-sensitive environment label.")
    parser.add_argument("--process-images", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--auto-crop", action="store_true")
    parser.add_argument("--deskew", action="store_true")
    parser.add_argument("--trim-dark-border", action="store_true")
    parser.add_argument("--despeckle", action="store_true")
    parser.add_argument("--normalize-tones", action="store_true")
    parser.add_argument("--lighten-edge-shadow", action="store_true")
    parser.add_argument("--lighten-background-stains", action="store_true")
    parser.add_argument("--lighten-scanlines", action="store_true")
    parser.add_argument(
        "--despeckle-backend",
        choices=("fallback", "numpy"),
        default="fallback",
        help="Despeckle processing backend. Defaults to conservative fallback; numpy is opt-in.",
    )
    parser.add_argument("--resume-processing", action="store_true")
    parser.add_argument("--reuse-scan-measurements", action="store_true")
    parser.add_argument("--manifest-csv", default=None, type=Path)
    parser.add_argument("--rules-profile", default=None, type=Path)
    parser.add_argument("--min-dpi", default=None, type=int)
    parser.add_argument("--name-pattern", default=None)
    parser.add_argument("--skip-benchmark", action="store_true")
    parser.add_argument(
        "--cleanup-artifacts",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove generated private artifacts after aggregate summary extraction. Default: enabled.",
    )
    parser.add_argument(
        "--min-scan-files-per-minute",
        default=None,
        type=_non_negative_float,
        help="Optional minimum acceptable scan throughput.",
    )
    parser.add_argument(
        "--min-processing-files-per-minute",
        default=None,
        type=_non_negative_float,
        help="Optional minimum acceptable derivative processing throughput.",
    )
    parser.add_argument(
        "--resource-summary",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Print aggregate runtime/hardware fields to stdout. Default: enabled.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        baseline_args = _build_baseline_args(args)
        baseline = run_aggregate_baseline(baseline_args)
        output_root = Path(args.out).expanduser().resolve()
        acceptance_path, acceptance = write_acceptance_summary(
            output_path=output_root / ACCEPTANCE_JSON,
            aggregate_baseline_summary_path=output_root / BASELINE_JSON,
            min_scan_files_per_minute=args.min_scan_files_per_minute,
            min_processing_files_per_minute=args.min_processing_files_per_minute,
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"run_production_validation.py: error: {exc}\n")

    _print_aggregate_result(
        baseline=baseline,
        acceptance=acceptance,
        acceptance_path=acceptance_path,
        include_resource_summary=args.resource_summary,
    )
    return 0 if acceptance["pass"] else 1


def _build_baseline_args(args: argparse.Namespace) -> argparse.Namespace:
    argv = [
        "--input",
        str(args.input),
        "--out",
        str(args.out),
        "--workers",
        str(args.workers),
        "--project",
        str(args.project),
        "--batch",
        str(args.batch),
        "--label",
        str(args.label),
        "--benchmark-repeats",
        str(args.benchmark_repeats),
    ]
    optional_values = [
        ("--benchmark-workers-list", args.benchmark_workers_list),
        ("--manifest-csv", args.manifest_csv),
        ("--rules-profile", args.rules_profile),
        ("--min-dpi", args.min_dpi),
        ("--name-pattern", args.name_pattern),
        ("--despeckle-backend", args.despeckle_backend),
    ]
    for flag, value in optional_values:
        if value is not None:
            argv.extend([flag, str(value)])
    boolean_flags = [
        ("--process-images", "--no-process-images", args.process_images),
        ("--cleanup-artifacts", "--no-cleanup-artifacts", args.cleanup_artifacts),
    ]
    for true_flag, false_flag, enabled in boolean_flags:
        argv.append(true_flag if enabled else false_flag)
    for flag in [
        "--auto-crop",
        "--deskew",
        "--trim-dark-border",
        "--despeckle",
        "--normalize-tones",
        "--lighten-edge-shadow",
        "--lighten-background-stains",
        "--lighten-scanlines",
        "--resume-processing",
        "--reuse-scan-measurements",
        "--skip-benchmark",
    ]:
        if getattr(args, flag.removeprefix("--").replace("-", "_")):
            argv.append(flag)
    return build_baseline_parser().parse_args(argv)


def _print_aggregate_result(
    *,
    baseline: dict[str, Any],
    acceptance: dict[str, Any],
    acceptance_path: Path,
    include_resource_summary: bool = True,
) -> None:
    counts = baseline["aggregate_counts"]
    scan = baseline["stage_timings"]["scan"]
    processing = baseline["stage_timings"]["processing"]
    cleanup = acceptance["cleanup"]
    print(f"Aggregate baseline: {BASELINE_JSON}")
    print(f"Acceptance summary: {acceptance_path.name}")
    print(f"Total files: {counts['total_files']}")
    print(f"Openable files: {counts['openable_files']}")
    print(f"Processed files: {counts['processing_processed_files']}")
    print(f"Processing failures: {counts['processing_failed_files']}")
    print(f"Scan files/min: {scan['files_per_minute']:.2f}")
    processed_rate = processing["processed_files_per_minute"]
    print(f"Processing files/min: {processed_rate:.2f}" if processed_rate is not None else "Processing files/min: n/a")
    print(f"Privacy self-check: {baseline['privacy_self_check']['status']}")
    print(f"Cleanup retained only aggregate summary: {cleanup['retained_public_summary_only']}")
    if include_resource_summary:
        _print_resource_summary(baseline.get("runtime_hardware", {}))
    print(f"Acceptance status: {acceptance['status']}")
    if acceptance["blocking_items"]:
        codes = ", ".join(str(item["code"]) for item in acceptance["blocking_items"])
        print(f"Blocking items: {codes}")


def _print_resource_summary(runtime_hardware: dict[str, Any]) -> None:
    if not runtime_hardware:
        return
    print(f"Runtime OS family: {runtime_hardware.get('os_family')}")
    print(f"Python version family: {runtime_hardware.get('python_version_family')}")
    print(f"CPU logical count: {runtime_hardware.get('cpu_logical_count')}")
    print(f"Total memory GB: {_format_optional_float(runtime_hardware.get('total_memory_gb'))}")
    print(f"Output disk free/total GB: {_format_disk(runtime_hardware)}")
    print(f"GPU visible count: {runtime_hardware.get('gpu_visible_count')}")
    print(f"GPU memory total GB: {_format_optional_float(runtime_hardware.get('gpu_memory_total_gb'))}")
    print(f"GPU acceleration used: {runtime_hardware.get('gpu_acceleration_used')}")
    warnings = runtime_hardware.get("warnings")
    if isinstance(warnings, list) and warnings:
        print(f"Resource telemetry warnings: {len(warnings)}")


def _format_disk(runtime_hardware: dict[str, Any]) -> str:
    free = _format_optional_float(runtime_hardware.get("output_disk_free_gb"))
    total = _format_optional_float(runtime_hardware.get("output_disk_total_gb"))
    return f"{free}/{total}"


def _format_optional_float(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return f"{float(value):.3f}"
    return str(value)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer.") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer.")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("threshold must be a non-negative number.") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("threshold must be a non-negative number.")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
