from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from archive_scan_qc.processing import ProcessingOptions, process_images
from archive_scan_qc.scanner import ScanConfig, scan_batch


class ScanProcessingComboTest(unittest.TestCase):
    def test_combined_retouch_improves_safe_synthetic_page_with_auditable_bounds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-combo-safe-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "safe.png"
            original = _safe_combo_page()
            original.save(source)

            manifest = _process_combo(input_dir, report_dir, process_dir)
            audit_summary = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertFalse(record["dark_border_trimmed"])
            self.assertTrue(record["cropped"])
            self.assertTrue(record["despeckled"])
            self.assertLess(record["output_size"][0], record["original_size"][0])
            self.assertLess(record["output_size"][1], record["original_size"][1])
            self.assertEqual(record["processing_warnings"], [])
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertFalse(record["processing_audit"]["pixel_change_guardrail_applied"])
            self.assertEqual(
                record["processing_audit"]["pixel_change_guardrail_scope"],
                "geometric_change_recorded_by_size_crop_trim_or_deskew",
            )
            self.assertIn("deskew_conservative", record["operations"])
            self.assertIn("auto_crop_conservative", record["operations"])
            self.assertIn("despeckle_isolated_pixels", record["operations"])
            with Image.open(process_dir / record["output_relative_path"]) as processed:
                self.assertEqual(list(processed.size), record["output_size"])
                self.assertGreater(_image_difference_ratio(original, processed), 0.0)

            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["guardrail_failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["pixel_change_guardrail_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["pixel_change_guardrail_deferred_to_geometric_files"], 1)
            self.assertEqual(audit_summary["metrics"]["pixel_change_ratio"]["count"], 1)
            self.assertEqual(audit_summary["metrics"]["size_change_ratio"]["count"], 1)
            self.assertTrue(audit_summary["timing"]["operation_timings"]["deskew"]["enabled"])
            self.assertTrue(audit_summary["timing"]["operation_timings"]["auto_crop"]["enabled"])
            self.assertTrue(audit_summary["timing"]["operation_timings"]["despeckle"]["enabled"])
            audit_text = json.dumps(audit_summary, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("safe.png", audit_text)
            self.assertNotIn(str(input_dir), audit_text)
            self.assertNotIn(record["source_sha256"], audit_text)

    def test_combined_retouch_protects_edge_content_after_coordinate_changes(self) -> None:
        cases = ["page_number", "binding_line", "margin_note", "stamp_edge", "archival_dark_mark"]
        with tempfile.TemporaryDirectory(prefix="scan-processing-combo-edge-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            originals: dict[str, Image.Image] = {}
            for name in cases:
                image = _edge_content_combo_page(name)
                originals[f"{name}.png"] = image
                image.save(input_dir / f"{name}.png")

            manifest = _process_combo(input_dir, report_dir, process_dir)

            records = {record["source_relative_path"]: record for record in manifest["files"]}
            for name in cases:
                filename = f"{name}.png"
                record = records[filename]
                self.assertEqual(record["status"], "processed", name)
                self.assertFalse(record["deskewed"], name)
                self.assertFalse(record["dark_border_trimmed"], name)
                self.assertFalse(record["cropped"], name)
                self.assertFalse(record["despeckled"], name)
                self.assertEqual(record["output_size"], record["original_size"], name)
                self.assertEqual(record["deskew_reason"], "edge content near rotation boundary", name)
                self.assertIn(
                    record["crop_reason"],
                    {
                        "foreground reaches crop safety margin",
                        "inconsistent crop margin evidence",
                        "crop boundary evidence is too sparse",
                    },
                    name,
                )
                self.assertIn(record["despeckle_reason"], {"no isolated dark pixels found", "protected edge dark marks preserved"}, name)
                with Image.open(process_dir / record["output_relative_path"]) as processed:
                    self.assertEqual(processed.convert("RGB").tobytes(), originals[filename].tobytes(), name)

    def test_combined_retouch_keeps_unsafe_dense_noise_reviewable_without_large_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-combo-unsafe-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            report_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _dense_noise_page()
            image.save(input_dir / "dense_noise.png")

            manifest = _process_combo(input_dir, report_dir, process_dir)

            record = manifest["files"][0]
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["deskewed"])
            self.assertFalse(record["dark_border_trimmed"])
            self.assertFalse(record["cropped"])
            self.assertFalse(record["despeckled"])
            self.assertEqual(record["output_size"], record["original_size"])
            self.assertIn(record["deskew_reason"], {"low confidence", "foreground too dense", "low contrast"})
            self.assertEqual(record["crop_reason"], "foreground reaches crop safety margin")
            self.assertEqual(record["despeckle_reason"], "despeckle skipped: candidate density exceeds safety threshold")
            self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
            self.assertTrue(record["processing_audit"]["pixel_change_guardrail_applied"])
            with Image.open(process_dir / record["output_relative_path"]) as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())


def _process_combo(input_dir: Path, report_dir: Path, process_dir: Path) -> dict[str, object]:
    report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir, workers=1))
    return process_images(
        report,
        input_dir,
        process_dir,
        ProcessingOptions(deskew=True, trim_dark_border=True, auto_crop=True, crop_margin_mm=0.0, despeckle=True, despeckle_content_type_check=False, workers=1),
    )


def _safe_combo_page() -> Image.Image:
    image = Image.new("RGB", (160, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 14, 143, 105), fill=(232, 232, 232), outline=(60, 60, 60), width=2)
    for y in range(34, 82, 12):
        draw.line((38, y, 118, y), fill=(25, 25, 25), width=2)
    for point in [(25, 20), (130, 92), (80, 18)]:
        image.putpixel(point, (0, 0, 0))
    return image.rotate(-2.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")


def _edge_content_combo_page(case: str) -> Image.Image:
    image = Image.new("RGB", (170, 130), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 18, 149, 111), outline=(35, 35, 35), width=2)
    for y in range(42, 90, 12):
        draw.line((42, y, 125, y), fill=(25, 25, 25), width=2)
    if case == "page_number":
        draw.rectangle((72, 120, 88, 127), fill=(20, 20, 20))
    elif case == "binding_line":
        draw.line((4, 0, 4, 129), fill=(25, 25, 25), width=2)
    elif case == "margin_note":
        draw.rectangle((58, 0, 98, 5), fill=(25, 25, 25))
    elif case == "stamp_edge":
        draw.ellipse((154, 46, 169, 64), outline=(150, 0, 0), width=2)
    elif case == "archival_dark_mark":
        draw.rectangle((0, 55, 5, 105), fill=(60, 60, 60))
    else:
        raise ValueError(case)
    return image.rotate(-2.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")


def _dense_noise_page() -> Image.Image:
    image = Image.new("RGB", (120, 120), "white")
    for y in range(8, 114, 5):
        for x in range(8, 114, 5):
            image.putpixel((x, y), (0, 0, 0))
    for point in [(1, 1), (118, 1), (1, 118), (118, 118)]:
        image.putpixel(point, (0, 0, 0))
    return image


def _image_difference_ratio(source: Image.Image, processed: Image.Image) -> float:
    comparable = processed.resize(source.size, Image.Resampling.BILINEAR)
    diff = ImageChops.difference(source.convert("L"), comparable.convert("L"))
    changed = sum(diff.point(lambda value: 255 if value > 8 else 0).histogram()[1:])
    return changed / max(1, source.width * source.height)
