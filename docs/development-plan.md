# AI4Archive Development Plan

日期：2026-06-02
本次修订：2026-06-09
依据：`archive-scan-qc-retouch-design.md`、`docs/frontend-interface-design-plan.md`、`docs/设计文档补充.docx`、`docs/批量自动质检软件功能需求说明.docx`、`generated/issue-plan/scan_qc_issue_plan.md`
目标：先交付一个可通过 CLI 稳定对外调用的图片批量质检修图程序版本，再把当前 CLI 与本地工作台原型演进为“标准后台服务 + 解耦前端 + 模板驱动图像处理”的可扩展生产系统。当前生产目标以 Windows 为首要平台，必须支持中文文件名和中文路径，保证源文件绝对安全，并支持多线程处理及多个外部请求的资源隔离。

## 1. 总体开发主线

后续开发不再以继续堆叠 CLI 参数或静态页面功能为主线，而应围绕以下产品边界推进：

1. 近期先固化 CLI 稳定批量服务版，让外部系统可以用固定命令、退出码、进度文件和报告产物调用批量质检修图能力。
2. 标准后台服务作为长期系统对外能力边界。
3. 前端完全通过服务接口调用，不直接调用 CLI、内部脚本或图像处理 Worker。
4. 图像处理规则模板驱动后端处理参数。
5. 先复用现有扫描质检、自动处理、复核、验收和聚合报告能力，补齐 JPG 批量/单文件质检、处理项多选、线程数设置和安全输出模式，再补充 TIF 转 JPG、JPG 转双层 PDF、PDF/OFD 转换、批量重命名、DPI 修改、Excel 分件、OCR 目录等扩展工具。
6. 真实图像质量提升单独作为工程主线推进，先建立质量基线、服务化 job 边界、模板化参数和 public-safe 聚合验收，再扩展背景清理、文字增强、阴影/扫描线/透印处理等算法；详见 `docs/image-quality-processing-roadmap.md`。
7. 性能优化继续推进，但必须挂在后台任务模型和模板参数体系下。
8. 多请求并发、job 级资源隔离、Windows 中文路径兼容和源文件只读安全作为基础门槛，必须在 CLI 稳定版和后台服务 MVP 阶段持续验证。

## 2. 当前基础

已经具备的基础能力：

- CLI 扫描质检、`preflight`、`run-plan`、图像处理、复核、验收、移交摘要。
- `production-run` 已具备单批次生产运行、进度文件、处理副本、复核队列和生产摘要基础。
- 本地 `production-workbench` 原型和 smoke/Playwright 覆盖。
- 隐私边界、聚合摘要、真实样本基线、benchmark、worker 推荐。
- 可选 NumPy/OpenCV/libvips 后端基础和 GPU/model capability probe。
- 设计文档已补充标准后台服务、前后端解耦、生产辅助工具和图像处理规则模板。

主要缺口：

- CLI 已有核心批处理能力，但还不是稳定发布版；公开命令范围、输出 schema、退出码、异常落盘、安装包验证和发布门槛需要固定。
- 标准后台服务尚未独立成为产品 API。
- 前端仍偏本地工作台/静态页面形态，不是纯 API client。
- 规则模板还停留在设计层，没有模板 schema、内置模板、参数映射和 dry-run。
- 实际图像质量提升仍不足，当前处理项主要证明“能安全跑”，还没有形成质量收益指标、模板化增强管线、before/after 本地复核和 public-safe 聚合验收。
- TIF 转 JPG、JPG 转双层 PDF、PDF/OFD 转换、批量重命名、DPI 修改、Excel 分件、OCR 目录尚未实现。
- 普通生产入口还需要显式补齐无登录单机流程、单个 JPG 质检、处理项多选、线程数设置和“受控覆盖发布”文案。
- 崩溃恢复需要统一成后台任务 checkpoint，而不是各功能自行处理。
- 性能仍未接近长期生产目标。
- Windows 中文路径、长路径、空格路径和中文文件名尚未形成系统性回归门禁。
- 多个外部请求同时调用时的 job_id、metadata、临时目录、复核队列和模板参数隔离仍需要作为独立服务契约固化。
- 源文件只读安全已有设计原则，但还需要形成处理前后哈希一致、异常中断不改源文件、受控覆盖发布非默认的自动化验收。

## 3. 阶段 0：基线固化

目标：确保后续服务化改造不破坏现有能力。

任务：

- 固定当前 CLI 能力清单和输出契约。
- 建立服务化前的 regression baseline：核心单测、workbench smoke、release validation 的最小组合。
- 明确哪些 JSON 是公开聚合证据，哪些是本地敏感行级证据。
- 增加 Windows 中文路径/空格路径/长路径 fixture 和源文件哈希不变 fixture，作为后续 CLI 与服务层门禁。
- 增加并发隔离设计基线：job_id、batch_id、metadata-out、derivatives-out、temporary directory、checkpoint、template snapshot、review queue 的归属字段必须可追溯。
- 决定 `docs/设计文档补充.docx` 和 `docs/批量自动质检软件功能需求说明.docx` 是纳入版本控制，还是转写为 Markdown 设计来源后归档。

验收：

- 现有核心 CLI 入口和本地工作台 smoke 可稳定运行。
- 文档中服务化目标和当前 CLI fallback 关系一致。
- 未跟踪生成物、工作树副本和设计补充文档的版本管理策略明确。
- Windows 中文路径和源文件只读安全的最小回归样例已纳入测试计划。
- 并发隔离所需的任务上下文字段和目录约定已在设计、CLI 输出和服务计划中保持一致。

## 4. 阶段 0.5：CLI 稳定批量服务版

目标：交付一个外部系统可通过 CLI 稳定调用的批量质检修图程序版本，作为标准后台服务完成前的近期可用版本。

公开入口：

- `archive-scan-qc production-run` 作为单批次生产入口。
- `archive-scan-qc run-plan` 作为多批次计划入口。
- `archive-scan-qc preflight` 作为运行前预检入口。
- `archive-scan-qc capability-probe` 作为运行环境能力探测入口。

任务：

- 固定公开 CLI 契约：参数、默认值、退出码、错误码、命令帮助、公开/内部命令边界。
- 固定输出契约：`production_run_progress.json`、`production_run_summary.json`、`processing_manifest.json`、`processing_retry_manifest.json`、`processing_audit_summary.json`、复核队列、HTML/CSV 报告和 schema_version。
- 强化异常和停滞处理：`production-run` 遇到非预期异常、进程中断或处理失败时必须写入 `failed`、`interrupted`、`blocked` 或可解释的 retry 状态，不能长期保留无法判断的 `running`。
- 强化断点复跑：复用已完成处理副本、retry manifest 和 checkpoint；重复调用不得覆盖原图或静默丢失已完成结果。
- 强化 Windows 路径兼容：CLI 参数、manifest 相对路径、输出目录、报告和恢复逻辑必须支持中文路径、空格路径和长路径；公开摘要继续脱敏。
- 强化源文件安全：处理前后记录源文件可读性和哈希一致性；异常、中断、重试和 `--no-resume-processing` 均不得修改输入目录。
- 强化进程级并发约定：外部调度器可并发启动多个 CLI job，但必须使用不同 metadata/output 目录；CLI 检测到输出目录冲突时必须拒绝或进入明确 resume 模式，不能混写。
- 补齐规则模板 CLI 支持：允许通过模板 ID 选择 `archival-safe-v1`、`text-clean-readable-v1`、`print-clean-v1`、`photo-mixed-safe-v1`、legacy IDs 和用户自定义模板；报告记录模板 ID、名称、版本和参数摘要；text-clean 系模板明确映射为纯文本清洁参数，并关闭去污点前的照片/混合内容保护判断。
- 输出隐私安全收敛：公开摘要只包含聚合状态、数量、风险代码、性能和处理结果，不泄露私有路径、文件名、hash、OCR 文本、缩略图或图片内容。
- 建立发布验证命令：补齐 clean install、wheel 构建、synthetic production-run、run-plan、preflight、capability-probe、核心单测和 release validation 的最小组合。
- 更新运维文档：说明安装、典型命令、目录约定、退出码、状态文件、失败重试、隐私边界和外部调度器集成方式。

验收：

- 在干净虚拟环境安装后，`archive-scan-qc --version`、`preflight`、`production-run`、`run-plan` 和 `capability-probe` 可稳定运行。
- 单批次 synthetic production-run 能生成处理副本、进度文件、生产摘要、处理 manifest、retry manifest、审计摘要和复核队列。
- 多批次 run-plan 能在一个批次失败时记录失败原因，并在允许继续执行时完成其他批次和聚合摘要。
- 非预期异常或中断后，状态文件能表达明确终态或可恢复状态；外部调度器无需人工查看进程日志即可判断下一步动作。
- 三个内置规则模板和用户自定义模板路径均可被 CLI 校验、选择和写入报告。
- 公开摘要通过隐私自检，不包含私有路径、文件名、hash、OCR 文本、缩略图、图片内容或行级敏感证据。
- Windows 中文路径样例可完成 CLI 外部调用、处理副本输出、恢复和报告生成。
- 两个并发 CLI job 使用不同输出目录时互不混淆；同一输出目录冲突时给出明确错误或恢复策略。
- 源文件处理前后哈希一致，异常中断后输入文件仍可打开。

## 5. 阶段 1：标准后台服务 MVP

目标：建立可被前端和外部系统调用的后端边界。

优先接口：

- `GET /api/health`
- `GET /api/capabilities`
- `POST /api/jobs`
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/cancel`
- `GET /api/production/session`
- `POST /api/production/setup`
- `POST /api/production/start`
- `GET /api/production/progress`
- `GET /api/production/review-queue`
- `POST /api/production/review-actions`
- `POST /api/production/finish-export`

实现策略：

- 第一版后台服务可先用 Python 复用现有模块，重点把服务契约、任务状态、隐私过滤和 checkpoint 做对。
- CLI 保留为开发、验证和离线兜底入口。
- 服务层统一管理路径校验、任务 ID、状态机、错误码、日志和恢复。
- API 响应默认输出聚合状态；需要本地私有路径或行级证据时必须走受控授权。
- 单机生产入口默认不设置产品登录；如后续启用局域网多角色模式，鉴权必须在服务边界扩展，不改变首屏生产动作。
- 服务层必须实现 job 级隔离：每个请求独立 job_id、模板快照、输入授权、输出目录、metadata 目录、临时目录、复核队列、checkpoint 和日志。
- 服务层必须实现资源配额：全局最大并发 job 数、每 job 最大 worker 数、全局最大 worker 数、磁盘剩余空间和单 job 临时目录配额。
- 服务层必须使用结构化路径 API 处理 Windows 中文路径，不允许把本地路径拼成 URL 或日志后再反解析。

验收：

- 前端或 `curl` 能创建任务、查询状态、获得聚合进度。
- 服务响应不泄露私有路径、文件名、hash、OCR 文本、缩略图或图片内容。
- 服务重启后能恢复 `running`、`finished`、`failed` 等关键任务状态。
- 两个外部请求同时提交时，状态、输出、模板参数、复核队列和错误处理互不混淆。
- 取消、失败或恢复一个任务不影响其他任务，也不修改源文件。
- Windows 中文路径任务可通过 API 创建、查询、恢复并生成处理副本。

Implementation note, 2026-06-09: the service-job boundary core has a first
local implementation in `archive_scan_qc.service_jobs`. It creates isolated
per-job roots, writes private `service_job.json` checkpoints, writes
public-safe `service_job_public_summary.json` aggregate status, rejects
input/service-root overlap, and can recover terminal or stale running progress
without exposing private paths. The first local HTTP API endpoints have landed;
async execution and production-specific session endpoints remain follow-up work.
The checkpoint loader also revalidates `input_dir` against the service root and
marks `running` records without progress as `needs_recovery` after restart.
Root-level recovery now writes `service_job_index_public_summary.json` so an
external scheduler can poll aggregate job state without reading private
checkpoints.
The core now supports a `cancelled` terminal state and rejects attempts to rerun
terminal service jobs.
It also enforces a per-job worker limit during job creation and reports the
non-sensitive worker quota in the public summary.
`archive_scan_qc.service_api` now provides endpoint-shaped health,
capabilities, rule-template catalog/detail, create, status, run, start, cancel,
and recover responses while keeping path-bearing request data out of public
responses. The prototype
`archive_scan_qc.service_http` transport exposes the core as local-only HTTP
endpoints for `curl`/frontend integration tests; it uses the configured
service root instead of accepting a client-provided service-root path and still
returns only public-safe aggregate JSON. The prototype `POST
/api/jobs/{job_id}/run` endpoint currently invokes the production runner
synchronously and returns the terminal public summary with aggregate quality
fields. The new `POST /api/jobs/{job_id}/start` endpoint is an in-process async
MVP: it returns `running` immediately, runs the same production runner in a
background thread, keeps active jobs `running` while the service process is
alive, and still marks stale running checkpoints as `needs_recovery` after
restart.

## 6. 阶段 2：图像处理规则模板系统

目标：让后端处理参数由模板驱动。

内置模板：

- `dat-31-2017-standard`：严格按照 DA/T 31-2017 和项目验收规则处理，原貌保护优先。
- `archival-safe-v1`：路线图命名的原貌保护模板，当前复用已验证的档案安全处理默认值。
- `text-clean-print`：面向纯文本扫描件，尽量提高洁净度和文字清晰度，接近干净打印效果。
- `text-clean-readable-v1`：路线图命名的纯文本可读性模板，默认启用当前已验证的背景、阴影、透印、扫描线、褪色文字和文字边缘增强组合。
- `print-clean-v1`：面向打印/利用副本的强清洁模板，当前复用 text-clean 管线并在 dry-run 中提示过处理复核。
- `high-fidelity-original`：面向照片、绘画、珍贵档案等，核心区域尽量不处理，只处理边框外或指定区域。
- `photo-mixed-safe-v1`：路线图命名的照片/混排保护模板，当前复用高保真低风险处理默认值。
- `custom`：用户自定义模板，必须通过参数校验和样例 dry-run 后才能用于正式批次。

任务：

- 定义模板 schema：质检阈值、处理开关、处理强度、区域保护、复核策略、输出策略、性能策略和审计字段。
- 先实现 CLI 级 `rule-template-catalog` 和 `rule-template-dry-run`，再在服务化阶段补齐 `GET /api/rule-templates`、`GET /api/rule-templates/{template_id}`、`POST /api/rule-templates`、`PUT /api/rule-templates/{template_id}`。
- 实现模板 dry-run：用样例图片生成处理计划和风险提示，不写正式输出。
- 将模板参数映射到现有 `rules_profile`、`ProcessingOptions` 和 review policy。
- 禁止自定义模板关闭关键 P0 完整性规则或突破隐私/审计边界。

验收：

- legacy 模板和四个 v1 内置模板可查询和选择。
- 同一批样例在不同模板下生成不同处理计划。
- 自定义模板非法参数会被拒绝。
- 报告记录模板 ID、名称、版本和参数摘要。

当前进展（2026-06-09）：CLI 已提供 public-safe 的
`rule-template-catalog` 和 `rule-template-dry-run`，分别输出
`rule_template_catalog.json` 与 `rule_template_dry_run.json`。dry-run 当前
输出聚合处理计划和风险码，不运行修图、不写派生图；`processing_manifest.json`
已记录经过整理的模板快照和最终处理选项。只读 HTTP API
`GET /api/rule-templates` 与 `GET /api/rule-templates/{template_id}` 已由
service API 暴露；完整自定义模板校验和写接口仍属于后续服务化阶段任务。
Follow-up, 2026-06-09: 路线图中的 `archival-safe-v1`,
`text-clean-readable-v1`, `print-clean-v1`, and `photo-mixed-safe-v1` 已作为
内置模板 ID 落地；legacy ID 继续兼容。`text-clean-readable-v1` 和
`print-clean-v1` 复用当前已验证的 text-clean 质量增强组合，后者在 dry-run
中额外提示过处理复核。

当前进展（2026-06-09）：M2 的第一步已扩展 `normalize_tones`，使中性浅纸面
低对比文字页可以产生可量化的背景和对比度提升；明显边缘阴影页会跳过全页 tone，
交给局部阴影清理，避免和 guardrail 冲突。
`image-processing-capability-smoke` 也开始要求折痕阴影、保守透印弱化和分段扫描线
在全链路 synthetic fixture 上至少各有一次可量化生效。
随后补入轻微发虚正文夹具，要求浅墨正文增强和文字边缘锐化也产生 public-safe
聚合增益。
最新补充：褪色正文增强已增加极浅打印字形窄路径，只在稳定小组件数量足够、
候选面积受控时放宽到极浅文字；过淡条状页、手写、表格、印章、照片和纹理仍保持跳过。
`image-processing-capability-smoke` 同步加入极浅打印字形 fixture，要求该能力进入
public-safe 聚合质量基线。
M3 的第一片保护契约也已补齐：`photo-mixed-safe-v1` 通过生产 CLI 端到端测试覆盖
合成照片/印章/表格混排页，要求 production summary 和 processing manifest 明确保持
强背景清理、透印清理、褪色文字增强和文字边缘锐化关闭，同时确认源图不变并仍输出
public-safe 质量摘要。`image-processing-capability-smoke` 也已把混排保护变成
public-safe 聚合质量证据：输出该 fixture 的像素变化率、颜色均值漂移和边缘能量漂移
及对应阈值。后续 M3 继续补按风险分组的本地 review package。

## 7. 阶段 3：前端改为 API Client

目标：生产工作台不再直接绑定本地脚本或静态数据。

任务：

- 批次设置页面改为调用后台服务。
- 增加模板选择和模板风险说明。
- 增加 JPG 文件夹/单文件入口、处理项多选、线程数设置、输出模式选择和“受控覆盖发布”确认流程。
- 进度、复核队列、预览、完成导出全部走 API。
- 保留后台/管理员聚合工作台，但明确是验收/维护入口。
- 前端不直接处理本地文件，不猜测绝对路径，不执行图片处理。

验收：

- 前端只依赖 API 返回数据。
- 可以通过 mock API 跑浏览器 smoke。
- 生产工人首屏保持“选文件夹、选模板、开始处理、待复核、完成导出”。
- 错误提示和阻断原因使用中文动作语言，不暴露工程内部术语。

## 8. 阶段 4：批量工具任务体系

目标：把新增功能统一放进后台任务模型。

优先顺序：

1. TIF 转 JPG：按输入目录递归转换到指定输出目录，文件基名不变，记录源/目标格式和失败原因。
2. 批量重命名：先做 dry-run、冲突检测、应用、回滚清单，日志至少包含原名称和新名称。
3. 批量 DPI 修改：只处理副本或元数据，明确原始 DPI、目标 DPI、实际写入 DPI 和结果。
4. Excel 案卷分件：按件名、起始页、终止页复制到用户指定目录。
5. JPG 转双层 PDF：基于文件夹页序生成 OCR 文本层 PDF。
6. PDF 转 OFD：作为利用副本转换，兼容需求文档中 OPD/OFD 表述差异。
7. OCR 目录表：默认提取文件编号和题名，识别率先按 70% 目标，低置信度必须人工复核。

统一要求：

- 每个工具都有 `plan/apply` 两步。
- 支持 checkpoint、恢复、失败重试。
- 不静默覆盖文件。
- 写操作都有执行确认和结果摘要。
- 可导出 Excel 日志，同时保留 JSON/CSV 机器字段。
- 对外摘要只包含聚合数量、状态、风险代码和隐私安全字段。

## 9. 阶段 5：性能与后端优化

目标：在质量提升成立并被模板、job 边界和聚合指标约束后，继续压缩处理瓶颈。真实图像质量优化的完整路线以 `docs/image-quality-processing-roadmap.md` 为准，本文阶段 5 只记录性能和后端实现侧的配套工作。

任务：

- 在后台任务中记录 per-stage/per-operation timing。
- 把 `deskew`、`despeckle` 继续迁移到 NumPy/OpenCV 路径。
- 扩展 libvips IO 到更多大图读写场景。
- 按模板启用不同处理强度，避免高保真模板做无意义重处理。
- worker 推荐从 benchmark 进入后台调度策略。
- 保留 CPU/Pillow fallback，直到可选后端经过真实样本验证。

验收：

- 不低于当前 149 张样本聚合基线。
- 处理失败为 0，或有可解释且可复核的原因。
- 隐私自检和 cleanup 仍通过。
- 质量发现数量与基线一致，或差异有清晰解释。

## 10. 暂缓范围

暂不优先做：

- 局域网多机集中处理完整形态。
- 国产系统安装包和实机适配。
- 真正 GPU/OCR/版面模型生产闭环。
- 管理驾驶舱式统计大屏。
- 生成式图像修复。

这些方向保留为长期目标；除非出现生产阻塞或明确指令，否则不应挤占标准后台服务、模板系统和生产工作台 API 化的优先级。

## 11. 建议下一批 Issue

完整 issue 拆分见 `docs/development-issues.md`。下列条目是建议优先启动的第一批工作。

1. CLI 稳定批量服务版：公开命令契约、状态文件、退出码和发布验证。
2. `production-run` 异常终态、停滞识别和断点复跑强化。
3. Windows 中文路径与源文件只读安全回归：中文目录、空格路径、长路径、处理前后哈希一致和异常中断安全。
4. CLI 并发隔离：多个外部 `production-run` 进程使用独立 metadata/output 目录时互不混淆，输出目录冲突时明确拒绝或恢复。
5. CLI 规则模板选择、模板校验和报告参数摘要。
6. 后台服务 MVP：任务模型、状态查询、隐私安全响应。
7. 后台服务 job 级隔离与资源配额：job_id、模板快照、临时目录、复核队列、checkpoint、workers 上限。
8. 规则模板 schema 与 3 个内置模板。
9. 模板 dry-run 和参数映射到现有处理链。
10. 生产工作台改为 API client。
11. 批量重命名 `plan/apply`。
12. Excel 案卷分件 `plan/apply`。
13. 双层 PDF/OCR 目录能力调研与最小实现方案。

## 12. 验证策略

- 文档-only 变更：文档 diff 审查即可。
- CLI 稳定版变更：在干净环境执行 CLI smoke、synthetic production-run、run-plan、preflight、capability-probe、核心 unittest、compileall 和 release validation。
- 服务接口变更：补充 API 单测、状态机单测和隐私泄露回归测试。
- 前端调用变更：跑 mock API 浏览器 smoke 和本地工作台 smoke。
- 图像处理参数或模板变更：跑模板 dry-run、核心处理单测和隐私安全检查。
- 核心图像处理、质量规则、隐私边界或发布候选：按现有策略使用固定私有样本做聚合验证，只公开聚合指标。
- Windows/中文路径变更：必须在 Windows 下使用中文目录、中文文件名、空格路径和长路径 fixture 跑 CLI/API smoke。
- 源文件安全变更：必须断言输入文件处理前后哈希一致，异常中断、失败重试和恢复路径均不修改源文件。
- 并发调度变更：必须同时运行至少两个 job，验证 metadata、输出目录、复核队列、模板参数、错误状态和取消操作互不污染。

## 13. 计划维护规则

- 设计目标变化时，先更新本计划和对应设计文档，再拆 issue。
- 新增功能必须说明所属阶段、接口边界、隐私边界和验证方式。
- 不把短期 CLI 兜底实现误写成长期前端集成方式。
- 短期 CLI 稳定版的任务模型、输出 schema、状态语义和隐私边界必须为后续标准后台服务复用，避免形成两套互不兼容的产品契约。
- 每个阶段完成后，用实际实现和验证结果修订本计划。
