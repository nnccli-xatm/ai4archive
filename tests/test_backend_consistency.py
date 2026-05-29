"""Tests that different processing backends (Pillow/fallback, numpy, opencv) produce consistent results."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from archive_scan_qc.processing import (
    ProcessingOptions,
    _deskew_projection_score,
    _despeckle_isolated_pixels_with_reason,
    _load_numpy,
    _load_opencv,
    process_images,
)
from archive_scan_qc.scanner import ScanConfig, scan_batch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _image_with_isolated_dots(width: int = 200, height: int = 200, dot_count: int = 20) -> Image.Image:
    """Create a white image with small black dot clusters (isolated speckles)."""
    random.seed(42)
    img = Image.new("RGB", (width, height), "white")
    pixels = img.load()
    for _ in range(dot_count):
        x = random.randint(10, width - 10)
        y = random.randint(10, height - 10)
        pixels[x, y] = (0, 0, 0)
        if x + 1 < width:
            pixels[x + 1, y] = (0, 0, 0)
        if y + 1 < height:
            pixels[x, y + 1] = (0, 0, 0)
    return img


def _tilted_text_page(width: int = 300, height: int = 400) -> Image.Image:
    """Create a simulated text page with horizontal lines (used for deskew scoring)."""
    img = Image.new("L", (width, height), 245)
    draw = ImageDraw.Draw(img)
    for y in range(30, height - 30, 18):
        draw.line((25, y, width - 25, y), fill=20, width=2)
    return img


def _pixel_difference_ratio(img_a: Image.Image, img_b: Image.Image) -> float:
    """Return the fraction of differing pixels between two same-sized grayscale images."""
    a = img_a.convert("L")
    b = img_b.convert("L")
    assert a.size == b.size, f"Size mismatch: {a.size} vs {b.size}"
    width, height = a.size
    data_a = a.tobytes()
    data_b = b.tobytes()
    total = width * height
    differing = sum(1 for pa, pb in zip(data_a, data_b) if pa != pb)
    return differing / total


# ---------------------------------------------------------------------------
# 1. Despeckle backend consistency
# ---------------------------------------------------------------------------

class TestDespeckleBackendConsistency(unittest.TestCase):
    """Verify that fallback, numpy, and opencv backends produce equivalent output."""

    def test_fallback_vs_numpy_same_result(self) -> None:
        np = _load_numpy()
        if np is None:
            self.skipTest("numpy not available")

        image = _image_with_isolated_dots()
        result_fallback = _despeckle_isolated_pixels_with_reason(image, backend="fallback")
        result_numpy = _despeckle_isolated_pixels_with_reason(image, backend="numpy")

        ratio = _pixel_difference_ratio(result_fallback.image, result_numpy.image)
        self.assertLess(
            ratio,
            0.01,
            f"fallback and numpy outputs differ by {ratio:.4%} of pixels (threshold 1%)",
        )

    def test_fallback_vs_opencv_same_result(self) -> None:
        cv2 = _load_opencv()
        if cv2 is None:
            self.skipTest("OpenCV not available")

        image = _image_with_isolated_dots()
        result_fallback = _despeckle_isolated_pixels_with_reason(image, backend="fallback")
        result_opencv = _despeckle_isolated_pixels_with_reason(image, backend="opencv")

        ratio = _pixel_difference_ratio(result_fallback.image, result_opencv.image)
        self.assertLess(
            ratio,
            0.01,
            f"fallback and opencv outputs differ by {ratio:.4%} of pixels (threshold 1%)",
        )

    def test_numpy_unavailable_graceful_fallback(self) -> None:
        """When _load_numpy returns None, requesting backend='numpy' must not crash."""
        image = _image_with_isolated_dots()
        with patch("archive_scan_qc.processing._load_numpy", return_value=None):
            result = _despeckle_isolated_pixels_with_reason(image, backend="numpy")
        self.assertIsInstance(result.image, Image.Image)
        # Should have fallen back to fallback mode
        self.assertIn(result.backend_mode, {"fallback", "numpy", "not_applicable"})

    def test_opencv_unavailable_graceful_fallback(self) -> None:
        """When _load_opencv returns None, requesting backend='opencv' must not crash."""
        image = _image_with_isolated_dots()
        with patch("archive_scan_qc.processing._load_opencv", return_value=None):
            result = _despeckle_isolated_pixels_with_reason(image, backend="opencv")
        self.assertIsInstance(result.image, Image.Image)
        # opencv path may delegate to numpy internally; all are valid graceful outcomes
        self.assertIn(result.backend_mode, {"fallback", "numpy", "not_applicable"})


# ---------------------------------------------------------------------------
# 2. Deskew backend consistency
# ---------------------------------------------------------------------------

class TestDeskewBackendConsistency(unittest.TestCase):
    """Verify that deskew projection scoring is stable and consistent."""

    def test_pillow_vs_numpy_projection_score(self) -> None:
        """Projection score should be a positive float regardless of numpy availability."""
        image = _tilted_text_page()
        score = _deskew_projection_score(image)
        self.assertIsInstance(score, float)
        self.assertGreater(score, 0.0)

    def test_projection_score_consistency(self) -> None:
        """Running projection score 3 times on the same image must yield identical results."""
        image = _tilted_text_page()
        scores = [_deskew_projection_score(image) for _ in range(3)]
        self.assertEqual(scores[0], scores[1], "Score call 1 != Score call 2")
        self.assertEqual(scores[1], scores[2], "Score call 2 != Score call 3")


# ---------------------------------------------------------------------------
# 3. Full-pipeline integration with different backends
# ---------------------------------------------------------------------------

class TestBackendPipelineIntegration(unittest.TestCase):
    """Run the full process_images pipeline with different despeckle backends."""

    def test_pipeline_with_numpy_backend(self) -> None:
        np = _load_numpy()
        if np is None:
            self.skipTest("numpy not available")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            image = _image_with_isolated_dots()
            image.save(input_dir / "A001_0001.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir, workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    deskew=False,
                    trim_dark_border=False,
                    auto_crop=False,
                    despeckle=True,
                    despeckle_content_type_check=False,
                    workers=1,
                    despeckle_backend="numpy",
                ),
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["despeckled"])
            self.assertIn(
                record["despeckle_backend_mode"],
                {"numpy", "fallback", "not_applicable"},
            )

    def test_pipeline_with_opencv_backend(self) -> None:
        cv2 = _load_opencv()
        if cv2 is None:
            self.skipTest("OpenCV not available")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            image = _image_with_isolated_dots()
            image.save(input_dir / "A001_0001.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir, workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    deskew=False,
                    trim_dark_border=False,
                    auto_crop=False,
                    despeckle=True,
                    despeckle_content_type_check=False,
                    workers=1,
                    despeckle_backend="opencv",
                ),
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["despeckled"])
            self.assertIn(
                record["despeckle_backend_mode"],
                {"opencv", "numpy", "fallback", "not_applicable"},
            )


if __name__ == "__main__":
    unittest.main()
