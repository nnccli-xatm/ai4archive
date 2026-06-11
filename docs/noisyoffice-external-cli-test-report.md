# NoisyOffice 外部 CLI 批量质检修图测试报告

## 结论摘要

- 外部 CLI 稳定性验收：通过。
- 测试范围：UCI NoisyOffice simulated noisy grayscale，共 216 张图，合计 42.6902 MP。
- 端到端耗时：95.1734 秒；吞吐：136.17 张/分钟，0.4486 MP/s。
- CLI wall time：86.306491 秒；扫描：30.367919 秒；处理：55.640214 秒。
- 原图被修改文件数：0；处理输出缺失文件数：0；处理失败文件数：0。
- 处理后尺寸与 clean GT 不一致文件数：0。

## 测试方法

- 数据来源：UCI NoisyOffice，使用 simulated noisy grayscale 作为输入，clean grayscale 作为 GT。
- 外部调用方式：脚本以子进程执行 `python -m archive_scan_qc.cli production-run`，不直接调用后端内部函数。
- 规则模板：`ocr-preprocess-leptonica-v1`。
- 质量指标：PSNR、SSIM、MSE、MAE、暗像素 F1、前景保留率、亮度均值偏差。
- PSNR/SSIM/暗像素 F1/前景保留率越高越好；MSE/MAE 越低越好。

## 整体质量指标

| 对象 | PSNR dB | SSIM | MSE | MAE | 暗像素 F1 | 前景保留率 |
|---|---:|---:|---:|---:|---:|---:|
| Noisy 输入基线 | 15.825200 | 0.893610 | 1939.028962 | 37.260796 | 0.895165 | 0.984674 |
| 处理后 | 18.753400 | 0.906673 | 1172.686583 | 20.694829 | 0.895165 | 0.984672 |
| 处理后-输入 | +2.928200 | +0.013063 | -766.342379 | -16.565967 | +0.000000 | -0.000002 |

## 噪声类型分组

| 噪声类型 | 图像数 | 输入 PSNR | 处理后 PSNR | PSNR 变化 | 输入 SSIM | 处理后 SSIM | SSIM 变化 |
|---|---:|---:|---:|---:|---:|---:|---:|
| c | 54 | 13.764300 | 14.605300 | +0.841000 | 0.828253 | 0.818528 | -0.009725 |
| f | 54 | 18.426900 | 20.885100 | +2.458200 | 0.944545 | 0.954508 | +0.009963 |
| p | 54 | 14.560000 | 18.986900 | +4.426900 | 0.905734 | 0.916639 | +0.010905 |
| w | 54 | 16.549400 | 20.536400 | +3.987000 | 0.895906 | 0.937017 | +0.041111 |

## 外部 CLI 稳定性检查

| return code | summary status | progress state | processed | skipped | failed | source modified | stable pass |
|---:|---|---|---:|---:|---:|---:|---|
| 0 | finished | finished | 216 | 0 | 0 | 0 | yes |

## 处理操作统计

| warnings | guardrail failed | despeckled | tone normalized | edge shadow | faded text | sharpen text |
|---:|---:|---:|---:|---:|---:|---:|
| 40 | 0 | 0 | 0 | 0 | 0 | 0 |

## OCR 处理聚合指标

| OCR gray | OCR binary | OCR review | guardrail failed | output safety reverted | deskewed | deskew safe-skip |
|---:|---:|---:|---:|---:|---:|---:|
| 176 | 176 | 54 | 0 | 40 | 0 | 179 |

| metric | average | max |
|---|---:|---:|
| ocr_preprocess_changed_pixel_ratio | 0.476465 | 0.736142 |
| ocr_background_delta | 15.157407 | 38.000000 |
| ocr_foreground_retention_ratio | 0.999998 | 1.000000 |
| ocr_text_edge_energy_ratio | 1.161470 | 1.656469 |
| ocr_text_soft_edge_ratio_delta | 0.067828 | 0.361823 |
| ocr_binary_foreground_ratio | 0.211154 | 0.442018 |
| ocr_binary_foreground_retention_ratio | 0.988228 | 1.000000 |

## 质量变化最差样本

| 文件 | 噪声类型 | 输入 PSNR | 处理后 PSNR | PSNR 变化 | 输入 SSIM | 处理后 SSIM | SSIM 变化 |
|---|---|---:|---:|---:|---:|---:|---:|
| FontLre_Noisec_VA.png | c | 14.508200 | 14.508200 | +0.000000 | 0.840148 | 0.840148 | +0.000000 |
| FontLre_Noisef_TR.png | f | 18.639100 | 18.639100 | +0.000000 | 0.948609 | 0.948609 | +0.000000 |
| FontLrm_Noisec_VA.png | c | 14.521400 | 14.521400 | +0.000000 | 0.852861 | 0.852861 | +0.000000 |
| FontLrm_Noisef_TR.png | f | 18.688700 | 18.688700 | +0.000000 | 0.947898 | 0.947898 | +0.000000 |
| FontLse_Noisec_VA.png | c | 14.656000 | 14.656000 | +0.000000 | 0.887894 | 0.887894 | +0.000000 |
| FontLse_Noisef_TR.png | f | 18.628600 | 18.628600 | +0.000000 | 0.955099 | 0.955099 | +0.000000 |
| FontLsm_Noisec_VA.png | c | 14.682400 | 14.682400 | +0.000000 | 0.894532 | 0.894532 | +0.000000 |
| FontLsm_Noisef_TR.png | f | 18.688700 | 18.688700 | +0.000000 | 0.954961 | 0.954961 | +0.000000 |
| FontLte_Noisec_TR.png | c | 15.595400 | 15.595400 | +0.000000 | 0.908036 | 0.908036 | +0.000000 |
| FontLte_Noisec_VA.png | c | 14.532400 | 14.532400 | +0.000000 | 0.875504 | 0.875504 | +0.000000 |

## 重要限制

- 当前测试评估的是修图输出与 clean grayscale GT 的接近程度，不评估官方二值化任务。
- NoisyOffice 中部分噪声类型可能是大面积纹理/污渍，当前保守 guardrail 可能选择少处理或回退，以避免破坏文字和档案原貌。
- 如果目标是最大化 NoisyOffice PSNR/SSIM，需要新增专门的强去噪/背景重建模板，而不是直接使用当前生产保守模板。

## 产物

- JSON 结果：`D:\pic-qc\ai4archive\generated\noisyoffice_external_cli_test\20260611T_noisy_ocr_leptonica\noisyoffice_external_cli_test_results.json`
- 图像级 CSV：`D:\pic-qc\ai4archive\generated\noisyoffice_external_cli_test\20260611T_noisy_ocr_leptonica\noisyoffice_external_cli_image_metrics.csv`
- 运行根目录：`D:\pic-qc\ai4archive\generated\noisyoffice_external_cli_test\20260611T_noisy_ocr_leptonica`


## OCR preprocessing quality gate

- Gate status: failed; enforced: no.
- Failed checks: ssim_delta, negative_noise_groups.

| check | actual | threshold | passed |
|---|---:|---:|---|
| psnr_delta_db | 2.928200 | 1.000000 | yes |
| ssim_delta | 0.013063 | 0.015000 | no |
| mse_reduction_ratio | 0.395220 | 0.100000 | yes |
| mae_reduction_ratio | 0.444595 | 0.050000 | yes |
| dark_f1_delta | 0.000000 | 0.000000 | yes |
| foreground_retention_delta | -0.000002 | -0.002000 | yes |
| positive_noise_groups | 4.000000 | 3.000000 | yes |
| negative_noise_groups | 1.000000 | 0.000000 | no |
