import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

from archive_scan_qc.processing import ProcessingOptions, process_images
from archive_scan_qc.scanner import ScanConfig, scan_batch


def _mean_luma(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    return ImageStat.Stat(image.crop(box).convert("L")).mean[0]


def _changed_ratio(before: Image.Image, after: Image.Image, box: tuple[int, int, int, int]) -> float:
    before_l = before.crop(box).convert("L")
    after_l = after.crop(box).convert("L")
    diff = ImageChops.difference(before_l, after_l)
    changed = sum(diff.point(lambda value: 255 if value > 8 else 0).histogram()[1:])
    return changed / max(1, before_l.width * before_l.height)


def _edge_shadow_page() -> Image.Image:
    image = Image.new("RGB", (220, 160), (242, 242, 238))
    draw = ImageDraw.Draw(image)
    for x in range(12):
        shade = 194 + x * 3
        draw.line((x, 0, x, 159), fill=(shade, shade, shade))
    draw.rectangle((84, 55, 170, 60), fill=(35, 35, 35))
    draw.rectangle((92, 78, 178, 83), fill=(45, 45, 45))
    return image


class EdgeShadowProcessingTest(unittest.TestCase):
    def _process_one(self, image: Image.Image, options: ProcessingOptions) -> tuple[dict, Image.Image, Image.Image, Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        input_dir = root / "input"
        report_dir = root / "reports"
        process_dir = root / "processed"
        input_dir.mkdir()
        source = input_dir / "A001_0001.png"
        image.save(source)

        report = scan_batch(ScanConfig("p1", "b1", input_dir, report_dir))
        manifest = process_images(report, input_dir, process_dir, options)
        with Image.open(process_dir / "images" / "A001_0001.png") as processed:
            processed_image = processed.copy()
        return manifest, image.copy(), processed_image, process_dir

    def test_lighten_edge_shadow_improves_safe_narrow_shadow_and_protects_body(self) -> None:
        manifest, source, processed, process_dir = self._process_one(
            _edge_shadow_page(),
            ProcessingOptions(lighten_edge_shadow=True, workers=1),
        )

        record = manifest["files"][0]
        audit = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
        self.assertTrue(record["edge_shadow_lightened"])
        self.assertEqual(record["edge_shadow_edges"], ["left"])
        self.assertIn("lighten_edge_shadow_conservative", record["operations"])
        self.assertGreater(_mean_luma(processed, (0, 0, 12, 160)), _mean_luma(source, (0, 0, 12, 160)) + 8)
        self.assertLess(_changed_ratio(source, processed, (70, 42, 190, 98)), 0.002)
        self.assertTrue(audit["operations"]["lighten_edge_shadow"])
        self.assertEqual(audit["counts"]["edge_shadow_lightened_files"], 1)
        self.assertGreater(audit["metrics"]["edge_shadow_delta"]["max"], 8)
        processed.verify()

    def test_lighten_edge_shadow_noops_when_marks_or_body_are_near_edge(self) -> None:
        edge_mark = _edge_shadow_page()
        ImageDraw.Draw(edge_mark).ellipse((2, 66, 10, 74), fill=(30, 30, 30))
        near_body = _edge_shadow_page()
        ImageDraw.Draw(near_body).rectangle((18, 68, 45, 72), fill=(30, 30, 30))
        edge_red = _edge_shadow_page()
        ImageDraw.Draw(edge_red).ellipse((174, 84, 214, 124), outline=(180, 28, 28), width=4)

        for image in [edge_mark, near_body, edge_red]:
            manifest, source, processed, _process_dir = self._process_one(
                image,
                ProcessingOptions(lighten_edge_shadow=True, workers=1),
            )
            record = manifest["files"][0]
            self.assertFalse(record["edge_shadow_lightened"])
            self.assertIn("lighten_edge_shadow_noop", record["operations"])
            self.assertLess(_changed_ratio(source, processed, (0, 0, source.width, source.height)), 0.001)
            self.assertIn("risk", record["edge_shadow_reason"])

    def test_lighten_edge_shadow_preserves_red_content_away_from_page_edge(self) -> None:
        image = _edge_shadow_page()
        ImageDraw.Draw(image).ellipse((106, 64, 144, 102), outline=(180, 28, 28), width=4)

        manifest, source, processed, _process_dir = self._process_one(
            image,
            ProcessingOptions(lighten_edge_shadow=True, workers=1),
        )

        record = manifest["files"][0]
        self.assertTrue(record["edge_shadow_lightened"])
        self.assertEqual(record["edge_shadow_edges"], ["left"])
        self.assertGreater(_mean_luma(processed, (0, 0, 12, 160)), _mean_luma(source, (0, 0, 12, 160)) + 8)
        self.assertLess(_changed_ratio(source, processed, (100, 58, 150, 108)), 0.001)

    def test_lighten_edge_shadow_noops_for_normal_and_dark_pages(self) -> None:
        normal = Image.new("RGB", (220, 160), (242, 242, 238))
        dark = Image.new("RGB", (220, 160), (92, 92, 88))

        for image in [normal, dark]:
            manifest, source, processed, _process_dir = self._process_one(
                image,
                ProcessingOptions(lighten_edge_shadow=True, workers=1),
            )
            record = manifest["files"][0]
            self.assertFalse(record["edge_shadow_lightened"])
            self.assertIn("lighten_edge_shadow_noop", record["operations"])
            self.assertLess(_changed_ratio(source, processed, (0, 0, source.width, source.height)), 0.001)

    def test_lighten_edge_shadow_combines_with_existing_processing_guardrails(self) -> None:
        image = _edge_shadow_page()
        manifest, _source, _processed, process_dir = self._process_one(
            image,
            ProcessingOptions(
                deskew=True,
                trim_dark_border=True,
                auto_crop=True,
                despeckle=True,
                normalize_tones=True,
                lighten_edge_shadow=True,
                workers=1,
            ),
        )

        record = manifest["files"][0]
        audit = json.loads((process_dir / "processing_audit_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "processed")
        self.assertTrue(record["processing_audit"]["guardrail_failures"] == [])
        self.assertIn("lighten_edge_shadow_conservative", record["operations"])
        self.assertTrue(audit["guardrails"]["enabled"])
        self.assertEqual(audit["counts"]["guardrail_failed_files"], 0)


if __name__ == "__main__":
    unittest.main()
