"""Synthetic tests for despeckle fallback behavior parity (AI4-866).

This test suite documents the current fallback candidate behavior for:
- Isolated pixels
- Tiny clusters  
- Larger clusters
- Edge-protected pixels
- Dense connected content candidate behavior
- Backend fallback behavior

These tests provide explicit parity anchors for future vectorized implementations.
"""

from __future__ import annotations

import unittest

from PIL import Image

# Add src to path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archive_scan_qc.processing import (
    _despeckle_candidate_points_fallback,
    _despeckle_candidate_points_from_dark_points,
    _despeckle_candidate_points_with_backend,
    _despeckle_protected_edge_margin,
)


class TestDespeckleFallbackIsolatedPixels(unittest.TestCase):
    """Test fallback behavior for isolated single-pixel candidates."""
    
    def test_single_isolated_pixel_is_candidate(self):
        """Single isolated dark pixel in middle of image should be a candidate."""
        mask = Image.new("L", (100, 100), 0)
        mask.putpixel((50, 50), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        self.assertEqual(len(candidates), 1)
        self.assertIn((50, 50), candidates)
    
    def test_multiple_isolated_pixels_all_candidates(self):
        """Multiple isolated dark pixels should all be candidates."""
        mask = Image.new("L", (100, 100), 0)
        positions = [(20, 30), (40, 50), (60, 70)]
        for x, y in positions:
            mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        self.assertEqual(len(candidates), 3)
        for pos in positions:
            self.assertIn(pos, candidates)
    
    def test_isolated_pixel_too_close_to_edge_excluded(self):
        """Isolated pixels near protected edge margin should be excluded."""
        mask = Image.new("L", (100, 100), 0)
        margin = _despeckle_protected_edge_margin(100, 100)
        
        # Test edge positions just inside margin
        edge_positions = [
            (margin - 1, 50),  # Left edge inside margin
            (50, margin - 1),  # Top edge inside margin
            (100 - margin, 50),  # Right edge inside margin
            (50, 100 - margin),  # Bottom edge inside margin
        ]
        
        for x, y in edge_positions:
            mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # All edge-adjacent candidates should be excluded
        self.assertEqual(len(candidates), 0)
    
    def test_isolated_pixel_at_excluded_border(self):
        """Pixels at absolute image borders are always excluded."""
        mask = Image.new("L", (100, 100), 0)
        border_positions = [
            (0, 50), (99, 50), (50, 0), (50, 99),  # Image borders
        ]
        
        for x, y in border_positions:
            mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Border pixels should be excluded
        self.assertEqual(len(candidates), 0)


class TestDespeckleFallbackTinyClusters(unittest.TestCase):
    """Test fallback behavior for tiny cluster candidates."""
    
    def test_2x2_tiny_cluster_is_candidate(self):
        """2x2 connected cluster should be a candidate."""
        mask = Image.new("L", (100, 100), 0)
        # Create 2x2 cluster
        for x in range(45, 47):
            for y in range(45, 47):
                mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # All 4 pixels in cluster should be candidates
        self.assertEqual(len(candidates), 4)
    
    def test_3x3_cluster_is_candidate(self):
        """3x3 cluster (9 pixels) is within short lint streak range and should be a candidate."""
        mask = Image.new("L", (100, 100), 0)
        # Create 3x3 cluster
        for x in range(45, 48):
            for y in range(45, 48):
                mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # 3x3 cluster should be a candidate (within short lint streak range)
        self.assertEqual(len(candidates), 9)
    
    def test_linear_3_pixel_streak_is_candidate(self):
        """Linear streak of 3 pixels within max span should be a candidate."""
        mask = Image.new("L", (100, 100), 0)
        # Horizontal streak
        for x in range(45, 48):
            mask.putpixel((x, 50), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Linear 3-pixel streak should be a candidate
        self.assertEqual(len(candidates), 3)
    
    def test_linear_12_pixel_streak_is_candidate(self):
        """Linear streak of 12 pixels is at max short lint streak limit and should be a candidate."""
        mask = Image.new("L", (100, 100), 0)
        # Horizontal streak at max short lint streak size
        for x in range(45, 57):
            mask.putpixel((x, 50), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # 12-pixel streak should be a candidate (max short lint streak)
        self.assertEqual(len(candidates), 12)
    
    def test_diagonal_3_pixel_streak_is_candidate(self):
        """Diagonal streak of 3 pixels within max span should be a candidate."""
        mask = Image.new("L", (100, 100), 0)
        # Diagonal streak
        for i in range(3):
            mask.putpixel((45 + i, 45 + i), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Diagonal 3-pixel streak should be a candidate
        self.assertEqual(len(candidates), 3)
    
    def test_multiple_tiny_clusters_without_context(self):
        """Multiple separated tiny clusters without context should all be candidates."""
        mask = Image.new("L", (100, 100), 0)
        # Create separated 2x2 clusters
        clusters = [
            [(20, 20), (21, 20), (20, 21), (21, 21)],
            [(50, 50), (51, 50), (50, 51), (51, 51)],
            [(80, 80), (81, 80), (80, 81), (81, 81)],
        ]
        for cluster in clusters:
            for x, y in cluster:
                mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # All separated tiny clusters should be candidates
        self.assertEqual(len(candidates), 12)


class TestDespeckleFallbackLargerClusters(unittest.TestCase):
    """Test fallback behavior for larger clusters."""
    
    def test_4x4_cluster_excluded(self):
        """4x4 cluster (16 pixels) exceeds short lint streak range and should be excluded."""
        mask = Image.new("L", (100, 100), 0)
        # Create 4x4 cluster
        for x in range(45, 49):
            for y in range(45, 49):
                mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # 4x4 cluster should be excluded (exceeds max short lint streak size)
        self.assertEqual(len(candidates), 0)
    
    def test_horizontal_line_exceeding_short_lint_streak(self):
        """Horizontal line beyond short lint streak range should be excluded."""
        mask = Image.new("L", (100, 100), 0)
        # Create horizontal line beyond short lint streak range
        for x in range(45, 60):
            mask.putpixel((x, 50), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Should be excluded (exceeds short lint streak max of 12 pixels)
        self.assertEqual(len(candidates), 0)
    
    def test_irregular_large_shape_excluded(self):
        """Irregular large shape exceeding component limits should be excluded."""
        mask = Image.new("L", (100, 100), 0)
        # Create irregular shape exceeding limits
        positions = [
            (45, 45), (46, 45), (47, 45), (48, 45), (49, 45),
            (45, 46), (49, 46),
            (45, 47), (49, 47),
            (45, 48), (46, 48), (47, 48), (48, 48), (49, 48),
        ]
        for x, y in positions:
            mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Should be excluded (irregular shape exceeds component limits)
        self.assertEqual(len(candidates), 0)


class TestDespeckleFallbackEdgeProtection(unittest.TestCase):
    """Test fallback behavior for edge-protected pixels."""
    
    def test_protected_edge_margin_calculation(self):
        """Edge margin calculation should follow size-based formula."""
        # Small image
        margin_small = _despeckle_protected_edge_margin(50, 50)
        self.assertGreaterEqual(margin_small, 1)
        self.assertLessEqual(margin_small, 5)
        
        # Medium image  
        margin_medium = _despeckle_protected_edge_margin(120, 120)
        self.assertGreaterEqual(margin_medium, 1)
        self.assertLessEqual(margin_medium, 5)
        
        # Large image
        margin_large = _despeckle_protected_edge_margin(1000, 1000)
        self.assertEqual(margin_large, 5)
    
    def test_pixels_inside_protected_margin_excluded(self):
        """All pixels inside protected margin should be excluded."""
        mask = Image.new("L", (120, 120), 0)
        margin = _despeckle_protected_edge_margin(120, 120)
        
        # Create candidates inside margin
        for x in range(margin):
            for y in range(10, 20):
                mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # All margin pixels should be excluded
        self.assertEqual(len(candidates), 0)
    
    def test_pixels_outside_protected_margin_included(self):
        """Pixels outside protected margin should be included if other criteria met."""
        mask = Image.new("L", (120, 120), 0)
        margin = _despeckle_protected_edge_margin(120, 120)
        
        # Create candidates outside margin
        outside_pos = (margin + 5, margin + 5)
        mask.putpixel(outside_pos, 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Outside margin pixel should be included
        self.assertIn(outside_pos, candidates)


class TestDespeckleFallbackDirectFunction(unittest.TestCase):
    """Test _despeckle_candidate_points_from_dark_points directly for precise control."""
    
    def test_direct_function_single_pixel(self):
        """Direct function call with single isolated pixel."""
        dark_points = {(50, 50)}
        
        candidates = _despeckle_candidate_points_from_dark_points(
            dark_points,
            width=100,
            height=100,
            left=0,
            top=0,
        )
        
        self.assertEqual(len(candidates), 1)
        self.assertIn((50, 50), candidates)
    
    def test_direct_function_with_offset(self):
        """Direct function call respects left/top offset."""
        dark_points = {(10, 10)}
        
        candidates = _despeckle_candidate_points_from_dark_points(
            dark_points,
            width=100,
            height=100,
            left=20,  # offset
            top=30,   # offset
        )
        
        self.assertEqual(len(candidates), 1)
        # Point should be offset by left/top
        self.assertIn((30, 40), candidates)
    
    def test_direct_function_edge_protection(self):
        """Direct function applies edge protection."""
        mask = Image.new("L", (100, 100), 0)
        margin = _despeckle_protected_edge_margin(100, 100)
        
        # Create pixel inside margin
        dark_points = {(margin - 1, 50)}
        
        candidates = _despeckle_candidate_points_from_dark_points(
            dark_points,
            width=100,
            height=100,
            left=0,
            top=0,
        )
        
        # Margin pixel should be excluded
        self.assertEqual(len(candidates), 0)


class TestDespeckleFallbackDenseContent(unittest.TestCase):
    """Test fallback behavior for dense connected content scenarios."""
    
    def test_below_dense_prefilter_threshold(self):
        """Dark pixel count below dense prefilter minimum uses normal processing."""
        mask = Image.new("L", (100, 100), 0)
        # Add few isolated pixels (below _DESPECKLE_DENSE_PREFILTER_MIN_DARK_PIXELS = 512)
        for i in range(10):
            mask.putpixel((20 + i * 5, 50), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Should use normal processing, all isolated pixels should be candidates
        self.assertEqual(len(candidates), 10)
    
    def test_sparse_scattered_pixels(self):
        """Many scattered pixels below connectivity threshold should be candidates."""
        mask = Image.new("L", (200, 200), 0)
        # Create scattered pattern but below dense threshold
        positions = []
        for x in range(20, 180, 15):
            for y in range(20, 180, 15):
                mask.putpixel((x, y), 255)
                positions.append((x, y))
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # All scattered pixels should be candidates
        self.assertEqual(len(candidates), len(positions))
    
    def test_empty_mask_returns_empty(self):
        """Empty mask should return empty candidate list."""
        mask = Image.new("L", (100, 100), 0)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        self.assertEqual(len(candidates), 0)
    
    def test_tiny_image_below_minimum_size(self):
        """Image below minimum size (3x3) should return empty candidates."""
        mask = Image.new("L", (2, 2), 0)
        mask.putpixel((1, 1), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Tiny image should return empty
        self.assertEqual(len(candidates), 0)


class TestDespeckleBackendFallback(unittest.TestCase):
    """Test backend fallback behavior."""
    
    def test_fallback_backend_explicit(self):
        """Explicit fallback backend should use fallback mode."""
        mask = Image.new("L", (100, 100), 0)
        mask.putpixel((50, 50), 255)
        
        candidates, mode = _despeckle_candidate_points_with_backend(mask, backend="fallback")
        
        self.assertEqual(mode, "fallback")
        self.assertEqual(len(candidates), 1)
        self.assertIn((50, 50), candidates)
    
    def test_invalid_backend_rejected(self):
        """Invalid backend should raise ValueError."""
        mask = Image.new("L", (100, 100), 0)
        
        with self.assertRaises(ValueError):
            _despeckle_candidate_points_with_backend(mask, backend="invalid")


class TestDespeckleFallbackDeterministic(unittest.TestCase):
    """Test that fallback behavior is deterministic and repeatable."""
    
    def test_same_input_same_output(self):
        """Same input should produce identical output across multiple calls."""
        mask = Image.new("L", (100, 100), 0)
        positions = [(20, 30), (45, 50), (60, 70), (25, 35)]
        for x, y in positions:
            mask.putpixel((x, y), 255)
        
        candidates_1 = _despeckle_candidate_points_fallback(mask)
        candidates_2 = _despeckle_candidate_points_fallback(mask)
        
        self.assertEqual(candidates_1, candidates_2)
    
    def test_candidate_sorting_consistency(self):
        """Candidates should be sorted consistently."""
        mask = Image.new("L", (100, 100), 0)
        # Add pixels in non-sorted order
        positions = [(60, 70), (20, 30), (45, 50)]
        for x, y in positions:
            mask.putpixel((x, y), 255)
        
        candidates = _despeckle_candidate_points_fallback(mask)
        
        # Should be sorted by (y, x)
        self.assertEqual(candidates, sorted(candidates, key=lambda p: (p[1], p[0])))


if __name__ == "__main__":
    unittest.main()
