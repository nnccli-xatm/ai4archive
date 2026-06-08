"""Test NumPy backend parity for despeckle candidate selection (AI4-867).

This test suite validates that the NumPy backend produces identical results
to the fallback backend across all AI4-866 parity anchors.
"""

import unittest

from PIL import Image

# Add src to path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archive_scan_qc.processing import (
    _despeckle_candidate_points_with_backend,
    _despeckle_candidate_points_fallback,
    _despeckle_protected_edge_margin,
    _load_numpy,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from measure_ai4_868_despeckle_performance import benchmark_configs

NUMPY_AVAILABLE = _load_numpy() is not None


@unittest.skipUnless(NUMPY_AVAILABLE, "NumPy optional fast path unavailable")
class TestNumPyBackendAvailability(unittest.TestCase):
    """Test NumPy backend availability and loading."""

    def test_numpy_available(self):
        """NumPy should be available for testing."""
        np = _load_numpy()
        self.assertIsNotNone(np, "NumPy is required for AI4-867 backend tests")


@unittest.skipUnless(NUMPY_AVAILABLE, "NumPy optional fast path unavailable")
class TestNumPyBackendParity(unittest.TestCase):
    """Verify NumPy backend produces identical results to fallback."""

    def test_single_isolated_pixel_parity(self):
        """Single isolated pixel should be candidate in both backends."""
        mask = Image.new("L", (100, 100), 0)
        mask.putpixel((50, 50), 255)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(set(fallback_candidates), set(numpy_candidates))
        self.assertEqual(len(fallback_candidates), 1)
        self.assertIn((50, 50), numpy_candidates)

    def test_multiple_isolated_pixels_parity(self):
        """Multiple isolated pixels should be candidates in both backends."""
        mask = Image.new("L", (100, 100), 0)
        positions = [(20, 30), (40, 50), (60, 70)]
        for x, y in positions:
            mask.putpixel((x, y), 255)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(set(fallback_candidates), set(numpy_candidates))
        self.assertEqual(len(fallback_candidates), 3)

    def test_edge_protection_parity(self):
        """Edge protection should work identically in both backends."""
        mask = Image.new("L", (100, 100), 0)
        margin = _despeckle_protected_edge_margin(100, 100)

        # Pixels just inside margin should be excluded
        edge_positions = [
            (margin - 1, 50),  # Left edge inside margin
            (50, margin - 1),  # Top edge inside margin
            (100 - margin, 50),  # Right edge inside margin
            (50, 100 - margin),  # Bottom edge inside margin
        ]

        for x, y in edge_positions:
            mask.putpixel((x, y), 255)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(len(fallback_candidates), 0)
        self.assertEqual(len(numpy_candidates), 0)

    def test_tiny_2x2_cluster_parity(self):
        """2x2 cluster should be candidate in both backends."""
        mask = Image.new("L", (100, 100), 0)
        # Create a 2x2 cluster
        for x in range(50, 52):
            for y in range(50, 52):
                mask.putpixel((x, y), 255)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(set(fallback_candidates), set(numpy_candidates))
        self.assertEqual(len(fallback_candidates), 4)

    def test_tiny_3x3_cluster_parity(self):
        """3x3 cluster should be candidate in both backends."""
        mask = Image.new("L", (100, 100), 0)
        # Create a 3x3 cluster
        for x in range(50, 53):
            for y in range(50, 53):
                mask.putpixel((x, y), 255)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(set(fallback_candidates), set(numpy_candidates))
        self.assertEqual(len(fallback_candidates), 9)

    def test_4x4_cluster_excluded_parity(self):
        """4x4 cluster should be excluded in both backends."""
        mask = Image.new("L", (100, 100), 0)
        # Create a 4x4 cluster
        for x in range(50, 54):
            for y in range(50, 54):
                mask.putpixel((x, y), 255)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(len(fallback_candidates), 0)
        self.assertEqual(len(numpy_candidates), 0)

    def test_empty_mask_parity(self):
        """Empty mask should produce empty results in both backends."""
        mask = Image.new("L", (100, 100), 0)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(len(fallback_candidates), 0)
        self.assertEqual(len(numpy_candidates), 0)

    def test_small_image_below_threshold_parity(self):
        """Images below 3x3 should produce empty results in both backends."""
        mask = Image.new("L", (2, 2), 0)
        mask.putpixel((1, 1), 255)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(len(fallback_candidates), 0)
        self.assertEqual(len(numpy_candidates), 0)

    def test_deterministic_output_parity(self):
        """Output should be deterministic and identical across runs."""
        mask = Image.new("L", (100, 100), 0)
        positions = [(20, 30), (45, 50), (60, 70), (25, 35)]
        for x, y in positions:
            mask.putpixel((x, y), 255)

        numpy_candidates_1, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")
        numpy_candidates_2, _ = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(numpy_candidates_1, numpy_candidates_2)
        self.assertEqual(numpy_candidates_1, sorted(numpy_candidates_1, key=lambda p: (p[1], p[0])))


class TestNumPyBackendFallback(unittest.TestCase):
    """Test that NumPy backend falls back correctly when NumPy is unavailable."""

    def test_backend_mode_reporting(self):
        """Backend mode should be correctly reported."""
        mask = Image.new("L", (100, 100), 0)
        mask.putpixel((50, 50), 255)

        candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        # When NumPy is available, should report "numpy"
        # When NumPy is unavailable, would fall back to "fallback"
        np = _load_numpy()
        if np is not None:
            self.assertEqual(backend_mode, "numpy")
        else:
            self.assertEqual(backend_mode, "fallback")

        # Should always produce candidates regardless of backend
        self.assertEqual(len(candidates), 1)


class TestSyntheticPerformanceBenchmarkConfiguration(unittest.TestCase):
    """Guard the AI4-868 benchmark against runaway default runtimes."""

    def test_default_benchmark_configs_exclude_large_masks(self):
        configs = benchmark_configs(include_large=False)

        self.assertEqual(len(configs), 6)
        for name, width, height, *_ in configs:
            self.assertNotIn("Large", name)
            self.assertLessEqual(width * height, 1600 * 2200)

    def test_large_benchmark_configs_are_explicit_opt_in(self):
        configs = benchmark_configs(include_large=True)

        self.assertGreater(len(configs), len(benchmark_configs(include_large=False)))
        self.assertTrue(any(width == 4000 and height == 6000 for _, width, height, *_ in configs))


@unittest.skipUnless(NUMPY_AVAILABLE, "NumPy optional fast path unavailable")
class TestSyntheticPerformanceBenchmark(unittest.TestCase):
    """Test synthetic performance benchmark for AI4-868."""

    def test_synthetic_mask_consistency(self):
        """Both backends should produce identical results on synthetic masks."""
        # Create a synthetic dark mask similar to the performance benchmark
        mask = Image.new("L", (640, 900), 0)
        for i in range(80):
            x = (i * 37 + 13) % 640
            y = (i * 53 + 19) % 900
            mask.putpixel((x, y), 255)

        # Add some clusters
        for cx, cy in [(100, 100), (540, 100), (100, 850)]:
            for dx in range(0, 20):
                for dy in range(0, 20):
                    if (dx + dy) % 3 == 0:
                        mask.putpixel((cx + dx, cy + dy), 255)

        # Both backends should produce identical results
        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(set(fallback_candidates), set(numpy_candidates))
        # Verify we have a reasonable number of candidates for this mask
        self.assertGreater(len(fallback_candidates), 50)
        self.assertLess(len(fallback_candidates), 300)

    def test_synthetic_medium_mask_consistency(self):
        """Both backends should produce identical results on medium synthetic masks."""
        mask = Image.new("L", (1600, 2200), 0)
        for i in range(200):
            x = (i * 37 + 13) % 1600
            y = (i * 53 + 19) % 2200
            mask.putpixel((x, y), 255)

        # Add some clusters
        for cx, cy in [(500, 500), (1400, 500), (500, 2000)]:
            for dx in range(0, 30):
                for dy in range(0, 30):
                    if (dx + dy) % 4 == 0:
                        mask.putpixel((cx + dx, cy + dy), 255)

        fallback_candidates, _ = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        numpy_candidates, backend_mode = _despeckle_candidate_points_with_backend(mask, backend="numpy")

        self.assertEqual(backend_mode, "numpy")
        self.assertEqual(set(fallback_candidates), set(numpy_candidates))
        self.assertGreater(len(fallback_candidates), 100)
        self.assertLess(len(fallback_candidates), 600)


if __name__ == "__main__":
    unittest.main()
