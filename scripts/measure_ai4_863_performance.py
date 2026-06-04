#!/usr/bin/env python
"""Performance measurement script for AI4-863 optimizations.

This script measures the performance improvements from scan measurement reuse
and resume processing optimizations.
"""

import argparse
import json
import time
from pathlib import Path
from typing import Any

try:
    from archive_scan_qc.cli import main
    from archive_scan_qc.processing import ProcessingOptions, process_images
    from archive_scan_qc.processing_plan import build_processing_plan
    from archive_scan_qc.scanner import ScanConfig, scan_batch
    from archive_scan_qc.reports import write_reports
except ImportError as exc:
    raise ImportError(
        f"Cannot import archive_scan_qc: {exc}. "
        "Make sure you're running from the repository root."
    ) from exc


def measure_scan(
    input_dir: Path,
    output_dir: Path,
    workers: int = 1,
) -> dict[str, Any]:
    """Measure scanning performance."""
    print(f"Measuring scan performance on {input_dir}...")
    
    start_time = time.perf_counter()
    config = ScanConfig(
        project_id="ai4-863-benchmark",
        batch_id="benchmark",
        input_dir=input_dir,
        output_dir=output_dir,
        workers=workers,
    )
    report = scan_batch(config)
    write_reports(report, output_dir)
    elapsed = time.perf_counter() - start_time
    
    return {
        "operation": "scan",
        "elapsed_seconds": elapsed,
        "files_processed": report["summary"]["total_files"],
        "files_per_minute": report["summary"]["performance"]["files_per_minute"],
        "workers": report["summary"]["performance"]["workers"],
    }


def measure_processing_plan(
    scan_report: Path,
    input_dir: Path,
    output_dir: Path,
    reuse_measurements: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    """Measure processing plan performance."""
    label = "WITH scan measurement reuse" if reuse_measurements else "WITHOUT scan measurement reuse"
    print(f"Measuring processing plan performance ({label})...")
    
    report = json.loads(scan_report.read_text(encoding="utf-8"))
    options = ProcessingOptions(
        deskew=True,
        trim_dark_border=True,
        reuse_scan_measurements=reuse_measurements,
        workers=workers,
    )
    
    start_time = time.perf_counter()
    plan = build_processing_plan(report, input_dir, options)
    elapsed = time.perf_counter() - start_time
    
    # Calculate metrics
    scan_reused_count = sum(1 for record in plan["files"] if record.get("scan_measurements_reused"))
    scan_reused_ratio = scan_reused_count / len(plan["files"]) if plan["files"] else 0.0
    
    return {
        "operation": "processing_plan",
        "reuse_measurements": reuse_measurements,
        "elapsed_seconds": elapsed,
        "files_processed": plan["summary"]["total_files"],
        "files_per_minute": (plan["summary"]["total_files"] / elapsed) * 60 if elapsed > 0 else 0.0,
        "scan_measurements_reused_count": scan_reused_count,
        "scan_measurements_reused_ratio": scan_reused_ratio,
        "workers": workers,
    }


def measure_full_processing(
    scan_report: Path,
    input_dir: Path,
    output_dir: Path,
    reuse_measurements: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    """Measure full processing performance."""
    label = "WITH scan measurement reuse" if reuse_measurements else "WITHOUT scan measurement reuse"
    print(f"Measuring full processing performance ({label})...")
    
    report = json.loads(scan_report.read_text(encoding="utf-8"))
    options = ProcessingOptions(
        deskew=True,
        trim_dark_border=True,
        reuse_scan_measurements=reuse_measurements,
        workers=workers,
    )
    
    start_time = time.perf_counter()
    result = process_images(report, input_dir, output_dir, options)
    elapsed = time.perf_counter() - start_time
    
    return {
        "operation": "full_processing",
        "reuse_measurements": reuse_measurements,
        "elapsed_seconds": elapsed,
        "files_processed": result["summary"]["processed_files"],
        "files_per_minute": result["summary"]["performance"]["processed_files_per_minute"],
        "scan_measurement_reuse": result["summary"]["performance"]["scan_measurement_reuse"],
        "workers": result["summary"]["workers"],
    }


def measure_resume_processing(
    initial_manifest: Path,
    scan_report: Path,
    input_dir: Path,
    output_dir: Path,
    workers: int = 1,
) -> dict[str, Any]:
    """Measure resume processing performance."""
    print("Measuring resume processing performance...")
    
    report = json.loads(scan_report.read_text(encoding="utf-8"))
    options = ProcessingOptions(
        deskew=True,
        trim_dark_border=True,
        resume_processing=True,
        reuse_scan_measurements=True,
        workers=workers,
    )
    
    start_time = time.perf_counter()
    result = process_images(report, input_dir, output_dir, options)
    elapsed = time.perf_counter() - start_time
    
    return {
        "operation": "resume_processing",
        "elapsed_seconds": elapsed,
        "files_processed": result["summary"]["processed_files"],
        "files_resumed": result["summary"]["resumed_files"],
        "existing_derivative_reused_files": result["summary"]["existing_derivative_reused_files"],
        "files_per_minute": result["summary"]["performance"]["processed_files_per_minute"],
        "workers": result["summary"]["workers"],
    }


def run_comprehensive_benchmark(
    input_dir: Path,
    output_root: Path,
    workers: int = 1,
    skip_processing: bool = False,
) -> dict[str, Any]:
    """Run comprehensive performance benchmark."""
    print(f"Starting comprehensive benchmark on {input_dir}...")
    print(f"Output directory: {output_root}")
    print(f"Workers: {workers}")
    print()
    
    output_root.mkdir(parents=True, exist_ok=True)
    
    results = {
        "benchmark_metadata": {
            "input_dir": str(input_dir),
            "output_root": str(output_root),
            "workers": workers,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "measurements": [],
    }
    
    # Step 1: Scan
    scan_dir = output_root / "scan"
    scan_result = measure_scan(input_dir, scan_dir, workers)
    results["measurements"].append(scan_result)
    print(f"? Scan completed: {scan_result['files_per_minute']:.2f} files/minute")
    print()
    
    scan_report_path = scan_dir / "scan_qc_report.json"
    
    # Step 2: Processing plan WITHOUT reuse
    plan_no_reuse_dir = output_root / "plan_no_reuse"
    plan_no_reuse_result = measure_processing_plan(
        scan_report_path,
        input_dir,
        plan_no_reuse_dir,
        reuse_measurements=False,
        workers=workers,
    )
    results["measurements"].append(plan_no_reuse_result)
    print(f"? Processing plan (no reuse): {plan_no_reuse_result['files_per_minute']:.2f} files/minute")
    print()
    
    # Step 3: Processing plan WITH reuse
    plan_reuse_dir = output_root / "plan_reuse"
    plan_reuse_result = measure_processing_plan(
        scan_report_path,
        input_dir,
        plan_reuse_dir,
        reuse_measurements=True,
        workers=workers,
    )
    results["measurements"].append(plan_reuse_result)
    print(f"? Processing plan (with reuse): {plan_reuse_result['files_per_minute']:.2f} files/minute")
    print(f"  Scan measurements reused: {plan_reuse_result['scan_measurements_reused_count']}/{plan_reuse_result['files_processed']} ({plan_reuse_result['scan_measurements_reused_ratio']:.1%})")
    print()
    
    # Calculate improvement
    if plan_no_reuse_result["elapsed_seconds"] > 0:
        speedup = plan_no_reuse_result["elapsed_seconds"] / plan_reuse_result["elapsed_seconds"]
        print(f"  Speedup: {speedup:.2f}x")
    print()
    
    if skip_processing:
        print("Skipping full processing measurement (--skip-processing)")
    else:
        # Step 4: Full processing WITHOUT reuse
        process_no_reuse_dir = output_root / "process_no_reuse"
        process_no_reuse_result = measure_full_processing(
            scan_report_path,
            input_dir,
            process_no_reuse_dir,
            reuse_measurements=False,
            workers=workers,
        )
        results["measurements"].append(process_no_reuse_result)
        print(f"? Full processing (no reuse): {process_no_reuse_result['files_per_minute']:.2f} files/minute")
        print()
        
        # Step 5: Full processing WITH reuse
        process_reuse_dir = output_root / "process_reuse"
        process_reuse_result = measure_full_processing(
            scan_report_path,
            input_dir,
            process_reuse_dir,
            reuse_measurements=True,
            workers=workers,
        )
        results["measurements"].append(process_reuse_result)
        print(f"? Full processing (with reuse): {process_reuse_result['files_per_minute']:.2f} files/minute")
        print()
        
        # Step 6: Resume processing
        resume_dir = output_root / "resume"
        resume_result = measure_resume_processing(
            process_reuse_dir / "processing_manifest.json",
            scan_report_path,
            input_dir,
            resume_dir,
            workers,
        )
        results["measurements"].append(resume_result)
        print(f"? Resume processing: {resume_result['files_per_minute']:.2f} files/minute")
        print(f"  Files resumed: {resume_result['files_resumed']}")
        print(f"  Derivatives reused: {resume_result['existing_derivative_reused_files']}")
        print()
    
    # Calculate overall improvement
    results["summary"] = _calculate_summary(results["measurements"])
    
    # Save results
    results_path = output_root / "benchmark_results.json"
    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"? Results saved to {results_path}")
    
    return results


def _calculate_summary(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate performance improvement summary."""
    summary = {
        "processing_plan_speedup": None,
        "full_processing_speedup": None,
        "resume_processing_improvement": None,
    }
    
    # Find processing plan measurements
    plan_no_reuse = next((m for m in measurements if m["operation"] == "processing_plan" and not m["reuse_measurements"]), None)
    plan_reuse = next((m for m in measurements if m["operation"] == "processing_plan" and m["reuse_measurements"]), None)
    
    if plan_no_reuse and plan_reuse:
        if plan_no_reuse["elapsed_seconds"] > 0:
            summary["processing_plan_speedup"] = plan_no_reuse["elapsed_seconds"] / plan_reuse["elapsed_seconds"]
    
    # Find full processing measurements
    process_no_reuse = next((m for m in measurements if m["operation"] == "full_processing" and not m["reuse_measurements"]), None)
    process_reuse = next((m for m in measurements if m["operation"] == "full_processing" and m["reuse_measurements"]), None)
    
    if process_no_reuse and process_reuse:
        if process_no_reuse["elapsed_seconds"] > 0:
            summary["full_processing_speedup"] = process_no_reuse["elapsed_seconds"] / process_reuse["elapsed_seconds"]
    
    # Find resume processing measurement
    resume = next((m for m in measurements if m["operation"] == "resume_processing"), None)
    if resume and process_no_reuse:
        if process_no_reuse["elapsed_seconds"] > 0:
            summary["resume_processing_improvement"] = process_no_reuse["elapsed_seconds"] / resume["elapsed_seconds"]
    
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="measure_ai4_863_performance",
        description="Measure AI4-863 performance improvements",
    )
    parser.add_argument("--input", required=True, type=Path, help="Input directory with images")
    parser.add_argument("--out", required=True, type=Path, help="Output directory for results")
    parser.add_argument("--workers", default=1, type=int, help="Worker count")
    parser.add_argument("--skip-processing", action="store_true", help="Skip expensive full processing measurements")
    
    args = parser.parse_args()
    
    try:
        results = run_comprehensive_benchmark(
            args.input,
            args.out,
            args.workers,
            args.skip_processing,
        )
        
        print()
        print("=" * 60)
        print("BENCHMARK SUMMARY")
        print("=" * 60)
        summary = results["summary"]
        if summary["processing_plan_speedup"]:
            print(f"Processing plan speedup: {summary['processing_plan_speedup']:.2f}x")
        if summary["full_processing_speedup"]:
            print(f"Full processing speedup: {summary['full_processing_speedup']:.2f}x")
        if summary["resume_processing_improvement"]:
            print(f"Resume processing improvement: {summary['resume_processing_improvement']:.2f}x")
        print("=" * 60)
        
        return 0
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
