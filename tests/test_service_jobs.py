from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from archive_scan_qc.service_jobs import (
    SERVICE_JOB_MAX_WORKERS,
    SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON,
    SERVICE_JOB_PUBLIC_SUMMARY_JSON,
    SERVICE_JOB_RECORD_JSON,
    ServiceJobConfig,
    cancel_service_job,
    create_service_job,
    recover_service_job,
    recover_service_jobs,
    run_service_job,
)
from archive_scan_qc.processing_quality_summary import PROCESSING_QUALITY_SUMMARY_JSON


class ServiceJobBoundaryTests(unittest.TestCase):
    def test_create_job_writes_isolated_private_record_and_public_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-boundary-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private input"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            summary = create_service_job(
                ServiceJobConfig(
                    input_dir=input_dir,
                    service_root=service_root,
                    rule_template="dat-31-2017-standard",
                    workers=1,
                ),
                job_id="job-testboundary001",
            )
            job_root = service_root / "jobs" / "job-testboundary001"
            record = json.loads((job_root / SERVICE_JOB_RECORD_JSON).read_text(encoding="utf-8"))
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["job_id"], "job-testboundary001")
            self.assertEqual(summary["state"], "created")
            self.assertTrue(summary["public_safe"])
            self.assertTrue(summary["isolation"]["metadata_isolated"])
            self.assertTrue(summary["isolation"]["derivatives_isolated"])
            self.assertTrue(summary["isolation"]["tmp_isolated"])
            self.assertEqual(summary["resource_limits"]["max_workers_per_job"], SERVICE_JOB_MAX_WORKERS)
            self.assertEqual(summary["resource_limits"]["workers_requested"], 1)
            self.assertEqual(Path(record["paths"]["metadata_dir"]).parent, job_root)
            self.assertEqual(Path(record["paths"]["derivatives_dir"]).parent, job_root)
            self.assertEqual(Path(record["paths"]["tmp_dir"]).parent, job_root)
            self.assertEqual(record["resource_limits"]["max_workers_per_job"], SERVICE_JOB_MAX_WORKERS)
            self.assertEqual(record["resource_limits"]["workers_requested"], 1)
            self.assertEqual(record["paths"]["input_dir"], str(input_dir.resolve()))
            _assert_public_text_omits(
                self,
                public_raw,
                str(root.resolve()),
                "private_page_001",
                "sha256",
                SERVICE_JOB_RECORD_JSON,
            )

    def test_create_rejects_overlapping_service_root_and_input_dir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-overlap-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            service_root = input_dir / "service-root"
            input_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "must not overlap"):
                create_service_job(ServiceJobConfig(input_dir=input_dir, service_root=service_root))

    def test_create_rejects_invalid_worker_limits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-workers-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")

            with self.assertRaisesRegex(ValueError, "positive integer"):
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=root / "service-root-low", workers=0),
                    job_id="job-testworkers001",
                )
            with self.assertRaisesRegex(ValueError, "per-job limit"):
                create_service_job(
                    ServiceJobConfig(
                        input_dir=input_dir,
                        service_root=root / "service-root-high",
                        workers=SERVICE_JOB_MAX_WORKERS + 1,
                    ),
                    job_id="job-testworkers002",
                )

    def test_run_job_writes_terminal_public_summary_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-run-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testrun001",
            )

            summary = run_service_job(service_root, "job-testrun001")
            job_root = service_root / "jobs" / "job-testrun001"
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")
            processing_manifest = json.loads(
                (job_root / "derivatives" / "processing_manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["state"], "finished")
            self.assertEqual(summary["recovery"]["status"], "terminal_summary_recovered")
            self.assertEqual(summary["counts"]["total_files"], 1)
            self.assertEqual(summary["counts"]["processed_files"], 1)
            self.assertTrue(summary["quality"]["provided"])
            self.assertEqual(summary["quality"]["status"], "pass")
            self.assertEqual(summary["quality"]["processed_files"], 1)
            self.assertEqual(processing_manifest["rule_template"]["id"], "dat-31-2017-standard")
            self.assertEqual(
                processing_manifest["rule_template"]["processing_defaults"]["reuse_scan_measurements"],
                True,
            )
            self.assertTrue((job_root / "metadata" / "production_run_summary.json").is_file())
            self.assertTrue((job_root / "derivatives" / "processing_manifest.json").is_file())
            self.assertTrue((job_root / "derivatives" / PROCESSING_QUALITY_SUMMARY_JSON).is_file())
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")
            self.assertFalse(summary["private_paths_exposed"])

    def test_cancel_job_marks_terminal_public_summary_without_private_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-cancel-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testcancel001",
            )

            summary = cancel_service_job(service_root, "job-testcancel001")
            job_root = service_root / "jobs" / "job-testcancel001"
            record = json.loads((job_root / SERVICE_JOB_RECORD_JSON).read_text(encoding="utf-8"))
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "cancelled")
            self.assertEqual(summary["state_label_zh"], "已取消")
            self.assertEqual(summary["recovery"]["status"], "cancelled_by_service_request")
            self.assertEqual(record["state"], "cancelled")
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")
            with self.assertRaisesRegex(RuntimeError, "already terminal"):
                run_service_job(service_root, "job-testcancel001")

    def test_recover_marks_stale_running_progress_without_leaking_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-recover-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testrecover001",
            )
            job_root = service_root / "jobs" / "job-testrecover001"
            (job_root / "metadata" / "production_run_progress.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run-progress.v1",
                        "state": "running",
                        "aggregate_processing": {
                            "aggregate_only": True,
                            "total_images": 8,
                            "processed_images": 3,
                            "remaining_images": 5,
                        },
                    }
                ),
                encoding="utf-8",
            )

            summary = recover_service_job(service_root, "job-testrecover001")
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "needs_recovery")
            self.assertEqual(
                summary["recovery"]["status"],
                "running_progress_requires_resume_after_service_restart",
            )
            self.assertEqual(summary["counts"]["total_files"], 8)
            self.assertEqual(summary["counts"]["processed_files"], 3)
            self.assertEqual(summary["counts"]["remaining_files"], 5)
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_recover_marks_running_record_without_progress_as_needs_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-record-recover-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testrecord001",
            )
            job_root = service_root / "jobs" / "job-testrecord001"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["state"] = "running"
            record["recovery"]["status"] = "running"
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            summary = recover_service_job(service_root, "job-testrecord001")
            public_raw = (job_root / SERVICE_JOB_PUBLIC_SUMMARY_JSON).read_text(encoding="utf-8")

            self.assertEqual(summary["state"], "needs_recovery")
            self.assertEqual(
                summary["recovery"]["status"],
                "running_record_requires_resume_after_service_restart",
            )
            _assert_public_text_omits(self, public_raw, str(root.resolve()), "private_page_001")

    def test_recover_rejects_tampered_record_paths_outside_job_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-tamper-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testtamper001",
            )
            job_root = service_root / "jobs" / "job-testtamper001"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["paths"]["metadata_dir"] = str(root / "escaped-metadata")
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "escapes"):
                recover_service_job(service_root, "job-testtamper001")

    def test_recover_rejects_tampered_input_dir_inside_service_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-input-tamper-") as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-source"
            service_root = root / "service-root"
            input_dir.mkdir()
            _write_page(input_dir / "private_page_001.png")
            create_service_job(
                ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                job_id="job-testinput001",
            )
            job_root = service_root / "jobs" / "job-testinput001"
            nested_input = job_root / "metadata"
            record_path = job_root / SERVICE_JOB_RECORD_JSON
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["paths"]["input_dir"] = str(nested_input)
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "input directory overlaps"):
                recover_service_job(service_root, "job-testinput001")

    def test_recover_service_jobs_indexes_multiple_jobs_without_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="service-job-index-") as temp_dir:
            root = Path(temp_dir)
            service_root = root / "service-root"
            for index in (1, 2):
                input_dir = root / f"private-source-{index}"
                input_dir.mkdir()
                _write_page(input_dir / f"private_page_{index:03d}.png")
                create_service_job(
                    ServiceJobConfig(input_dir=input_dir, service_root=service_root, workers=1),
                    job_id=f"job-testindex00{index}",
                )

            summary = recover_service_jobs(service_root)
            index_path = service_root / SERVICE_JOB_INDEX_PUBLIC_SUMMARY_JSON
            file_summary = json.loads(index_path.read_text(encoding="utf-8"))
            raw = json.dumps(summary, ensure_ascii=False)
            file_raw = json.dumps(file_summary, ensure_ascii=False)

            self.assertEqual(summary["job_count"], 2)
            self.assertEqual(summary["state_counts"], {"created": 2})
            self.assertEqual(file_summary["schema_version"], "scan-qc.service-job-index-public-summary.v1")
            self.assertEqual(file_summary["job_count"], 2)
            self.assertEqual(file_summary["state_counts"], {"created": 2})
            self.assertEqual({job["job_id"] for job in summary["jobs"]}, {"job-testindex001", "job-testindex002"})
            _assert_public_text_omits(self, raw, str(root.resolve()), "private_page_")
            _assert_public_text_omits(self, file_raw, str(root.resolve()), "private_page_")


def _write_page(path: Path) -> None:
    image = Image.new("RGB", (360, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 28, 332, 452), outline=(220, 220, 220), width=2)
    for index in range(6):
        y = 95 + index * 42
        draw.line((72, y, 288, y), fill=(35, 35, 35), width=2)
    image.save(path, dpi=(300, 300))


def _assert_public_text_omits(testcase: unittest.TestCase, raw: str, *private_values: str) -> None:
    normalized = raw.replace("\\\\", "\\")
    for value in private_values:
        testcase.assertNotIn(value, raw)
        testcase.assertNotIn(value, normalized)


if __name__ == "__main__":
    unittest.main()
