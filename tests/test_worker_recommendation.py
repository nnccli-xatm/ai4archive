"""Tests for adaptive worker recommendation (AI4-861)."""

from __future__ import annotations

import unittest

from archive_scan_qc.concurrency import (
    MAX_WORKERS,
    DEFAULT_WORKERS,
    MEMORY_PER_WORKER_GB,
    recommend_workers,
    resolve_worker_count,
    worker_metadata,
)


class TestResolveWorkerCountMemoryCap(unittest.TestCase):
    def test_memory_cap_reduces_workers(self):
        result = resolve_worker_count(8, 100, memory_gb=4.0)
        expected = max(1, int(4.0 / MEMORY_PER_WORKER_GB))
        self.assertLessEqual(result, expected)

    def test_abundant_memory_no_cap(self):
        result = resolve_worker_count(2, 100, memory_gb=64.0)
        self.assertEqual(result, 2)

    def test_backward_compatible_no_memory(self):
        result = resolve_worker_count(None, 100)
        self.assertEqual(result, DEFAULT_WORKERS)

    def test_memory_cap_one_worker_minimum(self):
        result = resolve_worker_count(8, 100, memory_gb=1.0)
        self.assertEqual(result, 1)

    def test_none_memory_ignored(self):
        result = resolve_worker_count(4, 100, memory_gb=None)
        self.assertGreaterEqual(result, 1)


class TestRecommendWorkersHeuristic(unittest.TestCase):
    def test_low_memory_reduces_workers(self):
        rec = recommend_workers(memory_gb=4.0, cpu_count=8)
        self.assertEqual(rec["source"], "heuristic")
        self.assertLessEqual(rec["recommended_workers"], int(4.0 / MEMORY_PER_WORKER_GB))

    def test_high_specs_use_default(self):
        rec = recommend_workers(memory_gb=32.0, cpu_count=8)
        self.assertEqual(rec["recommended_workers"], DEFAULT_WORKERS)

    def test_single_item_forces_one(self):
        rec = recommend_workers(memory_gb=32.0, cpu_count=8, item_count=1)
        self.assertEqual(rec["recommended_workers"], 1)

    def test_capped_by_memory_flag(self):
        rec = recommend_workers(memory_gb=2.0, cpu_count=16)
        self.assertTrue(rec["capped_by_memory"])


class TestRecommendWorkersBenchmark(unittest.TestCase):
    def test_benchmark_overrides_heuristic(self):
        rec = recommend_workers(
            memory_gb=32.0,
            cpu_count=8,
            benchmark_recommendation={"best_requested_workers": 4},
        )
        self.assertEqual(rec["source"], "benchmark")
        self.assertEqual(rec["recommended_workers"], 4)

    def test_benchmark_capped_by_max(self):
        rec = recommend_workers(
            memory_gb=32.0,
            cpu_count=16,
            benchmark_recommendation={"best_requested_workers": 20},
        )
        self.assertEqual(rec["recommended_workers"], MAX_WORKERS)

    def test_invalid_benchmark_falls_back(self):
        rec = recommend_workers(
            memory_gb=32.0,
            cpu_count=8,
            benchmark_recommendation={"best_requested_workers": "bad"},
        )
        self.assertEqual(rec["source"], "heuristic")


class TestWorkerMetadataWithRecommendation(unittest.TestCase):
    def test_metadata_includes_recommendation(self):
        rec = recommend_workers(memory_gb=8.0, cpu_count=4)
        meta = worker_metadata("auto", rec["recommended_workers"], recommendation=rec)
        self.assertEqual(meta["recommendation"]["source"], "heuristic")
        self.assertEqual(meta["effective_workers"], rec["recommended_workers"])

    def test_metadata_without_recommendation(self):
        meta = worker_metadata(2, 2)
        self.assertIsNone(meta["recommendation"])
