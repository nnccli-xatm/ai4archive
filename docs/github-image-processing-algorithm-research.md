# GitHub 图像处理算法调研记录

日期：2026-06-11

本文档沉淀本轮围绕扫描图片 OCR 预处理质量提升所做的 GitHub/开源算法调研。目标不是罗列所有图像处理库，而是筛选能服务于当前产品目标的算法路线：真实扫描图片必须纠偏，不能破坏文字/表格线清晰度，输出尺寸和源文件安全必须可控，并且能通过 `processing_path` 架构与现有算法并列比较。

## 1. 调研结论

1. 当前应保留 `ocr-preprocess-leptonica-v1` 作为真实扫描样本的保守基线。它与 Leptonica/Tesseract 的 OCR 前处理思路一致：先保证纠偏、尺寸稳定、背景归一和笔画保护，再把强二值化放到 sidecar。
2. 下一条最值得实现的并列路径不是继续微调旧 `ocr-preprocess-v1`，而是新增 `ocr-preprocess-opencv-local-v1` 或类似 path：以 OpenCV/NumPy 为主体，组合 preserve-canvas deskew、背景估计、局部阈值、形态学保护和受控 denoise。
3. Sauvola/Niblack 适合做局部阈值候选，尤其是背景不均匀文本页，但不应直接替代灰度主图；更适合作为 `ocr_binary` sidecar 或候选 mask 生成器。
4. unpaper 的思路对黑边、双页、版面居中、deskew 和边缘清理有参考价值，但它会移动/重排页面内容，直接接入为生产依赖风险较高。短期应吸收其算法思想，不直接把 unpaper CLI 作为默认处理路径。
5. ocropy/kraken 的非线性二值化和历史文献 OCR 方向有参考价值，但依赖更重、目标更偏 OCR 引擎/历史文献训练系统，当前不适合作为默认图片处理路径。
6. 深度学习修复、超分、去模糊等路线暂不进入默认实现。它们可能改善观感，但更容易改变笔画结构，且需要模型、硬件、隐私和可解释性边界，不符合当前“文档 OCR 预处理工具”的近期目标。

## 2. 调研来源

| 来源 | 类型 | 相关能力 | 对当前项目的意义 |
| --- | --- | --- | --- |
| [Leptonica GitHub](https://github.com/DanBloomberg/leptonica) / [Leptonica 官网](https://www.leptonica.org/) | C 图像处理库 | OCR 常用底层图像处理、形态学、几何、二值化、分析工具 | 当前 `ocr-preprocess-leptonica-v1` 的主要参考方向；适合做保守 OCR 前处理基线 |
| [Tesseract ImproveQuality](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html) | OCR 官方质量建议 | DPI、二值化、噪声、膨胀/腐蚀、旋转纠偏、边框处理 | 明确“纠偏会显著影响 OCR 行分割”，支持把 deskew 作为强需求 |
| [OpenCV thresholding](https://docs.opencv.org/4.x/d7/d4d/tutorial_py_thresholding.html) | 官方算法文档 | 全局阈值、Otsu、自适应阈值 | 适合作为下一条 OCR binary/local threshold path 的基础 |
| [OpenCV morphology](https://docs.opencv.org/4.x/d9/d61/tutorial_py_morphological_ops.html) | 官方算法文档 | erosion、dilation、opening、closing、gradient、top-hat、black-hat | 可用于背景估计、笔画保护、噪声候选过滤；必须加 guardrail 防断笔 |
| [OpenCV denoising](https://docs.opencv.org/4.x/d5/d69/tutorial_py_non_local_means.html) | 官方算法文档 | fast non-local means 去噪 | 可作为轻量去噪候选，但参数过强会损伤小字边缘 |
| [OpenCV Hough lines](https://docs.opencv.org/4.x/d9/db0/tutorial_hough_lines.html) | 官方算法文档 | 直线检测、表格线/版面方向估计 | 可辅助表格页纠偏和结构保护，不宜单独作为所有页面 deskew 依据 |
| [unpaper GitHub](https://github.com/unpaper/unpaper) / [image-processing.md](https://github.com/unpaper/unpaper/blob/main/doc/image-processing.md) | 扫描页后处理工具 | 黑边清理、mask、deskew、灰度过滤、噪点处理 | 对边缘/双页/页面居中有参考价值；直接接入会带来内容移动和参数不可控风险 |
| [OCRmyPDF advanced docs](https://ocrmypdf.readthedocs.io/en/v11.7.0/advanced.html) | OCR 工具链文档 | 通过 unpaper 执行 clean/clean-final，强调安全默认参数 | 佐证 aggressive clean 需要人工复核，且 clean-before-OCR 与 clean-final 应分开 |
| [scikit-image Sauvola/Niblack](https://scikit-image.org/docs/0.25.x/auto_examples/segmentation/plot_niblack_sauvola.html) | Python 图像算法文档 | Niblack/Sauvola 局部阈值 | 适合背景不均匀文本识别；可作为二值 sidecar 的候选 |
| [ocropy nlbin](https://github.com/ocropus-archive/DUP-ocropy/blob/master/ocropus-nlbin) | 历史 OCR 二值化脚本 | 非线性二值化、背景估计、退化历史书页 | 适合作为后续研究路线；短期依赖和维护风险较高 |
| [kraken GitHub](https://github.com/mittagessen/kraken) | OCR 引擎 | 历史/多语种 OCR、layout、识别模型 | 更像 OCR 系统而非轻量图像处理库；不适合直接塞进当前图片处理主路径 |

## 3. 候选算法路线评估

### 3.1 Leptonica/Tesseract 风格保守 OCR 预处理

适配度：高，已实现为 `ocr-preprocess-leptonica-v1`。

核心思路：

- 默认尝试纠偏，但使用 preserve-canvas，避免输出像素尺寸膨胀。
- 灰度主图只做背景归一和前景笔画保护，不做 hard edge snap。
- 二值化输出作为 `ocr_binary/` sidecar，便于 OCR 或人工复核。
- 对低置信背景、结构损伤、尺寸风险保持保守。

当前状态：

- 真实扫描样本目检显示它是截止目前最佳结果：没有旧强处理路线中的水波纹、笔画碎裂、表格线扭曲或尺寸膨胀。
- NoisyOffice 上有明显 PSNR/MSE/MAE 改善，但 SSIM 尚未达 gate，且 `c` 噪声组仍有结构损失。

后续方向：

- 不替换该路径，继续作为 baseline。
- 针对 `c` 噪声组优化背景归一和局部纹理抑制。
- 在 path 层新增候选算法，与它同批对照。

### 3.2 OpenCV/NumPy 局部阈值与形态学路径

适配度：高，建议作为下一条独立 path。

候选 path：`ocr-preprocess-opencv-local-v1`。

核心组件：

- deskew：沿用当前 preserve-canvas deskew，必要时用 Hough/table-line 信息辅助表格页。
- 背景估计：大尺度 morphology opening/closing、tile percentile、局部亮度场。
- 局部阈值：OpenCV adaptive threshold 或 Otsu + 局部 mask 修正。
- 去噪：median/bilateral/fastNlMeans 作为候选，默认必须很弱。
- 结构保护：使用连通域、表格线 mask、文字邻域梯度和 soft-edge 指标做 guardrail。

优势：

- 当前项目已有 OpenCV/NumPy 后端基础，能在 Python 内部实现，不必引入外部 CLI。
- 每个阶段可记录质量指标和 timing，容易纳入 `processing_path` 对照。
- 可先只影响 OCR 利用副本，不污染保真派生图。

风险：

- adaptive threshold 容易把纸纹、压缩噪声、浅污渍吸进前景。
- morphology 过强会断笔或改变表格线。
- fastNlMeans 对文字边缘可能产生软化，必须设置边缘能量和连通域 guard。

验收要求：

- 输出尺寸必须等于源图。
- 默认纠偏不能省略。
- 文字边缘能量不得低于源图阈值。
- 表格线连续性不能下降。
- NoisyOffice 至少达到 Leptonica path 的宏平均 PSNR/MSE 改善，并补齐 SSIM gate。

### 3.3 Sauvola/Niblack 局部二值化路径

适配度：中高，适合作为 binary sidecar 或局部 mask，不适合作为灰度主图默认。

候选 path：`ocr-preprocess-sauvola-binary-v1`。

核心思路：

- 对背景不均匀页面按局部窗口计算阈值。
- 使用文字/表格线保护 mask 限制阈值输出。
- 二值图进入 `ocr_binary/`，灰度主图仍保守。

优势：

- 对阴影、灰底、局部光照不均的文字识别更有针对性。
- 参数少，容易做 NoisyOffice/DIBCO 指标对照。

风险：

- 手写稀疏页、浅纹理纸面和压缩噪声容易被误判为前景。
- 窗口大小和 k 值对不同 DPI 非常敏感。
- 单独看二值图可能“更黑更清楚”，但 OCR 或结构指标不一定更好。

验收要求：

- 必须用 DIBCO/H-DIBCO 或 synthetic binary/OCR gate 验收，不能只用灰度 PSNR。
- 低置信二值输出必须进入 review，不得覆盖灰度主图。

### 3.4 unpaper-inspired 扫描页清理路径

适配度：中，适合吸收局部算法，不建议直接作为默认依赖。

候选 path：`ocr-preprocess-unpaper-inspired-v1`。

可借鉴能力：

- 黑边/暗边清理。
- 双页或 mask 区域分别纠偏。
- 页面内容居中、边缘噪声过滤。
- 灰度区域过滤。

主要风险：

- unpaper 的部分能力会移动或重排页面内容，与当前“源图尺寸、内容位置、表格线稳定”的要求冲突。
- OCRmyPDF 文档也强调 aggressive unpaper 参数需要谨慎，且 clean-final 会影响最终图像。
- 作为外部 CLI 依赖时，Windows 部署、临时文件、错误恢复和 public-safe 摘要边界都要额外处理。

建议：

- 短期不直接接入 unpaper CLI。
- 先把“mask 内 deskew”“边缘 dark area 清理”“双页区域识别”拆成内部可测函数。
- 只在 `processing_path` 下作为实验路径，不进入默认模板。

### 3.5 ocropy/kraken 非线性二值化与历史文献路线

适配度：中低，适合后续研究，不适合近期生产默认。

参考价值：

- ocropy 的 `ocropus-nlbin` 面向退化历史书页，包含背景估计和非线性二值化思路。
- kraken 是完整 OCR 系统，面向历史和非拉丁文字材料，包含 layout 和识别能力。

不适合近期默认的原因：

- 当前项目目标是图片处理和 OCR 预处理，不是替换 OCR 引擎。
- 依赖、模型、训练、运行环境和隐私边界明显更重。
- 对现代扫描件的收益需要单独验证，不能直接推断。

建议：

- 作为 `research-only` 路线保留。
- 如果后续有大量历史书页/古籍样本，再建立独立 private validation gate。

## 4. 推荐实现顺序

### P0：保留并固化当前 baseline

- 保持 `ocr-preprocess-leptonica-v1` 不变。
- 所有新路径必须与它对照，而不是覆盖它。
- NoisyOffice、真实扫描样本、OCR synthetic gate 都按 `processing_path` 分组输出。

### P1：新增 OpenCV/NumPy 局部路径

候选 ID：`ocr-preprocess-opencv-local-v1`。

建议阶段：

1. 复用 preserve-canvas deskew。
2. 加 tile/morphology 背景估计。
3. 生成受保护的局部阈值候选 mask。
4. 灰度主图只做保守背景归一，二值 sidecar 使用 adaptive/Otsu/Sauvola 候选择优。
5. 用文字边缘能量、soft-edge、连通域碎片、表格线连续性做 guard。

### P2：新增 Sauvola/Niblack binary sidecar 路线

候选 ID：`ocr-preprocess-sauvola-binary-v1`。

只承诺 binary sidecar，不承诺灰度主图一定变化。验收以 DIBCO/OCR 指标为主。

### P3：unpaper-inspired 局部能力拆解

候选 ID：`ocr-preprocess-unpaper-inspired-v1`。

仅在内部实现单点能力，不把外部 unpaper CLI 放进默认运行链路。

### P4：研究型历史文献路径

候选 ID：`ocr-preprocess-historical-nlbin-v1`。

需要真实历史文献样本、OCR provider 和更严格隐私/模型边界后再启动。

## 5. 与当前架构的落地关系

当前 `processing_path` 架构已经支持本调研结论：

- 外部接口不变：仍由 `rule_template` 选择。
- 内部新增 path：每个候选算法注册独立 path ID。
- 运行时只跑一条 path：通过模板或服务 job 参数选择。
- 产物可对照：summary、manifest、record、plan fingerprint 和 service public summary 都记录 path。
- 恢复安全：fingerprint 包含 path，避免不同算法复用旧输出。

新增算法时必须同步完成：

1. 在 `processing_paths.py` 注册 path spec。
2. 新增或映射 rule template。
3. 在 `_process_image` dispatch 下接入独立实现入口。
4. Manifest、summary、service job、plan fingerprint 保持 path 透传。
5. 新增针对尺寸、纠偏、文字清晰度、表格线、源文件安全和 public-safe 边界的回归。
6. 更新 `docs/ocr-preprocessing-image-quality-plan.md`、`docs/image-quality-processing-roadmap.md` 和 release checklist。

## 6. 不采纳路线

短期不采纳以下路线作为默认能力：

- 只做锐化/加黑，不验证 OCR 和边缘结构。
- 无纠偏输出。
- 输出画布尺寸膨胀或 DPI/像素尺寸不可控。
- 以 hard snap、强二值化或超采样旋转替代文字清晰度。
- 直接引入大型 OCR/深度学习系统作为默认图片处理依赖。
- 把 unpaper/OCRmyPDF 等外部 CLI 的 aggressive clean 直接暴露为稳定生产开关。

## 7. 工程落地状态

2026-06-11 已先落地 `ocr-preprocess-opencv-local-v1` 实验路径：

- 该路径通过独立 `processing_path` 注册和 rule template 选择进入，不替换 `ocr-preprocess-leptonica-v1`。
- 主灰度 OCR 利用副本继续使用 preserve-canvas deskew 和源图尺寸约束，不做默认裁切、锐化或画布扩张。
- 背景处理采用 OpenCV/NumPy 局部背景估计、受保护前景 mask 和保守背景推白；二值输出作为 `ocr_binary/` sidecar，使用 adaptive/Otsu 阈值。
- 真实扫描样本 12 张 local-only 复测已完成：12/12 输出，11/12 实际纠偏，10/12 触发 OpenCV local 背景归一，12/12 生成 binary sidecar，源图修改 0，尺寸不匹配 0，guardrail failure 0。
- 该路径仍是实验路径：`ocr_text_edge_energy_ratio` 平均约 0.915，说明保留清晰度没有明显优于 Leptonica 基线；12/12 均标记 OCR review required。后续必须继续与 `ocr-preprocess-leptonica-v1` 并列对照，不能提升为默认推荐路径。
