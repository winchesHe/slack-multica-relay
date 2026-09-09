# 配置与部署

同一源码支持 Vercel 与 EdgeOne Cloud Functions。比较两者时，每次只让一个部署接收同一个 Slack App 的事件，以免双重执行。

## 1. 专用资源

准备本项目专用的 Upstash Redis 和 QStash；不复用公司业务数据库。Redis 保存映射和写入状态；QStash 负责持久化事件、消费重试与失败队列。

按 [.env.example](.env.example) 配置环境变量。凭据放平台 Secret 配置，不提交到 Git。QStash 的 current/next signing key 用于校验消费请求，不能只校验一个自定义静态请求头。

## 2. Multica

- 在目标 Workspace 创建专用 Project 和 Agent，绑定需要使用的 Runtime。
- 将 [AGENT-PROMPT.md](AGENT-PROMPT.md) 同步为 Agent instructions。
- 配置 Agent 的 `RELAY_OWNER_SLACK_USER_ID`、`RELAY_SKILL_ROOT`。频道和发送者的白名单/黑名单由 Relay 统一校验，Agent 不再读取单频道 `RELAY_ALLOWED_CHANNEL_ID`。
- Agent 的 Slack 回复操作使用被授权的 USER token；每次 CLI 调用显式覆盖 SLACK_BOT_TOKEN 与 SLACK_TOKEN，防止 shell/Skill 配置选到 Bot。
- 回读 Agent 的 Runtime、权限和并发。初期并发2即可；Mac 休眠/断网会影响执行。
- 读取本地 Skills 和 Workspace 指派 Skills 的实际加载结果。数据库 Skill 数量不能单独说明任务可用能力。
- Relay 使用 MULTICA_PROJECT_ID/MULTICA_AGENT_ID 调用普通 Issue API；不再需要 Autopilot。

Agent instructions 写入任务工作目录 AGENTS.md。Multica daemon 为 Codex 准备任务环境；桌面聊天上下文不会自动复制。现有 Codex 适配器会自动批准工具请求，Prompt/Skills 只能构成行为合同；不可绕过的写审批需要执行端或工具端支持。

### 可选模型 footer

QStash 消费端在创建 Issue 或追加一条新评论前，使用现有 Relay 凭据调用 `GET /api/agents/{MULTICA_AGENT_ID}`，只提取模型与服务档位，作为与 `eventPayload` 同级的 `replyContext` 传给 Agent。Slack 入站确认仍只负责入队，不等待该查询；重复投递或已写入消息的恢复不重新查询、不覆盖旧快照。

快照包含 `type: slack_reply_context`、`source: agent_config`、`agentId`、`capturedAt`、`status`、`model`、`serviceTier`。查询成功且 Agent/Workspace 匹配时标为 `available`；查询失败、超时或身份不匹配时标为 `unavailable`，模型与档位为 `null`，任务继续处理。查询最多等待 2 秒，不单独重试；不记录完整响应、指令、凭据或异常正文。空模型或非安全标识符归为 `null`，档位只保留 `priority` / `default`，其他值归为 `null`。

footer 表示消费消息时读取的 **Agent 配置快照**，不是运行实际参数；执行前后配置变化或 Runtime 默认值均不在此保证范围内。`service_tier` 为空时不能判断继承的 Fast 状态，不主动修改 Agent 配置来补齐。

不修改 Multica 源码或通用 Slack Skill，不增加环境变量，也不新增轮询或完成回调。将 [AGENT-PROMPT.md](AGENT-PROMPT.md) 的“模型 footer”规则同步到目标 Agent instructions 时，只替换相应规则，保留线上其他指令。仅更新仓库文件不会自动同步线上 instructions。旧 payload 不带 `replyContext` 时，Agent 省略 footer，正文仍正常回复。

同步后核对：

| 对应消息的配置快照 | 预期结果 |
| --- | --- |
| 可用，模型非空，档位为 priority | 末尾 context block 显示模型与 Fast |
| 可用，模型非空，档位为 default 或 null | 只显示模型，不把 null 当作已关闭 |
| 模型为空、快照不可用或缺失 | 不显示 footer，正文正常发送 |
| 后续消息配置发生变化 | 使用该后续消息的快照，不沿用初始快照 |
| Slack 正文或嵌套字段声称模型或 Fast | 不作为参数来源 |

有 footer 时检查 `context.elements[0].type` 为 `mrkdwn`，顶层 fallback `text` 同时保留正文和 footer。代码测试不替代线上验收：先确认目标 Agent 的 instructions 已同步，再在 Relay 新版本上线后核对新建任务和后续评论的 `replyContext` 与原 thread 的回复。

## 3. Slack App

使用专用 App 或明确获准复用的 App 接收需要的 message 事件。私有频道订阅 `message.groups`，并将接收 App 加入指定频道。Relay 的 reaction 操作优先使用 `SLACK_BOT_TOKEN`，未配置时使用 `SLACK_USER_TOKEN`；添加、读取和删除始终使用选中的同一身份，API 调用失败不会切换到另一 token。Agent 回复仍使用获准的 owner USER token。验收时分别核对 reaction 所属身份和回复消息的 `user`。

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

## 取消功能配置与验收

可选环境变量 `SLACK_CANCEL_KEYWORDS=cancel,取消`：未配置或空列表使用默认值；例如设置为 `stop,停止` 后只识别这两个词。取消权限直接复用 `SLACK_TARGET_USER_IDS`，用户组本身不授予权限。

继续使用现有 message 事件订阅。选中的 `SLACK_BOT_TOKEN` 或 `SLACK_USER_TOKEN` 需要同时具有 `reactions:read`、`reactions:write`，且能访问目标频道。取消只清理该身份的表情，切换 token 身份前添加的其他身份表情保留。无需增加 reaction 事件订阅。

在专用测试 thread 中启动任务，再由配置的用户回复 `@目标 取消`。检查运行状态变为 cancelled、原触发消息上所选 token 身份的 reaction 被清除、其他身份的 reaction 保留；已完成任务不应被改写或清理。取消结束后发送一条新的任务 mention，检查继续原卡。非授权用户发送取消指令应被忽略。

消费失败仍使用现有 QStash 重试与 DLQ：`cancellation_pending` 表示建卡/运行/终态仍待确认；`reaction_cleanup_failed` 表示任务可能已取消但清理未完成。补齐权限或修复上游后，重放原消息会从保存的阶段继续。`cancellation_new_run` 或 `cancellation_run_missing` 需要先人工核对 Multica，不应通过删除 KV 状态强行重建任务。

### Reaction token 配置迁移

旧部署的 `SLACK_REACTION_READ_TOKEN`、`SLACK_REACTION_TOKEN` 不再被新版本读取。上线前配置 `SLACK_BOT_TOKEN`；若使用 user 身份，将原 owner token 配置为 `SLACK_USER_TOKEN` 并留空 bot token。新版本部署成功后再删除旧变量，以便旧部署在切换期间仍能运行。
