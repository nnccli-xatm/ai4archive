"""Tests for deskew numpy/OpenCV optimization (AI4-812)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.processing import (
    ProcessingOptions,
    _load_opencv,
    _load_numpy,
    _deskew_projection_score,
    _rotate_for_deskew,
    process_images,
)
from archive_scan_qc.scanner import ScanConfig, scan_batch


class TestProjectionScoreNumpy(unittest.TestCase):
    def test_projection_score_consistent(self):
        image = _tilted_text_page(-2.0)
        score = _deskew_projection_score(image)
        self.assertIsInstance(score, float)
        self.assertGreater(score, 0.0)

    def test_projection_score_upright_higher_than_tilted(self):
        upright = _upright_text_page()
        tilted = _tilted_text_page(-3.0)
        score_up = _deskew_projection_score(upright)
        score_tilt = _deskew_projection_score(tilted)
        self.assertGreater(score_up, score_tilt)


class TestRotateForDeskew(unittest.TestCase):
    def test_pil_rotation_works(self):
        image = _upright_text_page()
        rotated = _rotate_for_deskew(image, 2.0)
        self.assertIsInstance(rotated, Image.Image)
        self.assertNotEqual(rotated.size, image.size)

    def test_rotation_preserves_mode(self):
        image = _upright_text_page()
        rotated = _rotate_for_deskew(image, -1.5)
        self.assertEqual(rotated.mode, image.mode)

    def test_rotation_near_zero_approximates_original(self):
        image = _upright_text_page()
        rotated = _rotate_for_deskew(image, 0.1)
        self.assertAlmostEqual(rotated.size[0], image.size[0], delta=3)
        self.assertAlmostEqual(rotated.size[1], image.size[1], delta=3)


class TestDeskewOptimizedInPipeline(unittest.TestCase):
    def test_deskew_with_optimized_backends(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _tilted_text_page(-2.0)
            image.save(input_dir / "A001_0001.png", dpi=(300, 300))
            report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir, workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    deskew=True,
                    trim_dark_border=False,
                    auto_crop=False,
                    despeckle=False,
                    workers=1,
                ),
            )
            record = manifest["files"][0]
            self.assertTrue(record["deskewed"])


def _tilted_text_page(angle: float) -> Image.Image:
    image = Image.new("RGB", (300, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 30, 260, 370), outline=(60, 60, 60), width=2)
    for y in range(60, 340, 20):
        draw.line((60, y, 240, y), fill=(20, 20, 20), width=2)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")


def _upright_text_page() -> Image.Image:
    image = Image.new("RGB", (300, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 30, 260, 370), outline=(60, 60, 60), width=2)
    for y in range(60, 340, 20):
        draw.line((60, y, 240, y), fill=(20, 20, 20), width=2)
    return image


if __name__ == "__main__":
    unittest.main()
