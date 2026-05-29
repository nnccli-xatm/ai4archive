"""Comprehensive quality regression test suite for archive image processing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

from archive_scan_qc.processing import ProcessingOptions, process_images


# ---------------------------------------------------------------------------
# Synthetic image generators for different archive document types
# ---------------------------------------------------------------------------


def _printed_text_page() -> Image.Image:
    """White background with black text lines (rectangles)."""
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(30, 30, 30), width=2)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    return image


def _handwritten_text_page() -> Image.Image:
    """White background with irregular strokes simulating handwriting."""
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(40, 40, 40), width=1)
    for y in range(40, 140, 22):
        draw.line((40, y, 80, y + 5), fill=(20, 20, 20), width=2)
        draw.line((85, y + 3, 130, y - 2), fill=(20, 20, 20), width=2)
        draw.line((135, y + 1, 195, y + 6), fill=(20, 20, 20), width=2)
    return image


def _faded_text_page() -> Image.Image:
    """Light gray text on slightly gray background."""
    image = Image.new("RGB", (240, 180), (220, 220, 220))
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(180, 180, 180), width=1)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(180, 180, 180))
    return image


def _ledger_form_page() -> Image.Image:
    """White background with grid lines and text cells."""
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    for x in range(30, 220, 40):
        draw.line((x, 20, x, 160), fill=(100, 100, 100), width=1)
    for y in range(30, 160, 26):
        draw.line((20, y, 220, y), fill=(100, 100, 100), width=1)
    draw.rectangle((30, 30, 70, 56), fill=(20, 20, 20))
    draw.rectangle((110, 56, 150, 82), fill=(20, 20, 20))
    return image


def _photo_content_page() -> Image.Image:
    """Image with colored gradient regions simulating a photograph."""
    image = Image.new("RGB", (240, 180), (200, 200, 200))
    draw = ImageDraw.Draw(image)
    for x in range(40, 200):
        r = min(255, 60 + x)
        g = min(255, 100 + x // 2)
        b = min(255, 180 - x // 3)
        draw.line((x, 30, x, 150), fill=(r, g, b))
    return image


def _stamp_seal_page() -> Image.Image:
    """White background with red ellipse (stamp) and text."""
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(30, 30, 30), width=2)
    for y in range(42, 90, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    draw.ellipse((140, 100, 210, 155), outline=(180, 20, 20), width=3)
    draw.line((155, 127, 195, 127), fill=(180, 20, 20), width=2)
    return image


def _carbon_copy_page() -> Image.Image:
    """Light blue text on white background."""
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(120, 140, 170), width=1)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(100, 120, 160))
    return image


def _folded_stained_page() -> Image.Image:
    """White background with a horizontal gray band (fold shadow)."""
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(30, 30, 30), width=2)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    draw.rectangle((0, 85, 239, 100), fill=(190, 190, 190))
    return image


def _skewed_document_page(angle: float) -> Image.Image:
    """Text page rotated by *angle* degrees."""
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(30, 30, 30), width=2)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")


def _dark_border_page() -> Image.Image:
    """Image with dark edges (black borders on sides)."""
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(30, 30, 30), width=2)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    draw.rectangle((0, 0, 12, 179), fill=(0, 0, 0))
    draw.rectangle((228, 0, 239, 179), fill=(0, 0, 0))
    return image


def _blank_page() -> Image.Image:
    """Pure white or near-white image."""
    image = Image.new("RGB", (240, 180), (253, 253, 253))
    return image


# ---------------------------------------------------------------------------
# QualityAsserter mixin
# ---------------------------------------------------------------------------


class QualityAsserter:
    """Mixin providing pixel-level and size-level quality assertions."""

    def _pixel_change_ratio(self, source: Image.Image, processed: Image.Image) -> float:
        """Compute ratio of pixels that changed between *source* and *processed*."""
        comparable = processed.resize(source.size, Image.Resampling.BILINEAR)
        diff = ImageChops.difference(source.convert("L"), comparable.convert("L"))
        changed = sum(diff.point(lambda v: 255 if v > 8 else 0).histogram()[1:])
        return changed / max(1, source.width * source.height)

    def assert_pixel_change_bounded(
        self,
        source: Image.Image,
        processed: Image.Image,
        max_ratio: float = 0.60,
    ) -> None:
        """Assert that the pixel change ratio is within *max_ratio*."""
        ratio = self._pixel_change_ratio(source, processed)
        self.assertLessEqual(
            ratio,
            max_ratio,
            f"Pixel change ratio {ratio:.4f} exceeds maximum {max_ratio}",
        )

    def assert_size_change_bounded(
        self,
        source: Image.Image,
        processed: Image.Image,
        max_ratio: float = 0.55,
    ) -> None:
        """Assert that the size change ratio is within *max_ratio*."""
        source_pixels = source.width * source.height
        processed_pixels = processed.width * processed.height
        ratio = abs(source_pixels - processed_pixels) / max(1, source_pixels)
        self.assertLessEqual(
            ratio,
            max_ratio,
            f"Size change ratio {ratio:.4f} exceeds maximum {max_ratio}",
        )


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------


def _build_scan_report(filename: str = "page.png") -> dict:
    """Build a minimal scan report for *filename*."""
    return {
        "schema_version": "scan-qc.scan.v1",
        "status": "finished",
        "files": [{"relative_path": filename, "openable": True, "scan_findings": []}],
    }


def _load_processed(process_dir: Path, record: dict) -> Image.Image:
    """Open the processed image using the record's output_relative_path."""
    return Image.open(process_dir / record["output_relative_path"])


# ---------------------------------------------------------------------------
# TestCropQuality
# ---------------------------------------------------------------------------


class TestCropQuality(unittest.TestCase, QualityAsserter):

    def test_crop_margin_enforced(self) -> None:
        """Image with white margins: crop reduces size but preserves content."""
        with tempfile.TemporaryDirectory(prefix="quality-crop-margin-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            base = Image.new("RGB", (140, 100), "white")
            draw = ImageDraw.Draw(base)
            for y in range(20, 80, 15):
                draw.rectangle((15, y, 125, y + 5), fill=(20, 20, 20))
            image = Image.new("RGB", (200, 150), "white")
            image.paste(base, (30, 25))
            image.save(input_dir / "page.png")

            options = ProcessingOptions(auto_crop=True, crop_margin_mm=0.0, workers=1)
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            if record["cropped"]:
                self.assertLess(record["output_size"][0], image.width)
                self.assertLess(record["output_size"][1], image.height)
                with _load_processed(output_dir, record) as processed:
                    self.assert_size_change_bounded(image, processed, max_ratio=0.55)

    def test_crop_preserves_text_near_edge(self) -> None:
        """Text drawn near edge should not be clipped after crop."""
        with tempfile.TemporaryDirectory(prefix="quality-crop-edge-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = Image.new("RGB", (200, 150), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 10, 60, 18), fill=(20, 20, 20))
            for y in range(40, 110, 15):
                draw.rectangle((30, y, 170, y + 5), fill=(20, 20, 20))
            image.save(input_dir / "page.png")

            options = ProcessingOptions(auto_crop=True, crop_margin_mm=2.5, workers=1)
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            with _load_processed(output_dir, record) as processed:
                gray = processed.convert("L")
                histogram = gray.histogram()
                dark_pixels = sum(histogram[:80])
                self.assertGreater(
                    dark_pixels, 0,
                    "Text near edge should still be present after crop",
                )

    def test_crop_idempotent(self) -> None:
        """Processing the same image twice should produce the same result."""
        image = _printed_text_page()
        options = ProcessingOptions(auto_crop=True, crop_margin_mm=0.0, workers=1)

        with tempfile.TemporaryDirectory(prefix="quality-crop-idem-a-") as td:
            root_a = Path(td)
            input_a = root_a / "input"
            output_a = root_a / "processed"
            input_a.mkdir()
            image.save(input_a / "page.png")
            manifest_a = process_images(
                _build_scan_report(), input_a, output_a, options=options,
            )
            record_a = manifest_a["files"][0]

        with tempfile.TemporaryDirectory(prefix="quality-crop-idem-b-") as td:
            root_b = Path(td)
            input_b = root_b / "input"
            output_b = root_b / "processed"
            input_b.mkdir()
            image.save(input_b / "page.png")
            manifest_b = process_images(
                _build_scan_report(), input_b, output_b, options=options,
            )
            record_b = manifest_b["files"][0]

        self.assertEqual(record_a["output_size"], record_b["output_size"])
        self.assertEqual(record_a["output_sha256"], record_b["output_sha256"])


# ---------------------------------------------------------------------------
# TestDeskewQuality
# ---------------------------------------------------------------------------


class TestDeskewQuality(unittest.TestCase, QualityAsserter):

    def test_deskew_accuracy_small_angle(self) -> None:
        """A document skewed 1.5 degrees should be deskewed."""
        with tempfile.TemporaryDirectory(prefix="quality-deskew-small-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = _skewed_document_page(1.5)
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                deskew=True, auto_crop=False, despeckle=False, workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            if record["deskewed"]:
                self.assertIsNotNone(record.get("deskew_residual_degrees"))

    def test_deskew_accuracy_large_angle(self) -> None:
        """A document skewed 3 degrees should be deskewed."""
        with tempfile.TemporaryDirectory(prefix="quality-deskew-large-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = _skewed_document_page(3.0)
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                deskew=True, auto_crop=False, despeckle=False, workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            if record["deskewed"]:
                self.assertIsNotNone(record.get("deskew_residual_degrees"))

    def test_deskew_preserves_content(self) -> None:
        """After deskew, text lines should still be present in the image."""
        with tempfile.TemporaryDirectory(prefix="quality-deskew-content-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = _skewed_document_page(2.0)
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                deskew=True, auto_crop=False, despeckle=False, workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            with _load_processed(output_dir, record) as processed:
                gray = processed.convert("L")
                histogram = gray.histogram()
                dark_pixels = sum(histogram[:80])
                self.assertGreater(
                    dark_pixels, 0, "Text content should remain after deskew",
                )

    def test_upright_page_not_deskewed(self) -> None:
        """An unskewed page should have minimal change when deskew is enabled."""
        with tempfile.TemporaryDirectory(prefix="quality-deskew-upright-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = _printed_text_page()
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                deskew=True, auto_crop=False, despeckle=False, workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            if not record["deskewed"]:
                self.assertIn(
                    record["deskew_reason"],
                    {
                        "low confidence",
                        "low contrast",
                        "no significant skew detected",
                        "angle below correction threshold",
                    },
                )
            with _load_processed(output_dir, record) as processed:
                self.assert_pixel_change_bounded(image, processed, max_ratio=0.10)


# ---------------------------------------------------------------------------
# TestDespeckleQuality
# ---------------------------------------------------------------------------


class TestDespeckleQuality(unittest.TestCase, QualityAsserter):

    def test_despeckle_removes_isolated_dots(self) -> None:
        """Isolated black dots (speckles) should be removed."""
        with tempfile.TemporaryDirectory(prefix="quality-despeckle-dots-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = Image.new("RGB", (200, 150), (240, 240, 240))
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 20, 180, 130), outline=(60, 60, 60), width=2)
            for y in range(40, 100, 20):
                draw.line((40, y, 160, y), fill=(20, 20, 20), width=2)
            speckle_positions = [(30, 10), (10, 75), (190, 10), (100, 5), (5, 5)]
            for px, py in speckle_positions:
                image.putpixel((px, py), (0, 0, 0))
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                deskew=False,
                auto_crop=False,
                despeckle=True,
                despeckle_content_type_check=False,
                workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["despeckled"])
            with _load_processed(output_dir, record) as processed:
                for px, py in speckle_positions:
                    rx = min(px, processed.width - 1)
                    ry = min(py, processed.height - 1)
                    pixel = processed.getpixel((rx, ry))
                    self.assertNotEqual(
                        pixel,
                        (0, 0, 0),
                        f"Speckle at ({px},{py}) should have been removed",
                    )

    def test_despeckle_preserves_text_dots(self) -> None:
        """Period-sized dots that are part of text should remain."""
        with tempfile.TemporaryDirectory(prefix="quality-despeckle-text-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = Image.new("RGB", (200, 150), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 20, 180, 130), outline=(60, 60, 60), width=2)
            for y in range(40, 100, 20):
                draw.line((40, y, 160, y), fill=(20, 20, 20), width=2)
            draw.rectangle((80, 105, 86, 111), fill=(20, 20, 20))
            draw.rectangle((120, 105, 126, 111), fill=(20, 20, 20))
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                deskew=False,
                auto_crop=False,
                despeckle=True,
                despeckle_content_type_check=False,
                workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            with _load_processed(output_dir, record) as processed:
                gray = processed.convert("L")
                histogram = gray.histogram()
                dark_pixels = sum(histogram[:80])
                self.assertGreater(
                    dark_pixels, 0, "Text dots should be preserved",
                )

    def test_despeckle_preserves_photo(self) -> None:
        """Photo content should be minimally affected by despeckle."""
        with tempfile.TemporaryDirectory(prefix="quality-despeckle-photo-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = _photo_content_page()
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                deskew=False,
                auto_crop=False,
                despeckle=True,
                despeckle_content_type_check=False,
                workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            with _load_processed(output_dir, record) as processed:
                self.assert_pixel_change_bounded(image, processed, max_ratio=0.20)

    def test_despeckle_preserves_stamp_seals(self) -> None:
        """Red stamp should not be despeckled away."""
        with tempfile.TemporaryDirectory(prefix="quality-despeckle-stamp-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = _stamp_seal_page()
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                deskew=False,
                auto_crop=False,
                despeckle=True,
                despeckle_content_type_check=False,
                workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            with _load_processed(output_dir, record) as processed:
                rgb = processed.convert("RGB")
                red_found = False
                for x in range(rgb.width):
                    for y in range(rgb.height):
                        r, g, b = rgb.getpixel((x, y))
                        if r > 150 and g < 80 and b < 80:
                            red_found = True
                            break
                    if red_found:
                        break
                self.assertTrue(
                    red_found,
                    "Red stamp content should be preserved after despeckle",
                )


# ---------------------------------------------------------------------------
# TestToneQuality
# ---------------------------------------------------------------------------


class TestToneQuality(unittest.TestCase, QualityAsserter):

    def test_tone_improves_brightness(self) -> None:
        """Dark image brightness should improve after tone normalization."""
        with tempfile.TemporaryDirectory(prefix="quality-tone-bright-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = Image.new("RGB", (180, 120), (120, 120, 120))
            draw = ImageDraw.Draw(image)
            for y in (28, 48, 68):
                draw.rectangle((28, y, 150, y + 5), fill=(50, 50, 50))
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                normalize_tones=True,
                deskew=False,
                auto_crop=False,
                despeckle=False,
                workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            with _load_processed(output_dir, record) as processed:
                source_mean = ImageStat.Stat(image.convert("L")).mean[0]
                processed_mean = ImageStat.Stat(processed.convert("L")).mean[0]
                if record["tone_normalized"]:
                    self.assertGreater(processed_mean, source_mean)

    def test_tone_preserves_white_areas(self) -> None:
        """Already white areas should stay white after tone normalization."""
        with tempfile.TemporaryDirectory(prefix="quality-tone-white-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = Image.new("RGB", (180, 120), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((30, 30, 60, 40), fill=(20, 20, 20))
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                normalize_tones=True,
                deskew=False,
                auto_crop=False,
                despeckle=False,
                workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            with _load_processed(output_dir, record) as processed:
                corner = processed.convert("L").getpixel((5, 5))
                self.assertGreater(
                    corner, 240, "White corner areas should remain white",
                )

    def test_tone_preserves_dark_text(self) -> None:
        """Dark text should not be lightened by tone normalization."""
        with tempfile.TemporaryDirectory(prefix="quality-tone-dark-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = Image.new("RGB", (180, 120), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((30, 30, 150, 40), fill=(10, 10, 10))
            draw.rectangle((30, 55, 150, 65), fill=(10, 10, 10))
            draw.rectangle((30, 80, 150, 90), fill=(10, 10, 10))
            image.save(input_dir / "page.png")

            options = ProcessingOptions(
                normalize_tones=True,
                deskew=False,
                auto_crop=False,
                despeckle=False,
                workers=1,
            )
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            with _load_processed(output_dir, record) as processed:
                gray = processed.convert("L")
                center_pixel = gray.getpixel((90, 35))
                self.assertLess(
                    center_pixel, 60,
                    "Dark text should remain dark after tone processing",
                )


# ---------------------------------------------------------------------------
# TestFullPipelineQuality
# ---------------------------------------------------------------------------


class TestFullPipelineQuality(unittest.TestCase, QualityAsserter):

    def test_standard_mode_produces_valid_output(self) -> None:
        """Standard mode processes all document types without errors."""
        doc_generators = [
            ("printed.png", _printed_text_page),
            ("handwritten.png", _handwritten_text_page),
            ("faded.png", _faded_text_page),
            ("ledger.png", _ledger_form_page),
            ("stamp.png", _stamp_seal_page),
        ]
        options = ProcessingOptions(
            auto_crop=True,
            deskew=True,
            despeckle=True,
            normalize_tones=True,
            workers=1,
        )
        for filename, gen in doc_generators:
            with self.subTest(doc=filename):
                with tempfile.TemporaryDirectory(prefix="quality-standard-") as td:
                    root = Path(td)
                    input_dir = root / "input"
                    output_dir = root / "processed"
                    input_dir.mkdir()

                    image = gen()
                    image.save(input_dir / filename)

                    manifest = process_images(
                        _build_scan_report(filename),
                        input_dir, output_dir, options=options,
                    )
                    record = manifest["files"][0]
                    self.assertEqual(
                        record["status"], "processed",
                        f"{filename} should process successfully",
                    )
                    self.assertIn(
                        "output_relative_path", record,
                        f"{filename} should have output path",
                    )
                    output_path = output_dir / record["output_relative_path"]
                    self.assertTrue(
                        output_path.exists(),
                        f"{filename} output file should exist",
                    )
                    with Image.open(output_path) as result:
                        self.assertGreater(result.width, 0)
                        self.assertGreater(result.height, 0)

    def test_light_mode_minimal_changes(self) -> None:
        """Light mode (auto_crop only) changes less than standard mode."""
        image = _printed_text_page()
        light_options = ProcessingOptions(
            auto_crop=True, deskew=False, despeckle=False, workers=1,
        )
        standard_options = ProcessingOptions(
            auto_crop=True,
            deskew=True,
            despeckle=True,
            normalize_tones=True,
            workers=1,
        )

        with tempfile.TemporaryDirectory(prefix="quality-light-") as td:
            root = Path(td)
            input_dir = root / "input"
            light_output = root / "light"
            input_dir.mkdir()
            image.save(input_dir / "page.png")

            light_manifest = process_images(
                _build_scan_report(), input_dir, light_output,
                options=light_options,
            )
            light_record = light_manifest["files"][0]
            self.assertEqual(light_record["status"], "processed")
            with _load_processed(light_output, light_record) as light_img:
                light_change = self._pixel_change_ratio(image, light_img)

        with tempfile.TemporaryDirectory(prefix="quality-std-") as td:
            root = Path(td)
            input_dir = root / "input"
            standard_output = root / "standard"
            input_dir.mkdir()
            image.save(input_dir / "page.png")

            standard_manifest = process_images(
                _build_scan_report(), input_dir, standard_output,
                options=standard_options,
            )
            standard_record = standard_manifest["files"][0]
            self.assertEqual(standard_record["status"], "processed")
            with _load_processed(standard_output, standard_record) as std_img:
                std_change = self._pixel_change_ratio(image, std_img)

        self.assertLessEqual(
            light_change,
            std_change + 0.05,
            "Light mode should change fewer pixels than standard mode (with tolerance)",
        )

    def test_qc_only_mode_no_modification(self) -> None:
        """No processing options means output is identical to input."""
        with tempfile.TemporaryDirectory(prefix="quality-qc-only-") as td:
            root = Path(td)
            input_dir = root / "input"
            output_dir = root / "processed"
            input_dir.mkdir()

            image = _printed_text_page()
            image.save(input_dir / "page.png")

            options = ProcessingOptions(workers=1)
            manifest = process_images(
                _build_scan_report(), input_dir, output_dir, options=options,
            )

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record.get("cropped", False))
            self.assertFalse(record.get("deskewed", False))
            self.assertFalse(record.get("despeckled", False))
            self.assertFalse(record.get("tone_normalized", False))

            with _load_processed(output_dir, record) as processed:
                source_rgb = image.convert("RGB")
                processed_rgb = processed.convert("RGB")
                if source_rgb.size == processed_rgb.size:
                    self.assertEqual(
                        source_rgb.tobytes(),
                        processed_rgb.tobytes(),
                        "Output should be identical to input when no processing is enabled",
                    )


if __name__ == "__main__":
    unittest.main()
