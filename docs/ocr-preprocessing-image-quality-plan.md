# OCR 预处理图像质量专项开发计划

日期：2026-06-10

## 1. 问题结论

当前系统的图像处理能力没有达到“显著改善扫描图片质量、提升 OCR 输入质量”的目标。2026-06-10 使用真实 UCI NoisyOffice simulated noisy grayscale 重新验证，216 张图全部稳定处理完成，但质量指标没有改善：

- PSNR：15.8252 -> 15.8241，变化 -0.0011 dB。
- SSIM：0.893610 -> 0.893542，变化 -0.000068。
- MSE：1939.028962 -> 1939.365160，变差 +0.336198。
- 暗像素 F1：0.895165 -> 0.895099，变化 -0.000066。
- 216 张中，PSNR 仅 1 张改善、93 张不变、122 张变差。
- 实际处理主要是极保守 `despeckle`，平均像素变化率只有 0.000025；背景归一、文字增强、边缘锐化等 OCR 预处理关键操作基本没有生效。

这说明现有 `text-clean-print`/保守档案派生图路线不能满足 OCR 预处理要求。后续必须新增独立的 OCR 预处理 profile 和验收门槛，而不是继续微调现有保守模板。

## 1.1 当前实现进展

2026-06-10 已完成第一版专项实现，`ocr-preprocess-v1` 不再沿用通用档案式
`despeckle` 作为前置默认处理，而是在 OCR profile 内做灰度背景归一、暗前景及
相邻中灰笔画保护，并输出 `ocr_binary` sidecar。该 profile 仍然是显式利用副本，
不作为保真派生图默认模板。

真实 NoisyOffice final gate：

- 命令：`python scripts\run_noisyoffice_external_cli_test.py --data-root <local NoisyOffice root> --rule-template ocr-preprocess-v1 --enforce-ocr-quality-gate --no-doc-report --no-download`
- 样本：216 张 UCI NoisyOffice simulated noisy grayscale。
- 结果：stable CLI pass，源图修改 0，输出缺失 0，尺寸不匹配 0。
- PSNR：15.8252 -> 22.5287，提升 +6.7035 dB。
- SSIM：0.893610 -> 0.937759，提升 +0.044149。
- MSE：1939.028962 -> 693.842889，降低 64.217%。
- MAE：37.260796 -> 13.543558，降低 63.652%。
- 暗像素 F1：0.895165 -> 0.895165，不下降。
- 前景保留率：0.984674 -> 0.984674，不下降。
- 噪声分组：c/f/p/w 四组全部 PSNR 和 SSIM 正向改善。
- public-safe 摘要：`ocr_preprocessing_quality_summary.json`，只包含聚合指标和 gate
  状态，不包含路径、文件名、hash、OCR 文本、图片内容或行级记录。

Synthetic OCR gate：

- 命令：`python -m archive_scan_qc.cli ocr-preprocessing-ocr-validation --provider tesseract --require-ocr-metric --min-cer-relative-reduction 0.25 --min-wer-relative-reduction 0.0 --out generated\ocr_preprocessing_ocr_validation`
- provider probe：本机 Tesseract 可用；默认命令仍为 provider disabled，不自动调用外部 OCR。
- 结果：3 页 synthetic known-text，source CER/WER macro 均为 1.0，处理后 CER/WER macro
  均为 0.0，CER/WER 相对下降 100%。
- 摘要：`ocr_preprocessing_ocr_validation_summary.json` 不写入 expected text、OCR 输出文本、
  路径、文件名、hash、图片内容或行级记录。

服务化边界：

- `ocr-preprocess-v1` 已接入 production CLI、run-plan、service job create/run/recover/retry
  所需的 profile/options/manifest。
- service job public summary 只公开 allowlisted OCR 聚合质量指标，如
  `ocr_background_delta`、`ocr_foreground_retention_ratio`、`ocr_binary_foreground_ratio`。
- 私有样本聚合 allowlist 已加入 CER/WER 聚合指标，不接收或输出 OCR 文本。

真实扫描样本回归，2026-06-11：

- 本机 local-only 真实扫描样本 12 张暴露第一版 OCR profile 的目标偏差：旧逻辑只对 2/12
  生成 OCR 增强和二值 sidecar；8/12 因轻微彩色痕迹被 `protected_color_content`
  硬跳过，2/12 近白低对比页面因背景分离不足未增强。
- 修复后 `ocr-preprocess-v1` 不再把彩色痕迹作为 OCR 利用副本的硬拦截；轻微彩色/压缩彩边不复核，
  仅高彩或红色占比较高的页面进入 OCR review reason。保真派生图的颜色保护逻辑不变。
- 新增低对比前景增强路径：对近白纸面中的稀疏淡字估计前景阈值，压暗前景、推白背景，
  并明确禁止抬亮原本已经很暗的像素。
- 真实扫描样本复测结果：12/12 生成 OCR 灰度增强，12/12 生成 `ocr_binary` sidecar；
  平均 `ocr_preprocess_changed_pixel_ratio` = 0.217934，最大 0.598217；
  reason 分布为 4 张 `applied_low_contrast_foreground_enhancement`、8 张
  `applied_background_normalization`；`ocr_foreground_dark_loss_ratio` 和
  `ocr_foreground_dark_lift_ratio` 最大值均为 0，前景保留率为 1.0。
- 该批次 production status 仍为 `needs_review`，原因来自扫描 QC 的 P0 复核项；OCR 处理本身
  失败 0、guardrail failure 0、processing warning 0。

真实扫描样本二次修复，2026-06-11：

- 第一张极稀疏手写数字页在初版低对比增强中出现点状/水波纹伪影，原因是近白纸面下
  `p95 - separation` 阈值过宽，把纸面微纹和压缩浅灰噪声纳入淡前景。修复后对
  `p01 >= 200`、`p05 >= 240`、`p95 >= 245` 的极稀疏近白页使用 `p01` 附近的窄阈值，
  只增强真实前景，不再把大面积浅纹理压暗。
- OCR profile 新增专用纠偏边界：可用 OCR 预增强图重新检测低对比页倾斜，并对表格/横线页
  使用 form-line projection fallback；颜色、表格线和边缘内容保护仍保留给保真派生图，
  但不再阻止 OCR 利用副本纠偏。
- 真实扫描样本复测结果：12/12 生成 OCR 灰度增强，12/12 生成 `ocr_binary` sidecar；
  11/12 实际纠偏，剩余 1 张检测角度低于 0.2 度阈值未旋转；失败 0、guardrail failure 0、
  processing warning 0，源图修改 0。

## 2. 目标重新定义

新增目标不是“看起来更干净”，而是生成面向 OCR 的利用副本：

- 明显压低扫描噪声、灰底、背景纹理、透印、扫描线和光照不均。
- 提升文字/背景分离度、笔画连续性、局部对比度和 OCR 可识别性。
- 支持灰度增强输出和二值/准二值 OCR 输出两类派生图。
- 对照片、印章、彩色批注、表格线和原貌保护材料，不默认使用 OCR 强处理。
- 源图只读；OCR 预处理输出是独立派生图，不覆盖保真派生图。

新增 profile：

- `ocr-preprocess-v1`：面向 OCR 的强预处理，允许背景重建、局部阈值、去噪、文字增强和可复核二值化。
- `ocr-preprocess-light-v1`：较保守 OCR 预处理，用于轻噪声或混合版面。
- `archival-safe-v1`、`photo-mixed-safe-v1` 继续以保真为目标，不承担 OCR 最大化目标。

## 3. 达成标准

不能只用合成 smoke 证明“能跑”。每个 OCR 预处理能力必须同时通过稳定性、图像质量和 OCR 实测三类门槛。

### 3.1 稳定性门槛

- 真实 NoisyOffice：216 张全部处理成功，输出缺失 0，失败 0，源图修改 0。
- Windows 中文路径、空格路径、长路径输入通过。
- `production-run`、service job 和 retry/recovery 均能保留 OCR 预处理 manifest。
- public-safe summary 不包含路径、文件名、hash、OCR 文本、缩略图、图片内容或行级候选。

### 3.2 图像质量门槛

NoisyOffice simulated noisy grayscale 作为第一阶段强制门槛：

- `ocr-preprocess-v1` 灰度增强输出：PSNR macro 至少比 noisy 输入提升 +1.0 dB；目标 +2.0 dB。
- SSIM macro 至少提升 +0.015；目标 +0.03。
- MSE macro 至少降低 10%；目标 20%。
- MAE macro 至少降低 5%；目标 12%。
- 暗像素 F1 不得低于 noisy 输入，目标提升 +0.01。
- 前景保留率不得下降超过 0.002；目标不下降。
- 每个噪声类型分组都不得整体变差；至少 3/4 噪声类型达到 PSNR 正提升。

DIBCO/H-DIBCO 或等价二值化基准作为第二阶段门槛：

- F-measure、pseudo-F-measure、PSNR 或 DRD 至少一个核心指标明显优于输入 baseline。
- 二值 OCR 输出不得用灰度 PSNR 单独判定，必须看二值化指标和 OCR 结果。

### 3.3 OCR 实测门槛

OCR 预处理最终以 OCR 效果验收：

- synthetic known-text fixture：CER 相对下降至少 25%，clean 页面 CER 回归不超过 2%。
- 私有样本聚合验证：OCR 可读性分组中，CER/WER 或人工复核通过率必须有统计显著改善。
- 低置信度、版面混排、印章/批注、表格线损伤风险进入 local-only review queue。

## 4. 处理管线设计

OCR 预处理管线必须独立于保真派生图管线，输出 `ocr_derivatives/` 或 manifest 中明确的 `output_profile=ocr_preprocess`。

### 4.1 版面与文字区域建模

- EXIF transpose、DPI/尺寸标准化、灰度转换。
- 小角度 deskew，低置信度不旋转。
- 内容 bbox、页边、装订边、表格线、照片/印章/彩色区域检测。
- 文字前景 mask、背景 mask 和保护 mask 分离。

### 4.2 背景估计和光照校正

- 大尺度背景估计：形态学 opening/closing、低分辨率背景场、tile percentile。
- 光照场归一：按 tile 平滑校正，压低灰底和阴影。
- 泛黄/低饱和纸面归一：只作用于背景候选，不压扁文字笔画。
- 输出指标：背景均匀度、背景污染面积、局部亮度方差、前景对比度。

### 4.3 去噪和纹理抑制

- 从孤立 speckle 扩展到 OCR 场景下的点噪声、椒盐噪声、轻纹理噪声。
- 可选 OpenCV fastNlMeans、median/bilateral、connected-component filtering。
- 引入“文字笔画保护”：细笔画、标点、页码和小字号不能当作噪声删除。
- 输出指标：噪声候选面积、删除像素比例、连通域变化、笔画断裂风险。

### 4.4 自适应阈值和准二值化

- 支持 Sauvola/Wolf/Niblack 或 OpenCV adaptive threshold。
- 对低对比文字使用局部阈值；对干净页避免过处理。
- 输出两类结果：
  - `ocr_gray_enhanced`：灰度增强，用 PSNR/SSIM/MSE/MAE 验收。
  - `ocr_binary`：二值或准二值，用 DIBCO/OCR 指标验收。
- 失败或低置信度时保留灰度增强输出并标记 review required。

### 4.5 文字增强和边缘修复

- 局部对比增强：CLAHE 或受限局部拉伸。
- 细笔画增强：轻量 unsharp/形态学增强，避免光晕。
- 断笔风险控制：二值化后连通域数量、笔画宽度分布和前景保留率必须受控。

### 4.6 透印、扫描线和表格保护

- 透印识别只在低置信背景区域弱化，不处理正文/表格线。
- 扫描线按方向、宽度、连续性、颜色中性和文字接触关系判断。
- 表格线保留作为 OCR layout 重要信号，不得被强去噪删除。

## 5. 工程里程碑

### M0：质量基线和验收门槛

任务：

- 固化 NoisyOffice 当前负结果为 baseline。
- 扩展 `run_noisyoffice_external_cli_test.py`，支持比较不同 profile：`text-clean-print`、`ocr-preprocess-light-v1`、`ocr-preprocess-v1`。
- 增加 OCR 指标 harness：synthetic known text + 可选本机 OCR provider。
- 输出 `ocr_preprocessing_quality_summary.json` public-safe 聚合摘要。

完成标准：

- 没有达到 NoisyOffice +1.0 dB / SSIM +0.015 前，不允许把 OCR profile 标为完成。

### M1：OCR profile 和 manifest 边界

任务：

- 新增 `ocr-preprocess-v1` 和 `ocr-preprocess-light-v1` 模板。
- `ProcessingOptions` 增加 OCR 输出 profile，但不污染现有保真派生图默认行为。
- manifest 增加 `output_profile`、`ocr_preprocessing_operations`、`ocr_quality_metrics`、`ocr_review_required`。
- service job public summary 只公开聚合质量指标和 blocking codes。

完成标准：

- 默认生产模板不变化。
- OCR profile 可通过 CLI/service job 显式启用。
- public-safe 隐私边界测试通过。

### M2：背景归一和光照校正

任务：

- 实现 tile-based 背景估计和光照场校正。
- 对灰底、阴影、泛黄、低对比文本分别建立 synthetic + NoisyOffice 分组验证。
- 加入前景 mask 保护和过处理回退。

完成标准：

- NoisyOffice PSNR/SSIM 产生正向改善。
- 背景均匀度提升，前景保留率不下降超过门槛。

### M3：OCR 去噪和笔画保护

任务：

- 引入 OCR 场景专用 denoise backend。
- 用 connected component 和 stroke-width 约束保护文字。
- 将大面积纹理噪声、椒盐噪声、扫描线分别建模，不再只做孤立 speckle。

完成标准：

- NoisyOffice 216 张中，至少 70% PSNR 或 SSIM 正向改善。
- 四类噪声分组均不整体变差。

### M4：自适应阈值和二值 OCR 输出

任务：

- 实现 `ocr_binary` 输出。
- 增加 DIBCO/H-DIBCO 或等价二值化指标。
- 建立二值输出 review gate：断笔、糊字、表格线损伤、印章/批注损伤必须可复核。

完成标准：

- 二值输出在 OCR 或二值化指标上明显优于输入。
- 不以灰度 PSNR 单独否定二值输出，但必须保留灰度增强质量门槛。

### M5：OCR 实测和真实样本聚合

任务：

- 接入本机 OCR provider probe，默认不运行外部 OCR。
- 在 synthetic known-text 上计算 CER/WER。
- 私有样本聚合只提交分组指标，不提交 OCR 文本或行级结果。

完成标准：

- OCR CER 相对下降至少 25%。
- clean/low-noise 页面回归不超过 2%。
- 低置信度结果进入 review queue。

### M6：服务化和发布门槛

任务：

- OCR profile 接入 service job setup/start/progress/review/finish-export。
- release checklist 增加 NoisyOffice、DIBCO/OCR、隐私、源图只读和 review gate。
- CI 只跑小 synthetic；真实 NoisyOffice/DIBCO 作为 release/private validation gate。

完成标准：

- 不达质量门槛不得标记 release ready。
- 不达 OCR 门槛不得把 OCR profile 标为生产默认。

## 6. 不再接受的完成口径

以下都不能算完成：

- 只证明 CLI 稳定、无失败、无源图修改。
- 只在 synthetic smoke 上有非零操作计数。
- 只增加开关，但没有 NoisyOffice/DIBCO/OCR 指标提升。
- 只对少数样本改善，宏平均和分组平均仍变差。
- 把 OCR 文本、路径、文件名或行级候选写入 public-safe summary。

## 7. 近期开发顺序

1. 先实现 `ocr-preprocess-v1` 模板和 manifest 边界。
2. 扩展 NoisyOffice runner 支持 profile 对比和阈值失败退出。
3. 做背景估计/光照校正，先追求灰度增强 PSNR/SSIM 正改善。
4. 做 OCR 专用 denoise 和笔画保护，解决当前只改 0.0025% 像素的问题。
5. 做自适应阈值和二值 OCR 输出。
6. 接入 OCR 实测和私有样本聚合。

## 8. 风险和约束

- OCR 预处理和档案保真派生图目标冲突，必须分 profile 输出。
- NoisyOffice clean GT 是灰度图，适合评价灰度增强；二值 OCR 输出要用二值化/OCR 指标单独评价。
- 强去噪可能破坏印章、批注、照片和表格线，必须通过模板和 review gate 控制。
- 可选 OpenCV/NumPy 后端可以用于实现，但默认能力必须有 fallback 或明确依赖声明。

## 9. 保证机制

工程上不以口头保证算法效果，而以准入门槛保证结果：后续 OCR 预处理相关 issue 只有在 NoisyOffice、二值化/OCR、隐私和源图安全门槛全部通过后，才能标记完成或进入 release ready。任何未达到 +1.0 dB PSNR、+0.015 SSIM 或 OCR CER 下降目标的实现，只能标记为实验分支或继续迭代，不能作为“图像质量显著改善”交付。
