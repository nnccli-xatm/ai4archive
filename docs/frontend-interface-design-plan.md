# 前端访问界面设计与实施计划

关联 issue：AI4-69
日期：2026-05-09
阶段：生产方向修正与前端入口重新规划；静态聚合工作台保留为后台/管理员/验收入口

AI4-148 方向修订：普通生产入口必须是“生产工人批量质检修图工作台”，不是统计优先 dashboard。当前 `docs/frontend-workbench-prototype.html` 这类静态聚合工作台应明确定位为后台、管理员、验收或维护入口，用于查看公开安全的聚合摘要、验证摘要、证据包状态和交接摘要；它不是扫描处理工人的日常主入口。

本地单机生产流程优先完善。局域网内多扫描电脑 + 一台服务器集中监控处理的部署形态放到后续阶段；国产化适配、OS-specific packaging、UOS/麒麟/openKylin、aarch64、LoongArch 实机适配暂时不进入前端任务范围。

补充架构目标：产品最终形态必须是标准后台服务加前端调用。前端和后端完全解耦，后端专注批量质检、图像处理性能、任务调度、隐私过滤、审计和标准服务接口；前端只通过接口查询状态、预览图片、提交复核动作和触发导出。相同接口应允许外部归档系统、验收工具或其他业务系统调用，不能把前端页面和内部 CLI/Worker 绑定成不可复用的一体化实现。

2026-06-09 目标对齐：前端设计必须服务于“高质量、高性能、可独立运行、可作为外部服务引擎调用”的产品目标。前端不得持有或推断图像处理 Worker 状态，不得把本地绝对路径作为可外传状态，不得跨任务复用复核队列或模板参数；所有生产动作都必须绑定后端返回的 job_id、batch_id、模板快照和授权预览资源。

## 1. 目标与边界

本阶段目标是为现有扫描图片批量质检程序补充必要的前端访问界面设计，使操作人员能够通过浏览器驱动批次流程、查看任务状态、预览图片和阅读质检结果。前端只作为独立访问层，通过标准后台服务接口调用后端能力，不改变已经完成的 CLI、规则引擎、批处理、报告和处理副本架构；CLI 可以继续作为开发、验证和离线兜底入口，但不应成为长期前端集成的主要调用方式。

修订后的生产目标是：让计算机经验有限的扫描处理工人，用中文、图片和少量明确动作完成本地批量质检修图。本地单机首版不设置产品登录页，启动后直接进入生产工作台；后台服务仍保留本机操作员标识、任务 ID、日志和恢复状态。Windows 中文路径、中文文件名、空格路径和源文件只读安全由后台服务保证，前端只展示中文动作语义、聚合状态和授权预览。普通生产工作流必须是：

1. 选择本地“扫描图片文件夹”，或进入单个 JPG 文件质检。
2. 选择“处理后图片输出文件夹”。
3. 选择需要启用的处理项，例如去黑边、装订孔处理、纠偏、去除背景色。
4. 点击“开始处理”，执行批量质检和安全自动修图。
5. 只复核系统挑出的少量“待复核图片”。
6. 点击“完成导出”，得到处理后图片和必要摘要。

聚合摘要、验证摘要、证据包、性能对比、验收和移交材料继续保留，但它们属于后台/管理员/验收工作台，不属于普通生产工人的第一屏。

明确不做：

- 不在前端实现图片处理、修图、纠偏、裁边、去污或模型推理。
- 不把后端流程迁移到前端执行。
- 不让前端直接调用图像处理 Worker、私有脚本或内部命令行；前端只消费后台服务接口。
- 不让前端跨 job 复用可变状态、复核队列、模板参数、临时预览 URL 或输出目录。
- 不让前端把用户选择的本地绝对路径写入可外传摘要；本地受控显示也必须由后端按授权返回。
- 不引入重量级前端框架作为第一步。
- 不改变现有 `archive-scan-qc` CLI 参数、报告字段和输出目录约定。
- 不在公开报告或聚合界面暴露私有样本路径、文件哈希、缩略图或敏感行级证据，除非部署在明确的本地受控环境中。
- 不新增统计优先 dashboard 面板作为生产工作台主体验收内容。

## 2. 用户与核心场景

### 2.1 生产工人

- 选择本地扫描图片文件夹。
- 选择处理后图片输出文件夹。
- 点击开始处理。
- 查看正在检查、正在优化图片、等待复核、正在导出、已完成等中文状态。
- 处理少量待复核图片，并选择通过、需要重扫、重新处理或保留原貌。
- 完成导出后确认已输出图片数量和需要重扫数量。

生产工人入口不要求理解 manifest、run plan、rule engine、P0/P1/P2 聚合、artifact readiness、validation index、evidence bundle 等工程或验收术语。

### 2.2 复核人员

- 按批次、严重级别和规则查看待复核问题。
- 预览原图或后端生成的预览/证据图。
- 记录复核状态和备注，导出复核模板或汇总。
- 当前阶段只看图和记录判断，不执行图片处理。

### 2.3 管理人员

- 查看项目级 run-plan 聚合进度。
- 查看批次通过率、P0/P1/P2 数量、预检错误、处理失败数量和吞吐指标。
- 下载或打开验收、移交、抽检、返工相关汇总文件。
- 使用现有静态聚合工作台维护后台/admin/acceptance 视角，不把它作为生产工人默认入口。

### 2.4 生产词汇

普通生产界面统一使用下列中文词汇：

| 中文词汇 | 面向用户的含义 | 后台字段或术语 |
| --- | --- | --- |
| 扫描图片文件夹 | 原始扫描图片输入目录 | input folder、source root |
| 处理后图片输出文件夹 | 处理副本输出目录 | output folder、process_out |
| 开始处理 | 启动检查和自动优化 | run plan、pipeline start |
| 正在检查 | 自动质量检查中 | preflight、scan QC |
| 正在优化图片 | 自动安全修图中 | retouch、processing |
| 待复核图片 | 需要人工判断的图片 | review queue item |
| 通过 | 当前图片可继续 | pass |
| 需要重扫 | 必须重新扫描 | rescan |
| 重新处理 | 重新生成处理后图片 | reprocess |
| 保留原貌 | 不去除档案原有痕迹 | keep original mark |
| 完成导出 | 结束批次并生成成果 | finish export |

## 3. 信息架构

信息架构分为两个入口：生产工作台和后台/管理员工作台。两者可以复用后端产物，但不能混成一个统计优先的普通入口。

### 3.1 生产工作台入口

生产工作台第一屏必须围绕任务设置和动作：

1. 批次设置
   - 扫描图片文件夹，首期默认递归处理 JPG；同时提供单个 JPG 文件质检入口。
   - 处理后图片输出文件夹。
   - 图像处理规则模板可由管理员预设，普通用户从“DA/T 31-2017 标准模板、纯文本高清晰度模板、原貌高保真模板、用户自定义模板”中选择必要选项。
   - 模板选择后展示简短用途说明和主要风险提示，例如“严格保留档案原貌”“尽量提高文字洁净度”“照片/绘画核心区域不处理”。
   - 处理项多选：去黑边、装订孔处理、纠偏、去除背景色；装订孔和强背景处理默认显示原貌保护提示。
   - 输出模式：默认“生成处理后图片”；如启用“受控覆盖发布”，必须先完成备份、dry-run、确认和可回滚日志。
   - 线程数量：默认自动，允许高级用户手动设置，显示 CPU/内存/磁盘风险提示。

2. 处理进度
   - 状态：未开始、已准备、正在检查、正在优化图片、等待复核、正在导出、已完成、已阻断。
   - 进度条：已处理/总数、自动优化数量、待复核数量、阻断数量。
   - 阻断原因用中文动作提示表达，例如“有 3 张需要重扫”。

3. 待复核图片
   - 原图和处理后图片对比。
   - 缺陷区域提示。
   - 动作按钮：通过、需要重扫、重新处理、保留原貌。

4. 完成导出
   - 已输出处理后图片数量。
   - 需要重扫数量。
   - 输出文件夹。
   - 面向管理员的摘要入口。

5. 扩展生产工具
   - 样例图片参数设置：去黑边范围、纠偏、背景色处理等参数由后端保存为处理方案。
   - TIF 转 JPG、JPG 生成双层 PDF、PDF 转 OFD、批量重命名、批量修改 DPI。
   - 按 Excel 表格进行案卷分件，并在目标目录不存在时由后端创建。
   - OCR 自动生成目录表并写入指定 Excel 列，默认提取文件编号和题名，低置信度结果进入人工复核。
   - 所有批量工具都必须展示 dry-run 预览、冲突/缺页/覆盖风险和可恢复状态。

### 3.2 后台/管理员工作台入口

当前静态 HTML 原型或本地服务静态页面属于后台/管理员工作台。页面结构如下：

1. 工作台总览
   - 项目编号、批次数、当前状态、最近运行时间。
   - 聚合指标：总文件数、通过批次数、失败批次数、P0/P1/P2、预检错误数。
   - 最近失败批次和失败阶段。

2. 流程驱动
   - 步骤：配置批次、预检、质检扫描、可选处理副本、复核导出、验收汇总、移交清单。
   - 每个步骤显示输入条件、产物、状态、可执行命令或后续 API 调用。
   - 对不可执行步骤给出阻断原因，例如缺少输入目录、预检未通过、报告不存在。

3. 批次列表
   - 展示 run-plan 或报告目录中的批次。
   - 字段：批次编号、状态、失败阶段、文件数、P0/P1/P2、报告目录、处理目录。
   - 支持按状态和严重级别筛选。

4. 批次详情
   - 批次摘要、manifest 一致性、质量指标、规则命中概览。
   - 问题列表字段：规则、级别、来源、置信度、消息、相对路径。
   - 打开现有 `scan_qc_report.html`，不复制报告渲染逻辑。

5. 图片预览
   - 只读取后端已产生或用户授权暴露的图片/预览图。
   - 支持单图查看、缩放、适配窗口、左右切换。
   - 可显示后端报告提供的缺陷元数据，但不在前端生成新缺陷。
   - 不提供裁边、旋转、滤镜、增强、保存等图片处理控件。

6. 复核与导出
   - 读取 review export 模板。
   - 允许编辑状态和备注，形成新的复核输入文件。
   - 调用现有 `review-summary` 能力生成聚合摘要。

该入口可以继续展示聚合指标、验证摘要、证据包状态、移交摘要、性能信息和公开安全报告；但必须在文案和导航上标注为后台/管理员/验收用途，避免被后续实现当作生产工人主入口。

## 4. 调用边界

前端与后端保持完全解耦。目标架构固定为标准后台服务对外提供接口，前端、验收工具和外部系统都通过同一类稳定契约调用。静态页面可以作为原型或后台只读入口保留，但生产工作台的长期实现必须迁移到服务接口。

### 4.1 第一阶段：静态工作台

适用于当前仓库状态和低风险原型验证。单文件 HTML 工作台可以通过用户选择或嵌入示例数据读取现有 JSON/CSV 产物：

- `run_plan_summary.json`
- `scan_qc_report.json`
- `preflight_report.json`
- `processing_manifest.json`
- `review_summary.json`
- `acceptance_summary.json`

浏览器安全限制下，静态页面不能直接遍历本地目录；因此第一阶段应优先支持手动加载 JSON/CSV 文件，或由后端/脚本生成带嵌入数据的 HTML。该阶段不代表最终架构，只用于验证信息组织、中文动作流和后台/验收视角。

### 4.2 第二阶段：标准后台服务

在计划确认后，建设标准后台服务。服务可以先在本机运行，但接口形态应按可复用后台服务设计，而不是只封装当前页面的临时本地助手。服务负责路径校验、任务创建、任务状态、队列调度、日志、权限、敏感数据过滤、预览资源生命周期和外部系统调用契约；前端只消费接口结果。服务必须按 job 隔离请求上下文，同一时间允许多个外部请求或前端会话提交任务，但每个任务的 job_id、batch_id、模板快照、输入授权、输出目录、metadata、临时文件、复核队列、checkpoint 和日志都必须独立。

建议第一批生产接口：

- `GET /api/production/session`
- `POST /api/production/setup`
- `POST /api/production/start`
- `POST /api/production/pause`
- `POST /api/production/resume`
- `GET /api/production/progress`
- `GET /api/production/review-queue`
- `POST /api/production/review-actions`
- `POST /api/production/finish-export`
- `GET /api/rule-templates`
- `GET /api/rule-templates/{template_id}`
- `POST /api/rule-templates`
- `PUT /api/rule-templates/{template_id}`
- `POST /api/rule-templates/{template_id}/dry-run`
- `POST /api/preflight`
- `POST /api/scan`
- `POST /api/run-plan`
- `GET /api/projects/{project_id}/summary`
- `GET /api/batches/{batch_id}/report`
- `GET /api/batches/{batch_id}/preview?relative_path=...`
- `POST /api/review-summary`
- `POST /api/processing-parameters/from-sample`
- `POST /api/documents/tif-to-jpg`
- `POST /api/documents/pdf`
- `POST /api/documents/ofd-conversion`
- `POST /api/files/rename-plan`
- `POST /api/files/rename-apply`
- `POST /api/files/dpi-plan`
- `POST /api/files/dpi-apply`
- `POST /api/fonds/split-plan`
- `POST /api/fonds/split-apply`
- `POST /api/catalog/ocr-extract`
- `POST /api/catalog/export`

后续对外接口应补充：

- 批次任务创建、取消、暂停、恢复、重试和幂等提交。
- 按任务 ID 查询阶段状态、进度事件、阻断原因和聚合性能。
- 按授权令牌读取原图/处理后图预览，不暴露真实本地路径给非本机调用方。
- 查询 job 级资源配额和排队状态，包括 requested_workers、effective_workers、全局并发上限、当前限流原因和磁盘空间阻断原因。
- 提交复核动作、读取复核历史、生成验收摘要和移交摘要。
- 管理图像处理规则模板，支持内置模板查询、用户自定义模板保存、参数校验、样例图片 dry-run、模板版本和审批状态。
- 提交样例参数、TIF 转 JPG、JPG 生成双层 PDF、PDF/OFD 转换、批量重命名、DPI 修改、分件拷贝和 OCR 目录提取任务；所有写操作都应支持 dry-run、冲突检查、执行确认、任务恢复和回滚/补救清单。
- 输出 OpenAPI/接口文档，声明字段版本、错误码、隐私边界和外部系统调用限制。

## 5. 数据契约

生产工作台使用最小本地契约；后台/管理员工作台继续直接复用现有报告字段。前端展示字段以可选读取为原则，缺失时显示空状态。

所有生产数据契约必须包含后端分配的 job_id 或 task_id。前端可以在当前会话中缓存 job_id，用于查询进度、预览和提交复核动作；不得用输入目录、输出目录、文件名或模板名称推断任务身份。多个任务同时存在时，前端列表、轮询、预览 URL 和复核提交必须按 job_id 分组。

### 5.1 生产批次设置状态

```json
{
  "schema_version": "scan-qc.production-workbench.v1",
  "job_id": "job-20260609-0001",
  "batch_id": "batch-20260512-001",
  "input_mode": "folder|single_file",
  "input_folder": "/local/private/source",
  "input_file": "/local/private/source/page-0001.jpg",
  "output_folder": "/local/private/output",
  "metadata_folder": "/local/private/output/_production_workbench",
  "template_snapshot_id": "tpl-snap-20260609-0001",
  "rule_template_id": "dat-31-2017-standard",
  "rule_template_name": "DA/T 31-2017 标准模板",
  "selected_processing": ["trim_dark_border", "binding_hole_review", "deskew", "background_balance"],
  "output_mode": "copy_only|controlled_publish",
  "thread_count": "auto|1|2|4|8",
  "requested_workers": "auto|1|2|4|8",
  "effective_workers": 4,
  "login_required": false,
  "status": "not_started|ready|running|paused|needs_review|exporting|finished|blocked",
  "operator_id": "local-user",
  "created_at": "2026-05-12T09:00:00+08:00"
}
```

### 5.2 生产进度事件

```json
{
  "event_id": "evt-000001",
  "job_id": "job-20260609-0001",
  "batch_id": "batch-20260512-001",
  "stage": "checking|optimizing|review_waiting|exporting|finished|blocked",
  "message_zh": "正在优化图片",
  "processed_count": 120,
  "total_count": 300,
  "auto_fixed_count": 42,
  "review_required_count": 3,
  "blocked_count": 0,
  "occurred_at": "2026-05-12T09:03:00+08:00"
}
```

### 5.3 生产复核队列项

```json
{
  "review_item_id": "rev-0001",
  "job_id": "job-20260609-0001",
  "batch_id": "batch-20260512-001",
  "display_name": "第 0012 张",
  "severity": "needs_rescan|needs_decision|warning",
  "reason_zh": "疑似裁到正文，需要人工确认",
  "original_preview_url": "local-preview://original/rev-0001",
  "processed_preview_url": "local-preview://processed/rev-0001",
  "evidence_regions": [
    {"x": 120, "y": 80, "width": 300, "height": 60, "label_zh": "疑似裁切"}
  ],
  "suggested_action": "pass|rescan|reprocess|keep_original_mark",
  "allowed_actions": ["pass", "rescan", "reprocess", "keep_original_mark"]
}
```

### 5.4 操作员动作

```json
{
  "action_id": "act-0001",
  "job_id": "job-20260609-0001",
  "review_item_id": "rev-0001",
  "action": "pass|rescan|reprocess|keep_original_mark|pause_batch|resume_batch|finish_export",
  "note_zh": "保留装订孔，属于档案原貌",
  "operator_id": "local-user",
  "created_at": "2026-05-12T09:05:00+08:00"
}
```

### 5.5 生产输出摘要

```json
{
  "job_id": "job-20260609-0001",
  "batch_id": "batch-20260512-001",
  "status": "finished|blocked",
  "input_count": 300,
  "processed_output_count": 297,
  "rescan_required_count": 3,
  "auto_fixed_count": 80,
  "manual_review_count": 6,
  "output_folder": "/local/private/output",
  "public_summary_path": "workbench_public_summary.json",
  "finished_at": "2026-05-12T09:30:00+08:00"
}
```

### 5.6 后台项目聚合

来源：`run_plan_summary.json`

- `schema_version`
- `project_id`
- `summary.total_batches`
- `summary.passed_batches`
- `summary.failed_batches`
- `summary.p0_findings`
- `summary.p1_findings`
- `summary.p2_findings`
- `summary.preflight_error_count`
- `batches[].batch_id`
- `batches[].status`
- `batches[].failure_stage`
- `batches[].failure_reason`
- `batches[].report_dir`
- `batches[].process_out`

### 5.7 后台批次报告

来源：`scan_qc_report.json`

- `project.project_id`
- `manifest.batch_id`
- `summary.total_files`
- `summary.openable_files`
- `summary.total_findings`
- `summary.p0_findings`
- `summary.p1_findings`
- `summary.p2_findings`
- `summary.performance`
- `files[]`
- `findings[]`
- `rule_catalog`

### 5.8 预览资源

图片预览必须由后端或本地受控目录提供可访问 URL。前端不得猜测系统绝对路径，不在聚合报告中展示私有路径。推荐后续服务端返回：

```json
{
  "job_id": "job-20260609-0001",
  "batch_id": "batch-001",
  "relative_path": "A001_0001.jpg",
  "preview_url": "/api/batches/batch-001/preview?token=...",
  "source": "original",
  "width": 1600,
  "height": 2400
}
```

### 5.9 批量工具任务

批量重命名、DPI 修改、TIF/JPG/PDF/OFD 转换、按 Excel 分件、OCR 目录提取等扩展工具统一走后台任务模型。前端只展示计划、风险、进度和结果，不直接改文件。

```json
{
  "schema_version": "scan-qc.batch-tool-task.v1",
  "task_id": "tool-20260602-001",
  "task_type": "rename|dpi_update|tif_to_jpg|pdf_generate|ofd_convert|fonds_split|ocr_catalog",
  "status": "planned|running|paused|finished|blocked|failed",
  "dry_run": true,
  "input_count": 100,
  "planned_output_count": 100,
  "conflict_count": 0,
  "manual_review_count": 8,
  "log_export_formats": ["xlsx", "csv", "json"],
  "overwrite_mode": "copy_only|controlled_publish",
  "can_apply": true,
  "resume_supported": true
}
```

### 5.10 图像处理规则模板

规则模板由后台服务维护，前端负责展示、选择、编辑和提交 dry-run。内置模板只允许查看和克隆，用户自定义模板允许编辑、版本化和停用。模板参数最终由后台服务解释并传递给图像处理 Worker。

默认模板：

- `dat-31-2017-standard`：严格按照 DA/T 31-2017 和项目验收规则处理，原貌保护优先。
- `text-clean-print`：面向确认无照片、绘画、印章密集页的纯文本扫描件，尽量提高洁净度和文字清晰度，接近干净打印效果；后台可关闭去污点前的照片/混合内容保护判断。
- `high-fidelity-original`：面向照片、绘画、珍贵档案等，核心区域尽量不处理，只处理边框外或指定区域。
- `custom`：用户自定义模板，必须通过参数校验和样例 dry-run 后才能用于正式批次。

```json
{
  "schema_version": "scan-qc.rule-template.v1",
  "template_id": "text-clean-print",
  "name_zh": "纯文本高清晰度模板",
  "type": "built_in|custom",
  "version": "2026.1",
  "status": "active|draft|disabled",
  "description_zh": "适用于纯文本扫描件，尽量提升洁净度和文字清晰度。",
  "processing_parameters": {
    "crop_policy": "standard_margin|aggressive_text|edge_only",
    "deskew_strength": "conservative|standard|strong",
    "background_cleanup": "off|conservative|strong",
    "despeckle_strength": "off|conservative|strong",
    "text_enhancement": "off|conservative|strong",
    "core_region_protection": "standard|strict|edge_only"
  },
  "review_policy": {
    "manual_review_threshold": "standard|low|high",
    "protect_original_marks": true,
    "require_sample_dry_run": true
  }
}
```

## 6. 页面设计原则

- 生产工作台应偏操作型，信息密度适中，避免营销式首页和统计驾驶舱首页。
- 生产入口以文件夹选择、处理状态、待复核图片和动作按钮为主。
- 后台/管理员入口以流程状态、批次表格、聚合摘要和报告预览为主。
- 严重级别使用稳定颜色：P0 红、P1 橙、P2 蓝、通过绿、未运行灰。
- 所有关键动作显示输入、输出和阻断条件。
- 图片预览区必须有明确空状态、加载失败状态和敏感数据提示。
- 表格字段可横向滚动，不能压缩到文字重叠。
- 单文件 HTML 原型使用原生 HTML/CSS/JavaScript，后续确认后再决定是否引入前端构建工具。
- 普通生产界面默认不展示私有绝对路径、真实文件名、哈希、OCR 文本、行级规则对象或完整证据包；本地授权图片预览只在受控服务中显示。

## 7. 实施计划

### 7.1 阶段 A：设计确认

产物：

- 本文档。
- 一张低保真页面结构草案，可用文档说明或静态 HTML 原型表达。

验收：

- 确认前端只做流程驱动和预览。
- 确认首版采用单文件 HTML，还是进入轻量本地服务。
- 确认图片预览的数据来源和敏感数据边界。

### 7.2 阶段 B：单文件工作台原型

产物：

- `docs/frontend-workbench-prototype.html` 或同等单文件 HTML。
- 内联 CSS 和原生 JavaScript。
- 支持手动加载 `run_plan_summary.json` 和 `scan_qc_report.json`。
- 支持展示总览、流程步骤、批次列表、问题列表和图片预览占位。

验收：

- 直接在浏览器打开即可使用。
- 不依赖后端运行服务。
- 不执行图片处理。
- 能在无数据、缺字段、错误 JSON 时给出清晰状态。
- 页面文案明确这是后台/管理员/验收聚合入口，不是生产工人主入口。

### 7.3 阶段 C：本地服务方案评估

产物：

- API 设计文档。
- CLI 调用封装方案。
- 路径白名单、进程状态、日志与权限设计。
- 生产工作台契约实现方案，覆盖批次设置、进度事件、复核队列、操作员动作和输出摘要。

验收：

- 不改核心扫描和处理模块。
- 所有功能都能追溯到现有 CLI 或报告产物。
- 敏感路径和图片访问有服务端控制。
- 普通生产入口不以统计卡片、验证索引或证据包状态作为主流程。

### 7.4 阶段 D：可用工作台

产物：

- 浏览器生产工作台。
- 后台/管理员聚合工作台。
- 本地服务或生成式 HTML 报告入口。
- 复核导入导出流程。

验收：

- 操作员可以从配置到报告完成闭环。
- 复核人员可以预览图片、筛选问题、导出复核结果。
- 管理人员可以查看项目聚合状态。
- 普通生产入口通过“选择文件夹、开始处理、处理待复核图片、完成导出”完成闭环。

## 8. 风险与待确认事项

- 浏览器直接访问本地文件受安全限制，若需要自动读取目录和图片，必须由本地服务提供受控接口。
- 私有图片和行级报告包含敏感信息，前端不能默认把它们嵌入可外传 HTML。
- 如果后续需要多人协作、权限、任务队列和进程控制，单文件 HTML 不足以承担，需要进入第二阶段本地服务。
- 图片预览 URL、缩略图生成策略和证据图存储策略需要在实现前确认。
- 当前阶段不建议引入 React/Vue/Next 等框架；只有在确认需要复杂交互、多人状态同步或组件库后再评估。
- 最大产品风险是把现有聚合摘要界面继续扩展成普通生产入口，导致项目回到统计优先 dashboard；后续实现必须用本文件和 `archive-scan-qc-retouch-design.md` 的生产契约校验。

## 9. 后续实现 PR 对齐检查清单

- 第一屏是否要求选择“扫描图片文件夹”和“处理后图片输出文件夹”。
- 主按钮是否是“开始处理”，并能进入进度状态。
- 普通用户是否只看到少量“待复核图片”，而不是全量统计和规则对象。
- 复核动作是否包含通过、需要重扫、重新处理、保留原貌。
- 完成页是否强调已输出数量、需要重扫数量和输出文件夹。
- 当前静态聚合工作台是否被标注为后台/管理员/验收入口。
- 是否没有把验证摘要、证据包、性能统计或移交摘要放入普通生产入口第一屏。
- 是否所有前端状态、轮询、预览 URL 和复核动作都绑定 job_id，多个任务同时存在时不会串用输出目录、模板参数或复核队列。
- 是否 Windows 中文路径和真实文件名只在本地受控授权范围内展示，未进入可外传摘要或浏览器持久化状态。
- 公开材料是否不含私有路径、真实文件名、哈希、缩略图、OCR 文本或行级证据。
- 是否没有新增统计优先 dashboard 面板作为生产工作台主体验收内容。

## 10. 本轮结论

建议保留阶段 B 的单文件后台/管理员聚合工作台，同时把普通生产入口另行按 AI4-148 的本地生产契约推进。该路径既保留现有公开安全摘要的价值，又避免把普通扫描处理工人的主流程误导成统计优先 dashboard。
