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
    .locator("#outputPanel, #loadStatus, #inputStatus, #outputStatus, #stateName, #stateAction, #stateHint, #currentAdvice")
    .allTextContents();
  const combined = operatorStatusText.join("\n");
  for (const value of ["/tmp", "/private", "/Users", ...forbiddenPaths]) {
    expect(combined).not.toContain(value);
  }
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
    await expect(page.locator(".mode-selector")).toContainText("标准优化");
    await expect(page.locator(".mode-selector")).toContainText("适合正常批量加工，自动做保守裁边、纠偏、去黑边、去明显小污点，原图不覆盖。");
    await expect(page.locator(".mode-selector")).toContainText("轻度优化");
    await expect(page.locator(".mode-selector")).toContainText("只做较少处理，适合担心过度处理的批次。");
    await expect(page.locator(".mode-selector")).toContainText("只质检不修图");
    await expect(page.locator(".mode-selector")).toContainText("只检查，不生成修图优化结果。");

    await expect(page.locator(".maintenance-loader")).not.toHaveAttribute("open", "");
    await expect(page.getByText("选择维护示例")).toBeHidden();
    await page.getByText("维护入口").click();
    await expect(page.getByText("管理员排查、演练或查看本机状态时使用；这不是正常加工步骤。")).toBeVisible();
    await expect(page.getByText("只用于查看本机已经生成的处理状态，不会开始处理。")).toBeVisible();
  });

  test("finishes a synthetic review queue without console errors or warnings", async ({ page }) => {
    const consoleProblems = [];
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
          message_zh: "完成并导出结果：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
          folders: {
            derivatives: "/tmp/synthetic-output",
            metadata: "/tmp/synthetic-output/_production_workbench",
          },
          saved: {
            decision_summary: "/tmp/synthetic-output/_production_workbench/scan-qc-review-decisions.summary.json",
            verification_summary: "/tmp/synthetic-output/_production_workbench/review_decision_verification_summary.json",
            completion_note: "/tmp/synthetic-output/_production_workbench/本批次完成交接说明.txt",
          },
          completion_panel: {
            title_zh: "本批次已完成",
            message_zh: "处理后图片已准备好。本批可以交接。",
            completion_status_zh: "已完成",
            manual_work_zh: "没有待人工处理图片",
            admin_handoff_zh: "不需要",
            total_review_items: 3,
            reviewed_items: 3,
            pending_items: 0,
            derivatives_dir: "/tmp/synthetic-output",
            metadata_dir: "/tmp/synthetic-output/_production_workbench",
            completion_note_path: "/tmp/synthetic-output/_production_workbench/本批次完成交接说明.txt",
            checklist_zh: ["处理后图片已保存到输出文件夹", "复核结果和交接说明已保存到本机状态文件夹", "可以检查输出文件夹后准备下一批"],
            next_steps_zh: ["查看处理后图片。", "需要继续加工时，点击准备下一批。", "如果仍有异常或不能交接，请交管理员处理。"],
          },
          decision_summary: { completion_status: "complete" },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.getByRole("heading", { name: "当前图片" })).toBeVisible();
    await expect(page.getByText("已加载复核队列 3 项，待决定 3 项。")).toBeVisible();
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 1 张 / 共 3 张 / 待确认 3 张。");
    await expect(page.locator("#currentFocusHints")).toHaveText("看图片能否正常打开；重点判断是否需要重扫");
    await expect(page.locator("#currentRecommendation")).toHaveText("建议：退回重扫");
    await expect(page.getByRole("button", { name: "退回重扫" })).toHaveClass(/recommended-choice/);
    await expect(page.getByRole("button", { name: "确认通过" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "重新处理图片" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "确认保留原貌" })).toBeEnabled();
    await expect(page.locator("#previewSourceText")).toHaveText("预览：正在查看处理后图片。");
    await expect(page.locator(".comparison-title")).toHaveCount(0);
    await expect(page.getByText("正在查看处理后图片。可切到对比查看或查看原图。")).toBeVisible();
    await expect(page.getByRole("button", { name: "查看处理后图片" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "查看原图" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "对比查看" })).toBeEnabled();
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");

    await page.getByRole("button", { name: "放大" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");
    await page.locator("#operatorName").fill("复核员甲");
    await page.locator("#decisionNote").fill("切换查看方式时保留备注。");
    await page.getByRole("button", { name: "查看原图" }).click();
    await expect(page.locator("#previewSourceText")).toHaveText("预览：正在查看原图。");
    await expect(page.locator(".comparison-title")).toHaveCount(0);
    await expect(page.getByText("正在查看原图。可切到对比查看或查看处理后图片。")).toBeVisible();
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");
    await expect(page.locator("#operatorName")).toHaveValue("复核员甲");
    await expect(page.locator("#decisionNote")).toHaveValue("切换查看方式时保留备注。");
    await page.getByRole("button", { name: "查看处理后图片" }).click();
    await expect(page.locator("#previewSourceText")).toHaveText("预览：正在查看处理后图片。");
    await expect(page.getByText("正在查看处理后图片。可切到对比查看或查看原图。")).toBeVisible();
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");
    await page.getByRole("button", { name: "对比查看" }).click();
    await expect(page.locator("#previewSourceText")).toHaveText("预览：正在对比原图和处理后图片。");
    await expect(page.locator(".comparison-title", { hasText: "原图" })).toBeVisible();
    await expect(page.locator(".comparison-title", { hasText: "处理后图片" })).toBeVisible();
    await expect(page.locator("#decisionNote")).toHaveValue("切换查看方式时保留备注。");
    await page.getByRole("button", { name: "缩小" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：100%");
    await page.getByRole("button", { name: "还原" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：100%");
    await page.getByRole("button", { name: "适合窗口" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");

    await page.locator("#decisionNote").fill("边缘不清楚，需要补扫。");
    await expect(page.getByText("选择一个处理决定后，会记录当前图片，并自动显示下一张待确认图片。")).toBeVisible();
    await page.getByRole("button", { name: "退回重扫" }).click();
    await expect(page.getByText("已记录：退回重扫。已自动显示下一张待确认图片。已决定 1 项，待决定 2 项。")).toBeVisible();
    await expect(page.locator("#currentRecommendation")).toHaveText("建议：重新处理图片");
    await expect(page.getByRole("button", { name: "重新处理图片" })).toHaveClass(/recommended-choice/);
    await expect(page.locator("#previewSourceText")).toHaveText("预览：处理后图片不可用，正在显示原图。");
    await expect(page.getByText("处理后图片预览暂不可用。请查看原图，仍可选择一个处理决定。")).toBeVisible();
    await expect(page.getByRole("button", { name: "对比查看" })).toBeHidden();
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");
    await page.getByRole("button", { name: "上一张已确认" }).click();
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 1 张 / 共 3 张 / 待确认 2 张。");
    await expect(page.locator("#currentAdvice")).toContainText("当前决定：退回重扫。");
    await page.getByRole("button", { name: "清除当前决定" }).click();
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 0 项，待决定 3 项。");
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeDisabled();
    await page.locator("#decisionNote").fill("本张可以通过。");
    await page.getByRole("button", { name: "确认通过" }).click();
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 2 张 / 共 3 张 / 待确认 2 张。");
    await expect(page.locator("#operatorName")).toHaveValue("复核员甲");
    await expect(page.locator("#currentRecommendation")).toHaveText("建议：重新处理图片");
    await page.getByRole("button", { name: "重新处理图片" }).click();
    await expect(page.getByText("已记录：重新处理图片。已自动显示下一张待确认图片。已决定 2 项，待决定 1 项。")).toBeVisible();
    await expect(page.locator("#currentRecommendation")).toHaveText("建议：确认保留原貌");
    await expect(page.getByText("原图预览暂不可用。请查看处理后图片，仍可选择一个处理决定。")).toBeVisible();
    await page.locator("#decisionNote").fill("保留原貌即可。");
    await page.getByRole("button", { name: "确认保留原貌" }).click();
    await expect(page.getByText("已记录：确认保留原貌。所有待确认图片已有决定，可以完成并导出结果。")).toBeVisible();
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 3 项，待决定 0 项。");
    await page.getByRole("button", { name: "放大" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");

    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await expect(page.locator("#finishConfirmPanel")).toBeVisible();
    await expect(page.getByRole("heading", { name: "确认完成本批" })).toBeVisible();
    await expect(page.locator("#finishConfirmCounts")).toHaveText("共 3 项，已确认 3 项，待决定 0 项。");
    await expect(page.locator("#finishConfirmOutput")).toHaveText("处理后输出文件夹，已准备 3 张处理后图片");
    await expect(page.getByText("复核结果和交接说明将保存到本机状态文件夹。")).toBeVisible();
    await page.getByRole("button", { name: "返回继续检查" }).click();
    await expect(page.locator("#finishConfirmPanel")).toBeHidden();
    await expect(page.locator("#operatorName")).toHaveValue("复核员甲");
    await expect(page.locator("#decisionNote")).toHaveValue("保留原貌即可。");
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 3 项，待决定 0 项。");
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");

    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await page.getByRole("button", { name: "确认完成本批" }).click();
    await expect(page.locator("#completionTitle")).toHaveText("本批次已完成");
    await expect(page.locator("#completionCounts")).toHaveText("共 3 项，已确认 3 项，待决定 0 项。");
    await expect(page.locator("#completionStatusFact")).toHaveText("已完成");
    await expect(page.locator("#outputPlace")).toHaveText("已准备 3 张处理后图片");
    await expect(page.locator("#manualWorkFact")).toHaveText("没有待人工处理图片");
    await expect(page.locator("#adminHandoffFact")).toHaveText("不需要");
    await expectOperatorStatusHidesPaths(page, [
      "/tmp/synthetic-input",
      "/tmp/synthetic-output",
      "/tmp/synthetic-output/_production_workbench",
    ]);
    await expect(page.getByText("需要继续加工时，点击准备下一批。")).toBeVisible();
    await expect(page.getByText("如果仍有异常或不能交接，请交管理员处理。")).toBeVisible();
    await page.getByRole("button", { name: "准备下一批" }).click();
    await expect(page.locator("#completionTitle")).toBeHidden();
    await expect(page.locator("#stateName")).toHaveText("填写原图");
    await expect(page.locator("#inputPath")).toHaveValue("");
    await expect(page.locator("#outputPath")).toHaveValue("");
    await expect(page.getByRole("button", { name: "开始处理" })).toBeDisabled();

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
          message_zh: "完成并导出结果：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
          completion_panel: {
            title_zh: "本批次已完成",
            message_zh: "处理后图片已准备好。本批可以交接。",
            completion_status_zh: "已完成",
            manual_work_zh: "没有待人工处理图片",
            admin_handoff_zh: "不需要",
            total_review_items: 0,
            reviewed_items: 0,
            pending_items: 0,
            checklist_zh: ["处理后图片已保存到输出文件夹", "复核结果和交接说明已保存到本机状态文件夹", "可以检查输出文件夹后准备下一批"],
            next_steps_zh: ["查看处理后图片。"],
          },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#stateName")).toHaveText("待完成");
    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await expect(page.locator("#finishConfirmPanel")).toBeVisible();
    await expect(page.locator("#finishConfirmMessage")).toHaveText("本批没有需要人工确认的图片。请确认处理后图片已准备好，再完成本批。");
    await page.getByRole("button", { name: "返回继续检查" }).click();
    await expect(page.locator("#stateName")).toHaveText("待完成");
    await expect(finishRequested).toBe(false);
    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await page.getByRole("button", { name: "确认完成本批" }).click();
    await expect(page.locator("#completionTitle")).toHaveText("本批次已完成");
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
    await expect(page.locator("#progressText")).toHaveText("阶段：处理被阻断；总数 4 张，已完成 2 张，失败 2 张；状态：需要处理");
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

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.locator("#progressText")).toHaveText("阶段：处理被阻断；总数 2 张，已完成 0 张，失败 2 张；状态：需要处理");
    await expect(page.locator("#recoveryTitle")).toHaveText("处理没有全部完成");
    await expect(page.locator("#recoveryMessage")).toHaveText("本批次没有处理完，当前不能直接重试。");
    await expect(page.getByRole("button", { name: "重试本批次" })).toBeHidden();
    await expect(page.getByText("请交管理员处理，不要反复点击开始处理。")).toBeVisible();
    await expect(page.getByText("如果文件夹选错了，请返回重新选择文件夹。")).toBeVisible();
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
    await expect(page.locator("#progressText")).toHaveText("阶段：生成处理后图片；总数 120 张，已完成 48 张，失败 0 张；状态：正在处理");
    await expect(page.locator("#sourceText")).toHaveText("120 张");
    await expect(page.locator("#readyText")).toHaveText("48 张");
    await expectOperatorStatusHidesPaths(page, ["/tmp/running-input", "/tmp/running-output"]);
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
    await expect(page.locator("#modeStatus")).toHaveText("当前处理方式：标准优化");
    await page.getByLabel("只质检不修图").check();
    await expect(page.locator("#modeStatus")).toHaveText("当前处理方式：只质检不修图");
    await page.locator("#inputPath").fill("/tmp/mode-input");
    await page.locator("#outputPath").fill("/tmp/mode-output");
    await page.getByRole("button", { name: "开始处理" }).click();
    await expect.poll(() => configurePayloads.length).toBeGreaterThan(0);
    await expect(page.locator("#readinessTitle")).toHaveText("文件夹可以开始处理");
    await expect(page.locator("#readinessFacts")).toContainText("可处理图片：2 张");
    await expect(page.locator("#readinessFacts")).toContainText("输出文件夹：可以写入");
    await expect(page.locator("#readinessFacts")).toContainText("处理方式：只质检不修图");
    expect(configurePayloads[0]).toMatchObject({
      input_dir: "/tmp/mode-input",
      derivatives_dir: "/tmp/mode-output",
      processing_mode: "qc_only",
    });
    await expect(page.locator("#modeStatus")).toHaveText("当前处理方式：只质检不修图");
    await expect(page.locator("#stateName")).toHaveText("正在处理");

    expect(consoleProblems).toEqual([]);
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
    await expect(page.getByText("确认原图是常见图片格式。")).toBeVisible();
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
          message_zh: "完成并导出结果：处理后图片已保存到输出文件夹，复核结果和交接说明已保存到本机状态文件夹。",
          folders: {
            derivatives: "/tmp/no-review-output",
            metadata: "/tmp/no-review-output/_production_workbench",
          },
          completion_panel: {
            title_zh: "本批次已完成",
            message_zh: "处理后图片已准备好。本批可以交接。",
            completion_status_zh: "已完成",
            manual_work_zh: "没有待人工处理图片",
            admin_handoff_zh: "不需要",
            total_review_items: 0,
            reviewed_items: 0,
            pending_items: 0,
            derivatives_dir: "/tmp/no-review-output",
            metadata_dir: "/tmp/no-review-output/_production_workbench",
            completion_note_path: "/tmp/no-review-output/_production_workbench/本批次完成交接说明.txt",
            checklist_zh: ["处理后图片已保存到输出文件夹", "复核结果和交接说明已保存到本机状态文件夹", "可以检查输出文件夹后准备下一批"],
            next_steps_zh: ["查看处理后图片。", "需要继续加工时，点击准备下一批。", "如果仍有异常或不能交接，请交管理员处理。"],
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
    await page.getByRole("button", { name: "确认完成本批" }).click();
    await expect(page.locator("#completionTitle")).toHaveText("本批次已完成");
    await expect(page.locator("#completionCounts")).toHaveText("共 0 项，已确认 0 项，待决定 0 项。");
    await expect(page.locator("#completionStatusFact")).toHaveText("已完成");
    await expect(page.locator("#outputPlace")).toHaveText("已准备 2 张处理后图片");
    await expect(page.locator("#manualWorkFact")).toHaveText("没有待人工处理图片");
    await expect(page.locator("#adminHandoffFact")).toHaveText("不需要");
    await expectOperatorStatusHidesPaths(page, [
      "/tmp/no-review-input",
      "/tmp/no-review-output",
      "/tmp/no-review-output/_production_workbench",
    ]);
    await expect(page.locator("#completionSteps").getByText("查看处理后图片。")).toBeVisible();
    await expect(page.locator("#completionSteps").getByText("需要继续加工时，点击准备下一批。")).toBeVisible();

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
      await expect(page.locator("#previewSourceText")).toContainText("预览");
    }

    expect(consoleProblems).toEqual([]);
  });
});
