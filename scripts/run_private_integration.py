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
    parser.add_argument("--resume-processing", action="store_true", help="Resume derivative processing.")
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
                resume_processing=args.resume_processing,
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
        },
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
            "failed_batches": failed_batches,
            "preflight_errors": int(run_counts["preflight_error_count"]),
        },
        "benchmark": {
            "source": "benchmark repeated worker runs",
            "run_count": len(benchmark_runs),
            "finding_rule_counts_repeated_runs": dict(sorted(finding_rule_counts.items())),
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
        min_dpi=args.min_dpi,
        name_pattern=args.name_pattern,
        manifest_csv=args.manifest_csv,
        rules_profile=args.rules_profile,
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
