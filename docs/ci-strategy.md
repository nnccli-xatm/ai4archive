# CI 策略说明

本文档说明当前 GitHub Actions CI 的分层策略、触发条件、质量边界和维护方法。目标是在不牺牲主干质量的前提下，减少每个 PR 和每次合并后的等待时间。

## 目标

- PR 反馈要快，默认不跑完整图像回归套件。
- main 分支仍要有完整质量兜底。
- Python 3.10 和 3.12 保留兼容性验证，但不在每次 main push 上重复跑完整测试。
- 三版本四组语义回归保留在定时和手动深度验证里。
- CI 不依赖 runner 内部的 `GITHUB_TOKEN` checkout，继续使用公开 ref fetch，避免再次被 token 侧 403 阻断。

## 触发矩阵

### Pull Request

PR 触发两个层级：

1. `PR smoke Python 3.10/3.11/3.12`
   - 安装包。
   - 校验生产工作台前端。
   - 编译 `src` 和 `tests`。
   - 运行快速合同测试 `make test-fast PYTHON=python`。
   - 构建 wheel。
   - 安装 wheel 并执行 CLI 版本 smoke。

2. `PR targeted tests`
   - 固定使用 Python 3.11。
   - 根据 PR 相对 base 的变更文件选择测试。
   - 如果只新增测试方法，优先只运行新增的 `test_*` 方法。
   - 如果改的是源代码或无法精确定位方法，则运行相关测试文件。
   - 没有 Python 源码或测试变更时跳过。

PR 不跑三版本完整 `unittest discover`，避免小范围改动等待完整图像回归。

### main push

main 分支触发两个层级：

1. `Main compatibility smoke Python 3.10/3.12`
   - 验证非主版本 Python 的安装、编译、打包和 CLI smoke。
   - 不跑完整单元测试。

2. `Main regression <group> Python 3.11`
   - Python 3.11 按语义分组跑完整回归。
   - 四组为 `core-image-processing`、`production-cli`、`privacy-boundary` 和 `external-validation`。
   - 每组启动前由 `scripts/ci_regression_groups.py verify-coverage` 等价校验确保所有 `tests/test_*.py` 正好归入一个组。
   - 这样保留 main 的完整测试兜底，同时把高耗时图像处理、生产 CLI、隐私边界和外部验证失败面拆开定位。

### workflow_dispatch

手动触发支持 `deep-full` 模式：

- `deep-full`
  - Python 3.10、3.11、3.12 都运行四组语义回归。
  - `production-cli` 组额外运行前端工作台校验、编译、wheel 构建和 CLI smoke。
  - 适合发布前、重大算法改动后、或怀疑跨版本差异时使用。

### schedule

定时任务每天 UTC 18:00 运行 `deep-full` 等价验证。

该时间约为北京时间 02:00，避免阻塞白天无人值守开发节奏。

## 测试选择规则

`PR targeted tests` 读取 PR 相对 base 的变更文件：

- `tests/test_*.py`
  - 如果 diff 命中现有 `test_*` 方法体，运行对应完整 unittest 方法 ID（`test_xxx.Class.test_xxx`）。
  - 如果 diff 新增了 `def test_*`，继续按方法级选择新增测试。
  - 仅当改动落在测试方法/测试类外部，或无法安全映射到具体测试方法时，回退到运行整个测试文件。

- `src/archive_scan_qc/*`
  - 按模块映射到较小的合同测试、专项测试或工作台测试。
  - 不再对所有源码改动默认运行 `tests/test_scan_qc.py`。

- `scripts/check_offline_dependencies.py`、`scripts/frontend_issue_driver.py`、`scripts/generate_issue_plan.py`
  - 默认运行 `tests/test_delivery_tooling.py`，覆盖交付脚本、前端 issue 驱动和离线依赖检查。

- `scripts/release_readiness_summary.py`、`scripts/release_candidate_summary.py`
  - 默认运行 `tests/test_release_summaries.py`，覆盖发布就绪和发布候选聚合摘要。

- `src/archive_scan_qc/processing.py`、`scanner.py` 等图像处理核心模块
  - 默认运行小型图像专项套件，例如：
    - `tests/test_backend_consistency.py`
    - `tests/test_content_type_regression.py`
    - `tests/test_ai4_863_optimizations.py`
    - `tests/test_ai4_864_deskew_vectorization.py`
    - `tests/test_ai4_866_despeckle_fallback_parity.py`
    - `tests/test_ai4_867_numpy_backend.py`
    - `tests/test_ai4_869_numpy_despeckle_filtering.py`
    - `tests/test_quality_suite.py`
    - `tests/test_scan_processing_combo.py`
    - `tests/test_scan_processing_reuse.py`
    - `tests/test_scan_processing_workflow_regression.py`
    - 背景污渍、边缘阴影、色调归一和扫描线专项测试。
  - `tests/test_scan_processing_algorithm_regression.py` 作为深度回归，不再由普通源码改动默认拉起。

- 当前 `SOURCE_TEST_MAP` 已不再把普通源码模块映射到 `tests/test_scan_qc.py`。
  - 后续新增源码模块时，应优先补小型专项测试映射；只有深度回归入口本身才保留 `tests/test_scan_qc.py`。
  - 已抽离的小型模块测试包括 `tests/test_acceptance_summary_regression.py`、`tests/test_aggregate_baseline_regression.py`、`tests/test_analysis_provider.py`、`tests/test_artifact_readiness.py`、`tests/test_capability_probe.py`、`tests/test_cli_smoke.py`、`tests/test_deep_inspection_candidates.py`、`tests/test_deep_inspection_provider.py`、`tests/test_processing_review.py`、`tests/test_delivery_tooling.py`、`tests/test_release_summaries.py`、`tests/test_evidence_bundle.py`、`tests/test_final_handoff.py`、`tests/test_handoff_manifest.py`、`tests/test_preflight_run_plan.py`、`tests/test_production_rehearsal.py`、`tests/test_reports_contract.py`、`tests/test_review_decisions.py`、`tests/test_rework_actions.py`、`tests/test_rules_calibration.py`、`tests/test_validation_index.py`、`tests/test_workbench_summary.py` 和 `tests/test_scan_processing_workflow_regression.py`。

- 路径包含背景污渍、边缘阴影、色调归一、扫描线相关关键词时，额外运行对应专项测试文件。

这些规则仍然保守，但把 PR 默认目标从“拉起巨型回归文件”改为“先跑快速合同和相关专项”。深度图像回归继续由 main 四组语义回归、定时 deep-full 和手动 deep-full 兜底。

## 本地测试分层入口

`Makefile` 提供以下分层入口：

- `make test-fast`
  - 规则、manifest、sampling、acceptance、验收汇总回归、规则校准、报告合同、DA/T 合同、CLI smoke、能力探测、深度检查聚合合同、交付/发布脚本、聚合证据/最终交接/复核决策/返工动作/公共验证索引/工作台公共汇总/交付工件就绪校验和 worker 推荐等快速测试。
  - PR smoke 会在 Python 3.10、3.11、3.12 上运行这一层。

- `make test-image`
  - 图像处理专项、后端一致性、内容类型、质量套件、处理复用和处理流程回归测试。

- `make test-platform`
  - 本地工作台、Windows/WSL 路径、打开输出文件夹、preflight/run-plan 平台工作流、生产演练和生产工作台 guard。
  - 平台相关测试必须显式隔离环境假设，避免在普通本地环境误报。

- `make test-perf`
  - 性能测试和 worker scaling，不作为普通 PR 的默认阻塞项。

- `make test-deep-regression`
  - `tests/test_scan_qc.py` 和 `tests/test_scan_processing_algorithm_regression.py`。
  - 用于发布前、重大算法改动后或专门排查历史深度回归；处理流程类回归已下沉到 `make test-image`。

- `make test-core-image-processing`
  - 运行核心图像处理组，包括合成图像能力 smoke、扫描/处理算法回归、后端一致性、NumPy/deskew/despeckle 快路径奇偶性、质量套件和性能/worker 推荐测试。

- `make test-production-cli`
  - 运行生产 CLI 组，包括稳定 CLI 合同、preflight/run-plan、production-run/review queue、规则、manifest、sampling、service API/job boundary 和生产工作台 guard。

- `make test-privacy-boundary`
  - 运行隐私边界组，包括公共能力合同、能力探测、分析 provider、aggregate baseline public-safe 自检、聚合证据、最终交接、复核决策、校准摘要和 public-safe 索引。

- `make test-external-validation`
  - 运行外部验证组，包括 CI 分组自身、targeted selector、交付脚本测试，以及 DIBCO/NoisyOffice 形状的合成外部 CLI smoke。

- `make test-regression-groups`
  - 先校验所有 `tests/test_*.py` 正好归入一个语义组，再依次运行四组。

- `make test`
  - 完整 `unittest discover`，仍作为深度兜底入口。

## 四组语义回归

main push 的完整回归在 Python 3.11 上按 4 个语义组并行：

- `core-image-processing`
  - 覆盖实际图像读写、扫描 QC、处理算法、后端一致性、NumPy/deskew/despeckle 快路径奇偶性、质量套件、性能和 worker 推荐。
- `production-cli`
  - 覆盖稳定 CLI、生产运行、preflight/run-plan、规则/manifest/sampling、报告合同和生产工作台。
- `privacy-boundary`
  - 覆盖 public-safe 聚合输出、aggregate baseline 自检、provider 边界、证据/交接/复核摘要、校准和公共验证索引。
- `external-validation`
  - 覆盖 CI/交付脚本、targeted selector，以及在临时目录内生成的 DIBCO/NoisyOffice 形状合成外部 CLI smoke。

`scripts/ci_regression_groups.py` 是分组事实来源。它提供：

- `list-groups`：列出四个 CI 组。
- `list-tests <group>`：列出某组的 unittest 模块。
- `verify-coverage`：检查所有 `tests/test_*.py` 正好被分配一次，发现缺失、重复或已删除测试时失败。
- `run <group>`：先做覆盖校验，再运行该组测试；`external-validation` 还会运行合成外部 CLI smoke。

这种方式的优点：

- 失败域按产品能力拆开，定位比 hash shard 更直接。
- 新测试必须显式归组，避免“完整回归存在但 CI 没有真实覆盖”的漂移。
- 外部验证使用合成数据在 CI 内闭环，真实 DIBCO/NoisyOffice 样本仍留在批准的私有发布验证环境。

局限：

- 分组列表需要随新增测试维护；`verify-coverage` 会把遗漏变成显式失败。
- 各组耗时不保证完全均衡；如果核心图像处理组长期明显更慢，后续可以继续拆出更细的算法/性能子组。

## 质量边界

当前策略的质量边界如下：

- PR 必须通过三版本 smoke 和 targeted tests。
- main 必须通过 Python 3.11 四组语义回归，以及 Python 3.10/3.12 compatibility smoke。
- 每日定时和手动 `deep-full` 提供三版本四组语义回归兜底。

不再要求每个 PR 或每个 main push 都跑三版本完整测试。三版本完整测试用于发现跨版本慢性问题，而不是阻塞每个小 PR。

## 预期耗时

基于当前测试规模：

- PR smoke：通常几十秒。
- PR targeted tests：秒级到数分钟，取决于变更范围。
- main regression groups：目标 7-10 分钟级，取决于核心图像处理组中的算法深度回归耗时。
- deep-full：仍可能是 10-20 分钟级，用于定时和手动深度验证，取决于三版本四组矩阵的最慢组合。

实际耗时应以 GitHub Actions run 记录为准。

## 维护规则

新增测试或模块时，按下面规则维护 CI：

- 新增普通测试文件：必须加入 `scripts/ci_regression_groups.py` 的一个语义组；`verify-coverage` 会阻断未归组测试。
- 新增高价值 PR 阶段测试：确认文件名符合 `tests/test_*.py`。
- 新增新的图像处理子模块：在 `PR targeted tests` 的路径映射里增加对应专项测试文件。
- 某个语义组长期明显变慢：考虑继续按能力或历史耗时拆分该组。
- 发布前或大范围算法改动后：手动触发 `workflow_dispatch`，选择 `deep-full`。

## 判断 CI 失败的优先级

1. PR smoke 失败
   - 优先看安装、编译、打包或 CLI 是否损坏。

2. PR targeted tests 失败
   - 优先看本 PR 的直接功能或回归测试。

3. main regression group 失败
   - 说明语义回归兜底发现了 PR 阶段没有覆盖的问题，应按失败组对应能力面阻断后续合并并修复。

4. deep-full 失败
   - 如果只有某个 Python 版本失败，优先判断跨版本兼容性。
   - 如果三版本都失败，优先按失败语义组处理。
