"""Bounded worker-count helpers for local batch work."""

from __future__ import annotations

import os
from typing import Any

MAX_WORKERS = 8
DEFAULT_WORKERS = 2
MEMORY_PER_WORKER_GB = 3.0


def resolve_worker_count(requested_workers: int | None, item_count: int, *, memory_gb: float | None = None) -> int:
    """Return a conservative effective worker count for image batches."""
    if requested_workers is not None and requested_workers < 1:
        raise ValueError("--workers must be a positive integer.")
    if item_count <= 0:
        return 1

    cpu_count = os.cpu_count() or 1
    configured = requested_workers if requested_workers is not None else DEFAULT_WORKERS
    effective = max(1, min(configured, cpu_count, MAX_WORKERS, item_count))

    if memory_gb is not None and memory_gb > 0:
        memory_cap = max(1, int(memory_gb / MEMORY_PER_WORKER_GB))
        effective = min(effective, memory_cap)

    return effective


def recommend_workers(
    *,
    memory_gb: float | None = None,
    cpu_count: int | None = None,
    item_count: int = 0,
    benchmark_recommendation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recommend worker count from benchmark data or hardware heuristic."""
    if benchmark_recommendation is not None:
        bw = benchmark_recommendation.get("best_requested_workers")
        if isinstance(bw, int) and bw >= 1:
            return {
                "recommended_workers": min(bw, MAX_WORKERS),
                "source": "benchmark",
                "capped_by_memory": False,
                "capped_by_cpu": False,
                "memory_gb": memory_gb,
            }

    cpus = cpu_count or os.cpu_count() or 1
    effective = min(DEFAULT_WORKERS, cpus, MAX_WORKERS)
    if item_count > 0:
        effective = min(effective, item_count)

    capped_by_memory = False
    if memory_gb is not None and memory_gb > 0:
        memory_cap = max(1, int(memory_gb / MEMORY_PER_WORKER_GB))
        if memory_cap < effective:
            capped_by_memory = True
            effective = memory_cap

    capped_by_cpu = effective >= cpus

    return {
        "recommended_workers": max(1, effective),
        "source": "heuristic",
        "capped_by_memory": capped_by_memory,
        "capped_by_cpu": capped_by_cpu,
        "memory_gb": memory_gb,
    }


def _detect_memory_gb() -> float | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return round(psutil.virtual_memory().total / (1024**3), 1)
    except (ImportError, OSError):
        pass
    if hasattr(os, "sysconf"):
        try:
            return round(
                (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024**3), 1
            )
        except (OSError, ValueError, TypeError):
            pass
    return None


def concurrency_mode(worker_count: int) -> str:
    return "serial" if worker_count == 1 else "parallel"


def worker_metadata(
    requested_workers: int | None, effective_workers: int, *, recommendation: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "requested_workers": requested_workers,
        "effective_workers": effective_workers,
        "worker_cap": MAX_WORKERS,
        "mode": concurrency_mode(effective_workers),
        "recommendation": recommendation,
    }
