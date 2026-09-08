# 配置与部署

同一源码支持 Vercel 与 EdgeOne Cloud Functions。比较两者时，每次只让一个部署接收同一个 Slack App 的事件，以免双重执行。

## 1. 专用资源

准备本项目专用的 Upstash Redis 和 QStash；不复用公司业务数据库。Redis 保存映射和写入状态；QStash 负责持久化事件、消费重试与失败队列。

按 [.env.example](.env.example) 配置环境变量。凭据放平台 Secret 配置，不提交到 Git。QStash 的 current/next signing key 用于校验消费请求，不能只校验一个自定义静态请求头。

## 2. Multica

- 在目标 Workspace 创建专用 Project 和 Agent，绑定需要使用的 Runtime。
- 将 [AGENT-PROMPT.md](AGENT-PROMPT.md) 同步为 Agent instructions。
- 配置 Agent 的 `RELAY_OWNER_SLACK_USER_ID`、`RELAY_SKILL_ROOT`。频道和发送者的白名单/黑名单由 Relay 统一校验，Agent 不再读取单频道 `RELAY_ALLOWED_CHANNEL_ID`。
- Slack 操作使用被授权的 USER token；每次 CLI 调用显式覆盖 SLACK_BOT_TOKEN 与 SLACK_TOKEN，防止 shell/Skill 配置选到 Bot。
- 回读 Agent 的 Runtime、权限和并发。初期并发2即可；Mac 休眠/断网会影响执行。
- 读取本地 Skills 和 Workspace 指派 Skills 的实际加载结果。数据库 Skill 数量不能单独说明任务可用能力。
- Relay 使用 MULTICA_PROJECT_ID/MULTICA_AGENT_ID 调用普通 Issue API；不再需要 Autopilot。

Agent instructions 写入任务工作目录 AGENTS.md。Multica daemon 为 Codex 准备任务环境；桌面聊天上下文不会自动复制。现有 Codex 适配器会自动批准工具请求，Prompt/Skills 只能构成行为合同；不可绕过的写审批需要执行端或工具端支持。

### 完成 Hook 与 Footer（第一阶段）

完整范围、统计口径和阶段状态见 [Footer 计划](FOOTER-PLAN.zh-CN.md)。本阶段只实现原消息追加耗时，尚未开启线上链路。Vercel 与 Runtime 的 `RELAY_FOOTER_ENABLED` 必须一致；默认关闭。开启时 Relay 停止查询 Agent 模型配置，不再发送 replyContext；关闭时暂保留旧模型快照兼容路径。完整统计与旧代码删除在第二阶段交付。

Vercel 在现有 Redis、QStash 和 Multica 配置之外，还需要：

| 配置 | 用途 |
| --- | --- |
| `MULTICA_PLUGIN_INSTALLATION_ID` / `MULTICA_PLUGIN_SIGNING_SECRET` | 目标安装 ID 与对应 whsec 签名密钥；不是 Slack Signing Secret |
| `RELAY_FOOTER_CONSUMER_URL` | 当前部署的准确 `/api/queue/footer` URL，用于 QStash 发布与验签 |
| `RELAY_REPLY_TOKEN` | Runtime 登记身份的独立随机凭据，至少 32 字符 |
| `SLACK_REPLY_ACTOR` / `SLACK_REPLY_TOKEN` | 明确的 user 或 bot 与同一作者写入 token；当前线上使用 user |
| `SLACK_READ_TOKEN` | 可选，同工作区的 thread 读取 token；省略则使用 SLACK_REPLY_TOKEN |

Runtime 配置 `RELAY_REPLY_SCRIPT`（本仓库 scripts/reply.py 的持久化绝对路径）、`RELAY_SLACK_CLI`（现有 Slack Skill 的 scripts/slack.py）、`SLACK_REPLY_ACTOR`、`SLACK_TEAM_ID`、`RELAY_REPLY_TOKEN`、`RELAY_REPLY_REGISTER_URL`（`/api/slack/replies`）和 `RELAY_RECEIPT_DIR`（仅运行用户可访问的持久化目录）。`MULTICA_TASK_ID` 与 `MULTICA_WORKSPACE_ID` 由 Runtime 注入，不从 Slack 正文生成。脚本依赖 Python、rtk 和现有 Slack Skill，不需要另一套渲染 SDK。

最终回复示例，运行 ID 自动从环境读取：

```bash
rtk proxy python3 "$RELAY_REPLY_SCRIPT" \
  --issue '<当前 Issue UUID>' \
  --channel '<原频道 ID>' --thread-ts '<根 thread ts>' \
  --text-file '<正文 fallback 文件>' --blocks-file '<正文 blocks 文件>' \
  --format mrkdwn
```

可加 `--dry-run` 只预览。正式调用先复用 Slack Skill 的预览与身份校验，再发送并登记返回的真实 message ts。一个 run 只发一条最终回复；进度消息不经此入口。不要在包装脚本失败后另跑 Slack send：登记失败可用相同参数补登记；sending 状态表示结果不明，需要核对 Slack 与回执，不清空回执后重发。通用 Slack Skill 不需要修改。

插件 manifest 模板在 [multica.plugin.example.json](multica.plugin.example.json)。安装前把 net scope 和 transport URL 中的域名替换为实际 Vercel 域名；第一阶段仅订阅 task.completed，按 Multica 契约授予 tasks:read 和实际回调域名的 net scope，不需要 Action API 写 scope。Hook 使用服务端配置的 Multica 查询凭据；临时 callback_token 在 HTTP 返回后撤销，不能入队。安装所得 ID、签名密钥必须与 Vercel 配置匹配。

按 AGENTS.md 使用 Multica CLI 管理安装与 Agent 配置。当前 CLI 未提供 plugin 子命令，尚不能通过规定入口创建插件安装；不得猜接口或切换浏览器绕过。本次只准备代码、模板和本地 Prompt 候选。CLI 能力补齐后再安装、按同一 ID 回读，核对线上契约，再完成受控验证。

上线次序：准备安装与配置 → 部署新函数（保持开关关闭）→ 部署 Runtime 脚本并核对路径和 User 身份 → 对照最新线上 instructions 同步本地 Prompt 候选 → 协调开启两端开关 → 以明确获准的测试 thread 验收。第一阶段不要把未知 Token 配到不可信 Preview，也不要为联调关闭全局部署保护。

验收至少覆盖：耗时 footer 更新同一条消息、正文和附件保留、重复完成通知、先完成后登记、登记失败只补登记、更新响应丢失后的回读、无最终回复保持静默。缺少 blocks、已有 50 个 blocks 或消息超限时省略 footer，不能截断正文。确认 `:agent_time:` 在工作区存在。


## 3. Slack App

使用专用 App 或明确获准复用的 App 接收需要的 message 事件。私有频道订阅 `message.groups`，并将接收 App 加入指定频道。接收事件的 App 身份与外发身份分开配置：`SLACK_REACTION_TOKEN` 和 Agent 回复使用获准的 owner USER token。验收时核对 `reaction.users` 和回复消息的 `user` 是否等于 owner ID。

配置 Request URL 为 `https://<当前部署>/api/slack/events`，对应 Signing Secret 填入部署环境。新增 scopes 后重新安装。只修改已授权用于 Relay 的 App。

`SLACK_TEAM_ID`、`SLACK_TARGET_USER_IDS` 和 `SLACK_TARGET_SUBTEAM_IDS` 至少一个必填；`SLACK_ALLOWED_CHANNEL_IDS` 保留为白名单配置，默认使用 `all`，也可填写逗号分隔的频道 ID。`SLACK_BLOCKED_CHANNEL_IDS`、`SLACK_ALLOWED_SENDER_IDS` 和 `SLACK_BLOCKED_SENDER_IDS` 可选，黑名单优先于白名单。后续问答仍需再次 mention。

## 4. Vercel

导入仓库，Framework 选 Other，安装使用 `pnpm install --frozen-lockfile`。入口位于 api/；vercel.json 设置消费函数60秒。配置环境变量，RELAY_CONSUMER_URL 必须是该部署的准确公网消费 URL。

不要把生产密钥配置到不可信分支的 Preview。若部署保护拦住 Slack/QStash，优先使用已配置的正式域名/生产部署；不要静默关闭项目全局保护。

## 5. EdgeOne

导入同一仓库，使用 Cloud Functions（Node.js），入口位于 cloud-functions/；不使用 Edge Functions 的受限运行环境。edgeone.json 设置消费函数60秒、海外新加坡区域和 public 静态目录。控制台需选择不含中国大陆的试验区域，以匹配海外依赖。

按当前平台支持选择 Node.js 版本；TypeScript 源码与依赖会由平台构建。配置与 Vercel 相同的变量，但使用本部署的 RELAY_CONSUMER_URL。

## 6. 验收与比较

分别测健康请求、签名事件入队时间、队列到 Issue 的时间、Codex执行时间、最终Slack答复。浏览器访问快不代表Slack回调或入队快。

必须覆盖两个独立thread并发、同thread续问、重复投递、请求超时、创建响应丢失、失败保留、非允许频道/发送者拒绝、Runtime离线恢复。检查QStash失败队列，不能只看函数日志中的HTTP200。

队列中的消息包含Slack正文和附件元数据；Multica也保留内容。按实际需求设置访问权限与平台保留策略。Redis状态保留90天，超过保留窗口不保证重复判定；删除/更改Issue来源标识会影响恢复。

服务端创建/评论不提供完整的幂等接口。ambiguous\_\* 表示写入结果无法确认，需核对Multica；不要清空状态后直接重放。

## 验证记录要求

为每个平台分别保存 immutable commit、环境与地域、Slack 入站耗时、队列消费耗时、最终回复耗时和样本数量。完整回复包含 Agent Runtime 执行，不能只用该指标评定托管平台。

上线前至少回读：Slack Request URL 已验证、`RELAY_CONSUMER_URL` 指向同一部署、真实中文事件验签成功、owner 身份 reaction/回复正确、同 thread 追问复用 Issue、重复事件没有额外任务、临时 503 进入重试且 QStash DLQ 状态可见。Runtime 离线恢复必须单独实测，不能由普通队列重试或 HTTP 200 推断。

EdgeOne Cloud Functions 会把 `Request.body` 暴露为解析值，入口通过 `arrayBuffer()` 保留签名字节；Vercel 入口优先读取原始 Node stream。两边都不能用 `JSON.stringify(parsedBody)` 重建验签原文。
