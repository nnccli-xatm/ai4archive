from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_scan_qc import local_workbench as local_workbench_module
from archive_scan_qc.local_workbench import WorkbenchController, _pick_windows_folder_via_powershell


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_HTML = ROOT / "docs" / "production-workbench-prototype.html"
VALIDATOR = ROOT / "scripts" / "validate_production_workbench.py"


class ProductionWorkbenchRegressionGuardTests(unittest.TestCase):
    def test_production_workbench_validator_runs_in_unit_test_discovery(self) -> None:
        spec = importlib.util.spec_from_file_location("validate_production_workbench", VALIDATOR)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module.main(), 0)

    def test_operator_path_selection_has_no_browser_upload_controls(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            "从系统选择原图文件夹",
            "从系统选择输出文件夹",
            "/api/pick-folder",
            "pickInputButton",
            "pickOutputButton",
            "pickFolder",
        ]:
            self.assertIn(required, html)
        for forbidden in [
            'type="file"',
            "webkitdirectory",
            "directory multiple",
            "inputFolder",
            "outputFolder",
            "summaryFile",
            "浏览器辅助确认",
            "选择本机状态",
            "上传",
        ]:
            self.assertNotIn(forbidden, html)

    def test_comparison_preview_layout_keeps_visible_scrollable_image_area(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            ".preview-controls {\n      position: relative;\n      z-index: 2;",
            ".preview-zone {\n      display: grid;\n      grid-template-rows: auto 1fr;\n      gap: 12px;\n      align-items: start;\n      justify-items: center;\n      min-height: 620px;",
            "      overflow: auto;\n      background:",
            ".preview-frame {\n      position: relative;\n      z-index: 1;\n      width: min(100%, 520px);\n      min-height: 420px;",
            ".preview-frame.compact {\n      width: 100%;\n      min-height: 340px;",
            ".preview-frame.comparison-shell {\n      width: min(100%, 980px);\n      min-height: 520px;",
            "      .preview-zone {\n        padding: 14px;\n        min-height: 520px;",
            "      .preview-frame.compact {\n        min-height: 300px;",
            "      .preview-frame.comparison-shell {\n        min-height: 480px;",
            'els.previewFrame.classList.toggle("comparison-shell", canCompare && state.comparisonMode === "side_by_side");',
            '<div class="preview-comparison" aria-label="原图和处理后图片对比">',
            '<div class="comparison-title">原图</div>',
            '<div class="comparison-title">处理后图片</div>',
            "正在对比查看。看完后在右侧选择处理决定。",
        ]:
            self.assertIn(required, html)

    def test_windows_path_display_and_internal_wsl_path_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mount_root = root / "mnt"
            input_dir = mount_root / "c" / "Users" / "PS" / "batch" / "input"
            output_dir = mount_root / "c" / "Users" / "PS" / "batch" / "output"
            input_dir.mkdir(parents=True)
            (input_dir / "page.png").write_bytes(b"fake image placeholder")
            controller = WorkbenchController()

            with patch.object(local_workbench_module, "WINDOWS_DRIVE_MOUNT_ROOT", mount_root):
                status = controller.configure(r"C:\Users\PS\batch\input", r"C:\Users\PS\batch\output")

            self.assertEqual(status["folders"]["input"], r"C:\Users\PS\batch\input")
            self.assertEqual(status["folders"]["derivatives"], r"C:\Users\PS\batch\output")
            self.assertEqual(status["folders"]["metadata"], r"C:\Users\PS\batch\output\_production_workbench")
            self.assertEqual(controller.input_dir, input_dir.resolve())
            self.assertEqual(controller.derivatives_dir, output_dir.resolve())
            self.assertEqual(controller.metadata_dir, (output_dir / "_production_workbench").resolve())

    def test_windows_native_picker_remains_topmost_and_non_upload(self) -> None:
        with patch.object(local_workbench_module, "_run_folder_picker_command", return_value=r"C:\Users\PS\selected") as runner:
            self.assertEqual(_pick_windows_folder_via_powershell("选择原图"), r"C:\Users\PS\selected")

        command = runner.call_args.args[0]
        script = command[-1]
        self.assertIn("-STA", command)
        self.assertIn("$ownerForm.TopMost = $true", script)
        self.assertIn("$ownerForm.Activate(); $ownerForm.BringToFront()", script)
        self.assertIn("$dialog.ShowDialog($ownerForm)", script)
        self.assertNotIn("OpenFileDialog", script)

    def test_prepare_next_batch_clears_completed_batch_handoff_state(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")
        start = html.find("async function prepareNextBatch()")
        end = html.find("els.resetButton.addEventListener", start)
        self.assertNotEqual(start, -1)
        self.assertNotEqual(end, -1)
        body = html[start:end]

        for required in [
            'state.status = "choose_input";',
            "state.readyImages = 0;",
            "state.totalImages = 0;",
            "state.attentionFiles = 0;",
            "state.reviewItems = [];",
            "state.queueLoaded = false;",
            "state.decisions = {};",
            "state.finishConfirmationVisible = false;",
            "state.recoveryGuidance = null;",
            "state.folderReadiness = null;",
            'state.completionTitle = "等待完成本批";',
            'state.completionMessage = "完成并导出结果后，这里会显示本批交接清单。";',
            'state.completionStatusFact = "未完成";',
            "state.completionSteps = INITIAL_COMPLETION_STEPS.slice();",
            "请重新选择新一批扫描原图文件夹和输出文件夹，不要混用批次。",
        ]:
            self.assertIn(required, body)

        for stale_completed_batch_token in [
            'state.attentionFiles = 3;',
            'state.completionTitle = "本批已完成";',
            'state.completionStatusFact = "本批已完成";',
            "state.completionSteps = DEFAULT_COMPLETION_STEPS.slice();",
        ]:
            self.assertNotIn(stale_completed_batch_token, body)

        self.assertIn("NEXT_BATCH_STATUS_TEXT", html)
        self.assertIn("请重新选择扫描原图文件夹和输出文件夹", html)

    def test_completed_handoff_has_local_open_output_folder_action_without_path_disclosure(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")
        server = Path(local_workbench_module.__file__).read_text(encoding="utf-8")

        for required in [
            'id="openOutputFolderButton"',
            ">打开输出文件夹<",
            'id="openOutputFolderStatus"',
            "openOutputFolderAvailable",
            "openOutputFolder()",
            'apiPost("/api/open-output-folder", {})',
            "输出文件夹没有打开。请重新选择输出文件夹，或联系管理员处理。",
        ]:
            self.assertIn(required, html)
        for required in [
            'elif self.path == "/api/open-output-folder":',
            "open_output_folder",
            "_open_operator_folder",
            "_batch_has_completed",
            "处理后输出文件夹现在不能打开。请重新选择输出文件夹，或联系管理员处理。",
        ]:
            self.assertIn(required, server)
        self.assertIn("state.status === \"complete\" && state.outputChosen && state.openOutputFolderAvailable", html)


if __name__ == "__main__":
    unittest.main()
