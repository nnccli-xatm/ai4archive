from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.processing_review import build_processing_review_package


class ProcessingReviewPackageTests(unittest.TestCase):
    def test_processing_review_package_groups_sensitive_local_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "processing_manifest.json"
            manifest = {
                "schema_version": "scan-qc.processing.v1",
                "generated_at": "2026-01-02T03:04:05+00:00",
                "project": {"project_id": "p1", "batch_id": "b1"},
                "summary": {"total_files": 4, "processed_files": 2, "resumed_files": 1, "failed_files": 1},
                "operations": ["deskew_conservative", "dark_border_trim_conservative"],
                "files": [
                    {
                        "source_relative_path": "source/page_001.png",
                        "output_relative_path": "images/page_001.png",
                        "source_sha256": "source-hash-1",
                        "output_sha256": "output-hash-1",
                        "status": "processed",
                        "deskewed": True,
                        "deskew_reason": "detected skew",
                        "skew_angle_degrees": 1.25,
                        "skew_confidence": 0.4,
                        "dark_border_trimmed": True,
                        "dark_border_reason": "trimmed",
                        "cropped": False,
                        "tone_normalized": True,
                        "background_stains_lightened": True,
                        "despeckled": True,
                        "despeckle_reason": "isolated dark pixels",
                        "despeckle_pixels_changed": 3,
                        "bleed_through_cleaned": True,
                        "scanlines_lightened": True,
                        "faded_text_enhanced": True,
                        "text_edges_sharpened": True,
                        "processing_warnings": ["pixel_change_ratio exceeds review threshold"],
                        "processing_audit": {
                            "pixel_change_ratio": 0.2,
                            "guardrail_failures": ["manual guardrail warning"],
                        },
                        "operations": ["deskew", "trim", "despeckle"],
                    },
                    {
                        "source_relative_path": "source/page_002.png",
                        "output_relative_path": "images/page_002.png",
                        "status": "resumed",
                        "resumed": True,
                        "cropped": True,
                        "crop_bbox": [1, 1, 20, 20],
                        "processing_warnings": [],
                        "processing_audit": {},
                        "operations": ["resume_skip_existing_derivative"],
                    },
                    {
                        "source_relative_path": "source/page_003.png",
                        "output_relative_path": None,
                        "status": "failed",
                        "failure_reason": "source image is not openable",
                        "processing_warnings": [],
                        "operations": [],
                    },
                    {
                        "source_relative_path": "../unsafe/page_004.png",
                        "output_relative_path": "/unsafe/output.png",
                        "status": "processed",
                        "processing_warnings": [],
                        "operations": [],
                    },
                ],
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            package = build_processing_review_package(manifest, manifest_path)

            self.assertEqual(package["schema_version"], "scan-qc.processing-review.v1")
            self.assertEqual(package["generated_at"], "2026-01-02T03:04:05+00:00")
            self.assertTrue(package["privacy"]["local_only"])
            self.assertFalse(package["privacy"]["aggregate_only"])
            self.assertEqual(package["summary"]["processed_files"], 2)
            self.assertEqual(package["summary"]["resumed_files"], 1)
            self.assertEqual(package["summary"]["failed_files"], 1)
            self.assertEqual(package["groups"]["deskewed"]["count"], 1)
            self.assertEqual(package["groups"]["dark_border_trimmed"]["count"], 1)
            self.assertEqual(package["groups"]["cropped"]["count"], 1)
            self.assertEqual(package["groups"]["despeckled"]["count"], 1)
            self.assertEqual(package["groups"]["background_cleanup"]["count"], 1)
            self.assertEqual(package["groups"]["readability_improvement"]["count"], 2)
            self.assertEqual(package["groups"]["defect_cleanup"]["count"], 1)
            self.assertEqual(package["groups"]["original_appearance_risk"]["count"], 1)
            self.assertEqual(package["groups"]["failed"]["count"], 1)
            self.assertEqual(package["groups"]["guardrail_warnings"]["count"], 1)
            self.assertTrue(package["files"][0]["faded_text_enhanced"])
            self.assertTrue(package["files"][0]["text_edges_sharpened"])
            self.assertIsNone(package["files"][0]["before_href"])
            self.assertEqual(package["files"][0]["after_href"], "images/page_001.png")
            self.assertIsNone(package["files"][3]["before_href"])
            self.assertIsNone(package["files"][3]["after_href"])

    def test_processing_review_package_cli_writes_deterministic_json_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "processing_manifest.json"
            out_dir = root / "review"
            manifest = {
                "schema_version": "scan-qc.processing.v1",
                "generated_at": "2026-01-02T03:04:05+00:00",
                "summary": {"total_files": 1},
                "files": [
                    {
                        "source_relative_path": "source/page_<001>.png",
                        "output_relative_path": "images/page_001.png",
                        "status": "processed",
                        "deskewed": True,
                        "dark_border_trimmed": False,
                        "cropped": False,
                        "despeckled": False,
                        "processing_warnings": [],
                        "processing_audit": {},
                        "operations": ["deskew"],
                    }
                ],
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            self.assertEqual(main(["processing-review-package", "--manifest", str(manifest_path), "--out", str(out_dir)]), 0)
            first_json = (out_dir / "processing_review_package.json").read_text(encoding="utf-8")
            first_html = (out_dir / "processing_review_package.html").read_text(encoding="utf-8")
            self.assertEqual(main(["processing-review-package", "--manifest", str(manifest_path), "--out", str(out_dir)]), 0)
            self.assertEqual(first_json, (out_dir / "processing_review_package.json").read_text(encoding="utf-8"))
            self.assertEqual(first_html, (out_dir / "processing_review_package.html").read_text(encoding="utf-8"))

            self.assertIn("Sensitive local processing review package", first_json)
            self.assertIn("Sensitive local processing review package", first_html)
            self.assertIn("Deskewed", first_html)
            self.assertIn("Dark Border Trimmed", first_html)
            self.assertIn("Failed", first_html)
            self.assertIn("<code>source/page_&lt;001&gt;.png</code>", first_html)
            self.assertIn('href="../images/page_001.png"', first_html)
            self.assertNotIn("data:image", first_html.lower())
            self.assertNotIn("<img", first_html.lower())


if __name__ == "__main__":
    unittest.main()
