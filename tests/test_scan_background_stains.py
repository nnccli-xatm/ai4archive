from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

from archive_scan_qc.cli import main
from archive_scan_qc.processing import ProcessingOptions, process_images
from archive_scan_qc.processing_plan import build_processing_plan
from archive_scan_qc.scanner import ScanConfig, scan_batch


def _mean_luma(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    return ImageStat.Stat(image.crop(box).convert("L")).mean[0]


def _changed_ratio(before: Image.Image, after: Image.Image, box: tuple[int, int, int, int]) -> float:
    before_l = before.crop(box).convert("L")
    after_l = after.crop(box).convert("L")
    diff = ImageChops.difference(before_l, after_l)
    changed = sum(diff.point(lambda value: 255 if value > 8 else 0).histogram()[1:])
    return changed / max(1, before_l.width * before_l.height)


def _light_page_with_stains() -> Image.Image:
    image = Image.new("RGB", (240, 170), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (42, 66, 90):
        draw.rectangle((48, y, 178, y + 5), fill=(38, 38, 38))
    draw.ellipse((58, 116, 80, 134), fill=(216, 216, 211))
    draw.ellipse((188, 18, 210, 34), fill=(218, 218, 214))
    draw.rectangle((196, 112, 208, 124), fill=(214, 214, 210))
    return image


def _light_page_with_soft_cloud_stain(color: tuple[int, int, int] = (224, 218, 178)) -> Image.Image:
    image = Image.new("RGB", (320, 220), (242, 242, 236))
    draw = ImageDraw.Draw(image)
    for y in (48, 74, 100):
        draw.rectangle((72, y, 220, y + 5), fill=(36, 36, 36))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((170, 125, 260, 185), fill=180)
    mask = mask.filter(ImageFilter.GaussianBlur(9))
    stain = Image.new("RGB", image.size, color)
    return Image.composite(stain, image, mask)


def _process_one(image: Image.Image, options: ProcessingOptions) -> tuple[dict, Image.Image, Image.Image, Path]:
    temp = tempfile.TemporaryDirectory(prefix="scan-background-stains-")
    root = Path(temp.name)
    input_dir = root / "input"
    report_dir = root / "reports"
    process_dir = root / "processed"
    input_dir.mkdir()
    source = input_dir / "page.png"
    image.save(source, dpi=(300, 300))

    report = scan_batch(ScanConfig("project", "batch", input_dir, report_dir, workers=1))
    manifest = process_images(report, input_dir, process_dir, options)
    with Image.open(process_dir / "images" / "page.png") as processed:
        processed_image = processed.copy()
    _PROCESS_TEMPS.append(temp)
    return manifest, image.copy(), processed_image, process_dir


_PROCESS_TEMPS: list[tempfile.TemporaryDirectory[str]] = []


class BackgroundStainProcessingTest(unittest.TestCase):
    def tearDown(self) -> None:
        while _PROCESS_TEMPS:
            _PROCESS_TEMPS.pop().cleanup()

    def test_lighten_background_stains_improves_safe_stains_and_protects_text(self) -> None:
        manifest, source, processed, process_dir = _process_one(
            _light_page_with_stains(),
            ProcessingOptions(lighten_background_stains=True, workers=1),
        )

        record = manifest["files"][0]
        audit = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "processed")
        self.assertTrue(record["background_stains_lightened"])
        self.assertIn("lighten_background_stains_conservative", record["operations"])
        self.assertGreater(_mean_luma(processed, (54, 112, 84, 138)), _mean_luma(source, (54, 112, 84, 138)) + 6)
        self.assertLess(_changed_ratio(source, processed, (44, 36, 184, 100)), 0.002)
        self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
        self.assertGreater(record["background_stains_changed_pixel_ratio"], 0.0005)
        self.assertLess(record["background_stains_changed_pixel_ratio"], 0.025)
        self.assertTrue(audit["operations"]["lighten_background_stains"])
        self.assertEqual(audit["counts"]["background_stains_lightened_files"], 1)
        self.assertGreater(audit["metrics"]["background_stains_delta"]["max"], 4)
        with Image.open(process_dir / "images" / "page.png") as output:
            output.verify()

    def test_lighten_background_stains_reduces_soft_pale_yellow_cloud(self) -> None:
        manifest, source, processed, process_dir = _process_one(
            _light_page_with_soft_cloud_stain(),
            ProcessingOptions(lighten_background_stains=True, workers=1),
        )

        record = manifest["files"][0]
        audit_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
        audit = json.loads(audit_text)
        before_rgb = ImageStat.Stat(source.crop((190, 142, 238, 174)).convert("RGB")).mean
        after_rgb = ImageStat.Stat(processed.crop((190, 142, 238, 174)).convert("RGB")).mean
        self.assertTrue(record["background_stains_lightened"])
        self.assertGreater(_mean_luma(processed, (170, 125, 260, 185)), _mean_luma(source, (170, 125, 260, 185)) + 6)
        self.assertGreater(after_rgb[2] - before_rgb[2], after_rgb[0] - before_rgb[0])
        self.assertLessEqual(record["background_stains_changed_pixel_ratio"], 0.085)
        self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
        self.assertEqual(audit["counts"]["background_stains_lightened_files"], 1)
        self.assertTrue(audit["privacy"]["aggregate_only"])
        for forbidden in ("page.png", str(process_dir), "source_relative_path", "source_sha256"):
            self.assertNotIn(forbidden, audit_text)

    def test_lighten_background_stains_reduces_soft_local_gray_cloud(self) -> None:
        manifest, source, processed, _process_dir = _process_one(
            _light_page_with_soft_cloud_stain((218, 218, 214)),
            ProcessingOptions(lighten_background_stains=True, workers=1),
        )

        record = manifest["files"][0]
        self.assertTrue(record["background_stains_lightened"])
        self.assertGreater(_mean_luma(processed, (170, 125, 260, 185)), _mean_luma(source, (170, 125, 260, 185)) + 5)
        self.assertLessEqual(record["background_stains_changed_pixel_ratio"], 0.085)
        self.assertEqual(record["processing_audit"]["cumulative_change_guard_action"], "passed")

    def test_lighten_background_stains_is_default_off(self) -> None:
        manifest, source, processed, _process_dir = _process_one(_light_page_with_stains(), ProcessingOptions(workers=1))

        record = manifest["files"][0]
        self.assertFalse(record["background_stains_lightened"])
        self.assertEqual(record["background_stains_reason"], "background stain lightening disabled")
        self.assertIn("lighten_background_stains_disabled", record["operations"])
        self.assertLess(_changed_ratio(source, processed, (0, 0, source.width, source.height)), 0.001)

    def test_lighten_background_stains_noops_for_protected_content_risks(self) -> None:
        cases: dict[str, tuple[Image.Image, str]] = {}

        near_text = _light_page_with_stains()
        ImageDraw.Draw(near_text).ellipse((50, 48, 75, 62), fill=(216, 216, 211))
        cases["near_text"] = (near_text, "near text")

        red_stamp = _light_page_with_stains()
        ImageDraw.Draw(red_stamp).ellipse((166, 92, 214, 138), outline=(190, 32, 32), width=4)
        cases["red_stamp"] = (red_stamp, "stamp")

        color_mark = _light_page_with_stains()
        ImageDraw.Draw(color_mark).rectangle((186, 32, 210, 54), fill=(70, 145, 210))
        cases["color_mark"] = (color_mark, "color content")

        margin_note = _light_page_with_stains()
        ImageDraw.Draw(margin_note).rectangle((3, 58, 30, 62), fill=(45, 45, 45))
        cases["margin_note"] = (margin_note, "margin content risk")

        binding_hole = _light_page_with_stains()
        ImageDraw.Draw(binding_hole).ellipse((2, 78, 12, 88), fill=(25, 25, 25))
        cases["binding_hole"] = (binding_hole, "binding")

        historical_damage = Image.new("RGB", (240, 170), (240, 240, 236))
        hist_draw = ImageDraw.Draw(historical_damage)
        for y in (42, 66, 90):
            hist_draw.rectangle((48, y, 178, y + 5), fill=(38, 38, 38))
        hist_draw.ellipse((28, 108, 80, 140), fill=(218, 218, 214))
        cases["historical_damage"] = (historical_damage, "historical damage risk")

        page_number = _light_page_with_stains()
        page_draw = ImageDraw.Draw(page_number)
        page_draw.ellipse((178, 16, 212, 38), fill=(218, 218, 214))
        page_draw.text((184, 19), "12", fill=(35, 35, 35))
        cases["page_number"] = (page_number, "near text")

        table_line = _light_page_with_stains()
        table_draw = ImageDraw.Draw(table_line)
        table_draw.ellipse((170, 112, 212, 134), fill=(218, 218, 214))
        for y in (118, 128):
            table_draw.line((150, y, 222, y), fill=(52, 52, 52), width=1)
        for x in (168, 198):
            table_draw.line((x, 110, x, 138), fill=(52, 52, 52), width=1)
        cases["table_line"] = (table_line, "near text")

        for name, (image, reason_fragment) in cases.items():
            manifest, source, processed, _process_dir = _process_one(
                image,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed", name)
            self.assertFalse(record["background_stains_lightened"], name)
            self.assertIn("lighten_background_stains_noop", record["operations"], name)
            self.assertIn(reason_fragment, record["background_stains_reason"], name)
            self.assertLess(_changed_ratio(source, processed, (0, 0, source.width, source.height)), 0.001, name)

    def test_lighten_background_stains_noops_for_low_confidence_pages(self) -> None:
        normal = Image.new("RGB", (240, 170), (240, 240, 236))
        dark = Image.new("RGB", (240, 170), (130, 130, 126))
        color = _light_page_with_stains()
        ImageDraw.Draw(color).rectangle((180, 20, 220, 56), fill=(50, 120, 210))
        broad = _light_page_with_stains()
        draw = ImageDraw.Draw(broad)
        for x in range(0, 240, 10):
            draw.rectangle((x, 0, x + 4, 169), fill=(210, 210, 206))
        no_foreground = Image.new("RGB", (240, 170), (240, 240, 236))
        ImageDraw.Draw(no_foreground).ellipse((90, 70, 118, 92), fill=(216, 216, 211))

        cases = {
            "normal": (normal, "low-confidence tonal evidence"),
            "dark": (dark, "page is too dark"),
            "color": (color, "color content"),
            "broad": (broad, "broad uneven lighting"),
            "low_confidence": (no_foreground, "low-confidence tonal evidence"),
        }

        for name, (image, reason_fragment) in cases.items():
            manifest, source, processed, _process_dir = _process_one(
                image,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            record = manifest["files"][0]
            self.assertFalse(record["background_stains_lightened"], name)
            self.assertIn(reason_fragment, record["background_stains_reason"], name)
            self.assertLess(_changed_ratio(source, processed, (0, 0, source.width, source.height)), 0.001, name)

    def test_cli_plan_and_combined_processing_stay_guarded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-background-stains-cli-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            image = _light_page_with_stains()
            image.save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            plan = build_processing_plan(
                report,
                input_dir,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            self.assertTrue(plan["operations"]["lighten_background_stains"])
            self.assertEqual(plan["summary"]["background_stain_lightening_candidates"], 1)
            self.assertTrue(plan["files"][0]["background_stain_lightening_candidate"])

            exit_code = main(
                [
                    "--input",
                    str(input_dir),
                    "--out",
                    str(root / "reports"),
                    "--process-out",
                    str(root / "processed"),
                    "--deskew",
                    "--trim-dark-border",
                    "--auto-crop",
                    "--despeckle",
                    "--normalize-tones",
                    "--lighten-edge-shadow",
                    "--lighten-background-stains",
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            manifest = json.loads((root / "processed" / "processing_manifest.json").read_text(encoding="utf-8"))
            audit = json.loads((root / "processed" / "processing_audit_summary.json").read_text(encoding="utf-8"))
            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertIn("lighten_background_stains_conservative", record["operations"])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertTrue(audit["guardrails"]["enabled"])
            self.assertTrue(audit["operations"]["lighten_background_stains"])
            self.assertEqual(audit["counts"]["guardrail_failed_files"], 0)
            self.assertEqual(audit["privacy"]["aggregate_only"], True)


if __name__ == "__main__":
    unittest.main()
