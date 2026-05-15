#!/usr/bin/env python3
"""Aggregate-only performance baseline runner for private scan QC samples."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for path in [SRC_DIR, SCRIPTS_DIR]:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_private_integration import (  # noqa: E402
    _forbidden_values,
    _positive_int,
    privacy_self_check,
    run_private_integration,
)


BASELINE_JSON = "aggregate_baseline_summary.json"
GENERATED_ARTIFACT_NAMES = [
    "scan-reports",
    "processed-images",
    "run-plan",
    "benchmark",
    "benchmark-processed",
    "private_integration_summary.json",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_aggregate_baseline.py",
        description="Run a privacy-safe aggregate scan QC performance baseline on a private image directory.",
    )
    parser.add_argument(
        "--input",
        default=os.environ.get("SCAN_QC_BASELINE_INPUT") or os.environ.get("PUERSAI_HPC_BASELINE_INPUT"),
        type=Path,
        help="Private image input directory. Env: SCAN_QC_BASELINE_INPUT or PUERSAI_HPC_BASELINE_INPUT.",
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("SCAN_QC_BASELINE_OUT") or os.environ.get("PUERSAI_HPC_BASELINE_OUT"),
        type=Path,
        help="Private output root. Env: SCAN_QC_BASELINE_OUT or PUERSAI_HPC_BASELINE_OUT.",
    )
    parser.add_argument(
        "--workers",
        default=os.environ.get("SCAN_QC_BASELINE_WORKERS") or os.environ.get("PUERSAI_HPC_BASELINE_WORKERS") or "4",
        type=_positive_int,
        help="Requested worker count. Env: SCAN_QC_BASELINE_WORKERS or PUERSAI_HPC_BASELINE_WORKERS.",
    )
    parser.add_argument("--project", default="aggregate-baseline", help="Non-sensitive project identifier.")
    parser.add_argument("--batch", default="aggregate-baseline", help="Non-sensitive batch identifier.")
    parser.add_argument(
        "--label",
        default=os.environ.get("SCAN_QC_BASELINE_LABEL") or "puersai-hpc",
        help="Non-sensitive environment label for the aggregate summary.",
    )
    parser.add_argument("--process-images", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--auto-crop", action="store_true")
    parser.add_argument("--deskew", action="store_true")
    parser.add_argument("--trim-dark-border", action="store_true")
    parser.add_argument("--despeckle", action="store_true")
    parser.add_argument("--normalize-tones", action="store_true")
    parser.add_argument("--lighten-edge-shadow", action="store_true")
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
    parser.add_argument(
        "--benchmark-workers-list",
        default=os.environ.get("SCAN_QC_BASELINE_WORKERS_LIST") or os.environ.get("PUERSAI_HPC_BASELINE_WORKERS_LIST"),
        help="Optional comma-separated worker counts. Defaults to --workers.",
    )
    parser.add_argument("--benchmark-repeats", default=1, type=_positive_int)
    parser.add_argument("--skip-benchmark", action="store_true")
    parser.add_argument(
        "--cleanup-artifacts",
        action=argparse.BooleanOptionalAction,
        default=_env_flag("SCAN_QC_BASELINE_CLEANUP_ARTIFACTS")
        or _env_flag("PUERSAI_HPC_BASELINE_CLEANUP_ARTIFACTS"),
        help=(
            "After writing aggregate_baseline_summary.json, delete generated private artifacts under --out. "
            "Env: SCAN_QC_BASELINE_CLEANUP_ARTIFACTS or PUERSAI_HPC_BASELINE_CLEANUP_ARTIFACTS."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        baseline = run_aggregate_baseline(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"run_aggregate_baseline.py: error: {exc}\n")

    counts = baseline["aggregate_counts"]
    stages = baseline["stage_timings"]
    print(f"Aggregate baseline label: {baseline['target_environment']['label']}")
    print(f"Total files: {counts['total_files']}")
    print(f"Openable files: {counts['openable_files']}")
    print(f"Scan files/min: {stages['scan']['files_per_minute']:.2f}")
    processing_rate = stages["processing"]["processed_files_per_minute"]
    print(f"Processing files/min: {processing_rate:.2f}" if processing_rate is not None else "Processing files/min: n/a")
    print(f"Privacy self-check: {baseline['privacy_self_check']['status']}")
    print(f"Public aggregate summary: {BASELINE_JSON}")
    return 0 if baseline["privacy_self_check"]["passed"] else 1


def run_aggregate_baseline(args: argparse.Namespace) -> dict[str, Any]:
    start_seconds = time.perf_counter()
    integration_result = run_private_integration(args)
    integration_elapsed_seconds = _elapsed_since(start_seconds)
    summary = integration_result.summary
    baseline = _baseline_summary(args, summary)
    baseline["stage_timings"]["run_plan_and_benchmark"] = {
        "elapsed_seconds": integration_elapsed_seconds,
    }

    output_root = Path(args.out).expanduser().resolve()
    summary_path = output_root / BASELINE_JSON
    if args.cleanup_artifacts:
        cleanup_start = time.perf_counter()
        cleanup = cleanup_generated_artifacts(output_root=output_root, input_dir=Path(args.input).expanduser().resolve())
        cleanup["elapsed_seconds"] = _elapsed_since(cleanup_start)
        baseline["cleanup"] = cleanup
    baseline["stage_timings"]["report_write"] = {
        "elapsed_seconds": 0.0,
    }
    baseline["stage_timings"]["total_wall_clock"] = {
        "elapsed_seconds": _elapsed_since(start_seconds),
    }
    _update_privacy_self_check(args, baseline)

    report_write_start = time.perf_counter()
    summary_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    baseline["stage_timings"]["report_write"]["elapsed_seconds"] = _elapsed_since(report_write_start)
    baseline["stage_timings"]["total_wall_clock"]["elapsed_seconds"] = _elapsed_since(start_seconds)
    _update_privacy_self_check(args, baseline)
    summary_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return baseline


def cleanup_generated_artifacts(*, output_root: Path, input_dir: Path) -> dict[str, Any]:
    output_root = output_root.expanduser().resolve()
    input_dir = input_dir.expanduser().resolve()
    removed: list[str] = []
    preserved: list[str] = []

    for name in GENERATED_ARTIFACT_NAMES:
        candidate = (output_root / name).resolve()
        if not candidate.exists():
            continue
        if _should_preserve_cleanup_candidate(candidate, input_dir):
            preserved.append(name)
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        removed.append(name)

    return {
        "enabled": True,
        "removed_artifacts": removed,
        "preserved_artifacts": preserved,
        "retained_public_summary": BASELINE_JSON,
        "elapsed_seconds": 0.0,
    }


def _should_preserve_cleanup_candidate(candidate: Path, input_dir: Path) -> bool:
    if candidate == input_dir or input_dir in candidate.parents:
        return True
    if candidate == REPO_ROOT or REPO_ROOT in candidate.parents:
        return True
    return False


def _baseline_summary(args: argparse.Namespace, private_summary: dict[str, Any]) -> dict[str, Any]:
    counts = private_summary["aggregate_counts"]
    throughput = private_summary["throughput"]
    configuration = private_summary["configuration"]
    benchmark = private_summary.get("benchmark", {})
    return {
        "schema_version": "scan-qc.aggregate-baseline.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_environment": {
            "label": str(args.label),
            "validation_target": "puersai-hpc" if str(args.label) == "puersai-hpc" else str(args.label),
            "gpu_acceleration_used": False,
        },
        "privacy": {
            "aggregate_only": True,
            "guarantees": [
                "No source names, source paths, relative paths, hashes, previews, OCR text, image content, or row-level findings.",
                "Sensitive scan reports, processing manifests, and derivative images remain under the private output root.",
            ],
        },
        "worker_settings": {
            "requested_workers": int(args.workers),
            "benchmark_workers_list": args.benchmark_workers_list or str(args.workers),
            "benchmark_repeats": int(args.benchmark_repeats),
            "benchmark_enabled": bool(configuration["benchmark_enabled"]),
            "benchmark_run_count": int(configuration["benchmark_run_count"]),
        },
        "operations": {
            "processing_enabled": bool(configuration["processing_enabled"]),
            "auto_crop": bool(args.auto_crop),
            "deskew": bool(args.deskew),
            "trim_dark_border": bool(args.trim_dark_border),
            "despeckle": bool(args.despeckle),
            "normalize_tones": bool(getattr(args, "normalize_tones", False)),
            "lighten_edge_shadow": bool(getattr(args, "lighten_edge_shadow", False)),
            "despeckle_backend_requested": private_summary.get("despeckle_backend", {}).get(
                "requested_backend",
                getattr(args, "despeckle_backend", "fallback"),
            ),
            "resume_processing": bool(args.resume_processing),
            "reuse_scan_measurements": bool(getattr(args, "reuse_scan_measurements", False)),
        },
        "despeckle_backend": private_summary.get(
            "despeckle_backend",
            {
                "requested_backend": getattr(args, "despeckle_backend", "fallback"),
                "effective_backend_mode": "unknown",
                "numpy_available": False,
                "backend_counts": {"numpy": 0, "fallback": 0, "not_applicable": 0, "unknown": 0},
                "fallback_count": 0,
                "requested_numpy_fallback_count": 0,
                "warning_codes": [],
            },
        ),
        "warning_item_count": int(private_summary.get("warning_item_count", 0)),
        "warning_counts_by_code": private_summary.get("warning_counts_by_code", {}),
        "warning_items": private_summary.get("warning_items", []),
        "aggregate_counts": {
            "total_files": int(counts["total_files"]),
            "openable_files": int(counts["openable_files"]),
            "total_findings": int(counts["total_findings"]),
            "p0_findings": int(counts["p0_findings"]),
            "p1_findings": int(counts["p1_findings"]),
            "p2_findings": int(counts["p2_findings"]),
            "processing_processed_files": int(counts["processing_processed_files"]),
            "processing_failed_files": int(counts["processing_failed_files"]),
            "processing_resumed_files": int(counts.get("processing_resumed_files", 0)),
            "processing_duplicate_reused_files": int(counts.get("processing_duplicate_reused_files", 0)),
            "processing_existing_derivative_reused_files": int(
                counts.get("processing_existing_derivative_reused_files", 0)
            ),
            "processing_scan_measurement_reused_files": int(
                counts.get("processing_scan_measurement_reused_files", 0)
            ),
            "failed_batches": int(counts["failed_batches"]),
            "preflight_errors": int(counts["preflight_errors"]),
        },
        "stage_timings": {
            "scan": {
                "elapsed_seconds": float(throughput["scan_elapsed_seconds"]),
                "files_per_minute": float(throughput["scan_files_per_minute"]),
                "openable_files_per_minute": float(throughput["scan_openable_files_per_minute"]),
                "benchmark_files_per_minute": throughput["benchmark_scan_files_per_minute"],
            },
            "processing": {
                "elapsed_seconds": float(throughput["processing_elapsed_seconds"]),
                "processed_files_per_minute": float(throughput["processing_files_per_minute"]),
                "benchmark_processed_files_per_minute": throughput["benchmark_processing_files_per_minute"],
                "operation_timings": throughput.get("processing_operation_timings", {}),
                "benchmark_operation_timings": throughput.get("benchmark_processing_operation_timings", {}),
            },
        },
        "benchmark": {
            "source": benchmark.get("source"),
            "run_count": int(benchmark.get("run_count", 0)),
            "finding_rule_counts_repeated_runs": benchmark.get("finding_rule_counts_repeated_runs", {}),
            "worker_sweep": benchmark.get(
                "worker_sweep",
                {
                    "enabled": False,
                    "operation_timing_presence": False,
                    "workers": [],
                    "recommendation": None,
                },
            ),
        },
        "environment": private_summary["environment"],
        "runtime_hardware": _runtime_hardware_summary(Path(args.out)),
        "cleanup": {
            "enabled": False,
            "removed_artifacts": [],
            "preserved_artifacts": [],
            "retained_public_summary": BASELINE_JSON,
            "elapsed_seconds": 0.0,
        },
        "privacy_self_check": {
            "passed": False,
            "status": "not_run",
            "violation_count": None,
            "violations": [],
        },
    }


def _update_privacy_self_check(args: argparse.Namespace, baseline: dict[str, Any]) -> None:
    leaks = privacy_self_check(baseline, forbidden_values=_forbidden_values(args, Path(args.input), Path(args.out)))
    leaks.extend(_aggregate_privacy_leaks(baseline))
    baseline["privacy_self_check"]["passed"] = not leaks
    baseline["privacy_self_check"]["status"] = "pass" if not leaks else "failed"
    baseline["privacy_self_check"]["violation_count"] = len(leaks)
    baseline["privacy_self_check"]["violations"] = leaks
    if leaks:
        raise ValueError("Privacy self-check found sensitive fields in aggregate baseline summary: " + ", ".join(leaks))


def _runtime_hardware_summary(output_root: Path) -> dict[str, Any]:
    warnings: list[str] = []
    total_memory_gb = _total_memory_gb(warnings)
    disk = _disk_gb(output_root, warnings)
    gpu = _nvidia_gpu_summary(warnings)
    return {
        "schema_version": "scan-qc.runtime-hardware.v1",
        "os_family": platform.system() or None,
        "platform_family": platform.platform(aliased=True, terse=True),
        "python_version_family": _python_version_family(),
        "cpu_logical_count": os.cpu_count(),
        "total_memory_gb": total_memory_gb,
        "output_disk_free_gb": disk["free_gb"],
        "output_disk_total_gb": disk["total_gb"],
        "gpu_visible_count": gpu["visible_count"],
        "gpu_memory_total_gb": gpu["memory_total_gb"],
        "gpu_acceleration_used": False,
        "warnings": warnings,
    }


def _python_version_family() -> str:
    version = sys.version_info
    return f"{version.major}.{version.minor}"


def _total_memory_gb(warnings: list[str]) -> float | None:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None
        warnings.append("psutil unavailable; total memory collected from stdlib fallback when supported.")

    if psutil is not None:
        try:
            return _bytes_to_gb(int(psutil.virtual_memory().total))
        except Exception:
            warnings.append("psutil memory probe failed; total memory collected from stdlib fallback when supported.")

    if hasattr(os, "sysconf"):
        try:
            return _bytes_to_gb(int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES")))
        except (OSError, ValueError, TypeError):
            pass
    warnings.append("total memory unavailable.")
    return None


def _disk_gb(output_root: Path, warnings: list[str]) -> dict[str, float | None]:
    try:
        usage = shutil.disk_usage(output_root.expanduser())
    except OSError:
        warnings.append("output disk usage unavailable.")
        return {"free_gb": None, "total_gb": None}
    return {"free_gb": _bytes_to_gb(usage.free), "total_gb": _bytes_to_gb(usage.total)}


def _nvidia_gpu_summary(warnings: list[str]) -> dict[str, float | int]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        warnings.append("nvidia-smi unavailable; GPU count and memory reported as zero.")
        return {"visible_count": 0, "memory_total_gb": 0.0}
    except (OSError, subprocess.TimeoutExpired):
        warnings.append("nvidia-smi probe failed; GPU count and memory reported as zero.")
        return {"visible_count": 0, "memory_total_gb": 0.0}

    if result.returncode != 0:
        warnings.append("nvidia-smi returned no usable GPU telemetry; GPU count and memory reported as zero.")
        return {"visible_count": 0, "memory_total_gb": 0.0}

    memory_mib: list[float] = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            memory_mib.append(float(text))
        except ValueError:
            warnings.append("nvidia-smi output was not parseable; GPU count and memory reported as zero.")
            return {"visible_count": 0, "memory_total_gb": 0.0}
    return {"visible_count": len(memory_mib), "memory_total_gb": round(sum(memory_mib) / 1024, 3)}


def _bytes_to_gb(value: int) -> float:
    return round(float(value) / (1024**3), 3)


def _aggregate_privacy_leaks(payload: Any) -> list[str]:
    leaks: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
        elif isinstance(value, str):
            lowered = value.lower()
            if _looks_like_private_path(value):
                leaks.append(f"path-like value at {path}")
            if _looks_like_sensitive_filename(lowered):
                leaks.append(f"filename-like value at {path}")
            if _looks_like_hash(value):
                leaks.append(f"hash-like value at {path}")

    visit(payload, "")
    return leaks


def _looks_like_private_path(value: str) -> bool:
    if "/" in value or "\\" in value:
        return value.startswith(("/", "~")) or ":\\" in value or "/users/" in value.lower()
    return False


def _looks_like_sensitive_filename(value: str) -> bool:
    sensitive_extensions = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".jp2", ".pdf")
    return any(token in value for token in sensitive_extensions)


def _looks_like_hash(value: str) -> bool:
    text = value.strip()
    return len(text) in {32, 40, 64} and all(char in "0123456789abcdefABCDEF" for char in text)


def _elapsed_since(start_seconds: float) -> float:
    return _rounded_elapsed(time.perf_counter() - start_seconds)


def _rounded_elapsed(elapsed_seconds: float) -> float:
    return max(0.0, round(float(elapsed_seconds), 6))


def _env_flag(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main())
