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
        { local_id: "PRQ000001", reason_zh: "画面需要确认。", suggested_action: "rescan", severity: "P1", preview_source: "processed" },
        { local_id: "PRQ000002", reason_zh: "页面顺序需要确认。", suggested_action: "keep_original_trace", severity: "P2", preview_source: "original_fallback" },
        { local_id: "PRQ000003", reason_zh: "质量结果需要确认。", suggested_action: "skip", severity: "P0", preview_source: "unavailable" },
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
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "scan-qc.local-production-workbench.v1",
          finished: true,
          message_zh: "完成并导出结果：处理后图片和复核结果已保存。",
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
            title_zh: "完成并导出结果",
            message_zh: "本批次已完成。处理后图片在输出文件夹，复核结果已保存到本机状态文件夹。",
            total_review_items: 3,
            reviewed_items: 3,
            pending_items: 0,
            derivatives_dir: "/tmp/synthetic-output",
            metadata_dir: "/tmp/synthetic-output/_production_workbench",
            completion_note_path: "/tmp/synthetic-output/_production_workbench/本批次完成交接说明.txt",
            checklist_zh: ["处理后图片已准备好", "复核结果已保存", "交接说明已保存", "可以准备下一批"],
            next_steps_zh: ["到处理后输出文件夹检查图片数量和文件是否齐全。", "点击准备下一批，重新选择新的扫描原图文件夹和输出文件夹。"],
          },
          decision_summary: { completion_status: "complete" },
        }),
      });
    });

    await page.goto(`${baseUrl}${WORKBENCH_URL_PATH}`);
    await expect(page.getByRole("heading", { name: "当前图片" })).toBeVisible();
    await expect(page.getByText("已加载复核队列 3 项，待决定 3 项。")).toBeVisible();
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 1 张 / 共 3 张 / 待确认 3 张。");
    await expect(page.locator("#previewSourceText")).toHaveText("预览：处理后图片。");
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");

    await page.getByRole("button", { name: "放大" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：125%");
    await page.getByRole("button", { name: "缩小" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：100%");
    await page.getByRole("button", { name: "还原" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：100%");
    await page.getByRole("button", { name: "适合窗口" }).click();
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");

    await page.getByRole("button", { name: "退回重扫或重处理" }).click();
    await expect(page.locator("#previewSourceText")).toHaveText("预览：处理后图片不可用，正在显示原图。");
    await expect(page.locator("#zoomState")).toHaveText("查看：适合窗口");
    await page.getByRole("button", { name: "上一张已确认" }).click();
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 1 张 / 共 3 张 / 待确认 2 张。");
    await expect(page.locator("#currentAdvice")).toContainText("当前决定：退回重扫或重处理。");
    await page.getByRole("button", { name: "清除当前决定" }).click();
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 0 项，待决定 3 项。");
    await expect(page.getByRole("button", { name: "完成并导出结果" })).toBeDisabled();
    await page.getByRole("button", { name: "确认通过" }).click();
    await expect(page.locator("#reviewPositionText")).toHaveText("当前第 2 张 / 共 3 张 / 待确认 2 张。");
    await page.getByRole("button", { name: "确认保留原貌" }).click();
    await page.getByRole("button", { name: "交管理员处理" }).click();
    await expect(page.locator("#decisionSummary")).toHaveText("已决定 3 项，待决定 0 项。");

    await page.getByRole("button", { name: "完成并导出结果" }).click();
    await expect(page.locator("#completionTitle")).toHaveText("完成并导出结果");
    await expect(page.locator("#completionCounts")).toHaveText("共 3 项，已确认 3 项，待决定 0 项。");
    await expect(page.locator("#outputPlace")).toHaveText("/tmp/synthetic-output");
    await expect(page.locator("#decisionSavePlace")).toHaveText("/tmp/synthetic-output/_production_workbench");
    await expect(page.locator("#completionNotePlace")).toHaveText("/tmp/synthetic-output/_production_workbench/本批次完成交接说明.txt");
    await expect(page.locator("#completionChecklist")).toHaveText("处理后图片已准备好复核结果已保存交接说明已保存可以准备下一批");
    await expect(page.getByText("点击准备下一批，重新选择新的扫描原图文件夹和输出文件夹。")).toBeVisible();
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
      ["fixtures/production-run-finished", "#stateAction", "完成并导出结果"],
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
