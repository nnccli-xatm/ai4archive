from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

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


def _scanline_page(orientation: str = "horizontal") -> Image.Image:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (42, 64, 86):
        draw.rectangle((42, y, 158, y + 5), fill=(36, 36, 36))
    if orientation == "horizontal":
        draw.rectangle((16, 132, 244, 133), fill=(222, 222, 218))
    elif orientation == "vertical":
        draw.rectangle((212, 18, 213, 164), fill=(222, 222, 218))
    else:
        raise ValueError(orientation)
    return image


def _process_one(image: Image.Image, options: ProcessingOptions) -> tuple[dict, Image.Image, Image.Image, Path]:
    temp = tempfile.TemporaryDirectory(prefix="scanline-lightening-")
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


class ScanlineLighteningTest(unittest.TestCase):
    def tearDown(self) -> None:
        while _PROCESS_TEMPS:
            _PROCESS_TEMPS.pop().cleanup()

    def test_lighten_scanlines_improves_safe_horizontal_and_vertical_lines(self) -> None:
        cases = {
            "horizontal": ((12, 132, 248, 134), (36, 36, 164, 96)),
            "vertical": ((212, 18, 214, 164), (36, 36, 164, 96)),
        }
        for orientation, (line_box, text_box) in cases.items():
            manifest, source, processed, process_dir = _process_one(
                _scanline_page(orientation),
                ProcessingOptions(lighten_scanlines=True, workers=1),
            )

            record = manifest["files"][0]
            audit = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "processed", orientation)
            self.assertTrue(record["scanlines_lightened"], orientation)
            self.assertEqual(record["scanlines_orientation"], orientation)
            self.assertIn("lighten_scanlines_conservative", record["operations"])
            self.assertGreater(_mean_luma(processed, line_box), _mean_luma(source, line_box) + 4, orientation)
            self.assertLess(_changed_ratio(source, processed, text_box), 0.002, orientation)
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [], orientation)
            self.assertGreater(record["scanlines_changed_pixel_ratio"], 0.0007, orientation)
            self.assertLess(record["scanlines_changed_pixel_ratio"], 0.035, orientation)
            self.assertTrue(audit["operations"]["lighten_scanlines"], orientation)
            self.assertEqual(audit["counts"]["scanlines_lightened_files"], 1, orientation)
            self.assertGreater(audit["metrics"]["scanlines_delta"]["max"], 4, orientation)
            with Image.open(process_dir / "images" / "page.png") as output:
                output.verify()

    def test_lighten_scanlines_is_default_off(self) -> None:
        manifest, source, processed, _process_dir = _process_one(_scanline_page(), ProcessingOptions(workers=1))

        record = manifest["files"][0]
        self.assertFalse(record["scanlines_lightened"])
        self.assertEqual(record["scanlines_reason"], "scanline lightening disabled")
        self.assertIn("lighten_scanlines_disabled", record["operations"])
        self.assertLess(_changed_ratio(source, processed, (0, 0, source.width, source.height)), 0.001)

    def test_lighten_scanlines_noops_for_protected_content_risks(self) -> None:
        cases: dict[str, tuple[Image.Image, str]] = {}

        near_text = Image.new("RGB", (260, 180), (240, 240, 236))
        near_text_draw = ImageDraw.Draw(near_text)
        for y in (42, 64, 86):
            near_text_draw.rectangle((42, y, 158, y + 5), fill=(36, 36, 36))
        ImageDraw.Draw(near_text).rectangle((16, 64, 244, 65), fill=(222, 222, 218))
        cases["near_text"] = (near_text, "no confident")

        table_line = _scanline_page()
        ImageDraw.Draw(table_line).line((24, 126, 236, 126), fill=(45, 45, 45), width=2)
        cases["table_line"] = (table_line, "no confident")

        page_number = _scanline_page()
        ImageDraw.Draw(page_number).rectangle((120, 166, 140, 174), fill=(35, 35, 35))
        cases["page_number"] = (page_number, "margin content risk")

        red_stamp = _scanline_page()
        ImageDraw.Draw(red_stamp).ellipse((170, 84, 224, 138), outline=(180, 20, 20), width=4)
        cases["red_stamp"] = (red_stamp, "stamp")

        color_mark = _scanline_page()
        ImageDraw.Draw(color_mark).rectangle((178, 42, 210, 64), fill=(58, 128, 205))
        cases["color_mark"] = (color_mark, "color content")

        binding_hole = _scanline_page()
        ImageDraw.Draw(binding_hole).ellipse((2, 80, 12, 90), fill=(24, 24, 24))
        cases["binding_hole"] = (binding_hole, "binding")

        archival_mark = _scanline_page()
        ImageDraw.Draw(archival_mark).rectangle((4, 112, 18, 154), fill=(60, 60, 60))
        cases["archival_mark"] = (archival_mark, "edge mark")

        for name, (image, reason_fragment) in cases.items():
            manifest, source, processed, _process_dir = _process_one(
                image,
                ProcessingOptions(lighten_scanlines=True, workers=1),
            )
            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed", name)
            self.assertFalse(record["scanlines_lightened"], name)
            self.assertIn("lighten_scanlines_noop", record["operations"], name)
            self.assertIn(reason_fragment, record["scanlines_reason"], name)
            self.assertLess(_changed_ratio(source, processed, (0, 0, source.width, source.height)), 0.001, name)

    def test_lighten_scanlines_noops_for_low_confidence_pages(self) -> None:
        normal = Image.new("RGB", (260, 180), (240, 240, 236))
        dark = Image.new("RGB", (260, 180), (128, 128, 124))
        color = _scanline_page()
        ImageDraw.Draw(color).rectangle((176, 40, 214, 70), fill=(55, 130, 210))
        broad = _scanline_page()
        draw = ImageDraw.Draw(broad)
        for y in range(20, 160, 8):
            draw.rectangle((16, y, 244, y + 1), fill=(222, 222, 218))
        low_confidence = Image.new("RGB", (260, 180), (240, 240, 236))
        ImageDraw.Draw(low_confidence).rectangle((16, 132, 244, 133), fill=(222, 222, 218))

        cases = {
            "normal": (normal, "low-confidence tonal evidence"),
            "dark": (dark, "page is too dark"),
            "color": (color, "color content"),
            "broad": (broad, "broad uneven lighting"),
            "low_confidence": (low_confidence, "low-confidence tonal evidence"),
        }

        for name, (image, reason_fragment) in cases.items():
            manifest, source, processed, _process_dir = _process_one(
                image,
                ProcessingOptions(lighten_scanlines=True, workers=1),
            )
            record = manifest["files"][0]
            self.assertFalse(record["scanlines_lightened"], name)
            self.assertIn(reason_fragment, record["scanlines_reason"], name)
            self.assertLess(_changed_ratio(source, processed, (0, 0, source.width, source.height)), 0.001, name)

    def test_cli_plan_and_combined_processing_stay_guarded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scanline-lightening-cli-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            image = _scanline_page()
            image.save(input_dir / "page.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("project", "batch", input_dir, root / "scan", workers=1))
            plan = build_processing_plan(report, input_dir, ProcessingOptions(lighten_scanlines=True, workers=1))
            self.assertTrue(plan["operations"]["lighten_scanlines"])
            self.assertEqual(plan["summary"]["scanline_lightening_candidates"], 1)
            self.assertTrue(plan["files"][0]["scanline_lightening_candidate"])

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
                    "--lighten-scanlines",
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            manifest = json.loads((root / "processed" / "processing_manifest.json").read_text(encoding="utf-8"))
            audit = json.loads((root / "processed" / "processing_audit_summary.json").read_text(encoding="utf-8"))
            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertIn("lighten_scanlines_conservative", record["operations"])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertTrue(audit["guardrails"]["enabled"])
            self.assertTrue(audit["operations"]["lighten_scanlines"])
            self.assertEqual(audit["counts"]["guardrail_failed_files"], 0)
            self.assertEqual(audit["privacy"]["aggregate_only"], True)


if __name__ == "__main__":
    unittest.main()
