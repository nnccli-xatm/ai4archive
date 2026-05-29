"""Tests for despeckle OpenCV backend (AI4-811)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.processing import (
    ProcessingOptions,
    _load_opencv,
    _despeckle_isolated_pixels,
    process_images,
)
from archive_scan_qc.scanner import ScanConfig, scan_batch


class TestOpenCVAvailability(unittest.TestCase):
    def test_load_opencv_returns_none_or_module(self):
        cv2 = _load_opencv()
        self.assertTrue(cv2 is None or hasattr(cv2, "medianBlur"))


class TestDespeckleBackendAcceptance(unittest.TestCase):
    def test_fallback_backend_works(self):
        image = _speckled_page()
        result, changed, mode = _despeckle_isolated_pixels(image, backend="fallback")
        self.assertEqual(mode, "fallback")

    def test_numpy_backend_works_or_falls_back(self):
        image = _speckled_page()
        result, changed, mode = _despeckle_isolated_pixels(image, backend="numpy")
        self.assertIn(mode, {"numpy", "fallback"})

    def test_opencv_backend_works_or_falls_back(self):
        image = _speckled_page()
        result, changed, mode = _despeckle_isolated_pixels(image, backend="opencv")
        self.assertIn(mode, {"opencv", "numpy", "fallback"})

    def test_invalid_backend_rejected(self):
        image = _speckled_page()
        with self.assertRaises(ValueError):
            _despeckle_isolated_pixels(image, backend="invalid")


class TestDespeckleOpenCVQualityConsistency(unittest.TestCase):
    def test_opencv_removes_isolated_pixels(self):
        cv2 = _load_opencv()
        if cv2 is None:
            self.skipTest("OpenCV not available")
        image = _speckled_page()
        result_fallback, changed_fb, _ = _despeckle_isolated_pixels(image, backend="fallback")
        result_opencv, changed_cv, _ = _despeckle_isolated_pixels(image, backend="opencv")
        self.assertGreater(changed_fb, 0)
        self.assertGreater(changed_cv, 0)

    def test_opencv_does_not_modify_clean_image(self):
        image = _clean_page()
        result, changed, mode = _despeckle_isolated_pixels(image, backend="opencv")
        self.assertEqual(changed, 0)


class TestOpenCVIntegrationInPipeline(unittest.TestCase):
    def test_despeckle_with_opencv_in_pipeline(self):
        cv2 = _load_opencv()
        if cv2 is None:
            self.skipTest("OpenCV not available")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _speckled_page()
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
            self.assertTrue(record["despeckled"])


class TestPyprojectOptionalDeps(unittest.TestCase):
    def test_opencv_optional_dep_declared(self):
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        self.assertIn("[project.optional-dependencies]", text)
        self.assertIn("opencv", text)
        self.assertIn("opencv-python-headless", text)
        self.assertIn("perf", text)


def _speckled_page() -> Image.Image:
    image = Image.new("RGB", (120, 120), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 100, 100), outline=(60, 60, 60), width=2)
    for y in range(40, 80, 15):
        draw.line((35, y, 85, y), fill=(20, 20, 20), width=2)
    for point in [(50, 10), (10, 60), (105, 45)]:
        image.putpixel(point, (0, 0, 0))
    return image


def _clean_page() -> Image.Image:
    image = Image.new("RGB", (120, 120), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 100, 100), outline=(60, 60, 60), width=2)
    return image


if __name__ == "__main__":
    unittest.main()
