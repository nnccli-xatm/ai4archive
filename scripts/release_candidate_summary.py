#!/usr/bin/env python3
"""Write aggregate-only release-candidate decision evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for path in [SRC_DIR, SCRIPTS_DIR]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from archive_scan_qc.acceptance import ACCEPTANCE_JSON  # noqa: E402
from run_aggregate_baseline import BASELINE_JSON  # noqa: E402
from run_production_validation import main as run_production_validation_main  # noqa: E402
from release_readiness_summary import (  # noqa: E402
    RELEASE_READINESS_JSON,
    run_release_readiness_checks,
    write_release_readiness_summary,
)


RELEASE_CANDIDATE_JSON = "release_candidate_summary.json"
SCHEMA_VERSION = "scan-qc.release-candidate-summary.v1"


def build_release_candidate_summary(
    *,
    aggregate_baseline_summary: dict[str, Any],
    acceptance_summary: dict[str, Any],
    release_readiness_summary: dict[str, Any],
    cleanup_requested: bool,
    generated_at: str | None = None,
) -> dict[str, Any]:
    production_status = _status_from_acceptance(acceptance_summary)
    readiness_status = _safe_text(release_readiness_summary.get("status")) or "unknown"
    blocking_item_count = _blocking_count(acceptance_summary) + _readiness_blocking_count(release_readiness_summary)
    status = "pass" if production_status == "pass" and readiness_status == "pass" and blocking_item_count == 0 else "fail"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": status,
        "ready_for_release_candidate": status == "pass",
        "privacy": {
            "aggregate_only": True,
            "contains_paths": False,
            "contains_filenames": False,
            "contains_hashes": False,
            "contains_ocr_text": False,
            "contains_thumbnails": False,
            "contains_image_content": False,
            "contains_secrets": False,
            "contains_row_level_findings": False,
            "omits": [
                "private image paths",
                "filenames",
                "hashes",
                "OCR text",
                "thumbnails",
                "image content",
                "secrets",
                "row-level findings",
                "validation-host temporary paths",
            ],
        },
        "production_validation": {
            "status": production_status,
            "aggregate_baseline_schema_version": _safe_text(aggregate_baseline_summary.get("schema_version")),
            "acceptance_schema_version": _safe_text(acceptance_summary.get("schema_version")),
            "counts": _production_counts(aggregate_baseline_summary),
            "throughput": _throughput(aggregate_baseline_summary, acceptance_summary),
            "threshold_outcomes": _threshold_outcomes(acceptance_summary),
            "privacy": _privacy_status(aggregate_baseline_summary, acceptance_summary),
            "cleanup": _cleanup_status(aggregate_baseline_summary, acceptance_summary, cleanup_requested),
            "runtime_hardware": _runtime_hardware(aggregate_baseline_summary),
        },
        "release_readiness": {
            "status": readiness_status,
            "schema_version": _safe_text(release_readiness_summary.get("schema_version")),
            "blocking_item_count": _readiness_blocking_count(release_readiness_summary),
            "checks": _readiness_counts(release_readiness_summary),
            "capability_probe": _capability_probe_counts(release_readiness_summary),
        },
        "decision": {
            "blocking_item_count": blocking_item_count,
            "production_blocking_item_count": _blocking_count(acceptance_summary),
            "release_readiness_blocking_item_count": _readiness_blocking_count(release_readiness_summary),
        },
        "scan_processing_semantics": _safe_text(
            release_readiness_summary.get("scan_processing_semantics")
        )
        or "unchanged_cpu_pillow_baseline",
        "network_services_called": False,
        "model_inference_run": False,
    }


def write_release_candidate_summary(summary: dict[str, Any], output_path: Path) -> Path:
    path = output_path / RELEASE_CANDIDATE_JSON if output_path.suffix == "" else output_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output_root = Path(args.out).expanduser().resolve()
        if args.input is not None:
            _run_production_validation(args)
        if args.run_release_readiness:
            readiness = run_release_readiness_checks(run_capability_probe_check=args.run_capability_probe)
            write_release_readiness_summary(readiness, output_root / RELEASE_READINESS_JSON)

        baseline = _load_json(args.aggregate_baseline_summary or output_root / BASELINE_JSON, "aggregate_baseline_summary")
        acceptance = _load_json(args.acceptance_summary or output_root / ACCEPTANCE_JSON, "acceptance_summary")
        readiness = _load_json(args.release_readiness_summary or output_root / RELEASE_READINESS_JSON, "release_readiness_summary")
        summary = build_release_candidate_summary(
            aggregate_baseline_summary=baseline,
            acceptance_summary=acceptance,
            release_readiness_summary=readiness,
            cleanup_requested=bool(args.cleanup_artifacts),
        )
        path = write_release_candidate_summary(summary, output_root / RELEASE_CANDIDATE_JSON)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"release_candidate_summary.py: error: {exc}\n")

    print(f"Release candidate summary: {path}")
    print(f"Release candidate status: {summary['status']}")
    print(f"Blocking items: {summary['decision']['blocking_item_count']}")
    print("Privacy: aggregate-only; no paths, filenames, hashes, OCR text, thumbnails, image content, secrets, or row-level findings.")
    return 0 if summary["status"] == "pass" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_candidate_summary.py",
        description="Write one aggregate-only release-candidate decision summary.",
    )
    parser.add_argument("--out", required=True, type=Path, help=f"Output directory for {RELEASE_CANDIDATE_JSON}.")
    parser.add_argument("--input", default=None, type=Path, help="Optional private image input directory; invokes production validation.")
    parser.add_argument("--aggregate-baseline-summary", default=None, type=Path)
    parser.add_argument("--acceptance-summary", default=None, type=Path)
    parser.add_argument("--release-readiness-summary", default=None, type=Path)
    parser.add_argument("--workers", default=4, type=_positive_int)
    parser.add_argument("--benchmark-workers-list", default=None)
    parser.add_argument("--benchmark-repeats", default=1, type=_positive_int)
    parser.add_argument("--project", default="release-candidate-validation")
    parser.add_argument("--batch", default="release-candidate-validation")
    parser.add_argument("--label", default="puersai-hpc")
    parser.add_argument("--process-images", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--auto-crop", action="store_true")
    parser.add_argument("--deskew", action="store_true")
    parser.add_argument("--trim-dark-border", action="store_true")
    parser.add_argument("--despeckle", action="store_true")
    parser.add_argument("--resume-processing", action="store_true")
    parser.add_argument("--manifest-csv", default=None, type=Path)
    parser.add_argument("--rules-profile", default=None, type=Path)
    parser.add_argument("--min-dpi", default=None, type=int)
    parser.add_argument("--name-pattern", default=None)
    parser.add_argument("--skip-benchmark", action="store_true")
    parser.add_argument("--cleanup-artifacts", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--min-scan-files-per-minute", default=None, type=_non_negative_float)
    parser.add_argument("--min-processing-files-per-minute", default=None, type=_non_negative_float)
    parser.add_argument("--run-release-readiness", action="store_true")
    parser.add_argument("--run-capability-probe", action="store_true")
    return parser


def _run_production_validation(args: argparse.Namespace) -> None:
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
        "--no-resource-summary",
    ]
    for flag, value in [
        ("--benchmark-workers-list", args.benchmark_workers_list),
        ("--manifest-csv", args.manifest_csv),
        ("--rules-profile", args.rules_profile),
        ("--min-dpi", args.min_dpi),
        ("--name-pattern", args.name_pattern),
        ("--min-scan-files-per-minute", args.min_scan_files_per_minute),
        ("--min-processing-files-per-minute", args.min_processing_files_per_minute),
    ]:
        if value is not None:
            argv.extend([flag, str(value)])
    argv.append("--process-images" if args.process_images else "--no-process-images")
    argv.append("--cleanup-artifacts" if args.cleanup_artifacts else "--no-cleanup-artifacts")
    for flag in ["--auto-crop", "--deskew", "--trim-dark-border", "--despeckle", "--resume-processing", "--skip-benchmark"]:
        if getattr(args, flag.removeprefix("--").replace("-", "_")):
            argv.append(flag)
    exit_code = run_production_validation_main(argv)
    if exit_code not in (0, 1):
        raise ValueError("production validation failed before aggregate summaries were written.")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"{label} JSON could not be read.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return payload


def _status_from_acceptance(acceptance_summary: dict[str, Any]) -> str:
    status = _safe_text(acceptance_summary.get("status"))
    if status:
        return status
    return "pass" if acceptance_summary.get("pass") is True else "fail"


def _production_counts(baseline: dict[str, Any]) -> dict[str, Any]:
    counts = baseline.get("aggregate_counts")
    if not isinstance(counts, dict):
        counts = {}
    return {
        "total_files": _coerce_int(counts.get("total_files")),
        "openable_files": _coerce_int(counts.get("openable_files")),
        "processing_processed_files": _coerce_int(counts.get("processing_processed_files")),
        "processing_failed_files": _coerce_int(counts.get("processing_failed_files")),
        "total_findings": _coerce_int(counts.get("total_findings")),
        "severity_counts": {
            "p0": _coerce_int(counts.get("p0_findings")),
            "p1": _coerce_int(counts.get("p1_findings")),
            "p2": _coerce_int(counts.get("p2_findings")),
        },
    }


def _throughput(baseline: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    stage_timings = baseline.get("stage_timings")
    acceptance_throughput = acceptance.get("throughput")
    scan = stage_timings.get("scan") if isinstance(stage_timings, dict) else None
    processing = stage_timings.get("processing") if isinstance(stage_timings, dict) else None
    return {
        "scan_files_per_minute": _coerce_float(scan.get("files_per_minute") if isinstance(scan, dict) else None),
        "scan_openable_files_per_minute": _coerce_float(scan.get("openable_files_per_minute") if isinstance(scan, dict) else None),
        "processing_files_per_minute": _coerce_float(
            processing.get("processed_files_per_minute") if isinstance(processing, dict) else None
        ),
        "scan_threshold": _metric_from_acceptance(acceptance_throughput, "scan_files_per_minute"),
        "processing_threshold": _metric_from_acceptance(acceptance_throughput, "processing_files_per_minute"),
    }


def _threshold_outcomes(acceptance: dict[str, Any]) -> dict[str, Any]:
    thresholds = acceptance.get("thresholds")
    if not isinstance(thresholds, dict):
        thresholds = {}
    blocking_codes = {str(item.get("code")) for item in acceptance.get("blocking_items", []) if isinstance(item, dict)}
    return {
        "remaining_p0_max": _coerce_int(thresholds.get("remaining_p0_max")),
        "remaining_p1_max": _coerce_int(thresholds.get("remaining_p1_max")),
        "processing_failed_files_max": _coerce_int(thresholds.get("processing_failed_files_max")),
        "min_scan_files_per_minute": _coerce_float(thresholds.get("min_scan_files_per_minute")),
        "min_processing_files_per_minute": _coerce_float(thresholds.get("min_processing_files_per_minute")),
        "scan_throughput_passed": "scan_throughput_below_threshold" not in blocking_codes,
        "processing_throughput_passed": "processing_throughput_below_threshold" not in blocking_codes,
    }


def _privacy_status(baseline: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    self_check = acceptance.get("privacy_self_check")
    if not isinstance(self_check, dict):
        self_check = baseline.get("privacy_self_check") if isinstance(baseline.get("privacy_self_check"), dict) else {}
    return {
        "aggregate_only": _aggregate_only(baseline) and _aggregate_only(acceptance),
        "self_check_passed": self_check.get("passed") is True or self_check.get("status") == "pass",
        "self_check_status": _safe_text(self_check.get("status")),
        "violation_count": _coerce_int(self_check.get("violation_count")),
    }


def _cleanup_status(baseline: dict[str, Any], acceptance: dict[str, Any], cleanup_requested: bool) -> dict[str, Any]:
    cleanup = acceptance.get("cleanup")
    if not isinstance(cleanup, dict):
        cleanup = baseline.get("cleanup") if isinstance(baseline.get("cleanup"), dict) else {}
    return {
        "requested": cleanup_requested,
        "enabled": cleanup.get("enabled") is True,
        "retained_public_summary_only": cleanup.get("retained_public_summary_only") is True,
        "removed_artifact_count": _coerce_int(cleanup.get("removed_artifact_count")),
        "preserved_artifact_count": _coerce_int(cleanup.get("preserved_artifact_count")),
        "retained_public_summary": _safe_text(cleanup.get("retained_public_summary")),
    }


def _runtime_hardware(baseline: dict[str, Any]) -> dict[str, Any]:
    runtime = baseline.get("runtime_hardware")
    if not isinstance(runtime, dict):
        runtime = {}
    environment = baseline.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    return {
        "python_version_family": _safe_text(runtime.get("python_version_family")) or _safe_text(environment.get("python_version")),
        "pillow_version": _safe_text(environment.get("pillow_version")),
        "cpu_logical_count": _coerce_int(runtime.get("cpu_logical_count")),
        "gpu_visible_count": _coerce_int(runtime.get("gpu_visible_count")),
        "gpu_acceleration_used": runtime.get("gpu_acceleration_used") is True,
        "telemetry_warning_count": len(runtime.get("warnings")) if isinstance(runtime.get("warnings"), list) else 0,
    }


def _readiness_counts(readiness: dict[str, Any]) -> dict[str, Any]:
    summary = readiness.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    return {
        "checks_total": _coerce_int(summary.get("checks_total")),
        "checks_passed": _coerce_int(summary.get("checks_passed")),
        "checks_failed": _coerce_int(summary.get("checks_failed")),
        "checks_warning": _coerce_int(summary.get("checks_warning")),
        "checks_skipped": _coerce_int(summary.get("checks_skipped")),
    }


def _capability_probe_counts(readiness: dict[str, Any]) -> dict[str, Any]:
    probe = readiness.get("capability_probe")
    if not isinstance(probe, dict):
        probe = {}
    return {
        "available": probe.get("available") is True,
        "status": _safe_text(probe.get("status")) or "skipped",
        "blocking": probe.get("blocking") is True,
        "provider_packages_found_count": _coerce_int(probe.get("provider_packages_found_count")),
        "gpu_visible_count": _coerce_int(probe.get("gpu_visible_count")),
        "gpu_acceleration_configured": probe.get("gpu_acceleration_configured") is True,
        "model_acceleration_configured": probe.get("model_acceleration_configured") is True,
    }


def _metric_from_acceptance(throughput: Any, key: str) -> dict[str, Any]:
    if not isinstance(throughput, dict) or not isinstance(throughput.get(key), dict):
        return {"provided": False, "best_observed": None, "lowest_observed": None}
    metric = throughput[key]
    return {
        "provided": metric.get("provided") is True,
        "best_observed": _coerce_float(metric.get("best_observed")),
        "lowest_observed": _coerce_float(metric.get("lowest_observed")),
    }


def _blocking_count(acceptance: dict[str, Any]) -> int:
    items = acceptance.get("blocking_items")
    return len(items) if isinstance(items, list) else 0


def _readiness_blocking_count(readiness: dict[str, Any]) -> int:
    summary = readiness.get("summary")
    if isinstance(summary, dict):
        value = _coerce_int(summary.get("blocking_items"))
        if value is not None:
            return value
    return 1 if readiness.get("status") == "fail" else 0


def _aggregate_only(payload: dict[str, Any]) -> bool:
    privacy = payload.get("privacy")
    return isinstance(privacy, dict) and privacy.get("aggregate_only") is True


def _safe_text(value: Any) -> str | None:
    return str(value) if value is not None else None


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
