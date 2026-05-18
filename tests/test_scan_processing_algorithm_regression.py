from __future__ import annotations

import argparse
import importlib.util
import json
import math
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

from archive_scan_qc import processing as processing_module
from archive_scan_qc.benchmark import _processing_quality_regression, run_benchmark
from archive_scan_qc.processing import ProcessingOptions, _combination_quality_guard, process_images
from archive_scan_qc.scanner import ScanConfig, scan_batch


def _mean_luma(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    return ImageStat.Stat(image.crop(box).convert("L")).mean[0]


def _edge_energy(image: Image.Image) -> float:
    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(edges).mean[0])


def _changed_ratio(before: Image.Image, after: Image.Image, box: tuple[int, int, int, int]) -> float:
    before_luma = before.crop(box).convert("L")
    after_luma = after.crop(box).convert("L")
    diff = ImageChops.difference(before_luma, after_luma)
    changed = sum(diff.point(lambda value: 255 if value > 8 else 0).histogram()[1:])
    return changed / max(1, before_luma.width * before_luma.height)


def _mean_channel_spread(image: Image.Image) -> float:
    means = ImageStat.Stat(image.convert("RGB")).mean
    return max(means) - min(means)


def _side_paper_channel_spread(image: Image.Image) -> float:
    rgb = image.convert("RGB")
    bands = ((0, 0, 80, rgb.height), (160, 0, 240, rgb.height))
    spreads = []
    for box in bands:
        means = ImageStat.Stat(rgb.crop(box)).mean
        spreads.append(max(means) - min(means))
    return sum(spreads) / len(spreads)


def _mean_luma_delta(before: Image.Image, after: Image.Image) -> float:
    return abs(ImageStat.Stat(after.convert("L")).mean[0] - ImageStat.Stat(before.convert("L")).mean[0])


class ScanProcessingAlgorithmRegressionTest(unittest.TestCase):
    def test_combination_quality_guard_classifies_public_reason_codes(self) -> None:
        options = ProcessingOptions()
        base_metrics = _combination_guard_metrics()
        passed = _combination_quality_guard(
            base_metrics,
            options,
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(passed["action"], "passed")
        self.assertEqual(passed["reason_code"], "safe_combination_passed")

        combined = _combination_quality_guard(
            dict(base_metrics, pixel_change_ratio=0.42),
            options,
            cumulative_change_guard=_guard_reverted("pixel_change_ratio", "cumulative_change_score"),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(combined["action"], "reverted_to_source")
        self.assertEqual(combined["reason_code"], "combined_change_too_large_reverted")

        geometry = _combination_quality_guard(
            dict(base_metrics, size_change_ratio=0.21, crop_ratio=0.19, deskew_abs_angle_degrees=1.0),
            ProcessingOptions(audit_max_geometry_combo_crop_ratio=0.18, audit_max_geometry_combo_size_change_ratio=0.18),
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(geometry["reason_code"], "safe_combination_passed")

        geometry = _combination_quality_guard(
            dict(
                base_metrics,
                pixel_change_guardrail_scope="geometric_change_recorded_by_size_crop_trim_or_deskew",
                pixel_change_guardrail_applied=False,
                size_change_ratio=0.21,
                crop_ratio=0.19,
                deskew_abs_angle_degrees=1.0,
            ),
            ProcessingOptions(audit_max_geometry_combo_crop_ratio=0.18, audit_max_geometry_combo_size_change_ratio=0.18),
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(geometry["reason_code"], "geometric_risk_reverted")
        self.assertIn("geometry_crop_or_trim_ratio", geometry["reasons"])

        text = _combination_quality_guard(
            dict(
                base_metrics,
                faded_text_enhanced=True,
                text_edges_sharpened=True,
                faded_text_changed_pixel_ratio=0.06,
                text_edges_changed_pixel_ratio=0.05,
                local_content_changed_ratio=0.13,
            ),
            options,
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(text["reason_code"], "text_high_frequency_risk_reverted")
        self.assertIn("text_combo_changed_pixel_ratio", text["reasons"])

        low_confidence = _combination_quality_guard(
            base_metrics,
            options,
            cumulative_change_guard=_guard_passed(),
            local_content_change_guard=_guard_passed(),
            low_confidence_original_preserved=True,
        )
        self.assertEqual(low_confidence["action"], "kept_original")
        self.assertEqual(low_confidence["reason_code"], "low_confidence_original_preserved")

        cumulative_weakening = _combination_quality_guard(
            dict(
                base_metrics,
                background_stains_lightened=True,
                background_stains_changed_pixel_ratio=0.04,
                scanlines_lightened=True,
                scanlines_changed_pixel_ratio=0.015,
                local_content_changed_ratio=0.11,
                edge_content_changed_ratio=0.13,
                cumulative_foreground_weakened_ratio=0.11,
                cumulative_edge_foreground_weakened_ratio=0.13,
            ),
            options,
            cumulative_change_guard={
                **_guard_reverted("cumulative_foreground_weakening", "cumulative_edge_content_weakening"),
                "foreground_weakened_ratio": 0.11,
                "edge_foreground_weakened_ratio": 0.13,
            },
            local_content_change_guard=_guard_passed(),
        )
        self.assertEqual(cumulative_weakening["action"], "reverted_to_source")
        self.assertEqual(cumulative_weakening["reason_code"], "combined_change_too_large_reverted")
        self.assertIn("cumulative_foreground_weakening", cumulative_weakening["reasons"])
        self.assertIn("cumulative_edge_content_weakening", cumulative_weakening["reasons"])

    def test_combined_retouch_guard_allows_safe_cleanup_and_reverts_content_weakening(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-combined-retouch-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_combined_cleanup.png": _combined_retouch_guard_page("safe"),
                "synthetic_protected_faint_text.png": _combined_retouch_guard_page("faint_text"),
                "synthetic_protected_page_number.png": _combined_retouch_guard_page("page_number"),
                "synthetic_protected_table_lines.png": _combined_retouch_guard_page("table_lines"),
                "synthetic_protected_stamp.png": _combined_retouch_guard_page("stamp"),
                "synthetic_protected_marginal_note.png": _combined_retouch_guard_page("marginal_note"),
                "synthetic_protected_edge_mark.png": _combined_retouch_guard_page("edge_mark"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            def lift_background_and_center_marks(current: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                changed = current.copy()
                draw = ImageDraw.Draw(changed)
                draw.ellipse((132, 70, 184, 118), fill=(236, 236, 232))
                for y in (42, 62, 82):
                    draw.rectangle((40, y, 132, y + 4), fill=(236, 236, 232))
                draw.rectangle((174, 12, 200, 24), fill=(236, 236, 232))
                for y in (54, 76, 98):
                    draw.line((40, y, 174, y), fill=(236, 236, 232), width=2)
                for x in (76, 124, 172):
                    draw.line((x, 44, x, 106), fill=(236, 236, 232), width=2)
                draw.ellipse((86, 42, 142, 96), outline=(236, 236, 232), width=4)
                return processing_module.BackgroundStainLighteningResult(
                    changed,
                    True,
                    "background stains lightened: stable isolated stains on light paper",
                    224.0,
                    236.0,
                    12.0,
                    0.050,
                    0.050,
                )

            def lift_lines_and_edge_marks(current: Image.Image) -> processing_module.ScanlineLighteningResult:
                changed = current.copy()
                draw = ImageDraw.Draw(changed)
                draw.line((72, 124, 168, 124), fill=(242, 242, 238), width=2)
                draw.line((6, 46, 30, 58, 10, 70, 34, 82), fill=(236, 236, 232), width=2)
                draw.rectangle((4, 120, 24, 132), fill=(236, 236, 232))
                return processing_module.ScanlineLighteningResult(
                    changed,
                    True,
                    "scanlines lightened: stable horizontal scanline pattern",
                    "horizontal",
                    1,
                    205.0,
                    236.0,
                    31.0,
                    0.012,
                    0.012,
                )

            report = scan_batch(ScanConfig("synthetic-regression", "combined-retouch-guard", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_lighten_background_stains_conservative",
                side_effect=lift_background_and_center_marks,
            ), mock.patch.object(
                processing_module,
                "_lighten_scanlines_conservative",
                side_effect=lift_lines_and_edge_marks,
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(lighten_background_stains=True, lighten_scanlines=True, workers=1),
                )

            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            for name, original_bytes in source_bytes.items():
                self.assertEqual((input_dir / name).read_bytes(), original_bytes)

            safe_record = records["synthetic_safe_combined_cleanup.png"]
            safe_audit = safe_record["processing_audit"]
            self.assertTrue(safe_record["background_stains_lightened"])
            self.assertTrue(safe_record["scanlines_lightened"])
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertLessEqual(safe_audit["cumulative_change_pixel_ratio"], 0.08)
            self.assertLessEqual(safe_audit["cumulative_change_score"], 0.25)
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                self.assertEqual(output.size, pages["synthetic_safe_combined_cleanup.png"].size)

            protected_names = set(pages) - {"synthetic_safe_combined_cleanup.png"}
            for name in protected_names:
                record = records[name]
                audit = record["processing_audit"]
                self.assertFalse(record["background_stains_lightened"], name)
                self.assertFalse(record["scanlines_lightened"], name)
                self.assertEqual(audit["cumulative_change_guard_action"], "reverted_to_source", name)
                self.assertEqual(audit["combination_quality_guard_action"], "reverted_to_source", name)
                self.assertEqual(audit["combination_quality_guard_reason_code"], "combined_change_too_large_reverted", name)
                self.assertTrue(
                    {
                        "cumulative_foreground_weakening",
                        "cumulative_edge_content_weakening",
                    }.intersection(audit["cumulative_change_guard_reasons"]),
                    name,
                )
                self.assertIn("combination_quality_guard_reverted_to_source", record["operations"], name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.convert("RGB").tobytes(), pages[name].tobytes(), name)

            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], len(protected_names))
            self.assertEqual(audit_summary["counts"]["combination_quality_guard_reverted_files"], len(protected_names))
            self.assertEqual(
                audit_summary["guardrails"]["combination_quality_guard"]["reason_code_distribution"][
                    "combined_change_too_large_reverted"
                ],
                len(protected_names),
            )
            cumulative_reasons = audit_summary["guardrails"]["cumulative_change_guard"]["reason_distribution"]
            self.assertGreaterEqual(
                cumulative_reasons.get("cumulative_foreground_weakening", 0)
                + cumulative_reasons.get("cumulative_edge_content_weakening", 0),
                len(protected_names),
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_mild_broad_corner_vignette_cleanup_preserves_corner_mark(self) -> None:
        def mild_broad_corner_vignette_page(variant: str) -> Image.Image:
            image = Image.new("RGB", (320, 240), (244, 244, 240))
            pixels = image.load()
            for y in range(image.height):
                for x in range(image.width):
                    distance = math.hypot(x, y)
                    if distance < 135:
                        shade = int(round(244 - 12 * (1 - distance / 135) ** 1.15))
                        if variant == "photo_texture" and x < 75 and y < 75:
                            shade = max(
                                0,
                                min(255, shade + int(round(3 * math.sin(x * 0.7) + 2 * math.cos(y * 0.9)))),
                            )
                        pixels[x, y] = (shade, shade, shade - 4)
            draw = ImageDraw.Draw(image)
            for y in (78, 104, 130, 156):
                draw.rectangle((112, y, 248, y + 5), fill=(40, 40, 40))
            if variant == "safe":
                return image
            if variant == "photo_texture":
                return image
            if variant == "page_mark":
                draw.text((18, 16), "12", fill=(34, 34, 34))
                return image
            raise ValueError(f"unsupported variant: {variant}")

        with tempfile.TemporaryDirectory(prefix="scan-processing-mild-corner-vignette-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_mild_broad_corner_vignette.png": mild_broad_corner_vignette_page("safe"),
                "synthetic_mild_corner_photo_texture.png": mild_broad_corner_vignette_page("photo_texture"),
                "synthetic_mild_corner_page_mark.png": mild_broad_corner_vignette_page("page_mark"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "mild-corner-vignette", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_corner_shadows=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_safe_mild_broad_corner_vignette.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["corner_shadows_lightened"])
            self.assertEqual(safe_record["corner_shadows_reason_code"], "applied")
            self.assertEqual(safe_record["corner_shadows_corners"], ["top_left"])
            self.assertGreater(safe_record["corner_shadows_delta"], 2.0)
            self.assertGreater(safe_record["corner_shadows_changed_pixel_ratio"], 0.01)
            self.assertLessEqual(safe_record["corner_shadows_changed_pixel_ratio"], 0.06)
            self.assertLessEqual(safe_record["corner_shadows_candidate_pixel_ratio"], 0.10)
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                self.assertGreater(
                    _mean_luma(output, (0, 0, 70, 70)),
                    _mean_luma(pages[safe_name], (0, 0, 70, 70)) + 1.5,
                )
                self.assertLess(_changed_ratio(pages[safe_name], output, (104, 70, 260, 170)), 0.002)
                self.assertLess(_changed_ratio(pages[safe_name], output, (190, 180, 320, 240)), 0.001)

            protected_name = "synthetic_mild_corner_page_mark.png"
            protected_record = records[protected_name]
            with Image.open(process_dir / protected_record["output_relative_path"]) as protected_output:
                self.assertEqual((input_dir / protected_name).read_bytes(), source_bytes[protected_name])
                self.assertFalse(protected_record["corner_shadows_lightened"])
                self.assertEqual(protected_record["corner_shadows_reason_code"], "protected_content")
                self.assertEqual(protected_record["processing_audit"]["corner_shadows_changed_pixel_ratio"], 0.0)
                self.assertLess(
                    _changed_ratio(
                        pages[protected_name],
                        protected_output,
                        (0, 0, protected_output.width, protected_output.height),
                    ),
                    0.001,
                )
                self.assertLess(
                    abs(
                        _mean_luma(protected_output, (12, 10, 52, 42))
                        - _mean_luma(pages[protected_name], (12, 10, 52, 42))
                    ),
                    0.5,
                )

            texture_name = "synthetic_mild_corner_photo_texture.png"
            texture_record = records[texture_name]
            with Image.open(process_dir / texture_record["output_relative_path"]) as texture_output:
                self.assertEqual((input_dir / texture_name).read_bytes(), source_bytes[texture_name])
                self.assertFalse(texture_record["corner_shadows_lightened"])
                self.assertEqual(texture_record["corner_shadows_reason_code"], "texture_or_photo")
                self.assertEqual(texture_record["processing_audit"]["corner_shadows_changed_pixel_ratio"], 0.0)
                self.assertLess(
                    _changed_ratio(
                        pages[texture_name],
                        texture_output,
                        (0, 0, 75, 75),
                    ),
                    0.001,
                )
                self.assertLess(
                    abs(
                        _mean_luma(texture_output, (0, 0, 75, 75))
                        - _mean_luma(pages[texture_name], (0, 0, 75, 75))
                    ),
                    0.5,
                )

            corner_guard = audit_summary["guardrails"]["corner_shadows"]
            self.assertEqual(audit_summary["counts"]["corner_shadows_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["corner_shadows_skipped_files"], 2)
            self.assertEqual(corner_guard["applied_files"], 1)
            self.assertEqual(corner_guard["skipped_files"], 2)
            self.assertEqual(corner_guard["reason_code_distribution"]["applied"], 1)
            self.assertEqual(corner_guard["skip_reason_code_distribution"]["protected_content"], 1)
            self.assertEqual(corner_guard["skip_reason_code_distribution"]["texture_or_photo"], 1)
            self.assertIn("changed_pixel_ratio", corner_guard)
            self.assertIn("candidate_pixel_ratio", corner_guard)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_mild_partial_edge_shadow_cleanup_preserves_edge_content(self) -> None:
        def mild_partial_edge_shadow_page(variant: str) -> Image.Image:
            image = Image.new("RGB", (240, 180), (244, 244, 240))
            draw = ImageDraw.Draw(image)
            for x in range(14):
                shade = 235 + min(7, x // 2)
                draw.line((x, 12, x, image.height - 13), fill=(shade, shade, shade))
            for y in range(35, 145, 14):
                draw.rectangle((62, y, 190, y + 4), fill=(45, 45, 45))
            if variant == "safe":
                return image
            if variant == "table_lines":
                for y in (54, 82, 110):
                    draw.line((6, y, 220, y), fill=(58, 58, 58), width=2)
                for x in (18, 92, 166, 222):
                    draw.line((x, 54, x, 110), fill=(58, 58, 58), width=2)
            elif variant == "stamp":
                draw.ellipse((8, 74, 50, 116), outline=(180, 35, 35), width=4)
            elif variant == "edge_mark":
                draw.rectangle((4, 78, 22, 92), fill=(45, 45, 45))
            else:
                raise ValueError(f"unsupported variant: {variant}")
            return image

        with tempfile.TemporaryDirectory(prefix="scan-processing-mild-edge-shadow-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_mild_partial_edge_shadow.png": mild_partial_edge_shadow_page("safe"),
                "synthetic_mild_edge_table_lines.png": mild_partial_edge_shadow_page("table_lines"),
                "synthetic_mild_edge_stamp.png": mild_partial_edge_shadow_page("stamp"),
                "synthetic_mild_edge_mark.png": mild_partial_edge_shadow_page("edge_mark"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "mild-edge-shadow", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_edge_shadow=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_safe_mild_partial_edge_shadow.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["edge_shadow_lightened"])
            self.assertEqual(safe_record["edge_shadow_reason_code"], "applied_narrow_neutral_edge_shadow")
            self.assertEqual(safe_record["edge_shadow_edges"], ["left"])
            self.assertGreater(safe_record["edge_shadow_changed_pixel_ratio"], 0.01)
            self.assertLess(safe_record["edge_shadow_changed_pixel_ratio"], 0.08)
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                self.assertGreater(
                    _mean_luma(output, (0, 12, 14, 168)),
                    _mean_luma(pages[safe_name], (0, 12, 14, 168)) + 2.0,
                )
                self.assertLess(_changed_ratio(pages[safe_name], output, (54, 28, 200, 154)), 0.002)

            protected_names = set(pages) - {safe_name}
            for name in protected_names:
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertFalse(record["edge_shadow_lightened"], name)
                self.assertIn(
                    record["edge_shadow_reason_code"],
                    {"protected_edge_mark", "protected_color_content"},
                    name,
                )
                self.assertEqual(record["edge_shadow_changed_pixel_ratio"], 0.0, name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.convert("RGB").tobytes(), pages[name].tobytes(), name)

            edge_shadow_summary = audit_summary["guardrails"]["edge_shadow"]
            self.assertEqual(audit_summary["counts"]["edge_shadow_lightened_files"], 1)
            self.assertEqual(edge_shadow_summary["reason_code_distribution"]["applied_narrow_neutral_edge_shadow"], 1)
            self.assertEqual(
                edge_shadow_summary["skip_reason_code_distribution"]["protected_edge_mark"]
                + edge_shadow_summary["skip_reason_code_distribution"]["protected_color_content"],
                len(protected_names),
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_mild_vertical_fold_shadow_cleanup_allows_sparse_text_and_preserves_rules(self) -> None:
        def mild_fold_page(variant: str) -> Image.Image:
            image = Image.new("RGB", (320, 240), (244, 244, 240))
            draw = ImageDraw.Draw(image)
            for x in range(152, 169):
                distance = abs(x - 160) / 8
                shade = int(round(244 - 10 * (1 - distance) ** 1.2))
                draw.line((x, 12, x, image.height - 12), fill=(shade, shade, shade - 4))
            for y in (70, 120):
                draw.rectangle((72, y, 248, y + 2), fill=(50, 50, 50))
            if variant == "safe":
                return image
            if variant == "vertical_rule":
                draw.line((160, 28, 160, 214), fill=(35, 35, 35), width=2)
                return image
            if variant == "light_form_divider":
                draw.line((160, 28, 160, 214), fill=(202, 202, 198), width=1)
                return image
            if variant == "dense_foreground":
                for y in range(35, 205, 10):
                    draw.rectangle((45, y, 275, y + 3), fill=(45, 45, 45))
                return image
            raise ValueError(f"unsupported variant: {variant}")

        with tempfile.TemporaryDirectory(prefix="scan-processing-mild-fold-shadow-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_mild_vertical_fold_sparse_text.png": mild_fold_page("safe"),
                "synthetic_vertical_fold_rule_protected.png": mild_fold_page("vertical_rule"),
                "synthetic_vertical_fold_light_form_divider_protected.png": mild_fold_page("light_form_divider"),
                "synthetic_vertical_fold_dense_foreground.png": mild_fold_page("dense_foreground"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "mild-fold-shadow", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_fold_shadows=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_safe_mild_vertical_fold_sparse_text.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["fold_shadows_lightened"])
            self.assertEqual(safe_record["fold_shadows_reason_code"], "applied_narrow_neutral_background_band")
            self.assertEqual(safe_record["fold_shadows_orientation"], "vertical")
            self.assertGreater(safe_record["fold_shadows_delta"], 1.5)
            self.assertGreater(safe_record["fold_shadows_changed_pixel_ratio"], 0.01)
            self.assertLessEqual(safe_record["fold_shadows_changed_pixel_ratio"], 0.075)
            self.assertLessEqual(safe_record["fold_shadows_candidate_pixel_ratio"], 0.12)
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                self.assertGreater(
                    _mean_luma(output, (152, 12, 169, 228)),
                    _mean_luma(pages[safe_name], (152, 12, 169, 228)) + 1.0,
                )
                self.assertLess(_changed_ratio(pages[safe_name], output, (72, 66, 248, 126)), 0.01)

            protected_names = set(pages) - {safe_name}
            for name in protected_names:
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertFalse(record["fold_shadows_lightened"], name)
                self.assertNotEqual(record["fold_shadows_reason_code"], "applied_narrow_neutral_background_band")
                self.assertEqual(record["fold_shadows_changed_pixel_ratio"], 0.0, name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.convert("RGB").tobytes(), pages[name].tobytes(), name)
            self.assertEqual(
                records["synthetic_vertical_fold_light_form_divider_protected.png"]["fold_shadows_reason_code"],
                "ruled_content_intersects_candidate_fold_band",
            )

            fold_guard = audit_summary["guardrails"]["fold_shadows"]
            self.assertEqual(audit_summary["counts"]["fold_shadows_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["fold_shadows_skipped_files"], len(protected_names))
            self.assertEqual(fold_guard["applied_files"], 1)
            self.assertEqual(fold_guard["skipped_files"], len(protected_names))
            self.assertEqual(fold_guard["reason_code_distribution"]["applied_narrow_neutral_background_band"], 1)
            self.assertEqual(
                fold_guard["skip_reason_code_distribution"]["ruled_content_intersects_candidate_fold_band"],
                1,
            )
            self.assertIn("changed_pixel_ratio", fold_guard)
            self.assertIn("candidate_pixel_ratio", fold_guard)
            self.assertIn("candidate_width_bucket_distribution", fold_guard)
            self.assertIn("candidate_coverage_bucket_distribution", fold_guard)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_mild_horizontal_center_fold_shadow_cleanup_keeps_content_guards(self) -> None:
        def mild_horizontal_fold_page(variant: str) -> Image.Image:
            image = Image.new("RGB", (320, 240), (244, 244, 240))
            draw = ImageDraw.Draw(image)
            for y in range(116, 125):
                distance = abs(y - 120) / 4
                shade = int(round(244 - 8 * (1 - distance) ** 1.2))
                draw.line((85, y, 235, y), fill=(shade, shade, shade - 4))
            for y in (70, 170):
                draw.rectangle((72, y, 124, y + 2), fill=(50, 50, 50))
                draw.rectangle((180, y, 248, y + 2), fill=(50, 50, 50))
            if variant == "safe":
                return image
            if variant == "light_rule":
                draw.line((78, 120, 242, 120), fill=(202, 202, 198), width=1)
                return image
            if variant == "table_rows":
                for y in (112, 120, 128):
                    draw.line((66, y, 254, y), fill=(42, 42, 42), width=1)
                for x in (66, 160, 254):
                    draw.line((x, 108, x, 132), fill=(42, 42, 42), width=1)
                return image
            if variant == "handwriting_like":
                draw.arc((110, 106, 155, 134), 190, 340, fill=(55, 55, 55), width=2)
                draw.arc((150, 106, 210, 134), 200, 350, fill=(55, 55, 55), width=2)
                return image
            if variant == "dense_foreground":
                for y in range(36, 206, 10):
                    draw.rectangle((50, y, 270, y + 3), fill=(45, 45, 45))
                return image
            raise ValueError(f"unsupported variant: {variant}")

        with tempfile.TemporaryDirectory(prefix="scan-processing-mild-horizontal-fold-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_mild_horizontal_center_fold_sparse_text.png": mild_horizontal_fold_page("safe"),
                "synthetic_horizontal_fold_light_rule_protected.png": mild_horizontal_fold_page("light_rule"),
                "synthetic_horizontal_fold_table_rows_protected.png": mild_horizontal_fold_page("table_rows"),
                "synthetic_horizontal_fold_handwriting_like_protected.png": mild_horizontal_fold_page(
                    "handwriting_like"
                ),
                "synthetic_horizontal_fold_dense_foreground.png": mild_horizontal_fold_page("dense_foreground"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "mild-horizontal-fold", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_fold_shadows=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_safe_mild_horizontal_center_fold_sparse_text.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["fold_shadows_lightened"])
            self.assertEqual(safe_record["fold_shadows_reason_code"], "applied_narrow_neutral_background_band")
            self.assertEqual(safe_record["fold_shadows_orientation"], "horizontal")
            self.assertGreater(safe_record["fold_shadows_delta"], 1.5)
            self.assertGreater(safe_record["fold_shadows_changed_pixel_ratio"], 0.005)
            self.assertLessEqual(safe_record["fold_shadows_changed_pixel_ratio"], 0.075)
            self.assertLessEqual(safe_record["fold_shadows_candidate_pixel_ratio"], 0.12)
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                self.assertGreater(
                    _mean_luma(output, (85, 116, 235, 125)),
                    _mean_luma(pages[safe_name], (85, 116, 235, 125)) + 1.0,
                )
                self.assertLess(_changed_ratio(pages[safe_name], output, (72, 66, 248, 176)), 0.02)

            protected_names = set(pages) - {safe_name}
            for name in protected_names:
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertFalse(record["fold_shadows_lightened"], name)
                self.assertNotEqual(record["fold_shadows_reason_code"], "applied_narrow_neutral_background_band")
                self.assertEqual(record["fold_shadows_changed_pixel_ratio"], 0.0, name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.convert("RGB").tobytes(), pages[name].tobytes(), name)

            fold_guard = audit_summary["guardrails"]["fold_shadows"]
            self.assertEqual(audit_summary["counts"]["fold_shadows_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["fold_shadows_skipped_files"], len(protected_names))
            self.assertEqual(fold_guard["applied_files"], 1)
            self.assertEqual(fold_guard["skipped_files"], len(protected_names))
            self.assertEqual(fold_guard["orientation_distribution"]["horizontal"], 1)
            self.assertEqual(fold_guard["reason_code_distribution"]["applied_narrow_neutral_background_band"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_illumination_gradient_levels_safe_public_gradient_and_preserves_content_edges(self) -> None:
        def gradient_page(variant: str) -> Image.Image:
            width, height = 260, 180
            page = Image.new("RGB", (width, height))
            pixels = page.load()
            for y in range(height):
                for x in range(width):
                    value = int(round(243 - 14 * (x / (width - 1)) - 2 * (y / (height - 1))))
                    pixels[x, y] = (value, value, value)
            draw = ImageDraw.Draw(page)
            if variant == "safe":
                return page
            if variant == "marginal_note":
                draw.line((8, 40, 32, 50, 12, 64, 38, 72), fill=(45, 45, 45), width=2)
            elif variant == "punctuation_i_dots":
                draw.ellipse((128, 82, 130, 84), fill=(30, 30, 30))
                draw.rectangle((139, 83, 141, 88), fill=(30, 30, 30))
            elif variant == "page_number":
                draw.rectangle((123, 160, 137, 166), fill=(35, 35, 35))
            elif variant == "ruled_table":
                for row in (48, 82, 116):
                    draw.line((42, row, 220, row), fill=(45, 45, 45), width=2)
                for column in (86, 154):
                    draw.line((column, 38, column, 126), fill=(45, 45, 45), width=2)
            elif variant == "color_stamp":
                draw.ellipse((96, 54, 164, 112), outline=(180, 25, 25), width=4)
            elif variant == "fold_boundary":
                draw.line((130, 12, 130, 168), fill=(148, 148, 144), width=1)
            elif variant == "archival_mark":
                draw.rectangle((226, 132, 244, 154), fill=(48, 48, 48))
            else:
                raise ValueError(f"unsupported variant: {variant}")
            return page

        with tempfile.TemporaryDirectory(prefix="scan-processing-illumination-gradient-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_broad_illumination_gradient.png": gradient_page("safe"),
                "synthetic_illumination_marginal_note.png": gradient_page("marginal_note"),
                "synthetic_illumination_punctuation_i_dots.png": gradient_page("punctuation_i_dots"),
                "synthetic_illumination_page_number.png": gradient_page("page_number"),
                "synthetic_illumination_ruled_table.png": gradient_page("ruled_table"),
                "synthetic_illumination_color_stamp.png": gradient_page("color_stamp"),
                "synthetic_illumination_fold_boundary.png": gradient_page("fold_boundary"),
                "synthetic_illumination_archival_mark.png": gradient_page("archival_mark"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "illumination-gradient", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(level_illumination_gradient=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_safe_broad_illumination_gradient.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["illumination_gradient_levelled"])
            self.assertEqual(safe_record["illumination_gradient_reason_code"], "applied")
            self.assertIn(safe_record["illumination_gradient_orientation"], {"vertical", "diagonal_tl_br"})
            self.assertGreater(safe_record["illumination_gradient_delta_before"], 10.0)
            self.assertLess(
                safe_record["illumination_gradient_delta_after"],
                safe_record["illumination_gradient_delta_before"],
            )
            self.assertGreater(safe_record["illumination_gradient_changed_pixel_ratio"], 0.05)
            self.assertLessEqual(safe_record["illumination_gradient_changed_pixel_ratio"], 0.995)
            self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [])
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                before_delta = abs(
                    _mean_luma(pages[safe_name], (0, 0, 32, 180))
                    - _mean_luma(pages[safe_name], (228, 0, 260, 180))
                )
                after_delta = abs(_mean_luma(output, (0, 0, 32, 180)) - _mean_luma(output, (228, 0, 260, 180)))
                self.assertLess(after_delta, before_delta - 3.0)

            protected_names = set(pages) - {safe_name}
            for name in protected_names:
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertFalse(record["illumination_gradient_levelled"], name)
                self.assertIn(
                    record["illumination_gradient_reason_code"],
                    {"protected_content", "not_uniform", "low_confidence"},
                    name,
                )
                self.assertEqual(record["illumination_gradient_changed_pixel_ratio"], 0.0, name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.convert("RGB").tobytes(), pages[name].tobytes(), name)

            illumination_summary = audit_summary["guardrails"]["illumination_gradient"]
            self.assertEqual(audit_summary["counts"]["illumination_gradient_levelled_files"], 1)
            self.assertEqual(audit_summary["counts"]["illumination_gradient_skipped_files"], len(protected_names))
            self.assertEqual(illumination_summary["applied_files"], 1)
            self.assertEqual(illumination_summary["skipped_files"], len(protected_names))
            self.assertEqual(illumination_summary["reason_code_distribution"]["applied"], 1)
            self.assertEqual(
                sum(illumination_summary["skip_reason_code_distribution"].values()),
                len(protected_names),
            )
            self.assertGreaterEqual(illumination_summary["protection_triggered_files"], 5)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_illumination_gradient_levels_safe_public_mild_two_edge_falloff(self) -> None:
        def two_edge_page(variant: str) -> Image.Image:
            width, height = 220, 150
            page = Image.new("RGB", (width, height))
            pixels = page.load()
            for y in range(height):
                for x in range(width):
                    position = x / (width - 1)
                    edge_weight = abs(position - 0.5) * 2
                    value = int(round(242 - 5 * edge_weight))
                    pixels[x, y] = (value, value, value)
            draw = ImageDraw.Draw(page)
            if variant == "safe":
                font = ImageFont.load_default()
                for offset, text in enumerate(("ARCHIVE PAGE", "REFERENCE COPY", "INDEX 42")):
                    draw.text((74, 54 + offset * 18), text, fill=(45, 45, 45), font=font)
                return page
            if variant == "table_grid":
                for row in (42, 72, 102):
                    draw.line((36, row, 184, row), fill=(45, 45, 45), width=2)
                for column in (72, 118, 164):
                    draw.line((column, 32, column, 118), fill=(45, 45, 45), width=2)
            elif variant == "sparse_ruled_segments":
                for row in (42, 62, 82):
                    for x in range(54, 160, 20):
                        draw.rectangle((x, row, x + 8, row + 2), fill=(45, 45, 45))
            elif variant == "stamp_color_mark":
                draw.ellipse((82, 42, 138, 98), outline=(180, 25, 25), width=4)
            elif variant == "handwriting_sparse_strokes":
                for y_offset in (0, 22):
                    points = (
                        (56, 54 + y_offset),
                        (68, 48 + y_offset),
                        (82, 56 + y_offset),
                        (96, 49 + y_offset),
                        (110, 58 + y_offset),
                        (126, 50 + y_offset),
                        (140, 57 + y_offset),
                    )
                    for start, end in zip(points, points[1:]):
                        draw.line((*start, *end), fill=(45, 45, 45), width=1)
            elif variant == "edge_page_number":
                draw.rectangle((101, 132, 119, 139), fill=(35, 35, 35))
                draw.rectangle((0, 56, 16, 92), fill=(35, 35, 35))
            elif variant == "dense_low_contrast_foreground":
                for row in range(30, 123, 7):
                    draw.rectangle((30, row, 190, row + 2), fill=(210, 210, 210))
            elif variant == "photo_texture":
                for y in range(24, 126):
                    for x in range(42, 178):
                        texture = 186 + ((x * 11 + y * 7 + (x // 5) * (y // 3)) % 42)
                        pixels[x, y] = (texture, texture - 5, texture - 9)
            elif variant == "broad_stain":
                draw.ellipse((36, 24, 186, 132), fill=(218, 216, 208))
                draw.ellipse((56, 42, 166, 116), fill=(222, 220, 212))
            elif variant == "archival_corner_mark":
                draw.rectangle((6, 8, 30, 29), fill=(54, 50, 45))
                draw.line((0, 38, 28, 58), fill=(64, 60, 52), width=3)
            else:
                raise ValueError(f"unsupported variant: {variant}")
            return page

        with tempfile.TemporaryDirectory(prefix="scan-processing-mild-two-edge-illumination-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_mild_two_edge_illumination.png": two_edge_page("safe"),
                "synthetic_mild_two_edge_table_grid.png": two_edge_page("table_grid"),
                "synthetic_mild_two_edge_sparse_ruled_segments.png": two_edge_page("sparse_ruled_segments"),
                "synthetic_mild_two_edge_stamp_color_mark.png": two_edge_page("stamp_color_mark"),
                "synthetic_mild_two_edge_handwriting_sparse_strokes.png": two_edge_page(
                    "handwriting_sparse_strokes"
                ),
                "synthetic_mild_two_edge_edge_page_number.png": two_edge_page("edge_page_number"),
                "synthetic_mild_two_edge_dense_low_contrast_foreground.png": two_edge_page(
                    "dense_low_contrast_foreground"
                ),
                "synthetic_mild_two_edge_photo_texture.png": two_edge_page("photo_texture"),
                "synthetic_mild_two_edge_broad_stain.png": two_edge_page("broad_stain"),
                "synthetic_mild_two_edge_archival_corner_mark.png": two_edge_page("archival_corner_mark"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "mild-two-edge-illumination", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(level_illumination_gradient=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_safe_mild_two_edge_illumination.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["illumination_gradient_levelled"])
            self.assertEqual(safe_record["illumination_gradient_reason_code"], "applied")
            self.assertEqual(safe_record["illumination_gradient_orientation"], "vertical")
            self.assertGreaterEqual(safe_record["illumination_gradient_delta_before"], 3.5)
            self.assertLess(safe_record["illumination_gradient_delta_after"], safe_record["illumination_gradient_delta_before"])
            self.assertLessEqual(safe_record["illumination_gradient_correction_delta"], 4.0)
            self.assertGreater(safe_record["illumination_gradient_changed_pixel_ratio"], 0.50)
            self.assertLessEqual(safe_record["illumination_gradient_changed_pixel_ratio"], 0.95)
            self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [])
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                original_edge = _mean_luma(pages[safe_name], (0, 0, 24, 150))
                original_center = _mean_luma(pages[safe_name], (98, 0, 122, 150))
                processed_edge = _mean_luma(output, (0, 0, 24, 150))
                processed_center = _mean_luma(output, (98, 0, 122, 150))
                self.assertLess(processed_center - processed_edge, original_center - original_edge - 1.0)

            protected_names = set(pages) - {safe_name}
            for name in protected_names:
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertFalse(record["illumination_gradient_levelled"], name)
                self.assertIn(
                    record["illumination_gradient_reason_code"],
                    {"protected_content", "not_uniform", "low_confidence"},
                    name,
                )
                self.assertEqual(record["illumination_gradient_changed_pixel_ratio"], 0.0, name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.convert("RGB").tobytes(), pages[name].tobytes(), name)

            illumination_summary = audit_summary["guardrails"]["illumination_gradient"]
            self.assertEqual(audit_summary["counts"]["illumination_gradient_levelled_files"], 1)
            self.assertEqual(audit_summary["counts"]["illumination_gradient_skipped_files"], len(protected_names))
            self.assertEqual(illumination_summary["applied_files"], 1)
            self.assertEqual(illumination_summary["skipped_files"], len(protected_names))
            self.assertEqual(illumination_summary["reason_code_distribution"]["applied"], 1)
            self.assertEqual(
                sum(illumination_summary["skip_reason_code_distribution"].values()),
                len(protected_names),
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_synthetic_combinations_emit_aggregate_quality_and_performance_regression_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-algorithm-regression-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _synthetic_pages(input_dir)

            default_payload = _benchmark_combo(root, input_dir, "default")
            base_payload = _benchmark_combo(root, input_dir, "base", *BASE_FLAGS)
            full_payload = _benchmark_combo(root, input_dir, "full", *(BASE_FLAGS + CONSERVATIVE_REPAIR_FLAGS))

            default_quality = _single_quality(default_payload)
            base_run = base_payload["runs"][0]
            full_run = full_payload["runs"][0]
            base_quality = _single_quality(base_payload)
            full_quality = _single_quality(full_payload)

            for quality in (base_quality, full_quality):
                self.assertEqual(quality["status"], "pass")
                self.assertEqual(quality["counts"]["failed_files"], 0)
                self.assertEqual(quality["counts"]["guardrail_failed_files"], 0)
                self.assertEqual(quality["counts"]["cumulative_change_guard_reverted_files"], 0)
                self.assertEqual(quality["counts"]["processed_output_safety_guard_reverted_files"], 0)
                self.assertTrue(quality["aggregate_only"])
                self.assertTrue(quality["privacy"]["aggregate_only"])
                self.assertTrue(quality["processed_output_safety_guard"]["aggregate_only"])
                for operation in REQUIRED_OPERATIONS:
                    self.assertIn(operation, quality["algorithm_metrics"])
                    self.assertIn("metrics", quality["algorithm_metrics"][operation])

            self.assertEqual(default_quality["status"], "pass")
            self.assertEqual(default_quality["counts"]["enhancement_changed_files"], 0)
            self.assertEqual(default_quality["counts"]["cumulative_change_guard_checked_files"], 6)
            self.assertEqual(default_quality["counts"]["processed_output_safety_guard_checked_files"], 6)
            for operation in CONSERVATIVE_REPAIR_OPERATIONS:
                self.assertFalse(default_quality["algorithm_metrics"][operation]["enabled"], operation)
                self.assertEqual(default_quality["algorithm_metrics"][operation]["changed_files"], 0, operation)

            _assert_algorithm_thresholds(self, base_quality)
            _assert_algorithm_thresholds(self, full_quality)
            _assert_required_metrics_present(self, full_quality)
            _assert_operation_timing_signal(self, full_quality)
            _assert_local_content_guard_signal(self, full_quality)
            _assert_full_chain_quality_guard_signal(self, full_payload)

            self.assertGreater(base_run["processing"]["processed_files_per_minute"], 0)
            self.assertGreater(full_run["processing"]["processed_files_per_minute"], 0)
            throughput_ratio = round(
                full_run["processing"]["processed_files_per_minute"]
                / base_run["processing"]["processed_files_per_minute"],
                6,
            )
            self.assertGreater(throughput_ratio, 0)
            self.assertGreaterEqual(throughput_ratio, 0.01)
            for operation in REQUIRED_OPERATIONS:
                self.assertIn(operation, full_run["processing"]["operation_timings"])
                self.assertIn("files_per_minute", full_run["processing"]["operation_timings"][operation])

            for payload in (default_payload, base_payload, full_payload):
                raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                self.assertIn("quality_regression", raw)
                for forbidden in (
                    "private_default_page.png",
                    "private_edge_page.png",
                    "private_stain_page.png",
                    "private_scanline_page.png",
                    "private_faded_text_page.png",
                    "private_blurred_text_page.png",
                    str(input_dir),
                    "source_relative_path",
                    "source_sha256",
                    "OCR TEXT",
                    '"files": [',
                    '"findings": [',
                ):
                    self.assertNotIn(forbidden, raw)

    def test_full_chain_safe_combination_page_stays_conservative(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-full-chain-safe-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "synthetic_safe_combination.png"
            _safe_full_chain_combination_page().save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "safe-combination", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            with Image.open(process_dir / record["output_relative_path"]) as output:
                self.assertEqual(output.size, tuple(record["output_size"]))
            self.assertTrue(record["despeckled"])
            self.assertTrue(record["scanlines_lightened"])
            self.assertIn("despeckle_isolated_pixels", record["operations"])
            self.assertIn("lighten_scanlines_conservative", record["operations"])
            self.assertFalse(record["background_stains_lightened"])
            self.assertFalse(record["faded_text_enhanced"])
            self.assertFalse(record["text_edges_sharpened"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertFalse(audit["local_content_change_guard_reverted"])
            self.assertFalse(audit["cumulative_change_guard_reverted"])
            self.assertFalse(audit["combination_quality_guard_reverted"])
            self.assertGreater(audit["scanlines_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["scanlines_changed_pixel_ratio"], 0.04)
            self.assertLessEqual(audit["despeckle_pixel_ratio"], 0.001)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.05)
            self.assertLessEqual(audit["cumulative_change_score"], 0.25)
            self.assertLessEqual(audit["local_content_changed_ratio"], 0.02)
            despeckle_timing = audit_summary["timing"]["operation_timings"]["despeckle"]
            self.assertIn("component_count_bucket_distribution", despeckle_timing)
            self.assertIn("max_component_size_bucket_distribution", despeckle_timing)
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["counts"]["local_content_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["counts"]["combination_quality_guard_reverted_files"], 0)
            self.assertEqual(
                audit_summary["guardrails"]["combination_quality_guard"]["reason_code_distribution"][
                    "safe_combination_passed"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("synthetic_safe_combination.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_subtle_diagonal_edge_shadow_cleanup_preserves_protected_edges(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-diagonal-edge-shadow-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_safe_diagonal_edge_shadow.png": _subtle_diagonal_edge_shadow_page(),
                "private_diagonal_edge_handwriting.png": _subtle_diagonal_edge_shadow_page("edge_handwriting"),
                "private_diagonal_page_number.png": _subtle_diagonal_edge_shadow_page("page_number"),
                "private_diagonal_ruled_table.png": _subtle_diagonal_edge_shadow_page("ruled_table"),
                "private_diagonal_stamp.png": _subtle_diagonal_edge_shadow_page("stamp"),
                "private_diagonal_texture.png": _subtle_diagonal_edge_shadow_page("texture"),
                "private_diagonal_archival_edge_mark.png": _subtle_diagonal_edge_shadow_page("archival_edge_mark"),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "diagonal-edge-shadow", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(lighten_edge_shadow=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_record = records["private_safe_diagonal_edge_shadow.png"]
            with Image.open(process_dir / safe_record["output_relative_path"]) as safe_output:
                processed_safe = safe_output.convert("RGB")
            self.assertTrue(safe_record["edge_shadow_lightened"])
            self.assertEqual(safe_record["edge_shadow_reason_code"], "applied_narrow_neutral_edge_shadow")
            self.assertEqual(safe_record["edge_shadow_edges"], ["left"])
            self.assertGreater(
                _mean_luma(processed_safe, (0, 0, 24, 180)),
                _mean_luma(pages["private_safe_diagonal_edge_shadow.png"], (0, 0, 24, 180)) + 2.5,
            )
            self.assertLess(_changed_ratio(pages["private_safe_diagonal_edge_shadow.png"], processed_safe, (58, 34, 200, 110)), 0.002)
            self.assertGreater(safe_record["edge_shadow_candidate_pixel_ratio"], 0.04)
            self.assertLessEqual(safe_record["edge_shadow_changed_pixel_ratio"], 0.08)
            self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [])

            protected_names = set(pages) - {"private_safe_diagonal_edge_shadow.png"}
            for name in protected_names:
                record = records[name]
                with Image.open(process_dir / record["output_relative_path"]) as protected_output:
                    processed = protected_output.convert("RGB")
                self.assertFalse(record["edge_shadow_lightened"], name)
                self.assertIn("lighten_edge_shadow_noop", record["operations"], name)
                self.assertEqual(record["processing_audit"]["edge_shadow_changed_pixel_ratio"], 0.0, name)
                self.assertLess(_changed_ratio(pages[name], processed, (0, 0, processed.width, processed.height)), 0.001, name)

            edge_guard = audit_summary["guardrails"]["edge_shadow"]
            self.assertEqual(audit_summary["counts"]["edge_shadow_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["edge_shadow_skipped_files"], len(protected_names))
            self.assertEqual(edge_guard["applied_files"], 1)
            self.assertEqual(edge_guard["skipped_files"], len(protected_names))
            self.assertGreaterEqual(edge_guard["protection_triggered_files"], 3)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_diagonal_fold_shadow_cleanup_lightens_safe_sparse_text_case_and_preserves_protected_marks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-diagonal-fold-shadow-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_safe_diagonal_fold_shadow.png": _subtle_diagonal_fold_shadow_page(),
                "private_safe_diagonal_fold_sparse_text_crossing.png": _subtle_diagonal_fold_shadow_page(
                    "sparse_text_crossing"
                ),
                "private_diagonal_fold_handwriting_bridge.png": _subtle_diagonal_fold_shadow_page(
                    "handwriting_bridge"
                ),
                "private_diagonal_fold_repeated_ruled_segments.png": _subtle_diagonal_fold_shadow_page(
                    "repeated_ruled_segments"
                ),
                "private_diagonal_fold_dense_typed_text.png": _subtle_diagonal_fold_shadow_page("dense_typed_text"),
                "private_diagonal_fold_handwriting.png": _subtle_diagonal_fold_shadow_page("handwriting"),
                "private_diagonal_fold_page_number.png": _subtle_diagonal_fold_shadow_page("page_number"),
                "private_diagonal_fold_ruled_table.png": _subtle_diagonal_fold_shadow_page("ruled_table"),
                "private_diagonal_fold_stamp.png": _subtle_diagonal_fold_shadow_page("stamp"),
                "private_diagonal_fold_texture.png": _subtle_diagonal_fold_shadow_page("texture"),
                "private_diagonal_fold_archival_edge_mark.png": _subtle_diagonal_fold_shadow_page(
                    "archival_edge_mark"
                ),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "diagonal-fold-shadow", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_fold_shadows=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_names = {
                "private_safe_diagonal_fold_shadow.png",
                "private_safe_diagonal_fold_sparse_text_crossing.png",
            }
            for safe_name in safe_names:
                safe_record = records[safe_name]
                with Image.open(process_dir / safe_record["output_relative_path"]) as safe_output:
                    processed_safe = safe_output.convert("RGB")
                self.assertTrue(safe_record["fold_shadows_lightened"], safe_name)
                self.assertEqual(
                    safe_record["fold_shadows_reason_code"],
                    "applied_narrow_neutral_background_band",
                    safe_name,
                )
                self.assertEqual(safe_record["fold_shadows_orientation"], "diagonal_tl_br", safe_name)
                self.assertEqual(safe_record["fold_shadows_count"], 1, safe_name)
                self.assertGreaterEqual(safe_record["fold_shadows_delta"], 3.0, safe_name)
                self.assertGreater(safe_record["fold_shadows_candidate_pixel_ratio"], 0.002, safe_name)
                self.assertLessEqual(safe_record["fold_shadows_candidate_pixel_ratio"], 0.12, safe_name)
                self.assertGreater(safe_record["fold_shadows_changed_pixel_ratio"], 0.002, safe_name)
                self.assertLessEqual(safe_record["fold_shadows_changed_pixel_ratio"], 0.075, safe_name)
                self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [], safe_name)
                self.assertGreater(
                    processed_safe.convert("L").getpixel((110, 60)),
                    pages[safe_name].convert("L").getpixel((110, 60)),
                    safe_name,
                )
                self.assertEqual(
                    processed_safe.convert("L").getpixel((40, 122)),
                    pages[safe_name].convert("L").getpixel((40, 122)),
                    safe_name,
                )
            sparse_safe = "private_safe_diagonal_fold_sparse_text_crossing.png"
            with Image.open(process_dir / records[sparse_safe]["output_relative_path"]) as sparse_output:
                self.assertEqual(
                    sparse_output.convert("L").getpixel((120, 80)),
                    pages[sparse_safe].convert("L").getpixel((120, 80)),
                )

            protected_names = set(pages) - safe_names
            for name in protected_names:
                record = records[name]
                with Image.open(process_dir / record["output_relative_path"]) as protected_output:
                    processed = protected_output.convert("RGB")
                self.assertFalse(record["fold_shadows_lightened"], name)
                self.assertIn("lighten_fold_shadows_noop", record["operations"], name)
                self.assertEqual(record["processing_audit"]["fold_shadows_changed_pixel_ratio"], 0.0, name)
                self.assertEqual(processed.tobytes(), pages[name].tobytes(), name)

            fold_guard = audit_summary["guardrails"]["fold_shadows"]
            self.assertEqual(audit_summary["counts"]["fold_shadows_lightened_files"], len(safe_names))
            self.assertEqual(audit_summary["counts"]["fold_shadows_skipped_files"], len(protected_names))
            self.assertEqual(fold_guard["applied_files"], len(safe_names))
            self.assertEqual(fold_guard["skipped_files"], len(protected_names))
            self.assertEqual(
                fold_guard["reason_code_distribution"]["applied_narrow_neutral_background_band"],
                len(safe_names),
            )
            self.assertIn("no_confident_narrow_background_fold_band", fold_guard["skip_reason_code_distribution"])
            self.assertIn("color_content_stamp_or_annotation_risk", fold_guard["skip_reason_code_distribution"])
            self.assertIn("edge_adjacent_content_or_binding_risk", fold_guard["skip_reason_code_distribution"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_despeckle_cleans_compact_dust_clusters_but_preserves_content_marks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-despeckle-clusters-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_cluster.png": _safe_compact_dust_cluster_page(),
                "synthetic_protected_table_annotation.png": _risk_table_page_number_annotation_page(),
                "synthetic_protected_stamp.png": _risk_stamp_header_footer_page(),
                "synthetic_protected_edge_mark.png": _risk_edge_content_mark_page(),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "despeckle-clusters", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_record = records["synthetic_safe_cluster.png"]
            safe_audit = safe_record["processing_audit"]
            self.assertEqual((input_dir / "synthetic_safe_cluster.png").read_bytes(), source_bytes["synthetic_safe_cluster.png"])
            self.assertEqual(safe_record["status"], "processed")
            self.assertTrue(safe_record["despeckled"])
            self.assertEqual(safe_record["despeckle_pixels_changed"], 6)
            self.assertIn("despeckle_isolated_pixels", safe_record["operations"])
            self.assertEqual(safe_audit["guardrail_failures"], [])
            self.assertEqual(safe_audit["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_audit["processed_output_safety_guard_action"], "passed")
            self.assertEqual(safe_audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertLessEqual(safe_audit["despeckle_pixel_ratio"], 0.001)

            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                for point in _safe_compact_dust_cluster_points():
                    self.assertGreaterEqual(output.convert("L").getpixel(point), 240)

            for name in (
                "synthetic_protected_table_annotation.png",
                "synthetic_protected_stamp.png",
                "synthetic_protected_edge_mark.png",
            ):
                record = records[name]
                audit = record["processing_audit"]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["despeckled"], name)
                self.assertEqual(record["despeckle_pixels_changed"], 0, name)
                self.assertIn("despeckle_noop", record["operations"], name)
                self.assertEqual(audit["guardrail_failures"], [], name)
                self.assertEqual(audit["local_content_change_guard_action"], "passed", name)
                self.assertEqual(audit["cumulative_change_guard_action"], "passed", name)
                self.assertEqual(audit["processed_output_safety_guard_action"], "passed", name)
                self.assertEqual(audit["despeckle_pixel_ratio"], 0.0, name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertIsNone(ImageChops.difference(pages[name], output.convert("RGB")).getbbox(), name)

            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["pixels_changed"], 6)
            self.assertEqual(
                audit_summary["timing"]["operation_timings"]["despeckle"]["reason_code_distribution"][
                    "applied_isolated_pixels"
                ],
                1,
            )
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_code_distribution"][
                    "protected_edge_dark_marks"
                ],
                1,
            )
            self.assertGreaterEqual(
                audit_summary["timing"]["operation_timings"]["despeckle"]["max_component_size"]["max"],
                4,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_despeckle_cleans_scattered_pale_dust_but_preserves_tiny_real_marks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-despeckle-scattered-dust-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            protected_pages = _protected_sparse_bleed_through_mark_pages()
            pages = {
                "synthetic_scattered_pale_dust.png": _safe_scattered_pale_dust_page(),
                **protected_pages,
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "despeckle-scattered-dust", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_record = records["synthetic_scattered_pale_dust.png"]
            safe_audit = safe_record["processing_audit"]
            self.assertEqual(
                (input_dir / "synthetic_scattered_pale_dust.png").read_bytes(),
                source_bytes["synthetic_scattered_pale_dust.png"],
            )
            self.assertEqual(safe_record["status"], "processed")
            self.assertTrue(safe_record["despeckled"])
            self.assertEqual(safe_record["despeckle_pixels_changed"], len(_safe_scattered_pale_dust_points()))
            self.assertIn("despeckle_isolated_pixels", safe_record["operations"])
            self.assertEqual(safe_audit["guardrail_failures"], [])
            self.assertLessEqual(safe_audit["despeckle_pixel_ratio"], 0.001)
            self.assertEqual(safe_audit["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(safe_audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed")
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                output_luma = output.convert("L")
                for point in _safe_scattered_pale_dust_points():
                    self.assertGreaterEqual(output_luma.getpixel(point), 240)

            for name, source_image in protected_pages.items():
                record = records[name]
                audit = record["processing_audit"]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["despeckled"], name)
                self.assertEqual(record["despeckle_pixels_changed"], 0, name)
                self.assertIn("despeckle_noop", record["operations"], name)
                self.assertEqual(audit["guardrail_failures"], [], name)
                self.assertEqual(audit["despeckle_pixel_ratio"], 0.0, name)
                self.assertEqual(audit["local_content_change_guard_action"], "passed", name)
                self.assertEqual(audit["cumulative_change_guard_action"], "passed", name)
                self.assertEqual(audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed", name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertIsNone(ImageChops.difference(source_image, output.convert("RGB")).getbbox(), name)

            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["pixels_changed"],
                len(_safe_scattered_pale_dust_points()),
            )
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_code_distribution"]["applied_isolated_pixels"],
                1,
            )
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_code_distribution"]["no_isolated_candidates"]
                + audit_summary["guardrails"]["despeckle"]["reason_code_distribution"].get(
                    "repeated_pale_micro_pattern_risk",
                    0,
                ),
                len(protected_pages),
            )
            self.assertEqual(
                audit_summary["timing"]["operation_timings"]["despeckle"]["reason_code_distribution"][
                    "applied_isolated_pixels"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_despeckle_cleans_compact_pale_dust_clusters_but_preserves_tiny_real_marks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-despeckle-pale-clusters-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            protected_pages = _protected_sparse_bleed_through_mark_pages()
            pages = {
                "synthetic_compact_pale_dust_clusters.png": _safe_compact_pale_dust_cluster_page(),
                **protected_pages,
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "despeckle-pale-clusters", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_compact_pale_dust_clusters.png"
            safe_record = records[safe_name]
            safe_audit = safe_record["processing_audit"]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertEqual(safe_record["status"], "processed")
            self.assertTrue(safe_record["despeckled"])
            self.assertEqual(safe_record["despeckle_pixels_changed"], len(_safe_compact_pale_dust_cluster_points()))
            self.assertIn("despeckle_isolated_pixels", safe_record["operations"])
            self.assertEqual(safe_audit["guardrail_failures"], [])
            self.assertLessEqual(safe_audit["despeckle_pixel_ratio"], 0.001)
            self.assertEqual(safe_audit["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(safe_audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed")
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                output_luma = output.convert("L")
                for point in _safe_compact_pale_dust_cluster_points():
                    self.assertGreaterEqual(output_luma.getpixel(point), 240)

            for name, source_image in protected_pages.items():
                record = records[name]
                audit = record["processing_audit"]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["despeckled"], name)
                self.assertEqual(record["despeckle_pixels_changed"], 0, name)
                self.assertIn("despeckle_noop", record["operations"], name)
                self.assertEqual(audit["guardrail_failures"], [], name)
                self.assertEqual(audit["despeckle_pixel_ratio"], 0.0, name)
                self.assertEqual(audit["local_content_change_guard_action"], "passed", name)
                self.assertEqual(audit["cumulative_change_guard_action"], "passed", name)
                self.assertEqual(audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed", name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertIsNone(ImageChops.difference(source_image, output.convert("RGB")).getbbox(), name)

            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["pixels_changed"],
                len(_safe_compact_pale_dust_cluster_points()),
            )
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_code_distribution"]["applied_isolated_pixels"],
                1,
            )
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_code_distribution"]["no_isolated_candidates"]
                + audit_summary["guardrails"]["despeckle"]["reason_code_distribution"].get(
                    "repeated_pale_micro_pattern_risk",
                    0,
                ),
                len(protected_pages),
            )
            self.assertEqual(
                audit_summary["timing"]["operation_timings"]["despeckle"]["max_component_size"]["max"],
                6,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_despeckle_cleans_clean_page_faint_dust_specks_but_preserves_marks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-despeckle-clean-faint-dust-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            protected_pages = _protected_clean_page_faint_mark_pages()
            pages = {
                "synthetic_clean_page_faint_dust_specks.png": _safe_clean_page_faint_dust_speck_page(),
                **protected_pages,
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "despeckle-clean-faint-dust", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_clean_page_faint_dust_specks.png"
            safe_record = records[safe_name]
            safe_audit = safe_record["processing_audit"]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertEqual(safe_record["status"], "processed")
            self.assertTrue(safe_record["despeckled"])
            self.assertEqual(safe_record["despeckle_pixels_changed"], len(_safe_clean_page_faint_dust_speck_points()))
            self.assertIn("despeckle_isolated_pixels", safe_record["operations"])
            self.assertEqual(safe_audit["guardrail_failures"], [])
            self.assertLessEqual(safe_audit["despeckle_pixel_ratio"], 0.001)
            self.assertEqual(safe_audit["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                output_luma = output.convert("L")
                for point in _safe_clean_page_faint_dust_speck_points():
                    self.assertGreaterEqual(output_luma.getpixel(point), 240)

            for name, source_image in protected_pages.items():
                record = records[name]
                audit = record["processing_audit"]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertEqual(record["status"], "processed", name)
                self.assertFalse(record["despeckled"], name)
                self.assertEqual(record["despeckle_pixels_changed"], 0, name)
                self.assertIn("despeckle_noop", record["operations"], name)
                self.assertEqual(audit["guardrail_failures"], [], name)
                self.assertEqual(audit["despeckle_pixel_ratio"], 0.0, name)
                self.assertEqual(audit["local_content_change_guard_action"], "passed", name)
                self.assertEqual(audit["cumulative_change_guard_action"], "passed", name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertIsNone(ImageChops.difference(source_image, output.convert("RGB")).getbbox(), name)

            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["pixels_changed"],
                len(_safe_clean_page_faint_dust_speck_points()),
            )
            self.assertEqual(
                audit_summary["guardrails"]["despeckle"]["reason_code_distribution"]["applied_isolated_pixels"],
                1,
            )
            self.assertEqual(
                audit_summary["timing"]["operation_timings"]["despeckle"]["reason_code_distribution"][
                    "applied_isolated_pixels"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_despeckle_pale_cluster_guards_preserve_noisy_texture_and_micro_marks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-despeckle-pale-guards-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            protected_pages = {
                "synthetic_noisy_pale_texture.png": _high_density_pale_texture_page(),
                "synthetic_dotted_leader_marks.png": _pale_dotted_leader_page(),
                "synthetic_punctuation_like_marks.png": _pale_punctuation_like_page(),
                "synthetic_halftone_low_contrast_texture.png": _low_contrast_halftone_texture_page(),
            }
            pages = {
                "synthetic_compact_pale_dust_clusters.png": _safe_compact_pale_dust_cluster_page(),
                **protected_pages,
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "despeckle-pale-guards", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(despeckle=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_compact_pale_dust_clusters.png"
            safe_record = records[safe_name]
            self.assertTrue(safe_record["despeckled"])
            self.assertEqual(safe_record["despeckle_pixels_changed"], len(_safe_compact_pale_dust_cluster_points()))
            with Image.open(process_dir / safe_record["output_relative_path"]) as output:
                output_luma = output.convert("L")
                for point in _safe_compact_pale_dust_cluster_points():
                    self.assertGreaterEqual(output_luma.getpixel(point), 240)

            for name, source_image in protected_pages.items():
                record = records[name]
                audit = record["processing_audit"]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertEqual(record["status"], "processed", name)
                self.assertFalse(record["despeckled"], name)
                self.assertEqual(record["despeckle_pixels_changed"], 0, name)
                self.assertIn("despeckle_noop", record["operations"], name)
                self.assertEqual(audit["guardrail_failures"], [], name)
                self.assertEqual(audit["despeckle_pixel_ratio"], 0.0, name)
                self.assertEqual(audit["local_content_change_guard_action"], "passed", name)
                self.assertEqual(audit["cumulative_change_guard_action"], "passed", name)
                self.assertEqual(audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed", name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertIsNone(ImageChops.difference(source_image, output.convert("RGB")).getbbox(), name)

            despeckle_timing = audit_summary["timing"]["operation_timings"]["despeckle"]
            reason_codes = despeckle_timing["reason_code_distribution"]
            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["despeckle"]["pixels_changed"], len(_safe_compact_pale_dust_cluster_points()))
            self.assertEqual(reason_codes["applied_isolated_pixels"], 1)
            self.assertGreaterEqual(reason_codes["repeated_pale_micro_pattern_risk"], 2)
            self.assertGreaterEqual(reason_codes["pale_candidate_density_exceeds_safety_threshold"], 1)
            self.assertEqual(despeckle_timing["replacement_work_files"], 1)
            self.assertLess(despeckle_timing["average_seconds_per_file"], 0.2)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_warm_mild_bleed_through_is_cleaned_without_private_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-warm-bleed-through-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_warm_reverse_ghost.png"
            page = _warm_mild_bleed_through_page()
            page.save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "warm-bleed-through", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(clean_bleed_through=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(record["bleed_through_cleaned"])
            self.assertEqual(record["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertEqual(audit["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertGreater(audit["bleed_through_delta"], 3.0)
            self.assertGreater(audit["bleed_through_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["bleed_through_changed_pixel_ratio"], 0.045)
            self.assertLessEqual(audit["bleed_through_candidate_pixel_ratio"], 0.065)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(audit["processed_output_safety_guard_action"], "passed")

            original = page.convert("RGB")
            ghost_box = (118, 80, 176, 122)
            before = ImageStat.Stat(original.crop(ghost_box).convert("L")).mean[0]
            after = ImageStat.Stat(processed.crop(ghost_box).convert("L")).mean[0]
            self.assertGreater(after - before, 0.08)
            protected_box = (30, 34, 72, 50)
            self.assertIsNone(
                ImageChops.difference(original.crop(protected_box), processed.crop(protected_box)).getbbox()
            )

            self.assertEqual(audit_summary["counts"]["bleed_through_cleaned_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["bleed_through"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["bleed_through"]["reason_code_distribution"][
                    "applied_faint_reverse_ghost"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("private_warm_reverse_ghost", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_open_sparse_faint_bleed_through_is_cleaned_without_private_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-open-sparse-bleed-through-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            default_process_dir = root / "processed-default"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_open_sparse_reverse_ghost.png"
            page = _open_sparse_faint_bleed_through_page()
            page.save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "open-sparse-bleed-through", input_dir, output_dir))
            default_manifest = process_images(report, input_dir, default_process_dir, ProcessingOptions(workers=1))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(clean_bleed_through=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            default_record = default_manifest["files"][0]
            record = manifest["files"][0]
            audit = record["processing_audit"]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse(default_record["bleed_through_cleaned"])
            self.assertIn("clean_bleed_through_disabled", default_record["operations"])
            self.assertTrue(record["bleed_through_cleaned"])
            self.assertEqual(record["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertEqual(audit["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertGreater(audit["bleed_through_delta"], 3.0)
            self.assertGreater(audit["bleed_through_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["bleed_through_changed_pixel_ratio"], 0.045)
            self.assertLessEqual(audit["bleed_through_candidate_pixel_ratio"], 0.065)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(audit["processed_output_safety_guard_action"], "passed")

            original = page.convert("RGB")
            ghost_box = (118, 78, 178, 120)
            before = ImageStat.Stat(original.crop(ghost_box).convert("L")).mean[0]
            after = ImageStat.Stat(processed.crop(ghost_box).convert("L")).mean[0]
            self.assertGreater(after - before, 0.02)

            self.assertEqual(audit_summary["counts"]["bleed_through_cleaned_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["bleed_through"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["bleed_through"]["reason_code_distribution"][
                    "applied_faint_reverse_ghost"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("private_open_sparse_reverse_ghost", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_pale_diffuse_bleed_through_cleanup_expands_safe_ghost_and_preserves_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-pale-diffuse-bleed-through-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_pale_diffuse_reverse_ghost.png": _pale_diffuse_bleed_through_page(),
                **_protected_sparse_bleed_through_mark_pages(),
            }
            source_bytes = {}
            for name, page in pages.items():
                source = input_dir / name
                page.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "pale-diffuse-bleed-through", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(clean_bleed_through=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            for name, original_bytes in source_bytes.items():
                self.assertEqual((input_dir / name).read_bytes(), original_bytes)

            safe_name = "synthetic_safe_pale_diffuse_reverse_ghost.png"
            safe_record = records[safe_name]
            safe_audit = safe_record["processing_audit"]
            safe_processed = Image.open(process_dir / safe_record["output_relative_path"]).convert("RGB")
            self.assertTrue(safe_record["bleed_through_cleaned"])
            self.assertEqual(safe_record["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertGreater(safe_audit["bleed_through_delta"], 3.0)
            self.assertGreater(safe_audit["bleed_through_changed_pixel_ratio"], 0.002)
            self.assertLessEqual(safe_audit["bleed_through_changed_pixel_ratio"], 0.045)
            self.assertLessEqual(safe_audit["bleed_through_candidate_pixel_ratio"], 0.065)
            self.assertEqual(safe_audit["guardrail_failures"], [])
            self.assertEqual(safe_audit["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(safe_audit["processed_output_safety_guard_action"], "passed")
            self.assertGreater(
                _mean_luma(safe_processed, (108, 66, 152, 130)),
                _mean_luma(pages[safe_name], (108, 66, 152, 130)) + 0.03,
            )
            self.assertIsNone(
                ImageChops.difference(
                    pages[safe_name].crop((30, 34, 72, 50)),
                    safe_processed.crop((30, 34, 72, 50)),
                ).getbbox()
            )

            for name in set(pages) - {safe_name}:
                record = records[name]
                audit = record["processing_audit"]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertFalse(record["bleed_through_cleaned"], name)
                self.assertNotEqual(record["bleed_through_reason_code"], "applied_faint_reverse_ghost", name)
                self.assertEqual(audit["bleed_through_changed_pixel_ratio"], 0.0, name)
                self.assertIsNone(ImageChops.difference(pages[name].convert("RGB"), processed).getbbox(), name)

            bleed_guard = audit_summary["guardrails"]["bleed_through"]
            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["bleed_through_cleaned_files"], 1)
            self.assertEqual(bleed_guard["applied_files"], 1)
            self.assertEqual(bleed_guard["skipped_files"], len(pages) - 1)
            self.assertEqual(bleed_guard["reason_code_distribution"]["applied_faint_reverse_ghost"], 1)
            self.assertIn("protected_line_or_annotation", bleed_guard["skip_reason_code_distribution"])
            self.assertIn("protected_edge_content", bleed_guard["skip_reason_code_distribution"])
            self.assertIn("protected_color_content", bleed_guard["skip_reason_code_distribution"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_cool_gray_mild_bleed_through_is_cleaned_without_private_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-cool-gray-bleed-through-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_cool_gray_reverse_ghost.png"
            page = _cool_gray_mild_bleed_through_page()
            page.save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "cool-gray-bleed-through", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(clean_bleed_through=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]
            processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(record["bleed_through_cleaned"])
            self.assertEqual(record["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertEqual(audit["bleed_through_reason_code"], "applied_faint_reverse_ghost")
            self.assertGreater(audit["bleed_through_delta"], 3.0)
            self.assertGreater(audit["bleed_through_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(audit["bleed_through_changed_pixel_ratio"], 0.045)
            self.assertLessEqual(audit["bleed_through_candidate_pixel_ratio"], 0.065)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(audit["processed_output_safety_guard_action"], "passed")

            original = page.convert("RGB")
            ghost_box = (118, 80, 176, 124)
            before = ImageStat.Stat(original.crop(ghost_box).convert("L")).mean[0]
            after = ImageStat.Stat(processed.crop(ghost_box).convert("L")).mean[0]
            self.assertGreater(after - before, 0.08)
            protected_box = (30, 34, 72, 50)
            self.assertIsNone(
                ImageChops.difference(original.crop(protected_box), processed.crop(protected_box)).getbbox()
            )

            self.assertEqual(audit_summary["counts"]["bleed_through_cleaned_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["bleed_through"]["applied_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["bleed_through"]["reason_code_distribution"][
                    "applied_faint_reverse_ghost"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("private_cool_gray_reverse_ghost", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_sparse_bleed_through_cleanup_preserves_real_marks_with_public_skip_codes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-protected-bleed-through-marks-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = _protected_sparse_bleed_through_mark_pages()
            for name, page in pages.items():
                page.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "protected-bleed-through-marks", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(clean_bleed_through=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}
            expected_reason_codes = {
                "A001_page_number.png": "protected_line_or_annotation",
                "A002_dotted_leaders.png": "protected_line_or_annotation",
                "A003_punctuation_i_dot.png": "protected_line_or_annotation",
                "A004_marginal_annotation.png": "protected_edge_content",
                "A005_color_stamp_mark.png": "protected_color_content",
                "A006_table_ruled_lines.png": "protected_line_or_annotation",
                "A007_archival_dirt_marks.png": "protected_texture_or_archival_trace",
            }

            self.assertEqual(set(records), set(expected_reason_codes))
            for name, expected_code in expected_reason_codes.items():
                record = records[name]
                audit = record["processing_audit"]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                original = pages[name].convert("RGB")

                self.assertFalse(record["bleed_through_cleaned"], name)
                self.assertEqual(record["bleed_through_reason_code"], expected_code, name)
                self.assertEqual(audit["bleed_through_reason_code"], expected_code, name)
                self.assertEqual(audit["bleed_through_changed_pixel_ratio"], 0.0, name)
                self.assertLessEqual(audit["bleed_through_candidate_pixel_ratio"], 0.065, name)
                self.assertIsNone(ImageChops.difference(original, processed).getbbox(), name)

            bleed_guard = audit_summary["guardrails"]["bleed_through"]
            self.assertEqual(audit_summary["counts"]["bleed_through_cleaned_files"], 0)
            self.assertEqual(bleed_guard["applied_files"], 0)
            self.assertEqual(bleed_guard["skipped_files"], len(expected_reason_codes))
            self.assertIn("protected_line_or_annotation", bleed_guard["skip_reason_code_distribution"])
            self.assertIn("protected_edge_content", bleed_guard["skip_reason_code_distribution"])
            self.assertIn("protected_color_content", bleed_guard["skip_reason_code_distribution"])
            self.assertIn("protected_texture_or_archival_trace", bleed_guard["skip_reason_code_distribution"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*expected_reason_codes, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_mild_blue_gray_cast_stays_guarded_and_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-blue-gray-cast-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_full_chain_blue_gray_cast.png"
            image = Image.new("RGB", (240, 180), (235, 239, 243))
            draw = ImageDraw.Draw(image)
            for y in (42, 66, 90):
                draw.rectangle((36, y, 128, y + 3), fill=(58, 58, 58))
            image.save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "blue-gray-cast", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["paper_color_cast_normalized"])
            self.assertEqual(record["paper_color_cast_reason_code"], "applied_mild_uniform_scanner_cast")
            self.assertIn("normalize_paper_color_cast_conservative", record["operations"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_action"], "passed")
            self.assertEqual(audit["processed_output_safety_guard_action"], "passed")
            self.assertLessEqual(audit["paper_color_cast_delta"], 10.0)
            self.assertLessEqual(audit["paper_color_cast_brightness_delta"], 3.0)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.10)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], 1)
            self.assertEqual(audit_summary["counts"]["combination_quality_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["counts"]["processed_output_safety_guard_reverted_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "private_full_chain_blue_gray_cast.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_mild_warm_paper_cast_cleans_up_while_protected_color_content_noops(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-warm-cast-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_mild_warm_cast.png": _mild_warm_scanner_cast_page("safe"),
                "synthetic_protected_red_stamp_cast.png": _mild_warm_scanner_cast_page("stamp"),
                "synthetic_protected_blue_annotation_cast.png": _mild_warm_scanner_cast_page("annotation"),
                "synthetic_protected_colored_paper_cast.png": Image.new("RGB", (240, 180), (230, 214, 178)),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "warm-cast", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_paper_color_cast=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            for name, original_bytes in source_bytes.items():
                self.assertEqual((input_dir / name).read_bytes(), original_bytes)

            safe_record = records["synthetic_safe_mild_warm_cast.png"]
            safe_output = Image.open(process_dir / safe_record["output_relative_path"]).convert("RGB")
            safe_source = pages["synthetic_safe_mild_warm_cast.png"]
            safe_audit = safe_record["processing_audit"]
            self.assertTrue(safe_record["paper_color_cast_normalized"])
            self.assertEqual(safe_record["paper_color_cast_reason_code"], "applied_mild_uniform_scanner_cast")
            self.assertIn("normalize_paper_color_cast_conservative", safe_record["operations"])
            self.assertGreater(safe_audit["paper_color_cast_delta"], 8.0)
            self.assertLessEqual(safe_audit["paper_color_cast_delta"], 12.0)
            self.assertLessEqual(safe_audit["paper_color_cast_brightness_delta"], 4.0)
            self.assertGreater(safe_audit["paper_color_cast_changed_pixel_ratio"], 0.90)
            self.assertLessEqual(safe_audit["paper_color_cast_candidate_pixel_ratio"], 0.95)
            self.assertLess(_mean_channel_spread(safe_output), _mean_channel_spread(safe_source) - 8.0)
            self.assertLess(_mean_luma_delta(safe_source, safe_output), 4.0)
            self.assertLess(_changed_ratio(safe_source, safe_output, (34, 40, 130, 95)), 0.001)
            self.assertLess(_changed_ratio(safe_source, safe_output, (178, 22, 200, 34)), 0.001)
            self.assertEqual(safe_audit["guardrail_failures"], [])

            protected_expected_codes = {
                "synthetic_protected_red_stamp_cast.png": "protected_color_content",
                "synthetic_protected_blue_annotation_cast.png": "protected_color_content",
            }
            for name, expected_code in protected_expected_codes.items():
                record = records[name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertFalse(record["paper_color_cast_normalized"], name)
                self.assertEqual(record["paper_color_cast_reason_code"], expected_code, name)
                self.assertIn("normalize_paper_color_cast_noop", record["operations"], name)
                self.assertLess(_changed_ratio(pages[name], processed, (0, 0, processed.width, processed.height)), 0.001)

            colored_record = records["synthetic_protected_colored_paper_cast.png"]
            colored_output = Image.open(process_dir / colored_record["output_relative_path"]).convert("RGB")
            self.assertFalse(colored_record["paper_color_cast_normalized"])
            self.assertIn(colored_record["paper_color_cast_reason_code"], {"colored_paper", "too_dark"})
            self.assertLess(
                _changed_ratio(
                    pages["synthetic_protected_colored_paper_cast.png"],
                    colored_output,
                    (0, 0, colored_output.width, colored_output.height),
                ),
                0.001,
            )

            cast_guard = audit_summary["guardrails"]["paper_color_cast"]
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], 1)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_skipped_files"], 3)
            self.assertEqual(cast_guard["applied_files"], 1)
            self.assertEqual(cast_guard["skipped_files"], 3)
            self.assertEqual(cast_guard["reason_code_distribution"]["applied_mild_uniform_scanner_cast"], 1)
            self.assertEqual(cast_guard["skip_reason_code_distribution"]["protected_color_content"], 2)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "synthetic_safe_mild_warm_cast.png",
                "synthetic_protected_red_stamp_cast.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_mild_mixed_paper_cast_cleans_up_while_content_risks_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-mixed-cast-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_mild_mixed_cast.png": _mild_mixed_scanner_cast_page("safe"),
                "synthetic_protected_mixed_cast_stamp.png": _mild_mixed_scanner_cast_page("stamp"),
                "synthetic_protected_mixed_cast_photo_texture.png": _mild_mixed_scanner_cast_page("photo_texture"),
                "synthetic_protected_mixed_cast_ruled_edge.png": _mild_mixed_scanner_cast_page("ruled_edge"),
                "synthetic_protected_mixed_cast_colored_stationery.png": _mild_mixed_scanner_cast_page(
                    "colored_stationery"
                ),
                "synthetic_protected_mixed_cast_aged_paper.png": _mild_mixed_scanner_cast_page("aged_paper"),
                "synthetic_protected_mixed_cast_illumination.png": _mild_mixed_scanner_cast_page("illumination"),
                "synthetic_protected_mixed_cast_large_stain.png": _mild_mixed_scanner_cast_page("large_stain"),
                "synthetic_protected_mixed_cast_pale_ruled.png": _mild_mixed_scanner_cast_page("pale_ruled"),
                "synthetic_protected_mixed_cast_pale_table.png": _mild_mixed_scanner_cast_page("pale_table"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "mixed-cast", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(normalize_paper_color_cast=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            for name, original_bytes in source_bytes.items():
                self.assertEqual((input_dir / name).read_bytes(), original_bytes)

            safe_record = records["synthetic_safe_mild_mixed_cast.png"]
            safe_output = Image.open(process_dir / safe_record["output_relative_path"]).convert("RGB")
            safe_source = pages["synthetic_safe_mild_mixed_cast.png"]
            safe_audit = safe_record["processing_audit"]
            self.assertTrue(safe_record["paper_color_cast_normalized"])
            self.assertEqual(safe_record["paper_color_cast_reason_code"], "applied_mild_mixed_scanner_cast")
            self.assertIn("normalize_paper_color_cast_conservative", safe_record["operations"])
            self.assertGreater(safe_audit["paper_color_cast_delta"], 5.0)
            self.assertLessEqual(safe_audit["paper_color_cast_delta"], 12.0)
            self.assertLessEqual(safe_audit["paper_color_cast_brightness_delta"], 4.0)
            self.assertGreater(safe_audit["paper_color_cast_changed_pixel_ratio"], 0.70)
            self.assertLessEqual(safe_audit["paper_color_cast_candidate_pixel_ratio"], 0.95)
            self.assertLess(
                _side_paper_channel_spread(safe_output),
                _side_paper_channel_spread(safe_source) - 5.0,
            )
            self.assertLess(_mean_luma_delta(safe_source, safe_output), 4.0)
            self.assertLess(_changed_ratio(safe_source, safe_output, (34, 40, 130, 95)), 0.001)
            self.assertLess(_changed_ratio(safe_source, safe_output, (178, 22, 200, 34)), 0.001)
            self.assertEqual(safe_audit["guardrail_failures"], [])

            protected_expected_codes = {
                "synthetic_protected_mixed_cast_stamp.png": {"protected_color_content"},
                "synthetic_protected_mixed_cast_photo_texture.png": {
                    "protected_color_content",
                    "protected_photo_or_texture",
                },
                "synthetic_protected_mixed_cast_ruled_edge.png": {
                    "protected_dark_content",
                    "protected_edge_mark",
                },
                "synthetic_protected_mixed_cast_colored_stationery.png": {
                    "already_neutral",
                    "colored_paper",
                    "not_uniform",
                },
                "synthetic_protected_mixed_cast_aged_paper.png": {
                    "colored_paper",
                    "not_uniform",
                    "too_dark",
                },
                "synthetic_protected_mixed_cast_illumination.png": {"already_neutral", "not_uniform"},
                "synthetic_protected_mixed_cast_large_stain.png": {"not_uniform"},
                "synthetic_protected_mixed_cast_pale_ruled.png": {"protected_dark_content"},
                "synthetic_protected_mixed_cast_pale_table.png": {"protected_dark_content"},
            }
            observed_protected_codes = set()
            for name, expected_codes in protected_expected_codes.items():
                record = records[name]
                processed = Image.open(process_dir / record["output_relative_path"]).convert("RGB")
                self.assertFalse(record["paper_color_cast_normalized"], name)
                self.assertIn(record["paper_color_cast_reason_code"], expected_codes, name)
                observed_protected_codes.add(record["paper_color_cast_reason_code"])
                self.assertIn("normalize_paper_color_cast_noop", record["operations"], name)
                self.assertLess(_changed_ratio(pages[name], processed, (0, 0, processed.width, processed.height)), 0.001)

            cast_guard = audit_summary["guardrails"]["paper_color_cast"]
            self.assertEqual(audit_summary["counts"]["paper_color_cast_normalized_files"], 1)
            self.assertEqual(audit_summary["counts"]["paper_color_cast_skipped_files"], 9)
            self.assertEqual(cast_guard["applied_files"], 1)
            self.assertEqual(cast_guard["skipped_files"], 9)
            self.assertEqual(cast_guard["reason_code_distribution"]["applied_mild_mixed_scanner_cast"], 1)
            self.assertGreaterEqual(cast_guard["skip_reason_code_distribution"]["protected_color_content"], 1)
            self.assertTrue({"protected_photo_or_texture", "protected_color_content"} & observed_protected_codes)
            self.assertTrue({"protected_dark_content", "protected_edge_mark"} & observed_protected_codes)
            self.assertIn("not_uniform", cast_guard["skip_reason_code_distribution"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "synthetic_safe_mild_mixed_cast.png",
                "synthetic_protected_mixed_cast_stamp.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_segmented_scanline_chain_stays_aggregate_and_guarded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-segmented-scanline-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_segmented_scanline.png"
            _segmented_scanline_page().save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "segmented-scanline", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["scanlines_lightened"])
            self.assertEqual(record["scanlines_orientation"], "horizontal")
            self.assertIn("lighten_scanlines_conservative", record["operations"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["local_content_change_guard_action"], "passed")
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_action"], "passed")
            self.assertGreater(audit["scanlines_changed_pixel_ratio"], 0.0007)
            self.assertLessEqual(audit["scanlines_changed_pixel_ratio"], 0.04)
            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["scanlines"]["applied_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("private_segmented_scanline.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_subtle_continuous_scanline_cleanup_stays_conservative_and_aggregate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-subtle-scanline-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            safe_source = input_dir / "private_subtle_vertical_scanline.png"
            ruled_source = input_dir / "private_subtle_ruled_table.png"
            safe_page = _subtle_vertical_scanline_page()
            ruled_page = _subtle_ruled_table_page()
            safe_page.save(safe_source, dpi=(300, 300))
            ruled_page.save(ruled_source, dpi=(300, 300))
            safe_source_bytes = safe_source.read_bytes()
            ruled_source_bytes = ruled_source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "subtle-scanline", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_scanlines=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}
            safe_record = records["private_subtle_vertical_scanline.png"]
            ruled_record = records["private_subtle_ruled_table.png"]
            with Image.open(process_dir / safe_record["output_relative_path"]) as safe_processed:
                processed_safe = safe_processed.convert("RGB")
            with Image.open(process_dir / ruled_record["output_relative_path"]) as ruled_processed:
                processed_ruled = ruled_processed.convert("RGB")

            self.assertEqual(safe_source.read_bytes(), safe_source_bytes)
            self.assertEqual(ruled_source.read_bytes(), ruled_source_bytes)
            self.assertTrue(safe_record["scanlines_lightened"])
            self.assertEqual(safe_record["scanlines_orientation"], "vertical")
            self.assertIn("lighten_scanlines_conservative", safe_record["operations"])
            self.assertGreater(
                _mean_luma(processed_safe, (210, 18, 211, 202)),
                _mean_luma(safe_page, (210, 18, 211, 202)) + 2.0,
            )
            self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [])
            self.assertEqual(safe_record["processing_audit"]["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_record["processing_audit"]["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_record["processing_audit"]["processed_output_safety_guard_action"], "passed")
            self.assertGreater(safe_record["scanlines_changed_pixel_ratio"], 0.0007)
            self.assertLess(safe_record["scanlines_changed_pixel_ratio"], 0.035)

            self.assertFalse(ruled_record["scanlines_lightened"])
            self.assertIn("lighten_scanlines_noop", ruled_record["operations"])
            self.assertIn("SCANLINE_SCOPE_RISK", ruled_record["scanlines_reason"])
            self.assertLess(
                _changed_ratio(ruled_page, processed_ruled, (0, 0, ruled_page.width, ruled_page.height)),
                0.001,
            )
            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["scanlines_skipped_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["scanlines"]["applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["scanlines"]["skipped_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "private_subtle_vertical_scanline",
                "private_subtle_ruled_table",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_very_subtle_scanline_cleanup_preserves_real_document_lines(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-very-subtle-scanline-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_very_subtle_scanline.png": _very_subtle_scanline_page(),
                "synthetic_protected_table_scanline.png": _very_subtle_scanline_page("table"),
                "synthetic_protected_ruled_scanline.png": _very_subtle_scanline_page("ruled"),
                "synthetic_protected_underline_scanline.png": _very_subtle_scanline_page("underline"),
                "synthetic_protected_stamp_scanline.png": _very_subtle_scanline_page("stamp"),
                "synthetic_protected_marginal_mark_scanline.png": _very_subtle_scanline_page("marginal_mark"),
                "synthetic_protected_faint_marginal_note_scanline.png": _very_subtle_scanline_page(
                    "faint_marginal_note"
                ),
                "synthetic_protected_edge_line_scanline.png": _very_subtle_scanline_page("edge_line"),
                "synthetic_protected_near_edge_rule_scanline.png": _very_subtle_scanline_page("near_edge_rule"),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "very-subtle-scanline", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_scanlines=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}
            safe_name = "synthetic_safe_very_subtle_scanline.png"
            safe_record = records[safe_name]
            with Image.open(process_dir / safe_record["output_relative_path"]) as safe_processed:
                processed_safe = safe_processed.convert("RGB")

            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertEqual(safe_record["status"], "processed")
            self.assertTrue(safe_record["scanlines_lightened"])
            self.assertEqual(safe_record["scanlines_orientation"], "vertical")
            self.assertIn("lighten_scanlines_conservative", safe_record["operations"])
            self.assertGreater(
                _mean_luma(processed_safe, (210, 18, 211, 202)),
                _mean_luma(pages[safe_name], (210, 18, 211, 202)) + 2.0,
            )
            self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [])
            self.assertGreater(safe_record["scanlines_changed_pixel_ratio"], 0.0007)
            self.assertLess(safe_record["scanlines_changed_pixel_ratio"], 0.01)
            self.assertLessEqual(safe_record["scanlines_candidate_pixel_ratio"], 0.01)

            protected_names = set(pages) - {safe_name}
            for name in protected_names:
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name], name)
                self.assertFalse(record["scanlines_lightened"], name)
                self.assertIn("lighten_scanlines_noop", record["operations"], name)
                self.assertEqual(record["scanlines_changed_pixel_ratio"], 0.0, name)
                self.assertIsInstance(record["scanlines_reason"], str, name)
                with Image.open(process_dir / record["output_relative_path"]) as processed:
                    self.assertEqual(processed.convert("RGB").tobytes(), pages[name].tobytes(), name)

            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["scanlines_skipped_files"], len(protected_names))
            scanline_guard = audit_summary["guardrails"]["scanlines"]
            self.assertEqual(scanline_guard["applied_files"], 1)
            self.assertEqual(scanline_guard["skipped_files"], len(protected_names))
            self.assertGreaterEqual(scanline_guard["protection_triggered_files"], 2)
            self.assertGreaterEqual(len(scanline_guard["skip_reason_distribution"]), 3)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_safe_cloud_background_stain_stays_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-cloud-stain-combo-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_cloud_stain_combo.png"
            _safe_cloud_stain_combination_page().save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "cloud-stain-combo", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["despeckled"])
            self.assertTrue(record["background_stains_lightened"])
            self.assertIn("lighten_background_stains_conservative", record["operations"])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertLessEqual(audit["background_stains_changed_pixel_ratio"], 0.085)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.09)
            self.assertLessEqual(audit["cumulative_change_score"], 0.20)
            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in ("private_cloud_stain_combo.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_reverts_low_contrast_foreground_weakening_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-faint-foreground-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_faint_foreground_guard.png"
            image = Image.new("RGB", (180, 120), (242, 242, 238))
            draw = ImageDraw.Draw(image)
            draw.ellipse((104, 24, 152, 68), fill=(224, 224, 220))
            for y in (38, 56, 74):
                draw.rectangle((34, y, 136, y + 3), fill=(207, 207, 203))
            draw.line((42, 94, 136, 104), fill=(205, 205, 201), width=2)
            image.save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            def lift_faint_strokes(current: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                changed = current.copy()
                changed_draw = ImageDraw.Draw(changed)
                changed_draw.ellipse((104, 24, 152, 68), fill=(235, 235, 232))
                for y in (38, 56, 74):
                    changed_draw.rectangle((34, y, 136, y + 3), fill=(235, 235, 232))
                return processing_module.BackgroundStainLighteningResult(
                    changed,
                    True,
                    "background stains lightened: stable isolated stains on light paper",
                    224.0,
                    235.0,
                    11.0,
                    0.050,
                    0.050,
                )

            def lift_handwriting(current: Image.Image) -> processing_module.ScanlineLighteningResult:
                changed = current.copy()
                ImageDraw.Draw(changed).line((42, 94, 136, 104), fill=(235, 235, 232), width=2)
                return processing_module.ScanlineLighteningResult(
                    changed,
                    True,
                    "scanlines lightened: stable horizontal scanline pattern",
                    "horizontal",
                    1,
                    205.0,
                    235.0,
                    30.0,
                    0.010,
                    0.010,
                )

            report = scan_batch(ScanConfig("synthetic-regression", "faint-foreground-guard", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_lighten_background_stains_conservative",
                side_effect=lift_faint_strokes,
            ), mock.patch.object(
                processing_module,
                "_lighten_scanlines_conservative",
                side_effect=lift_handwriting,
            ):
                manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["background_stains_lightened"])
            self.assertFalse(record["scanlines_lightened"])
            self.assertEqual(audit["processed_output_safety_guard_action"], "reverted_to_source")
            self.assertEqual(audit["processed_output_safety_guard_reason_code"], "processed_output_quality_reverted")
            self.assertIn("protected_foreground_weakening", audit["processed_output_safety_guard_reasons"])
            self.assertIn("processed_output_safety_guard_reverted_to_source", record["operations"])
            with Image.open(process_dir / record["output_relative_path"]) as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            self.assertEqual(audit_summary["counts"]["processed_output_safety_guard_reverted_files"], 1)
            self.assertEqual(
                audit_summary["counts"]["processed_output_foreground_weakening_guard_reverted_files"],
                1,
            )
            self.assertEqual(
                audit_summary["guardrails"]["processed_output_safety_guard"]["reason_distribution"][
                    "protected_foreground_weakening"
                ],
                1,
            )
            quality = _processing_quality_regression(manifest)
            self.assertEqual(quality["counts"]["processed_output_foreground_weakening_guard_reverted_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("private_faint_foreground_guard", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_reverts_edge_annotation_foreground_weakening_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-edge-annotation-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "private_edge_annotation_guard.png"
            image = Image.new("RGB", (180, 120), (242, 242, 238))
            draw = ImageDraw.Draw(image)
            draw.ellipse((86, 34, 152, 84), fill=(224, 224, 220))
            draw.rectangle((4, 92, 24, 98), fill=(205, 205, 201))
            draw.text((144, 8), "12", fill=(206, 206, 202))
            image.save(source, dpi=(300, 300))

            def lift_page_number(current: Image.Image) -> processing_module.BackgroundStainLighteningResult:
                changed = current.copy()
                changed_draw = ImageDraw.Draw(changed)
                changed_draw.ellipse((86, 34, 152, 84), fill=(235, 235, 232))
                changed_draw.text((144, 8), "12", fill=(235, 235, 232))
                return processing_module.BackgroundStainLighteningResult(
                    changed,
                    True,
                    "background stains lightened: stable isolated stains on light paper",
                    224.0,
                    235.0,
                    11.0,
                    0.045,
                    0.045,
                )

            def lift_edge_mark(current: Image.Image) -> processing_module.ScanlineLighteningResult:
                changed = current.copy()
                ImageDraw.Draw(changed).rectangle((4, 92, 24, 98), fill=(235, 235, 232))
                return processing_module.ScanlineLighteningResult(
                    changed,
                    True,
                    "scanlines lightened: stable horizontal scanline pattern",
                    "horizontal",
                    1,
                    205.0,
                    235.0,
                    30.0,
                    0.008,
                    0.008,
                )

            report = scan_batch(ScanConfig("synthetic-regression", "edge-annotation-guard", input_dir, output_dir))
            with mock.patch.object(
                processing_module,
                "_lighten_background_stains_conservative",
                side_effect=lift_page_number,
            ), mock.patch.object(
                processing_module,
                "_lighten_scanlines_conservative",
                side_effect=lift_edge_mark,
            ):
                manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["background_stains_lightened"])
            self.assertFalse(record["scanlines_lightened"])
            self.assertEqual(audit["processed_output_safety_guard_action"], "reverted_to_source")
            self.assertIn("protected_foreground_weakening", audit["processed_output_safety_guard_reasons"])
            with Image.open(process_dir / record["output_relative_path"]) as processed:
                self.assertEqual(processed.convert("RGB").tobytes(), image.tobytes())
            self.assertEqual(
                audit_summary["guardrails"]["processed_output_safety_guard"]["reason_distribution"][
                    "protected_foreground_weakening"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("private_edge_annotation_guard", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_low_confidence_combination_preserves_original_with_public_reason_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-low-confidence-combo-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = input_dir / "synthetic_low_confidence_combo.png"
            Image.new("RGB", (150, 110), (244, 244, 244)).save(source, dpi=(300, 300))
            source_bytes = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "low-confidence-combo", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(enhance_faded_text=True, sharpen_text_edges=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(record["status"], "processed")
            self.assertFalse(record["faded_text_enhanced"])
            self.assertFalse(record["text_edges_sharpened"])
            self.assertEqual(audit["combination_quality_guard_action"], "kept_original")
            self.assertEqual(
                audit["combination_quality_guard_reason_code"],
                "low_confidence_original_preserved",
            )
            self.assertEqual(
                audit_summary["guardrails"]["combination_quality_guard"]["reason_code_distribution"][
                    "low_confidence_original_preserved"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("synthetic_low_confidence_combo.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_trim_dark_border_auto_crop_combination_keeps_narrow_gray_edge_change_controlled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-narrow-gray-trim-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (160, 120), (244, 244, 240))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 159, 119), outline=(94, 94, 94), width=3)
            draw.rectangle((54, 45, 106, 50), fill=(20, 20, 20))
            draw.rectangle((54, 62, 118, 67), fill=(20, 20, 20))
            image.save(input_dir / "synthetic_narrow_gray_combo.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "narrow-gray-trim", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(trim_dark_border=True, auto_crop=True, deskew=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertTrue(record["dark_border_trimmed"])
            self.assertEqual(record["dark_border_bbox"], [3, 3, 157, 117])
            self.assertFalse(record["cropped"])
            self.assertEqual(record["crop_reason"], "candidate crop exceeds conservative crop ratio")
            self.assertEqual(record["output_size"], [154, 114])
            self.assertLessEqual(audit["max_trim_margin_ratio"], 0.025)
            self.assertLessEqual(audit["cumulative_change_crop_ratio"], 0.025)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("synthetic_narrow_gray_combo.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_segmented_dark_scanner_border_trim_preserves_edge_content_cases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-segmented-dark-border-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_segmented_dark_scanner_border.png": _segmented_dark_scanner_border_page(),
                "synthetic_segmented_dark_border_page_number.png": _segmented_dark_scanner_border_page(
                    "page_number"
                ),
                "synthetic_segmented_dark_border_marginal_mark.png": _segmented_dark_scanner_border_page(
                    "marginal_mark"
                ),
                "synthetic_segmented_dark_border_stamp.png": _segmented_dark_scanner_border_page("stamp"),
                "synthetic_segmented_dark_border_table_lines.png": _segmented_dark_scanner_border_page(
                    "table_lines"
                ),
                "synthetic_segmented_dark_border_archival_edge.png": _segmented_dark_scanner_border_page(
                    "archival_edge"
                ),
                "synthetic_uncertain_broad_dark_shadow.png": _segmented_dark_scanner_border_page("broad_shadow"),
            }
            source_bytes = {}
            for filename, page in pages.items():
                source = input_dir / filename
                page.save(source, dpi=(300, 300))
                source_bytes[filename] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "segmented-dark-border", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_safe_segmented_dark_scanner_border.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["dark_border_trimmed"])
            self.assertEqual(safe_record["dark_border_reason_code"], "trimmed_broken_edge")
            self.assertEqual(safe_record["dark_border_bbox"], [4, 4, 236, 176])
            self.assertEqual(safe_record["output_size"], [232, 172])
            self.assertLessEqual(safe_record["processing_audit"]["max_trim_margin_ratio"], 0.025)
            self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [])

            protected_reason_codes = {
                "protected_edge_content_near_dark_border",
                "candidate_trim_exceeds_conservative_retain_ratio",
            }
            for name, source in source_bytes.items():
                if name == safe_name:
                    continue
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source)
                self.assertFalse(record["dark_border_trimmed"], name)
                self.assertEqual(record["output_size"], [240, 180], name)
                self.assertIn("dark_border_trim_noop", record["operations"], name)
                self.assertIn(record["dark_border_reason_code"], protected_reason_codes, name)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [], name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.convert("RGB").tobytes(), pages[name].tobytes(), name)

            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertEqual(audit_summary["counts"]["dark_border_skipped_files"], len(pages) - 1)
            self.assertEqual(
                audit_summary["guardrails"]["dark_border_trim"]["guardrail_reason_code_distribution"][
                    "trimmed_broken_edge"
                ],
                1,
            )
            self.assertGreaterEqual(
                audit_summary["guardrails"]["dark_border_trim"]["guardrail_reason_code_distribution"][
                    "protected_edge_content_near_dark_border"
                ],
                5,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages.keys(), str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_interrupted_dark_scanner_border_trim_preserves_protected_cases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-interrupted-dark-border-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_interrupted_dark_scanner_border.png": _interrupted_dark_scanner_border_page(),
                "synthetic_interrupted_dark_border_near_edge_content.png": _interrupted_dark_scanner_border_page(
                    "near_edge_content"
                ),
                "synthetic_interrupted_dark_border_table_lines.png": _interrupted_dark_scanner_border_page(
                    "table_lines"
                ),
                "synthetic_interrupted_dark_border_marginal_text.png": _interrupted_dark_scanner_border_page(
                    "marginal_text"
                ),
                "synthetic_interrupted_dark_border_stamp_block.png": _interrupted_dark_scanner_border_page(
                    "stamp_block"
                ),
                "synthetic_interrupted_dark_border_handwritten_note.png": _interrupted_dark_scanner_border_page(
                    "handwritten_note"
                ),
                "synthetic_interrupted_dark_border_punched_marks.png": _interrupted_dark_scanner_border_page(
                    "punched_marks"
                ),
                "synthetic_interrupted_dark_border_broken_frame.png": _interrupted_dark_scanner_border_page(
                    "broken_frame"
                ),
                "synthetic_interrupted_broad_dark_shadow.png": _interrupted_dark_scanner_border_page("broad_shadow"),
                "synthetic_uncertain_single_interrupted_dark_edge.png": _interrupted_dark_scanner_border_page(
                    "single_edge"
                ),
            }
            source_bytes = {}
            for filename, page in pages.items():
                source = input_dir / filename
                page.save(source, dpi=(300, 300))
                source_bytes[filename] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "interrupted-dark-border", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(trim_dark_border=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_safe_interrupted_dark_scanner_border.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["dark_border_trimmed"])
            self.assertEqual(safe_record["dark_border_reason_code"], "trimmed_broken_edge")
            self.assertEqual(safe_record["dark_border_bbox"], [4, 4, 236, 176])
            self.assertEqual(safe_record["output_size"], [232, 172])
            self.assertLessEqual(safe_record["processing_audit"]["max_trim_margin_ratio"], 0.025)
            self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [])

            protected_reason_codes = {
                "protected_edge_content_near_dark_border",
                "candidate_trim_exceeds_conservative_retain_ratio",
                "incomplete_dark_edge_border_evidence",
            }
            for name, source in source_bytes.items():
                if name == safe_name:
                    continue
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source)
                self.assertFalse(record["dark_border_trimmed"], name)
                self.assertEqual(record["output_size"], [240, 180], name)
                self.assertIn("dark_border_trim_noop", record["operations"], name)
                self.assertIn(record["dark_border_reason_code"], protected_reason_codes, name)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [], name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.convert("RGB").tobytes(), pages[name].tobytes(), name)

            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 1)
            self.assertEqual(audit_summary["counts"]["dark_border_skipped_files"], len(pages) - 1)
            self.assertEqual(
                audit_summary["guardrails"]["dark_border_trim"]["guardrail_reason_code_distribution"][
                    "trimmed_broken_edge"
                ],
                1,
            )
            self.assertGreaterEqual(
                audit_summary["guardrails"]["dark_border_trim"]["guardrail_reason_code_distribution"][
                    "protected_edge_content_near_dark_border"
                ],
                7,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages.keys(), str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_shallow_deskew_auto_crop_trim_combination_stays_controlled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-shallow-deskew-combo-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _shallow_stable_text_page().rotate(
                -0.45,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(246, 246, 246),
            )
            image.save(input_dir / "synthetic_shallow_deskew_combo.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "shallow-deskew-combo", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(trim_dark_border=True, auto_crop=True, deskew=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertTrue(record["deskewed"])
            self.assertAlmostEqual(record["skew_angle_degrees"], -0.45, delta=0.25)
            self.assertFalse(record["dark_border_trimmed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew safe canvas crop applied")
            self.assertEqual(record["crop_bbox"], [2, 1, 246, 323])
            self.assertEqual(record["output_size"], record["pre_deskew_size"])
            self.assertLessEqual(audit["size_change_ratio"], 0.01)
            self.assertEqual(audit["max_trim_margin_ratio"], 0.0)
            self.assertEqual(audit["crop_ratio"], 0.0)
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit_summary["counts"]["deskewed_files"], 1)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["counts"]["dark_border_trimmed_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["post_deskew_safe_crop_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["cropped_side_distribution"],
                {"left": 1, "top": 1, "right": 1, "bottom": 1},
            )
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["reason_distribution"],
                {"post-deskew safe canvas crop applied": 1},
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in ("synthetic_shallow_deskew_combo.png", str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_deskew_preserves_sparse_low_text_pages_and_corrects_safe_text(self) -> None:
        def sparse_or_safe_page(variant: str) -> Image.Image:
            image = Image.new("RGB", (360, 480), (248, 248, 246))
            draw = ImageDraw.Draw(image)
            if variant == "safe_text":
                for y in range(80, 330, 32):
                    draw.rectangle((70, y, 290, y + 5), fill=(35, 35, 35))
            elif variant == "page_number":
                draw.rectangle((164, 438, 196, 448), fill=(35, 35, 35))
                draw.rectangle((178, 424, 184, 438), fill=(35, 35, 35))
            elif variant == "marginal_marks":
                for y in (80, 150, 220, 300):
                    draw.line((18, y, 62, y + 8), fill=(35, 35, 35), width=3)
            elif variant == "color_stamp":
                draw.ellipse((230, 70, 320, 130), outline=(180, 30, 30), width=5)
                draw.line((250, 100, 300, 100), fill=(180, 30, 30), width=3)
            elif variant == "table_fragment":
                for y in (180, 220, 260):
                    draw.line((90, y, 270, y), fill=(30, 30, 30), width=3)
                for x in (130, 210):
                    draw.line((x, 160, x, 280), fill=(30, 30, 30), width=3)
            elif variant == "archival_texture":
                for index in range(36):
                    x = 15 + (index * 47) % 330
                    y = 30 + (index * 71) % 420
                    draw.rectangle((x, y, x + 2 + (index % 4), y + 1 + (index % 3)), fill=(90, 90, 86))
            else:
                raise ValueError(f"unsupported variant: {variant}")
            return image.rotate(-2.0, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(248, 248, 246))

        with tempfile.TemporaryDirectory(prefix="scan-processing-sparse-deskew-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                f"synthetic_sparse_deskew_{variant}.png": sparse_or_safe_page(variant)
                for variant in (
                    "safe_text",
                    "page_number",
                    "marginal_marks",
                    "color_stamp",
                    "table_fragment",
                    "archival_texture",
                )
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "sparse-deskew-guard", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_sparse_deskew_safe_text.png"
            safe_record = records[safe_name]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["deskewed"])
            self.assertAlmostEqual(safe_record["skew_angle_degrees"], -2.0, delta=0.35)
            self.assertEqual(safe_record["deskew_reason"], "deskew applied")
            self.assertIn("deskew_conservative", safe_record["operations"])

            expected_reasons = {
                "synthetic_sparse_deskew_page_number.png": {"low contrast", "insufficient foreground", "low confidence"},
                "synthetic_sparse_deskew_marginal_marks.png": {"low contrast", "low confidence"},
                "synthetic_sparse_deskew_color_stamp.png": {
                    "low confidence",
                    "table or color mark rotation risk",
                },
                "synthetic_sparse_deskew_table_fragment.png": {"table or color mark rotation risk"},
                "synthetic_sparse_deskew_archival_texture.png": {"low contrast", "low confidence"},
            }
            for name, public_reasons in expected_reasons.items():
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertFalse(record["deskewed"], name)
                self.assertIn(record["deskew_reason"], public_reasons, name)
                self.assertIn("deskew_noop", record["operations"], name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertIsNone(ImageChops.difference(pages[name], output.convert("RGB")).getbbox(), name)

            deskew_summary = audit_summary["guardrails"]["deskew"]
            self.assertEqual(audit_summary["counts"]["deskewed_files"], 1)
            self.assertEqual(audit_summary["counts"]["deskew_skipped_files"], len(expected_reasons))
            self.assertEqual(deskew_summary["corrected_files"], 1)
            self.assertEqual(deskew_summary["reason_distribution"]["deskew applied"], 1)
            self.assertEqual(
                sum(
                    count
                    for reason, count in deskew_summary["reason_distribution"].items()
                    if reason != "deskew applied"
                ),
                len(expected_reasons),
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_deskew_corrects_mild_sparse_typed_text_and_preserves_protected_aggregates(self) -> None:
        def draw_segmented_typed_line(draw: ImageDraw.ImageDraw, *, x: int, y: int) -> None:
            index = 0
            while x < 360:
                width = 5 + (index % 5)
                height = 8 + (index % 3)
                draw.rectangle((x, y, x + width, y + height), fill=(140, 140, 138))
                x += width + 4 + (index % 2)
                index += 1

        def mild_sparse_page(variant: str) -> Image.Image:
            image = Image.new("RGB", (520, 680), (248, 248, 246))
            draw = ImageDraw.Draw(image)
            if variant == "safe_text":
                for y in (185, 231):
                    draw_segmented_typed_line(draw, x=130, y=y)
            elif variant == "one_line":
                draw_segmented_typed_line(draw, x=130, y=205)
            elif variant == "ruled":
                for y in (190, 236):
                    draw.line((120, y, 390, y), fill=(52, 52, 50), width=2)
            elif variant == "segmented_guides":
                for y in (185, 231):
                    for x in range(130, 390, 16):
                        draw.rectangle((x, y, x + 8, y + 2), fill=(140, 140, 138))
                        draw.rectangle((x, y - 6, x + 2, y + 8), fill=(140, 140, 138))
            elif variant == "edge_marks":
                for y in (170, 216):
                    for x in range(0, 78, 16):
                        draw.rectangle((x, y, x + 8, y + 3), fill=(140, 140, 138))
                    draw.line((24, y + 13, 68, y + 19), fill=(140, 140, 138), width=2)
            elif variant == "handwriting_like":
                for y in (175, 215, 255):
                    points = [(x, y + (index % 4) - 2) for index, x in enumerate(range(110, 370, 10))]
                    draw.line(points, fill=(140, 140, 138), width=2)
            elif variant == "table":
                for y in (180, 226, 272):
                    draw.line((120, y, 390, y), fill=(52, 52, 50), width=2)
                for x in (132, 164, 196, 228, 260, 292, 324, 356):
                    draw.line((x, 165, x, 285), fill=(52, 52, 50), width=2)
            elif variant == "texture":
                for index in range(90):
                    x = 80 + (index * 37) % 360
                    y = 110 + (index * 53) % 430
                    shade = 90 + (index % 20)
                    draw.rectangle((x, y, x + 2 + (index % 3), y + 1 + (index % 2)), fill=(shade, shade, shade))
            else:
                raise ValueError(f"unsupported mild sparse deskew variant: {variant}")
            return image.rotate(-0.45, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(248, 248, 246))

        with tempfile.TemporaryDirectory(prefix="scan-processing-mild-sparse-deskew-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            variants = ("safe_text", "one_line", "ruled", "segmented_guides", "edge_marks", "handwriting_like", "table", "texture")
            pages = {f"synthetic_mild_sparse_deskew_{variant}.png": mild_sparse_page(variant) for variant in variants}
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            report = scan_batch(ScanConfig("synthetic-regression", "mild-sparse-deskew-guard", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(deskew=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_name = "synthetic_mild_sparse_deskew_safe_text.png"
            safe_record = records[safe_name]
            safe_audit = safe_record["processing_audit"]
            self.assertEqual((input_dir / safe_name).read_bytes(), source_bytes[safe_name])
            self.assertTrue(safe_record["deskewed"])
            self.assertAlmostEqual(safe_record["skew_angle_degrees"], -0.45, delta=0.20)
            self.assertEqual(safe_record["deskew_reason"], "deskew applied")
            self.assertIn("deskew_conservative", safe_record["operations"])
            self.assertLessEqual(safe_audit["size_change_ratio"], 0.03)
            self.assertLessEqual(safe_audit["cumulative_change_pixel_ratio"], 0.03)

            expected_reasons = {
                "synthetic_mild_sparse_deskew_one_line.png": {"low contrast", "low confidence"},
                "synthetic_mild_sparse_deskew_ruled.png": {"low contrast", "table or color mark rotation risk"},
                "synthetic_mild_sparse_deskew_segmented_guides.png": {"low contrast", "low confidence"},
                "synthetic_mild_sparse_deskew_edge_marks.png": {"low contrast", "low confidence"},
                "synthetic_mild_sparse_deskew_handwriting_like.png": {"low contrast", "low confidence"},
                "synthetic_mild_sparse_deskew_table.png": {"table or color mark rotation risk"},
                "synthetic_mild_sparse_deskew_texture.png": {"low contrast", "low confidence"},
            }
            for name, public_reasons in expected_reasons.items():
                record = records[name]
                self.assertEqual((input_dir / name).read_bytes(), source_bytes[name])
                self.assertFalse(record["deskewed"], name)
                self.assertIn(record["deskew_reason"], public_reasons, name)
                self.assertIn("deskew_noop", record["operations"], name)
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertIsNone(ImageChops.difference(pages[name], output.convert("RGB")).getbbox(), name)

            deskew_summary = audit_summary["guardrails"]["deskew"]
            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["deskewed_files"], 1)
            self.assertEqual(audit_summary["counts"]["deskew_skipped_files"], len(expected_reasons))
            self.assertEqual(deskew_summary["corrected_files"], 1)
            self.assertEqual(deskew_summary["reason_distribution"]["deskew applied"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_post_deskew_corner_wedge_crop_stays_within_geometry_limits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-post-deskew-wedge-full-chain-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (320, 240), (248, 248, 248))
            draw = ImageDraw.Draw(image)
            draw.rectangle((58, 50, 262, 184), outline=(160, 160, 160), width=1)
            for y in (70, 92, 114, 136, 158):
                draw.rectangle((72, y, 248, y + 3), fill=(35, 35, 35))
            image.rotate(
                1.0,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(245, 245, 245),
            ).save(input_dir / "private_full_chain_post_deskew_wedge.png", dpi=(300, 300))

            report = scan_batch(
                ScanConfig("synthetic-regression", "post-deskew-wedge-full-chain", input_dir, output_dir)
            )
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    scanner_gutter_trim=True,
                    despeckle=True,
                    normalize_tones=True,
                    lighten_edge_shadow=True,
                    lighten_background_stains=True,
                    level_illumination_gradient=True,
                    lighten_scanlines=True,
                    enhance_faded_text=True,
                    sharpen_text_edges=True,
                    workers=1,
                ),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["deskewed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew safe canvas crop applied")
            self.assertEqual(record["output_size"], [318, 238])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed")
            self.assertLessEqual(audit["crop_ratio"], 0.06)
            self.assertLessEqual(audit["cumulative_change_crop_ratio"], 0.06)
            self.assertEqual(audit["max_trim_margin_ratio"], 0.0)
            self.assertTrue(audit_summary["operations"]["deskew"])
            self.assertTrue(audit_summary["operations"]["trim_dark_border"])
            self.assertTrue(audit_summary["operations"]["scanner_gutter_trim"])
            self.assertTrue(audit_summary["operations"]["auto_crop"])
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in (
                "private_full_chain_post_deskew_wedge.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_faint_post_deskew_corner_wedge_crop_removes_safe_canvas_fill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-faint-post-deskew-wedge-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _faint_post_deskew_corner_wedge_page().save(
                input_dir / "private_faint_post_deskew_wedge.png", dpi=(300, 300)
            )

            report = scan_batch(ScanConfig("synthetic-regression", "faint-post-deskew-wedge", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, deskew=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertTrue(record["deskewed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew safe canvas crop applied")
            self.assertEqual(record["crop_bbox"], [3, 5, 321, 243])
            self.assertEqual(record["output_size"], [318, 238])
            self.assertLess(record["output_size"][0], record["pre_deskew_size"][0])
            self.assertLess(record["output_size"][1], record["pre_deskew_size"][1])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["post_deskew_safe_crop_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in (
                "private_faint_post_deskew_wedge.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_faint_post_deskew_corner_wedge_protected_content_cases_stay_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-faint-post-deskew-wedge-guards-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_faint_wedge_edge_handwriting.png": _faint_post_deskew_corner_wedge_page(
                    variant="edge_handwriting"
                ),
                "private_faint_wedge_page_number.png": _faint_post_deskew_corner_wedge_page(variant="page_number"),
                "private_faint_wedge_ruled_table.png": _faint_post_deskew_corner_wedge_page(variant="ruled_table"),
                "private_faint_wedge_color_stamp.png": _faint_post_deskew_corner_wedge_page(variant="color_stamp"),
                "private_faint_wedge_archival_corner.png": _faint_post_deskew_corner_wedge_page(
                    variant="archival_corner"
                ),
            }
            for filename, page in pages.items():
                page.save(input_dir / filename, dpi=(300, 300))

            report = scan_batch(
                ScanConfig("synthetic-regression", "faint-post-deskew-wedge-guard-cases", input_dir, output_dir)
            )
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, deskew=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            self.assertEqual(len(manifest["files"]), len(pages))
            for record in manifest["files"]:
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["deskewed"], record["source_relative_path"])
                self.assertEqual(record["deskew_reason"], "edge content near rotation boundary")
                self.assertFalse(record["cropped"], record["source_relative_path"])
                self.assertIn("auto_crop_noop", record["operations"])
                self.assertEqual(record["output_size"], [322, 244])
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
                self.assertEqual(record["processing_audit"]["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertGreaterEqual(audit_summary["counts"]["auto_crop_skipped_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages.keys(), str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_single_edge_light_post_deskew_canvas_crop_removes_safe_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-single-edge-post-deskew-canvas-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            _single_edge_post_deskew_light_canvas_page().save(
                input_dir / "private_single_edge_post_deskew_canvas.png", dpi=(300, 300)
            )

            report = scan_batch(
                ScanConfig("synthetic-regression", "single-edge-post-deskew-canvas", input_dir, output_dir)
            )
            with mock.patch.object(
                processing_module,
                "_detect_skew",
                return_value=processing_module.SkewDetection(0.35, 1.0, "synthetic single-edge deskew"),
            ), mock.patch.object(
                processing_module,
                "_safe_deskew_skip_from_scan_record",
                return_value=None,
            ), mock.patch.object(
                processing_module,
                "_safe_deskew_skip_from_page_evidence",
                return_value=None,
            ), mock.patch.object(
                processing_module,
                "_deskew_has_edge_content_risk",
                return_value=False,
            ), mock.patch.object(
                processing_module,
                "_deskew_has_color_or_table_risk",
                return_value=False,
            ), mock.patch.object(
                processing_module,
                "_rotate_for_deskew",
                side_effect=lambda image, _angle: image.copy(),
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(auto_crop=True, deskew=True, workers=1),
                )

            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertTrue(record["deskewed"])
            self.assertTrue(record["cropped"])
            self.assertEqual(record["crop_reason"], "post-deskew safe canvas crop applied")
            self.assertEqual(record["crop_bbox"], [14, 0, 320, 240])
            self.assertEqual(record["output_size"], [306, 240])
            self.assertLessEqual(audit["crop_ratio"], 0.05)
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["post_deskew_safe_crop_files"], 1)
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["reason_distribution"][
                    "post-deskew safe canvas crop applied"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in (
                "private_single_edge_post_deskew_canvas.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_single_edge_light_post_deskew_canvas_protects_marginal_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-single-edge-post-deskew-canvas-guards-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_single_edge_handwriting.png": _single_edge_post_deskew_light_canvas_page(
                    variant="edge_handwriting"
                ),
                "synthetic_single_edge_page_number.png": _single_edge_post_deskew_light_canvas_page(
                    variant="page_number"
                ),
                "synthetic_single_edge_ruled_table.png": _single_edge_post_deskew_light_canvas_page(
                    variant="ruled_table"
                ),
                "synthetic_single_edge_stamp.png": _single_edge_post_deskew_light_canvas_page(variant="stamp"),
                "synthetic_single_edge_archival_mark.png": _single_edge_post_deskew_light_canvas_page(
                    variant="archival_mark"
                ),
                "synthetic_single_edge_pale_page_number.png": _single_edge_post_deskew_light_canvas_page(
                    variant="pale_page_number"
                ),
                "synthetic_single_edge_pale_marginal_note.png": _single_edge_post_deskew_light_canvas_page(
                    variant="pale_marginal_note"
                ),
                "synthetic_single_edge_pale_ruled_table.png": _single_edge_post_deskew_light_canvas_page(
                    variant="pale_ruled_table"
                ),
                "synthetic_single_edge_pale_archival_texture.png": _single_edge_post_deskew_light_canvas_page(
                    variant="pale_archival_texture"
                ),
                "synthetic_single_edge_uneven_pale_margin.png": _single_edge_post_deskew_light_canvas_page(
                    variant="uneven_pale_margin"
                ),
                "synthetic_single_edge_near_edge_scanner_artifact.png": _single_edge_post_deskew_light_canvas_page(
                    variant="near_edge_scanner_artifact"
                ),
            }
            for filename, page in pages.items():
                page.save(input_dir / filename, dpi=(300, 300))

            report = scan_batch(
                ScanConfig("synthetic-regression", "single-edge-post-deskew-canvas-guards", input_dir, output_dir)
            )
            with mock.patch.object(
                processing_module,
                "_detect_skew",
                return_value=processing_module.SkewDetection(0.35, 1.0, "synthetic single-edge deskew"),
            ), mock.patch.object(
                processing_module,
                "_safe_deskew_skip_from_scan_record",
                return_value=None,
            ), mock.patch.object(
                processing_module,
                "_safe_deskew_skip_from_page_evidence",
                return_value=None,
            ), mock.patch.object(
                processing_module,
                "_deskew_has_edge_content_risk",
                return_value=False,
            ), mock.patch.object(
                processing_module,
                "_deskew_has_color_or_table_risk",
                return_value=False,
            ), mock.patch.object(
                processing_module,
                "_rotate_for_deskew",
                side_effect=lambda image, _angle: image.copy(),
            ):
                manifest = process_images(
                    report,
                    input_dir,
                    process_dir,
                    ProcessingOptions(auto_crop=True, deskew=True, workers=1),
                )

            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            for name, record in records.items():
                self.assertEqual(record["status"], "processed", name)
                self.assertTrue(record["deskewed"], name)
                self.assertFalse(record["cropped"], name)
                self.assertIn(
                    record["crop_reason"],
                    {
                        "post-deskew crop skipped: edge content protection",
                        "foreground reaches crop safety margin",
                    },
                    name,
                )
                self.assertEqual(record["output_size"], [320, 240], name)
                self.assertEqual(record["processing_audit"]["crop_ratio"], 0.0, name)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [], name)

            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], len(pages))
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["edge_content_protection_skip_files"],
                len(pages) - 1,
            )
            self.assertEqual(audit_summary["guardrails"]["auto_crop"]["protection_triggered_files"], len(pages))
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["skip_reason_distribution"][
                    "post-deskew crop skipped: edge content protection"
                ],
                len(pages) - 1,
            )
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["skip_reason_distribution"][
                    "foreground reaches crop safety margin"
                ],
                1,
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages.keys(), str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_conservative_auto_crop_preserves_faint_edge_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-faint-edge-auto-crop-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_safe_blank_light_margin.png": _light_margin_auto_crop_page(),
                "synthetic_faint_edge_page_number.png": _light_margin_auto_crop_page(variant="page_number"),
                "synthetic_faint_edge_marginal_note.png": _light_margin_auto_crop_page(variant="marginal_note"),
                "synthetic_faint_edge_table_border.png": _light_margin_auto_crop_page(variant="table_border"),
                "synthetic_faint_edge_archival_texture.png": _light_margin_auto_crop_page(
                    variant="archival_texture"
                ),
            }
            for filename, page in pages.items():
                page.save(input_dir / filename, dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "faint-edge-auto-crop", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(auto_crop=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}
            safe_record = records["synthetic_safe_blank_light_margin.png"]

            self.assertTrue(safe_record["cropped"])
            self.assertEqual(safe_record["crop_reason"], "conservative crop applied")
            self.assertEqual(safe_record["crop_bbox"], [10, 10, 231, 171])
            self.assertLessEqual(safe_record["processing_audit"]["crop_ratio"], 0.2)
            self.assertEqual(safe_record["processing_audit"]["guardrail_failures"], [])

            protected_names = set(pages) - {"synthetic_safe_blank_light_margin.png"}
            for name in protected_names:
                record = records[name]
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["cropped"], name)
                self.assertEqual(record["crop_reason"], "faint edge content protection", name)
                self.assertEqual(record["output_size"], [240, 180], name)
                with Image.open(input_dir / name) as source, Image.open(
                    process_dir / record["output_relative_path"]
                ) as processed:
                    self.assertIsNone(ImageChops.difference(source, processed).getbbox(), name)
                self.assertEqual(record["processing_audit"]["crop_ratio"], 0.0, name)
                self.assertEqual(record["processing_audit"]["cumulative_change_crop_ratio"], 0.0, name)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [], name)

            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 1)
            self.assertEqual(audit_summary["counts"]["auto_crop_skipped_files"], len(protected_names))
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["skip_reason_distribution"][
                    "faint edge content protection"
                ],
                len(protected_names),
            )
            self.assertEqual(
                audit_summary["guardrails"]["auto_crop"]["protection_triggered_files"],
                len(protected_names),
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages.keys(), str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_variable_pale_gutter_trim_stays_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-variable-gutter-full-chain-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = Image.new("RGB", (260, 190), (239, 239, 239))
            draw = ImageDraw.Draw(image)
            for y in range(190):
                for x in range(15):
                    value = 237 if (x + y) % 2 == 0 else 251
                    image.putpixel((x, y), (value, value, value))
            draw.rectangle((54, 38, 222, 154), outline=(80, 80, 80), width=2)
            for y in range(68, 130, 24):
                draw.rectangle((70, y, 180, y + 2), fill=(70, 70, 70))
            image.save(input_dir / "private_full_chain_variable_pale_gutter.png", dpi=(300, 300))

            report = scan_batch(
                ScanConfig("synthetic-regression", "variable-gutter-full-chain", input_dir, output_dir)
            )
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["scanner_gutter_trimmed"])
            self.assertEqual(record["scanner_gutter_reason"], "scanner gutter trim applied")
            self.assertEqual(record["output_size"], [245, 190])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertLessEqual(audit["scanner_gutter_max_trim_margin_ratio"], 0.06)
            self.assertLessEqual(audit["max_trim_margin_ratio"], 0.06)
            self.assertLessEqual(audit["cumulative_change_crop_ratio"], 0.06)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.08)
            self.assertTrue(audit_summary["operations"]["deskew"])
            self.assertTrue(audit_summary["operations"]["scanner_gutter_trim"])
            self.assertTrue(audit_summary["operations"]["auto_crop"])
            self.assertTrue(audit_summary["operations"]["lighten_scanlines"])
            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "private_full_chain_variable_pale_gutter.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_mixed_tone_binding_gutter_trim_stays_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-mixed-tone-gutter-full-chain-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            image = _mixed_tone_binding_gutter_page()
            image.save(input_dir / "private_full_chain_mixed_tone_gutter.png", dpi=(300, 300))

            report = scan_batch(
                ScanConfig("synthetic-regression", "mixed-tone-gutter-full-chain", input_dir, output_dir)
            )
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            self.assertEqual(record["status"], "processed")
            self.assertTrue(record["scanner_gutter_trimmed"])
            self.assertEqual(record["scanner_gutter_reason"], "scanner gutter trim applied")
            self.assertEqual(record["output_size"], [245, 190])
            self.assertEqual(audit["guardrail_failures"], [])
            self.assertEqual(audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertLessEqual(audit["scanner_gutter_max_trim_margin_ratio"], 0.06)
            self.assertLessEqual(audit["max_trim_margin_ratio"], 0.06)
            self.assertLessEqual(audit["cumulative_change_crop_ratio"], 0.06)
            self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.08)
            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "private_full_chain_mixed_tone_gutter.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_mixed_tone_gutter_protected_content_cases_stay_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-mixed-tone-gutter-guards-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "private_mixed_tone_edge_handwriting.png": _mixed_tone_binding_gutter_page(
                    variant="edge_handwriting"
                ),
                "private_mixed_tone_page_number.png": _mixed_tone_binding_gutter_page(variant="page_number"),
                "private_mixed_tone_ruled_table.png": _mixed_tone_binding_gutter_page(variant="ruled_table"),
                "private_broad_archival_shadow.png": _mixed_tone_binding_gutter_page(variant="broad_shadow"),
            }
            for filename, page in pages.items():
                page.save(input_dir / filename, dpi=(300, 300))

            report = scan_batch(
                ScanConfig("synthetic-regression", "mixed-tone-gutter-guard-cases", input_dir, output_dir)
            )
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(scanner_gutter_trim=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            self.assertEqual(len(manifest["files"]), 4)
            for record in manifest["files"]:
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["scanner_gutter_trimmed"], record["source_relative_path"])
                self.assertEqual(record["output_size"], [260, 190])
                self.assertIn("scanner_gutter_trim_noop", record["operations"])
                self.assertIn(
                    record["scanner_gutter_reason"],
                    {
                        "scanner gutter skipped: no narrow uniform light band",
                        "scanner gutter skipped: protected edge content",
                        "scanner gutter skipped: no inset content evidence",
                    },
                )
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [])
                self.assertEqual(record["processing_audit"]["cumulative_change_guard_action"], "passed")
            self.assertEqual(audit_summary["counts"]["scanner_gutter_trimmed_files"], 0)
            self.assertGreaterEqual(audit_summary["counts"]["scanner_gutter_skipped_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages.keys(), str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_full_chain_risk_combination_pages_skip_or_stay_low_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-full-chain-risk-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "synthetic_table_page_number_annotation.png": _risk_table_page_number_annotation_page(),
                "synthetic_stamp_header_footer.png": _risk_stamp_header_footer_page(),
                "synthetic_dark_photo_edge_trace.png": _risk_dark_photo_edge_trace_page(),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "risk-combination", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, _full_chain_options())
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)

            self.assertEqual(manifest["summary"]["processed_files"], len(pages))
            self.assertEqual(manifest["summary"]["failed_files"], 0)
            for record in manifest["files"]:
                audit = record["processing_audit"]
                with Image.open(process_dir / record["output_relative_path"]) as output:
                    self.assertEqual(output.size, tuple(record["output_size"]))
                self.assertEqual(record["status"], "processed")
                self.assertFalse(record["background_stains_lightened"], record["source_relative_path"])
                self.assertFalse(record["scanlines_lightened"], record["source_relative_path"])
                self.assertFalse(record["faded_text_enhanced"], record["source_relative_path"])
                self.assertFalse(record["text_edges_sharpened"], record["source_relative_path"])
                self.assertFalse(record["cropped"], record["source_relative_path"])
                self.assertEqual(audit["guardrail_failures"], [])
                self.assertIn(audit["cumulative_change_guard_action"], {"passed", "reverted_to_source"})
                self.assertLessEqual(audit["cumulative_change_pixel_ratio"], 0.01)
                self.assertLessEqual(audit["cumulative_change_score"], 0.08)

            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 0)
            self.assertEqual(audit_summary["counts"]["scanlines_lightened_files"], 0)
            self.assertEqual(audit_summary["counts"]["faded_text_enhanced_files"], 0)
            self.assertEqual(audit_summary["counts"]["text_edges_sharpened_files"], 0)
            self.assertEqual(audit_summary["counts"]["auto_crop_applied_files"], 0)
            self.assertEqual(audit_summary["counts"]["cumulative_change_guard_reverted_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["faded_text"]["applied_files"], 0)
            self.assertEqual(audit_summary["guardrails"]["text_edges"]["applied_files"], 0)
            self.assertGreaterEqual(audit_summary["guardrails"]["faded_text"]["protection_triggered_files"], 2)
            self.assertGreaterEqual(audit_summary["guardrails"]["text_edges"]["protection_triggered_files"], 1)
            self.assertGreaterEqual(audit_summary["guardrails"]["scanlines"]["protection_triggered_files"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_sharpen_text_edges_lifts_mildly_blurred_typed_body_text_but_skips_page_number(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-text-edge-body-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            safe_source = _mildly_blurred_typed_body_text_page()
            protected_source = _mildly_blurred_typed_body_text_page(page_number=True)
            pages = {
                "private_safe_mild_typed_body.png": safe_source,
                "private_protected_page_number.png": protected_source,
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "text-edge-body", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(sharpen_text_edges=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_record = records["private_safe_mild_typed_body.png"]
            safe_audit = safe_record["processing_audit"]
            with Image.open(process_dir / safe_record["output_relative_path"]) as safe_output:
                self.assertTrue(safe_record["text_edges_sharpened"])
                self.assertIn("sharpen_text_edges_conservative", safe_record["operations"])
                self.assertGreater(_edge_energy(safe_output), _edge_energy(safe_source))
                self.assertGreater(safe_audit["text_edges_edge_energy_after"], safe_audit["text_edges_edge_energy_before"])
                self.assertGreater(safe_audit["text_edges_changed_pixel_ratio"], 0.0)
                self.assertLessEqual(safe_audit["text_edges_changed_pixel_ratio"], 0.08)
                self.assertLessEqual(safe_audit["text_edges_candidate_pixel_ratio"], 0.12)
            self.assertEqual(safe_record["text_edges_reason_code"], "applied_stable_blurred_text_edges")
            self.assertEqual(safe_record["text_edges_reason_zh"], "检测到浅色纸面上的稳定模糊正文边缘，已保守锐化。")
            self.assertEqual(safe_audit["guardrail_failures"], [])

            protected_record = records["private_protected_page_number.png"]
            protected_audit = protected_record["processing_audit"]
            with Image.open(process_dir / protected_record["output_relative_path"]) as protected_output:
                self.assertFalse(protected_record["text_edges_sharpened"])
                self.assertIn("sharpen_text_edges_noop", protected_record["operations"])
                self.assertLess(_changed_ratio(protected_source, protected_output, (0, 0, 420, 90)), 0.001)
            self.assertIn(
                protected_record["text_edges_reason_code"],
                {"protected_edge_mark_or_binding", "protected_header_footer_or_page_number"},
            )
            self.assertEqual(protected_audit["text_edges_changed_pixel_ratio"], 0.0)
            self.assertEqual(protected_audit["guardrail_failures"], [])

            self.assertEqual(audit_summary["counts"]["text_edges_sharpened_files"], 1)
            self.assertEqual(audit_summary["counts"]["text_edges_skipped_files"], 1)
            text_edge_guard = audit_summary["guardrails"]["text_edges"]
            self.assertEqual(text_edge_guard["applied_files"], 1)
            self.assertEqual(text_edge_guard["skipped_files"], 1)
            self.assertEqual(text_edge_guard["reason_code_distribution"]["applied_stable_blurred_text_edges"], 1)
            self.assertIn(protected_record["text_edges_reason_code"], text_edge_guard["skip_reason_code_distribution"])
            self.assertIn("text_edges_changed_pixel_ratio", audit_summary["metrics"])
            self.assertIn("text_edges_candidate_pixel_ratio", audit_summary["metrics"])
            self.assertIn("text_edges_edge_energy_before", audit_summary["metrics"])
            self.assertIn("text_edges_edge_energy_after", audit_summary["metrics"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_sharpen_text_edges_skips_mildly_blurred_ruled_table_background(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-text-edge-ruled-table-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            source = _mildly_blurred_ruled_table_background_page()
            source.save(input_dir / "synthetic_ruled_table_background.png", dpi=(300, 300))

            report = scan_batch(ScanConfig("synthetic-regression", "text-edge-ruled-table", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(sharpen_text_edges=True, workers=1))
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            record = manifest["files"][0]
            audit = record["processing_audit"]

            with Image.open(process_dir / record["output_relative_path"]) as output:
                self.assertFalse(record["text_edges_sharpened"])
                self.assertIn("sharpen_text_edges_noop", record["operations"])
                self.assertLess(_changed_ratio(source, output, (42, 84, 378, 480)), 0.001)
            self.assertEqual(record["text_edges_reason_code"], "protected_scanline_or_ruled_background")
            self.assertEqual(record["text_edges_reason_zh"], "检测到扫描线或格线背景风险，跳过正文边缘锐化。")
            self.assertEqual(audit["text_edges_changed_pixel_ratio"], 0.0)
            self.assertEqual(audit["guardrail_failures"], [])

            text_edge_guard = audit_summary["guardrails"]["text_edges"]
            self.assertEqual(audit_summary["counts"]["text_edges_sharpened_files"], 0)
            self.assertEqual(audit_summary["counts"]["text_edges_skipped_files"], 1)
            self.assertEqual(text_edge_guard["applied_files"], 0)
            self.assertEqual(text_edge_guard["skipped_files"], 1)
            self.assertEqual(text_edge_guard["skip_reason_code_distribution"]["protected_scanline_or_ruled_background"], 1)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (
                "synthetic_ruled_table_background.png",
                str(input_dir),
                "source_relative_path",
                "source_sha256",
            ):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_diffuse_background_stain_lightens_while_protected_marks_stay_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-diffuse-stain-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "S001_safe_diffuse_background_stain.png": _diffuse_background_stain_page(),
                "S002_marginal_handwriting_note.png": _diffuse_background_stain_page("handwriting"),
                "S003_punctuation_i_dot.png": _diffuse_background_stain_page("punctuation"),
                "S004_page_number.png": _diffuse_background_stain_page("page_number"),
                "S005_ruled_table_lines.png": _diffuse_background_stain_page("ruled_table"),
                "S006_color_stamp.png": _diffuse_background_stain_page("stamp"),
                "S007_archival_texture_marks.png": _diffuse_background_stain_page("texture"),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            safe_original = pages["S001_safe_diffuse_background_stain.png"].convert("L")
            original_stain_mean = _mean_luma(safe_original, (178, 58, 223, 103))

            report = scan_batch(ScanConfig("synthetic-regression", "diffuse-background-stain", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_record = records["S001_safe_diffuse_background_stain.png"]
            safe_audit = safe_record["processing_audit"]
            with Image.open(process_dir / safe_record["output_relative_path"]) as processed_image:
                processed_safe = processed_image.convert("L")
                processed_stain_mean = _mean_luma(processed_safe, (178, 58, 223, 103))

            self.assertTrue(safe_record["background_stains_lightened"])
            self.assertIn("lighten_background_stains_conservative", safe_record["operations"])
            self.assertGreaterEqual(safe_audit["background_stains_delta"], 4.0)
            self.assertGreater(processed_stain_mean - original_stain_mean, 2.5)
            self.assertGreater(safe_audit["background_stains_changed_pixel_ratio"], 0.02)
            self.assertLessEqual(safe_audit["background_stains_changed_pixel_ratio"], 0.05)
            self.assertLessEqual(safe_audit["background_stains_candidate_pixel_ratio"], 0.05)
            self.assertEqual(safe_audit["guardrail_failures"], [])
            self.assertEqual(safe_audit["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(safe_audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed")

            protected_names = set(pages) - {"S001_safe_diffuse_background_stain.png"}
            for name in protected_names:
                record = records[name]
                self.assertFalse(record["background_stains_lightened"], name)
                self.assertIn("lighten_background_stains_noop", record["operations"], name)
                self.assertEqual(record["processing_audit"]["background_stains_changed_pixel_ratio"], 0.0, name)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [], name)
                with Image.open(process_dir / record["output_relative_path"]) as processed_image:
                    self.assertLessEqual(
                        _changed_ratio(pages[name], processed_image, (0, 0, pages[name].width, pages[name].height)),
                        0.001,
                        name,
                    )

            background_guard = audit_summary["guardrails"]["background_stains"]
            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["background_stains_skipped_files"], len(protected_names))
            self.assertEqual(background_guard["applied_files"], 1)
            self.assertEqual(background_guard["skipped_files"], len(protected_names))
            self.assertEqual(
                background_guard["reason_distribution"][
                    "background stain lightening applied: conservative localized low-contrast stains on light background"
                ],
                1,
            )
            self.assertIn(
                "background stain lightening skipped: color content, stamp, or annotation risk",
                background_guard["skip_reason_distribution"],
            )
            self.assertIn(
                "background stain lightening skipped: large stain or historical damage risk",
                background_guard["skip_reason_distribution"],
            )
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_faint_localized_thumbprint_stain_lightens_only_in_whitespace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-faint-thumbprint-stain-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "S001_safe_faint_thumbprint_stain.png": _faint_thumbprint_stain_page(),
                "S002_handwriting_stroke_in_smudge.png": _faint_thumbprint_stain_page("handwriting"),
                "S003_ruled_lines_in_smudge.png": _faint_thumbprint_stain_page("ruled_table"),
                "S004_color_mark_in_smudge.png": _faint_thumbprint_stain_page("color_mark"),
                "S005_photo_texture_in_smudge.png": _faint_thumbprint_stain_page("photo_texture"),
                "S006_edge_content_with_smudge.png": _faint_thumbprint_stain_page("edge_content"),
                "S007_dense_foreground_with_smudge.png": _faint_thumbprint_stain_page("dense_foreground"),
                "S008_pale_annotation_like_stroke.png": _faint_thumbprint_stain_page("pale_annotation"),
                "S009_subtle_ruled_table_adjacent_mark.png": _faint_thumbprint_stain_page("subtle_ruled_table"),
                "S010_textured_paper_detail_region.png": _faint_thumbprint_stain_page("subtle_texture"),
            }
            for name, image in pages.items():
                image.save(input_dir / name, dpi=(300, 300))

            safe_original = pages["S001_safe_faint_thumbprint_stain.png"].convert("L")
            original_stain_mean = _mean_luma(safe_original, (164, 68, 210, 108))

            report = scan_batch(ScanConfig("synthetic-regression", "faint-thumbprint-stain", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(lighten_background_stains=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            safe_record = records["S001_safe_faint_thumbprint_stain.png"]
            safe_audit = safe_record["processing_audit"]
            with Image.open(process_dir / safe_record["output_relative_path"]) as processed_image:
                processed_safe = processed_image.convert("L")
                processed_stain_mean = _mean_luma(processed_safe, (164, 68, 210, 108))

            self.assertTrue(safe_record["background_stains_lightened"])
            self.assertIn("lighten_background_stains_conservative", safe_record["operations"])
            self.assertIn("localized low-contrast stains", safe_record["background_stains_reason"])
            self.assertGreaterEqual(safe_audit["background_stains_delta"], 2.5)
            self.assertGreater(processed_stain_mean - original_stain_mean, 1.0)
            self.assertGreater(safe_audit["background_stains_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(safe_audit["background_stains_changed_pixel_ratio"], 0.022)
            self.assertLessEqual(safe_audit["background_stains_candidate_pixel_ratio"], 0.022)
            self.assertEqual(safe_audit["guardrail_failures"], [])
            self.assertEqual(safe_audit["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")

            protected_names = set(pages) - {"S001_safe_faint_thumbprint_stain.png"}
            for name in protected_names:
                record = records[name]
                self.assertFalse(record["background_stains_lightened"], name)
                self.assertIn("lighten_background_stains_noop", record["operations"], name)
                self.assertEqual(record["processing_audit"]["background_stains_changed_pixel_ratio"], 0.0, name)
                with Image.open(process_dir / record["output_relative_path"]) as processed_image:
                    self.assertLessEqual(
                        _changed_ratio(pages[name], processed_image, (0, 0, pages[name].width, pages[name].height)),
                        0.001,
                        name,
                    )

            background_guard = audit_summary["guardrails"]["background_stains"]
            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["background_stains_lightened_files"], 1)
            self.assertEqual(audit_summary["counts"]["background_stains_skipped_files"], len(protected_names))
            self.assertEqual(background_guard["applied_files"], 1)
            self.assertEqual(background_guard["skipped_files"], len(protected_names))
            self.assertGreaterEqual(len(background_guard["skip_reason_distribution"]), 3)
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            self.assertFalse(audit_summary["privacy"]["contains_paths"])
            self.assertFalse(audit_summary["privacy"]["contains_hashes"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_sparse_pale_typed_text_enhances_while_protected_pages_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-sparse-pale-typed-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()
            pages = {
                "A001_sparse_pale_typed.png": _sparse_pale_typed_page(),
                "A002_low_contrast_handwriting.png": _low_contrast_handwriting_page(),
                "A003_ruled_table_marks.png": _risk_table_page_number_annotation_page(),
                "A004_clear_text.png": _clear_text_page(),
            }
            source_bytes = {}
            for name, image in pages.items():
                source = input_dir / name
                image.save(source, dpi=(300, 300))
                source_bytes[name] = source.read_bytes()

            safe_original = pages["A001_sparse_pale_typed.png"].convert("L")
            original_text_mean = _mean_luma(safe_original, (46, 48, 210, 94))
            original_background_mean = _mean_luma(safe_original, (248, 48, 330, 94))

            report = scan_batch(ScanConfig("synthetic-regression", "sparse-pale-typed", input_dir, output_dir))
            manifest = process_images(
                report,
                input_dir,
                process_dir,
                ProcessingOptions(enhance_faded_text=True, workers=1),
            )
            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            records = {record["source_relative_path"]: record for record in manifest["files"]}

            for name, original_bytes in source_bytes.items():
                self.assertEqual((input_dir / name).read_bytes(), original_bytes)

            safe_record = records["A001_sparse_pale_typed.png"]
            safe_audit = safe_record["processing_audit"]
            with Image.open(process_dir / safe_record["output_relative_path"]) as processed_image:
                processed = processed_image.convert("L")
                processed_text_mean = _mean_luma(processed, (46, 48, 210, 94))
                processed_background_mean = _mean_luma(processed, (248, 48, 330, 94))
            self.assertTrue(safe_record["faded_text_enhanced"])
            self.assertEqual(safe_record["faded_text_reason_code"], "applied_stable_low_contrast_text")
            self.assertGreaterEqual(safe_audit["faded_text_delta"], 8.0)
            self.assertGreater(safe_audit["faded_text_changed_pixel_ratio"], 0.0)
            self.assertLessEqual(safe_audit["faded_text_changed_pixel_ratio"], 0.10)
            self.assertLessEqual(safe_audit["faded_text_candidate_pixel_ratio"], 0.16)
            self.assertGreater(original_text_mean - processed_text_mean, 0.25)
            self.assertLess(abs(original_background_mean - processed_background_mean), 0.5)
            self.assertEqual(safe_audit["guardrail_failures"], [])
            self.assertEqual(safe_audit["local_content_change_guard_action"], "passed")
            self.assertEqual(safe_audit["cumulative_change_guard_action"], "passed")
            self.assertEqual(safe_audit["combination_quality_guard_reason_code"], "safe_combination_passed")
            self.assertEqual(safe_audit["processed_output_safety_guard_reason_code"], "safe_processed_output_passed")

            expected_noop_codes = {
                "A002_low_contrast_handwriting.png": "protected_handwriting_marginalia_annotation",
                "A003_ruled_table_marks.png": "protected_texture_table_or_photo_region",
                "A004_clear_text.png": "protected_dark_foreground",
            }
            for name, expected_code in expected_noop_codes.items():
                record = records[name]
                self.assertFalse(record["faded_text_enhanced"], name)
                self.assertEqual(record["faded_text_reason_code"], expected_code, name)
                self.assertEqual(record["processing_audit"]["faded_text_changed_pixel_ratio"], 0.0, name)
                self.assertEqual(record["processing_audit"]["guardrail_failures"], [], name)

            faded_guard = audit_summary["guardrails"]["faded_text"]
            self.assertEqual(faded_guard["applied_files"], 1)
            self.assertEqual(faded_guard["skipped_files"], 3)
            self.assertEqual(
                faded_guard["reason_code_distribution"]["applied_stable_low_contrast_text"],
                1,
            )
            self.assertIn("protected_handwriting_marginalia_annotation", faded_guard["skip_reason_code_distribution"])
            self.assertIn("protected_texture_table_or_photo_region", faded_guard["skip_reason_code_distribution"])
            self.assertIn("protected_dark_foreground", faded_guard["skip_reason_code_distribution"])
            self.assertTrue(audit_summary["privacy"]["aggregate_only"])
            for forbidden in (*pages, str(input_dir), "source_relative_path", "source_sha256"):
                self.assertNotIn(forbidden, audit_summary_text)

    def test_quality_regression_reports_missing_operation_timing_code_without_private_rows(self) -> None:
        quality = _processing_quality_regression(
            {
                "summary": {
                    "total_files": 2,
                    "processed_files": 2,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "performance": {"operation_timings": {"deskew": {"enabled": True, "file_count": 2}}},
                },
                "files": [
                    {
                        "source_relative_path": "private_missing_timing_page.png",
                        "source_sha256": "a" * 64,
                        "processing_audit": {"cumulative_change_guard_checked": True},
                    }
                ],
            }
        )
        raw = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.assertEqual(quality["status"], "failed")
        integrity = quality["operation_timing_integrity"]
        self.assertTrue(integrity["aggregate_only"])
        self.assertEqual(integrity["status"], "missing")
        self.assertEqual(integrity["missing_code"], "missing_or_incomplete_processing_operation_timings")
        self.assertIn("auto_crop", integrity["missing_operations"])
        self.assertEqual(integrity["incomplete_operations"][0]["operation"], "deskew")
        self.assertIn("elapsed_seconds", integrity["incomplete_operations"][0]["missing_fields"])
        self.assertEqual(quality["slow_operations"][0]["operation"], "deskew")
        for forbidden in (
            "private_missing_timing_page.png",
            "source_relative_path",
            "source_sha256",
            "OCR TEXT",
            "a" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_quality_regression_aggregates_local_guard_reasons_without_private_rows(self) -> None:
        quality = _processing_quality_regression(
            {
                "summary": {
                    "total_files": 3,
                    "processed_files": 3,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "performance": {"operation_timings": _operation_timings_fixture()},
                },
                "files": [
                    {
                        "source_relative_path": "private_safe_stain_cleanup.png",
                        "source_sha256": "b" * 64,
                        "processing_audit": {
                            "background_stains_lightened": True,
                            "local_content_change_guard_checked": False,
                            "local_content_change_guard_reverted": False,
                            "local_content_change_guard_action": "passed",
                            "local_content_change_guard_reasons": [],
                            "combination_quality_guard_checked": False,
                            "combination_quality_guard_reverted": False,
                            "combination_quality_guard_action": "passed",
                            "combination_quality_guard_reason_code": "safe_combination_passed",
                            "combination_quality_guard_risk_tier": "low_risk_background",
                            "cumulative_change_score": 0.03,
                            "local_content_changed_ratio": 0.0,
                        },
                    },
                    {
                        "source_relative_path": "private_risky_text_damage.png",
                        "source_sha256": "c" * 64,
                        "processing_audit": {
                            "scanlines_lightened": True,
                            "local_content_change_guard_checked": True,
                            "local_content_change_guard_reverted": True,
                            "local_content_change_guard_action": "reverted_to_source",
                            "local_content_change_guard_reasons": [
                                "edge_content_changed_ratio",
                                "local_content_changed_ratio",
                            ],
                            "combination_quality_guard_checked": True,
                            "combination_quality_guard_reverted": True,
                            "combination_quality_guard_action": "reverted_to_source",
                            "combination_quality_guard_reason_code": "combined_change_too_large_reverted",
                            "combination_quality_guard_risk_tier": "high_risk_content",
                            "combination_quality_guard_reasons": ["cumulative_change_score"],
                            "processed_output_safety_guard_checked": True,
                            "processed_output_safety_guard_reverted": True,
                            "processed_output_safety_guard_action": "reverted_to_source",
                            "processed_output_safety_guard_reason_code": "processed_output_quality_reverted",
                            "processed_output_safety_guard_reasons": ["protected_foreground_weakening"],
                            "cumulative_change_score": 0.22,
                            "local_content_changed_ratio": 0.19,
                        },
                    },
                    {
                        "source_relative_path": "private_risky_text_sharpen.png",
                        "source_sha256": "d" * 64,
                        "processing_audit": {
                            "text_edges_sharpened": True,
                            "local_content_change_guard_checked": True,
                            "local_content_change_guard_reverted": False,
                            "local_content_change_guard_action": "passed",
                            "local_content_change_guard_reasons": [],
                            "combination_quality_guard_checked": True,
                            "combination_quality_guard_reverted": False,
                            "combination_quality_guard_action": "passed",
                            "combination_quality_guard_reason_code": "safe_combination_passed",
                            "combination_quality_guard_risk_tier": "low_risk_background",
                            "processed_output_safety_guard_checked": True,
                            "processed_output_safety_guard_reverted": False,
                            "processed_output_safety_guard_action": "passed",
                            "processed_output_safety_guard_reason_code": "safe_processed_output_passed",
                            "processed_output_safety_guard_reasons": [],
                            "cumulative_change_score": 0.05,
                            "local_content_changed_ratio": 0.02,
                        },
                    },
                ],
            }
        )
        raw = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.assertEqual(quality["status"], "pass")
        self.assertEqual(quality["counts"]["local_content_change_guard_checked_files"], 2)
        self.assertEqual(quality["counts"]["local_content_change_guard_skipped_files"], 1)
        self.assertEqual(quality["counts"]["local_content_change_guard_reverted_files"], 1)
        self.assertEqual(quality["counts"]["combination_quality_guard_checked_files"], 2)
        self.assertEqual(quality["counts"]["combination_quality_guard_reverted_files"], 1)
        self.assertEqual(quality["counts"]["processed_output_foreground_weakening_guard_reverted_files"], 1)
        local_guard = quality["local_content_change_guard"]
        self.assertTrue(local_guard["aggregate_only"])
        self.assertEqual(local_guard["checked_files"], 2)
        self.assertEqual(local_guard["skipped_files"], 1)
        self.assertEqual(local_guard["reverted_files"], 1)
        self.assertEqual(local_guard["reason_distribution"]["local_content_changed_ratio"], 1)
        self.assertEqual(local_guard["reason_distribution"]["edge_content_changed_ratio"], 1)
        combination_guard = quality["combination_quality_guard"]
        self.assertTrue(combination_guard["aggregate_only"])
        self.assertEqual(combination_guard["checked_files"], 2)
        self.assertEqual(combination_guard["reverted_files"], 1)
        self.assertEqual(combination_guard["reason_code_distribution"]["combined_change_too_large_reverted"], 1)
        processed_output_guard = quality["processed_output_safety_guard"]
        self.assertEqual(processed_output_guard["foreground_weakening_reverted_files"], 1)
        self.assertEqual(
            processed_output_guard["reason_distribution"]["protected_foreground_weakening"],
            1,
        )
        metric_signal = quality["guardrail_metric_signal"]
        self.assertTrue(metric_signal["aggregate_only"])
        self.assertEqual(metric_signal["metrics"]["cumulative_change_score"]["count"], 3)
        self.assertEqual(metric_signal["metrics"]["local_content_changed_ratio"]["max"], 0.19)
        self.assertEqual(quality["operation_timing_budget"]["status"], "pass")
        for forbidden in (
            "private_safe_stain_cleanup.png",
            "private_risky_text_damage.png",
            "private_risky_text_sharpen.png",
            "source_relative_path",
            "source_sha256",
            "b" * 64,
            "c" * 64,
            "d" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_quality_regression_reports_timing_budget_blocker_without_private_rows(self) -> None:
        operation_timings = _operation_timings_fixture()
        operation_timings["lighten_scanlines"] = {
            "enabled": True,
            "file_count": 2,
            "elapsed_seconds": 1.2,
            "files_per_minute": 100.0,
            "average_seconds_per_file": 0.6,
        }
        quality = _processing_quality_regression(
            {
                "summary": {
                    "total_files": 2,
                    "processed_files": 2,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "performance": {
                        "operation_timings": operation_timings,
                        "operation_timing_budget": {
                            "mode": "blocking",
                            "budgets_seconds_per_file": {"lighten_scanlines": 0.35},
                        },
                    },
                },
                "files": [
                    {
                        "source_relative_path": "private_slow_scanline_page.png",
                        "source_sha256": "e" * 64,
                        "processing_audit": {"local_content_change_guard_checked": True},
                    }
                ],
            }
        )
        raw = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.assertEqual(quality["status"], "failed")
        timing_budget = quality["operation_timing_budget"]
        self.assertTrue(timing_budget["aggregate_only"])
        self.assertEqual(timing_budget["status"], "failed")
        self.assertEqual(timing_budget["mode"], "blocking")
        self.assertEqual(timing_budget["budget_source"], "calibrated")
        self.assertEqual(timing_budget["blocker_code"], "processing_operation_timing_budget_exceeded")
        self.assertIsNone(timing_budget["diagnostic_code"])
        self.assertEqual(timing_budget["over_budget_operations"][0]["operation"], "lighten_scanlines")
        self.assertEqual(timing_budget["over_budget_operations"][0]["average_seconds_per_file"], 0.6)
        self.assertEqual(timing_budget["over_budget_operations"][0]["file_count"], 2)
        self.assertIn("lighten_scanlines", timing_budget["budgets_seconds_per_file"])
        for forbidden in (
            "private_slow_scanline_page.png",
            "source_relative_path",
            "source_sha256",
            "e" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_quality_regression_reports_default_timing_budget_as_diagnostic(self) -> None:
        quality = _processing_quality_regression(
            {
                "summary": {
                    "total_files": 20,
                    "processed_files": 20,
                    "failed_files": 0,
                    "skipped_files": 0,
                    "performance": {"operation_timings": _fixed_sample_operation_timings()},
                },
                "files": [{"processing_audit": {}} for _ in range(20)],
            }
        )

        self.assertEqual(quality["status"], "pass")
        timing_budget = quality["operation_timing_budget"]
        self.assertTrue(timing_budget["aggregate_only"])
        self.assertEqual(timing_budget["status"], "pass")
        self.assertEqual(timing_budget["mode"], "diagnostic")
        self.assertEqual(timing_budget["budget_source"], "diagnostic_defaults")
        self.assertIsNone(timing_budget["blocker_code"])
        self.assertEqual(timing_budget["diagnostic_code"], "processing_operation_timing_budget_diagnostic")
        self.assertEqual(
            [operation["operation"] for operation in timing_budget["over_budget_operations"]],
            ["auto_crop", "deskew", "despeckle", "lighten_background_stains", "clean_bleed_through"],
        )

    def test_synthetic_performance_signal_covers_conservative_operations_without_private_rows(self) -> None:
        comparison = _synthetic_performance_comparison_module()
        operation_timings = {
            operation: {
                "enabled": operation in BASE_OPERATION_NAMES,
                "file_count": 4 if operation in BASE_OPERATION_NAMES else 0,
                "elapsed_seconds": 0.04 if operation in BASE_OPERATION_NAMES else 0.0,
                "files_per_minute": 6000.0 if operation in BASE_OPERATION_NAMES else 0.0,
                "average_seconds_per_file": 0.01 if operation in BASE_OPERATION_NAMES else None,
            }
            for operation in REQUIRED_OPERATIONS
            if operation != "lighten_scanlines"
        }
        operation_timings["despeckle"]["backend_counts"] = {
            "numpy": 0,
            "fallback": 4,
            "not_applicable": 0,
            "unknown": 0,
        }
        benchmark = {
            "runs": [
                {
                    "processing": {
                        "operation_timings": operation_timings,
                        "private_path": "/private/archive/private_scan_page.png",
                        "source_sha256": "f" * 64,
                    }
                }
            ]
        }

        signal = comparison._operation_timing_regression_signal(benchmark)
        raw = json.dumps(signal, ensure_ascii=False, sort_keys=True)

        self.assertTrue(signal["aggregate_only"])
        self.assertEqual(signal["required_operations"], list(REQUIRED_OPERATIONS))
        self.assertFalse(signal["signal_available"])
        self.assertEqual(signal["missing_operations"], ["lighten_scanlines"])
        self.assertEqual(set(signal["operations"]), set(REQUIRED_OPERATIONS))
        self.assertEqual(
            signal["operations"]["lighten_scanlines"]["missing_reason"],
            "missing_from_benchmark_processing_operation_timings",
        )
        self.assertTrue(signal["operations"]["trim_dark_border"]["signal_available"])
        self.assertFalse(signal["operations"]["enhance_faded_text"]["enabled"])
        self.assertEqual(signal["operations"]["despeckle"]["backend_mode"], "fallback")
        for value in signal["privacy"].values():
            self.assertFalse(value)
        for forbidden in (
            "/private/archive",
            "private_scan_page.png",
            "source_sha256",
            "OCR TEXT",
            "thumbnail_data",
            '"findings": [',
            "f" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_synthetic_full_chain_guard_requires_enabled_operations_without_private_rows(self) -> None:
        comparison = _synthetic_performance_comparison_module()
        operation_timings = _operation_timings_fixture()
        operation_timings["lighten_fold_shadows"] = dict(operation_timings["lighten_fold_shadows"], enabled=False)
        benchmark = _full_chain_benchmark_fixture(operation_timings, elapsed_seconds=0.6, processed_files=4)

        guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(benchmark),
                    "full_chain_budget_signal": comparison._full_chain_budget_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                    "full_chain_quality_guard_signal": comparison._full_chain_quality_guard_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                    "private_path": "/private/archive/private_full_chain_page.png",
                    "source_sha256": "g" * 64,
                }
            ]
        )
        raw = json.dumps(guard, ensure_ascii=False, sort_keys=True)

        self.assertEqual(guard["status"], "failed")
        self.assertEqual(guard["code"], "full_chain_operation_not_enabled")
        self.assertEqual(guard["disabled_operations"], ["lighten_fold_shadows"])
        for value in guard["privacy"].values():
            self.assertFalse(value)
        for forbidden in (
            "/private/archive",
            "private_full_chain_page.png",
            "source_sha256",
            "g" * 64,
        ):
            self.assertNotIn(forbidden, raw)

    def test_synthetic_full_chain_guard_reports_budget_failures_without_private_rows(self) -> None:
        comparison = _synthetic_performance_comparison_module()
        benchmark = _full_chain_benchmark_fixture(
            _operation_timings_fixture(),
            elapsed_seconds=comparison.FULL_CHAIN_SYNTHETIC_BUDGET_SECONDS_PER_FILE * 4 + 0.4,
            processed_files=4,
        )
        budget_signal = comparison._full_chain_budget_signal({"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark)
        guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(benchmark),
                    "full_chain_budget_signal": budget_signal,
                    "full_chain_quality_guard_signal": comparison._full_chain_quality_guard_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                }
            ]
        )
        raw = json.dumps(guard, ensure_ascii=False, sort_keys=True)

        self.assertEqual(budget_signal["status"], "failed")
        self.assertEqual(budget_signal["code"], "full_chain_processing_budget_exceeded")
        self.assertEqual(guard["status"], "failed")
        self.assertEqual(guard["code"], "full_chain_processing_budget_exceeded")
        self.assertEqual(guard["budget_signal"]["over_budget_runs"][0]["processed_files"], 4)
        for forbidden in (
            "/private/archive",
            "private_full_chain_page.png",
            "source_sha256",
            "h" * 64,
        ):
            self.assertNotIn(forbidden, raw)

        passing_benchmark = _full_chain_benchmark_fixture(
            _operation_timings_fixture(),
            elapsed_seconds=0.8,
            processed_files=4,
        )
        passing_guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(
                        passing_benchmark
                    ),
                    "full_chain_budget_signal": comparison._full_chain_budget_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, passing_benchmark
                    ),
                    "full_chain_quality_guard_signal": comparison._full_chain_quality_guard_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, passing_benchmark
                    ),
                }
            ]
        )

        self.assertEqual(passing_guard["status"], "pass")
        self.assertEqual(passing_guard["budget_signal"]["status"], "pass")

    def test_synthetic_full_chain_guard_requires_quality_guard_signal_without_private_rows(self) -> None:
        comparison = _synthetic_performance_comparison_module()
        benchmark = _full_chain_benchmark_fixture(
            _operation_timings_fixture(),
            elapsed_seconds=0.8,
            processed_files=4,
        )
        quality_signal = comparison._full_chain_quality_guard_signal(
            {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
        )
        guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(benchmark),
                    "full_chain_budget_signal": comparison._full_chain_budget_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                    "full_chain_quality_guard_signal": quality_signal,
                    "private_path": "/private/archive/private_guard_quality_page.png",
                    "source_sha256": "i" * 64,
                }
            ]
        )
        raw = json.dumps(guard, ensure_ascii=False, sort_keys=True)

        self.assertEqual(quality_signal["status"], "pass")
        self.assertEqual(quality_signal["runs"][0]["counts"]["combination_quality_guard_checked_files"], 4)
        self.assertIn("guardrail_metric_signal", quality_signal["runs"][0])
        self.assertIn("algorithm_metrics", quality_signal["runs"][0])
        self.assertEqual(guard["status"], "pass")
        self.assertEqual(guard["quality_guard_signal"]["status"], "pass")
        for forbidden in (
            "/private/archive",
            "private_guard_quality_page.png",
            "source_sha256",
            "i" * 64,
        ):
            self.assertNotIn(forbidden, raw)

        missing_quality_guard = comparison._full_chain_regression_guard(
            [
                {
                    "id": comparison.FULL_CHAIN_VARIANT_ID,
                    "operation_timing_regression_signal": comparison._operation_timing_regression_signal(benchmark),
                    "full_chain_budget_signal": comparison._full_chain_budget_signal(
                        {"id": comparison.FULL_CHAIN_VARIANT_ID}, benchmark
                    ),
                }
            ]
        )
        self.assertEqual(missing_quality_guard["status"], "failed")
        self.assertEqual(missing_quality_guard["code"], "missing_full_chain_quality_guard_signal")


BASE_FLAGS = ("--deskew", "--trim-dark-border", "--scanner-gutter-trim", "--auto-crop", "--despeckle")
BASE_OPERATION_NAMES = ("auto_crop", "deskew", "trim_dark_border", "scanner_gutter_trim", "despeckle")
CONSERVATIVE_REPAIR_FLAGS = (
    "--normalize-tones",
    "--normalize-paper-color-cast",
    "--lighten-edge-shadow",
    "--lighten-corner-shadows",
    "--lighten-background-stains",
    "--lighten-fold-shadows",
    "--level-illumination-gradient",
    "--clean-bleed-through",
    "--lighten-scanlines",
    "--enhance-faded-text",
    "--sharpen-text-edges",
)
REQUIRED_OPERATIONS = (
    "deskew",
    "trim_dark_border",
    "scanner_gutter_trim",
    "auto_crop",
    "despeckle",
    "normalize_tones",
    "normalize_paper_color_cast",
    "lighten_edge_shadow",
    "lighten_corner_shadows",
    "lighten_background_stains",
    "lighten_fold_shadows",
    "level_illumination_gradient",
    "clean_bleed_through",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
)
CONSERVATIVE_REPAIR_OPERATIONS = (
    "normalize_tones",
    "normalize_paper_color_cast",
    "lighten_edge_shadow",
    "lighten_corner_shadows",
    "lighten_background_stains",
    "lighten_fold_shadows",
    "level_illumination_gradient",
    "clean_bleed_through",
    "lighten_scanlines",
    "enhance_faded_text",
    "sharpen_text_edges",
)


def _full_chain_options() -> ProcessingOptions:
    return ProcessingOptions(
        auto_crop=True,
        deskew=True,
        trim_dark_border=True,
        scanner_gutter_trim=True,
        despeckle=True,
        normalize_tones=True,
        normalize_paper_color_cast=True,
        lighten_edge_shadow=True,
        lighten_corner_shadows=True,
        lighten_background_stains=True,
        lighten_fold_shadows=True,
        level_illumination_gradient=True,
        clean_bleed_through=True,
        lighten_scanlines=True,
        enhance_faded_text=True,
        sharpen_text_edges=True,
        despeckle_backend="fallback",
        workers=1,
    )


def _mild_warm_scanner_cast_page(variant: str) -> Image.Image:
    image = Image.new("RGB", (240, 180), (246, 243, 232))
    draw = ImageDraw.Draw(image)
    for y in (42, 66, 90):
        draw.rectangle((36, y, 128, y + 3), fill=(58, 58, 58))
    draw.rectangle((182, 24, 196, 30), fill=(72, 72, 72))
    if variant == "stamp":
        draw.ellipse((142, 96, 202, 154), outline=(190, 28, 28), width=4)
    elif variant == "annotation":
        draw.line((28, 128, 150, 138), fill=(42, 84, 190), width=3)
    elif variant != "safe":
        raise ValueError(f"unknown mild warm scanner cast variant: {variant}")
    return image


def _mild_mixed_scanner_cast_page(variant: str) -> Image.Image:
    image = Image.new("RGB", (240, 180), (246, 246, 246))
    pixels = image.load()
    warm = (248, 243, 233)
    cool = (235, 241, 248)
    if variant == "colored_stationery":
        warm = (240, 236, 226)
        cool = (228, 236, 242)
    elif variant == "aged_paper":
        warm = (242, 235, 220)
        cool = (230, 235, 238)
    elif variant == "illumination":
        warm = (244, 239, 229)
        cool = (236, 242, 249)
    for y in range(image.height):
        for x in range(image.width):
            position = x / max(1, image.width - 1)
            pixels[x, y] = tuple(round(warm[index] * (1.0 - position) + cool[index] * position) for index in range(3))

    draw = ImageDraw.Draw(image)
    for y in (42, 66, 90):
        draw.rectangle((36, y, 128, y + 3), fill=(58, 58, 58))
    draw.rectangle((182, 24, 196, 30), fill=(72, 72, 72))
    if variant == "stamp":
        draw.ellipse((142, 96, 202, 154), outline=(190, 28, 28), width=4)
    elif variant == "photo_texture":
        for y in range(22, 104):
            for x in range(148, 222):
                red_value = 74 + ((x * 5 + y * 3) % 86)
                green_value = 82 + ((x * 7 + y * 2) % 72)
                blue_value = 92 + ((x * 3 + y * 11) % 84)
                pixels[x, y] = (red_value, green_value, blue_value)
    elif variant == "ruled_edge":
        for y in range(28, 154, 18):
            draw.line((10, y, 230, y), fill=(82, 82, 82), width=1)
        for x in range(28, 216, 38):
            draw.line((x, 24, x, 160), fill=(88, 88, 88), width=1)
        draw.line((2, 146, 20, 166), fill=(34, 34, 34), width=2)
    elif variant == "large_stain":
        overlay = Image.new("RGB", image.size, (238, 232, 214))
        mask = Image.new("L", image.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((40, 30, 210, 150), fill=95)
        mask = mask.filter(ImageFilter.GaussianBlur(14))
        image = Image.composite(overlay, image, mask)
    elif variant == "illumination":
        overlay = Image.new("RGB", image.size, (228, 228, 222))
        mask = Image.new("L", image.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rectangle((0, 0, 88, image.height), fill=110)
        mask = mask.filter(ImageFilter.GaussianBlur(18))
        image = Image.composite(overlay, image, mask)
    elif variant == "pale_ruled":
        for y in range(24, 164, 18):
            draw.line((8, y, 232, y), fill=(232, 232, 226), width=1)
    elif variant == "pale_table":
        for x in range(30, 220, 32):
            draw.line((x, 26, x, 154), fill=(232, 232, 226), width=1)
        for y in range(26, 155, 22):
            draw.line((26, y, 220, y), fill=(232, 232, 226), width=1)
    elif variant in {"colored_stationery", "aged_paper", "illumination"}:
        pass
    elif variant != "safe":
        raise ValueError(f"unknown mild mixed scanner cast variant: {variant}")
    return image


def _benchmark_combo(root: Path, input_dir: Path, label: str, *flags: str) -> dict[str, object]:
    output_dir = root / f"benchmark-{label}"
    process_dir = root / f"processed-{label}"
    flag_set = set(flags)
    return run_benchmark(
        argparse.Namespace(
            input=input_dir,
            out=output_dir,
            process_out=process_dir,
            project="synthetic-regression",
            batch="synthetic-regression",
            workers_list=[1],
            repeats=1,
            scan_only=False,
            auto_crop="--auto-crop" in flag_set,
            deskew="--deskew" in flag_set,
            trim_dark_border="--trim-dark-border" in flag_set,
            scanner_gutter_trim="--scanner-gutter-trim" in flag_set,
            despeckle="--despeckle" in flag_set,
            normalize_tones="--normalize-tones" in flag_set,
            normalize_paper_color_cast="--normalize-paper-color-cast" in flag_set,
            lighten_edge_shadow="--lighten-edge-shadow" in flag_set,
            lighten_corner_shadows="--lighten-corner-shadows" in flag_set,
            lighten_background_stains="--lighten-background-stains" in flag_set,
            lighten_fold_shadows="--lighten-fold-shadows" in flag_set,
            level_illumination_gradient="--level-illumination-gradient" in flag_set,
            clean_bleed_through="--clean-bleed-through" in flag_set,
            lighten_scanlines="--lighten-scanlines" in flag_set,
            enhance_faded_text="--enhance-faded-text" in flag_set,
            sharpen_text_edges="--sharpen-text-edges" in flag_set,
            reuse_scan_measurements=False,
            despeckle_backend="fallback",
            min_dpi=None,
            name_pattern=None,
            manifest_csv=None,
            rules_profile=None,
        )
    )


def _single_quality(payload: dict[str, object]) -> dict[str, object]:
    runs = payload["runs"]
    assert isinstance(runs, list)
    run = runs[0]
    assert isinstance(run, dict)
    processing = run["processing"]
    assert isinstance(processing, dict)
    quality = processing["quality_regression"]
    assert isinstance(quality, dict)
    return quality


def _assert_required_metrics_present(
    testcase: unittest.TestCase, quality: dict[str, object]
) -> None:
    algorithm_metrics = quality["algorithm_metrics"]
    assert isinstance(algorithm_metrics, dict)
    expected = {
        "despeckle": ("pixel_ratio",),
        "normalize_tones": ("background_delta", "contrast_delta", "changed_pixel_ratio"),
        "normalize_paper_color_cast": (
            "delta",
            "brightness_delta",
            "changed_pixel_ratio",
            "candidate_pixel_ratio",
        ),
        "lighten_edge_shadow": ("delta", "changed_pixel_ratio"),
        "lighten_corner_shadows": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "lighten_background_stains": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "lighten_fold_shadows": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "level_illumination_gradient": (
            "correction_delta",
            "changed_pixel_ratio",
            "candidate_pixel_ratio",
        ),
        "clean_bleed_through": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "lighten_scanlines": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
        "enhance_faded_text": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio", "candidate_text_ratio"),
        "sharpen_text_edges": ("delta", "changed_pixel_ratio", "candidate_pixel_ratio"),
    }
    for operation, metric_names in expected.items():
        metrics = algorithm_metrics[operation]["metrics"]
        for metric_name in metric_names:
            testcase.assertIn(metric_name, metrics, operation)
            testcase.assertEqual(metrics[metric_name]["count"], 6, f"{operation}.{metric_name}")


def _assert_operation_timing_signal(testcase: unittest.TestCase, quality: dict[str, object]) -> None:
    integrity = quality["operation_timing_integrity"]
    testcase.assertTrue(integrity["aggregate_only"])
    testcase.assertEqual(integrity["status"], "pass")
    testcase.assertIsNone(integrity["missing_code"])
    testcase.assertEqual(integrity["missing_operations"], [])
    testcase.assertEqual(integrity["incomplete_operations"], [])
    timing_budget = quality["operation_timing_budget"]
    testcase.assertTrue(timing_budget["aggregate_only"])
    testcase.assertEqual(timing_budget["status"], "pass")
    testcase.assertIsNone(timing_budget["blocker_code"])
    testcase.assertEqual(timing_budget["over_budget_operations"], [])
    testcase.assertIn("lighten_scanlines", timing_budget["budgets_seconds_per_file"])
    slow_operations = quality["slow_operations"]
    testcase.assertGreaterEqual(len(slow_operations), 3)
    previous_average = None
    for summary in slow_operations:
        testcase.assertIn(summary["operation"], REQUIRED_OPERATIONS)
        testcase.assertIn("enabled", summary)
        testcase.assertGreaterEqual(summary["file_count"], 0)
        testcase.assertIsNotNone(summary["average_seconds_per_file"])
        testcase.assertIsNotNone(summary["files_per_minute"])
        if previous_average is not None:
            testcase.assertGreaterEqual(previous_average, summary["average_seconds_per_file"])
        previous_average = summary["average_seconds_per_file"]


def _assert_local_content_guard_signal(testcase: unittest.TestCase, quality: dict[str, object]) -> None:
    counts = quality["counts"]
    local_guard = quality["local_content_change_guard"]
    testcase.assertTrue(local_guard["aggregate_only"])
    testcase.assertEqual(counts["local_content_change_guard_checked_files"], 2)
    testcase.assertEqual(counts["local_content_change_guard_skipped_files"], 4)
    testcase.assertEqual(local_guard["checked_files"], 2)
    testcase.assertEqual(local_guard["skipped_files"], 4)
    testcase.assertEqual(local_guard["reverted_files"], 0)
    testcase.assertEqual(local_guard["reason_distribution"], {})


def _assert_full_chain_quality_guard_signal(testcase: unittest.TestCase, payload: dict[str, object]) -> None:
    quality = _single_quality(payload)
    counts = quality["counts"]
    testcase.assertEqual(counts["combination_quality_guard_checked_files"], 6)
    testcase.assertEqual(counts["processed_output_safety_guard_checked_files"], 6)
    testcase.assertIn("processed_output_foreground_weakening_guard_reverted_files", counts)
    combination_guard = quality["combination_quality_guard"]
    testcase.assertTrue(combination_guard["aggregate_only"])
    testcase.assertEqual(combination_guard["checked_files"], 6)
    testcase.assertEqual(combination_guard["reverted_files"], 0)
    testcase.assertEqual(sum(combination_guard["reason_code_distribution"].values()), 6)
    testcase.assertGreaterEqual(
        combination_guard["reason_code_distribution"].get("safe_combination_passed", 0),
        3,
    )
    processed_output_guard = quality["processed_output_safety_guard"]
    testcase.assertTrue(processed_output_guard["aggregate_only"])
    testcase.assertEqual(processed_output_guard["checked_files"], 6)
    testcase.assertEqual(processed_output_guard["reverted_files"], 0)
    testcase.assertEqual(
        processed_output_guard["reason_code_distribution"]["safe_processed_output_passed"],
        6,
    )
    metric_signal = quality["guardrail_metric_signal"]
    testcase.assertTrue(metric_signal["aggregate_only"])
    for metric_name in (
        "cumulative_change_score",
        "cumulative_change_pixel_ratio",
        "local_content_changed_ratio",
        "edge_content_changed_ratio",
        "processed_output_dark_pixel_lift_ratio",
    ):
        testcase.assertIn(metric_name, metric_signal["metrics"])
        testcase.assertEqual(metric_signal["metrics"][metric_name]["count"], 6, metric_name)


def _assert_algorithm_thresholds(testcase: unittest.TestCase, quality: dict[str, object]) -> None:
    testcase.assertEqual(quality["threshold_violations"], [])
    thresholds = quality["thresholds"]
    algorithm_metrics = quality["algorithm_metrics"]
    checks = {
        ("deskew", "abs_angle_degrees"): "max_deskew_degrees",
        ("trim_dark_border", "max_trim_margin_ratio"): "max_trim_margin_ratio",
        ("scanner_gutter_trim", "max_trim_margin_ratio"): "max_trim_margin_ratio",
        ("auto_crop", "crop_ratio"): "max_crop_ratio",
        ("despeckle", "pixel_ratio"): "max_despeckle_pixel_ratio",
        ("normalize_tones", "background_delta"): "max_tone_background_delta",
        ("normalize_tones", "contrast_delta"): "max_tone_contrast_delta",
        ("normalize_tones", "changed_pixel_ratio"): "max_tone_changed_pixel_ratio",
        ("normalize_paper_color_cast", "delta"): "max_paper_color_cast_delta",
        ("normalize_paper_color_cast", "brightness_delta"): "max_paper_color_cast_brightness_delta",
        ("normalize_paper_color_cast", "changed_pixel_ratio"): "max_paper_color_cast_changed_pixel_ratio",
        ("normalize_paper_color_cast", "candidate_pixel_ratio"): "max_paper_color_cast_candidate_pixel_ratio",
        ("lighten_edge_shadow", "changed_pixel_ratio"): "max_edge_shadow_changed_pixel_ratio",
        ("lighten_corner_shadows", "changed_pixel_ratio"): "max_corner_shadows_changed_pixel_ratio",
        ("lighten_corner_shadows", "candidate_pixel_ratio"): "max_corner_shadows_candidate_pixel_ratio",
        ("lighten_background_stains", "changed_pixel_ratio"): "max_background_stains_changed_pixel_ratio",
        ("lighten_background_stains", "candidate_pixel_ratio"): "max_background_stains_candidate_pixel_ratio",
        ("lighten_fold_shadows", "changed_pixel_ratio"): "max_fold_shadows_changed_pixel_ratio",
        ("lighten_fold_shadows", "candidate_pixel_ratio"): "max_fold_shadows_candidate_pixel_ratio",
        ("level_illumination_gradient", "changed_pixel_ratio"): "max_illumination_gradient_changed_pixel_ratio",
        ("level_illumination_gradient", "candidate_pixel_ratio"): "max_illumination_gradient_candidate_pixel_ratio",
        ("clean_bleed_through", "changed_pixel_ratio"): "max_bleed_through_changed_pixel_ratio",
        ("clean_bleed_through", "candidate_pixel_ratio"): "max_bleed_through_candidate_pixel_ratio",
        ("lighten_scanlines", "changed_pixel_ratio"): "max_scanlines_changed_pixel_ratio",
        ("lighten_scanlines", "candidate_pixel_ratio"): "max_scanlines_candidate_pixel_ratio",
        ("enhance_faded_text", "changed_pixel_ratio"): "max_faded_text_changed_pixel_ratio",
        ("enhance_faded_text", "candidate_pixel_ratio"): "max_faded_text_candidate_pixel_ratio",
        ("sharpen_text_edges", "changed_pixel_ratio"): "max_text_edges_changed_pixel_ratio",
        ("sharpen_text_edges", "candidate_pixel_ratio"): "max_text_edges_candidate_pixel_ratio",
    }
    for (operation, metric_name), threshold_name in checks.items():
        observed = algorithm_metrics[operation]["metrics"][metric_name]["max"]
        if observed is not None:
            testcase.assertLessEqual(observed, thresholds[threshold_name], f"{operation}.{metric_name}")


def _operation_timings_fixture() -> dict[str, dict[str, object]]:
    return {
        operation: {
            "enabled": True,
            "file_count": 3,
            "elapsed_seconds": 0.03,
            "files_per_minute": 6000.0,
            "average_seconds_per_file": 0.01,
        }
        for operation in REQUIRED_OPERATIONS
    }


def _full_chain_benchmark_fixture(
    operation_timings: dict[str, dict[str, object]], *, elapsed_seconds: float, processed_files: int
) -> dict[str, object]:
    return {
        "runs": [
            {
                "run_index": 1,
                "requested_workers": 1,
                "processing": {
                    "processed_files": processed_files,
                    "elapsed_seconds": elapsed_seconds,
                    "operation_timings": operation_timings,
                    "quality_regression": _full_chain_quality_regression_fixture(processed_files),
                    "private_path": "/private/archive/private_full_chain_page.png",
                    "source_sha256": "h" * 64,
                },
            }
        ]
    }


def _full_chain_quality_regression_fixture(processed_files: int) -> dict[str, object]:
    return {
        "aggregate_only": True,
        "status": "pass",
        "counts": {
            "processed_files": processed_files,
            "failed_files": 0,
            "guardrail_failed_files": 0,
            "cumulative_change_guard_checked_files": processed_files,
            "cumulative_change_guard_reverted_files": 0,
            "local_content_change_guard_checked_files": processed_files,
            "local_content_change_guard_reverted_files": 0,
            "combination_quality_guard_checked_files": processed_files,
            "combination_quality_guard_reverted_files": 0,
            "processed_output_safety_guard_checked_files": processed_files,
            "processed_output_safety_guard_reverted_files": 0,
            "processed_output_foreground_weakening_guard_reverted_files": 0,
        },
        "local_content_change_guard": {
            "aggregate_only": True,
            "checked_files": processed_files,
            "skipped_files": 0,
            "reverted_files": 0,
            "warning_files": 0,
            "reason_distribution": {},
        },
        "combination_quality_guard": {
            "aggregate_only": True,
            "checked_files": processed_files,
            "skipped_files": 0,
            "reverted_files": 0,
            "warning_files": 0,
            "low_confidence_original_files": 0,
            "reason_code_distribution": {"safe_combination_passed": processed_files},
            "risk_tier_distribution": {"low_risk_background": processed_files},
            "reason_distribution": {},
        },
        "processed_output_safety_guard": {
            "aggregate_only": True,
            "checked_files": processed_files,
            "skipped_files": 0,
            "reverted_files": 0,
            "warning_files": 0,
            "washout_reverted_files": 0,
            "clipping_reverted_files": 0,
            "foreground_loss_reverted_files": 0,
            "foreground_weakening_reverted_files": 0,
            "reason_code_distribution": {"safe_processed_output_passed": processed_files},
            "reason_distribution": {},
        },
        "guardrail_metric_signal": {
            "aggregate_only": True,
            "metrics": {
                "cumulative_change_score": {"count": processed_files, "max": 0.04},
                "cumulative_change_pixel_ratio": {"count": processed_files, "max": 0.01},
                "local_content_changed_ratio": {"count": processed_files, "max": 0.0},
                "edge_content_changed_ratio": {"count": processed_files, "max": 0.0},
                "processed_output_dark_pixel_lift_ratio": {"count": processed_files, "max": 0.0},
            },
        },
        "algorithm_metrics": {
            operation: {
                "enabled": True,
                "changed_files": 0,
                "file_count": processed_files,
                "elapsed_seconds": 0.01,
                "files_per_minute": 6000.0,
                "metrics": {},
            }
            for operation in REQUIRED_OPERATIONS
        },
        "threshold_violations": [],
    }


def _fixed_sample_operation_timings() -> dict[str, dict[str, object]]:
    average_seconds_by_operation = {
        "auto_crop": 0.21,
        "deskew": 0.16,
        "trim_dark_border": 0.01,
        "scanner_gutter_trim": 0.01,
        "despeckle": 0.26,
        "normalize_tones": 0.01,
        "normalize_paper_color_cast": 0.01,
        "lighten_edge_shadow": 0.01,
        "lighten_corner_shadows": 0.01,
        "lighten_background_stains": 0.36,
        "lighten_fold_shadows": 0.01,
        "level_illumination_gradient": 0.01,
        "clean_bleed_through": 0.36,
        "lighten_scanlines": 0.01,
        "enhance_faded_text": 0.01,
        "sharpen_text_edges": 0.01,
    }
    return {
        operation: {
            "enabled": True,
            "file_count": 20,
            "elapsed_seconds": round(average_seconds * 20, 6),
            "files_per_minute": round(60 / average_seconds, 6),
            "average_seconds_per_file": average_seconds,
        }
        for operation, average_seconds in average_seconds_by_operation.items()
    }


def _synthetic_performance_comparison_module() -> object:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_synthetic_performance_comparison.py"
    spec = importlib.util.spec_from_file_location("run_synthetic_performance_comparison", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _synthetic_pages(input_dir: Path) -> None:
    _text_page().save(input_dir / "private_default_page.png", dpi=(300, 300))
    _edge_shadow_page().save(input_dir / "private_edge_page.png", dpi=(300, 300))
    _stain_page().save(input_dir / "private_stain_page.png", dpi=(300, 300))
    _scanline_page().save(input_dir / "private_scanline_page.png", dpi=(300, 300))
    _faded_text_page().save(input_dir / "private_faded_text_page.png", dpi=(300, 300))
    _blurred_text_page().save(input_dir / "private_blurred_text_page.png", dpi=(300, 300))


def _shallow_stable_text_page() -> Image.Image:
    image = Image.new("RGB", (240, 320), (246, 246, 246))
    draw = ImageDraw.Draw(image)
    for y in range(50, 230, 30):
        draw.rectangle((45, y, 195, y + 3), fill=(50, 50, 50))
    return image


def _safe_full_chain_combination_page() -> Image.Image:
    image = _scanline_page().resize((240, 180))
    draw = ImageDraw.Draw(image)
    for y in range(86, 130, 14):
        draw.line((58, y, 174, y), fill=(202, 202, 202), width=2)
    draw.ellipse((165, 28, 210, 58), fill=(222, 222, 222))
    image.putpixel((24, 24), (0, 0, 0))
    return image


def _combined_retouch_guard_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (220, 160), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    draw.ellipse((132, 70, 184, 118), fill=(224, 224, 220))
    draw.line((72, 124, 168, 124), fill=(234, 234, 230), width=2)

    if variant == "safe":
        return image
    if variant == "faint_text":
        for y in (42, 62, 82):
            draw.rectangle((40, y, 132, y + 4), fill=(205, 205, 201))
    elif variant == "page_number":
        draw.rectangle((174, 12, 200, 24), fill=(205, 205, 201))
    elif variant == "table_lines":
        for y in (54, 76, 98):
            draw.line((40, y, 174, y), fill=(205, 205, 201), width=2)
        for x in (76, 124, 172):
            draw.line((x, 44, x, 106), fill=(205, 205, 201), width=2)
    elif variant == "stamp":
        draw.ellipse((86, 42, 142, 96), outline=(180, 28, 28), width=4)
    elif variant == "marginal_note":
        draw.line((6, 46, 30, 58, 10, 70, 34, 82), fill=(205, 205, 201), width=2)
    elif variant == "edge_mark":
        draw.rectangle((4, 120, 24, 132), fill=(205, 205, 201))
    else:
        raise ValueError(f"unsupported variant: {variant}")
    return image


def _subtle_diagonal_edge_shadow_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (260, 180), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    for y in range(image.height):
        band_width = 12 + int(round(y * 0.10))
        for x in range(band_width):
            shade = min(239, 222 + int(x * 0.6) + int(y / image.height * 2))
            image.putpixel((x, y), (shade, shade, shade - 2))
    for y in (46, 68, 90):
        draw.rectangle((76, y, 184, y + 5), fill=(35, 35, 35))

    if variant == "edge_handwriting":
        draw.line((2, 120, 20, 132, 8, 148, 30, 158), fill=(55, 55, 55), width=2)
    elif variant == "page_number":
        draw.rectangle((116, 160, 144, 169), fill=(42, 42, 42))
    elif variant == "ruled_table":
        draw.line((0, 126, 238, 126), fill=(55, 55, 55), width=2)
        draw.line((12, 112, 12, 150), fill=(55, 55, 55), width=2)
    elif variant == "stamp":
        draw.ellipse((3, 30, 42, 70), outline=(180, 30, 30), width=3)
    elif variant == "texture":
        for x in range(24, 246, 6):
            for y in range(12, 168, 6):
                shade = 112 + ((x * 5 + y * 7) % 72)
                draw.rectangle((x, y, x + 2, y + 2), fill=(shade, shade, shade))
    elif variant == "archival_edge_mark":
        draw.rectangle((2, 104, 16, 154), fill=(58, 58, 58))
    elif variant != "safe":
        raise ValueError(f"unsupported variant: {variant}")
    return image


def _subtle_diagonal_fold_shadow_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (240, 180), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    draw.line((70, 20, 210, 160), fill=(237, 237, 233), width=3)
    draw.rectangle((28, 118, 68, 122), fill=(42, 42, 42))
    draw.rectangle((150, 42, 190, 46), fill=(42, 42, 42))

    if variant == "sparse_text_crossing":
        font = ImageFont.load_default()
        draw.text((112, 74), "REF", fill=(42, 42, 42), font=font)
        draw.text((72, 114), "42", fill=(42, 42, 42), font=font)
    elif variant == "handwriting_bridge":
        draw.line((112, 78, 154, 82), fill=(45, 45, 45), width=3)
        draw.line((72, 118, 104, 122), fill=(45, 45, 45), width=3)
    elif variant == "repeated_ruled_segments":
        draw.rectangle((112, 78, 154, 82), fill=(42, 42, 42))
        draw.rectangle((72, 118, 104, 122), fill=(42, 42, 42))
    elif variant == "dense_typed_text":
        font = ImageFont.load_default()
        for row, text in enumerate(("ARCHIVE", "SCAN", "INDEX", "COPY", "TOTAL")):
            draw.text((78, 58 + row * 14), text, fill=(42, 42, 42), font=font)
    elif variant == "handwriting":
        draw.line((92, 44, 152, 104), fill=(45, 45, 45), width=2)
    elif variant == "page_number":
        draw.rectangle((154, 104, 168, 112), fill=(35, 35, 35))
    elif variant == "ruled_table":
        for y in (70, 96, 122):
            draw.line((44, y, 196, y), fill=(35, 35, 35), width=2)
        draw.line((128, 58, 128, 132), fill=(35, 35, 35), width=2)
    elif variant == "stamp":
        draw.ellipse((92, 56, 142, 106), outline=(186, 24, 24), width=3)
    elif variant == "texture":
        for x in range(52, 190, 6):
            for y in range(34, 144, 6):
                shade = 104 + ((x * 5 + y * 7) % 82)
                draw.rectangle((x, y, x + 2, y + 2), fill=(shade, shade, shade))
    elif variant == "archival_edge_mark":
        draw.rectangle((2, 102, 18, 154), fill=(48, 48, 48))
    elif variant != "safe":
        raise ValueError(f"unsupported variant: {variant}")
    return image


def _safe_compact_dust_cluster_points() -> tuple[tuple[int, int], ...]:
    return ((128, 82), (129, 82), (130, 82), (128, 83), (129, 83), (130, 83))


def _safe_compact_dust_cluster_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (246, 246, 244))
    draw = ImageDraw.Draw(image)
    for y in (42, 68, 94):
        draw.rectangle((44, y, 112, y + 3), fill=(58, 58, 58))
    for point in _safe_compact_dust_cluster_points():
        image.putpixel(point, (18, 18, 18))
    return image


def _safe_scattered_pale_dust_points() -> tuple[tuple[int, int], ...]:
    return (
        (142, 40),
        (188, 48),
        (220, 62),
        (174, 74),
        (136, 92),
        (68, 116),
        (206, 116),
        (124, 126),
        (158, 138),
        (230, 142),
        (88, 144),
        (236, 34),
    )


def _safe_scattered_pale_dust_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (246, 246, 244))
    draw = ImageDraw.Draw(image)
    for y in (42, 68, 94):
        draw.rectangle((44, y, 112, y + 3), fill=(58, 58, 58))
    for point in _safe_scattered_pale_dust_points():
        image.putpixel(point, (228, 228, 225))
    return image


def _safe_compact_pale_dust_cluster_points() -> tuple[tuple[int, int], ...]:
    return (
        (168, 70),
        (169, 70),
        (168, 71),
        (169, 71),
        (206, 124),
        (207, 124),
        (206, 125),
        (207, 125),
    )


def _safe_compact_pale_dust_cluster_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (246, 246, 244))
    draw = ImageDraw.Draw(image)
    for y in (42, 68, 94):
        draw.rectangle((44, y, 112, y + 3), fill=(58, 58, 58))
    for point in _safe_compact_pale_dust_cluster_points():
        image.putpixel(point, (226, 224, 210))
    return image


def _safe_clean_page_faint_dust_speck_points() -> tuple[tuple[int, int], ...]:
    points = []
    for x, y in ((80, 60), (150, 90), (210, 130)):
        points.extend(((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)))
    return tuple(points)


def _safe_clean_page_faint_dust_speck_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (246, 246, 244))
    for point in _safe_clean_page_faint_dust_speck_points():
        image.putpixel(point, (228, 228, 224))
    return image


def _high_density_pale_texture_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (246, 246, 244))
    draw = ImageDraw.Draw(image)
    for y in (42, 68, 94):
        draw.rectangle((44, y, 112, y + 3), fill=(58, 58, 58))
    for y in range(24, 158, 24):
        for x in range(132, 238, 24):
            for offset_x, offset_y in ((0, 0), (1, 0), (0, 1), (1, 1)):
                image.putpixel((x + offset_x, y + offset_y), (226, 224, 210))
    return image


def _pale_dotted_leader_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (246, 246, 244))
    draw = ImageDraw.Draw(image)
    draw.rectangle((34, 62, 82, 65), fill=(58, 58, 58))
    draw.rectangle((204, 62, 236, 65), fill=(58, 58, 58))
    for x in range(90, 200, 10):
        image.putpixel((x, 64), (226, 224, 210))
    return image


def _pale_punctuation_like_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (246, 246, 244))
    draw = ImageDraw.Draw(image)
    for x in range(52, 212, 18):
        draw.rectangle((x, 76, x + 1, 88), fill=(62, 62, 62))
        image.putpixel((x, 70), (226, 224, 210))
    return image


def _low_contrast_halftone_texture_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (246, 246, 244))
    draw = ImageDraw.Draw(image)
    for y in (42, 68, 94):
        draw.rectangle((44, y, 112, y + 3), fill=(58, 58, 58))
    for index in range(36):
        x = 128 + ((index * 37) % 112)
        y = 28 + ((index * 23) % 124)
        image.putpixel((x, y), (226, 224, 210))
    return image


def _warm_mild_bleed_through_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (244, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.text((34, 36), "REAL", fill=(70, 70, 70))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((124, 82), "321", fill=255)
    mask_draw.text((124, 104), "654", fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(2.6))
    ghost = Image.new("RGB", image.size, (222, 198, 154))
    image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.60)))
    return image


def _open_sparse_faint_bleed_through_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (244, 244, 239))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((126, 82), "321", fill=255)
    mask_draw.text((126, 104), "654", fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(3.0))
    ghost = Image.new("RGB", image.size, (236, 236, 232))
    image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.30)))
    return image


def _pale_diffuse_bleed_through_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (244, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.text((34, 36), "REAL", fill=(70, 70, 70))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((110, 66), "189", fill=255)
    mask_draw.text((112, 90), "432", fill=255)
    mask_draw.text((112, 114), "765", fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(3.2))
    ghost = Image.new("RGB", image.size, (236, 236, 232))
    image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.30)))
    return image


def _cool_gray_mild_bleed_through_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (244, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.text((34, 36), "REAL", fill=(70, 70, 70))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((124, 82), "321", fill=255)
    mask_draw.text((124, 104), "654", fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(3.0))
    ghost = Image.new("RGB", image.size, (214, 222, 232))
    image.paste(ghost, (0, 0), mask.point(lambda value: int(value * 0.62)))
    return image


def _protected_sparse_bleed_through_mark_pages() -> dict[str, Image.Image]:
    paper = (244, 244, 239)
    pale_mark = (228, 228, 224)
    pages: dict[str, Image.Image] = {}

    image = Image.new("RGB", (260, 180), paper)
    ImageDraw.Draw(image).text((126, 82), "12", fill=pale_mark)
    pages["A001_page_number.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    draw = ImageDraw.Draw(image)
    for x in range(72, 180, 12):
        draw.ellipse((x, 92, x + 2, 94), fill=pale_mark)
    pages["A002_dotted_leaders.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    draw = ImageDraw.Draw(image)
    draw.ellipse((132, 86, 134, 88), fill=pale_mark)
    draw.rectangle((142, 102, 143, 104), fill=pale_mark)
    pages["A003_punctuation_i_dot.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    ImageDraw.Draw(image).text((22, 82), "note", fill=(226, 226, 222))
    pages["A004_marginal_annotation.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    ImageDraw.Draw(image).ellipse((110, 70, 152, 112), outline=(186, 24, 24), width=2)
    pages["A005_color_stamp_mark.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    draw = ImageDraw.Draw(image)
    for y in (70, 104, 138):
        draw.line((60, y, 210, y), fill=pale_mark, width=1)
    for x in (92, 152, 206):
        draw.line((x, 58, x, 150), fill=pale_mark, width=1)
    pages["A006_table_ruled_lines.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    draw = ImageDraw.Draw(image)
    for x, y in ((68, 50), (74, 120), (164, 40), (190, 126), (126, 96), (132, 99), (138, 102), (144, 105)):
        draw.rectangle((x, y, x + 1, y + 1), fill=(225, 225, 221))
    pages["A007_archival_dirt_marks.png"] = image

    return pages


def _protected_clean_page_faint_mark_pages() -> dict[str, Image.Image]:
    paper = (246, 246, 244)
    pale_mark = (228, 228, 224)
    pages: dict[str, Image.Image] = {}

    image = Image.new("RGB", (260, 180), paper)
    ImageDraw.Draw(image).text((122, 82), "12", fill=pale_mark)
    pages["A101_page_number.png"] = image

    image = _pale_punctuation_like_page()
    pages["A102_punctuation_like_dots.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    ImageDraw.Draw(image).text((18, 78), "thin note", fill=(226, 226, 222))
    pages["A103_thin_marginal_note.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    draw = ImageDraw.Draw(image)
    for y in (62, 92, 122):
        draw.line((54, y, 218, y), fill=pale_mark, width=1)
    for x in (86, 146, 206):
        draw.line((x, 48, x, 136), fill=pale_mark, width=1)
    pages["A104_ruled_table_lines.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    ImageDraw.Draw(image).ellipse((96, 62, 158, 124), outline=(186, 24, 24), width=2)
    pages["A105_color_stamp.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    draw = ImageDraw.Draw(image)
    draw.line((62, 96, 84, 78, 108, 104, 136, 82, 164, 106), fill=(96, 92, 86), width=2)
    pages["A106_handwriting.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    draw = ImageDraw.Draw(image)
    for x, y in (
        (34, 28),
        (72, 118),
        (118, 52),
        (168, 132),
        (214, 76),
        (126, 96),
        (132, 99),
        (138, 102),
        (144, 105),
    ):
        draw.rectangle((x, y, x + 1, y + 1), fill=(225, 225, 221))
    pages["A107_archival_texture.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    for x, y in ((132, 48), (132, 88), (132, 128)):
        for offset_x, offset_y in ((0, 0), (1, 0), (0, 1), (1, 1)):
            image.putpixel((x + offset_x, y + offset_y), pale_mark)
    pages["A108_clean_page_punctuation_column.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    for x, y in ((72, 92), (112, 92), (152, 92), (192, 92)):
        for offset_x, offset_y in ((0, 0), (1, 0), (0, 1), (1, 1)):
            image.putpixel((x + offset_x, y + offset_y), pale_mark)
    pages["A109_clean_page_ruled_or_leader_row.png"] = image

    image = Image.new("RGB", (260, 180), paper)
    for x, y in ((48, 40), (110, 122), (172, 64), (220, 140)):
        for offset_x, offset_y in ((0, 0), (1, 0), (0, 1), (1, 1)):
            image.putpixel((x + offset_x, y + offset_y), (225, 225, 221))
    pages["A110_clean_page_sparse_paper_texture.png"] = image

    return pages


def _safe_cloud_stain_combination_page() -> Image.Image:
    image = Image.new("RGB", (320, 220), (242, 242, 236))
    draw = ImageDraw.Draw(image)
    for y in (48, 74, 100):
        draw.rectangle((72, y, 220, y + 5), fill=(36, 36, 36))
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((170, 125, 260, 185), fill=180)
    mask = mask.filter(ImageFilter.GaussianBlur(9))
    image = Image.composite(Image.new("RGB", image.size, (224, 218, 178)), image, mask)
    image.putpixel((28, 28), (10, 10, 10))
    return image


def _risk_table_page_number_annotation_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (242, 242, 242))
    draw = ImageDraw.Draw(image)
    for y in (42, 72, 102, 132):
        draw.line((26, y, 214, y), fill=(190, 190, 190), width=2)
    for x in (26, 88, 150, 214):
        draw.line((x, 42, x, 132), fill=(190, 190, 190), width=2)
    draw.text((180, 18), "12", fill=(60, 60, 60))
    draw.line((48, 150, 116, 158), fill=(120, 120, 120), width=2)
    return image


def _diffuse_background_stain_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (260, 190), (242, 242, 236))
    draw = ImageDraw.Draw(image)
    for y in (54, 82, 110):
        draw.rectangle((42, y, 142, y + 4), fill=(42, 42, 42))

    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((178, 58, 222, 102), fill=150)
    mask = mask.filter(ImageFilter.GaussianBlur(7))
    image = Image.composite(Image.new("RGB", image.size, (224, 220, 196)), image, mask)
    draw = ImageDraw.Draw(image)

    if variant == "safe":
        return image
    if variant == "handwriting":
        draw.line((180, 78, 192, 70, 206, 84, 222, 74), fill=(96, 92, 86), width=2)
    elif variant == "punctuation":
        draw.ellipse((200, 80, 202, 82), fill=(52, 52, 48))
    elif variant == "page_number":
        draw.text((194, 74), "12", fill=(54, 54, 50))
    elif variant == "ruled_table":
        for y in (68, 84, 100):
            draw.line((176, y, 236, y), fill=(170, 170, 166), width=1)
        for x in (184, 208, 232):
            draw.line((x, 62, x, 110), fill=(170, 170, 166), width=1)
    elif variant == "stamp":
        draw.ellipse((176, 58, 232, 110), outline=(176, 40, 34), width=3)
    elif variant == "texture":
        for x, y in ((182, 70), (190, 84), (206, 92), (218, 78), (212, 100)):
            draw.rectangle((x, y, x + 2, y + 1), fill=(206, 202, 186))
    else:
        raise ValueError(f"unsupported diffuse stain variant: {variant}")
    return image


def _faint_thumbprint_stain_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (260, 190), (242, 242, 240))
    draw = ImageDraw.Draw(image)
    for y in range(42, 132, 24):
        draw.rectangle((28, y, 96, y + 4), fill=(35, 35, 35))
    draw.text((116, 24), "12", fill=(30, 30, 30))

    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((164, 68, 210, 108), fill=120)
    mask = mask.filter(ImageFilter.GaussianBlur(5))
    image = Image.composite(Image.new("RGB", image.size, (234, 234, 228)), image, mask)
    draw = ImageDraw.Draw(image)

    if variant == "safe":
        return image
    if variant == "handwriting":
        draw.line((166, 88, 178, 80, 192, 94, 208, 84), fill=(102, 98, 90), width=2)
    elif variant == "ruled_table":
        for y in (78, 94):
            draw.line((156, y, 220, y), fill=(176, 176, 170), width=1)
        for x in (176, 198, 218):
            draw.line((x, 70, x, 106), fill=(176, 176, 170), width=1)
    elif variant == "color_mark":
        draw.ellipse((178, 76, 204, 100), outline=(174, 42, 34), width=2)
    elif variant == "photo_texture":
        for x in range(156, 220, 4):
            for y in range(66, 118, 4):
                shade = 130 + ((x * 7 + y * 11) % 80)
                draw.rectangle((x, y, x + 2, y + 2), fill=(shade, shade + 4, shade + 8))
    elif variant == "edge_content":
        draw.rectangle((0, 76, 16, 108), fill=(58, 58, 56))
    elif variant == "dense_foreground":
        for y in range(24, 168, 12):
            draw.rectangle((18, y, 236, y + 5), fill=(48, 48, 46))
    elif variant == "pale_annotation":
        draw.line((166, 88, 178, 80, 192, 94, 208, 84), fill=(224, 222, 214), width=2)
    elif variant == "subtle_ruled_table":
        for y in (78, 94):
            draw.line((156, y, 220, y), fill=(228, 228, 222), width=1)
        for x in (176, 198, 218):
            draw.line((x, 70, x, 106), fill=(228, 228, 222), width=1)
    elif variant == "subtle_texture":
        for x in range(156, 220, 3):
            for y in range(66, 118, 3):
                shade = 224 + ((x * 7 + y * 11) % 9)
                draw.point((x, y), fill=(shade, shade, shade - 4))
    else:
        raise ValueError(f"unsupported faint thumbprint stain variant: {variant}")
    return image


def _sparse_pale_typed_page() -> Image.Image:
    image = Image.new("RGB", (360, 240), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for index, line in enumerate(("ARCHIVE REGISTER 1948", "PALE PRINT LINE")):
        draw.text((48, 52 + index * 32), line, fill=(228, 228, 228), font=font)
    return image


def _low_contrast_handwriting_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 240))
    draw = ImageDraw.Draw(image)
    for y in (48, 72, 98, 124):
        points = [(44, y), (58, y - 6), (76, y + 2), (94, y - 4), (112, y + 3), (132, y - 2)]
        draw.line(points, fill=(222, 222, 218), width=2, joint="curve")
    return image


def _clear_text_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 215, 159), outline=(30, 30, 30), width=2)
    for y in range(42, 132, 18):
        draw.rectangle((48, y, 190, y + 4), fill=(20, 20, 20))
    return image


def _risk_stamp_header_footer_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for y in range(38, 132, 18):
        draw.rectangle((42, y, 164, y + 3), fill=(58, 58, 58))
        draw.rectangle((46, y + 8, 136, y + 10), fill=(76, 76, 76))
    draw.ellipse((166, 48, 216, 98), outline=(180, 40, 35), width=3)
    draw.rectangle((48, 18, 170, 20), fill=(64, 64, 64))
    draw.rectangle((54, 158, 154, 160), fill=(64, 64, 64))
    return image.filter(ImageFilter.GaussianBlur(radius=0.6))


def _risk_edge_content_mark_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    draw.rectangle((2, 78, 4, 82), fill=(28, 28, 28))
    return image


def _risk_dark_photo_edge_trace_page() -> Image.Image:
    image = Image.new("RGB", (240, 180), (150, 150, 150))
    draw = ImageDraw.Draw(image)
    for y in range(20, 160):
        shade = 95 + ((y * 7) % 70)
        draw.line((24, y, 216, y), fill=(shade, shade, shade))
    for x in range(34, 210, 12):
        shade = 90 + (x % 50)
        draw.line((x, 26, min(216, x + 42), 154), fill=(shade, shade, shade), width=2)
    draw.rectangle((0, 74, 14, 102), fill=(45, 45, 45))
    draw.text((184, 12), "9", fill=(42, 42, 42))
    return image


def _faint_post_deskew_corner_wedge_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (320, 240), (248, 248, 248))
    draw = ImageDraw.Draw(image)
    for y in (70, 92, 114, 136, 158):
        draw.rectangle((72, y, 248, y + 3), fill=(45, 45, 45))
    if variant == "edge_handwriting":
        draw.line((2, 38, 18, 50, 7, 70, 20, 88, 4, 116), fill=(55, 55, 55), width=2)
    elif variant == "page_number":
        draw.text((6, 10), "12", fill=(55, 55, 55))
    elif variant == "ruled_table":
        for y in (22, 46, 70, 94):
            draw.line((0, y, 100, y), fill=(78, 78, 78), width=2)
        for x in (8, 42, 76):
            draw.line((x, 16, x, 106), fill=(82, 82, 82), width=2)
    elif variant == "color_stamp":
        draw.ellipse((4, 10, 48, 54), outline=(180, 30, 30), width=3)
    elif variant == "archival_corner":
        draw.line((0, 0, 38, 0), fill=(80, 80, 80), width=3)
        draw.line((0, 0, 0, 38), fill=(80, 80, 80), width=3)
    elif variant != "safe":
        raise ValueError(f"unsupported variant: {variant}")
    return image.rotate(
        0.45,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(246, 246, 246),
    )


def _single_edge_post_deskew_light_canvas_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (320, 240), (241, 241, 241))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 13, 239), fill=(250, 250, 250))
    for y in (70, 92, 114, 136, 158):
        draw.rectangle((82, y, 250, y + 3), fill=(45, 45, 45))
    if variant == "edge_handwriting":
        draw.line((2, 40, 11, 54, 4, 72, 12, 91, 5, 116), fill=(55, 55, 55), width=2)
    elif variant == "page_number":
        draw.text((2, 12), "12", fill=(55, 55, 55))
    elif variant == "pale_page_number":
        draw.text((2, 12), "12", fill=(232, 232, 232))
    elif variant == "pale_marginal_note":
        draw.line((3, 38, 10, 50, 5, 72, 11, 94, 4, 116), fill=(232, 232, 232), width=1)
    elif variant == "ruled_table":
        for y in (22, 46, 70, 94):
            draw.line((0, y, 58, y), fill=(78, 78, 78), width=2)
        for x in (5, 13, 38):
            draw.line((x, 16, x, 106), fill=(82, 82, 82), width=2)
    elif variant == "pale_ruled_table":
        for y in (22, 46, 70, 94):
            draw.line((0, y, 56, y), fill=(232, 232, 232), width=1)
        for x in (5, 12, 38):
            draw.line((x, 16, x, 106), fill=(232, 232, 232), width=1)
    elif variant == "stamp":
        draw.ellipse((2, 10, 42, 50), outline=(180, 30, 30), width=3)
    elif variant == "archival_mark":
        draw.line((0, 0, 38, 0), fill=(80, 80, 80), width=3)
        draw.line((0, 0, 0, 38), fill=(80, 80, 80), width=3)
        draw.line((6, 60, 6, 104), fill=(80, 80, 80), width=2)
    elif variant == "pale_archival_texture":
        for index in range(34):
            x = 1 + (index * 5) % 11
            y = 8 + (index * 17) % 214
            shade = 232 + (index % 9)
            draw.point((x, y), fill=(shade, shade, shade))
            if index % 3 == 0:
                draw.point((min(12, x + 1), y), fill=(shade, shade, shade))
    elif variant == "uneven_pale_margin":
        for y in range(0, 240, 6):
            shade = 243 if (y // 6) % 2 == 0 else 246
            draw.line((0, y, 13, y + 2), fill=(shade, shade, shade), width=1)
    elif variant == "near_edge_scanner_artifact":
        draw.line((6, 18, 6, 216), fill=(233, 233, 233), width=1)
        for y in range(34, 190, 31):
            draw.line((3, y, 11, y + 3), fill=(230, 230, 230), width=1)
    elif variant != "safe":
        raise ValueError(f"unsupported variant: {variant}")
    return image


def _light_margin_auto_crop_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (240, 180), (248, 248, 248))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 230, 170), fill=(240, 240, 240))
    for y in (62, 86, 110):
        draw.rectangle((72, y, 168, y + 3), fill=(48, 48, 48))

    faint = (233, 233, 233)
    if variant == "page_number":
        draw.text((3, 22), "12", fill=faint)
    elif variant == "marginal_note":
        draw.line((3, 56, 8, 52, 13, 58, 18, 55), fill=faint, width=1)
    elif variant == "table_border":
        for y in (26, 38, 50):
            draw.line((0, y, 24, y), fill=faint, width=1)
        for x in (4, 14, 23):
            draw.line((x, 20, x, 56), fill=faint, width=1)
    elif variant == "archival_texture":
        for x, y in ((2, 30), (5, 88), (8, 120), (16, 42), (20, 136)):
            draw.point((x, y), fill=(232, 232, 232))
    elif variant != "safe":
        raise ValueError(f"unsupported variant: {variant}")
    return image


def _mixed_tone_binding_gutter_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (260, 190), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    gutter_width = 34 if variant == "broad_shadow" else 15
    for y in range(190):
        for x in range(gutter_width):
            if variant == "broad_shadow":
                value = 186 + min(48, int(x * 1.8))
            elif x < 5:
                value = 250 if (x + y) % 2 == 0 else 236
            elif x < 9:
                value = 178 if y % 3 else 190
            else:
                value = 247 if (x + y) % 2 else 238
            image.putpixel((x, y), (value, value, value))
    draw.rectangle((54, 38, 222, 154), outline=(80, 80, 80), width=2)
    for y in range(68, 130, 24):
        draw.rectangle((70, y, 180, y + 2), fill=(70, 70, 70))
    if variant == "edge_handwriting":
        draw.line((3, 42, 12, 54, 5, 68, 13, 83, 4, 98), fill=(60, 60, 60), width=2)
    elif variant == "page_number":
        draw.text((4, 18), "12", fill=(62, 62, 62))
    elif variant == "ruled_table":
        for y in (44, 72, 100, 128):
            draw.line((0, y, 90, y), fill=(80, 80, 80), width=2)
        for x in (8, 38, 68):
            draw.line((x, 38, x, 136), fill=(85, 85, 85), width=2)
    return image


def _segmented_dark_scanner_border_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 240))
    draw = ImageDraw.Draw(image)
    for y in (62, 88, 114):
        draw.rectangle((74, y, 166, y + 3), fill=(35, 35, 35))

    if variant == "broad_shadow":
        for offset in range(20):
            value = 30 + min(105, offset * 7)
            draw.rectangle((offset, 0, offset, 179), fill=(value, value, value))
            draw.rectangle((239 - offset, 0, 239 - offset, 179), fill=(value, value, value))
            draw.rectangle((0, offset, 239, offset), fill=(value, value, value))
            draw.rectangle((0, 179 - offset, 239, 179 - offset), fill=(value, value, value))
        return image

    vertical_segments = ((0, 28), (42, 70), (86, 114), (130, 158))
    horizontal_segments = ((0, 38), (58, 96), (116, 154), (174, 212))
    for offset in range(4):
        for y0, y1 in vertical_segments:
            draw.line((offset, y0, offset, y1), fill=(20, 20, 20))
            draw.line((239 - offset, y0, 239 - offset, y1), fill=(20, 20, 20))
        for x0, x1 in horizontal_segments:
            draw.line((x0, offset, x1, offset), fill=(20, 20, 20))
            draw.line((x0, 179 - offset, x1, 179 - offset), fill=(20, 20, 20))

    if variant == "page_number":
        draw.rectangle((5, 20, 15, 25), fill=(35, 35, 35))
        draw.rectangle((8, 26, 18, 31), fill=(35, 35, 35))
    elif variant == "marginal_mark":
        draw.line((5, 48, 15, 58, 7, 72, 16, 88, 5, 104), fill=(45, 45, 45), width=2)
    elif variant == "stamp":
        draw.ellipse((5, 18, 46, 58), outline=(170, 28, 28), width=3)
        draw.line((8, 38, 42, 38), fill=(45, 45, 45), width=2)
    elif variant == "table_lines":
        for y in (42, 68, 94):
            draw.line((4, y, 92, y), fill=(55, 55, 55), width=2)
        for x in (12, 42, 72):
            draw.line((x, 36, x, 106), fill=(60, 60, 60), width=2)
    elif variant == "archival_edge":
        draw.line((4, 8, 4, 170), fill=(55, 55, 55), width=2)
        draw.line((4, 8, 52, 8), fill=(55, 55, 55), width=2)
    elif variant != "safe":
        raise ValueError(f"unsupported variant: {variant}")
    return image


def _interrupted_dark_scanner_border_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (240, 180), (244, 244, 240))
    draw = ImageDraw.Draw(image)
    for y in (62, 88, 114):
        draw.rectangle((74, y, 166, y + 3), fill=(35, 35, 35))

    if variant == "broad_shadow":
        for offset in range(20):
            value = 28 + min(108, offset * 7)
            draw.rectangle((offset, 0, offset, 179), fill=(value, value, value))
            draw.rectangle((239 - offset, 0, 239 - offset, 179), fill=(value, value, value))
            draw.rectangle((0, offset, 239, offset), fill=(value, value, value))
            draw.rectangle((0, 179 - offset, 239, 179 - offset), fill=(value, value, value))
        return image

    sides = ("left",) if variant == "single_edge" else ("left", "right", "top", "bottom")
    for offset in range(4):
        if "left" in sides:
            draw.line((offset, 0, offset, 179), fill=(20, 20, 20))
        if "right" in sides:
            draw.line((239 - offset, 0, 239 - offset, 179), fill=(20, 20, 20))
        if "top" in sides:
            draw.line((0, offset, 239, offset), fill=(20, 20, 20))
        if "bottom" in sides:
            draw.line((0, 179 - offset, 239, 179 - offset), fill=(20, 20, 20))

    vertical_gaps = (10, 36, 62, 88, 114, 140, 166)
    horizontal_gaps = (14, 49, 84, 119, 154, 189, 224)
    for y0 in vertical_gaps:
        if "left" in sides:
            draw.rectangle((0, y0, 3, y0 + 7), fill=(244, 244, 240))
        if "right" in sides:
            draw.rectangle((236, y0, 239, y0 + 7), fill=(244, 244, 240))
    for x0 in horizontal_gaps:
        if "top" in sides:
            draw.rectangle((x0, 0, x0 + 7, 3), fill=(244, 244, 240))
        if "bottom" in sides:
            draw.rectangle((x0, 176, x0 + 7, 179), fill=(244, 244, 240))

    if variant == "near_edge_content":
        draw.rectangle((5, 18, 15, 24), fill=(35, 35, 35))
        draw.rectangle((8, 25, 18, 31), fill=(35, 35, 35))
    elif variant == "table_lines":
        for y in (42, 68, 94):
            draw.line((4, y, 92, y), fill=(55, 55, 55), width=2)
        for x in (12, 42, 72):
            draw.line((x, 36, x, 106), fill=(60, 60, 60), width=2)
    elif variant == "marginal_text":
        for y in (30, 40, 50, 70, 80, 90):
            draw.rectangle((17, y, 20, y + 1), fill=(35, 35, 35))
    elif variant == "stamp_block":
        draw.rectangle((15, 22, 48, 52), outline=(45, 45, 45), width=2)
        draw.line((18, 38, 45, 38), fill=(45, 45, 45), width=2)
    elif variant == "handwritten_note":
        draw.line((16, 42, 24, 48, 18, 58, 27, 67, 17, 78, 25, 88), fill=(45, 45, 45), width=2)
    elif variant == "punched_marks":
        for y in (24, 44, 64, 84, 104, 124):
            draw.ellipse((16, y, 22, y + 6), fill=(40, 40, 40))
    elif variant == "broken_frame":
        for y0, y1 in ((18, 34), (54, 70), (90, 106), (126, 142)):
            draw.rectangle((14, y0, 17, y1), fill=(30, 30, 30))
    elif variant not in {"safe", "single_edge"}:
        raise ValueError(f"unsupported variant: {variant}")
    return image


def _text_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 12, 112, 82), outline=(50, 50, 50), width=2)
    for y in range(30, 68, 12):
        draw.line((32, y, 92, y), fill=(30, 30, 30), width=2)
    image.putpixel((24, 24), (0, 0, 0))
    return image


def _edge_shadow_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (232, 232, 232))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 14, 95), fill=(170, 170, 170))
    draw.rectangle((30, 26, 100, 70), fill=(238, 238, 238))
    for y in range(36, 60, 10):
        draw.line((42, y, 88, y), fill=(45, 45, 45), width=2)
    return image


def _stain_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (238, 238, 238))
    draw = ImageDraw.Draw(image)
    draw.ellipse((20, 18, 42, 36), fill=(214, 214, 214))
    draw.ellipse((86, 52, 110, 72), fill=(216, 216, 216))
    for y in range(34, 62, 12):
        draw.line((42, y, 78, y), fill=(45, 45, 45), width=2)
    return image


def _scanline_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    for y in (18, 45, 72):
        draw.line((6, y, 121, y), fill=(218, 218, 218), width=1)
    for y in range(34, 58, 12):
        draw.line((42, y, 86, y), fill=(50, 50, 50), width=2)
    return image


def _segmented_scanline_page() -> Image.Image:
    image = Image.new("RGB", (260, 180), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (42, 64, 86):
        draw.rectangle((42, y, 158, y + 5), fill=(36, 36, 36))
    for x0, x1 in ((16, 48), (96, 128), (196, 228)):
        draw.rectangle((x0, 132, x1, 133), fill=(226, 226, 222))
    return image


def _subtle_vertical_scanline_page() -> Image.Image:
    image = Image.new("RGB", (300, 220), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (44, 68):
        draw.rectangle((46, y, 115, y + 4), fill=(42, 42, 42))
    draw.rectangle((210, 18, 210, 202), fill=(237, 237, 233))
    return image


def _subtle_ruled_table_page() -> Image.Image:
    image = Image.new("RGB", (300, 220), (240, 240, 236))
    draw = ImageDraw.Draw(image)
    for y in (44, 68):
        draw.rectangle((46, y, 115, y + 4), fill=(42, 42, 42))
    for x in range(42, 236, 38):
        draw.rectangle((x, 18, x, 202), fill=(237, 237, 233))
    for y in range(34, 178, 28):
        draw.rectangle((30, y, 254, y), fill=(237, 237, 233))
    return image


def _very_subtle_scanline_page(variant: str = "safe") -> Image.Image:
    image = Image.new("RGB", (300, 220), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    for y in (44, 68):
        draw.rectangle((46, y, 115, y + 4), fill=(42, 42, 42))
    draw.rectangle((210, 18, 210, 202), fill=(241, 241, 237))
    if variant == "safe":
        return image
    if variant == "table":
        for x in (42, 104, 166, 228):
            draw.line((x, 28, x, 176), fill=(45, 45, 45), width=2)
        for y in (92, 126, 160):
            draw.line((32, y, 254, y), fill=(45, 45, 45), width=2)
    elif variant == "ruled":
        for y in range(32, 184, 16):
            draw.line((30, y, 254, y), fill=(237, 237, 233), width=1)
    elif variant == "underline":
        draw.line((152, 152, 236, 152), fill=(42, 42, 42), width=2)
    elif variant == "stamp":
        draw.ellipse((166, 84, 232, 146), outline=(184, 24, 24), width=4)
    elif variant == "marginal_mark":
        draw.line((18, 92, 88, 112, 142, 134, 214, 152), fill=(50, 50, 50), width=2)
    elif variant == "faint_marginal_note":
        draw.line((10, 76, 64, 88, 104, 82, 142, 96), fill=(92, 92, 88), width=1)
    elif variant == "edge_line":
        draw.rectangle((4, 32, 14, 182), fill=(58, 58, 58))
    elif variant == "near_edge_rule":
        image = Image.new("RGB", (300, 220), (242, 242, 238))
        draw = ImageDraw.Draw(image)
        for y in (44, 68):
            draw.rectangle((46, y, 115, y + 4), fill=(42, 42, 42))
        draw.rectangle((12, 18, 12, 202), fill=(241, 241, 237))
    else:
        raise ValueError(f"unsupported variant: {variant}")
    return image


def _faded_text_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (242, 242, 242))
    draw = ImageDraw.Draw(image)
    for y in range(22, 74, 13):
        draw.line((24, y, 104, y), fill=(188, 188, 188), width=2)
        draw.line((28, y + 6, 92, y + 6), fill=(192, 192, 192), width=2)
    return image


def _blurred_text_page() -> Image.Image:
    image = Image.new("RGB", (128, 96), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    for y in range(24, 72, 12):
        draw.line((24, y, 104, y), fill=(42, 42, 42), width=2)
        draw.line((28, y + 5, 92, y + 5), fill=(58, 58, 58), width=2)
    return image.filter(ImageFilter.GaussianBlur(radius=0.7))


def _mildly_blurred_typed_body_text_page(*, page_number: bool = False) -> Image.Image:
    image = Image.new("RGB", (420, 560), (245, 245, 242))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    lines = (
        "ARCHIVE QUALITY CONTROL PAGE",
        "TYPED TEXT EDGES ARE SOFT",
        "REVIEW SHOULD STAY SAFE",
        "PRINTED STROKES ONLY",
        "LOCAL BATCH SAMPLE",
        "NEUTRAL LIGHT PAPER",
        "MILD BLUR CASE",
        "STABLE ROW STRUCTURE",
        "FINAL TEXT LINE",
    )
    for index, line in enumerate(lines):
        draw.text((64, 100 + index * 34), line, fill=(72, 72, 72), font=font)
    if page_number:
        draw.text((358, 22), "12", fill=(65, 65, 65), font=font)
    return image.filter(ImageFilter.GaussianBlur(radius=0.75))


def _mildly_blurred_ruled_table_background_page() -> Image.Image:
    image = Image.new("RGB", (420, 560), (245, 245, 242))
    draw = ImageDraw.Draw(image)
    for y in range(96, 472, 32):
        draw.line((50, y, 370, y), fill=(118, 118, 118), width=1)
    for x in range(50, 371, 64):
        draw.line((x, 96, x, 472), fill=(118, 118, 118), width=1)
    for y in range(112, 440, 64):
        for x in (68, 150, 236, 310):
            draw.rectangle((x, y, x + 26, y + 3), fill=(86, 86, 86))
            draw.rectangle((x, y + 10, x + 16, y + 12), fill=(96, 96, 96))
    return image.filter(ImageFilter.GaussianBlur(radius=0.75))


def _combination_guard_metrics() -> dict[str, object]:
    return {
        "size_change_ratio": 0.0,
        "pixel_change_guardrail_scope": "same_size_pixel_change",
        "pixel_change_guardrail_applied": True,
        "pixel_change_ratio": 0.18,
        "brightness_delta": 8.0,
        "contrast_delta": 8.0,
        "crop_ratio": 0.0,
        "max_trim_margin_ratio": 0.0,
        "deskew_abs_angle_degrees": 0.0,
        "background_stains_candidate_pixel_ratio": 0.04,
        "scanlines_candidate_pixel_ratio": 0.04,
        "faded_text_enhanced": False,
        "faded_text_changed_pixel_ratio": 0.04,
        "faded_text_candidate_pixel_ratio": 0.04,
        "text_edges_sharpened": False,
        "text_edges_changed_pixel_ratio": 0.0,
        "text_edges_candidate_pixel_ratio": 0.0,
        "local_content_changed_ratio": 0.02,
        "edge_content_changed_ratio": 0.02,
    }


def _guard_passed() -> dict[str, object]:
    return {"checked": True, "action": "passed", "reverted": False, "reasons": []}


def _guard_reverted(*reasons: str) -> dict[str, object]:
    return {"checked": True, "action": "reverted_to_source", "reverted": True, "reasons": list(reasons)}
