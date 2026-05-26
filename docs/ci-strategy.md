# CI 策略说明

本文档说明当前 GitHub Actions CI 的分层策略、触发条件、质量边界和维护方法。目标是在不牺牲主干质量的前提下，减少每个 PR 和每次合并后的等待时间。

## 目标

- PR 反馈要快，默认不跑完整图像回归套件。
- main 分支仍要有完整质量兜底。
- Python 3.10 和 3.12 保留兼容性验证，但不在每次 main push 上重复跑完整测试。
- 三版本完整全量测试保留在定时和手动深度验证里。
- CI 不依赖 runner 内部的 `GITHUB_TOKEN` checkout，继续使用公开 ref fetch，避免再次被 token 侧 403 阻断。

## 触发矩阵

### Pull Request

PR 触发两个层级：

1. `PR smoke Python 3.10/3.11/3.12`
   - 安装包。
   - 校验生产工作台前端。
   - 编译 `src` 和 `tests`。
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

2. `Main full tests Python 3.11 shard 1/4 ... 4/4`
   - Python 3.11 跑完整 `unittest`。
   - 测试按稳定 hash 分为 4 个 shard 并行执行。
   - 这样保留 main 的完整测试兜底，同时降低 wall-clock 时间。

### workflow_dispatch

手动触发支持两种模式：

- `standard`
  - 等同于 main push 策略。
  - 适合验证 workflow 修改或重新确认主干状态。

- `deep-full`
  - Python 3.10、3.11、3.12 都运行完整测试、编译、构建和 CLI smoke。
  - 适合发布前、重大算法改动后、或怀疑跨版本差异时使用。

### schedule

定时任务每天 UTC 18:00 运行 `deep-full` 等价验证。

该时间约为北京时间 02:00，避免阻塞白天无人值守开发节奏。

## 测试选择规则

`PR targeted tests` 读取 PR 相对 base 的变更文件：

- `tests/test_*.py`
  - 如果 diff 中新增了 `def test_*`，只运行新增测试方法。
  - 否则运行整个测试文件。

- `src/archive_scan_qc/*`
  - 默认运行 `tests/test_scan_qc.py`。

- `src/archive_scan_qc/*scan_processing*`
  - 额外运行：
    - `tests/test_scan_processing_combo.py`
    - `tests/test_scan_processing_reuse.py`
    - `tests/test_scan_processing_algorithm_regression.py`

- 路径包含背景污渍、边缘阴影、色调归一、扫描线相关关键词时，额外运行对应专项测试文件。

这些规则偏保守：PR 阶段尽量跑相关测试，但不跑全仓库测试。

## 完整测试分片

main push 的完整测试在 Python 3.11 上按 4 个 shard 并行：

- 使用 `unittest.defaultTestLoader.discover("tests")` 收集完整测试 ID。
- 对每个测试 ID 做 SHA-256 hash。
- 按 `hash % 4` 分配到 shard。
- 每个 shard 用 `PYTHONPATH=src:tests python -m unittest <test ids...>` 执行。

`discover("tests")` 产生的测试 ID 是 `test_xxx.Class.method` 形式，直接按 ID 执行时需要把 `tests/` 放进 `PYTHONPATH`。完整 discover 可以只用 `PYTHONPATH=src`，但分片后的按 ID 执行必须同时包含 `tests`，否则 CI 会把每个测试模块误判为不可导入。

这种方式的优点：

- 分片稳定，不依赖测试文件顺序。
- 新测试会自动进入某个 shard。
- 不需要维护静态测试列表。

局限：

- hash 分片按测试数量近似均衡，不保证按耗时完全均衡。
- 如果某些图像回归测试明显更慢，后续可以改成基于历史耗时的静态 shard 表。

## 质量边界

当前策略的质量边界如下：

- PR 必须通过三版本 smoke 和 targeted tests。
- main 必须通过 Python 3.11 完整测试分片，以及 Python 3.10/3.12 compatibility smoke。
- 每日定时和手动 `deep-full` 提供三版本完整测试兜底。

不再要求每个 PR 或每个 main push 都跑三版本完整测试。三版本完整测试用于发现跨版本慢性问题，而不是阻塞每个小 PR。

## 预期耗时

基于当前测试规模：

- PR smoke：通常几十秒。
- PR targeted tests：秒级到数分钟，取决于变更范围。
- main full sharded：目标 3-5 分钟级，取决于最慢 shard。
- deep-full：仍可能是 10-12 分钟级，用于定时和手动深度验证。

实际耗时应以 GitHub Actions run 记录为准。

## 维护规则

新增测试或模块时，按下面规则维护 CI：

- 新增普通测试文件：无需改 CI，`unittest discover` 会自动纳入 main full 和 deep-full。
- 新增高价值 PR 阶段测试：确认文件名符合 `tests/test_*.py`。
- 新增新的图像处理子模块：在 `PR targeted tests` 的路径映射里增加对应专项测试文件。
- 某个 shard 长期明显变慢：考虑切换为基于历史耗时的静态 shard 分配。
- 发布前或大范围算法改动后：手动触发 `workflow_dispatch`，选择 `deep-full`。

## 判断 CI 失败的优先级

1. PR smoke 失败
   - 优先看安装、编译、打包或 CLI 是否损坏。

2. PR targeted tests 失败
   - 优先看本 PR 的直接功能或回归测试。

3. main full shard 失败
   - 说明完整测试兜底发现了 PR 阶段没有覆盖的问题，应阻断后续合并并修复。

4. deep-full 失败
   - 如果只有某个 Python 版本失败，优先判断跨版本兼容性。
   - 如果三版本都失败，优先按普通完整回归处理。
