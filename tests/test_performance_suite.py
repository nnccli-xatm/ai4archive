"""Performance benchmark test suite for archive_scan_qc processing operations."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.processing import ProcessingOptions, process_images


# ---------------------------------------------------------------------------
# Image generators
# ---------------------------------------------------------------------------

def _text_page_800x1100() -> Image.Image:
    """Text-like page at typical scan resolution (800x1100)."""
    size = (800, 1100)
    image = Image.new("RGB", size, (245, 242, 235))
    draw = ImageDraw.Draw(image)
    # Page border
    draw.rectangle((30, 40, size[0] - 30, size[1] - 40), outline=(80, 80, 80), width=2)
    # Simulated text lines
    for y in range(80, size[1] - 80, 18):
        draw.rectangle((60, y, size[0] - 60, y + 4), fill=(30, 30, 30))
    # Simulated paragraph breaks
    for y in range(200, size[1] - 100, 180):
        draw.rectangle((60, y, size[0] - 60, y + 18), fill=(245, 242, 235))
    # A few isolated speckle dots for despeckle testing
    for point in [(15, 20), (size[0] - 15, size[1] - 20), (100, 55), (700, 1050)]:
        image.putpixel(point, (0, 0, 0))
    # Slight skew so deskew has something to correct
    return image.rotate(-1.5, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(245, 242, 235))


def _text_page_with_dots(size: tuple[int, int]) -> Image.Image:
    """Page with isolated dots for despeckle testing at the given size."""
    image = Image.new("RGB", size, (245, 242, 235))
    draw = ImageDraw.Draw(image)
    # Page border
    margin = max(20, size[0] // 40)
    draw.rectangle(
        (margin, margin, size[0] - margin, size[1] - margin),
        outline=(80, 80, 80),
        width=2,
    )
    # Text lines
    line_spacing = max(10, size[1] // 60)
    for y in range(margin + 20, size[1] - margin - 20, line_spacing):
        draw.rectangle(
            (margin + 20, y, size[0] - margin - 20, y + 3),
            fill=(30, 30, 30),
        )
    # Isolated speckle dots scattered across the page
    step = max(40, size[0] // 20)
    for x in range(10, size[0] - 10, step):
        for y in range(10, size[1] - 10, step):
            image.putpixel((x, y), (0, 0, 0))
    return image


def _make_scan_report(filename: str = "page.png") -> dict:
    """Build a minimal scan report for process_images."""
    return {
        "schema_version": "scan-qc.scan.v1",
        "status": "finished",
        "files": [
            {
                "relative_path": filename,
                "openable": True,
                "scan_findings": [],
            }
        ],
    }


def _build_batch_scan_report(count: int, prefix: str = "page") -> dict:
    """Build a scan report with multiple files."""
    files = []
    for i in range(count):
        files.append(
            {
                "relative_path": f"{prefix}_{i:03d}.png",
                "openable": True,
                "scan_findings": [],
            }
        )
    return {
        "schema_version": "scan-qc.scan.v1",
        "status": "finished",
        "files": files,
    }


# ---------------------------------------------------------------------------
# TestOperationPerformance
# ---------------------------------------------------------------------------

class TestOperationPerformance(unittest.TestCase):
    """Measures timing for individual processing operations at various image sizes."""

    SMALL = (800, 1100)
    MEDIUM = (2400, 3300)
    LARGE = (4800, 6600)

    def _measure_processing(
        self,
        image: Image.Image,
        options: ProcessingOptions,
        timeout_seconds: float,
    ) -> tuple[float, dict]:
        """Run process_images in a temp directory and return (elapsed, result)."""
        with tempfile.TemporaryDirectory() as d:
            input_dir = Path(d) / "in"
            output_dir = Path(d) / "out"
            input_dir.mkdir()
            image.save(input_dir / "page.png")
            scan_report = _make_scan_report("page.png")

            start = time.monotonic()
            result = process_images(scan_report, input_dir, output_dir, options=options)
            elapsed = time.monotonic() - start

        return elapsed, result

    def test_deskew_performance_small(self) -> None:
        image = _text_page_800x1100()
        options = ProcessingOptions(deskew=True, workers=1)
        elapsed, result = self._measure_processing(image, options, 2.0)
        print(f"deskew SMALL  (800x1100):  {elapsed:.3f}s")
        self.assertLess(elapsed, 2.0, f"deskew small took {elapsed:.3f}s, expected < 2.0s")
        self.assertEqual(result["files"][0]["status"], "processed")

    def test_deskew_performance_medium(self) -> None:
        image = _text_page_with_dots(self.MEDIUM)
        options = ProcessingOptions(deskew=True, workers=1)
        elapsed, result = self._measure_processing(image, options, 10.0)
        print(f"deskew MEDIUM (2400x3300): {elapsed:.3f}s")
        self.assertLess(elapsed, 10.0, f"deskew medium took {elapsed:.3f}s, expected < 10.0s")
        self.assertEqual(result["files"][0]["status"], "processed")

    def test_auto_crop_performance_small(self) -> None:
        image = _text_page_800x1100()
        options = ProcessingOptions(auto_crop=True, workers=1)
        elapsed, result = self._measure_processing(image, options, 1.0)
        print(f"auto_crop SMALL (800x1100): {elapsed:.3f}s")
        self.assertLess(elapsed, 1.0, f"auto_crop small took {elapsed:.3f}s, expected < 1.0s")
        self.assertEqual(result["files"][0]["status"], "processed")

    def test_despeckle_performance_small(self) -> None:
        image = _text_page_with_dots(self.SMALL)
        options = ProcessingOptions(despeckle=True, despeckle_content_type_check=False, workers=1)
        elapsed, result = self._measure_processing(image, options, 3.0)
        print(f"despeckle SMALL (800x1100): {elapsed:.3f}s")
        self.assertLess(elapsed, 3.0, f"despeckle small took {elapsed:.3f}s, expected < 3.0s")
        self.assertEqual(result["files"][0]["status"], "processed")

    def test_full_pipeline_standard_small(self) -> None:
        image = _text_page_800x1100()
        options = ProcessingOptions(
            deskew=True,
            trim_dark_border=True,
            auto_crop=True,
            despeckle=True,
            despeckle_content_type_check=False,
            workers=1,
        )
        elapsed, result = self._measure_processing(image, options, 15.0)
        print(f"full pipeline SMALL (800x1100): {elapsed:.3f}s")
        self.assertLess(elapsed, 15.0, f"full pipeline small took {elapsed:.3f}s, expected < 15.0s")
        self.assertEqual(result["files"][0]["status"], "processed")


# ---------------------------------------------------------------------------
# TestBackendPerformance
# ---------------------------------------------------------------------------

class TestBackendPerformance(unittest.TestCase):
    """Compares despeckle backend timing on identical images."""

    BACKENDS = ("fallback", "numpy", "opencv")

    def test_despeckle_backend_timing_comparison(self) -> None:
        """Run each despeckle backend on a MEDIUM image and log timings."""
        image = _text_page_with_dots((2400, 3300))
        timings: dict[str, float] = {}

        for backend in self.BACKENDS:
            options = ProcessingOptions(
                despeckle=True,
                despeckle_content_type_check=False,
                despeckle_backend=backend,
                workers=1,
            )
            with tempfile.TemporaryDirectory() as d:
                input_dir = Path(d) / "in"
                output_dir = Path(d) / "out"
                input_dir.mkdir()
                image.save(input_dir / "page.png")
                scan_report = _make_scan_report("page.png")

                start = time.monotonic()
                result = process_images(scan_report, input_dir, output_dir, options=options)
                elapsed = time.monotonic() - start

            self.assertEqual(result["files"][0]["status"], "processed", f"backend={backend} failed")
            timings[backend] = elapsed
            print(f"despeckle backend={backend:10s}: {elapsed:.3f}s")

        # All backends must complete -- no strict ordering asserted (CI varies).
        self.assertEqual(len(timings), len(self.BACKENDS))
        for backend in self.BACKENDS:
            self.assertIn(backend, timings)
            self.assertGreater(timings[backend], 0.0)


# ---------------------------------------------------------------------------
# TestWorkerScaling
# ---------------------------------------------------------------------------

class TestWorkerScaling(unittest.TestCase):
    """Tests worker count scaling for batch throughput."""

    @staticmethod
    def _create_batch_images(input_dir: Path, count: int = 5, size: tuple[int, int] = (400, 550)) -> None:
        """Create synthetic page images in *input_dir*."""
        for i in range(count):
            img = Image.new("RGB", size, "white")
            draw = ImageDraw.Draw(img)
            draw.rectangle((20, 20, size[0] - 20, size[1] - 20), outline=(30, 30, 30), width=1)
            for y in range(40, size[1] - 40, 20):
                draw.rectangle((40, y, size[0] - 40, y + 3), fill=(20, 20, 20))
            img.save(input_dir / f"page_{i:03d}.png")

    def test_worker_throughput_comparison(self) -> None:
        """Compare batch processing with workers=1 vs workers=2."""
        image_count = 5
        timings: dict[int, float] = {}

        for workers in (1, 2):
            with tempfile.TemporaryDirectory() as d:
                input_dir = Path(d) / "in"
                output_dir = Path(d) / "out"
                input_dir.mkdir()
                self._create_batch_images(input_dir, count=image_count)
                scan_report = _build_batch_scan_report(image_count)
                options = ProcessingOptions(
                    deskew=True,
                    auto_crop=True,
                    despeckle=True,
                    despeckle_content_type_check=False,
                    workers=workers,
                )

                start = time.monotonic()
                result = process_images(scan_report, input_dir, output_dir, options=options)
                elapsed = time.monotonic() - start

            self.assertEqual(len(result["files"]), image_count)
            for record in result["files"]:
                self.assertEqual(record["status"], "processed")
            timings[workers] = elapsed
            print(f"workers={workers}: {elapsed:.3f}s for {image_count} images")

        speedup = timings[1] / max(timings[2], 1e-9)
        print(f"speedup ratio (workers=2 vs 1): {speedup:.2f}x")
        # Do NOT assert speedup > 1 -- CI may have a single core.


if __name__ == "__main__":
    unittest.main()
