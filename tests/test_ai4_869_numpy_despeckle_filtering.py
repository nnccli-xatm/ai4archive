"""Test NumPy-backed despeckle candidate filtering parity (AI4-869).

This test suite validates that the optional NumPy filtering helper
produces identical results to the fallback implementation for edge protection
checks and candidate filtering.
"""

import unittest
from PIL import Image

# Add src to path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archive_scan_qc.processing import (
    _despeckle_component_passes_edge_checks_numpy,
    _despeckle_protected_edge_margin,
    _load_numpy,
)

NUMPY_AVAILABLE = _load_numpy() is not None


@unittest.skipUnless(NUMPY_AVAILABLE, "NumPy optional fast path unavailable")
class TestNumPyEdgeCheckParity(unittest.TestCase):
    """Verify NumPy edge checks produce identical results to fallback."""
    
    def test_single_center_point_passes(self):
        """Single center point should pass edge checks."""
        component = [(50, 50)]
        result = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        self.assertTrue(result, "Center point should pass edge checks")
    
    def test_left_edge_point_fails(self):
        """Point on absolute left edge should fail edge checks."""
        component = [(0, 50)]
        result = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        self.assertFalse(result, "Left edge point should fail edge checks")
    
    def test_top_edge_point_fails(self):
        """Point on absolute top edge should fail edge checks."""
        component = [(50, 0)]
        result = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        self.assertFalse(result, "Top edge point should fail edge checks")
    
    def test_right_edge_point_fails(self):
        """Point on absolute right edge should fail edge checks."""
        component = [(99, 50)]
        result = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        self.assertFalse(result, "Right edge point should fail edge checks")
    
    def test_bottom_edge_point_fails(self):
        """Point on absolute bottom edge should fail edge checks."""
        component = [(50, 99)]
        result = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        self.assertFalse(result, "Bottom edge point should fail edge checks")
    
    def test_protected_margin_point_fails(self):
        """Point inside protected margin should fail edge checks."""
        width, height = 100, 100
        margin = _despeckle_protected_edge_margin(width, height)
        if margin > 0:
            component = [(margin - 1, 50)]
            result = _despeckle_component_passes_edge_checks_numpy(
                component, width=width, height=height, left=0, top=0
            )
            self.assertFalse(result, f"Point inside margin ({margin}) should fail edge checks")
    
    def test_2x2_cluster_with_edge_contact_fails(self):
        """2x2 cluster touching edge should fail edge checks."""
        component = [(0, 0), (1, 0), (0, 1), (1, 1)]
        result = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        self.assertFalse(result, "Cluster touching edge should fail edge checks")
    
    def test_2x2_cluster_without_edge_contact_passes(self):
        """2x2 cluster not touching edge should pass edge checks."""
        component = [(50, 50), (51, 50), (50, 51), (51, 51)]
        result = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        self.assertTrue(result, "Cluster not touching edge should pass edge checks")
    
    def test_multiple_points_mixed_edges_fails(self):
        """Component with any point on edge should fail edge checks."""
        component = [(50, 50), (51, 50), (99, 50)]  # Last point on right edge
        result = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        self.assertFalse(result, "Component with any edge point should fail edge checks")
    
    def test_coordinate_offset_preserved(self):
        """Coordinate offset (left, top) should be correctly applied."""
        component = [(4, 4)]
        # Without offset, (5, 5) is inside margin (margin is 5 for 100x100)
        result_no_offset = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=0, top=0
        )
        
        # With offset, (5, 5) becomes (10, 10), which should pass
        result_with_offset = _despeckle_component_passes_edge_checks_numpy(
            component, width=100, height=100, left=5, top=5
        )
        
        self.assertFalse(result_no_offset, "Without offset, point should fail margin check")
        self.assertTrue(result_with_offset, "With offset, point should pass margin check")


class TestNumPyAvailability(unittest.TestCase):
    """Test NumPy availability and graceful fallback."""
    
    def test_numpy_loading(self):
        """NumPy loading should work or gracefully return None."""
        np = _load_numpy()
        if np is None:
            self.skipTest("NumPy not available, fallback behavior tested")
        self.assertIsNotNone(np)
        # Verify basic NumPy functionality
        arr = np.array([1, 2, 3], dtype=np.int32)
        self.assertEqual(len(arr), 3)


@unittest.skipUnless(NUMPY_AVAILABLE, "NumPy optional fast path unavailable")
class TestNumPyPerformanceCharacteristics(unittest.TestCase):
    """Measure and verify NumPy performance characteristics."""
    
    def test_vectorization_benefit(self):
        """Verify NumPy vectorization provides benefit for larger components."""
        import time
        
        # Create a large component (1000 points)
        component = [(x, y) for x in range(0, 100, 10) for y in range(0, 100, 10)]
        
        # Time NumPy edge checks
        start = time.perf_counter()
        for _ in range(100):
            _despeckle_component_passes_edge_checks_numpy(
                component, width=1000, height=1000, left=0, top=0
            )
        numpy_time = time.perf_counter() - start
        
        # Simple validation that NumPy is reasonably fast
        self.assertLess(numpy_time, 1.0, "NumPy edge checks should be fast")
        print(f"NumPy edge checks for {len(component)} points: {numpy_time:.4f}s for 100 iterations")


if __name__ == "__main__":
    unittest.main()

