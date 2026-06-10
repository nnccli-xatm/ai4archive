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

状态：`done`

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

已完成证据：

- `production-cli`、`privacy-boundary` 和 `external-validation` 分组在当前分支通过。
- `public-capability-contract` CLI 和机器可读 contract 覆盖稳定 CLI、artifact
  schema、public-safe aggregate artifact 与服务边界。
- release checklist 已覆盖 CLI、服务、失败/恢复、源图只读和隐私边界。

### DEV-002 production-run 终态、停滞识别和断点复跑

状态：`done`

范围：

- 非预期异常、中断、缺失 terminal summary、stale running 都必须恢复为明确状态。
- 重试复用已完成派生图、retry manifest 和 checkpoint，不修改源文件。
- public summary 只能公开聚合 retry/reuse 计数和可恢复状态。

验收：

- 失败、interrupted、needs_recovery、retryable 和 non-retryable 状态均有测试。
- 输入文件处理前后 hash 一致。
- `GET /api/production/finish-export` 给出 public-safe blocking_codes。

已完成证据：

- `production-run` 失败和 KeyboardInterrupt 中断均写 terminal progress/summary，
  且 `source_images_modified=false`。
- service job recovery 覆盖 stale running、无 progress running、缺失 terminal
  summary 的 `needs_recovery`，以及 retryable/non-retryable 状态。
- `finish-export` 对 non-terminal、cancelled、needs_recovery 和 review-gate block
  均返回 public-safe blocking codes。

### DEV-003 Windows 中文路径和源文件只读安全

状态：`done`

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
- 异步状态 JSON 写入已对 Windows transient file lock 做短重试，测试等待后台
  worker 收尾后再清理临时目录。
- 异步 start/recover 回归已等待 worker 注销后再退出临时目录，避免 CI 输出目录
  清理竞态下的后台线程异常栈。

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

## P1b：OCR 预处理质量专项

专项计划见 `docs/ocr-preprocessing-image-quality-plan.md`。本组任务目标是让扫描图片输出对 OCR 有显著质量改善，不能再以“稳定运行但质量不提升”作为完成口径。

### DEV-151 OCR 预处理 profile 和验收 harness

状态：`done`

范围：

- 新增 `ocr-preprocess-v1` 和 `ocr-preprocess-light-v1` 模板/输出 profile。
- 扩展 NoisyOffice runner，支持 profile 对比和质量阈值失败退出。
- 增加 `ocr_preprocessing_quality_summary.json` public-safe 聚合摘要。
- 定义完成门槛：NoisyOffice 灰度增强 PSNR +1.0 dB、SSIM +0.015、MSE -10%；
  不达标不得标记完成。

已完成证据：

- `ocr-preprocess-v1`、`ocr-preprocess-light-v1` 已进入内置模板和输出 profile。
- `run_noisyoffice_external_cli_test.py` 支持 OCR 模板、阈值失败退出和
  `ocr_preprocessing_quality_summary.json` public-safe 聚合摘要。
- 2026-06-10 final NoisyOffice：216 张 stable pass，PSNR +6.7035 dB，
  SSIM +0.044149，MSE -64.217%，MAE -63.652%，四个噪声组全部正向，
  暗像素 F1 和前景保留率不下降。

### DEV-152 OCR 背景归一和光照校正

状态：`done`

范围：

- 实现 tile-based 背景估计、光照场校正、灰底/阴影/泛黄背景归一。
- 前景 mask、表格线、印章/批注和彩色区域保护必须参与回退。
- NoisyOffice 至少产生宏平均正向改善，且每个噪声分组不得整体变差。

已完成证据：

- OCR strong profile 对背景和中灰噪声做大幅提亮，对暗前景及相邻中灰笔画边缘做保护。
- `processing_manifest.json`、`processing_quality_summary.json` 和 service job public summary
  均记录 `ocr_background_delta`、`ocr_preprocess_changed_pixel_ratio`、
  `ocr_foreground_retention_ratio` 等聚合指标。

### DEV-153 OCR 专用去噪和笔画保护

状态：`done`

范围：

- 从孤立 speckle 扩展到椒盐噪声、轻纹理噪声和扫描噪声。
- 引入 connected component、stroke-width 和前景保留 guardrail，防止标点、小字和细笔画被误删。
- NoisyOffice 至少 70% 样本 PSNR 或 SSIM 正向改善，前景保留率不下降超过门槛。

已完成证据：

- OCR profile 不再默认使用通用 `despeckle` 删除小暗组件，避免误删标点、小字和细笔画。
- 灰度增强阶段保留暗前景和暗前景邻域的中灰笔画边缘；guardrail 要求前景保留率不低于门槛。
- final NoisyOffice gate 中暗像素 F1 delta = 0.0，前景保留率 delta = 0.0。

### DEV-154 自适应阈值和二值 OCR 输出

状态：`done`

范围：

- 输出 `ocr_gray_enhanced` 与 `ocr_binary` 两类 OCR 派生图。
- 实现 Sauvola/Wolf/Niblack 或 OpenCV adaptive threshold 路线。
- 用 DIBCO/H-DIBCO 或等价二值化指标验收二值输出，不用灰度 PSNR 单独判断。

已完成证据：

- `ocr-preprocess-v1` 输出灰度增强图和 `ocr_binary/` sidecar；manifest 记录
  `ocr_binary_output_relative_path`、阈值、前景比例和前景保留率。
- Synthetic OCR validation 同时评估灰度和二值输出，gate 使用每页更优 OCR 利用副本。
- DIBCO/H-DIBCO 尚未作为 CI 默认依赖；当前发布门槛使用 NoisyOffice 灰度质量 gate
  加 synthetic Tesseract OCR gate，后续可追加真实 DIBCO release gate。

### DEV-155 OCR 实测和真实样本聚合验收

状态：`done`

范围：

- 增加本机 OCR provider probe，默认不调用外部 OCR。
- synthetic known-text fixture 计算 CER/WER，目标 CER 相对下降至少 25%。
- 私有样本只提交聚合指标和风险代码；OCR 文本、目录行、路径、文件名、hash、
  缩略图、图片内容和行级候选必须保持 local-only。

已完成证据：

- 新增 `archive-scan-qc ocr-provider-probe`，默认 provider disabled，不运行 OCR。
- 新增 `archive-scan-qc ocr-preprocessing-ocr-validation`，显式选择本机 Tesseract 时
  才计算 synthetic known-text CER/WER。
- 2026-06-10 本机 Tesseract gate：source CER/WER macro = 1.0，处理后 CER/WER macro = 0.0，
  CER/WER 相对下降 100%，public-safe 摘要不包含 OCR 文本或行级记录。
- `private_validation_aggregate_summary.json` allowlist 已加入 source/processed CER、WER
  和相对下降等聚合指标。

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

状态：`done`

范围：

- 已验证同语义热点迁移到 NumPy/OpenCV。
- libvips/tile-based 大图 IO。
- worker 推荐进入服务调度策略。
- 保留 CPU/Pillow fallback，直到可选后端通过真实样本聚合验证。

已完成证据：

- `despeckle` 已有保守 `fallback`、可选 `numpy` 和可选 `opencv` 后端；回归覆盖
  同语义候选点、替换结果、backend fallback 和 public-safe backend timing 汇总。
- `image_io_backend='vips'` 已作为显式可选 IO 后端保留在能力边界内，默认仍走
  Pillow/fallback；public capability contract 将 OpenCV/libvips 标为可选 backend，
  不提升为稳定默认能力。
- `image-processing-capability-smoke`、synthetic performance comparison 和 private
  validation 聚合链路会汇总 `operation_timings`、backend mode/counts 和质量证据，
  用于判断是否可以调整默认值。
- service job 在 `workers` 省略时会按输入图片数量、CPU/内存启发式计算
  `workers_scheduled`，写入私有 job record 和 public-safe resource summary；
  异步启动按该 scheduled worker 数扣减 active worker 配额，不再把自动调度 job
  一律按最大 worker 占用处理。
- 显式 `workers` 请求仍优先生效；CPU/Pillow fallback 仍是默认稳定路径，真实样本
  聚合验证前不把可选 backend 设为默认。

## P3：前端和批量扩展工具

### DEV-301 生产工作台改为 API client

状态：`done`

范围：

- 批次设置、模板选择、进度、复核、预览和 finish-export 全部走服务 API。
- 前端不得直接执行 CLI、处理本地文件、猜测绝对路径或写 public-safe 摘要。

当前进展：

- 生产工作台原型已拆出 `productionApiRoutes`、`localBridgeRoutes`、`apiPath`、
  `productionApiPath` 和 `localBridgePath`，review item、preview 以及现有
  local bridge 调用均通过 client route helper 生成，不再在调用点散落硬编码路径。
- 本地 loopback workbench 已提供 `/api/production/review-queue`、
  `/api/production/review-item` 和 `/api/production/preview` 兼容路由，
  使用当前 local job id 校验批次边界，并以 local-only header 返回预览图片，
  避免 route client 改造后回退到旧
  `/api/preview/` 调用。
- 本地 loopback workbench 已提供 `/api/production/review-actions` 和
  `/api/production/finish-export` 兼容路由；前端自动保存和完成导出在有
  `job_id` 时走 production facade，两步流为先保存 review actions，再执行
  finish-export，旧 local bridge 仅作为无 job id 回退。
- 本地 loopback workbench 已提供 `/api/production/progress` 兼容路由；前端轮询
  在已有 `job_id` 后走 production progress，初始恢复才回退旧 `/api/status`。
- 本地 loopback workbench 已提供 `/api/production/setup` 和
  `/api/production/start` 兼容路由；前端保存文件夹走 production setup，
  启动处理在已有 `job_id` 后走 production start。
- 本地 loopback workbench 已提供 `/api/production/session` 兼容路由；前端初始
  状态恢复走 production session，不再直接轮询旧 `/api/status`。
- 前端守护测试已覆盖 production facade route helper、local bridge route helper
  和 local-only review/preview URL 构造；旧 local bridge 保留给系统选文件夹、
  打开输出文件夹、重置批次等本机外壳动作，不再承担核心生产 job 状态流。

已完成证据：

- 前端核心生产流 setup/start/session/progress/review-queue/review-item/preview/
  review-actions/finish-export 均通过 `productionApiPath` route helper 访问
  production facade。
- 本地 loopback workbench 为上述 production facade 提供兼容路由，并通过
  local job id 校验批次边界；local-only 队列和预览响应仍显式标记非 public-safe。
- `production-cli` 分组覆盖 workbench 静态验证、本地 HTTP facade、自动保存、
  完成交接、预览和服务 API 回归。

### DEV-401 批量重命名 plan/apply

状态：`done`

范围：

- dry-run、冲突检测、apply、rollback manifest、JSON/CSV/Excel 日志。

已完成证据：

- 新增 `archive-scan-qc batch-rename-plan`：读取本地 mapping CSV，生成
  `batch_rename_plan.json`、`.csv` 和 `.xlsx` dry-run 日志。
- plan 阶段检测空路径、绝对/越界路径、重复 source、重复 target、source missing、
  target exists 和 source/target 相同；blocked plan 以非零退出，不改动文件。
- 新增 `archive-scan-qc batch-rename-apply`：只接受 ready plan，执行本地 rename，
  写入 `batch_rename_apply.json`、`.csv`、`.xlsx` 和
  `batch_rename_rollback.json`。
- 批量重命名 manifest 明确标记 `local_only=true`、`public_safe=false`；它是
  path-bearing 本地生产工具，不进入 public-safe 交付摘要。
- `production-cli` CI 分组纳入 `test_batch_rename`，覆盖 ready apply、blocked
  plan、unsafe path、Excel 日志和 CLI blocked exit code。

### DEV-402 Excel 案卷分件 plan/apply

状态：`done`

范围：

- 按件名、起始页、终止页复制到指定目录，记录聚合结果和失败原因。

已完成证据：

- 新增 `archive-scan-qc case-split-plan`：从 CSV 或 `.xlsx` 分件表读取
  `case_name`、`start_page`、`end_page`，按输入目录图片文件名稳定排序生成分件复制计划。
- plan 阶段检测不安全件名、非法页码、起止页倒置、页码越界、页码范围重叠、重复件名
  和目标文件已存在；blocked plan 以非零退出，不复制文件。
- 新增 `archive-scan-qc case-split-apply`：只接受 ready plan，将页码范围复制到指定
  case folder，写入 `case_split_apply.json`、`.csv`、`.xlsx` 和
  `case_split_rollback.json`。
- 分件 manifest 明确标记 `local_only=true`、`public_safe=false`；它是 path-bearing
  本地生产工具，不进入 public-safe 交付摘要。
- `production-cli` CI 分组纳入 `test_case_split`，覆盖 CSV/XLSX 输入、ready apply、
  blocked plan、Excel 日志和 CLI blocked exit code。

### DEV-403 双层 PDF/OCR 目录方案

状态：`done`

范围：

- 调研 JPG 转双层 PDF、PDF/OFD 转换、OCR 目录提取。
- OCR 低置信度必须人工复核，不作为当前服务边界默认能力。

已完成证据：

- 新增 `docs/ocr-pdf-directory-plan.md`，明确双层 PDF、PDF/OFD provider、
  OCR 目录候选、人工复核和 public-safe 边界。
- 方案要求 OCR/PDF/OFD 作为 local-only、path-bearing、可复核的扩展工具进入
  service job 边界；真实样本聚合验证前不进入默认图像处理、稳定 public CLI/API
  或 public-safe 摘要。
- 方案定义了 `ocr_directory_candidates.json` local-only 明细和
  `ocr_directory_summary.json` public-safe 聚合摘要的分层边界，禁止公开 OCR 文本、
  目录行、路径、文件名、hash、缩略图、图片内容和 provider 原始日志。
- 方案给出后续最小实现命令边界：provider probe、searchable PDF plan/apply、
  OCR directory plan/apply 和 PDF/OFD convert plan/apply。

## Deferred

- 原图原地修改。
- 云端上传或网络修图。
- 生成式图像修复。
- 未经模板和复核的自动强清洁。
- 直接把 GPU/model 后端暴露成稳定 public CLI/API 契约。
