from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.cli import main
from archive_scan_qc.processing import ProcessingOptions, process_images
from archive_scan_qc.scanner import ScanConfig, scan_batch


class ScanToneNormalizationTest(unittest.TestCase):
    def test_normalize_tones_improves_gray_low_contrast_text_page(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-tone-normalize-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            source = _gray_low_contrast_text_page()
            source.save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            manifest = process_images(
                report,
                input_dir,
                root / "processed",
                ProcessingOptions(normalize_tones=True, workers=1),
            )

            record = manifest["files"][0]
            with Image.open(root / "processed" / "images" / "page.png") as processed:
                before_background, before_contrast = _background_and_contrast(source)
                after_background, after_contrast = _background_and_contrast(processed)
            with Image.open(root / "processed" / "images" / "page.png") as processed:
                processed.verify()

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["tone_normalized"])
            self.assertEqual(record["tone_reason"], "tone normalization applied: neutral gray low-contrast text page")
            self.assertIn("normalize_tones_conservative", record["operations"])
            self.assertGreater(after_background, before_background + 20)
            self.assertGreater(after_contrast, before_contrast + 35)
            self.assertEqual(record["processing_warnings"], [])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertEqual(
                record["processing_audit"]["pixel_change_guardrail_scope"],
                "tone_normalization_recorded_by_brightness_and_contrast",
            )

            audit = json.loads((root / "processed" / "processing_audit_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(audit["operations"]["normalize_tones"])
            self.assertEqual(audit["counts"]["tone_normalized_files"], 1)
            self.assertGreaterEqual(audit["metrics"]["tone_background_delta"]["max"], 20)
            self.assertGreaterEqual(audit["metrics"]["tone_contrast_delta"]["max"], 35)

    def test_normalize_tones_improves_light_paper_low_contrast_text_page(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-tone-light-paper-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            source = _light_paper_low_contrast_text_page()
            source.save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            manifest = process_images(
                report,
                input_dir,
                root / "processed",
                ProcessingOptions(normalize_tones=True, workers=1),
            )

            record = manifest["files"][0]
            with Image.open(root / "processed" / "images" / "page.png") as processed:
                before_background, before_contrast = _background_and_contrast(source)
                after_background, after_contrast = _background_and_contrast(processed)

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["tone_normalized"])
            self.assertIn("normalize_tones_conservative", record["operations"])
            self.assertGreaterEqual(after_background, before_background + 6)
            self.assertGreaterEqual(after_contrast, before_contrast + 40)
            self.assertEqual(record["processing_warnings"], [])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])

    def test_normalize_tones_protects_color_stamp_and_light_annotations(self) -> None:
        cases = {
            "red_stamp.png": (_gray_low_contrast_text_page(red_stamp=True), "red stamp or red annotation risk"),
            "light_pencil.png": (_light_pencil_annotation_page(), "light color annotation or faint mark risk"),
            "faint_ink.png": (_faint_blue_ink_page(), "light color annotation or faint mark risk"),
            "color_content.png": (_obvious_color_content_page(), "obvious color content"),
            "gray_texture.png": (_gray_texture_page(), "tonal separation too small"),
        }
        with tempfile.TemporaryDirectory(prefix="scan-tone-risk-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            before_bytes: dict[str, bytes] = {}
            for name, (image, _reason) in cases.items():
                path = input_dir / name
                image.save(path, dpi=(300, 300))
                before_bytes[name] = image.convert("RGB").tobytes()

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            manifest = process_images(
                report,
                input_dir,
                root / "processed",
                ProcessingOptions(normalize_tones=True, workers=1),
            )

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            for name, (_image, reason_fragment) in cases.items():
                record = records[name]
                self.assertEqual(record["status"], "processed", name)
                self.assertFalse(record["tone_normalized"], name)
                self.assertIn(reason_fragment, record["tone_reason"], name)
                self.assertIn("normalize_tones_noop", record["operations"], name)
                with Image.open(root / "processed" / "images" / name) as processed:
                    self.assertEqual(processed.convert("RGB").tobytes(), before_bytes[name], name)

    def test_normalize_tones_skips_light_paper_edge_shadow_for_local_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-tone-edge-shadow-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            source = _light_paper_low_contrast_edge_shadow_page()
            source.save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            manifest = process_images(
                report,
                input_dir,
                root / "processed",
                ProcessingOptions(normalize_tones=True, workers=1),
            )

            record = manifest["files"][0]
            self.assertFalse(record["tone_normalized"])
            self.assertIn("edge shadow should use local shadow cleanup", record["tone_reason"])

    def test_normalize_tones_noops_for_normal_exposure_and_default_off(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-tone-normal-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            normal = _normal_exposure_text_page()
            normal.save(input_dir / "normal.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            default_manifest = process_images(report, input_dir, root / "default", ProcessingOptions(workers=1))
            enabled_manifest = process_images(
                report,
                input_dir,
                root / "enabled",
                ProcessingOptions(normalize_tones=True, workers=1),
            )

            default_record = default_manifest["files"][0]
            enabled_record = enabled_manifest["files"][0]
            self.assertFalse(default_record["tone_normalized"])
            self.assertEqual(default_record["tone_reason"], "tone normalization disabled")
            self.assertIn("normalize_tones_disabled", default_record["operations"])
            self.assertFalse(enabled_record["tone_normalized"])
            self.assertIn("exposure and contrast already normal", enabled_record["tone_reason"])
            with Image.open(root / "default" / "images" / "normal.png") as default_output:
                self.assertEqual(default_output.convert("RGB").tobytes(), normal.convert("RGB").tobytes())

    def test_normalize_tones_cli_and_combined_processing_stays_guarded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-tone-cli-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _gray_low_contrast_text_page(border=True, speckles=True).save(input_dir / "page.png", dpi=(300, 300))

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(root / "reports"),
                    "--process-out",
                    str(root / "processed"),
                    "--trim-dark-border",
                    "--auto-crop",
                    "--deskew",
                    "--despeckle",
                    "--normalize-tones",
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            manifest = json.loads((root / "processed" / "processing_manifest.json").read_text(encoding="utf-8"))
            audit = json.loads((root / "processed" / "processing_audit_summary.json").read_text(encoding="utf-8"))
            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertIn("normalize_tones_conservative", record["operations"])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertTrue(audit["operations"]["normalize_tones"])
            self.assertEqual(audit["counts"]["guardrail_failed_files"], 0)
            self.assertEqual(audit["privacy"]["aggregate_only"], True)


def _gray_low_contrast_text_page(
    *,
    red_stamp: bool = False,
    border: bool = False,
    speckles: bool = False,
) -> Image.Image:
    image = Image.new("RGB", (180, 120), (188, 188, 188))
    draw = ImageDraw.Draw(image)
    for y in (28, 48, 68):
        draw.rectangle((28, y, 150, y + 5), fill=(92, 92, 92))
    if red_stamp:
        draw.ellipse((118, 72, 160, 106), outline=(190, 30, 30), width=4)
        draw.line((126, 88, 152, 88), fill=(190, 30, 30), width=3)
    if border:
        draw.rectangle((0, 0, 179, 119), outline=(10, 10, 10), width=4)
    if speckles:
        for point in [(35, 18), (89, 22), (135, 96)]:
            image.putpixel(point, (0, 0, 0))
    return image


def _normal_exposure_text_page() -> Image.Image:
    image = Image.new("RGB", (180, 120), "white")
    draw = ImageDraw.Draw(image)
    for y in (28, 48, 68):
        draw.rectangle((28, y, 150, y + 5), fill=(25, 25, 25))
    return image


def _light_paper_low_contrast_text_page() -> Image.Image:
    image = Image.new("RGB", (180, 120), (226, 226, 222))
    draw = ImageDraw.Draw(image)
    for y in (28, 48, 68):
        draw.rectangle((28, y, 150, y + 5), fill=(164, 164, 160))
    return image


def _light_paper_low_contrast_edge_shadow_page() -> Image.Image:
    image = Image.new("RGB", (180, 120), (232, 232, 228))
    draw = ImageDraw.Draw(image)
    for y in (28, 48, 68):
        draw.rectangle((28, y, 150, y + 5), fill=(168, 168, 164))
    for x in range(0, 28):
        shade = 174 + int(x * 1.6)
        draw.line((x, 0, x, 119), fill=(shade, shade, shade))
    return image


def _light_pencil_annotation_page() -> Image.Image:
    image = _gray_low_contrast_text_page()
    draw = ImageDraw.Draw(image)
    draw.line((20, 96, 156, 88), fill=(170, 150, 188), width=2)
    return image


def _faint_blue_ink_page() -> Image.Image:
    image = _gray_low_contrast_text_page()
    draw = ImageDraw.Draw(image)
    draw.arc((40, 82, 120, 118), 190, 350, fill=(145, 165, 200), width=2)
    return image


def _obvious_color_content_page() -> Image.Image:
    image = _gray_low_contrast_text_page()
    draw = ImageDraw.Draw(image)
    draw.rectangle((126, 18, 164, 48), fill=(80, 145, 210))
    return image


def _gray_texture_page() -> Image.Image:
    image = Image.new("RGB", (180, 120), (205, 205, 200))
    draw = ImageDraw.Draw(image)
    for x in range(0, 180, 6):
        draw.line((x, 0, x, 119), fill=(196, 196, 192))
    return image


def _background_and_contrast(image: Image.Image) -> tuple[int, int]:
    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = grayscale.width * grayscale.height
    p05 = _histogram_percentile(histogram, total, 0.05)
    p95 = _histogram_percentile(histogram, total, 0.95)
    return p95, p95 - p05


def _histogram_percentile(histogram: list[int], total: int, percentile: float) -> int:
    target = total * percentile
    running = 0
    for value, count in enumerate(histogram):
        running += count
        if running >= target:
            return value
    return 255


if __name__ == "__main__":
    unittest.main()
