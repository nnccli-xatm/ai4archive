# 自动化开发流程优化沉淀记录

记录自动化开发过程中暴露出的流程问题、修补动作和可复用规则。目标是把一次性经验沉淀到 portable skill、脚本、配置模板、自动化 prompt 或项目文档中，避免只留在对话、临时 state 或本机记忆里。

## 沉淀原则

- 先区分一次性执行问题和可复用流程缺口。可在其他 issue、PR、机器或迁移环境复现的问题，必须做 durable hardening。
- 优先落到最小可复用表面：`SKILL.md`、`scripts/`、`references/`、`config.example.json`、迁移包模板、active heartbeat prompt 或本文档。
- 只记录聚合、安全、可公开的信息；不写入密钥、私有样本路径、文件名、哈希、缩略图、OCR 文本或行级证据。
- 如果产品 PR 被流程问题阻塞，先路由、恢复或回收同一个 Linear issue/PR，再补流程硬化；不要绕过 Symphony 直接接管产品代码实现。
- 每次硬化要说明触发场景、修复位置、复用价值和剩余风险。

## 已沉淀事项

### 2026-06-03: 用 symphony-zy 替代 symphony-TM 的本地部署

- 触发场景：迁移包默认包含 `symphony-TM`，但当前部署要求使用最新可用的 `symphony-zy v1.0`。
- 修复位置：
  - `C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run`
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\config.local.json`
  - `docs/local-symphony-zy-automation-deployment.md`
- 复用规则：本机恢复或重启只使用 `run_symphony_zy_windows.ps1 -Detach`，不要重启旧 `symphony-TM` 或 `ai4archive-linear-runtime`。
- 剩余风险：公网 ingress 方案已在 `docs/CLOUDFLARE_DOMAIN_TUNNEL.md` 沉淀；实际切流仍需在 Cloudflare、Linear/GitHub 设置和本机 env 中配置真实域名、tunnel UUID、URL 和密钥。

### 2026-06-03: health checker 的 Windows 进程检测兼容性

- 触发场景：portable health checker 在 Windows 上检测 Codex 进程时依赖 Unix `ps/pgrep`，导致自检崩溃。
- 修复位置：
  - `docs/ai4archive-webhook-symphony-migration-kit/resources/ai4archive-symphony-delivery/scripts/check_ai4_symphony.py`
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\scripts\check_ai4_symphony.py`
- 复用规则：进程检测不能只依赖 Unix 命令；Windows 环境使用 PowerShell/CIM fallback。
- 剩余风险：迁移包重新打包或 skill 被覆盖后，需要确认 fallback 仍存在。

### 2026-06-04: heartbeat cadence 回归为 watchdog

- 触发场景：部署后 heartbeat 为 5 分钟，容易退化成高频轮询；新版流程要求 webhook 和 Symphony 承担实时推进，heartbeat 只做兜底 watchdog。
- 修复位置：
  - Codex automation `rdis13-symphony-delivery-loop`
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\config.local.json`
  - `docs/local-symphony-zy-automation-deployment.md`
- 复用规则：默认 heartbeat cadence 为 30 分钟；只有明确恢复场景才临时缩短，恢复后再调回 30 分钟。
- 剩余风险：如果 webhook ingress 未切通，30 分钟 cadence 只保证兜底，不保证实时触发。

### 2026-06-04: Linear connector 不可用时的最小回退

- 触发场景：Linear connector 返回重新认证或传输错误，但本机 Symphony ZY env 中已有可用 Linear API token。
- 修复位置：
  - 当前操作流程使用 Linear GraphQL 直接查询和创建 issue，不打印 token。
  - active heartbeat prompt 保留健康检查和 pending mutation 规则。
- 复用规则：Linear connector 不可用时，可以用本地受控 env 中的 Linear API token 做最小 GraphQL 操作；命令输出只允许 issue 编号、状态、URL 等聚合/公开字段，不能打印密钥。
- 剩余风险：该回退属于本机部署能力，portable 文档中仍应优先使用 connector；长期应恢复 connector 认证。

### 2026-06-04: Todo 长时间空转和 GraphQL 多行写入失败恢复

- 触发场景：`AI4-863` 在 Symphony 中运行超过 30 分钟仍停留在 `Todo`，日志显示 agent 尝试把多行 Markdown 直接插入 Linear GraphQL mutation，触发语法错误；工作区只产生分析文档和构建缓存，没有形成代码、测试、PR 或有效状态推进。
- 修复位置：
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\scripts\check_ai4_symphony.py`
  - `docs/ai4archive-webhook-symphony-migration-kit/resources/ai4archive-symphony-delivery/scripts/check_ai4_symphony.py`
  - `C:\Users\PS\code\symphony-zy\WORKFLOW.md`
  - `C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run\WORKFLOW.md`
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\SKILL.md`
- 复用规则：health check 默认把运行中的 `Todo` 超过 30 分钟标为失败；workflow 要求 `Todo` 进入执行前先切到 `In Progress`，所有 Linear GraphQL 写入必须用 variables 传 Markdown/body 文本，并提供 `issue(id:)`、`workflowStates(filter:)`、`issueUpdate(id,input)`、`commentCreate(input)` 示例；实现类 issue 不能只用“无需代码变更”作为交付结论，除非 issue 明确是调查类任务。
- 剩余风险：已经跑偏的同一 issue 需要停止当前坏会话后重新触发；如果上游 Symphony 以后提供一等 Linear 写 API，应把 raw GraphQL 约束迁移到工具层。

### 2026-06-04: 无 PR 进入 In Review 后 Symphony 空闲

- 触发场景：`AI4-863` 被 agent 移到 `In Review`，但工作区仍有实质未提交变更，且 Linear issue 没有关联 PR；`In Review` 不在 Symphony active states 中，日志显示 `Issue no longer visible, removing claim`，导致调度器停止继续开发。
- 修复位置：
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\scripts\check_ai4_symphony.py`
  - `docs/ai4archive-webhook-symphony-migration-kit/resources/ai4archive-symphony-delivery/scripts/check_ai4_symphony.py`
  - `C:\Users\PS\code\symphony-zy\WORKFLOW.md`
  - `C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run\WORKFLOW.md`
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\SKILL.md`
- 复用规则：compact health 在 Symphony 无 running/retrying 时扫描 issue 工作区；如果存在有意义的未提交变更，返回 `failed=idle_dirty_workspaces`。workflow 明确本项目评审态是 `In Review`，只有在分支已提交、推送、PR 已关联、验证已记录且工作区无实质未提交变更时才能进入；否则保持或移回 `In Progress` 继续同一 issue。
- 剩余风险：health checker 只用本地 git 状态识别 idle dirty workspace，不直接验证 PR 是否存在；后续可增加轻量 PR attachment 检查，但需避免把 Linear/GitHub token 依赖放入默认健康检查。

### 2026-06-04: heartbeat watchdog 未主动发现 idle dirty 停滞

- 触发场景：`AI4-863` 停在 `In Review` 且工作区有未提交变更期间，heartbeat automation 没有主动恢复。原因是 active prompt 只在 compact probe 显示异常时深入检查；旧 health check 又把 `running=0/retrying=0` 当作 `ok=true`，导致 watchdog 误判为可停止的健康 idle。
- 修复位置：
  - Codex automation `rdis13-symphony-delivery-loop`
  - `docs/ai4archive-webhook-symphony-migration-kit/templates/heartbeat-automation-prompt.md`
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\scripts\check_ai4_symphony.py`
  - `docs/ai4archive-webhook-symphony-migration-kit/resources/ai4archive-symphony-delivery/scripts/check_ai4_symphony.py`
- 复用规则：heartbeat prompt 必须把 `ok=false` 当作恢复任务；`failed=idle_dirty_workspaces` 或 `idle_dirty=` 必须恢复同一 issue，不能创建新 issue 或只报状态；即使 compact health 显示 `running=0/retrying=0`，也必须先排除脏工作区、pending mutations、queued webhook triggers 和未完成同 issue 状态后，才允许当作健康 idle。
- 剩余风险：heartbeat 仍是 30 分钟兜底，不替代 webhook 实时触发；如果 automation runner 本身未唤醒，需要另查 Codex automation 执行日志或客户端状态。

### 2026-06-04: after_run 拦截无效 In Review 交接

- 触发场景：`AI4-863` 再次被移动到 `In Review`，但工作区仍有实质未提交变更且没有关联 PR；仅靠 prompt guardrail 和 heartbeat 兜底不足以阻止 Symphony 释放 claim。短暂把 `In Review` 纳入 active states 会导致正常 PR 评审单被重复认领，因此不能作为最终方案。
- 修复位置：
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\scripts\recover_invalid_review_handoff.py`
  - `docs/ai4archive-webhook-symphony-migration-kit/resources/ai4archive-symphony-delivery/scripts/recover_invalid_review_handoff.py`
  - `C:\Users\PS\code\symphony-zy\WORKFLOW.md`
  - `C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run\WORKFLOW.md`
- 复用规则：不要把 `In Review` 放入 active states。用 `after_run` hook 在每轮 agent 结束后检查当前工作区对应的 Linear issue；如果 issue 已进入 `In Review`，但没有 GitHub PR 附件或工作区存在有意义的未提交变更，则自动移回 `In Progress` 并写入简短恢复说明。PR 已关联且工作区干净的正常评审单不动。
- 剩余风险：hook 依赖本机 `LINEAR_API_KEY` 和 Linear GraphQL 可用；如果 Linear 临时网络失败，health checker 与 heartbeat 仍要作为兜底恢复同一 issue。

### 2026-06-04: webhook 本地健康与真实 Linear 入站脱节

- 触发场景：compact health 显示 `webhook=yes`，但真实 Linear 评论事件没有投递到本机 bridge。排查发现本地 `127.0.0.1:4010` bridge 能接受带签名的 Linear 形态 POST 并转发给 Symphony；Linear 侧唯一 webhook 处于 disabled，且其公网入口仍指向旧 trycloudflare tunnel，公网 `/healthz` 不可达。后续已启动指向 `127.0.0.1:4010` 的新 tunnel，更新并启用 Linear webhook，且同步 webhook secret 后真实 `Comment` 事件返回 202。
- 修复位置：
  - 本次 watchdog 恢复流程已确认本地 bridge -> Symphony 正常，并将 `AI4-863` 从无 PR 的 `In Review` 恢复到 `In Progress` 后触发 refresh。
  - Linear webhook 已更新为当前 tunnel host、`enabled=true`、订阅 `Issue`/`Comment`，并与本地 `LINEAR_WEBHOOK_SECRET` 保持一致。
  - `docs/CLOUDFLARE_DOMAIN_TUNNEL.md` 已补充固定域名 Cloudflare Tunnel runbook，用于替代会变更 URL 的 trycloudflare quick tunnel。
  - 后续应在 portable health check 或部署校验中增加“Linear webhook enabled + URL host/path + tunnel /healthz 指向当前 bridge”的可选检查。
- 复用规则：`webhook=yes` 只能说明本地 bridge 健康，不能证明 Linear 真实入站工作。真实入站验证必须包含至少一次 Linear 侧事件投递，或通过 Linear GraphQL 确认 webhook enabled、订阅 `Issue/Comment`，并确认公网入口能到达当前 `AI4_WEBHOOK_PORT`。
- 剩余风险：当前运行态仍使用 trycloudflare quick tunnel，进程退出或机器重启后 URL 可能变化；该风险已有固定域名 Cloudflare Tunnel 方案，但要等真实 `CF_PUBLIC_HOSTNAME` 切入、Linear/GitHub webhook URL 更新并完成 provider redelivery/smoke test 后才算完全关闭。

### 2026-06-04: 测试通过后 git 交接失败仍进入 In Review

- 触发场景：`AI4-863` 多轮运行后已到测试通过阶段，但提交准备阶段先尝试了被本机策略拒绝的递归删除/强制删除命令，随后 `git add` 因 `.git/index.lock` 创建权限错误失败；未形成 commit、push 或 PR，issue 却被移到 `In Review`，导致 Symphony 释放 claim 后留下 idle dirty workspace。
- 修复位置：
  - 本次恢复中停止了挂起的只读 git 查询进程，确认 issue 无关联 PR，将 `AI4-863` 移回 `In Progress`，写入聚合恢复说明，并触发 Symphony refresh。
  - 实时跟踪时确认 `.git/index.lock` 可被手工创建并移除，说明故障更像 Git 进程/监控竞争，而不是永久目录权限损坏；恢复期间删除了误导性的 Linear 完成状态评论。
  - 当前不直接接管产品代码实现；由 Symphony 继续同一 issue 的提交、推送和 PR 交接。
- 复用规则：测试通过不等于可进入评审态。只有 `git add/commit/push` 成功、PR 已创建或关联，并且工作区没有实质未提交变更时，agent 才能把 issue 移到 `In Review`；任何提交/推送失败都必须保持 `In Progress` 并记录失败原因。Windows 环境下避免使用会被策略拒绝的递归强制删除清理命令，临时构建缓存不应阻塞最小可提交差异。实时监控同一 issue 时避免频繁在该工作区运行 `git status`，优先使用 Symphony state、session log、Linear/GitHub 元数据，防止监控本身参与 index 竞争。
- 剩余风险：如果下一轮仍因 GitHub Desktop、fsmonitor 或后台 git 查询造成 index lock 权限异常，需要进一步收敛工作区 git 监控来源，或在 portable health/recovery 中加入“提交前发现挂起只读 git 进程时先清理”的检查；如果验证阶段继续找不到 `pytest` 或无法导入包，需要把 Windows 环境验证命令固化为项目可用的最小命令。

## 后续记录模板

### 2026-06-04: AI4-863 长时间停滞的 git/PR 交接硬化

- 触发场景：AI4-863 多次生成本地实现和验证结果后，始终无法形成 commit、push 和 PR；worker 反复在 sandbox 只读 `.git` 上执行 direct git 写操作，并尝试 destructive recovery 命令，导致 token 大量消耗、无 PR 进入 Review、再由 watchdog 拉回的循环。
- 修复位置：
  - `C:\Users\PS\code\symphony-zy\bin\safe-git\`
  - `C:\Users\PS\code\symphony-zy\run_symphony_zy_windows.ps1`
  - `C:\Users\PS\code\symphony-zy\WORKFLOW.md`
  - `C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run\WORKFLOW.md`
  - `C:\Users\PS\code\symphony-zy\docs\git-handoff-hardening.md`
- 复用规则：Windows runtime 启动时把 safe-git 放到 PATH 前缀；issue workspace 内的 git 命令自动使用 `.git-meta`，并阻断 `reset --hard`、`clean`、`stash`、path checkout/restore；缺少已认证 `gh` 或缺少 PR 时必须保持 `In Progress`，不得进入 `In Review`。
- 追加修复：实时复测发现仅在启动脚本中添加 safe-git PATH 不足够；`mise exec` 启动链和 Codex 命令执行环境重建都会导致 worker 内 `Get-Command git` 仍解析到系统 Git，从而继续触发 `.git/index.lock` 权限错误。`symphony-zy` 启动脚本已改为通过 `mise which escript` 解析真实 `escript.exe` 并直接启动，同时在 WORKFLOW 的 Codex command 中用 `shell_environment_policy.set.*` 显式设置 `PATH`、`AI4_REAL_GIT`、`AI4_SAFE_GIT_BIN`、`AI4_SAFE_GIT_WORKSPACE_ROOT`。文档要求恢复后必须在 worker 内验证 `Get-Command git` 指向 `bin\safe-git\git.cmd`。
- 二次修复：继续复测发现不能把 Git Bash 的 `${PATH}` 直接传给 Codex；该值会被转换为 POSIX 冒号分隔路径，Windows worker 随后找不到 `powershell.exe`、`python` 和 `git.exe`，导致 863 再次空转。启动脚本现在生成 `AI4_CODEX_WINDOWS_PATH`，保留 Windows 原生分号 PATH，并显式补齐 System32、WindowsPowerShell、Python、safe-git、GitHub CLI 等关键目录；WORKFLOW 的 `shell_environment_policy.set.PATH` 改为使用该变量。
- 三次修复：同一轮复测中，`git add` 和本地 commit 已成功，但 `git push` 两次被 Codex shell 默认 10 秒超时杀掉，表现为 `Exit code: 124`，不能误判为实现失败或凭证失败。WORKFLOW 现在要求 `git push`、`gh pr create`、性能/集成验证命令显式设置较长 shell timeout；默认 10 秒超时只表示命令预算不足。
- 剩余风险：GitHub CLI 已安装但仍需非交互认证才能让 Symphony 自主 `gh pr create`；在认证完成前，Symphony 可以继续实现和提交本地分支，但 PR 创建仍可能需要 Codex/GitHub connector 兜底。

### 2026-06-04: AI4-863 push timeout recovery and safe-git health probes

- 触发场景：AI4-863 已通过 safe-git 成功建分支、`git add`、本地 commit，但 worker 仍用默认 shell timeout 执行 `git push`，连续以 10 秒 `Exit code: 124` 失败并停在 `In Progress`。
- 修复位置：
  - `C:\Users\PS\.codex\skills\ai4archive-symphony-delivery\scripts\check_ai4_symphony.py`
  - `C:\Users\PS\code\symphony-zy\docs\git-handoff-hardening.md`
  - `C:\Users\PS\code\symphony-zy\WORKFLOW.md`
  - `C:\Users\PS\code\symphony-zy\WORKFLOW.zy.example.md`
  - `C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run\WORKFLOW.md`
- 复用规则：当 issue workspace 存在 `.git-meta` 时，health checker 必须用 `GIT_DIR=.git-meta` 和 `GIT_WORK_TREE=<workspace>` 判断真实 worker git 状态，不能用普通 `.git` 视图误报脏工作区。`git push` 和 PR 创建必须使用长超时；10 秒超时只代表 shell 预算不足。若 Symphony 已完成本地提交但发布交接失败，orchestrator 可只接管 push/PR/comment/state handoff，不接管产品实现。
- 剩余风险：GitHub CLI 仍未登录，当前 PR 创建依赖 GitHub connector 兜底；若后续要完全无人值守，需要配置非交互式 `gh` 认证或把 PR 创建能力下沉到 Symphony 的受控工具层。

```text
### YYYY-MM-DD: 标题

- 触发场景：
- 修复位置：
- 复用规则：
- 剩余风险：
```
