from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_scan_qc.cli import main
from archive_scan_qc.production_review_queue import build_production_review_queue, write_production_review_queue


class ProductionReviewQueueTests(unittest.TestCase):
    def test_queue_covers_qc_processing_guardrail_and_rework_items(self) -> None:
        scan_qc_report = {
            "schema_version": "scan-qc.phase1.v1",
            "generated_at": "2026-01-02T00:00:00+00:00",
            "project": {"project_id": "p1", "batch_id": "b1"},
            "findings": [
                {
                    "relative_path": "001_unopenable.png",
                    "rule": "openable",
                    "severity": "P0",
                    "source": "scanner",
                    "message": "cannot open source image",
                    "source_sha256": "must-not-appear",
                },
                {
                    "relative_path": "002_blur.png",
                    "rule": "quality_suspected_blur",
                    "severity": "P1",
                    "message": "blur candidate",
                },
            ],
        }
        processing_review_package = {
            "schema_version": "scan-qc.processing-review.v1",
            "generated_at": "2026-01-03T00:00:00+00:00",
            "files": [
                {
                    "source_relative_path": "003_failed.png",
                    "status": "failed",
                    "failure_reason": "source image is not openable",
                    "source_sha256": "also-must-not-appear",
                },
                {
                    "source_relative_path": "004_guardrail.png",
                    "status": "processed",
                    "processing_warnings": ["pixel_change_ratio exceeds review threshold"],
                    "guardrail_failures": ["crop ratio over safety limit"],
                    "output_sha256": "never-include",
                },
            ],
        }
        rework_action_list = {
            "schema_version": "scan-qc.rework-action-list.v1",
            "actions": [
                {
                    "action_type": "duplicate_manifest_correction",
                    "priority": "P1",
                    "relative_path": "005_duplicate.png",
                    "findings": [{"message": "duplicate sequence value", "source_sha256": "hidden"}],
                }
            ],
        }

        queue = build_production_review_queue(
            scan_qc_report=scan_qc_report,
            processing_review_package=processing_review_package,
            rework_action_list=rework_action_list,
        )

        self.assertEqual(queue["schema_version"], "scan-qc.production-review-queue.v1")
        self.assertEqual(queue["generated_at"], "2026-01-03T00:00:00+00:00")
        self.assertTrue(queue["privacy"]["local_only"])
        self.assertFalse(queue["privacy"]["contains_hashes"])
        self.assertEqual(queue["summary"]["total_items"], 6)
        self.assertEqual(queue["summary"]["items_by_source_category"]["scan_qc"], 2)
        self.assertEqual(queue["summary"]["items_by_source_category"]["processing_failure"], 1)
        self.assertEqual(queue["summary"]["items_by_source_category"]["guardrail_warning"], 2)
        self.assertEqual(queue["summary"]["items_by_source_category"]["rework_action"], 1)
        self.assertEqual([item["local_id"] for item in queue["items"]], [f"PRQ00000{index}" for index in range(1, 7)])

        by_path = {item["relative_path"]: item for item in queue["items"]}
        self.assertEqual(by_path["001_unopenable.png"]["suggested_action"], "rescan")
        self.assertEqual(by_path["002_blur.png"]["suggested_action"], "reprocess")
        self.assertEqual(by_path["003_failed.png"]["suggested_action"], "reprocess")
        self.assertEqual(by_path["005_duplicate.png"]["suggested_action"], "skip")
        self.assertIn("扫描质检", by_path["001_unopenable.png"]["reason_zh"])
        self.assertIn("处理失败", by_path["003_failed.png"]["reason_zh"])
        self.assertTrue(all(item["sensitivity"]["local_only"] for item in queue["items"]))

        raw = json.dumps(queue, ensure_ascii=False)
        for forbidden in ["must-not-appear", "also-must-not-appear", "never-include", "data:image", "private ocr text"]:
            self.assertNotIn(forbidden, raw)

    def test_empty_queue_is_local_only_and_operator_ready_false(self) -> None:
        queue = build_production_review_queue(scan_qc_report={"findings": []}, processing_review_package={"files": []})

        self.assertEqual(queue["summary"]["total_items"], 0)
        self.assertFalse(queue["summary"]["ready_for_operator_review"])
        self.assertEqual(queue["items"], [])
        self.assertTrue(queue["privacy"]["local_only"])
        self.assertFalse(queue["privacy"]["contains_image_bytes"])

    def test_cli_writes_stable_local_only_queue_from_synthetic_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scan_path = root / "scan_qc_report.json"
            review_path = root / "processing_review_package.json"
            rework_path = root / "rework_action_list.json"
            out_path = root / "production_review_queue.json"
            scan_path.write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.phase1.v1",
                        "project": {"project_id": "p1", "batch_id": "b1"},
                        "findings": [
                            {
                                "relative_path": "b.png",
                                "rule": "openable",
                                "severity": "P0",
                                "message": "cannot open",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            review_path.write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.processing-review.v1",
                        "files": [
                            {
                                "source_relative_path": "a.png",
                                "status": "processed",
                                "processing_warnings": ["guardrail warning"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            rework_path.write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.rework-action-list.v1",
                        "actions": [
                            {
                                "action_type": "informational_follow_up",
                                "priority": "P2",
                                "relative_path": "c.png",
                                "findings": [{"message": "filename warning"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                main(
                    [
                        "production-review-queue",
                        "--scan-qc-report",
                        str(scan_path),
                        "--processing-review-package",
                        str(review_path),
                        "--rework-action-list",
                        str(rework_path),
                        "--out",
                        str(out_path),
                    ]
                ),
                0,
            )
            first = out_path.read_text(encoding="utf-8")
            self.assertEqual(
                write_production_review_queue(
                    out_path,
                    scan_qc_report_path=scan_path,
                    processing_review_package_path=review_path,
                    rework_action_list_path=rework_path,
                )[0],
                out_path,
            )
            self.assertEqual(first, out_path.read_text(encoding="utf-8"))

            saved = json.loads(first)
            self.assertEqual(saved["summary"]["total_items"], 3)
            self.assertEqual([item["relative_path"] for item in saved["items"]], ["b.png", "a.png", "c.png"])
            self.assertIn("LOCAL-ONLY PRODUCTION REVIEW QUEUE", first)
            self.assertNotIn("sha256", first.lower())


if __name__ == "__main__":
    unittest.main()
