from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_scan_qc.local_workbench import (
    COMPLETION_NOTE_TXT,
    MAINTENANCE_ERROR_LOG_JSONL,
    REVIEW_DECISION_DRAFT_JSON,
    REVIEW_DECISION_SUMMARY_JSON,
    WorkbenchPreflightError,
    WorkbenchController,
    _folder_is_writable,
    sanitize_operator_error_zh,
)
from archive_scan_qc.production_runner import ProductionRunConfig, build_production_run_summary
from archive_scan_qc.production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON
from archive_scan_qc.review_decisions import REVIEW_DECISION_VERIFICATION_JSON


def decision_summary(decisions: list[tuple[str, str]]) -> dict[str, object]:
    counts = {
        "pending": 0,
        "accepted_issue": 0,
        "false_positive": 0,
        "fixed_externally": 0,
        "needs_rescan": 0,
        "blocked": 0,
    }
    rows = []
    for local_id, decision in decisions:
        counts[decision] += 1
        rows.append({"scope": "production_review_queue", "local_id": local_id, "decision": decision})
    return {
        "schema": "scan-qc-review-decisions.local.v1",
        "source_type": "production_workbench",
        "source_target_count": len(rows),
        "generated_in_browser": True,
        "privacy": {"summary_only": True},
        "aggregate_counts": {
            "total_batches": 1,
            "total_findings": len(rows),
            "p0": 0,
            "p1": 0,
            "p2": 0,
            "review_completion": {
                "total": len(rows),
                "reviewed": sum(1 for _, decision in decisions if decision != "pending"),
                "pending": counts["pending"],
                "complete": counts["pending"] == 0,
                "counts": counts,
            },
        },
        "review_counts": counts,
        "reviewed_targets": sum(1 for _, decision in decisions if decision != "pending"),
        "decisions": rows,
    }


class LocalWorkbenchAutosaveTests(unittest.TestCase):
    def test_configure_rejects_output_inside_source_before_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = input_dir / "tool-output"
            input_dir.mkdir()
            controller = WorkbenchController()

            with self.assertRaises(ValueError) as raised:
                controller.configure(input_dir, output_dir)

            self.assertIn("不能和扫描原图文件夹相同", str(raised.exception))
            self.assertFalse(output_dir.exists())

    def test_configure_rejects_metadata_inside_source_before_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = input_dir / "tool-metadata"
            input_dir.mkdir()
            controller = WorkbenchController()

            with self.assertRaises(ValueError) as raised:
                controller.configure(input_dir, output_dir, metadata_dir)

            self.assertIn("本机状态文件夹不能放在扫描原图文件夹里面", str(raised.exception))
            self.assertFalse(output_dir.exists())
            self.assertFalse(metadata_dir.exists())

    def test_reset_for_next_batch_clears_configured_status_and_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"fake image placeholder")
            controller = WorkbenchController()
            configured = controller.configure(input_dir, output_dir, metadata_dir)
            self.assertTrue(configured["configured"])
            self.assertTrue(configured["folder_readiness"]["ready_to_start"])

            reset = controller.reset_for_next_batch()

            self.assertFalse(reset["configured"])
            self.assertIsNone(reset["folders"]["input"])
            self.assertIsNone(reset["folders"]["derivatives"])
            self.assertIsNone(reset["folders"]["metadata"])
            self.assertFalse(reset["folder_readiness"]["ready_to_start"])
            self.assertEqual(reset["folder_readiness"]["status"], "not_configured")
            self.assertEqual(reset["processing_mode"]["id"], "standard")

    def test_configure_unreadable_source_returns_generic_chinese_guidance_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            input_dir.chmod(0)
            self.addCleanup(lambda: input_dir.chmod(0o700) if input_dir.exists() else None)
            if input_dir.exists() and input_dir.is_dir() and os.access(input_dir, os.R_OK | os.X_OK):
                self.skipTest("platform/user can still read chmod 0 directory")
            controller = WorkbenchController()

            with self.assertRaises(ValueError) as raised:
                controller.configure(input_dir, output_dir)

            message = str(raised.exception)
            self.assertIn("扫描原图文件夹现在不能读取", message)
            self.assertNotIn(str(input_dir), message)
            self.assertFalse(output_dir.exists())

    def test_write_probe_does_not_overwrite_or_delete_existing_probe_like_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            existing_probe = folder / ".scan_qc_preflight_existing.tmp"
            created_probe = folder / ".scan_qc_preflight_created.tmp"
            existing_probe.write_text("operator file\n", encoding="utf-8")

            with patch(
                "archive_scan_qc.local_workbench._unique_probe_path",
                side_effect=[existing_probe, created_probe],
            ):
                self.assertTrue(_folder_is_writable(folder))

            self.assertEqual(existing_probe.read_text(encoding="utf-8"), "operator file\n")
            self.assertFalse(created_probe.exists())

    def test_configure_returns_aggregate_folder_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            (input_dir / "nested").mkdir(parents=True)
            (input_dir / "nested" / "a.png").write_bytes(b"image")
            (input_dir / "notes.txt").write_text("not an image", encoding="utf-8")
            controller = WorkbenchController()

            status = controller.configure(input_dir, output_dir, metadata_dir, processing_mode="light")

            readiness = status["folder_readiness"]
            self.assertTrue(readiness["aggregate_only"])
            self.assertEqual(readiness["status"], "ready")
            self.assertTrue(readiness["ready_to_start"])
            self.assertEqual(readiness["supported_image_count"], 1)
            self.assertFalse(readiness["input_empty"])
            self.assertTrue(readiness["output_writable"])
            self.assertEqual(readiness["selected_processing_mode"]["id"], "light")
            self.assertNotIn("nested", json.dumps(readiness, ensure_ascii=False))
            self.assertNotIn("a.png", json.dumps(readiness, ensure_ascii=False))

    def test_configure_readiness_guides_empty_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()

            readiness = controller.configure(input_dir, output_dir, metadata_dir)["folder_readiness"]

            self.assertEqual(readiness["status"], "empty")
            self.assertFalse(readiness["ready_to_start"])
            self.assertEqual(readiness["supported_image_count"], 0)
            self.assertTrue(readiness["input_empty"])
            self.assertIn("原图文件夹", readiness["title_zh"])
            self.assertIn("放好图片", " ".join(readiness["next_steps_zh"]))

    def test_start_blocks_unsupported_folder_with_plain_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "notes.txt").write_text("not an image", encoding="utf-8")
            controller = WorkbenchController()
            readiness = controller.configure(input_dir, output_dir, metadata_dir)["folder_readiness"]

            self.assertEqual(readiness["status"], "unsupported")
            self.assertFalse(readiness["ready_to_start"])
            with self.assertRaises(WorkbenchPreflightError) as raised:
                controller.start()
            guidance = raised.exception.guidance
            self.assertEqual(guidance["kind"], "no_supported_images")
            self.assertEqual(guidance["supported_image_count"], 0)
            self.assertIn("常见图片格式", " ".join(guidance["next_steps_zh"]))

    def test_empty_batch_summary_gives_operator_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            admin_dir = metadata_dir / "admin_reports"
            input_dir.mkdir()
            summary = build_production_run_summary(
                config=ProductionRunConfig(input_dir=input_dir, derivative_output_dir=output_dir, metadata_output_dir=metadata_dir),
                report={
                    "summary": {
                        "total_files": 0,
                        "openable_files": 0,
                        "p0_findings": 0,
                        "p1_findings": 0,
                        "p2_findings": 0,
                        "total_findings": 0,
                        "performance": {},
                    }
                },
                processing_manifest={
                    "image_root": str(output_dir / "images"),
                    "summary": {
                        "total_files": 0,
                        "processed_files": 0,
                        "resumed_files": 0,
                        "skipped_files": 0,
                        "failed_files": 0,
                        "retry_list_files": 0,
                        "performance": {},
                    },
                },
                admin_report_dir=admin_dir,
                generated_at="2026-01-01T00:00:00+00:00",
            )

            self.assertEqual(summary["status"], "finished")
            self.assertFalse(summary["ready_for_operator_handoff"])
            self.assertEqual(summary["local_batch_state"], "empty_input_folder")
            self.assertEqual(summary["recovery_guidance"]["kind"], "empty_input_folder")
            self.assertIn("原图文件夹", summary["recovery_guidance"]["title_zh"])
            self.assertEqual(summary["operator_summary"]["files_needing_attention"], 0)

    def test_unsupported_only_batch_summary_gives_plain_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            admin_dir = metadata_dir / "admin_reports"
            input_dir.mkdir()
            summary = build_production_run_summary(
                config=ProductionRunConfig(input_dir=input_dir, derivative_output_dir=output_dir, metadata_output_dir=metadata_dir),
                report={
                    "summary": {
                        "total_files": 2,
                        "openable_files": 0,
                        "p0_findings": 0,
                        "p1_findings": 2,
                        "p2_findings": 0,
                        "total_findings": 2,
                        "performance": {},
                    }
                },
                processing_manifest={
                    "image_root": str(output_dir / "images"),
                    "summary": {
                        "total_files": 2,
                        "processed_files": 0,
                        "resumed_files": 0,
                        "skipped_files": 2,
                        "failed_files": 0,
                        "retry_list_files": 0,
                        "performance": {},
                    },
                },
                admin_report_dir=admin_dir,
                generated_at="2026-01-01T00:00:00+00:00",
            )

            self.assertEqual(summary["local_batch_state"], "no_supported_images")
            self.assertFalse(summary["ready_for_operator_handoff"])
            self.assertEqual(summary["recovery_guidance"]["kind"], "no_supported_images")
            self.assertIn("常见图片格式", " ".join(summary["recovery_guidance"]["next_steps_zh"]))

    def test_draft_decisions_are_saved_and_returned_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-review-queue.v1",
                        "items": [
                            {"local_id": "PRQ000001", "relative_path": "a.png"},
                            {"local_id": "PRQ000002", "relative_path": "b.png"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            draft = decision_summary([("PRQ000001", "needs_rescan"), ("PRQ000002", "pending")])
            draft["operator_name"] = "复核员甲"
            draft["operator_decisions"] = [
                {
                    "scope": "production_review_queue",
                    "local_id": "PRQ000001",
                    "decision": "keep_original_trace",
                    "decided_at": "2026-05-13T10:20:30.000Z",
                    "note_zh": "本张保留原貌痕迹。",
                }
            ]
            result = controller.save_draft_review_decisions(draft)

            self.assertTrue(result["saved"])
            self.assertEqual(result["message_zh"], "已自动保存")
            self.assertEqual(result["decision_summary"]["completion_status"], "incomplete")
            self.assertEqual(json.loads((metadata_dir / REVIEW_DECISION_DRAFT_JSON).read_text(encoding="utf-8")), draft)
            status = controller.status()
            self.assertEqual(status["draft_decisions"], draft)
            self.assertEqual(status["queue"]["items"][0]["local_id"], "PRQ000001")

    def test_status_reports_original_and_processed_preview_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            (input_dir / "nested").mkdir(parents=True)
            (output_dir / "images" / "nested").mkdir(parents=True)
            metadata_dir.mkdir()
            (input_dir / "nested" / "a.png").write_bytes(b"original")
            (output_dir / "images" / "nested" / "a.png").write_bytes(b"processed")
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-review-queue.v1",
                        "items": [
                            {"local_id": "PRQ000001", "relative_path": "nested/a.png"},
                            {"local_id": "PRQ000002", "relative_path": "missing.png"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            status = controller.status()

            self.assertEqual(status["queue"]["items"][0]["preview_source"], "comparison")
            self.assertEqual(status["queue"]["items"][0]["preview_sources"], {"original": True, "processed": True})
            self.assertEqual(status["queue"]["items"][1]["preview_source"], "unavailable")
            self.assertEqual(status["queue"]["items"][1]["preview_sources"], {"original": False, "processed": False})
            self.assertEqual(controller.preview_path("PRQ000001", "original")[1], "original")
            self.assertEqual(controller.preview_path("PRQ000001", "processed")[1], "processed")

    def test_final_completion_still_writes_verifier_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            summary = decision_summary([("PRQ000001", "needs_rescan"), ("PRQ000002", "false_positive")])
            summary["operator_name"] = "复核员乙"
            summary["operator_decisions"] = [
                {
                    "scope": "production_review_queue",
                    "local_id": "PRQ000001",
                    "decision": "rescan",
                    "decided_at": "2026-05-13T11:00:00.000Z",
                    "note_zh": "边缘不清楚，需要补扫。",
                },
                {
                    "scope": "production_review_queue",
                    "local_id": "PRQ000002",
                    "decision": "pass",
                    "decided_at": "2026-05-13T11:01:00.000Z",
                    "note_zh": "",
                },
            ]
            result = controller.save_review_decisions(summary)

            self.assertTrue(result["finished"])
            self.assertEqual(
                result["message_zh"],
                "完成并导出结果：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
            )
            self.assertEqual(result["decision_summary"]["completion_status"], "complete")
            self.assertEqual(result["completion_panel"]["title_zh"], "本批次已完成")
            self.assertEqual(result["completion_panel"]["completion_status_zh"], "已完成")
            self.assertEqual(result["completion_panel"]["manual_work_zh"], "没有待人工处理图片")
            self.assertEqual(result["completion_panel"]["admin_handoff_zh"], "不需要")
            self.assertEqual(result["completion_panel"]["total_review_items"], 2)
            self.assertEqual(result["completion_panel"]["reviewed_items"], 2)
            self.assertEqual(result["completion_panel"]["pending_items"], 0)
            self.assertEqual(result["completion_panel"]["derivatives_dir"], str(output_dir.resolve()))
            self.assertEqual(result["completion_panel"]["metadata_dir"], str(metadata_dir.resolve()))
            self.assertTrue(result["completion_panel"]["decision_summary_path"].endswith(REVIEW_DECISION_SUMMARY_JSON))
            self.assertTrue(result["completion_panel"]["verification_summary_path"].endswith(REVIEW_DECISION_VERIFICATION_JSON))
            self.assertTrue(result["completion_panel"]["completion_note_path"].endswith(COMPLETION_NOTE_TXT))
            self.assertEqual(
                result["completion_panel"]["next_steps_zh"],
                ["查看处理后图片。", "需要继续加工时，点击准备下一批。", "如果仍有异常或不能交接，请交管理员处理。"],
            )
            self.assertTrue((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            saved_summary = json.loads((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).read_text(encoding="utf-8"))
            self.assertEqual(saved_summary["operator_name"], "复核员乙")
            self.assertEqual(saved_summary["operator_decisions"][0]["note_zh"], "边缘不清楚，需要补扫。")
            self.assertEqual(saved_summary["operator_decisions"][0]["decided_at"], "2026-05-13T11:00:00.000Z")
            completion_note = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")
            self.assertIn("复核人员：复核员乙", completion_note)
            self.assertIn("处理后图片文件夹：", completion_note)
            self.assertIn("复核结果保存位置：", completion_note)
            self.assertIn("复核结果和交接说明已保存到本机状态文件夹", completion_note)
            self.assertIn("下一批：", completion_note)
            verification = json.loads((metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "pass")

    def test_final_completion_allows_no_review_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            summary = decision_summary([])
            result = controller.save_review_decisions(summary)

            self.assertTrue(result["finished"])
            self.assertEqual(result["decision_summary"]["completion_status"], "complete")
            self.assertEqual(result["completion_panel"]["total_review_items"], 0)
            self.assertEqual(result["completion_panel"]["reviewed_items"], 0)
            self.assertEqual(result["completion_panel"]["pending_items"], 0)
            self.assertTrue((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            completion_note = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")
            self.assertIn("复核总数：0", completion_note)
            verification = json.loads((metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "pass")

    def test_private_fields_are_rejected_for_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            draft = decision_summary([("PRQ000001", "pending")])
            draft["relative_path"] = "private.png"

            with self.assertRaises(ValueError):
                controller.save_draft_review_decisions(draft)
            self.assertFalse((metadata_dir / REVIEW_DECISION_DRAFT_JSON).exists())

    def test_run_exception_is_sanitized_for_operator_status_and_default_metadata(self) -> None:
        private_path = "/Users/example/private-root/private_scan_alpha.tif"
        private_hash = "a" * 64
        private_ocr = "PRIVATE_OCR_TEXT_12345"
        private_chinese_ocr = "张三档案题名"
        raw_error = (
            f"Traceback File \"/tmp/provider.py\", line 42: RuntimeError cannot identify image file "
            f"{private_path} sha256={private_hash} OCR={private_ocr} text={private_chinese_ocr}"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"fake image placeholder")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            with patch("archive_scan_qc.local_workbench.run_production_folder", side_effect=RuntimeError(raw_error)):
                controller._run_once()

            status = controller.status()
            operator_json = json.dumps(
                {
                    "last_error_zh": status["last_error_zh"],
                    "recovery_guidance": status["recovery_guidance"],
                },
                ensure_ascii=False,
            )
            self.assertEqual(status["last_error_zh"], "图片无法打开：请检查原图图片是否损坏。")
            self.assertEqual(status["recovery_guidance"]["kind"], "processing_failed_admin")
            self.assertIn("请交管理员处理", " ".join(status["recovery_guidance"]["next_steps_zh"]))
            for forbidden in [
                private_path,
                "private_scan_alpha.tif",
                private_hash,
                private_ocr,
                private_chinese_ocr,
                "Traceback",
                "RuntimeError",
            ]:
                self.assertNotIn(forbidden, operator_json)

            maintenance_log = (metadata_dir / MAINTENANCE_ERROR_LOG_JSONL).read_text(encoding="utf-8")
            self.assertIn("scan-qc.local-workbench-maintenance-error.v1", maintenance_log)
            self.assertIn("image_unopenable", maintenance_log)
            self.assertIn("RuntimeError", maintenance_log)
            for forbidden in [
                private_path,
                "private_scan_alpha.tif",
                private_hash,
                private_ocr,
                private_chinese_ocr,
                "Traceback File",
                "/tmp/provider.py",
                "sha256",
            ]:
                self.assertNotIn(forbidden, maintenance_log)

    def test_operator_error_sanitizer_keeps_known_guidance_but_rewrites_private_or_technical_text(self) -> None:
        safe_message = "当前批次正在处理。"
        self.assertEqual(sanitize_operator_error_zh(safe_message), safe_message)

        cases = [
            (
                "PermissionError: [Errno 13] Permission denied: '/Users/example/private-root/private_scan_beta.tif'",
                "文件夹无法读取：请检查扫描原图文件夹是否存在、是否有权限。",
            ),
            (
                "OSError: No space left on device while writing C:\\Users\\example\\private\\output.json",
                "输出文件夹无法写入：请检查输出文件夹和磁盘空间。",
            ),
            (
                "ValueError: hash deadbeefdeadbeefdeadbeefdeadbeef PRIVATE_OCR_TEXT stack frame",
                "其他异常：本批次没有正常启动，请交管理员处理。",
            ),
            (
                "图片打不开：张三档案题名 PRIVATE_CHINESE_OCR_001",
                "图片无法打开：请检查原图图片是否损坏。",
            ),
            (
                "处理失败：张三档案题名",
                "其他异常：本批次没有正常启动，请交管理员处理。",
            ),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                sanitized = sanitize_operator_error_zh(raw)
                self.assertEqual(sanitized, expected)
                self.assertNotIn("/Users/example", sanitized)
                self.assertNotIn("private_scan_beta.tif", sanitized)
                self.assertNotIn("deadbeef", sanitized)
                self.assertNotIn("PRIVATE_OCR_TEXT", sanitized)
                self.assertNotIn("张三档案题名", sanitized)
                self.assertNotIn("stack", sanitized.lower())


if __name__ == "__main__":
    unittest.main()
