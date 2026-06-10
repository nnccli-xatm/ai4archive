# 双层 PDF/OCR 目录方案

日期：2026-06-10

本文档是 DEV-403 的工程方案，不表示当前稳定 CLI/API 已内置 OCR、PDF/OFD 转换或双层 PDF 生产能力。结论先行：这些能力后续应作为 local-only、path-bearing、可复核的生产扩展工具进入后台 job 边界；在真实样本聚合验证和人工复核闭环完成前，不进入默认图像处理、public-safe 摘要或稳定服务契约。

## 目标

- 从已排序 JPG/PNG/TIF 页序生成可检索双层 PDF。
- 对 PDF/OFD 转换建立可替换 provider 边界，而不是把某个外部程序写死为核心依赖。
- 从 OCR 结果中提取目录候选字段，默认关注文件编号、题名、页码或日期等可复核字段。
- 低置信度、冲突、跨页合并和结构不确定结果必须进入人工复核。
- public-safe 输出只发布聚合计数、状态和风险代码，不发布 OCR 文本、文件名、路径、hash、缩略图、图片内容或行级目录项。

## 非目标

- 不做云端 OCR 或网络上传。
- 不把 OCR 文本写入 public-safe summary。
- 不在当前图像质量处理管线中默认运行 OCR。
- 不把 OCR 目录结果直接作为无需人工确认的生产目录。
- 不把 PDF/OFD provider 的 stderr/stdout 原文暴露给公开摘要。

## 推荐架构

### 1. 输入与页序

输入应来自已经完成边界检查的本地 job 或显式 CLI 参数：

- `input_dir`：本地图片目录，按文件名稳定排序，或显式 manifest 页序。
- `case_split_plan.json` 或未来页序 manifest：可作为分件后的页范围依据。
- `template_snapshot`：记录 OCR/PDF 工具版本、参数、语言、DPI、阈值和复核策略。

源图片仍然只读。所有 PDF、OCR sidecar、目录候选和复核包写入独立输出目录。

### 2. 双层 PDF 生成

建议拆成三个可替换阶段：

- 图像页规范化：只读输入，必要时复制到 job tmp，记录 DPI、尺寸、色彩模式和页序。
- OCR sidecar：provider 生成 hOCR/ALTO/PDF text layer 坐标，不把全文写入 public-safe。
- PDF 合成：把原图页和 OCR text layer 合成双层 PDF，输出 local-only manifest。

候选 provider：

- `ocrmypdf`/Tesseract：适合本机 CPU、依赖清晰、可生成双层 PDF。
- PaddleOCR/ONNX sidecar：适合后续 GPU/版面模型探索，但只能作为 optional provider。
- 自研 provider adapter：标准输入输出只交换 local-only job 内路径和聚合状态，不进入公开摘要。

首批验收应只要求 provider probe 和本地 synthetic smoke，不承诺默认生产质量。

### 3. PDF/OFD 转换

PDF/OFD 转换应作为独立 provider adapter：

- 输入、输出和 tmp 必须在 job 隔离目录内。
- provider 命令、版本和退出码记录在 local-only manifest。
- public-safe 只输出 `converted_count`、`failed_count`、`provider_status_counts` 和 allowlisted risk codes。
- 失败时保留可重试状态，不删除已成功产物。

OFD 转换在依赖、授权和平台差异明确前，不作为稳定默认能力。

### 4. OCR 目录提取

目录提取不应直接消费完整 OCR 文本进入公开输出。建议生成两层产物：

- local-only `ocr_directory_candidates.json`：包含页码、候选字段、文本片段、置信度、来源坐标和复核状态。
- public-safe `ocr_directory_summary.json`：只包含候选数量、字段覆盖率、置信度分桶、冲突计数、复核状态计数和 blocking codes。

首批字段建议：

- `file_number`：文件编号或档号候选。
- `title`：题名候选。
- `start_page` / `end_page`：目录行对应页码候选。
- `date`：可选日期候选。
- `confidence_bucket`：`high`、`medium`、`low`、`unknown`。

低置信度或冲突条件：

- 平均置信度低于阈值。
- 同一页出现多个不一致编号/题名。
- 页码范围倒置、越界或重叠。
- OCR 行跨页合并不确定。
- 目录候选与人工 Excel/manifest 页序冲突。

这些情况必须进入本地 review queue，不能自动交付。

## CLI/API 形态建议

后续最小实现可以拆成四个 local-only 命令：

- `ocr-provider-probe`：只探测本机 provider，不运行 OCR。
- `searchable-pdf-plan` / `searchable-pdf-apply`：生成双层 PDF 计划并执行。
- `ocr-directory-plan` / `ocr-directory-apply`：从 OCR sidecar 生成目录候选和复核包。
- `pdf-ofd-convert-plan` / `pdf-ofd-convert-apply`：PDF/OFD 转换计划和执行。

服务化后，这些命令应进入 service job 的扩展工具队列，复用 job 隔离、资源配额、取消、恢复、review history 和 public-safe summary 模型。

## Public-Safe 边界

允许公开：

- job 状态、总页数、生成 PDF 数、转换成功/失败数。
- OCR provider 是否可用、语言配置是否匹配、置信度分桶。
- 目录候选数量、字段覆盖率、冲突计数、复核状态计数。
- allowlisted blocking/risk codes。

禁止公开：

- OCR 全文、目录行文本、题名、编号、文件名、路径、hash、缩略图、图片内容。
- provider 原始日志、命令行中的私有路径、模型 prompt、坐标行级证据。
- 未复核的目录候选明细。

## MVP 验收

- synthetic 页序可生成双层 PDF 或明确 provider unavailable。
- OCR provider 不可用时命令返回可恢复状态，而不是失败堆栈。
- 目录候选 summary 不含 OCR 文本、路径、文件名、hash 或行级记录。
- 低置信度和冲突候选进入 local-only review queue。
- 中断/重试复用已完成页和 sidecar，不覆盖源文件。
- release checklist 增加 provider 探测、隐私检查、低置信度复核和真实样本聚合验证。

## 当前结论

DEV-403 当前完成的是边界方案和验收设计。后续实现必须先做 provider probe 和 plan/apply 骨架，再接入真实 OCR/PDF provider；不能直接把 OCR/PDF/OFD 功能作为默认图像处理能力暴露给生产 CLI 或服务 API。
