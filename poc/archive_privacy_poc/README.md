# 档案个人信息脱敏 POC

这个 POC 用来验证“规则识别 + 可选 OpenAI Privacy Filter + span 合并 + 脱敏预览 + 指标评测”的最小闭环。

默认不需要外部依赖，只用 Python 标准库即可运行。默认规则引擎覆盖身份证号、手机号、邮箱、银行卡/账号、地址、姓名上下文、家庭成员、未成年人信息、出生日期、案号、密级标识、URL、token/密钥等。

## 运行

```bash
python3 archive_privacy_poc.py \
  --input sample_dataset.jsonl \
  --out-dir out
```

生成文件：

- `out/predictions.jsonl`：每条样本的原始 span、合并 span、gold span、脱敏文本。
- `out/summary.json`：typed / untyped precision、recall、F1 和分标签指标。
- `out/report.md`：Markdown 运行报告。
- `out/review.html`：可视化高亮页面，适合人工查看。

## 继续验证结果

已完成两组验证：

### 1. 离线本地模型验证

设置离线环境变量后，OPF Python API 和 CLI 都可以只使用本地 checkpoint 推理：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-opf/bin/python - <<'PY'
from opf import OPF
redactor = OPF(model='models/privacy_filter', device='cpu', output_mode='typed')
print(redactor.redact('联系人王丽，电话13800138000，邮箱wang.li@example.com。').to_json())
PY
```

验证结论：

- 模型不会在推理时访问公网。
- 本地中文样例可识别姓名、手机号、邮箱。
- CPU 推理可用，单条短文本通常为秒级。

### 2. 挑战样本验证

挑战样本文件：

```bash
challenge_dataset.jsonl
```

规则引擎运行：

```bash
.venv-opf/bin/python archive_privacy_poc.py \
  --input challenge_dataset.jsonl \
  --out-dir out-challenge-rule
```

规则 + OPF 运行：

```bash
HF_HUB_OFFLINE=1 .venv-opf/bin/python archive_privacy_poc.py \
  --input challenge_dataset.jsonl \
  --out-dir out-challenge-opf \
  --enable-opf \
  --opf-device cpu \
  --opf-checkpoint models/privacy_filter
```

当前合成挑战集结果：

| 模式 | Precision | Recall | F1 | 观察 |
|---|---:|---:|---:|---|
| 规则 | 1.0 | 0.9474 | 0.9730 | 漏掉 `liu.yang at example dot org` 这类模糊邮箱。 |
| 规则 + OPF | 1.0 | 1.0 | 1.0 | OPF 补充识别模糊邮箱；规则仍优先处理身份证、案号、电话、密钥、URL。 |

注意：这是小型合成挑战集，只用于验证链路和典型行为，不代表真实档案准确率。

验证中发现并修正的问题：

- `联系人是刘洋` 曾把“是”误并入姓名，已修正。
- `母亲周兰患有...` 曾漏掉家庭成员，已补充内联亲属规则。
- 少数民族长姓名曾被截断，已放宽姓名长度。
- `现住/寄到` 地址曾漏识别，已补充地址上下文。
- OPF 曾把公开办公室电话作为私人电话，已加公开电话上下文过滤。
- OPF 曾把两个 secret 合成一个大 span 并覆盖规则 span，已改为确定性规则优先。

## 接入 OpenAI Privacy Filter

当前工作区已经准备了一个本地 OPF 环境：

- 虚拟环境：`.venv-opf/`
- 本地权重：`models/privacy_filter/`
- 权重文件：`models/privacy_filter/model.safetensors`

启用 OPF 跑完整样本：

```bash
.venv-opf/bin/python archive_privacy_poc.py \
  --input sample_dataset.jsonl \
  --out-dir out-opf \
  --enable-opf \
  --opf-device cpu \
  --opf-checkpoint models/privacy_filter
```

如果在新机器上重新配置，先按官方仓库安装 `opf` 包并准备模型权重：

```bash
git clone https://github.com/openai/privacy-filter.git
cd privacy-filter
pip install -e .
```

然后在本 POC 目录运行：

```bash
python3 archive_privacy_poc.py \
  --input sample_dataset.jsonl \
  --out-dir out-opf \
  --enable-opf \
  --opf-device cpu \
  --opf-checkpoint /path/to/privacy_filter
```

说明：

- 如果模型权重未在本地，官方 `opf` 默认可能会尝试下载默认 checkpoint。
- 真实档案文本不应在公网 Demo 或不受控环境中处理。
- OPF 输出会被映射到本 POC 的统一标签，例如 `private_person -> personal_name`、`private_phone -> personal_phone`、`account_number -> financial_account`。
- 当前合并策略是“确定性规则优先，OPF 补充召回”。这可以避免 OPF 把中文身份证号、出生日期、案号等误归到 `account_number` 后覆盖规则结果。

## 数据格式

每行一个 JSON 对象：

```json
{
  "id": "sample-001",
  "title": "干部履历表片段",
  "text": "OCR 文本",
  "gold_mentions": [
    {"label": "personal_name", "text": "王丽"},
    {"label": "personal_id", "text": "110101199003071233"}
  ]
}
```

`gold_mentions` 会按文本自动解析位置。若同一文本出现多次，可以加 `occurrence`：

```json
{"label": "personal_name", "text": "王丽", "occurrence": 2}
```

也支持显式位置：

```json
{
  "gold_spans": [
    {"label": "personal_name", "start": 18, "end": 20}
  ]
}
```

## 当前定位

这个 POC 只验证个人信息脱敏链路，不做档案开放最终鉴定。开放审核还需要接入：

- 档案法、开放办法、保密、政府信息公开、个人信息保护等规则库。
- 形成单位/移交单位协同审核流程。
- 国家秘密、工作秘密、国家安全、商业秘密、知识产权等风险识别。
- PDF/OFD/Office/扫描件的真删除和输出复检。

## 本地图片 OCR 导入

如需用本地图片样本测试，先把图片目录转成 POC JSONL：

```bash
.venv-opf/bin/python ocr_images_to_jsonl.py \
  --image-dir /path/to/local/images \
  --out-dir out-ocr-local \
  --recursive \
  --max-files 20
```

再运行脱敏检测：

```bash
HF_HUB_OFFLINE=1 .venv-opf/bin/python archive_privacy_poc.py \
  --input out-ocr-local/ocr_dataset.jsonl \
  --out-dir out-ocr-local/opf \
  --enable-opf \
  --opf-device cpu \
  --opf-checkpoint models/privacy_filter
```

隐私说明：

- OCR 使用 macOS Vision，本地执行，不调用云 OCR。
- `ocr_dataset.jsonl` 包含 OCR 原文，属于敏感文件，不要上传。
- `ocr_blocks_private.jsonl` 包含 OCR 坐标和文本，属于敏感文件，不要上传。
- `manifest_private.jsonl` 包含本地文件路径，属于敏感文件，不要上传。
- 生成的 `review.html` 只包含 OCR 文本高亮，不包含原图。
