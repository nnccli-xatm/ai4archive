from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from archive_scan_qc import local_workbench as local_workbench_module
from archive_scan_qc.local_workbench import (
    COMPLETION_NOTE_TXT,
    MAINTENANCE_ERROR_LOG_JSONL,
    REVIEW_DECISION_DRAFT_JSON,
    REVIEW_DECISION_SUMMARY_JSON,
    WorkbenchPreflightError,
    WorkbenchController,
    _folder_is_writable,
    _normalize_operator_path,
    _pick_windows_folder_via_powershell,
    sanitize_operator_error_zh,
)
from archive_scan_qc.production_runner import ProductionRunConfig, build_production_run_summary, run_production_folder
from archive_scan_qc.production_runner import PRODUCTION_RUN_PROGRESS_JSON, PRODUCTION_RUN_SUMMARY_JSON
from archive_scan_qc.production_review_queue import PRODUCTION_REVIEW_QUEUE_JSON
from archive_scan_qc.review_decisions import REVIEW_DECISION_VERIFICATION_JSON


def decision_summary(decisions: list[tuple[str, str]]) -> dict[str, object]:
    counts = {
        "pending": 0,
        "accepted_issue": 0,
        "false_positive": 0,
        "fixed_externally": 0,
        "needs_rescan": 0,
        "keep_original_trace": 0,
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
            "p0_pending": 0,
            "p1_pending": 0,
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


def assert_public_restore_payload_is_private(
    test_case: unittest.TestCase,
    payload: dict[str, object],
    forbidden_terms: list[str],
) -> None:
    public_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for forbidden in forbidden_terms:
        test_case.assertNotIn(forbidden, public_json)


class LocalWorkbenchAutosaveTests(unittest.TestCase):
    def test_configure_preflight_reports_reusable_and_needs_processing_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            metadata_dir = root / "private-metadata"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "private_reusable_page.png", dpi=(300, 300))
            Image.new("RGB", (80, 60), (230, 230, 230)).save(input_dir / "private_missing_page.png", dpi=(300, 300))
            run_production_folder(
                ProductionRunConfig(
                    input_dir=input_dir,
                    derivative_output_dir=output_dir,
                    metadata_output_dir=metadata_dir,
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    despeckle=True,
                    resume_processing=True,
                    reuse_scan_measurements=True,
                    workers=1,
                )
            )
            manifest = json.loads((output_dir / "processing_manifest.json").read_text(encoding="utf-8"))
            missing_record = next(record for record in manifest["files"] if record["source_relative_path"] == "private_missing_page.png")
            (output_dir / missing_record["output_relative_path"]).unlink()

            status = WorkbenchController().configure(input_dir, output_dir, metadata_dir, processing_mode="standard")

            precheck = status["folder_readiness"]["preflight_processing_summary"]
            self.assertTrue(precheck["aggregate_only"])
            self.assertTrue(precheck["retry_scope_safe"])
            self.assertEqual(precheck["total_files"], 2)
            self.assertEqual(precheck["reusable_files"], 1)
            self.assertEqual(precheck["needs_processing_files"], 1)
            self.assertEqual(precheck["unknown_scope_files"], 0)
            self.assertIn("可复用处理后输出", precheck["message_zh"])
            self.assertIn("补处理", precheck["message_zh"])
            risk = status["folder_readiness"]["existing_output_risk"]
            self.assertEqual(risk["kind"], "reusable_current_batch")
            self.assertIn("已有 1 张可复用处理后输出", risk["message_zh"])

    def test_configure_preflight_counts_changed_options_and_failures_as_needs_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            metadata_dir = root / "private-metadata"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "private_changed_options.png", dpi=(300, 300))
            Image.new("RGB", (80, 60), (230, 230, 230)).save(input_dir / "private_failed_record.png", dpi=(300, 300))
            run_production_folder(
                ProductionRunConfig(
                    input_dir=input_dir,
                    derivative_output_dir=output_dir,
                    metadata_output_dir=metadata_dir,
                    auto_crop=True,
                    deskew=False,
                    trim_dark_border=False,
                    despeckle=False,
                    processing_mode="light",
                    resume_processing=True,
                    reuse_scan_measurements=True,
                    workers=1,
                )
            )
            manifest_path = output_dir / "processing_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for record in manifest["files"]:
                if record["source_relative_path"] == "private_failed_record.png":
                    record["status"] = "failed"
                    record["failure_reason"] = "PRIVATE_STACK_SHOULD_NOT_LEAK"
                    record["error"] = "/Users/private/input/private_failed_record.png"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            status = WorkbenchController().configure(input_dir, output_dir, metadata_dir, processing_mode="standard")

            precheck = status["folder_readiness"]["preflight_processing_summary"]
            self.assertTrue(precheck["retry_scope_safe"])
            self.assertEqual(precheck["total_files"], 2)
            self.assertEqual(precheck["reusable_files"], 0)
            self.assertEqual(precheck["needs_processing_files"], 2)
            public_text = json.dumps(precheck, ensure_ascii=False, sort_keys=True)
            self.assertNotIn("PRIVATE_STACK_SHOULD_NOT_LEAK", public_text)
            self.assertNotIn("private_failed_record.png", public_text)

    def test_configure_preflight_is_conservative_for_missing_old_or_incomplete_state(self) -> None:
        cases = {
            "missing": None,
            "old": {"schema_version": "scan-qc.processing.v0", "files": []},
            "incomplete": {"schema_version": "scan-qc.processing.v1", "files": [{"status": "processed"}]},
        }
        for name, manifest in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                input_dir = root / "private-input"
                output_dir = root / "private-output"
                metadata_dir = root / "private-metadata"
                input_dir.mkdir()
                (output_dir / "images").mkdir(parents=True)
                metadata_dir.mkdir()
                Image.new("RGB", (80, 60), "white").save(input_dir / "private_state_page.png", dpi=(300, 300))
                Image.new("RGB", (80, 60), "white").save(output_dir / "images" / "private_state_page.png", dpi=(300, 300))
                if manifest is not None:
                    (output_dir / "processing_manifest.json").write_text(
                        json.dumps(manifest, ensure_ascii=False),
                        encoding="utf-8",
                    )

                status = WorkbenchController().configure(input_dir, output_dir, metadata_dir)

                precheck = status["folder_readiness"]["preflight_processing_summary"]
                self.assertFalse(precheck["retry_scope_safe"])
                self.assertEqual(precheck["state"], "unknown")
                self.assertEqual(precheck["total_files"], 1)
                self.assertIsNone(precheck["reusable_files"])
                self.assertIsNone(precheck["needs_processing_files"])
                self.assertEqual(precheck["unknown_scope_files"], 1)
                self.assertIn("不会误报完成", precheck["message_zh"])
                self.assertIn("不会编造复用数量", precheck["message_zh"])

    def test_configure_preflight_summary_omits_private_details(self) -> None:
        private_hash = "a" * 64
        private_ocr = "PRIVATE_OCR_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "private-input"
            output_dir = root / "private-output"
            metadata_dir = root / "private-metadata"
            input_dir.mkdir()
            Image.new("RGB", (80, 60), "white").save(input_dir / "private_sensitive_page.png", dpi=(300, 300))
            run_production_folder(
                ProductionRunConfig(
                    input_dir=input_dir,
                    derivative_output_dir=output_dir,
                    metadata_output_dir=metadata_dir,
                    auto_crop=True,
                    deskew=True,
                    trim_dark_border=True,
                    despeckle=True,
                    resume_processing=True,
                    reuse_scan_measurements=True,
                    workers=1,
                )
            )
            manifest_path = output_dir / "processing_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["private_debug"] = {
                "path": str(input_dir),
                "filename": "private_sensitive_page.png",
                "hash": private_hash,
                "ocr": private_ocr,
                "thumbnail": "data:image/png;base64,PRIVATE_THUMBNAIL",
                "evidence": ["PRIVATE_EVIDENCE_LINE"],
                "traceback": "Traceback File worker.py line 42",
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            status = WorkbenchController().configure(input_dir, output_dir, metadata_dir)

            public_payload = {
                "preflight_processing_summary": status["folder_readiness"]["preflight_processing_summary"],
                "existing_output_risk": status["folder_readiness"]["existing_output_risk"],
            }
            assert_public_restore_payload_is_private(
                self,
                public_payload,
                [
                    str(root),
                    str(input_dir),
                    str(output_dir),
                    str(metadata_dir),
                    "private_sensitive_page.png",
                    private_hash,
                    private_ocr,
                    "PRIVATE_THUMBNAIL",
                    "PRIVATE_EVIDENCE_LINE",
                    "Traceback",
                    ".png",
                    "hash",
                    "ocr",
                    "thumbnail",
                    "evidence",
                    "data:image",
                ],
            )

    def test_configure_accepts_windows_drive_paths_from_windows_browser_on_wsl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mount_root = root / "mnt"
            input_dir = mount_root / "c" / "Users" / "PS" / "scan batch"
            output_dir = mount_root / "c" / "Users" / "PS" / "processed batch"
            metadata_dir = mount_root / "c" / "Users" / "PS" / "workbench state"
            input_dir.mkdir(parents=True)
            (input_dir / "page.png").write_bytes(b"fake image placeholder")
            controller = WorkbenchController()

            with patch.object(local_workbench_module, "WINDOWS_DRIVE_MOUNT_ROOT", mount_root), patch.object(
                local_workbench_module, "_running_on_native_windows", return_value=False
            ):
                configured = controller.configure(
                    r"C:\Users\PS\scan batch",
                    "C:/Users/PS/processed batch",
                    r"C:\Users\PS\workbench state",
                )

            self.assertTrue(configured["configured"])
            self.assertEqual(configured["folders"]["input"], r"C:\Users\PS\scan batch")
            self.assertEqual(configured["folders"]["derivatives"], "C:/Users/PS/processed batch")
            self.assertEqual(configured["folders"]["metadata"], r"C:\Users\PS\workbench state")
            self.assertEqual(controller.input_dir, input_dir.resolve())
            self.assertEqual(controller.derivatives_dir, output_dir.resolve())
            self.assertEqual(controller.metadata_dir, metadata_dir.resolve())
            self.assertIn("folder_readiness", configured)

    def test_default_metadata_display_stays_in_windows_path_style(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mount_root = root / "mnt"
            input_dir = mount_root / "d" / "batch" / "input"
            output_dir = mount_root / "d" / "batch" / "output"
            input_dir.mkdir(parents=True)
            (input_dir / "page.png").write_bytes(b"fake image placeholder")
            controller = WorkbenchController()

            with patch.object(local_workbench_module, "WINDOWS_DRIVE_MOUNT_ROOT", mount_root), patch.object(
                local_workbench_module, "_running_on_native_windows", return_value=False
            ):
                configured = controller.configure(r"D:\batch\input", r"D:\batch\output")

            self.assertEqual(configured["folders"]["input"], r"D:\batch\input")
            self.assertEqual(configured["folders"]["derivatives"], r"D:\batch\output")
            self.assertEqual(configured["folders"]["metadata"], r"D:\batch\output\_production_workbench")
            self.assertEqual(controller.metadata_dir, (output_dir / "_production_workbench").resolve())

    def test_normalize_accepts_windows_file_url_and_wsl_unc_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mount_root = Path(temp_dir) / "mnt"
            with patch.object(local_workbench_module, "WINDOWS_DRIVE_MOUNT_ROOT", mount_root), patch.object(
                local_workbench_module, "_running_on_native_windows", return_value=False
            ):
                self.assertEqual(
                    _normalize_operator_path("file:///C:/Users/PS/scan%20batch"),
                    mount_root / "c" / "Users" / "PS" / "scan batch",
                )
            self.assertEqual(
                _normalize_operator_path(r"\\wsl.localhost\Ubuntu-22.04\home\ps\scan batch"),
                Path("/home/ps/scan batch"),
            )

    def test_normalize_native_windows_keeps_drive_paths_native(self) -> None:
        with patch.object(local_workbench_module, "_running_on_native_windows", return_value=True):
            self.assertEqual(
                _normalize_operator_path(r"C:\Users\PS\scan batch"),
                Path(r"C:\Users\PS\scan batch"),
            )
            self.assertEqual(
                _normalize_operator_path("file:///C:/Users/PS/scan%20batch"),
                Path(r"C:\Users\PS\scan batch"),
            )

    def test_configure_rejects_windows_network_share_with_operator_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            controller = WorkbenchController()

            with self.assertRaises(ValueError) as raised:
                controller.configure(r"\\archive-server\share\scan batch", output_dir)

            self.assertIn("暂不支持 Windows 网络共享路径", str(raised.exception))
            self.assertFalse(output_dir.exists())

    def test_configure_rejects_drive_relative_windows_path_with_plain_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            controller = WorkbenchController()

            with self.assertRaises(ValueError) as raised:
                controller.configure(r"C:scan batch", output_dir)

            self.assertIn("Windows 路径请使用完整盘符路径", str(raised.exception))
            self.assertFalse(output_dir.exists())

    def test_windows_folder_picker_uses_topmost_owner_window(self) -> None:
        with patch.object(local_workbench_module, "_run_folder_picker_command", return_value=r"C:\Users\PS\scan") as runner:
            selected = _pick_windows_folder_via_powershell("选择原图")

        self.assertEqual(selected, r"C:\Users\PS\scan")
        command = runner.call_args.args[0]
        script = command[-1]
        self.assertIn("$ownerForm.TopMost = $true", script)
        self.assertIn("$ownerForm.Activate(); $ownerForm.BringToFront()", script)
        self.assertIn("$dialog.ShowDialog($ownerForm)", script)
        self.assertIn("-STA", command)

    def test_configure_rejects_output_inside_source_before_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = input_dir / "tool-output"
            input_dir.mkdir()
            controller = WorkbenchController()

            with self.assertRaises(WorkbenchPreflightError) as raised:
                controller.configure(input_dir, output_dir)

            self.assertIn("不能和扫描原图文件夹相同", str(raised.exception))
            guidance = raised.exception.guidance
            self.assertTrue(guidance["aggregate_only"])
            self.assertFalse(guidance["ready_to_start"])
            self.assertFalse(guidance["can_start_processing"])
            self.assertEqual(guidance["kind"], "unsafe_folder_choice")
            self.assertIn("不要把输出文件夹放进扫描原图文件夹", " ".join(guidance["next_steps_zh"]))
            self.assertFalse(output_dir.exists())

    def test_configure_rejects_source_inside_output_with_aggregate_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            input_dir = output_dir / "input"
            input_dir.mkdir(parents=True)
            (input_dir / "page.png").write_bytes(b"image")
            controller = WorkbenchController()

            with self.assertRaises(WorkbenchPreflightError) as raised:
                controller.configure(input_dir, output_dir)

            guidance = raised.exception.guidance
            self.assertTrue(guidance["aggregate_only"])
            self.assertEqual(guidance["kind"], "unsafe_source_inside_output")
            self.assertFalse(guidance["ready_to_start"])
            self.assertFalse(guidance["can_start_processing"])
            self.assertIn("扫描原图文件夹不能放在处理后输出文件夹里面", guidance["message_zh"])
            self.assertIn("不要把扫描原图文件夹放进输出文件夹", " ".join(guidance["next_steps_zh"]))
            public_json = json.dumps(guidance, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(str(root), public_json)
            self.assertNotIn("page.png", public_json)

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
            self.assertEqual(
                reset["next_batch_reset"]["message_zh"],
                "上一批结果和交接说明已保留；当前工作台队列已清空，可以选择下一批文件夹。",
            )
            self.assertTrue(reset["next_batch_reset"]["previous_batch_retained"]["aggregate_only"])
            self.assertTrue(reset["next_batch_reset"]["current_workspace_cleared"]["queue_cleared"])

    def test_reset_for_next_batch_retains_completed_handoff_and_clears_transient_state(self) -> None:
        private_hash = "c" * 64
        private_ocr = "PRIVATE_RESET_OCR_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            private_file = input_dir / "private_reset_page.png"
            private_file.write_bytes(b"fake image placeholder")
            (output_dir / "images").mkdir(parents=True)
            processed_output = output_dir / "images" / "private_reset_page.png"
            processed_output.write_bytes(b"processed image bytes")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "operator_summary": {"total_source_images": 1, "derivative_images_ready": 1},
                        "counts": {"total_files": 1, "openable_files": 1, "processed_files": 1, "resumed_files": 0},
                        "private_debug": {
                            "path": str(private_file),
                            "file_name": private_file.name,
                            "sha256": private_hash,
                            "ocr_text": private_ocr,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps(
                    {
                        "summary": {"total_items": 1},
                        "items": [
                            {
                                "local_id": "PRQ000001",
                                "relative_path": private_file.name,
                                "thumbnail_data_url": "data:image/png;base64,PRIVATE_THUMBNAIL",
                                "evidence": [{"text": "PRIVATE_LINE_EVIDENCE"}],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / REVIEW_DECISION_DRAFT_JSON).write_text(
                json.dumps({"operator_decisions": [{"note_zh": "PRIVATE_DRAFT_NOTE"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_RUN_PROGRESS_JSON).write_text(
                json.dumps({"status": "finished"}, ensure_ascii=False),
                encoding="utf-8",
            )
            result = controller.save_review_decisions(decision_summary([("PRQ000001", "false_positive")]))
            self.assertTrue(result["completion_panel"]["completion_note_saved"])
            completion_note_before = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")

            reset = controller.reset_for_next_batch()

            self.assertTrue(processed_output.exists())
            self.assertTrue((metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).exists())
            self.assertTrue((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            self.assertTrue((metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).exists())
            self.assertEqual((metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8"), completion_note_before)
            self.assertFalse((metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).exists())
            self.assertFalse((metadata_dir / REVIEW_DECISION_DRAFT_JSON).exists())
            self.assertFalse((metadata_dir / PRODUCTION_RUN_PROGRESS_JSON).exists())
            self.assertFalse(reset["configured"])
            self.assertIsNone(reset["queue"])
            self.assertIsNone(reset["draft_decisions"])
            self.assertEqual(reset["next_batch_reset"]["previous_batch_retained"]["output_results_retained"], True)
            self.assertEqual(reset["next_batch_reset"]["previous_batch_retained"]["completion_note_retained"], True)
            self.assertEqual(reset["next_batch_reset"]["previous_batch_retained"]["handoff_credentials_retained"], True)
            self.assertEqual(reset["next_batch_reset"]["current_workspace_cleared"]["cleared_transient_artifacts"], 3)
            reset_text = json.dumps(reset, ensure_ascii=False, sort_keys=True)
            self.assertIn("上一批结果和交接说明已保留", reset_text)
            self.assertIn("当前工作台队列已清空", reset_text)
            assert_public_restore_payload_is_private(
                self,
                reset,
                [
                    str(root),
                    str(input_dir),
                    str(output_dir),
                    str(metadata_dir),
                    str(private_file),
                    private_file.name,
                    private_hash,
                    private_ocr,
                    "PRIVATE_THUMBNAIL",
                    "PRIVATE_LINE_EVIDENCE",
                    "PRIVATE_DRAFT_NOTE",
                    PRODUCTION_RUN_SUMMARY_JSON,
                    PRODUCTION_REVIEW_QUEUE_JSON,
                    REVIEW_DECISION_SUMMARY_JSON,
                    REVIEW_DECISION_VERIFICATION_JSON,
                    REVIEW_DECISION_DRAFT_JSON,
                    COMPLETION_NOTE_TXT,
                    "sha256",
                    "ocr_text",
                    ".png",
                    "thumbnail",
                    "evidence",
                    "data:image",
                ],
            )

    def test_finish_blocks_running_batch_with_aggregate_chinese_guidance(self) -> None:
        private_hash = "d" * 64
        private_ocr = "PRIVATE_RUNNING_OCR_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            private_file = input_dir / "private_running_page.png"
            private_file.write_bytes(b"fake image placeholder")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "status": "processing",
                        "counts": {
                            "total_files": 1,
                            "openable_files": 1,
                            "processed_files": 1,
                            "resumed_files": 0,
                            "skipped_files": 0,
                            "failed_files": 0,
                            "retry_list_files": 0,
                        },
                        "operator_summary": {
                            "total_source_images": 1,
                            "derivative_images_ready": 1,
                            "files_needing_attention": 0,
                        },
                        "private_debug": {"file": private_file.name, "sha256": private_hash, "ocr": private_ocr},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_RUN_PROGRESS_JSON).write_text(
                json.dumps({"status": "running", "current_file": private_file.name}, ensure_ascii=False),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps({"summary": {"total_items": 1}, "items": [{"local_id": "PRQ000001"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "当前不能显示为可以交接") as raised:
                controller.save_review_decisions(decision_summary([("PRQ000001", "false_positive")]))

            public_text = str(raised.exception)
            self.assertIn("请检查输出文件夹", public_text)
            self.assertFalse((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            self.assertFalse((metadata_dir / COMPLETION_NOTE_TXT).exists())
            assert_public_restore_payload_is_private(
                self,
                {"error_zh": public_text},
                [str(root), str(private_file), private_file.name, private_hash, private_ocr, ".png", "sha256", "OCR"],
            )

    def test_finish_blocks_pending_confirmations_without_overwriting_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"fake image placeholder")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "status": "finished",
                        "counts": {
                            "total_files": 1,
                            "openable_files": 1,
                            "processed_files": 1,
                            "resumed_files": 0,
                            "skipped_files": 0,
                            "failed_files": 0,
                            "retry_list_files": 0,
                        },
                        "operator_summary": {
                            "total_source_images": 1,
                            "derivative_images_ready": 1,
                            "files_needing_attention": 1,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            draft = decision_summary([("PRQ000001", "pending")])
            controller.save_draft_review_decisions(draft)
            draft_before = (metadata_dir / REVIEW_DECISION_DRAFT_JSON).read_text(encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "仍有 1 项待人工确认"):
                controller.save_review_decisions(draft)

            self.assertEqual((metadata_dir / REVIEW_DECISION_DRAFT_JSON).read_text(encoding="utf-8"), draft_before)
            self.assertFalse((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            self.assertFalse((metadata_dir / COMPLETION_NOTE_TXT).exists())

    def test_finish_blocks_insufficient_output_count_with_operator_next_step(self) -> None:
        private_hash = "e" * 64
        private_ocr = "PRIVATE_SHORT_OUTPUT_OCR_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            private_file = input_dir / "private_short_output.jpg"
            private_file.write_bytes(b"fake image placeholder")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "status": "finished",
                        "counts": {
                            "total_files": 3,
                            "openable_files": 3,
                            "processed_files": 1,
                            "resumed_files": 0,
                            "skipped_files": 0,
                            "failed_files": 0,
                            "retry_list_files": 0,
                        },
                        "operator_summary": {
                            "total_source_images": 3,
                            "derivative_images_ready": 1,
                            "files_needing_attention": 0,
                        },
                        "private_debug": {"path": str(private_file), "hash": private_hash, "ocr": private_ocr},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_RUN_PROGRESS_JSON).write_text(
                json.dumps({"state": "finished"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps({"summary": {"total_items": 1}, "items": [{"local_id": "PRQ000001"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "当前不能显示为可以交接") as raised:
                controller.save_review_decisions(decision_summary([("PRQ000001", "false_positive")]))

            public_text = str(raised.exception)
            self.assertIn("重新开始处理本批", public_text)
            self.assertFalse((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            assert_public_restore_payload_is_private(
                self,
                {"error_zh": public_text},
                [str(root), str(private_file), private_file.name, private_hash, private_ocr, ".jpg", "sha256", "OCR"],
            )

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
            self.assertTrue(readiness["can_start_processing"])
            self.assertEqual(readiness["supported_image_count"], 1)
            self.assertFalse(readiness["input_empty"])
            self.assertTrue(readiness["output_writable"])
            self.assertEqual(readiness["blocking_reasons_zh"], [])
            self.assertEqual(readiness["title_zh"], "文件夹可以开始处理")
            self.assertEqual(readiness["message_zh"], "本批预检结果：已识别到 1 张可处理图片，输出文件夹可以写入；未发现已有工作台结果，可以开始。")
            self.assertEqual(readiness["existing_output_risk"]["kind"], "none")
            self.assertFalse(readiness["existing_output_risk"]["existing_artifacts_detected"])
            self.assertEqual(readiness["selected_processing_mode"]["id"], "light")
            self.assertNotIn("nested", json.dumps(readiness, ensure_ascii=False))
            self.assertNotIn("a.png", json.dumps(readiness, ensure_ascii=False))

    def test_configure_readiness_warns_about_existing_workbench_results_without_private_details(self) -> None:
        private_hash = "a" * 64
        private_ocr = "PRIVATE_OCR_TEXT_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "operator-output"
            metadata_dir = output_dir / "_production_workbench"
            input_dir.mkdir()
            (input_dir / "incoming_private_page.png").write_bytes(b"image")
            processed_dir = output_dir / "images" / "nested"
            processed_dir.mkdir(parents=True)
            processed_file = processed_dir / "old_private_result.png"
            processed_file.write_bytes(b"processed image bytes")
            metadata_dir.mkdir(parents=True)
            summary_file = metadata_dir / PRODUCTION_RUN_SUMMARY_JSON
            summary_file.write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "debug_should_not_surface": {
                            "path": str(processed_file),
                            "file": processed_file.name,
                            "sha256": private_hash,
                            "ocr_text": private_ocr,
                            "thumbnail": "data:image/png;base64,AAAA",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            readiness = WorkbenchController().configure(input_dir, output_dir)["folder_readiness"]

            self.assertTrue(readiness["ready_to_start"])
            self.assertEqual(readiness["status"], "ready")
            risk = readiness["existing_output_risk"]
            self.assertTrue(risk["aggregate_only"])
            self.assertEqual(risk["kind"], "existing_workbench_results")
            self.assertTrue(risk["processed_outputs_detected"])
            self.assertTrue(risk["workbench_metadata_detected"])
            self.assertIn("已有本工具结果", readiness["message_zh"])
            self.assertIn("空的输出文件夹", " ".join(readiness["next_steps_zh"]))
            public_json = json.dumps(readiness, ensure_ascii=False, sort_keys=True)
            for forbidden in [
                str(root),
                str(output_dir),
                str(processed_file),
                "incoming_private_page.png",
                "old_private_result.png",
                private_hash,
                private_ocr,
                "sha256",
                "ocr_text",
                "thumbnail",
                "data:image",
            ]:
                self.assertNotIn(forbidden, public_json)

    def test_configure_readiness_warns_about_completed_handoff_material_and_keeps_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = output_dir / "_production_workbench"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            (output_dir / "images").mkdir(parents=True)
            processed_file = output_dir / "images" / "completed.png"
            processed_file.write_bytes(b"processed before preflight")
            metadata_dir.mkdir(parents=True)
            completion_note = metadata_dir / COMPLETION_NOTE_TXT
            completion_note.write_text("上一批中文交接说明\n", encoding="utf-8")
            summary_file = metadata_dir / PRODUCTION_RUN_SUMMARY_JSON
            summary_file.write_text('{"schema_version":"scan-qc.production-run.v1","status":"finished"}\n', encoding="utf-8")
            before = {
                processed_file: processed_file.read_bytes(),
                completion_note: completion_note.read_text(encoding="utf-8"),
                summary_file: summary_file.read_text(encoding="utf-8"),
            }

            readiness = WorkbenchController().configure(input_dir, output_dir)["folder_readiness"]

            self.assertEqual(readiness["existing_output_risk"]["kind"], "completed_handoff")
            self.assertTrue(readiness["existing_output_risk"]["completed_handoff_detected"])
            self.assertIn("完成交接材料", readiness["title_zh"])
            self.assertIn("先完成或归档上一批", readiness["message_zh"])
            self.assertIn("更换一个空的输出文件夹", " ".join(readiness["next_steps_zh"]))
            self.assertEqual(processed_file.read_bytes(), before[processed_file])
            self.assertEqual(completion_note.read_text(encoding="utf-8"), before[completion_note])
            self.assertEqual(summary_file.read_text(encoding="utf-8"), before[summary_file])
            self.assertTrue(processed_file.exists())
            self.assertTrue(completion_note.exists())
            self.assertTrue(summary_file.exists())

    def test_configure_readiness_guides_reusable_current_batch_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = output_dir / "_production_workbench"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            (output_dir / "images").mkdir(parents=True)
            (output_dir / "images" / "page.png").write_bytes(b"processed")
            metadata_dir.mkdir(parents=True)
            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "blocked",
                        "counts": {
                            "total_files": 2,
                            "processed_files": 1,
                            "resumed_files": 0,
                            "failed_files": 1,
                            "retry_list_files": 1,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            readiness = WorkbenchController().configure(input_dir, output_dir)["folder_readiness"]

            self.assertTrue(readiness["ready_to_start"])
            self.assertTrue(readiness["can_start_processing"])
            self.assertEqual(readiness["existing_output_risk"]["kind"], "reusable_current_batch")
            self.assertTrue(readiness["existing_output_risk"]["reusable_current_batch_detected"])
            self.assertIn("本批已有可复用处理结果", readiness["message_zh"])
            self.assertIn("只补齐缺失输出", readiness["message_zh"])
            self.assertIn("复用已有结果", " ".join(readiness["next_steps_zh"]))

    def test_configure_readiness_blocks_unwritable_output_with_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            controller = WorkbenchController()

            with patch("archive_scan_qc.local_workbench._folder_is_writable", return_value=False):
                readiness = controller.configure(input_dir, output_dir, metadata_dir)["folder_readiness"]

            self.assertTrue(readiness["aggregate_only"])
            self.assertEqual(readiness["status"], "blocked")
            self.assertFalse(readiness["ready_to_start"])
            self.assertFalse(readiness["can_start_processing"])
            self.assertFalse(readiness["output_writable"])
            self.assertIn("输出文件夹不能写入", readiness["title_zh"])
            self.assertIn("磁盘", " ".join(readiness["next_steps_zh"]))
            self.assertIn("换一个可以写入", " ".join(readiness["next_steps_zh"]))
            public_json = json.dumps(readiness, ensure_ascii=False, sort_keys=True)
            for forbidden in [str(root), str(input_dir), str(output_dir), "page.png", "sha256", "OCR", "thumbnail", "data:image"]:
                self.assertNotIn(forbidden, public_json)

    def test_configure_readiness_blocks_when_aggregate_disk_space_is_clearly_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page_1.png").write_bytes(b"a" * 200)
            (input_dir / "page_2.png").write_bytes(b"b" * 200)
            controller = WorkbenchController()

            with patch(
                "archive_scan_qc.local_workbench._aggregate_processing_precheck_from_snapshot",
                return_value={"retry_scope_safe": False, "needs_processing_files": 2},
            ), patch("archive_scan_qc.local_workbench.shutil.disk_usage") as disk_usage_mock:
                disk_usage_mock.return_value = shutil._ntuple_diskusage(total=10_000, used=9_900, free=100)
                readiness = controller.configure(input_dir, output_dir, metadata_dir)["folder_readiness"]

            self.assertEqual(readiness["status"], "blocked")
            self.assertFalse(readiness["ready_to_start"])
            self.assertEqual(readiness["output_space_check"]["status"], "blocked")
            self.assertLess(readiness["available_bytes"], readiness["estimated_required_bytes"])
            self.assertIn("空间明显不足", readiness["message_zh"])
            self.assertIn("清理输出磁盘空间", " ".join(readiness["next_steps_zh"]))

    def test_configure_readiness_keeps_ready_with_warning_when_space_estimate_is_unreliable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            controller = WorkbenchController()

            with patch("archive_scan_qc.local_workbench._scan_input_folder_preflight") as scan_mock:
                scan_mock.return_value = {
                    "input_empty": False,
                    "supported_image_count": 1,
                    "directory_snapshots": [{"relative_path": ".", "mtime_ns": 1, "ctime_ns": 1, "size": 1}],
                    "supported_file_snapshots": [{"relative_path": "page.png", "mtime_ns": 1, "ctime_ns": 1, "size": 0}],
                }
                readiness = controller.configure(input_dir, output_dir, metadata_dir)["folder_readiness"]

            self.assertEqual(readiness["status"], "ready")
            self.assertTrue(readiness["ready_to_start"])
            self.assertEqual(readiness["output_space_check"]["status"], "warning")
            self.assertIsNone(readiness["estimated_required_bytes"])
            self.assertIn("无法可靠估算", readiness["output_space_check"]["message_zh"])

    def test_configure_readiness_space_estimate_respects_retry_scope_safe_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page_1.png").write_bytes(b"a" * 100)
            (input_dir / "page_2.png").write_bytes(b"b" * 100)
            controller = WorkbenchController()

            with patch(
                "archive_scan_qc.local_workbench._aggregate_processing_precheck_from_snapshot",
                return_value={"retry_scope_safe": True, "needs_processing_files": 1, "reusable_files": 1},
            ), patch("archive_scan_qc.local_workbench.shutil.disk_usage") as disk_usage_mock:
                disk_usage_mock.return_value = shutil._ntuple_diskusage(total=10_000, used=5_000, free=5_000)
                readiness = controller.configure(input_dir, output_dir, metadata_dir)["folder_readiness"]

            self.assertEqual(readiness["status"], "ready")
            self.assertEqual(readiness["output_space_check"]["status"], "ok")
            self.assertEqual(readiness["estimated_required_bytes"], 120)
            self.assertEqual(readiness["available_bytes"], 5_000)

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

    def test_start_reuses_fresh_ready_preflight_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            controller = WorkbenchController()

            with patch.object(local_workbench_module, "_scan_input_folder_preflight", wraps=local_workbench_module._scan_input_folder_preflight) as scan_mock:
                controller.configure(input_dir, output_dir, metadata_dir, processing_mode="light")
                with patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                    run_mock.return_value = {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "artifacts": {},
                        "counts": {"total_files": 1, "processed_files": 1, "resumed_files": 0, "failed_files": 0, "retry_list_files": 0},
                    }
                    controller.start()
                    if controller._thread:
                        controller._thread.join(timeout=5)

            self.assertEqual(scan_mock.call_count, 1)
            status = controller.status()
            self.assertEqual(status["preflight_reuse_summary"]["status"], "reused")
            self.assertEqual(status["preflight_reuse_summary"]["supported_image_count"], 1)
            run_mock.assert_called_once()
            config = run_mock.call_args.args[0]
            self.assertEqual(config.processing_mode, "light")

    def test_start_rescans_when_preflight_input_snapshot_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            controller = WorkbenchController()

            with patch.object(local_workbench_module, "_scan_input_folder_preflight", wraps=local_workbench_module._scan_input_folder_preflight) as scan_mock:
                controller.configure(input_dir, output_dir, metadata_dir)
                (input_dir / "added.png").write_bytes(b"image")
                with patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                    run_mock.return_value = {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "artifacts": {},
                        "counts": {"total_files": 2, "processed_files": 2, "resumed_files": 0, "failed_files": 0, "retry_list_files": 0},
                    }
                    controller.start()
                    if controller._thread:
                        controller._thread.join(timeout=5)

            self.assertGreaterEqual(scan_mock.call_count, 2)
            status = controller.status()
            self.assertEqual(status["preflight_reuse_summary"]["status"], "rescanned")
            self.assertEqual(status["preflight_reuse_summary"]["reason"], "input_folder_changed")
            self.assertEqual(status["preflight_reuse_summary"]["supported_image_count"], 2)
            run_mock.assert_called_once()

    def test_start_rescans_when_processing_mode_changes_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir, processing_mode="light")
            with controller._lock:
                controller.processing_mode = "standard"

            with patch.object(local_workbench_module, "_scan_input_folder_preflight", wraps=local_workbench_module._scan_input_folder_preflight) as scan_mock:
                with patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                    run_mock.return_value = {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "artifacts": {},
                        "counts": {"total_files": 1, "processed_files": 1, "resumed_files": 0, "failed_files": 0, "retry_list_files": 0},
                    }
                    controller.start()
                    if controller._thread:
                        controller._thread.join(timeout=5)

            self.assertGreaterEqual(scan_mock.call_count, 1)
            status = controller.status()
            self.assertEqual(status["preflight_reuse_summary"]["status"], "rescanned")
            self.assertEqual(status["preflight_reuse_summary"]["reason"], "preflight_identity_changed")
            run_mock.assert_called_once()
            self.assertEqual(run_mock.call_args.args[0].processing_mode, "standard")

    def test_start_rescans_when_output_folder_changes_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            changed_output_dir = root / "changed-output"
            changed_metadata_dir = root / "changed-metadata"
            changed_output_dir.mkdir()
            changed_metadata_dir.mkdir()
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)
            with controller._lock:
                controller.derivatives_dir = changed_output_dir
                controller.metadata_dir = changed_metadata_dir

            with patch.object(local_workbench_module, "_scan_input_folder_preflight", wraps=local_workbench_module._scan_input_folder_preflight) as scan_mock:
                with patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                    run_mock.return_value = {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "artifacts": {},
                        "counts": {"total_files": 1, "processed_files": 1, "resumed_files": 0, "failed_files": 0, "retry_list_files": 0},
                    }
                    controller.start()
                    if controller._thread:
                        controller._thread.join(timeout=5)

            self.assertGreaterEqual(scan_mock.call_count, 1)
            status = controller.status()
            self.assertEqual(status["preflight_reuse_summary"]["status"], "rescanned")
            self.assertEqual(status["preflight_reuse_summary"]["reason"], "preflight_identity_changed")
            run_mock.assert_called_once()
            config = run_mock.call_args.args[0]
            self.assertEqual(config.derivative_output_dir.resolve(), changed_output_dir.resolve())
            self.assertEqual(config.metadata_output_dir.resolve(), changed_metadata_dir.resolve())

    def test_start_rescans_when_preflight_snapshot_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "page.png").write_bytes(b"image")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)
            with controller._lock:
                controller._last_preflight_snapshot["created_monotonic"] -= local_workbench_module.PREFLIGHT_SNAPSHOT_MAX_AGE_SECONDS + 1

            with patch.object(local_workbench_module, "_scan_input_folder_preflight", wraps=local_workbench_module._scan_input_folder_preflight) as scan_mock:
                with patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                    run_mock.return_value = {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "artifacts": {},
                        "counts": {"total_files": 1, "processed_files": 1, "resumed_files": 0, "failed_files": 0, "retry_list_files": 0},
                    }
                    controller.start()
                    if controller._thread:
                        controller._thread.join(timeout=5)

            self.assertGreaterEqual(scan_mock.call_count, 1)
            status = controller.status()
            self.assertEqual(status["preflight_reuse_summary"]["status"], "rescanned")
            self.assertEqual(status["preflight_reuse_summary"]["reason"], "expired_preflight_snapshot")
            run_mock.assert_called_once()

    def test_start_does_not_reuse_unsupported_preflight_to_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            (input_dir / "notes.txt").write_text("not an image", encoding="utf-8")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            with patch("archive_scan_qc.local_workbench.run_production_folder") as run_mock:
                with self.assertRaises(WorkbenchPreflightError) as raised:
                    controller.start()

            self.assertEqual(raised.exception.guidance["kind"], "no_supported_images")
            self.assertEqual(raised.exception.guidance["supported_image_count"], 0)
            self.assertEqual(controller.status()["preflight_reuse_summary"]["status"], "rescanned")
            run_mock.assert_not_called()

    def test_recreated_controller_restores_completed_batch_aggregate_state(self) -> None:
        private_hash = "f" * 64
        private_ocr = "PRIVATE_OCR_TEXT_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            metadata_dir.mkdir()
            private_file = input_dir / "private_page_alpha.png"
            private_file.write_bytes(b"fake image placeholder")
            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "counts": {
                            "total_files": 3,
                            "openable_files": 3,
                            "processed_files": 2,
                            "resumed_files": 1,
                            "failed_files": 0,
                            "retry_list_files": 0,
                            "reused_files": 1,
                            "reprocessed_files": 2,
                        },
                        "operator_summary": {
                            "message_zh": "处理后图片已经准备好。",
                            "total_source_images": 3,
                            "derivative_images_ready": 3,
                            "files_needing_attention": 0,
                            "input_folder": str(input_dir),
                            "metadata_folder": str(metadata_dir),
                        },
                        "local_reuse_summary": {
                            "schema_version": "scan-qc.local-processing-reuse-summary.v1",
                            "aggregate_only": True,
                            "reused_files": 1,
                            "reprocessed_files": 2,
                            "failed_files": 0,
                            "remaining_files": 0,
                        },
                        "debug_should_not_surface": {
                            "file": private_file.name,
                            "path": str(private_file),
                            "sha256": private_hash,
                            "ocr": private_ocr,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / "production_run_progress.json").write_text(
                json.dumps({"schema_version": "scan-qc.production-run-progress.v1", "state": "finished"}),
                encoding="utf-8",
            )
            restored_decisions = decision_summary(
                [
                    ("PRQ000001", "needs_rescan"),
                    ("PRQ000002", "keep_original_trace"),
                ]
            )
            restored_decisions["review_counts"]["needs_reprocess"] = 1
            restored_decisions["aggregate_counts"]["review_completion"]["counts"]["needs_reprocess"] = 1
            restored_decisions["review_counts"]["needs_rescan"] = 0
            restored_decisions["review_counts"]["needs_reprocess"] = 0
            restored_decisions["aggregate_counts"]["review_completion"]["counts"]["needs_rescan"] = 1
            restored_decisions["aggregate_counts"]["review_completion"]["counts"]["needs_reprocess"] = 1
            (metadata_dir / REVIEW_DECISION_SUMMARY_JSON).write_text(
                json.dumps(restored_decisions, ensure_ascii=False),
                encoding="utf-8",
            )

            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)
            restored = WorkbenchController()
            status = restored.configure(input_dir, output_dir, metadata_dir)

            self.assertFalse(status["running"])
            self.assertEqual(status["restored_batch"]["kind"], "completed")
            self.assertEqual(status["restored_batch"]["derivative_images_ready"], 3)
            self.assertTrue(status["restored_batch"]["can_open_output_folder"])
            self.assertFalse(status["restored_batch"]["auto_started"])
            self.assertEqual(status["completion_panel"]["title_zh"], "已恢复本批完成状态")
            self.assertEqual(status["completion_panel"]["processed_output_images"], 3)
            self.assertEqual(status["completion_panel"]["needs_rescan_images"], 1)
            self.assertEqual(status["completion_panel"]["needs_reprocess_images"], 1)
            self.assertEqual(status["completion_panel"]["keep_original_images"], 1)
            self.assertIn("需要重扫 1 张；需要重新处理 1 张。", status["completion_panel"]["next_steps_zh"])
            self.assertIn("确认保留原貌 1 张。", status["completion_panel"]["next_steps_zh"])
            self.assertEqual(status["completion_panel"]["local_reuse_summary"]["reused_files"], 1)
            assert_public_restore_payload_is_private(
                self,
                {
                    "restored_batch": status["restored_batch"],
                    "completion_panel": status["completion_panel"],
                    "recovery_guidance": status["recovery_guidance"],
                },
                [str(root), str(private_file), private_file.name, private_hash, private_ocr, ".png", "sha256", "OCR"],
            )

    def test_recreated_controller_blocks_completed_handoff_when_aggregates_disagree(self) -> None:
        private_hash = "f" * 64
        private_ocr = "PRIVATE_RESTORED_OCR_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            metadata_dir.mkdir()
            private_file = input_dir / "private_restored_page.jpg"
            private_file.write_bytes(b"fake image placeholder")
            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "counts": {
                            "total_files": 5,
                            "openable_files": 5,
                            "p0_findings": 0,
                            "processed_files": 2,
                            "resumed_files": 0,
                            "skipped_files": 0,
                            "failed_files": 0,
                            "retry_list_files": 0,
                        },
                        "operator_summary": {
                            "message_zh": "处理后图片已经准备好。",
                            "total_source_images": 5,
                            "derivative_images_ready": 5,
                            "files_needing_attention": 0,
                            "input_folder": str(input_dir),
                        },
                        "debug_should_not_surface": {
                            "file": private_file.name,
                            "path": str(private_file),
                            "sha256": private_hash,
                            "ocr": private_ocr,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / "production_run_progress.json").write_text(
                json.dumps({"schema_version": "scan-qc.production-run-progress.v1", "state": "finished"}),
                encoding="utf-8",
            )
            (metadata_dir / REVIEW_DECISION_SUMMARY_JSON).write_text(
                json.dumps(decision_summary([]), ensure_ascii=False),
                encoding="utf-8",
            )
            (metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "decision_summary": {
                            "completion_status": "complete",
                            "total_decisions": 0,
                            "pending": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / COMPLETION_NOTE_TXT).write_text("历史完成说明", encoding="utf-8")

            status = WorkbenchController().configure(input_dir, output_dir, metadata_dir)

            self.assertEqual(status["restored_batch"]["kind"], "handoff_count_mismatch")
            self.assertFalse(status["restored_batch"]["can_open_output_folder"])
            self.assertTrue(status["restored_batch"]["can_prepare_next_batch"])
            self.assertIsNone(status["completion_panel"])
            self.assertEqual(status["recovery_guidance"]["title_zh"], "交接前数量需要确认")
            self.assertIn("当前不能显示为可以交接", status["recovery_guidance"]["message_zh"])
            assert_public_restore_payload_is_private(
                self,
                {
                    "restored_batch": status["restored_batch"],
                    "completion_panel": status["completion_panel"],
                    "recovery_guidance": status["recovery_guidance"],
                },
                [str(root), str(private_file), private_file.name, private_hash, private_ocr, ".jpg", "sha256", "OCR"],
            )
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)
            with self.assertRaisesRegex(ValueError, "本批还没有完成"):
                controller.open_output_folder()

    def test_recreated_controller_restores_review_queue_aggregate_state(self) -> None:
        private_hash = "e" * 64
        private_ocr = "PRIVATE_REVIEW_OCR_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            metadata_dir.mkdir()
            private_name = "private_review_page.tif"
            (input_dir / private_name).write_bytes(b"fake image placeholder")
            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "needs_review",
                        "counts": {
                            "total_files": 2,
                            "openable_files": 2,
                            "processed_files": 2,
                            "resumed_files": 0,
                            "failed_files": 0,
                            "retry_list_files": 0,
                        },
                        "operator_summary": {
                            "message_zh": "还有图片需要人工确认。",
                            "total_source_images": 2,
                            "derivative_images_ready": 2,
                            "files_needing_attention": 2,
                        },
                        "private_debug": {"relative_path": private_name, "hash": private_hash, "ocr": private_ocr},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / "production_run_progress.json").write_text(
                json.dumps({"schema_version": "scan-qc.production-run-progress.v1", "state": "needs_review"}),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-review-queue.v1",
                        "summary": {"total_items": 2, "ready_for_operator_review": True},
                        "items": [
                            {"local_id": "PRQ000001", "relative_path": private_name, "reason_zh": "需要确认。"},
                            {"local_id": "PRQ000002", "relative_path": "another_private_page.tif", "reason_zh": "需要确认。"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / REVIEW_DECISION_DRAFT_JSON).write_text(
                json.dumps(decision_summary([("PRQ000001", "false_positive"), ("PRQ000002", "pending")]), ensure_ascii=False),
                encoding="utf-8",
            )

            restored = WorkbenchController()
            status = restored.configure(input_dir, output_dir, metadata_dir)

            self.assertFalse(status["running"])
            self.assertEqual(status["restored_batch"]["kind"], "needs_review")
            self.assertEqual(status["restored_batch"]["total_review_items"], 2)
            self.assertEqual(status["restored_batch"]["reviewed_items"], 1)
            self.assertEqual(status["restored_batch"]["pending_items"], 1)
            self.assertIsNone(status["completion_panel"])
            assert_public_restore_payload_is_private(
                self,
                {"restored_batch": status["restored_batch"], "recovery_guidance": status["recovery_guidance"]},
                [str(root), private_name, "another_private_page.tif", private_hash, private_ocr, ".tif", "relative_path", "hash", "OCR"],
            )

    def test_recreated_controller_treats_running_progress_as_interrupted_not_complete(self) -> None:
        private_file = "private_interrupted_page.jpg"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            metadata_dir.mkdir()
            (input_dir / private_file).write_bytes(b"fake image placeholder")
            (metadata_dir / "production_run_progress.json").write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run-progress.v1",
                        "state": "running",
                        "message_zh": "正在生成处理后图片。",
                        "summary": str(metadata_dir / "private_summary_path.json"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            restored = WorkbenchController()
            status = restored.configure(input_dir, output_dir, metadata_dir)

            self.assertFalse(status["running"])
            self.assertEqual(status["restored_batch"]["kind"], "interrupted_or_blocked")
            self.assertFalse(status["restored_batch"]["auto_started"])
            self.assertFalse(status["restored_batch"]["can_open_output_folder"])
            self.assertNotEqual(status["recovery_guidance"]["kind"], "processing_running")
            self.assertEqual(status["recovery_guidance"]["kind"], "processing_interrupted")
            self.assertIsNone(status["completion_panel"])
            assert_public_restore_payload_is_private(
                self,
                {"restored_batch": status["restored_batch"], "recovery_guidance": status["recovery_guidance"]},
                [str(root), private_file, ".jpg", "private_summary_path"],
            )

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
        private_hash = "b" * 64
        private_ocr = "PRIVATE_COMPLETION_OCR_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            private_file = input_dir / "private_completion_page.png"
            private_file.write_bytes(b"fake image placeholder")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "operator_summary": {"total_source_images": 8, "derivative_images_ready": 7},
                        "counts": {"total_files": 8, "openable_files": 8, "processed_files": 7, "resumed_files": 0},
                        "local_reuse_summary": {
                            "schema_version": "scan-qc.local-processing-reuse-summary.v1",
                            "aggregate_only": True,
                            "total_files": 8,
                            "reused_files": 2,
                            "reprocessed_files": 5,
                            "failed_files": 0,
                            "remaining_files": 0,
                            "next_action_zh": "无需整批重跑，检查输出文件夹后交接。",
                        },
                        "stage_timings": {
                            "schema_version": "scan-qc.production-stage-timings.v1",
                            "aggregate_only": True,
                            "stages": [
                                {
                                    "id": "scan",
                                    "label_zh": "PRIVATE_LABEL_SHOULD_NOT_LEAK",
                                    "elapsed_seconds": 1.2,
                                    "status": "completed",
                                    "private_path": str(private_file),
                                },
                                {
                                    "id": "process",
                                    "label_zh": "生成处理后图片",
                                    "elapsed_seconds": 8.5,
                                    "status": "running",
                                    "ocr_text": private_ocr,
                                },
                                {
                                    "id": "summarize",
                                    "label_zh": "整理处理结果",
                                    "elapsed_seconds": 0.4,
                                    "status": "completed",
                                    "sha256": private_hash,
                                },
                            ],
                        },
                        "private_debug": {
                            "path": str(private_file),
                            "file_name": private_file.name,
                            "sha256": private_hash,
                            "ocr_text": private_ocr,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            summary = decision_summary(
                [
                    ("PRQ000001", "needs_rescan"),
                    ("PRQ000002", "fixed_externally"),
                    ("PRQ000003", "false_positive"),
                ]
            )
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
                    "decision": "reprocess",
                    "decided_at": "2026-05-13T11:00:30.000Z",
                    "note_zh": "已经重新处理。",
                },
                {
                    "scope": "production_review_queue",
                    "local_id": "PRQ000003",
                    "decision": "keep_original_trace",
                    "decided_at": "2026-05-13T11:01:00.000Z",
                    "note_zh": "属于档案原貌，保留即可。",
                },
            ]
            result = controller.save_review_decisions(summary)

            self.assertTrue(result["finished"])
            self.assertEqual(
                result["message_zh"],
                "本批已完成：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
            )
            self.assertEqual(result["decision_summary"]["completion_status"], "complete")
            self.assertEqual(result["completion_panel"]["title_zh"], "本批已完成")
            self.assertEqual(result["completion_panel"]["completion_status_zh"], "本批已完成")
            self.assertEqual(result["completion_panel"]["manual_work_zh"], "没有待人工处理图片")
            self.assertEqual(result["completion_panel"]["admin_handoff_zh"], "不需要")
            self.assertEqual(result["completion_panel"]["total_review_items"], 3)
            self.assertEqual(result["completion_panel"]["reviewed_items"], 3)
            self.assertEqual(result["completion_panel"]["pending_items"], 0)
            self.assertEqual(result["completion_panel"]["total_source_images"], 8)
            self.assertEqual(result["completion_panel"]["processed_output_images"], 7)
            self.assertEqual(result["completion_panel"]["needs_rescan_images"], 1)
            self.assertEqual(result["completion_panel"]["needs_reprocess_images"], 1)
            self.assertEqual(result["completion_panel"]["keep_original_images"], 1)
            self.assertEqual(
                result["completion_panel"]["local_reuse_summary"],
                {
                    "schema_version": "scan-qc.local-processing-reuse-summary.v1",
                    "aggregate_only": True,
                    "total_files": 8,
                    "reused_files": 2,
                    "reprocessed_files": 5,
                    "failed_files": 0,
                    "remaining_files": 0,
                    "next_action_zh": "无需整批重跑，检查输出文件夹后交接。",
                    "message_zh": "本批共 8 张：已复用 2 张，实际重新处理 5 张，仍失败 0 张，剩余待处理 0 张。无需整批重跑，检查输出文件夹后交接。",
                },
            )
            self.assertEqual(
                result["completion_panel"]["closure_gate_summary"],
                {
                    "open_p0_count": 0,
                    "open_p1_count": 0,
                    "manually_handled_count": 3,
                    "can_complete_delivery": True,
                    "operator_message_zh": "P0/P1 问题已经有处理结论，可以完成交接。",
                },
            )
            self.assertEqual(result["completion_panel"]["processing_mode"]["id"], "standard")
            self.assertEqual(result["completion_panel"]["processing_mode"]["label_zh"], "标准优化")
            self.assertIn("推荐用于正常批量生产", result["completion_panel"]["processing_mode"]["purpose_zh"])
            self.assertIn("生成处理后优化图片", result["completion_panel"]["processing_mode"]["output_zh"])
            self.assertNotIn(str(input_dir), json.dumps(result["completion_panel"]["processing_mode"], ensure_ascii=False))
            self.assertTrue(result["completion_panel"]["decision_summary_saved"])
            self.assertTrue(result["completion_panel"]["verification_summary_saved"])
            self.assertTrue(result["completion_panel"]["completion_note_saved"])
            self.assertEqual(result["saved"], {"decision_summary": True, "verification_summary": True, "completion_note": True})
            self.assertNotIn("folders", result)
            self.assertEqual(
                result["completion_panel"]["next_steps_zh"],
                [
                    "打开输出文件夹，检查 7 张处理后图片的数量和画面状态。",
                    "需要重扫 1 张；需要重新处理 1 张。",
                    "确认保留原貌 1 张。",
                    "本机状态文件夹已保存复核结果和交接说明，正常界面不显示具体路径或文件名。",
                    "需要继续加工时，点击准备下一批；当前复核队列会清空。为新批次必须重新选择扫描原图文件夹，不要混用批次；输出文件夹可沿用上次保存的位置。",
                    "如果仍有异常或不能交接，请交管理员处理。",
                ],
            )
            self.assertTrue((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            saved_summary = json.loads((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).read_text(encoding="utf-8"))
            self.assertEqual(saved_summary["operator_name"], "复核员乙")
            self.assertEqual(saved_summary["operator_decisions"][0]["note_zh"], "边缘不清楚，需要补扫。")
            self.assertEqual(saved_summary["operator_decisions"][0]["decided_at"], "2026-05-13T11:00:00.000Z")
            completion_note = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")
            self.assertIn("本批次是否完成：已完成，可交接", completion_note)
            self.assertIn("复核人员：复核员乙", completion_note)
            self.assertIn("处理方式：标准优化", completion_note)
            self.assertIn("推荐用于正常批量生产", completion_note)
            self.assertIn("扫描原图总数：8 张", completion_note)
            self.assertIn("复核结果和交接说明已保存到本机状态文件夹", completion_note)
            self.assertIn("处理后图片数量：7 张", completion_note)
            self.assertIn("需要重扫：1 张", completion_note)
            self.assertIn("需要重新处理：1 张", completion_note)
            self.assertIn("确认保留原貌：1 张", completion_note)
            self.assertIn("待决定：0", completion_note)
            self.assertIn(
                "本批共 8 张：已复用 2 张，实际重新处理 5 张，仍失败 0 张，剩余待处理 0 张。无需整批重跑，检查输出文件夹后交接。",
                completion_note,
            )
            self.assertIn("交接前检查：打开输出文件夹", completion_note)
            self.assertIn("当前复核队列", completion_note)
            self.assertIn("不要混用批次", completion_note)
            self.assertIn("下一批：", completion_note)
            self.assertIn("未关闭 P0：0", completion_note)
            self.assertIn("未关闭 P1：0", completion_note)
            self.assertIn("已有人工处理结论：3", completion_note)
            self.assertIn(
                "聚合阶段耗时：检查扫描图片 1.2 秒、生成处理后图片 8.5 秒（进行中）、整理处理结果 0.4 秒。",
                completion_note,
            )
            self.assertNotIn("PRIVATE_LABEL_SHOULD_NOT_LEAK", completion_note)
            verification = json.loads((metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "pass")
            public_result_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
            assert_public_restore_payload_is_private(
                self,
                {"result": result, "completion_note": completion_note},
                [
                    str(root),
                    str(input_dir),
                    str(output_dir),
                    str(metadata_dir),
                    str(private_file),
                    private_file.name,
                    private_hash,
                    private_ocr,
                    "PRIVATE_LABEL_SHOULD_NOT_LEAK",
                    REVIEW_DECISION_SUMMARY_JSON,
                    REVIEW_DECISION_VERIFICATION_JSON,
                    COMPLETION_NOTE_TXT,
                    "sha256",
                    "ocr_text",
                    ".png",
                ],
            )
            self.assertNotIn("derivatives_dir", public_result_text)
            self.assertNotIn("metadata_dir", public_result_text)
            self.assertNotIn("completion_note_path", public_result_text)

    def test_final_completion_keeps_handoff_stable_without_safe_stage_timings(self) -> None:
        cases = {
            "missing": None,
            "empty": {"schema_version": "scan-qc.production-stage-timings.v1", "aggregate_only": True, "stages": []},
            "non_aggregate": {
                "schema_version": "scan-qc.production-stage-timings.v1",
                "aggregate_only": False,
                "stages": [{"id": "scan", "label_zh": "检查扫描图片", "elapsed_seconds": 1.2, "status": "completed"}],
            },
            "unsafe_stage": {
                "schema_version": "scan-qc.production-stage-timings.v1",
                "aggregate_only": True,
                "stages": [
                    {
                        "id": "private_stage",
                        "label_zh": "私有文件 Secret_Case_0001.tif",
                        "elapsed_seconds": 2.4,
                        "status": "completed",
                        "thumbnail": "data:image/png;base64,PRIVATE",
                    },
                    {"id": "scan", "label_zh": "检查扫描图片", "elapsed_seconds": 0, "status": "pending"},
                ],
            },
        }
        for case_name, stage_timings in cases.items():
            with self.subTest(case_name=case_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    input_dir = root / "input"
                    output_dir = root / "output"
                    metadata_dir = root / "metadata"
                    input_dir.mkdir()
                    controller = WorkbenchController()
                    controller.configure(input_dir, output_dir, metadata_dir)

                    run_summary = {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "counts": {
                            "total_files": 0,
                            "openable_files": 0,
                            "processed_files": 0,
                            "resumed_files": 0,
                            "skipped_files": 0,
                            "failed_files": 0,
                            "retry_list_files": 0,
                        },
                        "operator_summary": {
                            "total_source_images": 0,
                            "derivative_images_ready": 0,
                            "files_needing_attention": 0,
                        },
                    }
                    if stage_timings is not None:
                        run_summary["stage_timings"] = stage_timings
                    (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                        json.dumps(run_summary, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    (metadata_dir / PRODUCTION_RUN_PROGRESS_JSON).write_text(
                        json.dumps({"schema_version": "scan-qc.production-run-progress.v1", "state": "finished"}),
                        encoding="utf-8",
                    )
                    (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                        json.dumps({"summary": {"total_items": 0}}, ensure_ascii=False),
                        encoding="utf-8",
                    )

                    result = controller.save_review_decisions(decision_summary([]))

                    completion_note = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")
                    self.assertTrue(result["finished"])
                    self.assertIn("本批次是否完成：已完成，可交接", completion_note)
                    self.assertNotIn("聚合阶段耗时", completion_note)
                    self.assertNotIn("Secret_Case_0001.tif", completion_note)
                    self.assertNotIn(
                        "thumbnail",
                        json.dumps({"result": result, "completion_note": completion_note}, ensure_ascii=False),
                    )

    def test_final_completion_allows_no_review_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir, processing_mode="qc_only")

            summary = decision_summary([])
            result = controller.save_review_decisions(summary)

            self.assertTrue(result["finished"])
            self.assertEqual(result["decision_summary"]["completion_status"], "complete")
            self.assertEqual(result["completion_panel"]["total_review_items"], 0)
            self.assertEqual(result["completion_panel"]["reviewed_items"], 0)
            self.assertEqual(result["completion_panel"]["pending_items"], 0)
            self.assertEqual(result["completion_panel"]["processing_mode"]["id"], "qc_only")
            self.assertEqual(result["completion_panel"]["processing_mode"]["label_zh"], "只质检不修图")
            self.assertIn("不会生成处理后优化图片", result["completion_panel"]["processing_mode"]["output_zh"])
            self.assertNotIn("local_reuse_summary", result["completion_panel"])
            self.assertTrue((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            completion_note = (metadata_dir / COMPLETION_NOTE_TXT).read_text(encoding="utf-8")
            self.assertIn("复核总数：0", completion_note)
            self.assertIn("只质检不修图", completion_note)
            verification = json.loads((metadata_dir / REVIEW_DECISION_VERIFICATION_JSON).read_text(encoding="utf-8"))
            self.assertEqual(verification["status"], "pass")

    def test_final_completion_allows_handoff_when_aggregates_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "counts": {
                            "total_files": 4,
                            "openable_files": 4,
                            "p0_findings": 2,
                            "processed_files": 3,
                            "resumed_files": 1,
                            "skipped_files": 0,
                            "failed_files": 0,
                            "retry_list_files": 0,
                        },
                        "operator_summary": {
                            "total_source_images": 4,
                            "derivative_images_ready": 4,
                            "files_needing_attention": 2,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / "production_run_progress.json").write_text(
                json.dumps({"schema_version": "scan-qc.production-run-progress.v1", "state": "needs_review"}),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps({"summary": {"total_items": 2}}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = controller.save_review_decisions(
                decision_summary([("PRQ000001", "needs_rescan"), ("PRQ000002", "false_positive")])
            )

            self.assertTrue(result["finished"])
            self.assertEqual(result["completion_panel"]["completion_status_zh"], "本批已完成")
            self.assertEqual(result["completion_panel"]["processed_output_images"], 4)
            self.assertTrue(result["completion_panel"]["open_output_folder_available"])
            self.assertIn("打开输出文件夹", result["completion_panel"]["next_steps_zh"][0])

    def test_final_completion_blocks_handoff_when_summary_counts_disagree(self) -> None:
        private_hash = "a" * 64
        private_ocr = "PRIVATE_OCR_SHOULD_NOT_LEAK"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            private_file = input_dir / "private_mismatch_page.png"
            private_file.write_bytes(b"fake image placeholder")
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            (metadata_dir / PRODUCTION_RUN_SUMMARY_JSON).write_text(
                json.dumps(
                    {
                        "schema_version": "scan-qc.production-run.v1",
                        "status": "finished",
                        "counts": {
                            "total_files": 3,
                            "openable_files": 3,
                            "p0_findings": 1,
                            "processed_files": 1,
                            "resumed_files": 0,
                            "skipped_files": 0,
                            "failed_files": 0,
                            "retry_list_files": 0,
                        },
                        "operator_summary": {
                            "total_source_images": 3,
                            "derivative_images_ready": 3,
                            "files_needing_attention": 1,
                            "debug_path": str(private_file),
                        },
                        "debug_should_not_surface": {
                            "file": private_file.name,
                            "sha256": private_hash,
                            "ocr": private_ocr,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (metadata_dir / "production_run_progress.json").write_text(
                json.dumps({"schema_version": "scan-qc.production-run-progress.v1", "state": "needs_review"}),
                encoding="utf-8",
            )
            (metadata_dir / PRODUCTION_REVIEW_QUEUE_JSON).write_text(
                json.dumps({"summary": {"total_items": 1}}, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "本机状态数量互相不一致"):
                controller.save_review_decisions(decision_summary([("PRQ000001", "false_positive")]))

            self.assertFalse((metadata_dir / COMPLETION_NOTE_TXT).exists())
            self.assertFalse((metadata_dir / REVIEW_DECISION_SUMMARY_JSON).exists())
            assert_public_restore_payload_is_private(
                self,
                {
                    "message_zh": "本机状态数量互相不一致，当前不能显示为可以交接。",
                    "next_steps_zh": [
                        "请先检查输出文件夹中的处理后图片数量。",
                        "如果仍有待确认图片，请重新确认待看图项目。",
                        "如本批处理被中断或文件夹被改动，请重新开始处理本批。",
                    ],
                },
                [str(root), str(private_file), private_file.name, private_hash, private_ocr, ".png", "sha256", "OCR"],
            )

    def test_open_output_folder_requires_completed_batch_and_returns_operator_safe_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            output_dir = root / "output"
            metadata_dir = root / "metadata"
            input_dir.mkdir()
            controller = WorkbenchController()
            controller.configure(input_dir, output_dir, metadata_dir)

            with self.assertRaisesRegex(ValueError, "本批还没有完成"):
                controller.open_output_folder()

            controller.save_review_decisions(decision_summary([]))
            with patch.dict(os.environ, {"AI4ARCHIVE_OPEN_FOLDER_COMMAND": "test-open-folder"}), patch.object(
                local_workbench_module.subprocess, "Popen"
            ) as popen:
                result = controller.open_output_folder()

            self.assertTrue(result["opened"])
            self.assertEqual(result["message_zh"], "已打开输出文件夹。请检查处理后图片数量和画面状态。")
            self.assertNotIn(str(output_dir), json.dumps(result, ensure_ascii=False))
            popen.assert_called_once()

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
