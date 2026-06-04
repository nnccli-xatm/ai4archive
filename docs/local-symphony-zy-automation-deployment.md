# 本地 Symphony ZY 自动化开发部署记录

记录日期：2026-06-03

## 部署目标

按 `docs/ai4archive-webhook-symphony-migration-kit` 的迁移思路在本机部署自动化开发闭环，但运行时不再使用迁移包内的 `symphony-TM` 快照，改用本机最新可用的 `symphony-zy v1.0`。

## 当前绑定

- 主项目仓库：`D:\pic-qc\ai4archive`
- Symphony ZY 源码：`C:\Users\PS\code\symphony-zy`
- Symphony ZY 版本：`v1.0`，HEAD `93a3c5e6c00284ac4d0baba2e72cf02e9c865901`
- 本地运行配置目录：`C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run`
- Codex skill：`C:\Users\PS\.codex\skills\ai4archive-symphony-delivery`
- Linear project slugId：`245c02afedba`
- GitHub repo：`nnccli-xatm/ai4archive`
- Symphony workspace root：`C:\Users\PS\code\ai4archive-symphony-workspaces`

## 本地服务入口

- Symphony state API：`http://127.0.0.1:4000/api/v1/state`
- Symphony dashboard：`http://127.0.0.1:4000/`
- Enhanced webhook bridge：`http://127.0.0.1:4010/healthz`
- Zhipu proxy health：`http://127.0.0.1:41239/health`

## 运行目录内容

`C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run` 保存本机部署配置：

- `WORKFLOW.md`：ai4archive 专用工作流，使用 Linear project `245c02afedba`、Zhipu local Responses proxy 和 ai4archive workspace root。
- `.env.symphony.local`：本机密钥和端口配置，禁止提交或公开。
- `run_symphony_zy_windows.ps1`：启动包装脚本，调用 `symphony-zy\run_symphony_zy_windows.ps1` 并固定使用 enhanced bridge。

恢复或重启时只使用：

```powershell
C:\Users\PS\code\symphony-zy\tmp\ai4archive-local-run\run_symphony_zy_windows.ps1 -Detach
```

不要再启动旧的 `symphony-TM` 或 `ai4archive-linear-runtime`。

## Heartbeat 自动化

- Automation ID：`rdis13-symphony-delivery-loop`
- 名称：`AI4Archive Symphony ZY delivery watchdog`
- 状态：`ACTIVE`
- 周期：`FREQ=MINUTELY;INTERVAL=30`
- 作用：作为 30 分钟兜底 watchdog；实时推进由 Symphony 和 webhook 触发承担。每次 heartbeat 先做 compact probe，只有 PR/CI 变化、Symphony 异常、idle gap、queued webhook trigger 或设计 checkpoint 到期时才展开处理。

## 已验证

- `check_ai4_symphony.py --compact`：`ok=true`，`webhook=yes`，`automation=active`
- `http://127.0.0.1:4000/api/v1/state`：HTTP 200
- `http://127.0.0.1:4010/healthz`：HTTP 200
- `http://127.0.0.1:41239/health`：HTTP 200，模型 `glm-4.7`
- Linear 真实 `Comment` webhook 已通过公网 tunnel 投递到 4010 bridge，并由 Symphony 返回 HTTP 202；测试评论已删除，仅保留聚合验证结论。
- Portable skill Python 脚本已通过 `py_compile`
- `symphony-zy` bridge、proxy 脚本已通过 `py_compile`

## 公网 webhook ingress

长期固定入口方案见 `docs/CLOUDFLARE_DOMAIN_TUNNEL.md`。该 runbook 使用 Cloudflare 管理的域名 tunnel，把公网 `/api/v1/webhooks/linear` 和 `/api/v1/webhooks/github` 只转发到本机 `127.0.0.1:4010` enhanced bridge，并用 catch-all 404 阻断其他路径；不要把 `127.0.0.1:4000` Symphony state API 直接暴露到公网。

当前机器为恢复实时触发，临时使用指向 4010 bridge 的 trycloudflare quick tunnel，并已验证 Linear `Issue`/`Comment` webhook 配置和真实事件投递。该状态可以继续支撑当前开发流，但进程退出或机器重启后 URL 仍可能变化；切换到固定域名时，应按 `docs/CLOUDFLARE_DOMAIN_TUNNEL.md` 配置命名 tunnel 或 dashboard-managed tunnel，再更新 Linear/GitHub webhook URL、同步 provider secret，并用 provider redelivery 或签名 smoke test 验证公网入口。

## 注意事项

- Windows 本地部署不使用 tmux，因此健康检查里的 tmux 项是非必需项。
- 4010 bridge 是公网 webhook 的唯一允许本地 origin。固定域名 Cloudflare Tunnel 配置已沉淀在 `docs/CLOUDFLARE_DOMAIN_TUNNEL.md`；真实域名、tunnel UUID、provider webhook URL 和密钥只放在 Cloudflare、Linear/GitHub 设置或本机 env 中，不写入公开文档。
- 如果后续迁移包再次安装 portable skill，需保留本机 `config.local.json` 和 state，并确认 health checker 的 Windows 进程检测 fallback 仍存在。

## 流程硬化沉淀

自动化开发过程中发现的流程缺陷、重复性故障和可复用优化，需要沉淀到 `docs/automation-flow-hardening-log.md`。如果问题会影响后续 issue、PR、迁移环境或其他机器，还应同步修补 portable skill、脚本、配置模板或 active heartbeat prompt，不能只留在当前对话或本机临时 state 中。
