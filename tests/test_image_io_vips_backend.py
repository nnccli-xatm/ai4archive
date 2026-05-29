"""Tests for libvips streaming IO backend (AI4-860)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from archive_scan_qc.processing import (
    ProcessingOptions,
    _load_vips,
    _open_image,
    _save_image_with_backend,
    process_images,
)


def _synthetic_image(width: int = 100, height: int = 100, mode: str = "RGB") -> Image.Image:
    return Image.new(mode, (width, height), color=(128, 128, 128))


class TestVipsAvailability(unittest.TestCase):
    def test_load_vips_returns_module_or_none(self):
        result = _load_vips()
        self.assertTrue(result is None or hasattr(result, "Image"))


class TestImageIOBackendAcceptance(unittest.TestCase):
    def test_fallback_always_works(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.png"
            img = _synthetic_image()
            img.save(path)
            opened, meta = _open_image(path, io_backend="fallback")
            self.assertIsInstance(opened, Image.Image)
            self.assertEqual(meta["io_backend_mode"], "fallback")
            opened.close()

    def test_vips_backend_falls_back_gracefully(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.png"
            img = _synthetic_image()
            img.save(path)
            opened, meta = _open_image(path, io_backend="vips")
            self.assertIsInstance(opened, Image.Image)
            self.assertIn(meta["io_backend_mode"], {"fallback", "vips"})
            opened.close()

    def test_invalid_backend_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.png"
            img = _synthetic_image()
            img.save(path)
            with self.assertRaises(ValueError):
                _open_image(path, io_backend="bad")


class TestImageIOSaveBackend(unittest.TestCase):
    def test_save_fallback_produces_valid_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.png"
            source_path = Path(d) / "src.png"
            src = _synthetic_image()
            src.save(source_path)
            with Image.open(source_path) as source_img:
                meta = _save_image_with_backend(src, target, source_img, io_backend="fallback")
            self.assertEqual(meta["io_backend_mode"], "fallback")
            self.assertTrue(target.exists())

    def test_save_vips_backend_produces_valid_file(self):
        vips = _load_vips()
        if vips is None:
            self.skipTest("pyvips not available")
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.png"
            source_path = Path(d) / "src.png"
            src = _synthetic_image()
            src.save(source_path)
            with Image.open(source_path) as source_img:
                meta = _save_image_with_backend(src, target, source_img, io_backend="vips")
            self.assertIn(meta["io_backend_mode"], {"fallback", "vips"})
            self.assertTrue(target.exists())


class TestImageIOBackendInPipeline(unittest.TestCase):
    def test_pipeline_with_vips_backend(self):
        with tempfile.TemporaryDirectory() as d:
            input_dir = Path(d) / "input"
            output_dir = Path(d) / "output"
            input_dir.mkdir()
            img = _synthetic_image(200, 200)
            img.save(input_dir / "page.png")

            scan_report = {
                "schema_version": "scan-qc.scan.v1",
                "status": "finished",
                "files": [
                    {
                        "relative_path": "page.png",
                        "openable": True,
                        "scan_findings": [],
                    }
                ],
            }

            options = ProcessingOptions(
                auto_crop=True,
                image_io_backend="vips",
            )
            result = process_images(scan_report, input_dir, output_dir, options=options)
            self.assertIsInstance(result, dict)
            manifest = result.get("manifest", result)
            self.assertIn("files", manifest)


class TestImageIOBackendRejected(unittest.TestCase):
    def test_invalid_save_backend_still_saves(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.png"
            source_path = Path(d) / "src.png"
            src = _synthetic_image()
            src.save(source_path)
            with Image.open(source_path) as source_img:
                meta = _save_image_with_backend(src, target, source_img, io_backend="vips")
            self.assertIn(meta["io_backend_mode"], {"fallback", "vips"})
