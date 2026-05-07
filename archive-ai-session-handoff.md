# 档案开放鉴定与脱敏 POC 会话交接文档

更新时间：2026-05-07  
工作区：`/Users/wangyou/Documents/New project 3`

本文档用于在后续重新开始会话时完整继承当前工作成果。文档不包含真实 OCR 原文、图片文件名、图片内容或识别出的个人信息。

## 最高优先级约束

1. `\\PUERSAI-HPC\ocr-test` 和 `\\PUERSAI-HPC\test-pic` 下的图片涉及隐私，绝对不能上传网络，不能贴到对话里，不能把 OCR 原文或图片内容输出到聊天回复。
2. 所有真实样本 OCR 输出、脱敏预览、预处理图片都视为敏感文件，只能保存在本机可信路径。
3. 允许在本机离线处理图片、OCR、脱敏识别和统计，但输出给用户时只能给数量、标签、置信度、路径和结论，不展示原文。
4. 后续如要做正式法律法规结论，必须重新核验最新有效法律法规、部门规章、国家档案局文件和地方实施细则。

## 项目目标

构建一个为各级档案馆开放档案做人工智能辅助鉴定的系统。

核心定位：

- 面向各级档案馆馆藏档案开放审核。
- 系统给出辅助鉴定意见，最终决定仍由档案馆和相关责任人员作出。
- 需要符合法律法规、保密要求、个人信息保护要求、政府信息公开要求、档案开放审核流程。
- 当前 POC 聚焦个人信息识别、OCR、脱敏预览，不等同于完整开放鉴定系统。

## 已有调研文档

项目根目录已有以下调研/需求文档：

- `archive-open-appraisal-ai-research.md`：档案开放鉴定法律法规、系统能力、成熟系统和开源方向调研。
- `openai-privacy-filter-demo-research.md`：OpenAI Privacy Filter Demo 项目调研，评估对脱敏系统的帮助。
- `archive-ai-demand-analysis.md`：档案 AI 系统需求分析。
- `data-desensitization-research.md`：数据脱敏方向调研。

这些文档应作为后续产品设计、需求拆分、系统架构设计的基础材料。

## 法规与业务边界摘要

当前系统设计需要覆盖的法规/制度方向包括：

- 《中华人民共和国档案法》及档案开放相关实施制度。
- 档案开放审核、到期开放、延期开放、移交单位协同审核、开放目录管理等制度要求。
- 《中华人民共和国保守国家秘密法》及保密审查要求。
- 政府信息公开相关制度。
- 《中华人民共和国个人信息保护法》。
- 商业秘密、工作秘密、知识产权、国家安全、公共安全、社会稳定、民族宗教、外交、未成年人、医疗健康等敏感风险。

当前 POC 只覆盖“个人信息脱敏辅助识别”闭环，尚未覆盖完整开放鉴定结论链。

## 当前 POC 路径

POC 主目录：

```bash
/Users/wangyou/Documents/New project 3/poc/archive_privacy_poc
```

核心文件：

- `archive_privacy_poc.py`：主 POC。支持规则识别、可选 OPF、本地脱敏预览、HTML review、指标评估。
- `ocr_images_to_jsonl.py`：本地图片 OCR 导入脚本，使用 macOS Vision，不调用云 OCR。
- `ocr_preprocess_experiment.py`：本地图像预处理 OCR 对比实验脚本。
- `sample_dataset.jsonl`：合成样本。
- `challenge_dataset.jsonl`：合成挑战样本。
- `README.md`：POC 使用说明。
- `ocr-local-optimization-notes.md`：OCR 优化实验记录。
- `.gitignore`：忽略虚拟环境、模型权重和本地敏感 OCR 输出。

## 本地环境和模型

虚拟环境：

```bash
/Users/wangyou/Documents/New project 3/poc/archive_privacy_poc/.venv-opf
```

本地 OpenAI Privacy Filter 权重：

```bash
/Users/wangyou/Documents/New project 3/poc/archive_privacy_poc/models/privacy_filter
```

权重文件：

- `config.json`
- `dtypes.json`
- `viterbi_calibration.json`
- `model.safetensors`

大小：

- `models/` 约 `2.6G`
- `.venv-opf/` 约 `643M`

关键结论：

- OPF 权重已经启用。
- 权重首次是从 Hugging Face 下载到本地。
- 后续推理可以完全本地离线运行。
- 离线运行时使用：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
```

本机可用依赖：

- Python 3.11 虚拟环境。
- `opf`
- `ocrmac`
- `Pillow`
- `numpy`
- macOS Vision OCR。

当前未发现可用命令：

- `tesseract`
- ImageMagick `magick`/`convert`

## 支持的输入格式

当前 POC 已实际支持：

- JSONL 文本输入。
- 图片 OCR 输入，扩展名：
  - `.png`
  - `.jpg`
  - `.jpeg`
  - `.tif`
  - `.tiff`
  - `.bmp`
  - `.webp`

当前系统设计后续应扩展到：

- PDF
- OFD
- Word/WPS
- Excel/表格
- TXT/HTML/XML
- 扫描图片
- 压缩包批处理
- 多页档案卷件结构

当前 POC 暂未实现 PDF/OFD/Office 的版式保持、真删除、批注、套红、电子签章、归档输出等能力。

## 检测标签

当前统一标签包括：

- `personal_name`：姓名
- `personal_id`：身份证件号
- `personal_phone`：联系电话
- `personal_email`：电子邮箱
- `personal_address`：住址/地址
- `personal_date`：个人日期
- `financial_account`：金融账号
- `family_member`：家庭成员
- `minor_info`：未成年人信息
- `case_number`：案号
- `secrecy_mark`：密级标识
- `private_url`：个人/敏感链接
- `secret`：口令/密钥

OPF 标签会映射到上述统一标签。合并策略是“确定性规则优先，OPF 补召回”。

## 已完成的重要规则修正

已经修复/增强：

- `联系人是刘洋` 不再把“是”并入姓名。
- `母亲周兰患有...` 这类内联亲属姓名可识别。
- 少数民族长姓名支持到 12 个中文字符和 `·`。
- 地址上下文新增 `现住`、`寄到`。
- 公开电话上下文抑制 OPF 电话误报，例如 `公开电话`、`办公室公开电话`、`值班电话`。
- 日期上下文抑制 OPF 误报，例如 `票据号`、`项目编号`、`档号`、`案号`。
- 规则 span 优先于 OPF span，避免 OPF 大 span 覆盖规则结果。
- 新增表格式姓名识别：
  - `姓 名`
  - `曾 用 名`
  - `原 名`
  - `别 名`
  - `申请人`
  - `当事人`
  - `联系人`
  - 支持 OCR 中的空格/换行。
  - 支持姓名后接 `性别`、`出生`、`民族`、`年龄`、`身份证`、`电话`、`地址`、`单位` 等字段边界。
- review 页面增强：
  - 顶部“命中文档”索引。
  - 有命中的卡片左侧红色边框。
  - 高亮文字红色描边、加粗。
  - 每个样本显示命中数量和标签类型。

## 合成样本验证结果

在 `sample_dataset.jsonl` 上：

```bash
.venv-opf/bin/python archive_privacy_poc.py \
  --input sample_dataset.jsonl \
  --out-dir out
```

当前规则模式：

- Typed Precision：`1.0`
- Typed Recall：`1.0`
- Typed F1：`1.0`

在 `challenge_dataset.jsonl` 上：

规则模式：

- Precision：`1.0`
- Recall：`0.9474`
- F1：`0.973`
- 主要漏项：模糊邮箱 `liu.yang at example dot org`

规则 + OPF：

- Precision：`1.0`
- Recall：`1.0`
- F1：`1.0`

命令：

```bash
.venv-opf/bin/python archive_privacy_poc.py \
  --input challenge_dataset.jsonl \
  --out-dir out-challenge-rule

HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-opf/bin/python archive_privacy_poc.py \
  --input challenge_dataset.jsonl \
  --out-dir out-challenge-opf \
  --enable-opf \
  --opf-device cpu \
  --opf-checkpoint models/privacy_filter
```

注意：合成样本指标只能说明链路和典型规则行为，不代表真实档案准确率。

## 网络共享诊断记录

原计划使用：

```text
\\PUERSAI-HPC\ocr-test
```

该共享曾出现问题：

- 主机可 ping 通。
- SMB 445 端口可连通。
- 挂载成功到 `/Volumes/ocr-test`。
- 但 `ls/find/stat/smbutil statshares` 卡在 macOS `U` 状态。
- `diskutil unmount force` 也曾卡住。
- 判断不是普通网络不通，而是 SMB 共享会话、服务端目录枚举或存储/文件数量压力问题。
- `df` 曾显示该共享所在卷约 `14Ti`，使用约 `96%`，元数据/文件数量压力也高。

后改用：

```text
//ps@PUERSAI-HPC/test-pic
```

当前挂载：

```bash
/Volumes/test-pic
```

当前状态：

- `/Volumes/test-pic` 可访问。
- 挂载为只读 SMB。
- 共享内发现 23 张 JPG。

后续原则：

- 不要对大共享根目录随意跑递归 `find`。
- 优先使用较小测试目录。
- 出问题时先判断：网络层、SMB 端口、挂载层、目录枚举层。
- 多网卡情况下尽量使用固定 IP 挂载，而不是主机名。

## 真实 OCR 样本处理记录

真实测试目录：

```bash
/Volumes/test-pic
```

本地 OCR 输出：

```bash
out-test-pic-local/
```

首次 OCR：

- 图片数量：`23`
- 格式：`jpg`
- OCR 总字符数：`8381`
- OCR 块总数：`891`
- 空 OCR 图片：`0`

首次规则 + OPF 命中：

- 命中文档：`6`
- 命中总数：`6`
- `personal_name`：`4`
- `personal_date`：`2`

用户人工查看后发现：

- 有明显姓名没有命中。
- OCR 存在乱码。

随后增强表格式姓名规则并重新跑：

- 命中文档：`21`
- 命中总数：`23`
- `personal_name`：`21`
- `personal_date`：`2`

当前真实样本没有人工 gold 标注，因此 precision/recall 没有意义。真实样本应以人工 review 为准。

## OCR 质量问题

对真实样本的 OCR 块统计：

- 23 个文档全部存在低置信度 OCR 块。
- 平均 OCR 置信度最低约 `0.359`。
- 最高也只有约 `0.688`。

结论：

- OCR 是当前准确率瓶颈之一。
- 规则和 OPF 无法完全修复 OCR 乱码导致的漏检。
- 必须保留 OCR 质量提示、人工复核优先级和后续 OCR 引擎/预处理对比。

## OCR 预处理实验

新增脚本：

```bash
ocr_preprocess_experiment.py
```

实验输出：

```bash
out-ocr-preprocess-local/
out-ocr-preprocess-local-zh/
```

这些目录包含 OCR 原文、块坐标和预处理图片，均为敏感输出，已加入 `.gitignore`。

对比方案：

- `original`：原图直接 OCR。
- `gray2x`：灰度、自动对比度、2 倍放大、锐化。
- `gray3x`：灰度、自动对比度、3 倍放大、锐化。
- `binary2x`：`gray2x` 后简单二值化。

实验命令：

```bash
.venv-opf/bin/python ocr_preprocess_experiment.py \
  --image-dir /Volumes/test-pic \
  --out-dir out-ocr-preprocess-local \
  --recursive \
  --max-files 23 \
  --variants original,gray2x,gray3x,binary2x
```

只用中文 OCR 的对比：

```bash
.venv-opf/bin/python ocr_preprocess_experiment.py \
  --image-dir /Volumes/test-pic \
  --out-dir out-ocr-preprocess-local-zh \
  --recursive \
  --max-files 23 \
  --variants original,gray2x \
  --languages zh-Hans
```

结果摘要：

| 方案 | OCR 字符数 | OCR 块数 | 平均置信度 | 低置信度块 | 规则姓名命中文档 | 规则命中总数 |
|---|---:|---:|---:|---:|---:|---:|
| `original` | 8381 | 891 | 0.5435 | 476 | 19 | 19 |
| `gray2x` | 8408 | 872 | 0.5413 | 465 | 21 | 21 |
| `gray3x` | 8411 | 873 | 0.5399 | 466 | 19 | 19 |
| `binary2x` | 8242 | 868 | 0.5218 | 498 | 15 | 15 |

结论：

- 推荐 `gray2x` 作为当前默认预处理。
- 简单二值化会降低效果，不推荐。
- `zh-Hans` 与 `zh-Hans,en-US` 结果一致，语言参数不是这批样本的主要问题。
- 需要后续尝试本地离线 OCR 引擎，例如 PaddleOCR、RapidOCR、Tesseract。

`gray2x + OPF` 输出：

```bash
out-ocr-preprocess-local/gray2x/opf/
```

命中结果：

- 命中总数：`23`
- `personal_name`：`21`
- `personal_date`：`2`

## 当前重要 review 页面

旧版真实样本 review：

```bash
/Users/wangyou/Documents/New project 3/poc/archive_privacy_poc/out-test-pic-local/opf/review.html
```

当前浏览器打开的是这个旧路径：

```text
file:///Users/wangyou/Documents/New%20project%203/poc/archive_privacy_poc/out-test-pic-local/opf/review.html
```

推荐后续优先看 `gray2x` 版：

```bash
/Users/wangyou/Documents/New project 3/poc/archive_privacy_poc/out-ocr-preprocess-local/gray2x/opf/review.html
```

打开命令：

```bash
open "/Users/wangyou/Documents/New project 3/poc/archive_privacy_poc/out-ocr-preprocess-local/gray2x/opf/review.html"
```

## 敏感输出目录

以下目录包含真实 OCR 原文、坐标、预处理图片或高亮预览，不能提交、上传或粘贴内容：

- `out-test-pic-local/`
- `out-ocr-preprocess-local/`
- `out-ocr-preprocess-local-zh/`
- `out-ocr-local/`

已在 `.gitignore` 中忽略。

## 可复现命令清单

进入 POC 目录：

```bash
cd "/Users/wangyou/Documents/New project 3/poc/archive_privacy_poc"
```

合成样本规则运行：

```bash
.venv-opf/bin/python archive_privacy_poc.py \
  --input sample_dataset.jsonl \
  --out-dir out
```

合成挑战样本规则运行：

```bash
.venv-opf/bin/python archive_privacy_poc.py \
  --input challenge_dataset.jsonl \
  --out-dir out-challenge-rule
```

合成挑战样本规则 + OPF：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-opf/bin/python archive_privacy_poc.py \
  --input challenge_dataset.jsonl \
  --out-dir out-challenge-opf \
  --enable-opf \
  --opf-device cpu \
  --opf-checkpoint models/privacy_filter
```

图片 OCR 导入：

```bash
.venv-opf/bin/python ocr_images_to_jsonl.py \
  --image-dir /Volumes/test-pic \
  --out-dir out-test-pic-local \
  --recursive \
  --max-files 23
```

真实样本规则 + OPF：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-opf/bin/python archive_privacy_poc.py \
  --input out-test-pic-local/ocr_dataset.jsonl \
  --out-dir out-test-pic-local/opf \
  --enable-opf \
  --opf-device cpu \
  --opf-checkpoint models/privacy_filter
```

OCR 预处理实验：

```bash
.venv-opf/bin/python ocr_preprocess_experiment.py \
  --image-dir /Volumes/test-pic \
  --out-dir out-ocr-preprocess-local \
  --recursive \
  --max-files 23 \
  --variants original,gray2x,gray3x,binary2x
```

`gray2x` OCR 结果跑规则 + OPF：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-opf/bin/python archive_privacy_poc.py \
  --input out-ocr-preprocess-local/gray2x/ocr_dataset.jsonl \
  --out-dir out-ocr-preprocess-local/gray2x/opf \
  --enable-opf \
  --opf-device cpu \
  --opf-checkpoint models/privacy_filter
```

语法检查：

```bash
.venv-opf/bin/python -m py_compile \
  archive_privacy_poc.py \
  ocr_images_to_jsonl.py \
  ocr_preprocess_experiment.py
```

## 后续工作建议

短期：

1. 以 `gray2x` 版 `review.html` 做人工抽检。
2. 记录漏检类型，但不要把真实姓名或 OCR 原文发到聊天。
3. 为每个样本增加 OCR 质量评分和低置信度提醒。
4. 给 review 页面增加“只看命中文档 / 只看低置信度文档 / 标记误报漏报”的本地人工复核功能。

中期：

1. 引入本地离线 OCR 引擎对比：
   - PaddleOCR
   - RapidOCR
   - Tesseract
2. 增加版面处理：
   - 自动旋转校正
   - 表格区域切分
   - 按行/单元格重建文本顺序
   - 多页档案合并
3. 增加人工标注格式，把真实样本转成有 gold 的小评测集。
4. 增加更多档案常见字段：
   - 身份证号 OCR 断裂/空格
   - 出生年月
   - 籍贯
   - 工作单位
   - 家庭成员
   - 地址
   - 电话
   - 档号/案号误报抑制

长期：

1. 从“脱敏识别 POC”扩展到“开放鉴定辅助系统”：
   - 档案元数据解析
   - 开放期限计算
   - 保密风险识别
   - 个人信息风险识别
   - 商业秘密/工作秘密/知识产权识别
   - 开放、控制、延期、部分开放、脱敏开放建议
   - 审核理由和法规依据生成
   - 人工复核、留痕、审批流、版本管理
2. 构建规则库：
   - 国家法律法规
   - 国家档案局规范
   - 地方档案开放细则
   - 馆内规则
3. 构建审计闭环：
   - 每条 AI 建议必须可解释
   - 每次人工修改必须留痕
   - 每次输出必须支持复检

## 重新开始会话时的建议首句

如果后续重新开始，可以直接对 Codex 说：

```text
请先读取 /Users/wangyou/Documents/New project 3/archive-ai-session-handoff.md，
严格遵守其中的隐私约束，并在此基础上继续档案开放鉴定与脱敏 POC。
```

然后根据需要继续：

```text
优先打开 gray2x 版 review.html，继续根据人工发现的漏检补规则。
```

或：

```text
继续做本地离线 OCR 引擎对比，不要上传任何图片或 OCR 内容。
```
