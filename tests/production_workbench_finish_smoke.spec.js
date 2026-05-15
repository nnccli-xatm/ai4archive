const { test, expect } = require("@playwright/test");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const WORKBENCH_URL_PATH = "/docs/production-workbench-prototype.html";

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

function waitForServer(url) {
  const deadline = Date.now() + 10000;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      fetch(url)
        .then((response) => {
          if (response.ok) resolve();
          else if (Date.now() > deadline) reject(new Error(`server returned ${response.status}`));
          else setTimeout(attempt, 150);
        })
        .catch((error) => {
          if (Date.now() > deadline) reject(error);
          else setTimeout(attempt, 150);
        });
    };
    attempt();
  });
}

async function expectOperatorStatusHidesPaths(page, forbiddenPaths) {
  const operatorStatusText = await page
    .locator("#outputPanel, #loadStatus, #inputStatus, #outputStatus, #readinessBox, #recoveryBox, #stateName, #stateAction, #stateHint, #progressText, #stageTimingText, #currentAdvice, #currentRecommendationReason, #currentDecisionStatus, #decisionGuideList, #decisionSaveGuidance, #previewSourceText, #activePreviewModeText, #previewFrame")
    .allTextContents();
  const combined = operatorStatusText.join("\n");
  for (const value of ["/tmp", "/private", "/Users", ...forbiddenPaths]) {
    expect(combined).not.toContain(value);
  }
}

async function expectProcessingModeRadiosDisabled(page, disabled) {
  const radios = page.locator('input[name="processingMode"]');
  await expect(radios).toHaveCount(3);
  for (let index = 0; index < 3; index += 1) {
    if (disabled) await expect(radios.nth(index)).toBeDisabled();
    else await expect(radios.nth(index)).toBeEnabled();
  }
}

async function expectLaunchSetupControlsDisabled(page) {
  await expect(page.locator("#inputPath")).toBeDisabled();
  await expect(page.locator("#outputPath")).toBeDisabled();
  await expect(page.locator("#inputFolder")).toBeDisabled();
  await expect(page.locator("#outputFolder")).toBeDisabled();
  await expect(page.locator('label[for="inputFolder"]')).toHaveAttribute("aria-disabled", "true");
  await expect(page.locator('label[for="outputFolder"]')).toHaveAttribute("aria-disabled", "true");
  await expectProcessingModeRadiosDisabled(page, true);
  await expect(page.getByRole("button", { name: "保存文件夹" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
}

test.describe("production workbench finish/export browser smoke", () => {
  let server;
  let baseUrl;

  test.beforeAll(async () => {
    const port = await freePort();
    baseUrl = `http://127.0.0.1:${port}`;
    server = childProcess.spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1"], {
      cwd: ROOT,
      stdio: "ignore",
    });
    await waitForServer(`${baseUrl}${WORKBENCH_URL_PATH}`);
  });

  test.afterAll(() => {
    if (server) server.kill();
  });

  test("keeps the startup folder sequence primary and maintenance secondary", async ({ page }) => {
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.getByRole("heading", { name: "批次准备" })).toBeVisible();
    await expect(page.getByLabel("开始处理顺序")).toContainText("填写原图文件夹");
    await expect(page.getByLabel("开始处理顺序")).toContainText("填写输出文件夹");
    await expect(page.getByLabel("开始处理顺序")).toContainText("保存文件夹");
    await expect(page.getByLabel("开始处理顺序")).toContainText("开始处理");
    await expect(page.locator("#inputStatus")).toHaveText("填写本批次扫描原图所在的本机文件夹位置。");
    await expect(page.locator("#outputStatus")).toHaveText("填写处理后图片保存到的本机文件夹位置。");
    await expect(page.getByRole("button", { name: "保存文件夹" })).toBeVisible();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.locator("#loadStatus")).toHaveText("请先填写原图文件夹和输出文件夹，点击“保存文件夹”，确认可以开始后再点击“开始处理”。");
    await page.locator("#pickInputButton").focus();
    await page.keyboard.press("1");
    await expect(page.locator("#loadStatus")).toHaveText("请先填写原图文件夹和输出文件夹，点击“保存文件夹”，确认可以开始后再点击“开始处理”。");
    await expect(page.locator(".mode-selector")).toContainText("标准优化");
    await expect(page.locator(".mode-selector")).toContainText("推荐用于正常批量生产");
    await expect(page.locator(".mode-selector")).toContainText("轻度优化");
    await expect(page.locator(".mode-selector")).toContainText("用于担心过度处理的批次");
    await expect(page.locator(".mode-selector")).toContainText("只质检不修图");
    await expect(page.locator(".mode-selector")).toContainText("只做质量检查，不生成处理后优化图片。");

    await expect(page.locator(".maintenance-loader")).not.toHaveAttribute("open", "");
    await expect(page.getByText("选择维护示例")).toBeHidden();
    await page.getByText("维护入口").click();
    await expect(page.getByText("管理员排查、演练或查看本机状态时使用；这不是正常加工步骤。")).toBeVisible();
    await expect(page.getByText("只用于查看本机已经生成的处理状态，不会开始处理。")).toBeVisible();
  });

  test("shows aggregate Chinese running progress and keeps setup locked until review", async ({ page }) => {
    let payload = {
      schema_version: "scan-qc.local-production-workbench.v1",
      running: true,
      configured: true,
      folders: {
        input: "/tmp/private-running-input",
        derivatives: "/tmp/private-running-output",
        metadata: "/tmp/private-running-output/_production_workbench",
      },
      summary: {
        schema_version: "scan-qc.production-run.v1",
        status: "running",
        operator_summary: {
          message_zh: "本机正在处理图片。",
          total_source_images: 12,
          derivative_images_ready: 5,
          files_needing_attention: 0,
        },
        counts: {
          total_files: 12,
          processed_files: 5,
          failed_files: 0,
        },
      },
      progress: {
        schema_version: "scan-qc.production-run-progress.v1",
        state: "running",
        current_step: "quality_check",
        steps: [{ id: "quality_check", state: "running", completed_items: 5, total_items: 12 }],
        stage_timings: {
          aggregate_only: true,
          stages: [
            { id: "scan", label_zh: "检查扫描图片", elapsed_seconds: 1.2, status: "completed", source_path: "/tmp/private-running-input/page001.tif" },
            { id: "processing", label_zh: "生成处理后图片", elapsed_seconds: 8.5, status: "running", sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" },
            { id: "summarize", label_zh: "整理处理结果", elapsed_seconds: 0, status: "pending", ocr_text: "PRIVATE_OCR_TEXT" },
          ],
        },
      },
    };

    const queue = {
      schema_version: "scan-qc.production-review-queue.v1",
      items: [
        {
          local_id: "PRQ-PROGRESS-1",
          reason_zh: "画面需要确认。",
          focus_hints_zh: ["确认画面是否完整", "判断是否需要重扫"],
          suggested_action: "rescan",
          severity: "P1",
          preview_source: "unavailable",
          preview_sources: { original: false, processed: false },
        },
      ],
    };

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(payload),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#stateAction")).toHaveText("正在处理");
    await expect(page.locator("#progressText")).toHaveText("阶段：正在检查质量；已处理 5 张 / 共 12 张；待处理 7 张；状态：正在处理");
    await expect(page.locator("#stageTimingText")).toHaveText("检查扫描图片 1.2 秒；生成处理后图片 8.5 秒");
    await expect(page.locator("#loadStatus")).toHaveText("批次正在运行，请等待。本机正在处理图片，处理完成或失败前不能更改文件夹和处理方式，也不要反复点击开始处理。");
    await expect(page.locator("#inputStatus")).toHaveText("批次正在运行，完成或失败前不能更改原图文件夹。");
    await expect(page.locator("#outputStatus")).toHaveText("批次正在运行，完成或失败前不能更改输出文件夹。");
    await expectLaunchSetupControlsDisabled(page);
    await expectOperatorStatusHidesPaths(page, [
      "/tmp/private-running-input",
      "/tmp/private-running-output",
      "private-running-input",
      "private-running-output",
      "PRQ-PROGRESS-1",
      "page001.tif",
      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "PRIVATE_OCR_TEXT",
    ]);

    payload = {
      ...payload,
      progress: {
        schema_version: "scan-qc.production-run-progress.v1",
        state: "running",
      },
      summary: {
        schema_version: "scan-qc.production-run.v1",
        status: "running",
        operator_summary: { message_zh: "本机正在处理图片。", files_needing_attention: 0 },
        counts: {},
      },
    };
    await page.evaluate(() => pollServerStatus());
    await expect(page.locator("#progressText")).toHaveText("阶段：正在生成处理后图片；正在统计图片数量；状态：正在处理");
    await expect(page.locator("#stageTimingText")).toHaveClass(/hidden/);
    await expectLaunchSetupControlsDisabled(page);

    payload = {
      schema_version: "scan-qc.local-production-workbench.v1",
      running: false,
      configured: true,
      folders: {
        input: "/tmp/private-running-input",
        derivatives: "/tmp/private-running-output",
        metadata: "/tmp/private-running-output/_production_workbench",
      },
      summary: {
        schema_version: "scan-qc.production-run.v1",
        status: "needs_review",
        operator_summary: {
          message_zh: "有图片需要人工确认。",
          total_source_images: 12,
          derivative_images_ready: 12,
          files_needing_attention: 1,
        },
        counts: {
          total_files: 12,
          processed_files: 12,
          failed_files: 0,
        },
        stage_timings: {
          aggregate_only: true,
          stages: [
            { id: "scan", label_zh: "检查扫描图片", elapsed_seconds: 1.3, status: "completed" },
            { id: "processing", label_zh: "生成处理后图片", elapsed_seconds: 8.8, status: "completed" },
            { id: "summarize", label_zh: "整理处理结果", elapsed_seconds: 0.4, status: "completed" },
          ],
        },
      },
      progress: {
        schema_version: "scan-qc.production-run-progress.v1",
        state: "needs_review",
      },
      queue,
    };
    await page.evaluate(() => pollServerStatus());
    await expect(page.locator("#stateAction")).toHaveText("有图片需要人工确认");
    await expect(page.locator("#progressText")).toHaveText("阶段：等待人工确认；已处理 12 张 / 共 12 张；状态：需要人工确认");
    await expect(page.locator("#stageTimingText")).toHaveText("检查扫描图片 1.3 秒；生成处理后图片 8.8 秒；整理处理结果 0.4 秒");
    await expect(page.locator("#remainingWorkText")).toHaveText("还需确认 1 张");
    await expect(page.getByRole("button", { name: "确认通过" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeEnabled();
    await expectLaunchSetupControlsDisabled(page);
  });

  test("finishes a synthetic review queue without console errors or warnings", async ({ page }) => {
    const consoleProblems = [];
    let resetRequested = false;
    let openOutputRequested = false;
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));

    const summary = {
      schema_version: "scan-qc.production-run.v1",
      status: "needs_review",
      operator_summary: {
        message: "Synthetic review queue ready.",
        message_zh: "有图片需要人工确认。",
        total_source_images: 3,
        openable_source_images: 3,
        derivative_images_ready: 3,
        files_needing_attention: 3,
      },
      counts: {
        total_files: 3,
        processed_files: 3,
        resumed_files: 0,
        failed_files: 0,
        retry_list_files: 0,
      },
    };
    const queue = {
      schema_version: "scan-qc.production-review-queue.v1",
      items: [
        {
          local_id: "PRQ000001",
          reason_zh: "画面需要确认。",
          focus_hints_zh: ["看图片能否正常打开", "重点判断是否需要重扫"],
          suggested_action: "rescan",
          severity: "P1",
          preview_source: "comparison",
          preview_sources: { original: true, processed: true },
        },
        {
          local_id: "PRQ000002",
          reason_zh: "页面顺序需要确认。",
          focus_hints_zh: ["看页面是否倾斜", "确认是否需要重新处理"],
          suggested_action: "reprocess",
          severity: "P2",
          preview_source: "original_fallback",
          preview_sources: { original: true, processed: false },
        },
        {
          local_id: "PRQ000003",
          reason_zh: "质量结果需要确认。",
          focus_hints_zh: ["对比原图和处理后图片", "重点判断是否保留原貌"],
          suggested_action: "keep_original_trace",
          severity: "P0",
          preview_source: "processed",
          preview_sources: { original: false, processed: true },
        },
      ],
    };

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: "/tmp/synthetic-input",
            derivatives: "/tmp/synthetic-output",
            metadata: "/tmp/synthetic-output/_production_workbench",
          },
          summary,
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "needs_review" },
          queue,
          draft_decisions: null,
        }),
      });
    });
    await page.route("**/api/save-draft-decisions", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          saved: true,
          message_zh: "已自动保存",
          decision_summary: { completion_status: "incomplete" },
        }),
      });
    });
    await page.route("**/api/preview/**", async (route) => {
      await route.fulfill({
        contentType: "image/png",
        body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
          "base64",
        ),
      });
    });
    await page.route("**/api/finish-decisions", async (route) => {
      const request = route.request();
      const payload = JSON.parse(request.postData() || "{}");
      expect(payload.decisions).toHaveLength(3);
      expect(payload.aggregate_counts.review_completion.complete).toBe(true);
      expect(payload.operator_name).toBe("复核员甲");
      expect(payload.operator_decisions).toHaveLength(3);
      expect(payload.operator_decisions[0]).toMatchObject({
        local_id: "PRQ000001",
        decision: "pass",
        note_zh: "本张可以通过。",
      });
      expect(payload.operator_decisions[2]).toMatchObject({
        local_id: "PRQ000003",
        decision: "keep_original_trace",
        note_zh: "保留原貌即可。",
      });
      expect(payload.operator_decisions.every((item) => typeof item.decided_at === "string" && item.decided_at.length > 0)).toBe(true);
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          finished: true,
          message_zh: "本批已完成：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
          completion_panel: {
            title_zh: "本批已完成",
            message_zh: "处理后图片已准备好。请检查输出文件夹后再交接。",
            completion_status_zh: "本批已完成",
            processing_mode: {
              id: "standard",
              label_zh: "标准优化",
              purpose_zh: "推荐用于正常批量生产，兼顾批量图片质量和处理效率。",
              output_zh: "会生成处理后优化图片，原图不覆盖。",
            },
            manual_work_zh: "没有待人工处理图片",
            admin_handoff_zh: "不需要",
            total_review_items: 3,
            reviewed_items: 3,
            pending_items: 0,
            processed_output_images: 3,
            needs_rescan_images: 0,
            needs_reprocess_images: 0,
            local_reuse_summary: {
              schema_version: "scan-qc.local-processing-reuse-summary.v1",
              aggregate_only: true,
              reused_files: 2,
              reprocessed_files: 1,
              failed_files: 0,
              remaining_files: 0,
            },
            open_output_folder_available: true,
            checklist_zh: ["打开输出文件夹，检查 3 张处理后图片的数量和画面状态", "需要重扫 0 张，需要重新处理 0 张", "复核结果和交接说明已保存到本机状态文件夹", "准备下一批会清空当前复核队列，请重新选择新一批文件夹"],
            next_steps_zh: [
              "打开输出文件夹，检查 3 张处理后图片的数量和画面状态。",
              "需要重扫 0 张；需要重新处理 0 张。",
              "本机状态文件夹已保存复核结果和交接说明，正常界面不显示具体路径或文件名。",
              "需要继续加工时，点击准备下一批；当前复核队列会清空。为新批次必须重新选择扫描原图文件夹，不要混用批次；输出文件夹可沿用上次保存的位置。",
              "如果仍有异常或不能交接，请交管理员处理。",
            ],
          },
          decision_summary: { completion_status: "complete" },
        }),
      });
    });
    await page.route("**/api/open-output-folder", async (route) => {
      openOutputRequested = true;
      expect(route.request().postDataJSON()).toEqual({});
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          opened: true,
          message_zh: "已打开输出文件夹。请检查处理后图片数量和画面状态。",
        }),
      });
    });
    await page.route("**/api/reset-batch", async (route) => {
      resetRequested = true;
      expect(route.request().postDataJSON()).toEqual({});
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: false,
          folders: { input: null, derivatives: null, metadata: null },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "not_configured",
            ready_to_start: false,
            supported_image_count: 0,
            input_empty: true,
            output_writable: false,
            title_zh: "文件夹还没有保存",
            message_zh: "请先保存扫描原图文件夹和处理后输出文件夹。",
            next_steps_zh: ["填写两个文件夹位置。", "保存文件夹后查看准备情况。"],
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.getByRole("heading", { name: "当前图片" })).toBeVisible();
    await expect(page.getByText("已加载复核队列 3 张，还需确认 3 张。")).toBeVisible();
    await expect(page.locator("#remainingWorkText")).toHaveText("还需确认 3 张");
    await expect(page.locator("#reviewMovementText")).toHaveText("正在看第 1 张；确认后会自动跳到下一张待确认图片。");
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 1 张 / 共 3 张；还需确认 3 张。");
    await expect(page.locator("#currentFocusHints")).toHaveText("看图片能否正常打开；重点判断是否需要重扫");
    await expect(page.locator("#currentRecommendation")).toHaveText("建议：退回重扫");
    await expect(page.locator("#currentRecommendationReason")).toHaveText("建议退回重扫，是因为这类问题通常需要扫描工位重新获取图片，不能只靠重新处理解决。");
    await expect(page.locator("#currentDecisionStatus")).toHaveText("当前决定：未决定");
    await expect(page.getByRole("button", { name: "退回重扫" })).toHaveClass(/recommended-choice/);
    await expect(page.locator('[data-guide-decision="rescan"]')).toHaveClass(/recommended-guide/);
    await expect(page.locator("#decisionGuideList")).toContainText("确认通过：图片可以继续使用；点击后会保存决定并看下一张。");
    await expect(page.locator("#decisionGuideList")).toContainText("退回重扫：原图不清楚、缺页、歪斜严重或打不开；点击后交回扫描工位补扫。");
    await expect(page.locator("#decisionGuideList")).toContainText("重新处理图片：原图可用，但处理后图片需要重新生成；点击后保留原图并安排重新处理。");
    await expect(page.locator("#decisionGuideList")).toContainText("确认保留原貌：痕迹、颜色、折痕或旧化是档案本来样子；点击后不让自动优化覆盖原貌。");
    await expect(page.locator("#decisionSaveGuidance")).toHaveText("选择任一决定后，会保存当前图片的决定和备注，并自动显示下一张待确认图片；全部确认后可以完成并导出结果。");
    await expect(page.getByRole("button", { name: "确认通过" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "重新处理图片" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "确认保留原貌" })).toBeEnabled();
    await expect(page.locator("#previewSourceText")).toHaveText("图片查看：正在查看处理后图片。");
    await expect(page.locator(".comparison-title")).toHaveCount(0);
    await expect(page.locator("#activePreviewModeText")).toHaveText("当前查看：处理后图片。可切换原图、处理后图片或对比查看。");
    await expect(page.locator('#comparisonControls button[data-comparison-mode="processed"]')).toHaveClass(/active/);
    await expect(page.locator('#comparisonControls button[data-comparison-mode="processed"]')).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByText("正在查看处理后图片，可切换原图或对比查看。")).toBeVisible();
    await expect(page.getByRole("button", { name: "查看处理后图片" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "查看原图" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "对比查看" })).toBeEnabled();
    await expect(page.locator("#comparisonControls button").nth(0)).toHaveText("查看原图");
    await expect(page.locator("#comparisonControls button").nth(1)).toHaveText("查看处理后图片");
    await expect(page.locator("#comparisonControls button").nth(2)).toHaveText("对比查看");
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");
    await expectOperatorStatusHidesPaths(page, [
      "/tmp/synthetic-input",
      "/tmp/synthetic-output",
      "synthetic-input",
      "synthetic-output",
      "PRIVATE",
      "PUERSAI",
      "sha256",
      "OCR",
      "PRQ000001",
      ".jpg",
      ".png",
      ".tif",
    ]);

    await page.keyboard.press("2");
    await expect(page.locator("#remainingWorkText")).toHaveText("还需确认 3 张");
    await expect(page.locator("#currentDecisionStatus")).toHaveText("当前决定：未决定");
    await page.locator("#operatorName").focus();
    await page.keyboard.press("2");
    await expect(page.locator("#operatorName")).toHaveValue("2");
    await expect(page.locator("#remainingWorkText")).toHaveText("还需确认 3 张");
    await expect(page.locator("#currentDecisionStatus")).toHaveText("当前决定：未决定");
    await page.locator("#operatorName").fill("复核员甲");

    await page.getByRole("button", { name: "放大" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");
    await page.locator("#decisionNote").fill("切换查看方式时保留备注。");
    await page.getByRole("button", { name: "查看原图" }).click();
    await expect(page.locator("#previewSourceText")).toHaveText("图片查看：正在查看原图。");
    await expect(page.locator("#activePreviewModeText")).toHaveText("当前查看：原图。可切换原图、处理后图片或对比查看。");
    await expect(page.locator('#comparisonControls button[data-comparison-mode="original"]')).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".comparison-title")).toHaveCount(0);
    await expect(page.getByText("正在查看原图，可切换处理后图片或对比查看。")).toBeVisible();
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");
    await expect(page.locator("#operatorName")).toHaveValue("复核员甲");
    await expect(page.locator("#decisionNote")).toHaveValue("切换查看方式时保留备注。");
    await page.getByRole("button", { name: "查看处理后图片" }).click();
    await expect(page.locator("#previewSourceText")).toHaveText("图片查看：正在查看处理后图片。");
    await expect(page.locator("#activePreviewModeText")).toHaveText("当前查看：处理后图片。可切换原图、处理后图片或对比查看。");
    await expect(page.getByText("正在查看处理后图片，可切换原图或对比查看。")).toBeVisible();
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");
    await page.getByRole("button", { name: "对比查看" }).click();
    await expect(page.locator("#previewSourceText")).toHaveText("图片查看：正在对比原图和处理后图片。");
    await expect(page.locator("#activePreviewModeText")).toHaveText("当前查看：对比查看。可切换原图、处理后图片或对比查看。");
    await expect(page.locator('#comparisonControls button[data-comparison-mode="side_by_side"]')).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".comparison-title", { hasText: "原图" })).toBeVisible();
    await expect(page.locator(".comparison-title", { hasText: "处理后图片" })).toBeVisible();
    await expect(page.getByText("正在对比查看。看完后在右侧选择处理决定。")).toBeVisible();
    await expect(page.locator("#decisionNote")).toHaveValue("切换查看方式时保留备注。");
    await page.getByRole("button", { name: "缩小" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：100%");
    await page.getByRole("button", { name: "还原" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：100%");
    await page.getByRole("button", { name: "适合窗口" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");

    await page.locator("#decisionNote").fill("边缘不清楚，需要补扫。");
    await expect(page.getByText("选择一个处理决定后，会记录当前图片，并自动显示下一张待确认图片。")).toBeVisible();
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeEnabled();
    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await expect(page.locator("#finishConfirmPanel")).toBeHidden();
    await expect(page.locator("#loadStatus")).toHaveText("还有 3 张图片没有决定，暂不能完成。请先逐张选择确认通过、退回重扫、重新处理图片或确认保留原貌。");
    await page.locator("#previewFrame").focus();
    await page.keyboard.press("2");
    await expect(page.getByText("已记录：退回重扫。已自动显示下一张待确认图片。已确认 1 张，还需确认 2 张。")).toBeVisible();
    await expect(page.locator("#remainingWorkText")).toHaveText("还需确认 2 张");
    await expect(page.locator("#reviewMovementText")).toHaveText("正在看第 2 张；确认后会自动跳到下一张待确认图片。");
    await expect(page.locator("#currentRecommendation")).toHaveText("建议：重新处理图片");
    await expect(page.locator("#currentRecommendationReason")).toHaveText("建议重新处理图片，是因为原图仍可使用，通常重新生成处理后图片即可继续。");
    await expect(page.getByRole("button", { name: "重新处理图片" })).toHaveClass(/recommended-choice/);
    await expect(page.locator('[data-guide-decision="reprocess"]')).toHaveClass(/recommended-guide/);
    await expect(page.locator("#previewSourceText")).toHaveText("图片查看：处理后图片不可用，正在显示原图。");
    await expect(page.locator("#activePreviewModeText")).toHaveText("当前查看：原图。本张没有处理后图片，可查看原图后继续判断。");
    await expect(page.getByText("本张没有处理后图片，可查看原图。看完后在右侧选择处理决定。")).toBeVisible();
    await expect(page.locator('#comparisonControls button[data-comparison-mode="original"]')).toBeEnabled();
    await expect(page.locator('#comparisonControls button[data-comparison-mode="processed"]')).toBeDisabled();
    await expect(page.locator('#comparisonControls button[data-comparison-mode="processed"]')).toHaveAttribute("title", "本张没有处理后图片，可查看原图");
    await expect(page.locator('#comparisonControls button[data-comparison-mode="side_by_side"]')).toBeDisabled();
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");
    await page.getByRole("button", { name: "上一张已确认图片" }).click();
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 1 张 / 共 3 张；还需确认 2 张。");
    await expect(page.locator("#currentDecisionStatus")).toHaveText("当前决定：退回重扫");
    await expect(page.locator("#currentAdvice")).toHaveText("如果刚才点错了，可以撤销当前决定后重新选择。");
    await page.getByRole("button", { name: "撤销当前决定" }).click();
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 0 项，待决定 3 项。");
    await expect(page.locator("#currentDecisionStatus")).toHaveText("当前决定：未决定");
    await expect(page.locator("#loadStatus")).toHaveText("已自动保存");
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeEnabled();
    await page.locator("#decisionNote").fill("本张可以通过。");
    await page.locator("#previewFrame").focus();
    await page.keyboard.press("1");
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 2 张 / 共 3 张；还需确认 2 张。");
    await expect(page.locator("#operatorName")).toHaveValue("复核员甲");
    await expect(page.locator("#currentRecommendation")).toHaveText("建议：重新处理图片");
    await page.getByRole("button", { name: "重新处理图片" }).click();
    await expect(page.getByText("已记录：重新处理图片。已自动显示下一张待确认图片。已确认 2 张，还需确认 1 张。")).toBeVisible();
    await expect(page.locator("#currentRecommendation")).toHaveText("建议：确认保留原貌");
    await expect(page.locator("#currentRecommendationReason")).toHaveText("建议保留原貌，是因为当前痕迹更像档案本身状态，自动优化不应覆盖这种原貌。");
    await expect(page.locator('[data-guide-decision="keep_original_trace"]')).toHaveClass(/recommended-guide/);
    await expect(page.locator("#activePreviewModeText")).toHaveText("当前查看：处理后图片。本张没有原图，可继续判断或交管理员处理。");
    await expect(page.getByText("本张没有原图，可查看处理后图片。看完后在右侧选择处理决定。")).toBeVisible();
    await expect(page.locator('#comparisonControls button[data-comparison-mode="original"]')).toBeDisabled();
    await expect(page.locator('#comparisonControls button[data-comparison-mode="original"]')).toHaveAttribute("title", "本张没有原图，可查看处理后图片");
    await page.locator("#decisionNote").fill("保留原貌即可。");
    await page.locator("#previewFrame").focus();
    await page.keyboard.press("4");
    await expect(page.getByText("已记录：确认保留原貌。所有待确认图片都已确认，可以点击完成并导出结果。")).toBeVisible();
    await expect(page.locator("#remainingWorkText")).toHaveText("还需确认 0 张");
    await expect(page.locator("#reviewMovementText")).toHaveText("所有待确认图片都已确认，可以点击完成并导出结果。");
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 3 项，待决定 0 项。");
    await expect(page.locator("#currentIssue")).toHaveText("已经没有待确认图片。");
    await expect(page.locator("#currentFocusHints")).toHaveText("已经没有待确认图片。");
    await expect(page.locator("#currentAdvice")).toHaveText("可以完成并导出结果。");
    await expect(page.locator("#previewSourceText")).toHaveText("图片查看：等待本机处理结果。");
    await expect(page.locator("#activePreviewModeText")).toHaveText("当前查看：等待本机处理结果");
    await expect(page.locator("#zoomState")).toHaveText("查看：暂无图片");
    await expect(page.locator("#decisionNote")).toBeDisabled();
    await expect(page.locator("#decisionNote")).toHaveValue("");
    await expect(page.getByRole("button", { name: "确认通过" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "退回重扫" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "重新处理图片" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "确认保留原貌" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "放大" })).toBeDisabled();
    await page.locator("#previewFrame").focus();
    await page.keyboard.press("4");
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 3 项，待决定 0 项。");
    await expect(page.locator("#currentIssue")).toHaveText("已经没有待确认图片。");

    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await expect(page.locator("#finishConfirmPanel")).toBeVisible();
    await expect(page.getByRole("heading", { name: "确认完成本批" })).toBeVisible();
    await expect(page.locator("#finishConfirmCounts")).toHaveText("共 3 项，已确认 3 项，待决定 0 项。");
    await expect(page.locator("#finishConfirmOutput")).toHaveText("处理后输出文件夹，已准备 3 张处理后图片");
    await expect(page.getByText("复核结果和交接说明将保存到本机状态文件夹。")).toBeVisible();
    await page.getByRole("button", { name: "返回继续检查" }).click();
    await expect(page.locator("#finishConfirmPanel")).toBeHidden();
    await expect(page.locator("#operatorName")).toHaveValue("复核员甲");
    await expect(page.locator("#decisionNote")).toHaveValue("");
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 3 项，待决定 0 项。");
    await expect(page.locator("#zoomState")).toHaveText("查看：暂无图片");

    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await page.getByRole("button", { name: "确认完成本批" }).click();
    await expect(page.locator("#completionTitle")).toHaveText("本批已完成");
    await expect(page.locator("#completionMessage")).toHaveText("处理后图片已准备好。请检查输出文件夹后再交接。");
    await expect(page.locator("#completionReuseMessage")).toHaveText("本批复用了 2 张，重新处理 1 张，失败 0 张，剩余 0 张。");
    await expect(page.locator("#completionCounts")).toHaveText("共 3 项，已确认 3 项，待决定 0 项。");
    await expect(page.locator("#completionStatusFact")).toHaveText("本批已完成");
    await expect(page.locator("#outputPlace")).toHaveText("已准备 3 张处理后图片");
    await expect(page.locator("#rescanFact")).toHaveText("0 张");
    await expect(page.locator("#reprocessFact")).toHaveText("0 张");
    await expect(page.locator("#manualWorkFact")).toHaveText("没有待人工处理图片");
    await expect(page.locator("#adminHandoffFact")).toHaveText("不需要");
    await expectOperatorStatusHidesPaths(page, [
      "/tmp/synthetic-input",
      "/tmp/synthetic-output",
      "/tmp/synthetic-output/_production_workbench",
    ]);
    await expect(page.getByText("打开输出文件夹，检查 3 张处理后图片的数量和画面状态。")).toBeVisible();
    await expect(page.getByText("需要重扫 0 张；需要重新处理 0 张。")).toBeVisible();
    await expect(page.getByText("本机状态文件夹已保存复核结果和交接说明，正常界面不显示具体路径或文件名。")).toBeVisible();
    await expect(page.getByText("需要继续加工时，点击准备下一批；当前复核队列会清空。为新批次必须重新选择扫描原图文件夹，不要混用批次；输出文件夹可沿用上次保存的位置。")).toBeVisible();
    await expect(page.getByText("如果仍有异常或不能交接，请交管理员处理。")).toBeVisible();
    await expect(page.getByRole("button", { name: "打开输出文件夹" })).toBeEnabled();
    await page.getByRole("button", { name: "打开输出文件夹" }).click();
    await expect(page.locator("#openOutputFolderStatus")).toHaveText("已打开输出文件夹。请检查处理后图片数量和画面状态。");
    await expect.poll(() => openOutputRequested).toBe(true);
    await expectOperatorStatusHidesPaths(page, [
      "/tmp/synthetic-input",
      "/tmp/synthetic-output",
      "/tmp/synthetic-output/_production_workbench",
    ]);
    await page.getByRole("button", { name: "准备下一批" }).click();
    await expect(page.locator("#completionTitle")).toBeHidden();
    await expect(page.locator("#stateName")).toHaveText("新批次起点");
    await expect(page.locator("#stateAction")).toHaveText("请重新选择扫描原图文件夹");
    await expect(page.locator("#inputPath")).toHaveValue("");
    await expect(page.locator("#outputPath")).toHaveValue("/tmp/synthetic-output");
    await expect(page.locator("#inputStatus")).toHaveText("必须重新选择新一批扫描原图文件夹，不要混用批次。");
    await expect(page.locator("#outputStatus")).toHaveText("已沿用上次保存的输出文件夹提示；如本批要换位置，请重新选择输出文件夹。");
    await expect(page.locator("#readinessBox")).toBeHidden();
    await expect(page.locator("#queueText")).toHaveText("等待处理开始。");
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 0 项，待决定 0 项。");
    await expect(page.locator("#completionTitle")).toHaveText("等待完成本批");
    await expect(page.locator("#completionReuseMessage")).toBeHidden();
    await expect(page.locator("#completionStatusFact")).toHaveText("未完成");
    await expect(page.getByRole("button", { name: "打开输出文件夹" })).toBeDisabled();
    await expect(page.locator("#openOutputFolderStatus")).toHaveText("完成本批并保存输出文件夹后可以打开检查。");
    await expect(page.locator("#outputPlace")).toHaveText("已选择的处理后输出文件夹");
    await expect(page.locator("#pendingText")).toHaveText("0 个");
    await expect(page.locator("#completionCounts")).toHaveText("共 0 项，已确认 0 项，待决定 0 项。");
    await expect(page.locator("#previewSourceText")).toHaveText("图片查看：等待本机处理结果。");
    await expect(page.locator("#finishConfirmPanel")).toBeHidden();
    await expect(page.locator("#loadStatus")).toHaveText("已准备下一批：当前复核队列已清空。请重新选择新一批扫描原图文件夹；输出文件夹已保留上次保存的位置提示。");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.locator("#inputPath")).toBeEnabled();
    await expect(page.locator("#outputPath")).toBeEnabled();
    await expect(page.locator("#inputFolder")).toBeEnabled();
    await expect(page.locator("#outputFolder")).toBeEnabled();
    await expectProcessingModeRadiosDisabled(page, false);
    await expect.poll(() => resetRequested).toBe(true);

    expect(consoleProblems).toEqual([]);
    expect(fs.existsSync(path.join(ROOT, "docs", "production-workbench-prototype.html"))).toBe(true);
  });

  test("shows empty-folder next steps without console errors or warnings", async ({ page }) => {
    const consoleProblems = [];
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: "/tmp/empty-input",
            derivatives: "/tmp/empty-output",
            metadata: "/tmp/empty-output/_production_workbench",
          },
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "finished",
            ready_for_operator_handoff: false,
            local_batch_state: "empty_input_folder",
            operator_summary: {
              message_zh: "扫描原图文件夹里没有可处理文件，请确认是否选错文件夹。",
              total_source_images: 0,
              openable_source_images: 0,
              derivative_images_ready: 0,
              files_needing_attention: 0,
            },
            counts: {
              total_files: 0,
              openable_files: 0,
              processed_files: 0,
              resumed_files: 0,
              failed_files: 0,
              retry_list_files: 0,
            },
            recovery_guidance: {
              schema_version: "scan-qc.local-recovery-guidance.v1",
              aggregate_only: true,
              kind: "empty_input_folder",
              title_zh: "原图文件夹是空的",
              message_zh: "这个扫描原图文件夹里没有发现可处理文件。",
              next_steps_zh: ["确认是否选到了本批次真正的扫描原图文件夹。", "放好图片后，重新保存文件夹并开始处理。"],
              failed_files: 0,
              retryable_files: 0,
              derivative_images_ready: 0,
              total_files: 0,
            },
          },
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "finished" },
          queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
          draft_decisions: null,
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#recoveryTitle")).toHaveText("原图文件夹是空的");
    await expect(page.getByText("确认是否选到了本批次真正的扫描原图文件夹。")).toBeVisible();
    await expect(page.locator("#queueText")).toHaveText("没有待人工确认图片。");
    await expect(page.locator("#sourceText")).toHaveText("0 张");
    await expect(page.locator("#readyText")).toHaveText("0 张");

    expect(consoleProblems).toEqual([]);
  });

  test("confirms no-review batches before finishing", async ({ page }) => {
    const consoleProblems = [];
    let finishRequested = false;
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: "/tmp/no-review-input",
            derivatives: "/tmp/no-review-output",
            metadata: "/tmp/no-review-output/_production_workbench",
          },
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "finished",
            operator_summary: {
              message_zh: "处理后图片已生成，可以完成并导出结果。",
              total_source_images: 2,
              openable_source_images: 2,
              derivative_images_ready: 2,
              files_needing_attention: 0,
            },
            counts: {
              total_files: 2,
              openable_files: 2,
              processed_files: 2,
              resumed_files: 0,
              failed_files: 0,
              retry_list_files: 0,
            },
            stage_timings: {
              aggregate_only: true,
              stages: [
                { id: "scan", label_zh: "检查扫描图片", elapsed_seconds: 0.9, status: "completed" },
                { id: "processing", label_zh: "生成处理后图片", elapsed_seconds: 3.4, status: "completed" },
                { id: "summarize", label_zh: "整理处理结果", elapsed_seconds: 0.6, status: "completed" },
              ],
            },
          },
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "finished" },
          queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
          draft_decisions: null,
        }),
      });
    });
    await page.route("**/api/finish-decisions", async (route) => {
      finishRequested = true;
      const payload = JSON.parse(route.request().postData() || "{}");
      expect(payload.decisions).toHaveLength(0);
      expect(payload.aggregate_counts.review_completion.complete).toBe(true);
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          finished: true,
          message_zh: "本批已完成：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
          completion_panel: {
            title_zh: "本批已完成",
            message_zh: "处理后图片已准备好。请检查输出文件夹后再交接。",
            completion_status_zh: "本批已完成",
            processing_mode: {
              id: "standard",
              label_zh: "标准优化",
              purpose_zh: "推荐用于正常批量生产，兼顾批量图片质量和处理效率。",
              output_zh: "会生成处理后优化图片，原图不覆盖。",
            },
            manual_work_zh: "没有待人工处理图片",
            admin_handoff_zh: "不需要",
            total_review_items: 0,
            reviewed_items: 0,
            pending_items: 0,
            checklist_zh: ["打开输出文件夹，检查处理后图片数量和画面状态", "复核结果和交接说明已保存到本机状态文件夹", "准备下一批会清空当前复核队列，请重新选择新一批文件夹"],
            next_steps_zh: ["打开输出文件夹，检查处理后图片数量和画面状态。"],
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#stateName")).toHaveText("待完成");
    await expect(page.locator("#stageTimingText")).toHaveText("检查扫描图片 0.9 秒；生成处理后图片 3.4 秒；整理处理结果 0.6 秒");
    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await expect(page.locator("#finishConfirmPanel")).toBeVisible();
    await expect(page.locator("#finishConfirmMessage")).toHaveText("本批没有需要人工确认的图片。请确认处理后图片已准备好，再完成本批。");
    await expect(page.locator("#finishConfirmMode")).toContainText("标准优化");
    await expect(page.locator("#finishConfirmMode")).toContainText("推荐用于正常批量生产");
    await page.getByRole("button", { name: "返回继续检查" }).click();
    await expect(page.locator("#stateName")).toHaveText("待完成");
    await expect(finishRequested).toBe(false);
    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await page.getByRole("button", { name: "确认完成本批" }).click();
    await expect(page.locator("#completionTitle")).toHaveText("本批已完成");
    await expect(page.locator("#stageTimingText")).toHaveText("检查扫描图片 0.9 秒；生成处理后图片 3.4 秒；整理处理结果 0.6 秒");
    expect(finishRequested).toBe(true);
    expect(consoleProblems).toEqual([]);
  });

  test("shows retry action for retryable failures and posts retry request", async ({ page }) => {
    const consoleProblems = [];
    let retryRequested = false;
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: "/tmp/retry-input",
            derivatives: "/tmp/retry-output",
            metadata: "/tmp/retry-output/_production_workbench",
          },
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "blocked",
            operator_summary: {
              message_zh: "有文件处理失败。",
              total_source_images: 4,
              openable_source_images: 4,
              derivative_images_ready: 2,
              files_needing_attention: 2,
            },
            counts: {
              total_files: 4,
              openable_files: 4,
              processed_files: 2,
              resumed_files: 0,
              failed_files: 2,
              retry_list_files: 2,
            },
          },
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "blocked" },
          queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
          draft_decisions: null,
        }),
      });
    });
    await page.route("**/api/retry", async (route) => {
      retryRequested = true;
      expect(route.request().postDataJSON()).toEqual({});
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: true,
          configured: true,
          folders: {
            input: "/tmp/retry-input",
            derivatives: "/tmp/retry-output",
            metadata: "/tmp/retry-output/_production_workbench",
          },
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "running" },
          recovery_guidance: {
            schema_version: "scan-qc.local-recovery-guidance.v1",
            aggregate_only: true,
            kind: "processing_running",
            title_zh: "正在处理",
            message_zh: "本机正在生成处理后图片，请稍候。",
            next_steps_zh: ["等待处理完成后查看结果。"],
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#progressText")).toHaveText("阶段：需要管理员处理；已处理 2 张 / 共 4 张；有 2 张需要管理员处理；状态：需要处理");
    await expect(page.locator("#recoveryTitle")).toHaveText("处理没有全部完成");
    await expect(page.locator("#recoveryMessage")).toHaveText("本批次有图片没有处理完，可以先检查文件夹后重试本批次。");
    await expect(page.getByText("检查扫描原图文件夹和输出文件夹是否选对。")).toBeVisible();
    await expect(page.getByText("点击重试本批次，系统会继续使用当前文件夹。")).toBeVisible();
    await expect(page.getByText("如果文件夹选错了，请返回重新选择文件夹。")).toBeVisible();
    await expect(page.getByRole("button", { name: "重试本批次" })).toBeVisible();
    await page.getByRole("button", { name: "重试本批次" }).click();
    await expect(page.locator("#loadStatus")).toHaveText("正在重试本批次，请等待；系统会继续使用当前文件夹。");
    expect(retryRequested).toBe(true);
    expect(consoleProblems).toEqual([]);
  });

  test("does not offer retry for administrator failures", async ({ page }) => {
    let resetRequested = false;
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: "/tmp/admin-input",
            derivatives: "/tmp/admin-output",
            metadata: "/tmp/admin-output/_production_workbench",
          },
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "blocked",
            operator_summary: {
              message_zh: "处理没有正常完成。",
              total_source_images: 2,
              openable_source_images: 2,
              derivative_images_ready: 0,
              files_needing_attention: 2,
            },
            counts: {
              total_files: 2,
              openable_files: 2,
              processed_files: 0,
              failed_files: 2,
              retry_list_files: 0,
            },
          },
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "blocked" },
          queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
          draft_decisions: null,
        }),
      });
    });
    await page.route("**/api/reset-batch", async (route) => {
      resetRequested = true;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false, configured: false }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#progressText")).toHaveText("阶段：需要管理员处理；已处理 0 张 / 共 2 张；有 2 张需要管理员处理；状态：需要处理");
    await expect(page.locator("#recoveryTitle")).toHaveText("处理没有全部完成");
    await expect(page.locator("#recoveryMessage")).toHaveText("本批次没有处理完，当前不能直接重试。");
    await expect(page.getByRole("button", { name: "重试本批次" })).toBeHidden();
    await expect(page.getByRole("button", { name: "开始新批次" })).toBeVisible();
    await expect(page.getByText("请交管理员处理，不要反复点击开始处理。")).toBeVisible();
    await expect(page.getByText("如果文件夹选错了，请返回重新选择文件夹。")).toBeVisible();
    await page.getByRole("button", { name: "开始新批次" }).click();
    await expect(page.locator("#stateName")).toHaveText("新批次起点");
    await expect(page.locator("#stateAction")).toHaveText("请重新选择扫描原图文件夹");
    await expect(page.locator("#inputPath")).toHaveValue("");
    await expect(page.locator("#outputPath")).toHaveValue("/tmp/admin-output");
    await expect(page.locator("#failurePanel")).toBeHidden();
    await expect(page.locator("#recoveryTitle")).toHaveText("文件夹还没有准备好");
    await expect(page.locator("#queueText")).toHaveText("等待处理开始。");
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 0 项，待决定 0 项。");
    await expect(page.locator("#completionTitle")).toHaveText("等待完成本批");
    await expect(page.locator("#completionStatusFact")).toHaveText("未完成");
    await expect(page.locator("#outputPlace")).toHaveText("已选择的处理后输出文件夹");
    await expect(page.locator("#pendingText")).toHaveText("0 个");
    await expect(page.locator("#completionCounts")).toHaveText("共 0 项，已确认 0 项，待决定 0 项。");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.locator("#inputPath")).toBeEnabled();
    await expect(page.locator("#outputPath")).toBeEnabled();
    await expectProcessingModeRadiosDisabled(page, false);
    await expect.poll(() => resetRequested).toBe(true);
  });

  test("shows aggregate in-progress counts and current stage without private paths", async ({ page }) => {
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: true,
          configured: true,
          folders: {
            input: "/tmp/running-input",
            derivatives: "/tmp/running-output",
            metadata: "/tmp/running-output/_production_workbench",
          },
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "running",
            operator_summary: {
              message_zh: "正在生成处理后图片，请保持本机和磁盘可用。",
              total_source_images: 120,
              openable_source_images: 120,
              derivative_images_ready: 48,
              files_needing_attention: 0,
            },
            counts: {
              total_files: 120,
              processed_files: 48,
              failed_files: 0,
            },
          },
          progress: {
            schema_version: "scan-qc.production-run-progress.v1",
            state: "running",
            current_step: "process",
            completed_steps: 1,
            total_steps: 3,
            steps: [
              { id: "scan", label: "检查扫描图片", state: "completed" },
              { id: "process", label: "生成处理后图片", state: "running", total_items: 120, completed_items: 48 },
              { id: "summarize", label: "整理处理结果", state: "pending" },
            ],
          },
          queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
          draft_decisions: null,
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#stateName")).toHaveText("正在处理");
    await expect(page.locator("#loadStatus")).toHaveText("批次正在运行，请等待。本机正在处理图片，处理完成或失败前不能更改文件夹和处理方式，也不要反复点击开始处理。");
    await expect(page.locator("#progressText")).toHaveText("阶段：正在生成处理后图片；已处理 48 张 / 共 120 张；状态：正在处理");
    await expect(page.locator("#sourceText")).toHaveText("120 张");
    await expect(page.locator("#readyText")).toHaveText("48 张");
    await expect(page.locator("#inputPath")).toBeDisabled();
    await expect(page.locator("#outputPath")).toBeDisabled();
    await expect(page.locator("#inputFolder")).toBeDisabled();
    await expect(page.locator("#outputFolder")).toBeDisabled();
    await expectProcessingModeRadiosDisabled(page, true);
    await expect(page.getByRole("button", { name: "保存文件夹" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeDisabled();
    await expectOperatorStatusHidesPaths(page, ["/tmp/running-input", "/tmp/running-output"]);
  });

  test("locks setup controls during processing and unlocks the right controls after status transitions", async ({ page }) => {
    let statusState = "running";
    await page.route("**/api/status", async (route) => {
      const basePayload = {
        schema_version: "scan-qc.local-production-workbench.v1",
        configured: true,
        folders: {
          input: "/tmp/transition-input",
          derivatives: "/tmp/transition-output",
          metadata: "/tmp/transition-output/_production_workbench",
        },
        processing_mode: { id: "standard", label_zh: "标准优化" },
      };
      if (statusState === "running") {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            ...basePayload,
            running: true,
            summary: {
              schema_version: "scan-qc.production-run.v1",
              status: "running",
              operator_summary: {
                message_zh: "正在生成处理后图片，请保持本机和磁盘可用。",
                total_source_images: 8,
                derivative_images_ready: 3,
                files_needing_attention: 0,
              },
              counts: { total_files: 8, processed_files: 3, failed_files: 0 },
            },
            progress: {
              schema_version: "scan-qc.production-run-progress.v1",
              state: "running",
              current_step: "process",
              steps: [{ id: "process", label: "生成处理后图片", state: "running", total_items: 8, completed_items: 3 }],
            },
            queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
          }),
        });
        return;
      }
      if (statusState === "needs_review") {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            ...basePayload,
            running: false,
            summary: {
              schema_version: "scan-qc.production-run.v1",
              status: "needs_review",
              operator_summary: {
                message_zh: "有图片需要人工确认。",
                total_source_images: 8,
                derivative_images_ready: 8,
                files_needing_attention: 1,
              },
              counts: { total_files: 8, processed_files: 8, failed_files: 0 },
            },
            progress: { schema_version: "scan-qc.production-run-progress.v1", state: "needs_review" },
            queue: {
              schema_version: "scan-qc.production-review-queue.v1",
              items: [{
                local_id: "PRQ000010",
                reason_zh: "画面需要确认。",
                focus_hints_zh: ["看图片能否正常打开"],
                suggested_action: "pass",
                severity: "P1",
                preview_source: "unavailable",
                preview_sources: { original: false, processed: false },
              }],
            },
          }),
        });
        return;
      }
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          ...basePayload,
          running: false,
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "blocked",
            operator_summary: {
              message_zh: "处理没有正常完成。",
              total_source_images: 8,
              derivative_images_ready: 3,
              files_needing_attention: 5,
            },
            counts: { total_files: 8, processed_files: 3, failed_files: 5, retry_list_files: 0 },
          },
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "blocked" },
          queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#loadStatus")).toHaveText("批次正在运行，请等待。本机正在处理图片，处理完成或失败前不能更改文件夹和处理方式，也不要反复点击开始处理。");
    await expect(page.locator("#inputPath")).toBeDisabled();
    await expect(page.locator("#outputPath")).toBeDisabled();
    await expectProcessingModeRadiosDisabled(page, true);
    await expect(page.getByRole("button", { name: "保存文件夹" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();

    statusState = "needs_review";
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForTimeout(1300);
    await expect(page.locator("#stateName")).toHaveText("需要确认");
    await expect(page.getByRole("button", { name: "确认通过" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeEnabled();
    await expect(page.locator("#previewSourceText")).toHaveText("图片查看：本机暂未找到可查看图片。");
    await expect(page.locator("#activePreviewModeText")).toHaveText("当前查看：暂无可查看图片，请检查本机文件夹");
    await expect(page.getByText("本张暂时没有可查看图片。请检查本机文件夹是否还在，必要时交管理员处理。")).toBeVisible();
    await expect(page.locator('#comparisonControls button[data-comparison-mode="original"]')).toBeDisabled();
    await expect(page.locator('#comparisonControls button[data-comparison-mode="processed"]')).toBeDisabled();
    await expect(page.locator('#comparisonControls button[data-comparison-mode="side_by_side"]')).toBeDisabled();
    await expectOperatorStatusHidesPaths(page, ["PRQ000010", ".jpg", ".png", ".tif", "sha256", "OCR"]);
    await expect(page.locator("#inputPath")).toBeDisabled();
    await expect(page.getByRole("button", { name: "保存文件夹" })).toBeDisabled();

    statusState = "failed";
    await page.reload();
    await expect(page.locator("#stateName")).toHaveText("需处理");
    await expect(page.locator("#inputPath")).toBeEnabled();
    await expect(page.locator("#outputPath")).toBeEnabled();
    await expectProcessingModeRadiosDisabled(page, false);
    await expect(page.getByRole("button", { name: "保存文件夹" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeDisabled();
    await expectOperatorStatusHidesPaths(page, ["/tmp/transition-input", "/tmp/transition-output"]);
  });

  test("sends selected processing mode before starting a local run", async ({ page }) => {
    const consoleProblems = [];
    const configurePayloads = [];
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      configurePayloads.push(payload);
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: {
            id: payload.processing_mode,
            label_zh: "只质检不修图",
          },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "ready",
            ready_to_start: true,
            supported_image_count: 2,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: {
              id: payload.processing_mode,
              label_zh: "只质检不修图",
            },
            title_zh: "文件夹可以开始处理",
            message_zh: "发现 2 张可处理图片，输出文件夹可以写入。",
            next_steps_zh: ["确认处理方式无误。", "点击开始处理。"],
          },
        }),
      });
    });
    await page.route("**/api/start", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: true,
          configured: true,
          processing_mode: {
            id: "qc_only",
            label_zh: "只质检不修图",
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#modeStatus")).toContainText("当前处理方式：标准优化");
    await expect(page.locator("#modeStatus")).toContainText("推荐用于正常批量生产");
    await page.getByLabel("只质检不修图").check();
    await expect(page.locator("#modeStatus")).toContainText("当前处理方式：只质检不修图");
    await expect(page.locator("#modeStatus")).toContainText("不会生成处理后优化图片");
    await page.locator("#inputPath").fill("/tmp/mode-input");
    await page.locator("#outputPath").fill("/tmp/mode-output");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect.poll(() => configurePayloads.length).toBeGreaterThan(0);
    await expect(page.locator("#readinessTitle")).toHaveText("文件夹可以开始处理");
    await expect(page.locator("#readinessFacts")).toContainText("可处理图片：2 张");
    await expect(page.locator("#readinessFacts")).toContainText("输出文件夹：可以写入");
    await expect(page.locator("#readinessFacts")).toContainText("处理方式：只质检不修图");
    await expect(page.locator("#readinessFacts")).toContainText("输出结果：不会生成处理后优化图片");
    expect(configurePayloads[0]).toMatchObject({
      input_dir: "/tmp/mode-input",
      derivatives_dir: "/tmp/mode-output",
      processing_mode: "qc_only",
    });
    await expect(page.locator("#modeStatus")).toContainText("当前处理方式：只质检不修图");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeEnabled();
    await page.getByRole("button", { name: "开始处理" }).click();
    await expect(page.locator("#stateName")).toHaveText("正在处理");

    expect(consoleProblems).toEqual([]);
  });

  test("shows ready preflight guidance and starts only after folders are ready", async ({ page }) => {
    let startRequested = false;
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: { id: "standard", label_zh: "标准优化" },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "ready",
            ready_to_start: true,
            supported_image_count: 3,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: "standard", label_zh: "标准优化" },
            title_zh: "文件夹可以开始处理",
            message_zh: "发现 3 张可处理图片，输出文件夹可以写入。",
            next_steps_zh: ["确认处理方式无误。", "点击开始处理。"],
          },
        }),
      });
    });
    await page.route("**/api/start", async (route) => {
      startRequested = true;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: true, configured: true }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.locator("#inputPath").fill("/tmp/ready-input");
    await page.locator("#outputPath").fill("/tmp/ready-output");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.locator("#loadStatus")).toHaveText("文件夹已保存，可以开始处理。原图不会被覆盖，处理后图片会准备到输出文件夹。");
    await expect(page.locator("#readinessTitle")).toHaveText("文件夹可以开始处理");
    await expect(page.locator("#readinessFacts")).toContainText("可处理图片：3 张");
    await expect(page.locator("#readinessFacts")).toContainText("输出文件夹：可以写入");
    await expect(page.locator("#readinessFacts")).toContainText("处理方式：标准优化");
    await expect(page.locator("#readinessFacts")).toContainText("方式说明：推荐用于正常批量生产");
    await expectOperatorStatusHidesPaths(page, ["/tmp/ready-input", "/tmp/ready-output", "ready-input", "ready-output"]);
    await page.getByRole("button", { name: "开始处理" }).click();
    await expect(page.locator("#loadStatus")).toHaveText("批次正在运行，请等待。本机正在处理图片，处理完成或失败前不能更改文件夹和处理方式，也不要反复点击开始处理。");
    await expect.poll(() => startRequested).toBe(true);
  });

  test("shows existing output risk prompts without private details", async ({ page }) => {
    const cases = [
      {
        kind: "none",
        title: "文件夹可以开始处理",
        message: "本批预检结果：已识别到 3 张可处理图片，输出文件夹可以写入；未发现已有工作台结果，可以开始。",
        steps: ["确认处理方式无误。", "点击开始处理。"],
        hidden: true,
      },
      {
        kind: "reusable_current_batch",
        title: "本批已有可复用处理结果",
        message: "本批已有可复用处理结果，可以继续本批，只补齐缺失输出。",
        steps: ["复用已有结果。", "继续本批，只补齐缺失输出。"],
        promptTitle: "可继续本批",
        promptText: "可以继续本批，系统会复用已有结果，只补齐缺失输出。",
      },
      {
        kind: "existing_workbench_results",
        title: "输出文件夹已有本工具结果",
        message: "输出文件夹已有本工具结果，建议换空输出文件夹后再处理。",
        steps: ["更换一个空的输出文件夹。", "如上一批还没有交接，请先交接上一批。"],
        promptTitle: "建议换空输出文件夹",
        promptText: "建议换空输出文件夹，或先交接上一批后再继续。",
      },
      {
        kind: "completed_handoff",
        title: "输出文件夹已有完成交接材料",
        message: "输出文件夹已有完成交接材料，请先完成或归档上一批。",
        steps: ["先交接上一批。", "更换一个空的输出文件夹。"],
        promptTitle: "先交接上一批",
        promptText: "建议换空输出文件夹，或先交接上一批后再继续。",
      },
    ];
    let activeCase = cases[0];

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: { id: "standard", label_zh: "标准优化" },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "ready",
            ready_to_start: true,
            supported_image_count: 3,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: "standard", label_zh: "标准优化" },
            title_zh: activeCase.title,
            message_zh: activeCase.message,
            next_steps_zh: activeCase.steps,
            existing_output_risk: {
              schema_version: "scan-qc.local-existing-output-risk.v1",
              aggregate_only: true,
              kind: activeCase.kind,
              severity: activeCase.kind === "none" ? "none" : "warning",
              private_path: "/tmp/private-output/batch-001",
              file_name: "secret-page.png",
              sha256: "abc123privatehash",
              ocr_text: "PRIVATE OCR TEXT",
              thumbnail: "data:image/png;base64,private",
              evidence: ["row-level-private-evidence"],
              stack_trace: "Traceback private detail",
            },
          },
        }),
      });
    });

    for (const item of cases) {
      activeCase = item;
      await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
      await page.locator("#inputPath").fill(`/tmp/${item.kind}-input`);
      await page.locator("#outputPath").fill(`/tmp/${item.kind}-output`);
      await page.getByRole("button", { name: "保存文件夹" }).click();
      await expect(page.locator("#readinessTitle")).toHaveText(item.title);
      await expect(page.locator("#readinessMessage")).toHaveText(item.message);
      await expect(page.getByRole("button", { name: "开始处理" })).toBeEnabled();
      if (item.hidden) {
        await expect(page.locator("#readinessRiskPrompt")).toBeHidden();
      } else {
        await expect(page.locator("#readinessRiskPrompt")).toBeVisible();
        await expect(page.locator("#readinessRiskTitle")).toHaveText(item.promptTitle);
        await expect(page.locator("#readinessRiskMessage")).toContainText(item.promptText);
      }
      await expectOperatorStatusHidesPaths(page, [
        `/tmp/${item.kind}-input`,
        `/tmp/${item.kind}-output`,
        "/tmp/private-output/batch-001",
        "secret-page.png",
        "abc123privatehash",
        "PRIVATE OCR TEXT",
        "data:image/png",
        "row-level-private-evidence",
        "Traceback private detail",
      ]);
    }
  });

  test("keeps setup controls locked after configure while start request is delayed", async ({ page }) => {
    let startRequested = false;
    let resolveStart;
    const startGate = new Promise((resolve) => {
      resolveStart = resolve;
    });
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: { id: "standard", label_zh: "标准优化" },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "ready",
            ready_to_start: true,
            supported_image_count: 4,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: "standard", label_zh: "标准优化" },
            title_zh: "文件夹可以开始处理",
            message_zh: "发现 4 张可处理图片，输出文件夹可以写入。",
            next_steps_zh: ["确认处理方式无误。", "点击开始处理。"],
          },
        }),
      });
    });
    await page.route("**/api/start", async (route) => {
      startRequested = true;
      await startGate;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: true,
          configured: true,
          progress: {
            state: "running",
            current_step: "process",
            steps: [{ id: "process", label: "正在处理", state: "running", completed_items: 1, total_items: 4 }],
          },
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "running",
            counts: { total_files: 4, processed_files: 1, failed_files: 0 },
            operator_summary: {
              total_source_images: 4,
              derivative_images_ready: 1,
              files_needing_attention: 0,
              message_zh: "本机正在处理图片，请等待。",
            },
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.locator("#inputPath").fill("/tmp/delayed-start-input");
    await page.locator("#outputPath").fill("/tmp/delayed-start-output");
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeEnabled();

    await page.getByRole("button", { name: "开始处理" }).click();
    await expect.poll(() => startRequested).toBe(true);
    await expect(page.locator("#loadStatus")).toHaveText("批次正在运行，请等待。本机正在处理图片，处理完成或失败前不能更改文件夹和处理方式，也不要反复点击开始处理。");
    await expect(page.locator("#stateName")).toHaveText("准备完成");
    await expect(page.locator("#readinessFacts")).toContainText("可处理图片：4 张");
    await expectLaunchSetupControlsDisabled(page);
    await expectOperatorStatusHidesPaths(page, ["/tmp/delayed-start-input", "/tmp/delayed-start-output", "delayed-start-input", "delayed-start-output"]);

    resolveStart();
    await expect(page.locator("#stateName")).toHaveText("正在处理");
    await expect(page.locator("#progressText")).toContainText("已处理 1 张 / 共 4 张");
    await expectLaunchSetupControlsDisabled(page);
  });

  test("saved-ready then path edit disables Start until folders are saved again", async ({ page }) => {
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: { id: "standard", label_zh: "标准优化" },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "ready",
            ready_to_start: true,
            supported_image_count: 2,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: "standard", label_zh: "标准优化" },
            title_zh: "文件夹可以开始处理",
            message_zh: "发现 2 张可处理图片，输出文件夹可以写入。",
            next_steps_zh: ["确认处理方式无误。", "点击开始处理。"],
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.locator("#inputPath").fill("/tmp/saved-ready-input");
    await page.locator("#outputPath").fill("/tmp/saved-ready-output");
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeEnabled();

    await page.locator("#inputPath").fill("/tmp/edited-ready-input");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.locator("#loadStatus")).toHaveText("扫描原图文件夹已更改，请重新保存文件夹。");
    await expect(page.locator("#readinessBox")).toBeHidden();

    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeEnabled();
    await page.locator("#outputPath").fill("/tmp/edited-ready-output");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.locator("#loadStatus")).toHaveText("处理后输出文件夹已更改，请重新保存文件夹。");
  });

  test("after completing a batch, editing the next setup invalidates stale readiness", async ({ page }) => {
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: "/tmp/completed-edit-input",
            derivatives: "/tmp/completed-edit-output",
            metadata: "/tmp/completed-edit-output/_production_workbench",
          },
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "finished",
            operator_summary: {
              message_zh: "处理后图片已生成，可以完成并导出结果。",
              total_source_images: 2,
              derivative_images_ready: 2,
              files_needing_attention: 0,
            },
            counts: { total_files: 2, openable_files: 2, processed_files: 2, failed_files: 0, retry_list_files: 0 },
          },
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "finished" },
          queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
        }),
      });
    });
    await page.route("**/api/finish-decisions", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          finished: true,
          message_zh: "本批已完成：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
          completion_panel: {
            title_zh: "本批已完成",
            message_zh: "处理后图片已准备好。请检查输出文件夹后再交接。",
            completion_status_zh: "本批已完成",
            processing_mode: {
              id: "standard",
              label_zh: "标准优化",
              purpose_zh: "推荐用于正常批量生产，兼顾批量图片质量和处理效率。",
              output_zh: "会生成处理后优化图片，原图不覆盖。",
            },
            manual_work_zh: "没有待人工处理图片",
            admin_handoff_zh: "不需要",
            total_review_items: 0,
            reviewed_items: 0,
            pending_items: 0,
            next_steps_zh: ["打开输出文件夹，检查处理后图片数量和画面状态。", "本机状态文件夹已保存复核结果和交接说明，正常界面不显示具体路径或文件名。", "需要继续加工时，点击准备下一批；当前复核队列会清空。", "为新批次必须重新选择扫描原图文件夹，不要混用批次；输出文件夹可沿用上次保存的位置。"],
          },
        }),
      });
    });
    await page.route("**/api/reset-batch", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false, configured: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: { id: "standard", label_zh: "标准优化" },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "ready",
            ready_to_start: true,
            supported_image_count: 5,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: "standard", label_zh: "标准优化" },
            title_zh: "文件夹可以开始处理",
            message_zh: "发现 5 张可处理图片，输出文件夹可以写入。",
            next_steps_zh: ["确认处理方式无误。", "点击开始处理。"],
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await page.getByRole("button", { name: "确认完成本批" }).click();
    await expect(page.locator("#completionTitle")).toHaveText("本批已完成");
    await page.getByRole("button", { name: "准备下一批" }).click();
    await page.locator("#inputPath").fill("/tmp/new-completed-input");
    await page.locator("#outputPath").fill("/tmp/new-completed-output");
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeEnabled();
    await expect(page.locator("#readinessTitle")).toHaveText("文件夹可以开始处理");

    await page.locator("#inputPath").fill("/tmp/new-completed-input-edited");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.locator("#readinessBox")).toBeHidden();
    await expect(page.locator("#loadStatus")).toHaveText("扫描原图文件夹已更改，请重新保存文件夹。");
  });

  test("saved-ready then processing mode edit disables Start until folders are saved again", async ({ page }) => {
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: { id: payload.processing_mode, label_zh: payload.processing_mode === "light" ? "轻度优化" : "标准优化" },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "ready",
            ready_to_start: true,
            supported_image_count: 2,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: payload.processing_mode, label_zh: payload.processing_mode === "light" ? "轻度优化" : "标准优化" },
            title_zh: "文件夹可以开始处理",
            message_zh: "发现 2 张可处理图片，输出文件夹可以写入。",
            next_steps_zh: ["确认处理方式无误。", "点击开始处理。"],
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.locator("#inputPath").fill("/tmp/mode-stale-input");
    await page.locator("#outputPath").fill("/tmp/mode-stale-output");
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeEnabled();

    await page.getByLabel("轻度优化").check();
    await expect(page.locator("#modeStatus")).toContainText("当前处理方式：轻度优化");
    await expect(page.locator("#modeStatus")).toContainText("担心过度处理");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.locator("#loadStatus")).toHaveText("处理方式已更改，请重新保存文件夹。");
    await expect(page.locator("#readinessBox")).toBeHidden();
  });

  test("start reconfigure returning not-ready does not call start", async ({ page }) => {
    let configureCount = 0;
    let startRequested = false;
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      configureCount += 1;
      const payload = JSON.parse(route.request().postData() || "{}");
      const ready = configureCount === 1;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: { id: "standard", label_zh: "标准优化" },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: ready ? "ready" : "unsupported",
            ready_to_start: ready,
            supported_image_count: ready ? 2 : 0,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: "standard", label_zh: "标准优化" },
            title_zh: ready ? "文件夹可以开始处理" : "没有可处理的图片",
            message_zh: ready ? "发现 2 张可处理图片，输出文件夹可以写入。" : "文件夹里没有找到当前支持处理的图片。",
            next_steps_zh: ready ? ["确认处理方式无误。", "点击开始处理。"] : ["确认原图是常见图片格式。"],
          },
        }),
      });
    });
    await page.route("**/api/start", async (route) => {
      startRequested = true;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: true, configured: true }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.locator("#inputPath").fill("/tmp/recheck-input");
    await page.locator("#outputPath").fill("/tmp/recheck-output");
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeEnabled();
    await page.getByRole("button", { name: "开始处理" }).click();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
    await expect(page.locator("#loadStatus")).toHaveText("还不能开始：没有可处理的图片。确认原图是常见图片格式。");
    expect(configureCount).toBe(2);
    expect(startRequested).toBe(false);
  });

  test("shows one Chinese reason when source or output folder is missing", async ({ page }) => {
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.locator("#loadStatus")).toHaveText("还不能开始：请先填写扫描原图文件夹。");
    await page.locator("#inputPath").fill("/tmp/missing-output-input");
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.locator("#loadStatus")).toHaveText("还不能开始：请先填写处理后输出文件夹。");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();
  });

  test("shows start-request preflight block without private paths", async ({ page }) => {
    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "ready",
            ready_to_start: true,
            supported_image_count: 1,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: "standard", label_zh: "标准优化" },
            title_zh: "文件夹可以开始处理",
            message_zh: "发现 1 张可处理图片，输出文件夹可以写入。",
            next_steps_zh: ["确认处理方式无误。", "点击开始处理。"],
          },
        }),
      });
    });
    await page.route("**/api/start", async (route) => {
      await route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({
          error_zh: "原图文件夹是空的",
          preflight_guidance: {
            schema_version: "scan-qc.local-folder-preflight.v1",
            aggregate_only: true,
            kind: "input_folder_empty",
            title_zh: "原图文件夹是空的",
            message_zh: "扫描原图文件夹里没有文件，处理没有启动。",
            next_steps_zh: ["确认是否选到了本批次真正的扫描原图文件夹。", "放好图片后，重新保存文件夹并开始处理。"],
            failed_files: 0,
            retryable_files: 0,
            derivative_images_ready: 0,
            total_files: 0,
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.locator("#inputPath").fill("/tmp/start-empty-input");
    await page.locator("#outputPath").fill("/tmp/start-empty-output");
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await page.getByRole("button", { name: "开始处理" }).click();
    await expect(page.locator("#loadStatus")).toHaveText("原图文件夹是空的");
    await expect(page.locator("#recoveryTitle")).toHaveText("原图文件夹是空的");
    await expect(page.getByText("扫描原图文件夹里没有文件，处理没有启动。")).toBeVisible();
    await expect(page.getByText("放好图片后，重新保存文件夹并开始处理。")).toBeVisible();
    await expectOperatorStatusHidesPaths(page, ["/tmp/start-empty-input", "/tmp/start-empty-output", "start-empty-input", "start-empty-output"]);
  });

  test("shows unsupported-folder readiness guidance before starting", async ({ page }) => {
    const consoleProblems = [];
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });
    await page.route("**/api/configure", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: payload.input_dir,
            derivatives: payload.derivatives_dir,
            metadata: `${payload.derivatives_dir}/_production_workbench`,
          },
          processing_mode: { id: "standard", label_zh: "标准优化" },
          folder_readiness: {
            schema_version: "scan-qc.local-folder-readiness.v1",
            aggregate_only: true,
            status: "unsupported",
            ready_to_start: false,
            supported_image_count: 0,
            input_empty: false,
            output_writable: true,
            selected_processing_mode: { id: "standard", label_zh: "标准优化" },
            title_zh: "没有可处理的图片",
            message_zh: "文件夹里没有找到当前支持处理的图片。",
            next_steps_zh: ["确认原图是常见图片格式。", "如果格式不对，请重新导出为支持的图片格式后再处理。"],
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.locator("#inputPath").fill("/tmp/unsupported-input");
    await page.locator("#outputPath").fill("/tmp/unsupported-output");
    await page.getByRole("button", { name: "保存文件夹" }).click();
    await expect(page.locator("#readinessTitle")).toHaveText("没有可处理的图片");
    await expect(page.locator("#readinessFacts")).toContainText("可处理图片：0 张");
    await expect(page.locator("#readinessFacts")).toContainText("输出文件夹：可以写入");
    await expect(page.locator("#readinessSteps").getByText("确认原图是常见图片格式。", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();

    expect(consoleProblems).toEqual([]);
  });

  test("finishes and exports a no-review batch without console errors or warnings", async ({ page }) => {
    const consoleProblems = [];
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          running: false,
          configured: true,
          folders: {
            input: "/tmp/no-review-input",
            derivatives: "/tmp/no-review-output",
            metadata: "/tmp/no-review-output/_production_workbench",
          },
          summary: {
            schema_version: "scan-qc.production-run.v1",
            status: "finished",
            ready_for_operator_handoff: true,
            operator_summary: {
              message_zh: "处理后图片已生成，可以完成并导出结果。",
              total_source_images: 2,
              openable_source_images: 2,
              derivative_images_ready: 2,
              files_needing_attention: 0,
            },
            counts: {
              total_files: 2,
              openable_files: 2,
              processed_files: 2,
              failed_files: 0,
              retry_list_files: 0,
            },
          },
          progress: { schema_version: "scan-qc.production-run-progress.v1", state: "finished" },
          queue: { schema_version: "scan-qc.production-review-queue.v1", items: [] },
          draft_decisions: null,
        }),
      });
    });
    await page.route("**/api/finish-decisions", async (route) => {
      const payload = JSON.parse(route.request().postData() || "{}");
      expect(payload.decisions).toHaveLength(0);
      expect(payload.aggregate_counts.review_completion.complete).toBe(true);
      expect(payload.aggregate_counts.review_completion.total).toBe(0);
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          finished: true,
          message_zh: "本批已完成：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
          completion_panel: {
            title_zh: "本批已完成",
            message_zh: "处理后图片已准备好。请检查输出文件夹后再交接。",
            completion_status_zh: "本批已完成",
            manual_work_zh: "没有待人工处理图片",
            admin_handoff_zh: "不需要",
            total_review_items: 0,
            reviewed_items: 0,
            pending_items: 0,
            checklist_zh: ["打开输出文件夹，检查处理后图片数量和画面状态", "复核结果和交接说明已保存到本机状态文件夹", "准备下一批会清空当前复核队列，请重新选择新一批文件夹"],
            next_steps_zh: ["打开输出文件夹，检查处理后图片数量和画面状态。", "本机状态文件夹已保存复核结果和交接说明，正常界面不显示具体路径或文件名。", "需要继续加工时，点击准备下一批；当前复核队列会清空。", "为新批次必须重新选择扫描原图文件夹，不要混用批次；输出文件夹可沿用上次保存的位置。", "如果仍有异常或不能交接，请交管理员处理。"],
          },
          decision_summary: { completion_status: "complete" },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#stateName")).toHaveText("待完成");
    await expect(page.locator("#stateAction")).toHaveText("没有需要人工确认");
    await expect(page.locator("#queueText")).toHaveText("没有待人工确认图片。");
    await expect(page.locator("#currentAdvice")).toHaveText("可以完成并导出结果。");
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeEnabled();

    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await expect(page.locator("#finishConfirmPanel")).toBeVisible();
    await expect(page.locator("#finishConfirmCounts")).toHaveText("共 0 项，已确认 0 项，待决定 0 项。");
    await expect(page.locator("#finishConfirmMode")).toContainText("标准优化");
    await page.getByRole("button", { name: "确认完成本批" }).click();
    await expect(page.locator("#completionTitle")).toHaveText("本批已完成");
    await expect(page.locator("#completionCounts")).toHaveText("共 0 项，已确认 0 项，待决定 0 项。");
    await expect(page.locator("#completionStatusFact")).toHaveText("本批已完成");
    await expect(page.locator("#completionModeFact")).toHaveText("标准优化");
    await expect(page.locator("#outputPlace")).toHaveText("已准备 2 张处理后图片");
    await expect(page.locator("#manualWorkFact")).toHaveText("没有待人工处理图片");
    await expect(page.locator("#adminHandoffFact")).toHaveText("不需要");
    await expectOperatorStatusHidesPaths(page, [
      "/tmp/no-review-input",
      "/tmp/no-review-output",
      "/tmp/no-review-output/_production_workbench",
    ]);
    await expect(page.locator("#completionSteps").getByText("打开输出文件夹，检查处理后图片数量和画面状态。")).toBeVisible();
    await expect(page.locator("#completionSteps").getByText("本机状态文件夹已保存复核结果和交接说明，正常界面不显示具体路径或文件名。")).toBeVisible();
    await expect(page.locator("#completionSteps").getByText("需要继续加工时，点击准备下一批；当前复核队列会清空。")).toBeVisible();
    await expect(page.locator("#completionSteps").getByText("为新批次必须重新选择扫描原图文件夹，不要混用批次；输出文件夹可沿用上次保存的位置。")).toBeVisible();

    expect(consoleProblems).toEqual([]);
  });

  test("renders maintained fixture states without console errors or warnings", async ({ page }) => {
    const consoleProblems = [];
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) consoleProblems.push(`${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => consoleProblems.push(`pageerror: ${error.message}`));

    await page.route("**/api/status", async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ schema_version: "scan-qc.local-production-workbench.v1", running: false }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await page.getByText("维护入口").click();
    const fixtures = [
      ["fixtures/production-run-empty", "#recoveryTitle", "原图文件夹是空的"],
      ["fixtures/production-run-running", "#stateAction", "正在处理"],
      ["fixtures/production-run-needs-review", "#stateAction", "有图片需要人工确认"],
      ["fixtures/production-run-finished", "#stateAction", "没有需要人工确认"],
      ["fixtures/production-run-retryable", "#recoveryTitle", "处理没有全部完成"],
      ["fixtures/production-run-blocked", "#stateAction", "需要管理员处理"],
    ];

    for (const [fixture, selector, visibleText] of fixtures) {
      await page.locator("#fixtureSelect").selectOption(fixture);
      await expect(page.locator(selector)).toHaveText(visibleText);
      await expect(page.locator("#reviewPositionText")).toContainText("当前第");
      await expect(page.locator("#previewSourceText")).toContainText("图片查看");
    }

    expect(consoleProblems).toEqual([]);
  });
});
