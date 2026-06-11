# Image Quality Processing Roadmap

日期：2026-06-09

本文档定义后续真实图像质量优化的工程路线。当前系统已经具备扫描质检、保守派生处理、生产 CLI、服务 job 原型、隐私边界和 CI 分组，但实际修图提升仍偏弱：多数处理项以“不破坏”为第一目标，质量改善缺少可量化基线、模板化参数、局部内容保护、真实样本验收和服务化恢复边界。

2026-06-10 的真实 NoisyOffice 外部 CLI 验证表明，当前 `text-clean-print` 对 OCR 预处理意义上的质量改善不成立：216 张图稳定处理完成，但 PSNR、SSIM、MSE、MAE、暗像素 F1 和前景保留率均出现宏平均微幅变差。面向 OCR 的强预处理必须作为独立专项推进，详见 `docs/ocr-preprocessing-image-quality-plan.md`。该专项计划新增 `ocr-preprocess-v1` 目标 profile，并要求以 NoisyOffice、DIBCO/OCR 指标和私有样本聚合门槛作为完成标准。

2026-06-11 的真实扫描样本人工目检确认，`ocr-preprocess-leptonica-v1` 的 preserve-canvas
deskew 路径是当前最佳保留基线：它保留源图尺寸、执行必须纠偏，并避免旧强 OCR 路线造成的
水波纹、笔画/表格线扭曲和尺寸膨胀。后续 OCR 图像优化应在该路径上继续提升可读性，或至少
与该路径并列对照；不得用无纠偏输出或破坏文字结构的强处理结果替代该基线。
GitHub/开源算法调研已沉淀到 `docs/github-image-processing-algorithm-research.md`，后续新增
OCR 图像处理 path 时以该文档的候选路线和风险判断为输入。

后续开发不能继续无边界增加修图开关。正确主线是：先建立服务化 job 边界、路径隔离、状态恢复和 public-safe 摘要，再在这个边界内建设可度量、可回滚、模板驱动的图像增强管线。

## 1. 产品目标

目标不是做通用美图工具，而是做档案扫描生产中的“安全派生图优化”：

- 源文件永远只读，不做原地覆盖。
- 输出派生图必须提升可读性、洁净度或版面可用性。
- 对照片、印章、批注、彩色内容、历史纸张纹理和装订痕迹保持保守。
- 每个处理结果必须有 manifest、质量指标、guardrail、失败原因和复核入口。
- 外部系统只能依赖稳定 job/API/CLI 契约，不直接调用内部处理函数。
- public-safe 摘要只输出聚合指标，不泄露路径、文件名、hash、缩略图、OCR 文本或图片内容。

## 2. 当前问题判断

现状的核心问题不是缺少按钮，而是缺少质量闭环：

- 处理项偏保守，`deskew`、`auto_crop`、`trim_dark_border`、`despeckle` 主要修正明显问题，对泛黄、灰底、阴影、透印、低对比文字和扫描线的提升有限。
- 可选 `normalize_tones`、`lighten_edge_shadow`、`lighten_background_stains`、`lighten_scanlines` 等方向已经存在，但缺少足够强的模板策略、区域保护、真实样本验收和默认启用依据。
- 当前 synthetic smoke 证明“能跑”，不能证明“质量明显变好”。
- 当前 benchmark 更偏性能和失败率，缺少 before/after 质量收益指标。
- 局部质量改善和过处理风险尚未形成同一套 guardrail。
- 处理开关直接暴露给 CLI/模板时，容易形成无法解释的参数组合。

结论：后续重点应从“功能开关堆叠”切到“模板驱动的质量目标 + 服务 job 安全边界 + 可量化验收”。

## 3. 架构原则

### 3.1 Job 边界优先

所有新质量处理必须运行在 job 边界内：

- 每个 job 有独立 `job_id`、输入授权、输出目录、临时目录、模板快照、checkpoint、manifest、review queue 和日志。
- 输入目录和服务根目录不得重叠；输出目录冲突必须拒绝或进入明确 resume 模式。
- job checkpoint 是 local-only 敏感状态；public summary 是可分享聚合状态。
- 处理失败、中断、取消和恢复必须有明确状态，不允许长期停在不可判断的 `running`。
- 重试必须复用已完成派生图和 manifest，不能静默覆盖或丢失已完成结果。

### 3.2 模板优先于开关

新增算法不直接变成默认公开开关。它们先进入模板：

- `archival-safe-v1`：保留原貌，只做几何纠偏、边框和极小噪点处理。
- `text-clean-readable-v1`：面向纯文本材料，强调灰底清理、文字对比、扫描线和透印控制。
- `print-clean-v1`：面向后续打印/利用副本，允许更强的背景均衡和文字锐化，但必须有过处理复核。
- `ocr-preprocess-v1`：面向 OCR 的强预处理利用副本，允许更强背景归一、去噪、局部阈值、二值/准二值输出和文字增强；不作为保真派生图默认模板，必须通过 NoisyOffice、DIBCO/OCR 和复核门槛。
- `ocr-preprocess-leptonica-v1`：与旧 OCR profile 并列的保守 OCR 灰度主图路径，默认尝试保留画布尺寸的纠偏，不默认裁切/锐化，只做背景归一和前景笔画保护，二值化作为 sidecar 复核；当前作为真实扫描样本人工目检最佳保留基线。
- `ocr-preprocess-opencv-local-v1`：实验性并列 OCR 路径，使用 OpenCV/NumPy 局部背景估计、受保护前景 mask 和 adaptive/Otsu binary sidecar；外部接口不变，通过模板/path 选择运行，当前只作为与 Leptonica 基线对照的候选路线。
- `photo-mixed-safe-v1`：照片、图像、印章、彩色批注和混合版面保护优先。
- `custom`：用户自定义模板必须通过 schema 校验、dry-run 和参数边界检查。

公开 CLI/API 接受模板 ID 和少数高层意图，不接受无限组合的底层滤镜参数。

### 3.2.1 稳定处理路径

为比较算法效果，模板和处理实现之间必须有稳定的 `processing_path` 层：

- 外部 CLI/API 形状保持不变，生产运行仍通过 `--rule-template` 或 service job
  `rule_template` 选择处理路线。
- 内部由模板解析出 `processing_profile`，再解析到 `processing_path`，例如
  `ocr-preprocess-leptonica-v1` 或 `ocr-preprocess-opencv-local-v1`。
- `processing_path` 是 public-safe 枚举，不是本地文件路径；可以写入 catalog、
  dry-run、生产摘要、service job public summary、processing manifest 和 plan
  fingerprint。
- 新算法必须先注册为新的 path ID，再接入独立 dispatch 入口；不得通过复用旧 path
  的隐藏开关来替换算法，否则无法做真实批次对照和恢复隔离。
- 同一批真实样本比较算法时，报告必须按 `processing_path` 分组记录质量指标、性能、
  guardrail、review 和源文件安全结果。

2026-06-11 进展：`ocr-preprocess-opencv-local-v1` 已接入该路由模型，production CLI、
manifest、service job checkpoint/public summary 和 public capability contract 均保留其
`processing_profile=ocr_preprocess_opencv_local` 与 path ID。真实扫描样本 12 张复测显示它能在
不扩张画布、不修改源图的前提下完成 11/12 纠偏和 12/12 binary sidecar，但平均文字边缘能量仍未
超过 Leptonica 基线，因此暂不提升为默认 OCR 路径。

### 3.3 质量收益必须可度量

每个处理阶段都要记录 before/after 聚合指标：

- 背景亮度均匀度、背景污染面积、边缘阴影面积、角落阴影面积。
- 文字/前景对比度、局部对比提升、锐度变化、低对比区域比例。
- 斜率角度和纠偏置信度。
- 黑边面积、裁切比例、内容边缘保留率。
- 噪点候选数量、去噪像素比例、连通文字保护命中率。
- 扫描线强度、位置和处理后残留强度。
- 透印候选面积和处理后变化。
- 尺寸变化、色彩偏移、过曝/欠曝比例、过处理 guardrail 命中数。

public-safe 输出只保留聚合计数、均值、分位数、状态和风险代码；行级图片证据只留在本地 review package。

## 4. 目标处理管线

后续处理管线按稳定阶段推进，每个阶段都可以在模板中启用或禁用。

### 4.1 输入标准化

- EXIF transpose。
- 格式、色彩模式、DPI、尺寸、帧数和可打开性检查。
- 源文件 hash 读取和只读安全记录。
- 超大图内存预算和 tile/stream 处理策略。

验收：输入文件处理前后 hash 一致；异常中断后仍可打开。

### 4.2 版面几何

- 更可靠的小角度 deskew，继续保持低置信度不旋转。
- 黑边/暗边 trim 与内容边缘保护联动。
- 自动裁切只裁掉可信空白/边框，不裁正文、页码、印章和装订边。
- 后续可研究轻量透视/拍照倾斜纠正，但不进入首批默认能力。

验收：斜率改善有聚合指标；裁切比例和内容边缘损失有 guardrail。

### 4.3 背景与照明

- 背景估计：区分纸张背景、文字前景、印章/批注、照片/图像块。
- 灰底/泛黄归一：默认保持纸张质感，`text-clean-readable-v1` 可更强。
- 边缘阴影、角落阴影、折痕阴影和渐变照明校正。
- 背景污渍 lightening：只处理小面积、低饱和、非文字连通区域。

验收：背景均匀度提升；文字区域对比不下降；彩色内容偏移受限。

### 4.4 文字可读性

- 局部对比增强，优先作用于文字附近低对比区域。
- 轻量锐化文字边缘，避免光晕和笔画断裂。
- 对严重灰底材料提供可选二值化/准二值化利用副本，但不覆盖保真派生图。
- 文字保护 mask 参与去污点、去透印和背景处理。

验收：前景/背景对比提升；细笔画保留率不过低；过锐化 guardrail 为零或可复核。

### 4.5 噪点、扫描线和透印

- 去孤立噪点继续保守，NumPy/OpenCV 只作为同语义加速或更稳定候选实现。
- 扫描线处理按方向、连续性、宽度和颜色中性判断，避免处理正文横线、表格线和装订线。
- 透印处理先识别背面淡文字候选，只对低置信度背景区域做弱化。
- 胶印、霉斑、污渍和纸纹不得简单等同噪点。

验收：噪点/扫描线/透印指标下降；表格线、正文线、批注和印章保留。

### 4.6 输出与审计

- 同一源图可生成多个派生 profile：`archival-safe`、`text-readable`、`print-clean`。
- manifest 记录模板、参数摘要、操作顺序、每阶段指标、guardrail、失败原因和复核建议。
- review package 提供本地 before/after 引用和分组列表，但不嵌入图片字节。
- public summary 只输出聚合状态和质量收益统计。

验收：外部系统可以只读 public summary 判断 job 状态；人工复核可在本地追溯具体图片。

## 5. 里程碑

### M0：质量基线和服务边界固化

目标：先证明“怎么判断变好”，并把处理运行关进 job 边界。

任务：

- 定义 `processing_quality_summary.json` public-safe schema。
- 扩展 synthetic fixture：灰底、泛黄、边缘阴影、角落阴影、折痕、低对比文字、扫描线、透印、照片混排、印章批注、表格线。
- 建立 private validation set 分类标签，但标签和样本路径只留本地。
- 在 service job 中挂载质量处理 job 状态：`queued`、`running`、`completed`、`failed`、`cancelled`、`stale_recovered`。
- 明确 derivatives、metadata、temp、review、logs、public-summary 的目录隔离。
- 增加源文件 hash 不变、输出目录冲突、恢复和 public-safe 摘要测试。

验收：

- synthetic quality baseline 可稳定生成聚合指标。
- 两个并发 job 的输出、状态、模板和 review queue 不混淆。
- public summary 不包含路径、文件名、hash、缩略图、OCR 文本或图片内容。

Implementation note, 2026-06-09: `processing_quality_summary.json` with schema
`scan-qc.processing-quality-summary.v1` is now the first public-safe quality
baseline artifact. The synthetic `image-processing-capability-smoke` command
writes it next to the smoke summary and embeds the same aggregate baseline in
the smoke payload. It covers aggregate changed-file counts, before/after metric
averages/maxima, guardrails, fixture context, and privacy flags without paths,
filenames, hashes, thumbnails, OCR text, or image content.

Fixture update, 2026-06-10: the synthetic smoke baseline now covers 19 generated
fixture groups: clean text, small-angle skewed text, dark border, scanner
gutter, speckles, faded edge shadow, color cast, low-contrast text,
illumination gradient, scanlines, diffuse bleed-through, broad thin-paper
bleed-through, corner shadow, localized background stain, fold shadow, blurred
text edges, ultra-pale typed text, low-saturation carbon-copy text, and mixed
photo/stamp/table content. This remains public-safe because the images are
generated at runtime and only aggregate fixture group IDs are published.
Follow-up, 2026-06-10: `image-processing-capability-smoke` now treats declared
required quality operations as status blockers. If aggregate audit counts show
zero applied files for a required operation, the public-safe smoke fails with a
specific blocking code instead of merely publishing a zero count.
Follow-up, 2026-06-10: the same smoke gate now also checks required public-safe
quality metric maxima. An operation counted as applied is not enough; key
deskew, tone, color-cast, shadow, stain, fold, illumination, bleed-through,
scanline, faded-text, and text-edge metrics must clear minimum evidence
thresholds or the smoke emits an explicit blocking code. Text-edge smoke
evidence also requires aggregate edge energy to increase after sharpening.
Follow-up, 2026-06-10: the public-safe handoff chain now consumes the image
smoke evidence directly. `evidence-bundle-verify`,
`public-safe-validation-index`, `artifact-readiness-checklist`, and
`workbench-summary` recognize `image_processing_capability_smoke.json` and
`processing_quality_summary.json`; failed image-quality gates propagate as
aggregate `blocking_codes` instead of disappearing after the smoke command.

Production-run update, 2026-06-09: `production-run` now writes
`processing_quality_summary.json` beside the processing manifest and audit
summary for every completed derivative-processing batch. The production summary
stores a public-safe aggregate excerpt, and service job public summaries expose
only quality status, processed/failed counts, guardrail failed counts,
changed-file counts, and whitelisted aggregate quality metrics including
changed-pixel ratios and brightness/contrast deltas.
Follow-up, 2026-06-10: `processing_quality_summary.json` now distinguishes
`quality_signal.status` values for `measured_with_changes`,
`measured_no_quality_operations`, and `not_applicable`. Service job public
quality summaries expose that status so schedulers can triage no-benefit clean
batches without opening manifests or row-level evidence.

### M1：模板 schema 和 dry-run

目标：用模板表达质量目标，而不是暴露底层滤镜组合。

任务：

- 定义模板 schema：目标 profile、处理阶段、强度区间、保护规则、输出策略、复核策略。
- 实现内置模板：`archival-safe-v1`、`text-clean-readable-v1`、`print-clean-v1`、`photo-mixed-safe-v1`。
- 实现 template dry-run：读取样例或 scan report，生成处理计划和风险提示，不写正式派生图。
- 将模板快照写入 job checkpoint、processing manifest 和 public summary 的聚合字段。
- 非法模板参数必须拒绝；模板不得关闭源文件安全和隐私边界。

验收：

- 同一 synthetic fixture 在不同模板下生成不同处理计划。
- 自定义模板越界会失败并返回 public-safe 错误码。
- dry-run 能指出预计高风险图片类别和需要复核的处理阶段。

Implementation note, 2026-06-09: the first M1 slice is now available as
public-safe CLI output. `rule-template-catalog` writes
`rule_template_catalog.json` with schema `scan-qc.rule-template-catalog.v1`,
and `rule-template-dry-run` writes `rule_template_dry_run.json` with schema
`scan-qc.rule-template-dry-run.v1`. The dry-run reports aggregate scan counts,
planned operation stages, and risk codes without running image processing or
writing derivative images. HTTP template APIs are now exposed through
`GET /api/rule-templates`, `GET /api/rule-templates/{template_id}`,
`POST /api/rule-templates`, and `PUT /api/rule-templates/{template_id}`.
Follow-up, 2026-06-09: `POST /api/rule-templates/validate` now provides the
first service-side custom template validation gate for inline drafts. It does
not write templates and returns only public-safe aggregate validation counts,
risk codes, and privacy flags.
Follow-up, 2026-06-09: service-managed custom template writes now save validated
drafts under server-owned storage. Catalog/detail responses include saved
custom templates without returning paths or rule rows, and service jobs can use
the saved template ID with a private template snapshot.

Follow-up, 2026-06-09: the roadmap template IDs `archival-safe-v1`,
`text-clean-readable-v1`, `print-clean-v1`, and `photo-mixed-safe-v1` are now
built-ins. Legacy IDs remain supported. `print-clean-v1` now resolves to a
`print_clean` processing profile instead of being only a renamed text-clean
plan: dry-run/detail payloads, production manifests, and production summaries
publish the profile, while `print-clean-v1` still adds an overprocessing review
risk code during dry-run.
Follow-up, 2026-06-10: the same `print_clean` profile now also reaches stable
blurred text-edge sharpening. Standard readable runs keep the original
conservative sharpening strength, while print-clean runs use a stronger bounded
edge profile and preserve the same candidate screening, content-protection
checks, source-byte invariant, aggregate audit, and guardrails.

Processing-manifest update, 2026-06-09: `processing_manifest.json` now records
the selected `rule_template` snapshot when the source scan report carries one.
The snapshot is limited to template ID, version, source, and processing defaults;
the manifest still records the final applied processing options separately.

### M2：Text-clean 质量管线 v1

目标：先把纯文本扫描件做出肉眼可见提升。

任务：

- 实现背景估计和灰底/泛黄归一。
- 实现低对比文字增强和轻量边缘锐化。
- 实现边缘/角落/折痕阴影弱化。
- 强化扫描线检测和弱化。
- 对透印做保守弱化候选，不做激进清除。
- 每个阶段都产生 before/after 指标和 guardrail。

验收：

- `text-clean-readable-v1` 在 synthetic 和私有样本聚合指标上提升背景均匀度、文字对比和扫描线残留。
- 处理失败为 0，或失败原因可解释且可复核。
- 彩色内容、印章、批注和表格线误处理率低于约定阈值。

Implementation note, 2026-06-09: `normalize_tones` now has a guarded light-paper
low-contrast path. It raises neutral light-paper backgrounds modestly while
darkening low-contrast printed text enough to improve aggregate contrast, and it
skips obvious edge-shadow pages so localized shadow cleanup remains responsible
for those cases. The synthetic smoke now requires at least one tone-normalized
fixture with public-safe `tone_background_delta` and `tone_contrast_delta`
evidence.
Follow-up, 2026-06-10: the light-paper low-contrast path now accepts moderate
but visible contrast gains instead of no-oping when the conservative candidate
improves contrast by less than the older high threshold. A dedicated regression
checks that a neutral low-contrast text page changes visibly, improves
background/contrast metrics, keeps the source file unchanged, and publishes only
aggregate audit evidence.
Follow-up, 2026-06-10: `tone_changed_pixel_ratio` now counts moderate
tone-normalization pixel deltas above 16 luma points. This makes the same
light-paper low-contrast regression publish non-zero public-safe changed-ratio
evidence for the acted text region while avoiding the full-page background
overcount that appears at lower thresholds.
Follow-up, 2026-06-10: `print-clean-v1` now uses the `print_clean` processing
profile for light-paper low-contrast tone normalization. The profile keeps the
same color, texture, foreground-density, and guardrail stack but maps the safe
text-clean candidate to a whiter background and stronger text contrast for
print/use copies; the regression compares it against the standard readable
profile on the same synthetic page and verifies source bytes plus aggregate
audit privacy remain unchanged.
Follow-up, 2026-06-10: `print_clean` now also strengthens stable blurred text
edge sharpening for the same print/use-copy target. The regression compares the
same synthetic page under `standard` and `print_clean`, requires higher edge
delta and edge-energy gain for the print-clean derivative, and keeps the local
source, public-safe audit, and combination/cumulative guardrails unchanged.
Follow-up, 2026-06-10: the production CLI contract for
`production-run --rule-template print-clean-v1` now runs a stable blurred text
page and requires the processing manifest to show the `print_clean` profile plus
`applied_print_clean_blurred_text_edges` evidence, while the source image bytes
remain unchanged.

Follow-up, 2026-06-10: the same `print_clean` profile now strengthens stable
faded-text darkening after the existing color, density, component-structure, and
background-stability checks have passed. The core regression runs the same pale
typed page under `standard` and `print_clean`, requires
`applied_print_clean_stable_low_contrast_text`, a larger text-darkening delta,
unchanged source bytes, and passed cumulative/combination guardrails. Public
smoke reason-code aggregation also allowlists the print-clean low-saturation
faded-text code without exposing paths, filenames, hashes, or image content.
The faded-text preflight now also treats sparse stable dark foreground text as a
protected no-op so already-dark blurred text can continue into the text-edge
sharpening path instead of being reclassified as faded text.
Follow-up, 2026-06-10: the production CLI contract now also runs a stable faded
text page under `production-run --rule-template print-clean-v1`, requiring the
processing manifest to show `print_clean`,
`applied_print_clean_stable_low_contrast_text`, a positive bounded changed-pixel
ratio, no guardrail failures, and unchanged source bytes.
Follow-up, 2026-06-10: `print-clean-v1` production CLI coverage now also includes
a stable localized background-stain page. The contract requires bounded positive
background-stain deltas in the manifest and public-safe
`processing_quality_summary.json`, no guardrail failures, and unchanged source
bytes.
Follow-up, 2026-06-10: `print-clean-v1` production CLI coverage now includes a
stable low-contrast neutral scanline page as well. The manifest and public-safe
quality summary must show bounded positive scanline deltas and changed-pixel
ratios while keeping source bytes unchanged.
Follow-up, 2026-06-10: `print-clean-v1` production CLI coverage now also includes
a faint reverse-side bleed-through page. The contract requires
`applied_faint_reverse_ghost`, bounded positive bleed-through deltas,
public-safe quality metrics, no guardrail failures, and unchanged source bytes.
Follow-up, 2026-06-10: `print-clean-v1` production CLI coverage now includes a
low-amplitude illumination-gradient page. Because this operation is a full-page
low-delta leveling pass, the contract treats a high changed-pixel ratio as
acceptable only with bounded correction delta, guardrail pass, public-safe
quality metrics, and unchanged source bytes.

Follow-up, 2026-06-09: the synthetic smoke fixtures now also cover a safe narrow
fold-shadow band, a diffuse reverse-side bleed-through ghost, and a segmented
neutral scanline. The public-safe smoke must show at least one applied file and
positive aggregate delta for each of those three M2 cleanup stages.

Follow-up, 2026-06-10: conservative bleed-through cleanup now has a narrow
broad thin-paper path. It allows a single wide, stable light-paper show-through
candidate only when protected/edge/mid-gray mark density stays low, then shrinks
the acted area to the darkest diffuse core so changed and candidate pixel ratios
remain inside the existing guardrails. The public-safe smoke fixture set now
includes this broad thin-paper bleed-through case.

Follow-up, 2026-06-09: the smoke fixture set now includes a mildly blurred typed
body-text page. The public-safe smoke must show both faded-text enhancement and
text-edge sharpening deltas, including increased aggregate text-edge energy.

Follow-up, 2026-06-09: faded-text enhancement now has a narrow ultra-pale typed
glyph path. The candidate threshold extends only to very light printed glyphs,
requires many stable small components and a tight changed area, and keeps broad
line-like low-confidence pages, handwriting, tables, stamps, photos, and texture
risks on the skip path. The public-safe `image-processing-capability-smoke`
fixture set now includes this ultra-pale typed-glyph case.
Follow-up, 2026-06-10: the same faded-text path now requires a stronger visible
gain once both text-structure and background-stability evidence are strong. It
raises only the minimum darkening for already-selected safe text pixels, leaving
candidate detection, protected-content skips, changed-pixel bounds, and
public-safe aggregate reporting unchanged.
Follow-up, 2026-06-10: faded-text enhancement now has a narrow low-saturation
carbon-copy text path. It can conservatively darken stable cool low-saturation
text components on light paper while keeping red stamps, saturated color marks,
long form rules, and boxed form structures on the protected no-op path. The
public audit reports `applied_stable_low_saturation_text` as an aggregate reason
code without paths, filenames, hashes, thumbnails, OCR text, or image content.
`image-processing-capability-smoke` now includes a matching synthetic fixture,
publishes the whitelisted reason code/count under
`operation_reason_code_counts`, and fails with
`low_saturation_faded_text_reason_not_observed` if the evidence disappears.

Follow-up, 2026-06-09: the smoke fixture set now includes a light-paper page
with a low-amplitude illumination gradient and sparse typed text. The public-safe
baseline must show at least one illumination-gradient leveling operation plus
aggregate correction-delta and changed-pixel-ratio evidence, without publishing
paths, filenames, or image content.

Follow-up, 2026-06-09: the smoke fixture set now includes a light-paper page
with localized neutral background stains away from printed text. The public-safe
baseline must show at least one background-stain cleanup operation plus aggregate
stain-delta and changed-pixel-ratio evidence, keeping the same no-path,
no-filename, no-image-content boundary.

Follow-up, 2026-06-09: existing color-cast, edge-shadow, and corner-shadow smoke
fixtures are now formal public-safe gates. The smoke must show at least one
applied file plus aggregate before/after deltas for paper color-cast
normalization, edge-shadow cleanup, and corner-shadow cleanup.

Follow-up, 2026-06-09: paired vertical/horizontal dark-edge scanner shadows now
have a narrow conservative trim path. It requires balanced continuous opposite
edges, stable light paper immediately inside the edge, and protected-content
boundary checks. The public-safe smoke now requires at least one dark-border trim
and aggregate max-trim-margin evidence without publishing paths or filenames.
Follow-up, 2026-06-09: the public-safe smoke now also includes a scanner-gutter
fixture and requires at least one scanner-gutter trim. The quality summary
publishes the aggregate `scanner_gutter_max_trim_margin_ratio` metric so external
validation can distinguish gutter trim evidence from other geometry trims.
Follow-up, 2026-06-09: the public-safe smoke now also includes a small-angle
skewed text fixture and requires at least one conservative deskew. The quality
summary publishes aggregate `deskew_abs_angle_degrees` evidence so CI can
distinguish actual geometry correction from a merely enabled deskew option.

### M3：Photo/mixed-safe 管线 v1

目标：让照片、图文混排、印章和批注不会被文字清洁策略误伤。

任务：

- 增加照片/图像块、印章/批注、彩色区域、表格线和装订边保护候选。
- 模板默认对混合内容降级为保守处理。
- review queue 把“可读性提升”和“原貌风险”分开分组。
- 输出 profile 支持同源图的保守版和利用版并存。

验收：

- mixed/photo fixtures 不触发强背景清理或强锐化。
- 被保护区域的色彩偏移和结构变化受限。
- 本地 review package 能按风险分组展示候选。

Implementation note, 2026-06-09: `photo-mixed-safe-v1` now has an end-to-end
production CLI contract test using a synthetic mixed photo/stamp/table page.
The test proves that production summaries and processing manifests keep
normalize-tones, strong background cleanup, bleed-through cleanup, faded-text
enhancement, and text-edge sharpening disabled for the mixed-safe template while
still preserving source bytes and writing a public-safe quality summary.
Follow-up, 2026-06-09: `image-processing-capability-smoke` now also publishes a
protected mixed-content fixture check in the public-safe quality baseline. It
reports aggregate changed-pixel ratio, color mean absolute delta, and edge
energy delta ratio with limits, without paths or filenames. Risk-grouped local
processing review packaging now separates background cleanup, readability
improvement, defect cleanup, and original appearance risk groups. Local-only
operator preview authorization is now available through service job preview
endpoints, and review actions now append to local-only job review history while
publishing only aggregate history counts; richer review UI remains follow-up
work.

### M4：服务 API 和状态恢复

目标：把质量处理从 CLI 能力演进到稳定后台任务。

任务：

- `POST /api/jobs` 支持模板 ID、输入授权、输出策略和 worker 限额。
- `GET /api/jobs/{job_id}` 返回 public-safe 状态、进度、质量聚合和风险代码。
- `POST /api/jobs/{job_id}/retry` 只允许失败、中断或可恢复 job 显式重试，并复用已完成派生图。
- `POST /api/jobs/{job_id}/cancel` 支持取消并写入明确终态。
- 服务重启后扫描 checkpoint，恢复终态和 stale running job。
- API 不返回本地敏感路径；本地复核资源必须走受控 local-only 通道。

验收：

- 前端或外部系统无需读取文件系统即可判断 job 是否完成、失败、取消或可恢复。
- 服务重启后 public summary 与 checkpoint 一致。
- 并发 job 的路径、模板、状态和输出完全隔离。

Implementation note, 2026-06-09: `archive_scan_qc.service_api` provides the
endpoint-shaped core for health, capabilities, job create, job index, job
status, and cancellation. `archive_scan_qc.service_http` now exposes the first
local-only HTTP transport behind `archive-scan-qc service-api`. The transport
keeps `service_root` server-owned, returns sanitized public-safe errors, and is
covered by HTTP tests for create/status/cancel/index responses without leaking
paths or filenames. Follow-up, 2026-06-09: `POST /api/jobs/{job_id}/run`
triggers the existing production runner synchronously and returns the terminal
public summary with aggregate quality fields. Follow-up, 2026-06-09:
`POST /api/jobs/{job_id}/start` now starts the same production runner in a local
background thread and returns a `running` public summary immediately. Active
in-process async jobs stay `running` while the service is alive; stale running
checkpoints after restart still recover as `needs_recovery`. Serving local-only
review resources has landed through a fixed local-only artifact allowlist.
Follow-up, 2026-06-09: async service start now enforces an in-process
`max_active_async_jobs` limit before marking a job `running`, and the service
capabilities response exposes that non-sensitive limit alongside the per-job
worker limit.
Follow-up, 2026-06-09: async service reservation now also enforces a
non-sensitive `max_active_workers` limit before marking a job `running`; jobs
rejected by the global active-worker quota remain in their prior public state.
Follow-up, 2026-06-10: when a service job omits `workers`, creation now computes
a bounded `workers_scheduled` value from input image count plus CPU/memory
heuristics, stores it in the private job record, and publishes only public-safe
resource summary fields. Async scheduling deducts this scheduled worker count
from `max_active_workers`; explicit worker requests still take precedence.
Follow-up, 2026-06-09: service job creation now checks configured minimum
service-root free space, and job start checks the isolated temp directory
against `max_tmp_bytes_per_job` before entering `running`.
Follow-up, 2026-06-09: `POST /api/jobs/{job_id}/retry` now provides a
synchronous explicit retry boundary for `failed`, `interrupted`, and
`needs_recovery` jobs while keeping ordinary terminal reruns rejected.
Public service summaries now also expose aggregate retry/reuse counts so
external schedulers can verify resume behavior without reading private
processing or retry manifests.
They also expose a public-safe retry status block with attempt number and
resume/reuse booleans, while retry file lists remain private.
Follow-up, 2026-06-09: the same local HTTP transport now exposes
`GET /api/rule-templates` and `GET /api/rule-templates/{template_id}` for
public-safe template catalog/detail responses without reading scan reports or
writing derivative images.
Follow-up, 2026-06-09: terminal service job public summaries now carry
public-safe quality context copied from `processing_quality_summary.json`,
including blocking codes, warning/retry counts, per-category changed-file
counts, quality-operation category booleans, whitelisted aggregate quality
metrics including changed-pixel ratios and brightness/contrast deltas, and
aggregate guardrail status.
This lets API clients judge completion quality without opening private
production summaries or path-bearing checkpoints.
Follow-up, 2026-06-09: service job roots now reserve an isolated `review`
subdirectory alongside `metadata`, `derivatives`, `tmp`, `checkpoints`, and
`logs`. Public summaries expose only the review isolation boolean; local review
packages remain path-bearing local-only artifacts.
Follow-up, 2026-06-10: the isolated `logs` directory now stores a local-only
`service_job_event_log.json` for service state/review events. Public summaries
publish only aggregate event count/latest event metadata and never expose event
rows or log paths.
Follow-up, 2026-06-09: completed service jobs now write the local-only
processing review package and production review queue into that isolated
`review` directory. Public summaries expose only review availability and
aggregate queue counts, so API clients do not need to open path-bearing review
files to know whether operator review material exists.
Follow-up, 2026-06-09: the local processing review package now separates
background cleanup, readability improvement, defect cleanup, and original
appearance risk groups. Service public summaries expose only aggregate group
counts so operator review planning does not require reading row-level package
content.
Follow-up, 2026-06-09: terminal service job recovery now regenerates missing
local review artifacts from existing production metadata when possible, then
refreshes only the public-safe review availability and aggregate group counts.
Follow-up, 2026-06-10: root-level service recovery now keeps valid jobs
recoverable even when another checkpoint is invalid or unreadable. The public
index exposes only skipped counts and aggregate recovery issue codes, not
skipped job IDs, local paths, checkpoint rows, or exception messages.
Follow-up, 2026-06-10: the same public index now includes nested quality
aggregates for quality availability, status counts, quality-signal status
counts, aggregate file counts, and blocking-code counts, without exposing job
IDs, paths, filenames, hashes, quality rows, OCR text, thumbnails, or image
content.
Follow-up, 2026-06-10: the public index now also includes nested source-integrity
aggregates for checked/unchanged/modified/missing/added source-image counts and
source-change booleans, without exposing file lists, paths, filenames, hashes,
OCR text, thumbnails, or image content.
Follow-up, 2026-06-10: `GET /api/production/session` now mirrors only those
root-level aggregate quality/source-integrity/recovery blocks plus job/state
counts. It no longer embeds per-job summaries or job IDs in the session view.
Follow-up, 2026-06-09: the service API now exposes a local-only review artifact
reader at `GET /api/jobs/{job_id}/local-review/{artifact_id}`. It accepts only
`processing-review-package` and `production-review-queue`, validates the
resolved path inside the isolated job `review` directory, and marks responses
as sensitive `public_safe=false` payloads. The HTTP transport now rejects
non-loopback bind hosts.
Follow-up, 2026-06-09: missing service job checkpoints now return explicit
public-safe 404 `job_not_found` errors, separate from 400 `input_dir_missing`
for invalid create-job inputs.
Follow-up, 2026-06-10: invalid service job IDs now return explicit public-safe
400 `invalid_job_id` errors. HTTP responses do not echo the rejected ID, input
path, or filename, and the core creation path does not create a job directory for
the rejected ID.
Follow-up, 2026-06-09: missing service-managed custom rule templates now return
explicit public-safe 404 `rule_template_not_found` errors. Job creation validates
the requested custom template before writing job directories or checkpoints, so
failed template references do not create stale recoverable jobs.
Follow-up, 2026-06-10: service job public summaries now expose the resolved
public-safe `processing_profile` in their template block. Checkpoints still hold
the private template snapshot for recovery, while public summaries expose only
template IDs, processing mode, and profile enum without local paths, filenames,
hashes, OCR text, thumbnails, image content, or template rule rows.
Follow-up, 2026-06-10: legacy service-job checkpoints that do not yet contain
`processing_profile` are recovered by deriving the public-safe profile from the
built-in template ID. `print-clean-v1` recovers as `print_clean`; custom or
unknown snapshots fall back to `standard`.
Follow-up, 2026-06-10: service-job checkpoint, per-job public summary, and root
index public summary writes now use same-directory temporary JSON files followed
by atomic replace. This prevents async status polling from reading empty or
partially written state files while a worker is updating checkpoints.
If a legacy or externally damaged checkpoint JSON cannot be parsed, single-job
recovery returns a validation error and root-level recovery publishes only the
aggregate `invalid_checkpoint_json` issue code without paths or exception text.
Follow-up, 2026-06-09: the service API now also exposes a production-worker
facade around the same job boundary: session, setup, async start, progress,
aggregate review-queue availability, and finish/export readiness. It remains
public-safe. Review actions now persist local decision summaries and aggregate
verification under the job's isolated review directory, append to local-only
review history, and publish only aggregate history counts. Dedicated
review-history endpoints now let clients refresh that aggregate summary without
reading local history rows. Local-only preview resources now resolve authorized
`job_id`/`local_id`/`source` requests without publishing paths or filenames.
Follow-up, 2026-06-09: service job public summaries now include nested
`scan-qc.service-job-public-timings.v1` timing context. The service layer
filters production-run stage timings, aggregate processing throughput, and
per-operation timings through fixed public allowlists so API clients can monitor
quality work cost without opening private production summaries or echoing
unknown strings from checkpoint/progress files.
Follow-up, 2026-06-09: service job runs now perform an in-memory source-image
hash snapshot before and after production processing. Public summaries expose
only nested `scan-qc.service-job-source-integrity.v1` aggregate counts and
source modification booleans; they do not store or return hashes, filenames, or
file lists.
Follow-up, 2026-06-09: service-job boundary regression now forces two async
jobs into the production runner concurrently and verifies per-job isolation for
state, metadata, derivatives, template snapshots, processing manifests, local
review artifacts, source-integrity counts, and public-safe summaries.
Follow-up, 2026-06-10: async cancellation race coverage now verifies that a
running job remains `cancelled` after the background worker finishes and still
refreshes public-safe source-integrity aggregates without exposing private
paths or filenames.
Follow-up, 2026-06-10: the production `finish-export` facade now exposes
public-safe `blocking_codes` plus a `retryable` boolean. Recovered
`needs_recovery` jobs report `job_needs_recovery` and `retryable=true`, so
external schedulers can choose the explicit retry endpoint without reading
private checkpoints or local logs.
Follow-up, 2026-06-10: service API regression now also runs a completed
`print-clean-v1` job with localized background-stain cleanup and verifies that
job status plus production session summaries expose only public-safe
`print_clean` profile, job-level aggregate stain metrics, and session-level
quality status counts, with no private paths, filenames, or source hashes; the
session summary still omits job lists and job IDs.
The local HTTP transport has matching coverage for
`POST /api/jobs/{job_id}/run` plus `GET /api/production/session`, so external
callers observe the same public-safe quality/status boundary.
Core recovery/index regression now covers the same `print-clean-v1` cleanup:
recovered job summaries preserve job-level aggregate stain metrics, while the
root public index's nested quality aggregate keeps only status counts and omits
quality rows, paths, filenames, and hashes.
Follow-up, 2026-06-10: `public_capability_contract.json` now includes a
machine-readable `service_contract.public_surfaces` block for the same quality
boundary: job summaries may expose `print_clean` profile and whitelisted
aggregate quality metrics, while production-session and root-index quality
aggregates prohibit job IDs, processing profiles, quality rows, and
`quality_metrics`.

### M5：性能和后端实现

目标：在质量提升成立后，再优化速度和依赖。

任务：

- 把可证明同语义的热点迁移到 NumPy/OpenCV。
- 对大图读写引入 libvips 或 tile-based 策略，但输出语义必须保持一致。
- worker 推荐进入服务调度，限制总并发和 per-job workers。
- 对每个操作记录 timing，形成质量收益/耗时比。
- CPU/Pillow fallback 保留，直到可选后端通过真实样本聚合验证。

验收：

- 质量指标不低于 Pillow baseline。
- 后端切换不改变 public schema 和模板语义。
- 处理吞吐不低于当前私有样本基线，失败率不升高。

Implementation note, 2026-06-10: performance backends remain optional and
bounded. `despeckle` has fallback, NumPy, and OpenCV paths with parity/fallback
tests and public-safe backend timing summaries; libvips remains an explicit
optional image IO backend. Service jobs now carry auto worker scheduling into
the job boundary through `workers_scheduled`, while CPU/Pillow fallback remains
the stable default until private aggregate validation supports a backend
default change.

### M6：生产验收和发布门槛

目标：让“图像变好”成为发布门禁，而不是人工口头判断。

任务：

- 扩展 `core-image-processing` CI：快速 synthetic quality regression。
- 保留 350 秒级深度图像回归为 `DEEP_FULL_ONLY_TESTS`，只在定时 deep-full、手动 deep-full 或本地发布门禁运行。
- 增加 private validation 聚合报告：只公开分组指标和风险代码。
- release checklist 增加质量收益、过处理风险、源文件安全、恢复和 public-safe 检查。

验收：

- 发布前必须有 synthetic quality regression 和私有样本聚合验证。
- 质量收益、过处理风险、处理失败、隐私自检和 cleanup 均有明确结果。
- 任何算法默认值变化必须更新模板版本和 migration note。

Implementation note, 2026-06-10: `archive-scan-qc private-validation-aggregate`
now writes `private_validation_aggregate_summary.json` with schema
`scan-qc.private-validation-aggregate.v1`. It reduces operator-approved private
validation JSON inputs to public group IDs, aggregate item counts, allowlisted
quality metric summaries, quality-signal status counts, and risk-code counts.
Raw private labels, paths, filenames, hashes, OCR text, row-level rows,
thumbnails, and image bytes remain local.

## 6. 第一批建议 Issue

1. 定义 `processing_quality_summary.json` schema 和 public-safe 字段。
2. 增加质量 synthetic fixture 生成器：灰底、阴影、扫描线、透印、低对比文字、混合内容。
3. 扩展 image-processing capability smoke，使其报告质量收益聚合指标。
4. 把 service job 目录隔离接入质量处理输出：derivatives、metadata、temp、review、logs。
5. 增加源文件 hash 不变、输出目录冲突和 stale running 恢复测试。
6. 定义模板 schema 和四个内置模板。
7. 实现模板 dry-run，输出处理计划和风险提示。
8. 实现 text-clean 背景估计和灰底/泛黄归一。
9. 实现低对比文字增强和轻量锐化 guardrail。
10. 实现边缘/角落/折痕阴影弱化。
11. 强化扫描线检测和弱化，保护表格线。
12. 实现保守透印候选弱化。
13. 增加照片/图像块、印章、批注和彩色区域保护候选。
14. 实现本地 before/after review package 分组，不嵌入图片字节。
15. 增加 public-safe quality regression 到 CI 分组。
16. 将 quality job 状态接入服务 API MVP。
17. 增加取消、恢复、重试和并发隔离 API 测试。
18. 将已验证热点迁移到 NumPy/OpenCV，并保留 Pillow fallback。
19. 更新 release checklist 的图像质量发布门槛。
20. 用私有样本跑模板对比，只提交聚合指标和风险代码。

## 7. 验证矩阵

- 文档变更：文档 diff 审查。
- 模板 schema 变更：模板单测、非法参数测试、dry-run 测试。
- 质量指标变更：synthetic quality regression、public-safe schema 测试、隐私泄露测试。
- 图像算法变更：核心图像专项、质量收益测试、过处理 guardrail、私有样本聚合验证。
- 服务 job 变更：路径隔离、checkpoint、恢复、取消、并发、public summary 测试。
- CLI/API 边界变更：生产 CLI、服务 API、隐私边界和外部验证分组。
- 性能后端变更：backend parity、timing、fallback、真实样本聚合验证。

## 8. 不做或暂缓

暂不优先做：

- 原图原地修改。
- 云端上传或网络修图。
- 生成式图像修复。
- 默认强二值化覆盖所有材料。
- 未经模板和复核的自动强清洁。
- 把 OpenCV/GPU/model 后端直接暴露成稳定公开契约。

这些能力即使后续需要，也必须经过模板、job 边界、public-safe 摘要、真实样本聚合验证和发布门禁。

## 9. 文档维护规则

- 新增图像算法时，同步更新本文档、`docs/development-plan.md`、运维说明和 public capability contract 中的稳定/实验边界。
- 新增公开 CLI/API 或 schema 时，同步更新 release checklist 和 public capability contract。
- 新增处理模板时，必须写明目标材料、默认处理阶段、风险边界、验证样本和迁移策略。
- 每次私有样本验证只提交聚合指标，不提交样本路径、文件名、hash、缩略图、OCR 文本或图片内容。
