from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.processing import ProcessingOptions, process_images
from archive_scan_qc.scanner import ScanConfig, scan_batch


def _draw_text_like_page(
    *,
    size: tuple[int, int],
    background: tuple[int, int, int],
    bars: tuple[tuple[int, int, int, int], ...],
    foreground: tuple[int, int, int],
) -> Image.Image:
    page = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(page)
    for box in bars:
        draw.rectangle(box, fill=foreground)
    return page


def _file_bytes_by_name(paths: list[Path]) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(paths)}


def _records_by_source(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(record["source_relative_path"]): record
        for record in manifest["files"]
        if isinstance(record, dict)
    }


def _output_paths_by_source(records: dict[str, dict[str, object]]) -> dict[str, str]:
    return {
        source: str(record["output_relative_path"])
        for source, record in records.items()
        if isinstance(record.get("output_relative_path"), str) and record["output_relative_path"]
    }


def _output_bytes_by_source(process_dir: Path, records: dict[str, dict[str, object]]) -> dict[str, bytes]:
    return {
        source: (process_dir / output_path).read_bytes()
        for source, output_path in _output_paths_by_source(records).items()
    }


def _assert_privacy_summary(testcase: unittest.TestCase, audit_summary: dict[str, object]) -> None:
    privacy = audit_summary["privacy"]
    testcase.assertTrue(privacy["aggregate_only"])
    testcase.assertFalse(privacy["contains_paths"])
    testcase.assertFalse(privacy["contains_hashes"])


def _assert_text_excludes(testcase: unittest.TestCase, text: str, tokens: tuple[str, ...]) -> None:
    for token in tokens:
        testcase.assertNotIn(token, text)


class ScanProcessingWorkflowRegressionTest(unittest.TestCase):
    def test_case_variant_extensions_and_output_stem_collisions_remain_stable_and_privacy_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-case-variant-collision-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            fixtures = (
                ("private_case_variant_alpha.JPG", "JPEG", (242, 242, 238), (26, 26, 26)),
                ("private_case_variant_Alpha.jpeg", "JPEG", (240, 240, 236), (34, 34, 34)),
                ("private_case_variant_alpha-v2.TIF", "TIFF", (244, 244, 240), (42, 42, 42)),
                ("private_case_variant_alpha_v2.tiff", "TIFF", (238, 238, 234), (50, 50, 50)),
                ("private_case_variant_beta.PnG", "PNG", (241, 241, 237), (28, 28, 28)),
            )
            for index, (filename, image_format, background, foreground) in enumerate(fixtures):
                page = _draw_text_like_page(
                    size=(152, 196),
                    background=background,
                    bars=((22, 36, 124, 44), (24, 84, 126, 92), (24, 128, 88 + index * 8, 136)),
                    foreground=foreground,
                )
                page.save(input_dir / filename, format=image_format)

            source_paths = sorted(input_dir.iterdir())
            source_bytes_before = _file_bytes_by_name(source_paths)

            first_report = scan_batch(ScanConfig("synthetic-regression", "case-variant-collision-guard", input_dir, output_dir))
            first_manifest = process_images(first_report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))
            first_records = _records_by_source(first_manifest)
            first_output_paths = _output_paths_by_source(first_records)

            self.assertEqual(set(first_records), {name for name, *_ in fixtures})
            self.assertEqual(len(set(first_output_paths.values())), len(fixtures))
            first_output_sha = {source: record["output_sha256"] for source, record in first_records.items()}
            first_output_bytes = _output_bytes_by_source(process_dir, first_records)

            first_audit_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            first_audit = json.loads(first_audit_text)
            first_retry_text = (process_dir / "processing_retry_manifest.json").read_text(encoding="utf-8")
            first_retry = json.loads(first_retry_text)
            _assert_privacy_summary(self, first_audit)
            self.assertIn("summary", first_retry)
            self.assertEqual(first_retry["summary"]["retry_list_files"], 0)

            second_report = scan_batch(ScanConfig("synthetic-regression", "case-variant-collision-guard", input_dir, output_dir))
            second_manifest = process_images(second_report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))
            second_records = _records_by_source(second_manifest)
            second_output_paths = _output_paths_by_source(second_records)
            second_output_sha = {source: record["output_sha256"] for source, record in second_records.items()}
            second_output_bytes = _output_bytes_by_source(process_dir, second_records)

            self.assertEqual(set(second_records), {name for name, *_ in fixtures})
            self.assertEqual(first_output_paths, second_output_paths)
            self.assertEqual(first_output_sha, second_output_sha)
            self.assertEqual(first_output_bytes, second_output_bytes)
            for source in sorted(first_records):
                self.assertEqual(first_records[source]["status"], second_records[source]["status"])

            second_audit_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            second_audit = json.loads(second_audit_text)
            second_retry_text = (process_dir / "processing_retry_manifest.json").read_text(encoding="utf-8")
            second_retry = json.loads(second_retry_text)
            _assert_privacy_summary(self, second_audit)
            self.assertIn("summary", second_retry)
            self.assertEqual(second_retry["summary"]["retry_list_files"], 0)

            forbidden_tokens = tuple(name.rsplit(".", 1)[0] for name, *_ in fixtures) + (str(input_dir), str(process_dir))
            for text in (first_audit_text, first_retry_text, second_audit_text, second_retry_text):
                _assert_text_excludes(self, text, forbidden_tokens)

    def test_same_batch_rerun_processing_is_idempotent_and_privacy_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-rerun-idempotency-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            first_page = _draw_text_like_page(
                size=(140, 180),
                background=(244, 244, 240),
                bars=((18, 34, 112, 42), (20, 72, 118, 78)),
                foreground=(30, 30, 30),
            )
            first_page.save(input_dir / "private_same_batch_source_a.png")
            second_page = _draw_text_like_page(
                size=(140, 180),
                background=(240, 240, 236),
                bars=((22, 36, 108, 44), (24, 84, 120, 90)),
                foreground=(34, 34, 34),
            )
            second_page.save(input_dir / "private_same_batch_source_b.png")
            source_paths = sorted(input_dir.glob("*.png"))
            source_bytes_before = _file_bytes_by_name(source_paths)

            first_report = scan_batch(ScanConfig("synthetic-regression", "same-batch-rerun-guard", input_dir, output_dir))
            first_manifest = process_images(first_report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))
            first_records = _records_by_source(first_manifest)
            first_output_paths = _output_paths_by_source(first_records)
            first_output_sha = {source: record["output_sha256"] for source, record in first_records.items()}
            first_output_bytes = _output_bytes_by_source(process_dir, first_records)

            first_audit_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            _assert_privacy_summary(self, json.loads(first_audit_text))

            second_report = scan_batch(ScanConfig("synthetic-regression", "same-batch-rerun-guard", input_dir, output_dir))
            second_manifest = process_images(second_report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))
            second_records = _records_by_source(second_manifest)

            self.assertEqual(set(first_records), set(second_records))
            self.assertEqual(first_output_paths, _output_paths_by_source(second_records))
            self.assertEqual(first_output_sha, {source: record["output_sha256"] for source, record in second_records.items()})
            self.assertEqual(first_output_bytes, _output_bytes_by_source(process_dir, second_records))
            for source in sorted(first_records):
                self.assertEqual(first_records[source]["status"], second_records[source]["status"])

            second_audit_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            _assert_privacy_summary(self, json.loads(second_audit_text))
            forbidden_tokens = (
                "private_same_batch_source_a",
                "private_same_batch_source_b",
                str(input_dir),
                str(process_dir),
            )
            _assert_text_excludes(self, first_audit_text, forbidden_tokens)
            _assert_text_excludes(self, second_audit_text, forbidden_tokens)

    def test_output_under_input_subtree_is_not_recursively_processed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-output-under-input-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = input_dir / "processed"
            input_dir.mkdir()

            for filename, background, foreground in (
                ("private_nested_source_a.png", (244, 244, 240), (30, 30, 30)),
                ("private_nested_source_b.png", (240, 240, 236), (34, 34, 34)),
            ):
                page = _draw_text_like_page(
                    size=(140, 180),
                    background=background,
                    bars=((18, 34, 112, 42), (20, 72, 118, 78)),
                    foreground=foreground,
                )
                page.save(input_dir / filename)

            source_paths = sorted(input_dir.glob("*.png"))
            source_bytes_before = _file_bytes_by_name(source_paths)

            first_report = scan_batch(
                ScanConfig("synthetic-regression", "output-under-input-recursion-guard", input_dir, output_dir)
            )
            first_manifest = process_images(first_report, input_dir, process_dir, ProcessingOptions(workers=1))
            expected_sources = {"private_nested_source_a.png", "private_nested_source_b.png"}
            self.assertEqual(set(_records_by_source(first_manifest)), expected_sources)

            second_report = scan_batch(
                ScanConfig("synthetic-regression", "output-under-input-recursion-guard", input_dir, output_dir)
            )
            self.assertEqual({entry["relative_path"] for entry in second_report["files"]}, expected_sources)
            second_manifest = process_images(second_report, input_dir, process_dir, ProcessingOptions(workers=1))
            second_sources = set(_records_by_source(second_manifest))
            self.assertEqual(second_sources, expected_sources)
            self.assertFalse(any(source.startswith("processed/") for source in second_sources))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))

            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            _assert_privacy_summary(self, audit_summary)
            self.assertEqual(audit_summary["counts"]["total_files"], 2)
            _assert_text_excludes(
                self,
                audit_summary_text,
                ("private_nested_source_a", "private_nested_source_b", str(input_dir), str(process_dir)),
            )

    def test_corrupt_input_is_privacy_safe_and_does_not_abort_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-corrupt-input-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            control = _draw_text_like_page(
                size=(120, 180),
                background=(244, 244, 240),
                bars=((24, 52, 96, 58),),
                foreground=(34, 34, 34),
            )
            control.save(input_dir / "private_valid_control.png")
            corrupt_path = input_dir / "private_corrupt_input.png"
            corrupt_path.write_bytes(b"\x89PNG\r\n\x1a\nBROKEN-TRUNCATED")
            source_paths = sorted(input_dir.glob("*.png"))
            source_bytes_before = _file_bytes_by_name(source_paths)

            report = scan_batch(ScanConfig("synthetic-regression", "corrupt-input-guard", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))

            records = _records_by_source(manifest)
            self.assertEqual(records["private_valid_control.png"]["status"], "processed")
            self.assertTrue((process_dir / "images" / "private_valid_control.png").exists())
            self.assertIn(records["private_corrupt_input.png"]["status"], {"skipped", "failed"})
            self.assertEqual(records["private_corrupt_input.png"]["failure_reason"], "source image is not openable")
            self.assertEqual(records["private_corrupt_input.png"]["error"], "source image is not openable")

            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            self.assertEqual(audit_summary["counts"]["total_files"], 2)
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["skipped_files"], 1)
            _assert_privacy_summary(self, audit_summary)
            _assert_text_excludes(
                self,
                audit_summary_text,
                ("private_valid_control", "private_corrupt_input", str(input_dir), str(corrupt_path)),
            )

    def test_chinese_and_spaced_filenames_are_processed_with_stable_unique_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-chinese-space-filename-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "输入 扫描(批次A)"
            output_dir = root / "reports"
            process_dir = root / "processed 输出"
            input_dir.mkdir()

            pages = (
                "私有 档案(封面)-A.PNG",
                "批次 01 - 合同（扫描）.jpg",
                "记录_第2页 (复印).JPEG",
                "目录-附注（最终版） 03.png",
            )
            for index, page_name in enumerate(pages):
                page = _draw_text_like_page(
                    size=(140, 180),
                    background=(244 - index * 2, 243 - index * 2, 239 - index * 2),
                    bars=((18, 36, 112, 44), (20, 84, 118, 90)),
                    foreground=(34 + index * 4, 34 + index * 4, 34 + index * 4),
                )
                page.save(input_dir / page_name)

            source_paths = sorted(input_dir.iterdir())
            source_bytes_before = _file_bytes_by_name(source_paths)

            first_report = scan_batch(
                ScanConfig("synthetic-regression", "chinese-spaced-filename-processing-guard", input_dir, output_dir)
            )
            first_manifest = process_images(first_report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))
            first_records = _records_by_source(first_manifest)
            first_output_paths = _output_paths_by_source(first_records)
            first_output_sha = {source: record["output_sha256"] for source, record in first_records.items()}

            self.assertEqual(set(first_records), set(pages))
            self.assertEqual(len(set(first_output_paths.values())), len(pages))
            for source, output_path in first_output_paths.items():
                self.assertTrue((process_dir / output_path).exists(), msg=f"missing derivative for {source}")
                self.assertEqual(first_records[source]["status"], "processed")

            second_report = scan_batch(
                ScanConfig("synthetic-regression", "chinese-spaced-filename-processing-guard", input_dir, output_dir)
            )
            second_manifest = process_images(second_report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))
            second_records = _records_by_source(second_manifest)

            self.assertEqual(set(second_records), set(pages))
            self.assertEqual(first_output_paths, _output_paths_by_source(second_records))
            self.assertEqual(first_output_sha, {source: record["output_sha256"] for source, record in second_records.items()})

            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            retry_manifest_text = (process_dir / "processing_retry_manifest.json").read_text(encoding="utf-8")
            privacy = audit_summary["privacy"]
            self.assertTrue(privacy["aggregate_only"])
            self.assertFalse(privacy["contains_file_list"])
            self.assertFalse(privacy["contains_paths"])
            self.assertFalse(privacy["contains_hashes"])
            self.assertFalse(privacy["contains_thumbnails"])
            self.assertFalse(privacy["contains_image_content"])
            self.assertEqual(audit_summary["counts"]["total_files"], len(pages))
            self.assertEqual(audit_summary["counts"]["processed_files"], len(pages))

            forbidden_tokens = (*pages, str(input_dir), str(process_dir), "source_relative_path", "source_sha256")
            _assert_text_excludes(self, audit_summary_text, forbidden_tokens)
            _assert_text_excludes(self, retry_manifest_text, forbidden_tokens)

    def test_non_image_sidecars_are_tolerated_without_aborting_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-sidecar-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            valid = _draw_text_like_page(
                size=(140, 190),
                background=(243, 243, 239),
                bars=((24, 48, 118, 56),),
                foreground=(36, 36, 36),
            )
            valid.save(input_dir / "private_valid_scan_001.png")
            sidecars = {
                "private_valid_scan_001.xmp": b"<x:xmpmeta>local sidecar metadata</x:xmpmeta>",
                "private_valid_scan_001.jpg.tmp": b"TEMP-EXPORT",
                "private_valid_scan_001.notes.txt": b"operator notes should not block image processing",
                "private_valid_scan_001_thumb.db": b"sqlite-ish-placeholder",
            }
            for filename, payload in sidecars.items():
                (input_dir / filename).write_bytes(payload)

            source_paths = [path for path in input_dir.iterdir() if path.is_file()]
            source_bytes_before = _file_bytes_by_name(source_paths)

            report = scan_batch(ScanConfig("synthetic-regression", "non-image-sidecar-guard", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))

            records = _records_by_source(manifest)
            self.assertEqual(records["private_valid_scan_001.png"]["status"], "processed")
            self.assertTrue((process_dir / "images" / "private_valid_scan_001.png").exists())
            self.assertEqual(set(records) - {"private_valid_scan_001.png"}, set(sidecars))
            for sidecar_name in sidecars:
                self.assertEqual(records[sidecar_name]["status"], "skipped")
                self.assertEqual(records[sidecar_name]["failure_reason"], "source image is not openable")
                self.assertEqual(records[sidecar_name]["error"], "source image is not openable")

            unsupported_format_count = sum(1 for finding in report["findings"] if finding["rule"] == "unsupported_format")
            self.assertEqual(unsupported_format_count, len(sidecars))

            audit_summary_text = (process_dir / "processing_audit_summary.json").read_text(encoding="utf-8")
            audit_summary = json.loads(audit_summary_text)
            self.assertEqual(audit_summary["counts"]["total_files"], 1 + len(sidecars))
            self.assertEqual(audit_summary["counts"]["processed_files"], 1)
            self.assertEqual(audit_summary["counts"]["failed_files"], 0)
            self.assertEqual(audit_summary["counts"]["skipped_files"], len(sidecars))
            _assert_privacy_summary(self, audit_summary)
            _assert_text_excludes(self, audit_summary_text, ("private_valid_scan_001", str(input_dir), str(process_dir)))

    def test_multi_frame_tiff_processing_has_aggregate_guard_without_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scan-processing-multi-frame-guard-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "reports"
            process_dir = root / "processed"
            input_dir.mkdir()

            single_page = _draw_text_like_page(
                size=(120, 180),
                background=(244, 244, 240),
                bars=((24, 48, 96, 54),),
                foreground=(38, 38, 38),
            )
            single_page.save(input_dir / "private_single_page_control.tif", dpi=(300, 300))
            multi_frame_first = _draw_text_like_page(
                size=(120, 180),
                background=(244, 244, 240),
                bars=((20, 42, 98, 48),),
                foreground=(36, 36, 36),
            )
            multi_frame_first.save(
                input_dir / "private_multi_frame_input.tif",
                save_all=True,
                append_images=[multi_frame_first.transpose(Image.Transpose.FLIP_LEFT_RIGHT)],
                dpi=(300, 300),
            )
            source_paths = sorted(input_dir.glob("*.tif"))
            source_bytes_before = _file_bytes_by_name(source_paths)

            report = scan_batch(ScanConfig("synthetic-regression", "multi-frame-guard", input_dir, output_dir))
            manifest = process_images(report, input_dir, process_dir, ProcessingOptions(workers=1))
            self.assertEqual(source_bytes_before, _file_bytes_by_name(source_paths))

            records = _records_by_source(manifest)
            self.assertEqual(records["private_single_page_control.tif"]["status"], "processed")
            self.assertEqual(records["private_multi_frame_input.tif"]["status"], "processed")
            frame_counts = {item["relative_path"]: item.get("frame_count") for item in report["files"]}
            self.assertEqual(frame_counts["private_single_page_control.tif"], 1)
            self.assertEqual(frame_counts["private_multi_frame_input.tif"], 2)
            self.assertIn("multi_page_image_container", {finding["rule"] for finding in report["findings"]})


if __name__ == "__main__":
    unittest.main()
