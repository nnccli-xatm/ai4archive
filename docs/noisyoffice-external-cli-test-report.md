# NoisyOffice 外部 CLI 批量质检修图测试报告

## 结论摘要

- 外部 CLI 稳定性验收：通过。
- 测试范围：UCI NoisyOffice simulated noisy grayscale，共 216 张图，合计 42.6902 MP。
- 端到端耗时：157.336437 秒；吞吐：82.37 张/分钟，0.2713 MP/s。
- CLI wall time：149.857703 秒；扫描：20.871120 秒；处理：128.760969 秒。
- 原图被修改文件数：0；处理输出缺失文件数：0；处理失败文件数：0。
- 处理后尺寸与 clean GT 不一致文件数：0。

## 测试方法

- 数据来源：UCI NoisyOffice，使用 simulated noisy grayscale 作为输入，clean grayscale 作为 GT。
- 外部调用方式：脚本以子进程执行 `python -m archive_scan_qc.cli production-run`，不直接调用后端内部函数。
- 规则模板：`text-clean-print`。
- 质量指标：PSNR、SSIM、MSE、MAE、暗像素 F1、前景保留率、亮度均值偏差。
- PSNR/SSIM/暗像素 F1/前景保留率越高越好；MSE/MAE 越低越好。

## 整体质量指标

| 对象 | PSNR dB | SSIM | MSE | MAE | 暗像素 F1 | 前景保留率 |
|---|---:|---:|---:|---:|---:|---:|
| Noisy 输入基线 | 15.825200 | 0.893610 | 1939.028962 | 37.260796 | 0.895165 | 0.984674 |
| 处理后 | 15.824100 | 0.893542 | 1939.365160 | 37.262630 | 0.895099 | 0.984540 |
| 处理后-输入 | -0.001100 | -0.000068 | +0.336198 | +0.001834 | -0.000066 | -0.000134 |

## 噪声类型分组

| 噪声类型 | 图像数 | 输入 PSNR | 处理后 PSNR | PSNR 变化 | 输入 SSIM | 处理后 SSIM | SSIM 变化 |
|---|---:|---:|---:|---:|---:|---:|---:|
| c | 54 | 13.764300 | 13.763600 | -0.000700 | 0.828253 | 0.828178 | -0.000075 |
| f | 54 | 18.426900 | 18.425100 | -0.001800 | 0.944545 | 0.944471 | -0.000074 |
| p | 54 | 14.560000 | 14.559400 | -0.000600 | 0.905734 | 0.905679 | -0.000055 |
| w | 54 | 16.549400 | 16.548300 | -0.001100 | 0.895906 | 0.895839 | -0.000067 |

## 外部 CLI 稳定性检查

| return code | summary status | progress state | processed | skipped | failed | source modified | stable pass |
|---:|---|---|---:|---:|---:|---:|---|
| 0 | finished | finished | 216 | 0 | 0 | 0 | yes |

## 处理操作统计

| warnings | guardrail failed | despeckled | tone normalized | edge shadow | faded text | sharpen text |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 125 | 0 | 0 | 0 | 0 |

## 质量变化最差样本

| 文件 | 噪声类型 | 输入 PSNR | 处理后 PSNR | PSNR 变化 | 输入 SSIM | 处理后 SSIM | SSIM 变化 |
|---|---|---:|---:|---:|---:|---:|---:|
| FontLtm_Noisef_TR.png | f | 18.548100 | 18.537100 | -0.011000 | 0.947815 | 0.947429 | -0.000386 |
| FontLtm_Noisef_TE.png | f | 18.369800 | 18.360700 | -0.009100 | 0.946871 | 0.946551 | -0.000320 |
| FontLre_Noisef_VA.png | f | 18.458300 | 18.452200 | -0.006100 | 0.947566 | 0.947304 | -0.000262 |
| FontLtm_Noisew_TR.png | w | 16.591300 | 16.585500 | -0.005800 | 0.899266 | 0.898942 | -0.000324 |
| FontLre_Noisef_TR.png | f | 18.639100 | 18.633900 | -0.005200 | 0.948609 | 0.948380 | -0.000229 |
| FontLtm_Noisew_VA.png | w | 16.575100 | 16.570500 | -0.004600 | 0.900895 | 0.900640 | -0.000255 |
| Fontfrm_Noisef_TE.png | f | 18.469500 | 18.464900 | -0.004600 | 0.936877 | 0.936677 | -0.000200 |
| FontLtm_Noisep_VA.png | p | 16.703200 | 16.698800 | -0.004400 | 0.951651 | 0.951417 | -0.000234 |
| Fontfrm_Noisef_TR.png | f | 18.551400 | 18.547000 | -0.004400 | 0.936351 | 0.936155 | -0.000196 |
| Fontnre_Noisef_TE.png | f | 18.393500 | 18.389100 | -0.004400 | 0.941891 | 0.941665 | -0.000226 |

## 重要限制

- 当前测试评估的是修图输出与 clean grayscale GT 的接近程度，不评估官方二值化任务。
- NoisyOffice 中部分噪声类型可能是大面积纹理/污渍，当前保守 guardrail 可能选择少处理或回退，以避免破坏文字和档案原貌。
- 如果目标是最大化 NoisyOffice PSNR/SSIM，需要新增专门的强去噪/背景重建模板，而不是直接使用当前生产保守模板。

## 产物

- JSON 结果：`D:\pic-qc\ai4archive\generated\noisyoffice_external_cli_test\20260609T080229Z\noisyoffice_external_cli_test_results.json`
- 图像级 CSV：`D:\pic-qc\ai4archive\generated\noisyoffice_external_cli_test\20260609T080229Z\noisyoffice_external_cli_image_metrics.csv`
- 运行根目录：`D:\pic-qc\ai4archive\generated\noisyoffice_external_cli_test\20260609T080229Z`
