#!/usr/bin/env python3
"""Measure NumPy vs fallback despeckle candidate extraction performance.

This script provides aggregate-safe synthetic benchmarks for AI4-868 to prove
whether the NumPy backend is measurably faster than the fallback path for
despeckle candidate extraction only.
"""

from __future__ import annotations

import argparse
import time
from typing import Any
from PIL import Image
import sys
from pathlib import Path

# Add src to path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from archive_scan_qc.processing import (
    _despeckle_candidate_points_with_backend,
    _load_numpy,
)


def create_synth_dark_mask(
    width: int,
    height: int,
    speckle_count: int,
    dense_clusters: bool = False,
    large_clusters: bool = False,
) -> Image.Image:
    """Create a synthetic dark mask with isolated pixels and optional clusters.

    Args:
        width: Mask width in pixels
        height: Mask height in pixels
        speckle_count: Number of isolated dark pixels to add
        dense_clusters: Whether to add dense clusters
        large_clusters: Whether to add larger clusters

    Returns:
        A PIL Image in mode 'L' (grayscale) with dark pixels at 255
    """
    mask = Image.new("L", (width, height), 0)

    # Add isolated speckles
    for i in range(speckle_count):
        x = (i * 37 + 13) % width
        y = (i * 53 + 19) % height
        mask.putpixel((x, y), 255)

    if dense_clusters:
        # Add some dense clusters
        for cluster_start in [(100, 100), (width - 150, 100), (100, height - 150)]:
            cx, cy = cluster_start
            for dx in range(0, 20):
                for dy in range(0, 20):
                    if (dx + dy) % 3 == 0:  # Sparse but clustered
                        mask.putpixel((cx + dx, cy + dy), 255)

    if large_clusters:
        # Add some larger connected regions
        for cluster_start in [(200, 200), (width - 250, 200)]:
            cx, cy = cluster_start
            for dx in range(0, 15):
                for dy in range(0, 15):
                    mask.putpixel((cx + dx, cy + dy), 255)
    
    return mask


def measure_backend_performance(
    mask: Image.Image,
    backend: str,
    iterations: int = 100,
) -> dict[str, Any]:
    """Measure performance of a specific backend for candidate extraction.

    Args:
        mask: Dark mask to process
        backend: Backend to use ('fallback' or 'numpy')
        iterations: Number of iterations to run

    Returns:
        Dictionary with timing and result information
    """
    times = []
    candidates_list = []

    for _ in range(iterations):
        start = time.perf_counter()
        candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend=backend)
        end = time.perf_counter()

        times.append(end - start)
        candidates_list.append(candidates)

    # Verify results are consistent
    first_candidates = candidates_list[0] if candidates_list else []
    for i, candidates in enumerate(candidates_list[1:], 1):
        if set(candidates) != set(first_candidates):
            print(f"Warning: Result inconsistency at iteration {i}")
            print(f"  First: {len(first_candidates)} candidates")
            print(f"  Iter {i}: {len(candidates)} candidates")
    
    return {
        "backend": backend,
        "actual_backend_mode": backend_mode if candidates_list else "none",
        "iterations": iterations,
        "total_time_seconds": sum(times),
        "avg_time_seconds": sum(times) / len(times) if times else 0,
        "min_time_seconds": min(times) if times else 0,
        "max_time_seconds": max(times) if times else 0,
        "candidate_count": len(first_candidates) if first_candidates else 0,
    }


def compare_backends_on_mask(
    mask: Image.Image,
    iterations: int = 100,
) -> dict[str, Any]:
    """Compare fallback vs NumPy backends on a single mask.

    Args:
        mask: Dark mask to test
        iterations: Number of iterations per backend

    Returns:
        Dictionary with comparison results
    """
    numpy_available = _load_numpy() is not None

    fallback_result = measure_backend_performance(mask, "fallback", iterations)

    if numpy_available:
        numpy_result = measure_backend_performance(mask, "numpy", iterations)
    else:
        numpy_result = {
            "backend": "numpy",
            "actual_backend_mode": "unavailable",
            "iterations": 0,
            "total_time_seconds": 0,
            "avg_time_seconds": 0,
            "min_time_seconds": 0,
            "max_time_seconds": 0,
            "candidate_count": 0,
            "error": "NumPy not available",
        }
    
    # Calculate speedup
    speedup = None
    if numpy_available and numpy_result["avg_time_seconds"] > 0:
        speedup = fallback_result["avg_time_seconds"] / numpy_result["avg_time_seconds"]

    return {
        "mask_size": mask.size,
        "numpy_available": numpy_available,
        "fallback": fallback_result,
        "numpy": numpy_result,
        "speedup": speedup,
        "conclusion": _conclusion_for_comparison(speedup, numpy_available),
    }


def benchmark_configs(include_large: bool) -> list[tuple[str, int, int, int, bool, bool]]:
    """Return bounded default benchmark cases, with large cases opt-in."""
    configs = [
        ("Small sparse", 640, 900, 80, False, False),
        ("Small medium", 640, 900, 200, True, False),
        ("Small dense", 640, 900, 500, True, True),
        ("Medium sparse", 1600, 2200, 200, False, False),
        ("Medium medium", 1600, 2200, 500, True, False),
        ("Medium dense", 1600, 2200, 1200, True, True),
    ]
    if include_large:
        configs.extend(
            [
                ("Large sparse", 4000, 6000, 400, False, False),
                ("Large medium", 4000, 6000, 1000, True, False),
                ("Large dense", 4000, 6000, 3000, True, True),
            ]
        )
    return configs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run aggregate-safe synthetic despeckle candidate backend benchmarks."
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Iterations per backend and mask. Defaults to a bounded quick benchmark.",
    )
    parser.add_argument(
        "--include-large",
        action="store_true",
        help="Include 4000x6000 synthetic masks. This is intentionally opt-in.",
    )
    return parser.parse_args()


def _conclusion_for_comparison(speedup: float | None, numpy_available: bool) -> str:
    """Generate a text conclusion for the comparison."""
    if not numpy_available:
        return "NumPy backend unavailable - cannot compare"
    if speedup is None or speedup <= 1.0:
        return f"No measurable speedup (ratio: {speedup:.2f}x or less)"
    elif speedup < 1.5:
        return f"Modest speedup: {speedup:.2f}x"
    elif speedup < 3.0:
        return f"Significant speedup: {speedup:.2f}x"
    else:
        return f"Major speedup: {speedup:.2f}x"


def main() -> None:
    """Run synthetic performance benchmarks for despeckle candidate extraction."""
    args = parse_args()
    if args.iterations < 1:
        raise SystemExit("--iterations must be at least 1")

    print("=" * 70)
    print("AI4-868: Despeckle Candidate Extraction Performance Benchmark")
    print("=" * 70)

    numpy_available = _load_numpy() is not None
    print(f"\nNumPy available: {numpy_available}")
    iterations = args.iterations
    print(f"Iterations per test: {iterations}\n")
    if not args.include_large:
        print("Large 4000x6000 masks are skipped by default; pass --include-large for deep runs.\n")

    test_configs = benchmark_configs(args.include_large)

    all_results = []

    for name, width, height, speckles, dense, large in test_configs:
        print(f"\n{name} ({width}x{height}):")
        print("-" * 70)

        mask = create_synth_dark_mask(width, height, speckles, dense, large)
        result = compare_backends_on_mask(mask, iterations)
        all_results.append((name, result))

        print(f"  Fallback: {result['fallback']['avg_time_seconds']:.6f}s avg ({result['fallback']['candidate_count']} candidates)")
        if numpy_available:
            print(f"  NumPy:     {result['numpy']['avg_time_seconds']:.6f}s avg ({result['numpy']['candidate_count']} candidates)")
            print(f"  Speedup:   {result['speedup']:.2f}x")
        else:
            print(f"  NumPy:     unavailable")
        print(f"  Conclusion: {result['conclusion']}")

    # Print summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    if not numpy_available:
        print("\nNumPy backend unavailable - cannot measure performance improvement.")
        print("The implementation provides optional fast path but NumPy is not installed.")
        return

    measurable_speedups = [r[1]["speedup"] for r in all_results if r[1]["speedup"] is not None]

    if measurable_speedups:
        avg_speedup = sum(measurable_speedups) / len(measurable_speedups)
        min_speedup = min(measurable_speedups)
        max_speedup = max(measurable_speedups)

        print(f"\nAverage speedup across {len(measurable_speedups)} tests: {avg_speedup:.2f}x")
        print(f"Minimum speedup: {min_speedup:.2f}x")
        print(f"Maximum speedup: {max_speedup:.2f}x")

        if avg_speedup > 1.5:
            print("\n[PASS] NumPy backend provides MEASURABLE performance improvement")
            print("  Recommendation: Continue broader vectorization work")
        elif avg_speedup > 1.0:
            print("\n~ NumPy backend provides MODEST performance improvement")
            print("  Recommendation: Consider broader vectorization with caution")
        else:
            print("\n[WARN] NumPy backend does NOT provide measurable speedup")
            print("  Recommendation: Investigate why NumPy path is slower or equal")
    else:
        print("\nNo measurable speedup data available")


if __name__ == "__main__":
    main()

