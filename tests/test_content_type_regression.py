"""Regression tests ensuring specific archive content types survive processing.

Each test synthesises an image that mimics a real-world artefact (halftone
dots, ditto marks, stamp seals, etc.) and verifies that the standard
processing pipeline preserves the content-defining features.
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from archive_scan_qc.processing import ProcessingOptions, process_images


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run_standard_processing(image, extra_options=None):
    """Run standard mode processing on a single image, return (source, processed, record)."""
    with tempfile.TemporaryDirectory() as d:
        input_dir = Path(d) / "in"
        output_dir = Path(d) / "out"
        input_dir.mkdir()
        image.save(input_dir / "page.png")
        scan_report = {
            "schema_version": "scan-qc.scan.v1",
            "status": "finished",
            "files": [{"relative_path": "page.png", "openable": True, "scan_findings": []}],
        }
        opts = {"auto_crop": True, "deskew": True, "trim_dark_border": True, "despeckle": True}
        if extra_options:
            opts.update(extra_options)
        result = process_images(scan_report, input_dir, output_dir, options=ProcessingOptions(**opts))
        processed_path = output_dir / "images" / "page.png"
        processed = Image.open(processed_path).copy() if processed_path.exists() else None
        source = image
        record = result["files"][0] if "files" in result else {}
        return source, processed, record


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestContentTypeRegression(unittest.TestCase):
    """Content-type preservation regression tests for the processing pipeline."""

    # 1. Halftone stipple --------------------------------------------------

    def test_halftone_stipple_preserved(self):
        """Dot pattern simulating halftone print must survive processing."""
        img = Image.new("RGB", (200, 200), "white")
        draw = ImageDraw.Draw(img)
        # Place small dots in a regular grid (halftone pattern)
        for cy in range(20, 180, 12):
            for cx in range(20, 180, 12):
                draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill="black")

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Count dark pixels in the dot-pattern region before and after
        src_pixels = source.load()
        dst_pixels = processed.load()
        src_dark = 0
        dst_dark = 0
        for y in range(20, 180):
            for x in range(20, 180):
                if src_pixels[x, y][0] < 80:
                    src_dark += 1
                px, py = min(x, processed.width - 1), min(y, processed.height - 1)
                if dst_pixels[px, py][0] < 80:
                    dst_dark += 1
        self.assertGreater(src_dark, 0, "Source must contain dark dot pixels")
        keep_ratio = dst_dark / max(1, src_dark)
        self.assertGreater(keep_ratio, 0.40, "Halftone dots should be largely preserved")

    # 2. JPEG block noise ---------------------------------------------------

    def test_jpeg_block_noise_conserved(self):
        """8x8 block structure from JPEG compression should not be fully smoothed."""
        img = Image.new("RGB", (200, 200), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        # Draw 8x8 blocks with slightly different gray values
        for by in range(0, 200, 8):
            for bx in range(0, 200, 8):
                offset = ((bx * 3 + by * 7) % 30) - 15
                gray = max(0, min(255, 200 + offset))
                draw.rectangle([bx, by, bx + 7, by + 7], fill=(gray, gray, gray))

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Measure that not all blocks converged to the same value
        region = processed.crop((20, 20, 180, 180)).convert("L")
        values = list(region.getdata())
        mean_val = sum(values) / len(values)
        variance = sum((v - mean_val) ** 2 for v in values) / len(values)
        self.assertGreater(variance, 5.0, "Block structure variance should be retained")

    # 3. Black wedge cleanup ------------------------------------------------

    def test_black_wedge_cleanup_safe(self):
        """A black triangular wedge in the corner must not destroy content area."""
        img = Image.new("RGB", (200, 200), "white")
        draw = ImageDraw.Draw(img)
        # Draw black triangle in top-left corner (wedge)
        draw.polygon([(0, 0), (50, 0), (0, 50)], fill="black")
        # Draw meaningful content in centre
        draw.rectangle([80, 80, 120, 120], fill="black")

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # The centre black square must survive
        region = processed.crop((70, 70, 130, 130)).convert("L")
        dark_pixels = sum(1 for v in region.getdata() if v < 80)
        self.assertGreater(dark_pixels, 50, "Centre content should remain after wedge cleanup")

    # 4. Ledger ditto marks -------------------------------------------------

    def test_ledger_ditto_repeat_preserved(self):
        """Ditto marks must not be removed by despeckle."""
        img = Image.new("RGB", (200, 100), "white")
        draw = ImageDraw.Draw(img)
        # Draw two short horizontal strokes (ditto marks)
        draw.line([(70, 40), (80, 40)], fill="black", width=2)
        draw.line([(85, 40), (95, 40)], fill="black", width=2)
        # Draw a text-like block nearby
        draw.rectangle([70, 50, 95, 70], fill="black")

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Check the ditto-mark region still has dark pixels
        w, h = processed.size
        ditto_region = processed.crop((60, 30, min(100, w), 50)).convert("L")
        dark_pixels = sum(1 for v in ditto_region.getdata() if v < 100)
        self.assertGreater(dark_pixels, 2, "Ditto marks should survive despeckle")

    # 5. Fold shadow cleanup ------------------------------------------------

    def test_fold_shadow_cleanup_safe(self):
        """Text above and below a fold-shadow band must be preserved."""
        img = Image.new("RGB", (200, 200), "white")
        draw = ImageDraw.Draw(img)
        # Horizontal gray band across the middle (fold shadow)
        draw.rectangle([0, 90, 200, 110], fill=(160, 160, 160))
        # Text block above fold
        draw.rectangle([80, 40, 120, 80], fill="black")
        # Text block below fold
        draw.rectangle([80, 120, 120, 160], fill="black")

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Check text above fold
        w, h = processed.size
        above = processed.crop((70, 30, min(130, w), 90)).convert("L")
        above_dark = sum(1 for v in above.getdata() if v < 80)
        self.assertGreater(above_dark, 20, "Text above fold should be preserved")

        below = processed.crop((70, 110, min(130, w), min(170, h))).convert("L")
        below_dark = sum(1 for v in below.getdata() if v < 80)
        self.assertGreater(below_dark, 20, "Text below fold should be preserved")

    # 6. Photo content despeckle skip ----------------------------------------

    def test_photo_content_despeckle_skip(self):
        """Gradient (photo-like) regions should not be significantly altered by despeckle."""
        img = Image.new("RGB", (200, 200), "white")
        pixels = img.load()
        # Create a smooth gradient region simulating a photo
        for y in range(50, 150):
            for x in range(50, 150):
                v = int(50 + (x - 50) * 1.5)
                pixels[x, y] = (v, v, v)

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Compare mean brightness of the gradient region before and after
        src_region = source.crop((50, 50, 150, 150)).convert("L")
        dst_region = processed.crop(
            (50, 50, min(150, processed.width), min(150, processed.height))
        ).convert("L")
        src_mean = sum(src_region.getdata()) / (src_region.width * src_region.height)
        dst_mean = sum(dst_region.getdata()) / (dst_region.width * dst_region.height)
        delta = abs(dst_mean - src_mean)
        self.assertLess(delta, 40, "Photo-like gradient should not be drastically altered")

    # 7. Faint text not removed ----------------------------------------------

    def test_faint_text_not_removed(self):
        """Very light gray text must not be cleaned up as noise."""
        img = Image.new("RGB", (200, 100), (250, 250, 250))
        draw = ImageDraw.Draw(img)
        # Draw faint text rectangles (value 210 on 250 background)
        for x_start in range(50, 150, 10):
            draw.rectangle([x_start, 40, x_start + 6, 60], fill=(210, 210, 210))

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Faint text region should still have pixels darker than the background
        w, h = processed.size
        region = processed.crop((40, 35, min(160, w), min(65, h))).convert("L")
        bg_val = 250
        faint_pixels = sum(1 for v in region.getdata() if v < bg_val - 10)
        self.assertGreater(faint_pixels, 10, "Faint text pixels should survive processing")

    # 8. Ruled lines preserved -----------------------------------------------

    def test_ruled_lines_preserved(self):
        """Horizontal and vertical table-grid lines must survive processing."""
        img = Image.new("RGB", (200, 200), "white")
        draw = ImageDraw.Draw(img)
        # Horizontal ruled lines
        for y in range(40, 180, 30):
            draw.line([(20, y), (180, y)], fill="black", width=1)
        # Vertical ruled lines
        for x in range(40, 180, 30):
            draw.line([(x, 20), (x, 180)], fill="black", width=1)

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Count dark pixels in the grid region
        w, h = processed.size
        region = processed.crop((20, 20, min(180, w), min(180, h))).convert("L")
        dark_pixels = sum(1 for v in region.getdata() if v < 100)
        self.assertGreater(dark_pixels, 30, "Grid lines should be preserved")

    # 9. Stamp seal color preserved -------------------------------------------

    def test_stamp_seal_color_preserved(self):
        """Red stamp (ellipse) color should be approximately preserved."""
        img = Image.new("RGB", (200, 200), "white")
        draw = ImageDraw.Draw(img)
        # Red ellipse simulating a stamp seal
        draw.ellipse([60, 60, 140, 120], fill=(200, 30, 30))

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Find red pixels in the stamp region of the processed image
        w, h = processed.size
        region = processed.crop((50, 50, min(150, w), min(130, h)))
        px = region.load()
        red_count = 0
        total_r = 0
        total_g = 0
        total_b = 0
        for y in range(region.height):
            for x in range(region.width):
                r, g, b = px[x, y][:3]
                if r > 150 and g < 100 and b < 100:
                    red_count += 1
                    total_r += r
                    total_g += g
                    total_b += b
        self.assertGreater(red_count, 100, "Red stamp pixels should be preserved")
        avg_r = total_r / max(1, red_count)
        avg_g = total_g / max(1, red_count)
        self.assertGreater(avg_r, 150, "Red channel should remain dominant")
        self.assertLess(avg_g, 100, "Green channel should remain low for stamp")

    # 10. Carbon copy text preserved ------------------------------------------

    def test_carbon_copy_text_preserved(self):
        """Blue carbon-copy text should survive processing."""
        img = Image.new("RGB", (200, 100), "white")
        draw = ImageDraw.Draw(img)
        # Blue text rectangles simulating carbon copy
        for x_start in range(40, 160, 12):
            draw.rectangle([x_start, 35, x_start + 8, 65], fill=(30, 30, 180))

        source, processed, _ = _run_standard_processing(img)
        self.assertIsNotNone(processed, "Processed image must exist")

        # Check blue pixels are still present
        w, h = processed.size
        region = processed.crop((30, 30, min(170, w), min(70, h)))
        px = region.load()
        blue_count = 0
        for y in range(region.height):
            for x in range(region.width):
                r, g, b = px[x, y][:3]
                if b > 120 and r < 100 and g < 100:
                    blue_count += 1
        self.assertGreater(blue_count, 50, "Blue carbon-copy text should be preserved")


if __name__ == "__main__":
    unittest.main()
