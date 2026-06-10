# Development Issues

日期：2026-06-10

本文档把 `docs/development-plan.md` 和
`docs/image-quality-processing-roadmap.md` 中的后续工作拆成可执行 issue。
优先级遵循当前工程重点：服务化 job 边界、路径隔离、状态恢复、
public-safe 摘要和可量化图像质量验收，暂不继续无边界增加修图开关。

## 状态标记

- `done`：当前分支已有实现和回归覆盖。
- `in-progress`：已部分实现，但仍有明确缺口。
- `next`：建议下一批优先启动。
- `later`：排在服务边界和质量门禁之后。
- `deferred`：暂缓，除非生产阻塞或用户明确要求。

## P0：服务和生产边界

### DEV-001 CLI 稳定批量服务版

状态：`in-progress`

范围：

- 固定 `production-run`、`run-plan`、`preflight`、`capability-probe` 和
  `public-capability-contract` 的公开命令契约。
- 固定状态文件、退出码、schema_version、敏感 artifact 和 public-safe
  aggregate artifact 分类。
- 保持 CLI 作为服务化完成前的离线兜底入口。

验收：

- `production-cli` CI 分组通过。
- public capability contract 能生成 public-safe JSON。
- release checklist 覆盖安装、运行、失败、恢复和隐私边界。

### DEV-002 production-run 终态、停滞识别和断点复跑

状态：`in-progress`

范围：

- 非预期异常、中断、缺失 terminal summary、stale running 都必须恢复为明确状态。
- 重试复用已完成派生图、retry manifest 和 checkpoint，不修改源文件。
- public summary 只能公开聚合 retry/reuse 计数和可恢复状态。

验收：

- 失败、interrupted、needs_recovery、retryable 和 non-retryable 状态均有测试。
- 输入文件处理前后 hash 一致。
- `GET /api/production/finish-export` 给出 public-safe blocking_codes。

### DEV-003 Windows 中文路径和源文件只读安全

状态：`in-progress`

范围：

- 覆盖中文目录、中文文件名、空格路径和长路径。
- API 和 CLI 都不得把私有路径、文件名、hash、OCR 文本、缩略图或图片内容写入 public-safe 摘要。
- 异常、中断、重试、恢复和取消路径都不得修改输入目录。

验收：

- Windows 下跑 CLI/API smoke。
- 服务 API 回归包含中文/空格路径和 source hash unchanged。
- 隐私边界测试序列化响应后找不到私有路径片段。

当前进展：

- 服务 API 已覆盖中文/空格路径、source hash unchanged 和 public response 脱敏。
- CLI `production-run` 已覆盖中文/空格路径下处理成功、source hash unchanged、
  `source_images_modified=false` 和派生图写入独立输出目录。

### DEV-004 CLI 并发隔离

状态：`done`

范围：

- 多个外部 `production-run` 进程使用不同 metadata/output 目录时互不污染。
- 同一输出目录冲突时明确拒绝或进入可解释 resume 模式。
- 保证 template snapshot、review queue、processing manifest、retry manifest 和 logs 不混写。

验收：

- 至少两个并发 CLI job 同时运行。
- 输出目录冲突测试返回明确错误或恢复策略。
- 两个 job 的 public/private artifacts 均归属正确目录。

当前进展：

- `production-run` 已拒绝 metadata 和 derivatives 指向同一目录。
- `production-run` 已在 metadata 和 derivatives 目录中创建独占
  `.archive_scan_qc_production_run.lock`，同一输出目录被另一个运行占用时
  直接拒绝，并保持既有 progress/summary 不被覆盖。
- `run-plan` 已在加载计划时拒绝任意两个批次复用同一个 `report_dir` 或
  `process_out`，也拒绝一个批次的 report 目录撞上另一个批次的 process
  目录。
- 已增加两个真实 `production-run` CLI 进程使用不同 metadata/derivatives
  目录并发运行的回归，验证 summary、manifest 和 lock 文件互不污染。

### DEV-005 后台服务 MVP

状态：`done`

范围：

- `GET /api/health`
- `GET /api/capabilities`
- `POST /api/jobs`
- `GET /api/jobs`
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/run`
- `POST /api/jobs/{job_id}/start`
- `POST /api/jobs/{job_id}/retry`
- `POST /api/jobs/{job_id}/cancel`
- production facade endpoints

已完成证据：

- service core、HTTP transport、production session、finish-export、review actions
  和 review history 已有回归。
- `GET /api/capabilities` 发布 resource limits、schema 列表和
  `public_boundaries.service_quality`。

### DEV-006 服务 job 级隔离和资源配额

状态：`done`

范围：

- job_id、template snapshot、metadata、derivatives、tmp、checkpoints、review、
  logs 和 service_job.json 隔离。
- 限制 max active async jobs、max active workers、per-job workers、free space 和
  per-job tmp quota。

已完成证据：

- service job tests 覆盖目录隔离、资源配额、并发 async、取消竞态、日志和恢复索引。
- public summaries 只公开聚合状态、计数、边界布尔和 allowlisted codes。

### DEV-007 public-safe 服务质量边界

状态：`done`

范围：

- job-level summary 可以公开 `print_clean` profile 和 whitelisted aggregate
  quality_metrics。
- production session 和 root index quality aggregate 只能公开状态计数、
  quality-signal 计数、aggregate file counts 和 blocking-code counts。
- public capability contract 和 service capabilities 都要机器可读表达该边界。

已完成证据：

- `public_capability_contract.json` 包含 `service_contract.public_surfaces`。
- `GET /api/capabilities` 包含 `public_boundaries.service_quality`。
- service API、HTTP 和 recovery/index 回归覆盖 `print-clean-v1`。

## P1：模板和图像质量

### DEV-101 规则模板 schema 和内置模板

状态：`done`

范围：

- 内置 `archival-safe-v1`、`text-clean-readable-v1`、`print-clean-v1`、
  `photo-mixed-safe-v1`，兼容 legacy IDs。
- 输出 template ID、processing profile、参数摘要和风险代码。

### DEV-102 模板 dry-run 和参数映射

状态：`done`

范围：

- `rule-template-catalog`
- `rule-template-dry-run`
- 服务端模板 catalog/detail/validate/save
- dry-run 不写派生图，不泄露本地私有行级证据。

### DEV-103 processing_quality_summary public-safe schema

状态：`done`

范围：

- 公开 aggregate counts、quality signal、allowlisted metric summary、guardrail
  status 和 privacy flags。
- 不公开 file lists、paths、hashes、OCR 文本、缩略图、图片内容或行级记录。

### DEV-104 synthetic quality fixture 和 capability smoke

状态：`done`

范围：

- 灰底、泛黄、边缘/角落/折痕阴影、扫描黑边、gutter、扫描线、透印、
  低对比文字、褪色文字、模糊文字和混合内容保护。
- smoke gate 要求关键操作 count 和 public-safe 质量指标非零/达标。

### DEV-105 text-clean 背景归一和文字增强

状态：`done`

范围：

- text-clean 背景估计、浅纸低对比正文增强、褪色文字增强、低饱和文字增强。
- print-clean profile 比 standard 更强，但仍受颜色、纹理、前景密度和组合变化 guardrail 约束。

### DEV-106 阴影、扫描线和透印处理

状态：`done`

范围：

- 边缘阴影、角落阴影、折痕阴影、低幅照明梯度、扫描线和保守透印弱化。
- public-safe quality summary 输出对应 aggregate delta 和 changed-pixel evidence。

### DEV-107 photo-mixed-safe 原貌保护

状态：`done`

范围：

- 混合照片、印章、表格和彩色内容保护。
- `photo-mixed-safe-v1` 关闭强背景清理、透印清理、褪色文字增强和文字边缘锐化，同时仍写 public-safe quality summary。

### DEV-108 本地 before/after review package 分组

状态：`done`

范围：

- 本地 review package 按背景清理、可读性提升、缺陷清理和原貌风险分组。
- 不嵌入图片字节；预览只能通过 local-only preview endpoint 授权读取。

当前进展：

- `finish-export` 已接入 public-safe review gate：当本地 review queue 有待复核
  条目但没有通过聚合验证的完整操作员复核结论时，返回
  `operator_review_required`、`operator_review_incomplete`、
  `operator_review_invalid` 或 `operator_review_not_closed` blocking code；
  响应只公开复核项数量、最新验证/完成状态和是否允许交付，不公开 local ID、
  路径、文件名或行级决策。
- 服务 API 已提供 local-only 单条复核项读取：
  `GET /api/jobs/{job_id}/local-review-item/{local_id}` 和
  `GET /api/production/review-item?job_id=...&local_id=...`。该响应用于本机
  工作台逐项复核，可返回 `local_id`、相对路径、建议动作、允许动作和操作员
  提示；它是 sensitive/local-only，不进入 public-safe 摘要。
- 生产工作台原型已生成 service facade 的 review-item 和 preview URL，并在准备
  下一批时清空 `job_id`，避免复用上一批 local-only URL。完整前端 API client
  改造继续归入 DEV-301。

### DEV-109 私有样本聚合验证

状态：`done`

范围：

- `private-validation-aggregate` 只公开 public group ID、aggregate item counts、
  allowlisted metric summaries、quality-signal status counts 和 risk codes。
- 不提交样本路径、文件名、hash、标签、OCR 文本、缩略图、图片内容或行级记录。

已完成证据：

- `private-validation-aggregate` 已有固定合成聚合 fixture：
  `docs/fixtures/private-validation-aggregate/`，模拟 operator-approved private
  validation 的分组结果，但只提交 public group ID、聚合计数、allowlisted
  metric summaries、quality-signal 状态和 risk codes。
- 回归通过 CLI 从 fixture 生成 `private_validation_aggregate_summary.json`，
  并验证输出不包含真实路径、文件名、hash、OCR、缩略图或图片内容。

## P2：CI、发布和性能

### DEV-201 CI 四组稳定回归

状态：`done`

范围：

- `core-image-processing`
- `production-cli`
- `privacy-boundary`
- `external-validation`
- deep-full-only 保留长耗时大回归。

已完成证据：

- `scripts/ci_regression_groups.py verify-coverage` 通过。
- `test_scan_processing_algorithm_regression` 被保留在 deep-full-only，不进入常规 CI。

### DEV-202 release checklist 图像质量门槛

状态：`done`

范围：

- 质量收益、过处理风险、源文件安全、恢复、public-safe 摘要和 private validation
  聚合检查。

### DEV-203 性能和后端热点迁移

状态：`later`

范围：

- 已验证同语义热点迁移到 NumPy/OpenCV。
- libvips/tile-based 大图 IO。
- worker 推荐进入服务调度策略。
- 保留 CPU/Pillow fallback，直到可选后端通过真实样本聚合验证。

启动条件：

- 当前质量提升路径和服务 job 边界保持稳定。
- private validation 聚合结果支持默认值或后端变化。

## P3：前端和批量扩展工具

### DEV-301 生产工作台改为 API client

状态：`next`

范围：

- 批次设置、模板选择、进度、复核、预览和 finish-export 全部走服务 API。
- 前端不得直接执行 CLI、处理本地文件、猜测绝对路径或写 public-safe 摘要。

### DEV-401 批量重命名 plan/apply

状态：`later`

范围：

- dry-run、冲突检测、apply、rollback manifest、JSON/CSV/Excel 日志。

### DEV-402 Excel 案卷分件 plan/apply

状态：`later`

范围：

- 按件名、起始页、终止页复制到指定目录，记录聚合结果和失败原因。

### DEV-403 双层 PDF/OCR 目录方案

状态：`later`

范围：

- 调研 JPG 转双层 PDF、PDF/OFD 转换、OCR 目录提取。
- OCR 低置信度必须人工复核，不作为当前服务边界默认能力。

## Deferred

- 原图原地修改。
- 云端上传或网络修图。
- 生成式图像修复。
- 未经模板和复核的自动强清洁。
- 直接把 GPU/model 后端暴露成稳定 public CLI/API 契约。
