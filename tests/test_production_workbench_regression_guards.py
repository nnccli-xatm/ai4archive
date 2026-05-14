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


if __name__ == "__main__":
    unittest.main()
