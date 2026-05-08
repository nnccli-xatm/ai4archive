# 本地 OCR 优化实验记录

本记录只包含统计结论，不包含 OCR 原文、图片文件名或图片内容。所有 OCR、预处理和 OPF 推理均在本机执行。

## 样本

- 来源：本机挂载的 `test-pic` 共享
- 数量：23 张 JPG
- 风险提示：样本和衍生预处理图片均涉及隐私，输出目录已加入 `.gitignore`

## 对比方案

使用 macOS Vision OCR，比较以下本地图像预处理：

| 方案 | 处理 |
|---|---|
| `original` | 原图直接 OCR |
| `gray2x` | 灰度、自动对比度、2 倍放大、锐化 |
| `gray3x` | 灰度、自动对比度、3 倍放大、锐化 |
| `binary2x` | `gray2x` 后简单二值化 |

## 结果

| 方案 | OCR 字符数 | OCR 块数 | 平均置信度 | 低置信度块 | 规则姓名命中文档 | 规则命中总数 |
|---|---:|---:|---:|---:|---:|---:|
| `original` | 8381 | 891 | 0.5435 | 476 | 19 | 19 |
| `gray2x` | 8408 | 872 | 0.5413 | 465 | 21 | 21 |
| `gray3x` | 8411 | 873 | 0.5399 | 466 | 19 | 19 |
| `binary2x` | 8242 | 868 | 0.5218 | 498 | 15 | 15 |

单独使用 `zh-Hans` 与 `zh-Hans,en-US` 的统计结果一致，语言参数不是这批样本的主要影响因素。

## 结论

- 推荐把 `gray2x` 作为当前默认 OCR 预处理方案。
- 简单二值化不适合这批图片，会降低召回。
- 所有样本都存在低置信度 OCR 块，说明仅靠规则或 OPF 无法完全解决漏检，必须保留人工复核和 OCR 质量提示。
- 当前本机没有 `tesseract` 或 ImageMagick；后续如允许安装本地离线 OCR 引擎，可加入 PaddleOCR、Tesseract 或 RapidOCR 做进一步对比。

## 当前输出

- `out-ocr-preprocess-local/experiment_summary_no_text.json`：不含原文的实验汇总
- `out-ocr-preprocess-local/gray2x/ocr_dataset.jsonl`：`gray2x` OCR 原文，敏感
- `out-ocr-preprocess-local/gray2x/opf/review.html`：`gray2x` 脱敏高亮预览，敏感

## 建议下一步

1. 以 `gray2x` 版 `review.html` 做人工抽检，记录漏检类型。
2. 为每张图片增加“低置信度 OCR 块比例”提示，低质量样本进入人工优先队列。
3. 尝试本地离线 OCR 引擎对比，重点看表格、姓名、身份证号、日期、地址字段。
4. 对扫描件增加版面级处理：旋转校正、表格区域切分、按行/单元格重建文本顺序。
