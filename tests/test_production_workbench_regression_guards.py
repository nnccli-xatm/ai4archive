from __future__ import annotations

import importlib.util
import json
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

    def test_aggregate_processing_wait_copy_uses_operator_safe_chinese(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            'id="aggregateProcessingText"',
            "function deriveAggregateProcessingLabel(summary, progress)",
            "function aggregateProcessingAvailable(aggregate)",
            "aggregate.aggregate_only === true",
            "aggregate.unavailable_reason",
            "聚合处理速度：",
            "张/分钟",
            "预计还需等待",
            "少于 1 分钟",
            "暂不能估算剩余时间，继续等待处理进度更新。",
            'els.aggregateProcessingText.classList.toggle("hidden", !state.aggregateProcessingLabel);',
        ]:
            self.assertIn(required, html)

        for forbidden_visible in [
            "missing_total_images",
            "missing_processed_images",
            "no_total_images",
            "no_processed_images",
            "no_elapsed_seconds",
            "null",
            "undefined",
        ]:
            self.assertNotIn(f"`{forbidden_visible}`", html)

    def test_aggregate_processing_fixtures_cover_running_and_finished_states(self) -> None:
        running_progress = json.loads(
            (ROOT / "docs" / "fixtures" / "production-run-running" / "production_run_progress.json").read_text(encoding="utf-8")
        )
        finished_summary = json.loads(
            (ROOT / "docs" / "fixtures" / "production-run-finished" / "production_run_summary.json").read_text(encoding="utf-8")
        )

        running = running_progress["aggregate_processing"]
        self.assertTrue(running["aggregate_only"])
        self.assertEqual(running["total_images"], 120)
        self.assertEqual(running["processed_images"], 48)
        self.assertEqual(running["remaining_images"], 72)
        self.assertEqual(running["images_per_minute"], 4.8)
        self.assertEqual(running["estimated_remaining_seconds"], 900.0)
        self.assertIsNone(running["unavailable_reason"])

        finished = finished_summary["aggregate_processing"]
        self.assertTrue(finished["aggregate_only"])
        self.assertEqual(finished["remaining_images"], 0)
        self.assertEqual(finished["images_per_minute"], 6.0)
        self.assertEqual(finished["estimated_remaining_seconds"], 0.0)
        self.assertIsNone(finished["unavailable_reason"])

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

    def test_review_decision_flow_auto_advances_to_clear_empty_state(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            "function hasActivePendingReview()",
            'return state.status === "needs_review" && pendingCount() > 0;',
            "const activeReviewMode = hasActivePendingReview();",
            "const activeItem = activeReviewMode ? item : null;",
            "els.currentIssue.textContent = activeItem",
            "? activeItem.issue",
            ': (pending ? (state.operatorMessage || "处理开始后在这里显示问题原因。") : "已经没有待确认图片。");',
            'els.previewSourceText.textContent = previewSourceLabel(activeItem || {}, Boolean(activeItem));',
            "button.disabled = !activeItem;",
            "renderPreview(activeItem, Boolean(activeItem));",
            "所有待确认图片都已确认，可以点击完成并导出结果。",
            "可以完成并导出结果。",
            "已自动显示下一张待确认图片",
            'function recordReviewDecision(decision, source = "button")',
            'recordReviewDecision(button.dataset.decision);',
            'if (recordReviewDecision(decision, "keyboard")) event.preventDefault();',
            'document.addEventListener("keydown", handleReviewDecisionShortcut);',
            'return Boolean(element.closest("#decisionDesk, .preview-zone"));',
            "function elementAcceptsTextInput(element)",
            'aria-keyshortcuts="1"',
            'aria-keyshortcuts="2"',
            'aria-keyshortcuts="3"',
            'aria-keyshortcuts="4"',
        ]:
            self.assertIn(required, html)

    def test_finish_export_blocks_render_actionable_operator_guidance(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            'id="finishBlocker"',
            'id="finishBlockerTitle"',
            'id="finishBlockerMessage"',
            'id="finishBlockerSteps"',
            'id="finishBlockerWaitButton"',
            'id="finishBlockerReviewButton"',
            'id="finishBlockerRetryButton"',
            "function finishBlockerFromError(error)",
            "blocking_reasons_zh",
            "完成前还要等待",
            "继续等待处理完成",
            "还有待确认项",
            "返回看图确认",
            "已经记录的复核决定会保留",
            "输出图片数量不足",
            "当前不会显示为本批已完成",
            "检查输出文件夹",
            "重试本批次",
            "state.finishBlocker = finishBlockerFromError(error);",
            "els.finishBlockerReviewButton.addEventListener",
        ]:
            self.assertIn(required, html)

        self.assertIn("state.status = \"complete\";", html)
        self.assertIn("applyCompletionPanel(payload.completion_panel);", html)

    def test_review_decision_buttons_keep_stable_dimensions(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            "#decisionActions button {\n      min-height: 54px;\n      font-size: 17px;\n    }",
            ".decision-actions .primary-choice {\n      min-height: 54px;",
            "button.classList.toggle(\"primary-choice\", activeItem && activeItem.suggestedAction === button.dataset.decision);",
            "button.classList.toggle(\"recommended-choice\", activeItem && activeItem.suggestedAction === button.dataset.decision);",
        ]:
            self.assertIn(required, html)

    def test_review_items_use_service_local_only_item_and_preview_urls(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            "function serviceQuery(params)",
            "const productionApiRoutes = Object.freeze({",
            "const localBridgeRoutes = Object.freeze({",
            "function apiPath(route, params = {})",
            "function productionApiPath(name, params = {})",
            "function localBridgePath(name)",
            "function productionReviewItemUrl(localId)",
            "/api/production/review-item",
            'return productionApiPath("reviewItem", { job_id: state.jobId, local_id: localId });',
            "function productionPreviewUrl(localId, source)",
            "/api/production/preview",
            'return productionApiPath("preview", { job_id: state.jobId, local_id: localId, source });',
            "function reviewActionsRequest(reviewDecisions)",
            'productionApiPath("progress", { job_id: state.jobId })',
            'productionApiPath("reviewActions")',
            'productionApiPath("finishExport")',
            "reviewItemUrl: productionReviewItemUrl(item.local_id || \"\")",
            "originalPreviewUrl: hasOriginal ? productionPreviewUrl(item.local_id || \"\", \"original\") : \"\"",
            "processedPreviewUrl: hasProcessed ? productionPreviewUrl(item.local_id || \"\", \"processed\") : \"\"",
            "state.jobId = summary.job_id || (progress && progress.job_id) || state.jobId || \"\";",
            "state.jobId = \"\";",
        ]:
            self.assertIn(required, html)

        self.assertNotIn("/api/preview/", html)

    def test_windows_path_display_and_internal_wsl_path_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mount_root = root / "mnt"
            input_dir = mount_root / "c" / "Users" / "PS" / "batch" / "input"
            output_dir = mount_root / "c" / "Users" / "PS" / "batch" / "output"
            input_dir.mkdir(parents=True)
            (input_dir / "page.png").write_bytes(b"fake image placeholder")
            controller = WorkbenchController()

            with patch.object(local_workbench_module, "WINDOWS_DRIVE_MOUNT_ROOT", mount_root), patch.object(
                local_workbench_module, "_running_on_native_windows", return_value=False
            ):
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
            'state.completionReuseMessage = "";',
            'state.completionStatusFact = "未完成";',
            "state.completionSteps = INITIAL_COMPLETION_STEPS.slice();",
            "const reusableOutputPath = state.lastServerOutputPath && els.outputPath.value.trim() === state.lastServerOutputPath",
            "state.outputChosen = Boolean(reusableOutputPath);",
            "els.inputPath.value = \"\";",
            "els.outputPath.value = reusableOutputPath;",
            "state.lastServerInputPath = \"\";",
            "state.lastServerOutputPath = reusableOutputPath;",
            "必须重新选择新一批扫描原图文件夹，不要混用批次。",
            "已沿用上次保存的输出文件夹提示；如本批要换位置，请重新选择输出文件夹。",
            "没有可安全沿用的输出文件夹提示，请重新选择输出文件夹。",
            "请重新选择新一批扫描原图文件夹；输出文件夹已保留上次保存的位置提示。",
        ]:
            self.assertIn(required, body)

        for stale_completed_batch_token in [
            "state.outputChosen = false;",
            "els.outputPath.value = \"\";",
            "state.lastServerOutputPath = \"\";",
            'state.attentionFiles = 3;',
            'state.completionTitle = "本批已完成";',
            'state.completionStatusFact = "本批已完成";',
            "state.completionSteps = DEFAULT_COMPLETION_STEPS.slice();",
        ]:
            self.assertNotIn(stale_completed_batch_token, body)

        self.assertIn("NEXT_BATCH_STATUS_TEXT", html)
        self.assertIn("请重新选择扫描原图文件夹", html)
        self.assertIn("输出文件夹可沿用上次保存的位置", html)

    def test_completed_handoff_has_local_open_output_folder_action_without_path_disclosure(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")
        server = Path(local_workbench_module.__file__).read_text(encoding="utf-8")

        for required in [
            'id="openOutputFolderButton"',
            ">打开输出文件夹<",
            'id="openOutputFolderStatus"',
            "openOutputFolderAvailable",
            "openOutputFolder()",
            'apiPost(localBridgePath("openOutputFolder"), {})',
            "输出文件夹没有打开。请重新选择输出文件夹，或联系管理员处理。",
        ]:
            self.assertIn(required, html)
        for required in [
            'elif self.path == "/api/open-output-folder":',
            'parsed.path == "/api/production/review-queue"',
            'parsed.path == "/api/production/progress"',
            'parsed.path == "/api/production/review-item"',
            'parsed.path == "/api/production/preview"',
            "production_progress",
            "production_review_queue",
            "production_review_item",
            "production_preview_path",
            "production_review_actions",
            "production_finish_export",
            "X-AI4-Local-Only",
            "X-AI4-Preview-Source",
            "open_output_folder",
            "_open_operator_folder",
            "_batch_has_completed",
            "处理后输出文件夹现在不能打开。请重新选择输出文件夹，或联系管理员处理。",
        ]:
            self.assertIn(required, server)
        self.assertIn("state.status === \"complete\" && state.outputChosen && state.openOutputFolderAvailable", html)
        self.assertIn("panel.completion_note_saved", html)
        self.assertNotIn("panel.completion_note_path", html)

    def test_completed_handoff_shows_only_aggregate_reuse_counts_when_available(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            'id="completionReuseMessage"',
            "function localReuseMessage(panel)",
            "panel.local_reuse_summary",
            "reuse.aggregate_only !== true",
            '"total_files", "reused_files", "reprocessed_files", "failed_files", "remaining_files"',
            "reuse.total_files",
            "reuse.remaining_files",
            "reuse.next_action_zh",
            "本批共 ${total} 张：已复用 ${reused} 张，实际重新处理 ${reprocessed} 张，仍失败 ${failed} 张，剩余待处理 ${remaining} 张。${nextAction}",
            "无需整批重跑，检查输出文件夹后交接。",
            "还有失败或待处理图片，请先重试本批次；仍失败再交管理员处理。",
            "state.completionReuseMessage = localReuseMessage(panel);",
            'els.completionReuseMessage.classList.toggle("hidden", !state.completionReuseMessage);',
        ]:
            self.assertIn(required, html)

        for forbidden in [
            "reuse.source_path",
            "reuse.relative_path",
            "reuse.sha256",
            "reuse.ocr_text",
            "reuse.thumbnail",
        ]:
            self.assertNotIn(forbidden, html)

    def test_running_state_shows_aggregate_remaining_work_and_locks_start_button(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            "已处理 ${Math.min(completed, total)} 张 / 共 ${total} 张；待处理 ${Math.max(0, total - completed)} 张",
            'els.startButton.textContent = inRunStatus() ? "处理中，请等待" : "开始处理";',
            'els.startButton.title = inRunStatus() ? "批次正在运行，不能重复开始处理。" : (canStartProcessing() ? "" : startBlockedMessage);',
            "function runningPreflightPlanMessage(summary)",
            "function runningPreflightSummary(summary)",
            "state.folderReadiness && state.folderReadiness.preflight_processing_summary",
            "summary && summary.preflight_processing_summary",
            "preflight.aggregate_only !== true",
            "preflight.retry_scope_safe !== true || rawState === \"unknown\"",
            "开始前判断：本批共 ${total} 张，预计可复用处理后输出 ${reusable} 张，预计需要新处理或补处理 ${needsProcessing} 张。",
            "开始前预检摘要暂不能安全用于判断可复用输出。",
            "当前聚合进度会继续显示已处理、剩余和预计等待",
            "处理完成或失败前不能更改文件夹和处理方式，也不要反复点击开始处理。",
        ]:
            self.assertIn(required, html)

        start = html.find("function runningPreflightSummary(summary)")
        end = html.find("function makeReviewItems(count)", start)
        self.assertNotEqual(start, -1)
        self.assertNotEqual(end, -1)
        running_plan_body = html[start:end]
        for forbidden in [
            "current_file",
            "currentPath",
            "sha256",
            "OCR",
            "row-level",
            "relative_path",
            "source_path",
            "file_name",
            "filename",
            "ocr_text",
            "thumbnail",
            "<img",
            "exception",
            "traceback",
            "stack",
        ]:
            self.assertNotIn(forbidden, running_plan_body.lower())

    def test_running_fixture_covers_safe_preflight_plan_counts(self) -> None:
        running_summary = json.loads(
            (ROOT / "docs" / "fixtures" / "production-run-running" / "production_run_summary.json").read_text(encoding="utf-8")
        )
        preflight = running_summary["preflight_processing_summary"]

        self.assertTrue(preflight["aggregate_only"])
        self.assertTrue(preflight["retry_scope_safe"])
        self.assertNotEqual(preflight["state"], "unknown")
        self.assertEqual(preflight["total_files"], 120)
        self.assertEqual(preflight["reusable_files"], 36)
        self.assertEqual(preflight["needs_processing_files"], 84)

    def test_progress_area_shows_only_aggregate_stage_timings(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")

        for required in [
            'id="stageTimingText"',
            'id="stageTimingAdviceText"',
            "stageTimingLabel",
            "stageTimingAdvice",
            "function deriveStageTimingLabel(summary, progress)",
            "function deriveStageTimingAdvice(summary, progress)",
            "source.stage_timings",
            "timings.aggregate_only !== true",
            "检查扫描图片",
            "生成处理后图片",
            "整理处理结果",
            "主要耗时在生成处理后图片，请继续等待；如长时间没有变化再交管理员处理。",
            "formatStageSeconds",
            "seconds.toFixed(1)",
            '["completed", "finished", "running"].includes(stage.status)',
            "^[\\u4e00-\\u9fff\\s]{2,18}$",
            'els.stageTimingText.classList.toggle("hidden", !state.stageTimingLabel);',
            'els.stageTimingAdviceText.classList.toggle("hidden", !state.stageTimingAdvice);',
        ]:
            self.assertIn(required, html)

        start = html.find("function safeStageTimingLabel(stage)")
        end = html.find("function makeReviewItems(count)", start)
        if start == -1:
            start = html.find("function safeStageTimingLabelInfo(stage)")
        self.assertNotEqual(start, -1)
        self.assertNotEqual(end, -1)
        body = html[start:end]
        for forbidden in [
            "source_path",
            "relative_path",
            "file_name",
            "filename",
            "ocr_text",
            "<img",
        ]:
            self.assertNotIn(forbidden, body.lower())

        advice_start = html.find("function deriveStageTimingAdvice(summary, progress)")
        advice_end = html.find("function runningPreflightSummary(summary)", advice_start)
        self.assertNotEqual(advice_start, -1)
        self.assertNotEqual(advice_end, -1)
        advice_body = html[advice_start:advice_end]
        for required in [
            '["blocked", "failed", "error"].includes(rawStatus)',
            "stage.safeForAdvice",
            "slowStageAdviceByLabel[stage.label]",
            "stage.elapsedSeconds > best.elapsedSeconds",
        ]:
            self.assertIn(required, advice_body)

    def test_start_preflight_shows_aggregate_count_and_chinese_recovery(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")
        server = Path(local_workbench_module.__file__).read_text(encoding="utf-8")

        for required in [
            "本批预检结果",
            "function preflightSummaryMessage(readiness)",
            "function preflightProcessingSummaryMessage(summary)",
            "readiness.preflight_processing_summary",
            "summary.aggregate_only !== true",
            "summary.retry_scope_safe === true",
            "summary.retry_scope_safe === false || rawState === \"unknown\"",
            "本批共 ${total} 张：可复用处理后输出 ${reusable} 张，需要新处理或补处理 ${needsProcessing} 张。",
            "开始前不能安全判断哪些输出可复用。",
            "系统会保守核对并补齐需要处理的输出",
            "已识别到 ${count} 张可处理图片",
            "本批预检未通过：请先选择扫描原图文件夹和输出文件夹。",
            "本批预检未通过：已识别到 0 张可处理图片，请确认是否选错原图文件夹。",
            "本批预检未通过：已识别到 0 张可处理图片，请确认原图格式是否支持。",
            "但输出文件夹不能写入",
            "blockedStartMessage()",
            "return state.status === \"ready\" && Boolean(state.folderReadiness && state.folderReadiness.ready_to_start === true);",
            'els.loadStatus.textContent = canStartProcessing() ? `${preflightSummaryMessage(state.folderReadiness)} 可以开始处理，原图不会被覆盖。` : blockedStartMessage();',
        ]:
            self.assertIn(required, html)
        for required in [
            '"schema_version": "scan-qc.local-folder-readiness.v1"',
            '"aggregate_only": True',
            '"supported_image_count": 0',
            '"ready_to_start": False',
            '"existing_output_risk": existing_output_risk',
            '"schema_version": "scan-qc.local-existing-output-risk.v1"',
            "未发现已有工作台结果，可以开始",
            "已有本工具结果",
            "本批已有可复用处理结果",
            "只补齐缺失输出",
            "完成交接材料",
        ]:
            self.assertIn(required, server)
        for forbidden in [
            "relative_path",
            "source_sha256",
            "output_sha256",
            "ocr_text",
            "thumbnail",
            "exception",
            "traceback",
            "stack",
        ]:
            self.assertNotIn(forbidden, html[html.find("function preflightSummaryMessage"):html.find("function blockedStartMessage")])

    def test_existing_output_risk_prompt_uses_aggregate_kind_without_private_details(self) -> None:
        html = WORKBENCH_HTML.read_text(encoding="utf-8")
        start = html.find("function existingOutputRiskPrompt(readiness)")
        end = html.find("function canStartProcessing()", start)
        self.assertNotEqual(start, -1)
        self.assertNotEqual(end, -1)
        body = html[start:end]

        for required in [
            'readiness.existing_output_risk',
            'kind === "reusable_current_batch"',
            'kind === "existing_workbench_results" || kind === "completed_handoff"',
            "可继续本批",
            "只补齐缺失输出",
            "建议换空输出文件夹",
            "先交接上一批",
            "已有本工具结果或完成交接材料",
            'els.readinessRiskPrompt.classList.toggle("hidden", !riskPrompt.show);',
            'els.readinessBox.classList.toggle("risk-reusable", riskPrompt.kind === "reusable_current_batch");',
            'els.readinessBox.classList.toggle("risk-blocking", riskPrompt.blocking);',
        ]:
            self.assertIn(required, html)

        for forbidden in [
            "source_path",
            "relative_path",
            "file_name",
            "filename",
            "sha256",
            "hash",
            "ocr_text",
            "thumbnail",
            "evidence",
            "stack",
            "traceback",
            "<img",
        ]:
            self.assertNotIn(forbidden, body.lower())


if __name__ == "__main__":
    unittest.main()
