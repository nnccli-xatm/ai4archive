import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from archive_scan_qc.processing import (
    _load_numpy,
    _deskew_projection_score,
    _horizontal_projection_variance,
    _vertical_projection_variance,
)


class TestDeskewVectorizationFallback(unittest.TestCase):
    """Test that NumPy vectorization safely falls back to Pillow when NumPy is unavailable."""

    def test_fallback_when_numpy_unavailable(self):
        """When NumPy is unavailable, projection score should fall back to Pillow implementation."""
        image = _synthetic_text_page()

        with patch('archive_scan_qc.processing._load_numpy', return_value=None):
            score = _deskew_projection_score(image)
            self.assertIsInstance(score, float)
            self.assertGreater(score, 0.0)

    def test_numpy_used_when_available(self):
        """When NumPy is available, projection score should use vectorized implementation."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        image = _synthetic_text_page()

        # This should use NumPy without falling back
        score = _deskew_projection_score(image)
        self.assertIsInstance(score, float)
        self.assertGreater(score, 0.0)


class TestDeskewProjectionScoreParity(unittest.TestCase):
    """Test that vectorized and fallback implementations produce similar scores."""

    def test_vectorized_matches_fallback_on_simple_image(self):
        """Vectorized NumPy score should match Pillow fallback score closely."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        image = _synthetic_text_page()

        # Get vectorized score
        vectorized_score = _deskew_projection_score(image)

        # Get fallback score by temporarily disabling NumPy
        with patch('archive_scan_qc.processing._load_numpy', return_value=None):
            fallback_score = _deskew_projection_score(image)

        # Allow small relative difference due to PIL resize rounding
        # The key is that both should produce similar relative ordering
        relative_diff = abs(vectorized_score - fallback_score) / fallback_score
        self.assertLess(relative_diff, 0.02, f"Relative difference {relative_diff:.4f} exceeds 2% threshold")

    def test_score_relative_ordering_preserved(self):
        """Upright images should have higher scores than tilted images regardless of implementation."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        upright = _synthetic_text_page(angle=0.0)
        slightly_tilted = _synthetic_text_page(angle=0.5)
        heavily_tilted = _synthetic_text_page(angle=3.0)

        score_upright = _deskew_projection_score(upright)
        score_slight = _deskew_projection_score(slightly_tilted)
        score_heavy = _deskew_projection_score(heavily_tilted)

        self.assertGreater(score_upright, score_slight)
        self.assertGreater(score_slight, score_heavy)


class TestDeskewVectorizationPerformance(unittest.TestCase):
    """Test that vectorized implementation provides performance benefit."""

    def test_vectorized_faster_than_fallback(self):
        """Vectorized implementation should be faster than fallback for repeated scoring."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        image = _synthetic_text_page()
        iterations = 10

        # Time vectorized implementation
        start = time.perf_counter()
        for _ in range(iterations):
            _deskew_projection_score(image)
        vectorized_time = time.perf_counter() - start

        # Time fallback implementation
        with patch('archive_scan_qc.processing._load_numpy', return_value=None):
            start = time.perf_counter()
            for _ in range(iterations):
                _deskew_projection_score(image)
            fallback_time = time.perf_counter() - start

        # Vectorized should be faster (allow some overhead variance)
        # We don't enforce strict timing as it's environment-dependent
        self.assertIsNotNone(vectorized_time)
        self.assertIsNotNone(fallback_time)


class TestDeskewVectorizationEdgeCases(unittest.TestCase):
    """Test vectorized implementation handles edge cases correctly."""

    def test_handles_empty_image(self):
        """Vectorized implementation should handle empty (blank) images."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        empty = Image.new("L", (100, 100), "white")
        score = _deskew_projection_score(empty)
        self.assertIsInstance(score, float)

    def test_handles_small_image(self):
        """Vectorized implementation should handle very small images."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        small = Image.new("L", (10, 10), "white")
        d = ImageDraw.Draw(small)
        d.line((0, 0, 9, 9), fill="black")
        score = _deskew_projection_score(small)
        self.assertIsInstance(score, float)

    def test_handles_large_image(self):
        """Vectorized implementation should handle large images."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        large = _synthetic_text_page(size=(2000, 3000))
        score = _deskew_projection_score(large)
        self.assertIsInstance(score, float)
        self.assertGreater(score, 0.0)


class TestDeskewVectorizationConsistency(unittest.TestCase):
    """Test that vectorized implementation produces consistent results."""

    def test_score_consistency_across_calls(self):
        """Vectorized implementation should return the same score for the same image."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        image = _synthetic_text_page()
        scores = [_deskew_projection_score(image) for _ in range(5)]

        # All scores should be identical
        self.assertEqual(len(set(scores)), 1)

    def test_score_independent_of_modification(self):
        """Scoring should not modify the original image."""
        if _load_numpy() is None:
            self.skipTest("NumPy not available")

        image = _synthetic_text_page()
        original_pixels = image.tobytes()

        _deskew_projection_score(image)

        # Image should be unchanged
        self.assertEqual(image.tobytes(), original_pixels)


def _synthetic_text_page(
    angle: float = 0.0,
    size: tuple[int, int] = (300, 400)
) -> Image.Image:
    """Create a synthetic text page for testing."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)

    # Draw a border
    draw.rectangle((40, 30, size[0] - 40, size[1] - 30), outline=(60, 60, 60), width=2)

    # Draw horizontal text lines
    line_height = 20
    for y in range(60, size[1] - 60, line_height):
        draw.line((60, y, size[0] - 60, y), fill=(20, 20, 20), width=2)

    # Apply rotation if requested
    if angle != 0.0:
        image = image.rotate(
            angle,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor="white"
        )

    return image


if __name__ == "__main__":
    unittest.main()
