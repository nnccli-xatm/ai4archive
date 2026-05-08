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

from .processing import ProcessingOptions, process_images
from .rules import RulesProfileError, load_rules_profile
from .scanner import ScanConfig, scan_batch


BENCHMARK_JSON = "benchmark_results.json"
BENCHMARK_CSV = "benchmark_results.csv"


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
    parser.add_argument("--process-out", default=None, type=Path, help="Optional derivative-image scratch root.")
    parser.add_argument("--project", default="benchmark-project", help="Project identifier used internally for scanning.")
    parser.add_argument("--batch", default="benchmark-batch", help="Batch identifier used internally for scanning.")
    parser.add_argument("--workers-list", required=True, type=workers_list, help="Comma-separated worker counts, e.g. 1,2,4.")
    parser.add_argument("--repeats", default=1, type=lambda value: positive_int(value, "--repeats"))
    parser.add_argument("--scan-only", action="store_true", help="Only run scan checks; do not write derivative images.")
    parser.add_argument("--auto-crop", action="store_true", help="Enable conservative auto-crop during processing.")
    parser.add_argument("--deskew", action="store_true", help="Enable conservative deskew during processing.")
    parser.add_argument("--trim-dark-border", action="store_true", help="Enable conservative dark-border trim during processing.")
    parser.add_argument("--despeckle", action="store_true", help="Enable conservative despeckle during processing.")
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
    work_root = output_dir / ".benchmark_work"
    process_root = (args.process_out or (work_root / "processed")).resolve()

    rules_profile = load_rules_profile(args.rules_profile) if args.rules_profile else None
    if rules_profile and args.min_dpi is not None:
        rules_profile = replace(rules_profile, min_dpi=args.min_dpi)
    if rules_profile and args.name_pattern is not None:
        rules_profile = replace(rules_profile, name_pattern=args.name_pattern)

    started_at = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, Any]] = []
    payload = _payload(started_at, results)
    _write_results(payload, output_dir)

    run_index = 0
    for repeat_index in range(1, args.repeats + 1):
        for workers in args.workers_list:
            run_index += 1
            scan_out = work_root / f"run-{run_index:03d}-scan"
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
                try:
                    processing_manifest = process_images(
                        report,
                        args.input,
                        process_dir,
                        ProcessingOptions(
                            auto_crop=args.auto_crop,
                            deskew=args.deskew,
                            trim_dark_border=args.trim_dark_border,
                            despeckle=args.despeckle,
                            workers=workers,
                        ),
                    )
                    _remove_sensitive_processing_manifest(process_dir)
                finally:
                    shutil.rmtree(process_dir, ignore_errors=True)

            results.append(_aggregate_run(run_index, repeat_index, workers, args, report, processing_manifest))
            payload = _payload(started_at, results)
            _write_results(payload, output_dir)

    shutil.rmtree(work_root, ignore_errors=True)
    payload = _payload(started_at, results, finished_at=datetime.now(timezone.utc).isoformat())
    _write_results(payload, output_dir)
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
        "runs": results,
    }


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
        },
        "scan": {
            "elapsed_seconds": scan_performance["elapsed_seconds"],
            "files_per_minute": scan_performance["files_per_minute"],
            "openable_files_per_minute": scan_performance["openable_files_per_minute"],
        },
    }


def _operations(args: argparse.Namespace) -> dict[str, bool]:
    return {
        "deskew": args.deskew,
        "auto_crop": args.auto_crop,
        "trim_dark_border": args.trim_dark_border,
        "despeckle": args.despeckle,
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
        "total_files",
        "openable_files",
        "p0_findings",
        "p1_findings",
        "p2_findings",
        "finding_rule_counts_json",
        "processed_files",
        "processing_failed_files",
        "processing_skipped_files",
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
        "total_files": run["total_files"],
        "openable_files": run["openable_files"],
        "p0_findings": severities["P0"],
        "p1_findings": severities["P1"],
        "p2_findings": severities["P2"],
        "finding_rule_counts_json": json.dumps(run["finding_rule_counts"], sort_keys=True),
        "processed_files": processing["processed_files"],
        "processing_failed_files": processing["failed_files"],
        "processing_skipped_files": processing["skipped_files"],
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


def _remove_sensitive_processing_manifest(process_dir: Path) -> None:
    manifest_path = process_dir / "processing_manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
