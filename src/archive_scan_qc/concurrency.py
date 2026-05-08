"""Bounded worker-count helpers for local batch work."""

from __future__ import annotations

import os
from typing import Any

MAX_WORKERS = 8
DEFAULT_WORKERS = 2


def resolve_worker_count(requested_workers: int | None, item_count: int) -> int:
    """Return a conservative effective worker count for image batches."""
    if requested_workers is not None and requested_workers < 1:
        raise ValueError("--workers must be a positive integer.")
    if item_count <= 0:
        return 1

    cpu_count = os.cpu_count() or 1
    configured = requested_workers if requested_workers is not None else DEFAULT_WORKERS
    return max(1, min(configured, cpu_count, MAX_WORKERS, item_count))


def concurrency_mode(worker_count: int) -> str:
    return "serial" if worker_count == 1 else "parallel"


def worker_metadata(requested_workers: int | None, effective_workers: int) -> dict[str, Any]:
    return {
        "requested_workers": requested_workers,
        "effective_workers": effective_workers,
        "worker_cap": MAX_WORKERS,
        "mode": concurrency_mode(effective_workers),
    }
