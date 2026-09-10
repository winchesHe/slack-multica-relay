# Slack → Multica Relay

在获准频道中 @真人或 User Group，把请求交给 Multica 的专用 Agent，执行环境可以是本地 Codex。适配 Vercel Functions、EdgeOne Cloud Functions 与 Cloudflare Workers，共用同一套处理逻辑。

## 链路

`Slack → 签名与准入校验 → QStash 持久化 → HTTP 200 → 消费函数 → Multica Issue → Agent/Runtime → Slack 回复`

- 只处理当前消息明确 mention 的事件。Team 必填；频道和发送者支持白名单（可设为 `all`）及黑名单，黑名单优先。Bot、编辑/删除、普通讨论不触发。
- 入站只等待 QStash 接收；队列负责后台投递及3次重试，耗尽后在其失败队列查看/重放。
- 每个 Slack thread 通过普通 Issue API 创建独立任务卡，不经过 Autopilot 同标题60秒去重。
- thread scope 包含 Workspace、Project、Agent；不同 Agent 配置不采用彼此的映射。
- 同 thread 后续消息追加评论。QStash 按 thread 限并发，Redis 锁与消息状态处理重投。
- 标题使用消息摘要与稳定的线程短标识；描述和后续评论分为原文引用、来源及 JSON 上下文，附件仅保留定位字段。
- 描述首行的 `relay-thread` 标记和 `relay-payload:v1` 数据区块用于 KV 映射过期后的恢复，请勿删除或修改。读取兼容历史裸 JSON；损坏的数据会停止恢复，不自动重新建卡。
- 写请求结果不明时先查回读；查不到则保留 ambiguous 错误，不盲目再次 POST。需要人工核对/重放，不承诺 exactly-once。
- `comment_persisted` 只表示评论保存，实际执行和原 thread 回复要分别验收。
- Prompt 真源为 [AGENT-PROMPT.md](AGENT-PROMPT.md)，需要明确同步到 Multica Agent instructions。Relay 不调用 Codex 或修改 Multica 源码。

## 本地验证

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm lint
```

配置与部署见 [搭建手册](SETUP-GUIDE.zh-CN.md)，契约边界见 [审查记录](REVIEW.md)。

## 状态与日志

日志记录关联标识、耗时和有限错误码。正文保存在队列和 Multica；Redis 保存线程/消息状态90天，并保存单条事件的冻结正文24小时。内容级调试日志尚未启用，凭据不进入日志。

| 接口结果                | 含义                                                                   |
| ----------------------- | ---------------------------------------------------------------------- |
| 入站 accepted / HTTP200 | QStash 已接收，不代表 Agent 完成                                       |
| 入站 ignored / HTTP200  | 不满足触发范围                                                         |
| 入站503                 | 收件未确认，交给 Slack 重试                                            |
| 消费 created            | Issue 已创建或从回读恢复                                               |
| 消费 comment_persisted  | 后续评论已保存                                                         |
| 消费 duplicate          | 已处理的消息                                                           |
| 消费 rejected / HTTP200 | 无效输入或确定性失败，retryable=false，不继续队列重试                      |
| 消费503                 | 保留队列重试/DLQ责任，原因包括 timeout、thread*lock_busy、ambiguous*\* |

`GET /api/health` 仅证明函数可响应。消费有45秒整体预算，部署函数上限60秒；入站发布请求超时2秒。平台冷启动、网络延迟与配额仍须实测。

## 在 thread 内取消任务

在原任务 thread 回复 `@目标 cancel` 或 `@目标 取消`。只有发送者在 `SLACK_TARGET_USER_IDS` 中才生效，频道和发送者黑白名单继续生效。`SLACK_CANCEL_KEYWORDS` 可配置逗号分隔的关键词，非空配置替换默认值；去掉目标 mention 和首尾空白后完整匹配，英文忽略大小写。

取消指令复用现有消息订阅，经 QStash 持久化处理，不会建卡、追加任务评论或添加启动 reaction。消费者保存目标运行 ID，取消排队或执行中的运行并回读状态，然后清理已记录触发消息上当前 reaction token 身份的全部 reaction（包括启动标记）；其他人的 reaction 保留。已完成、已失败且没有活动运行时保留原标记。

取消期间的任务请求会被忽略。取消完成后重新发送 mention 可继续原任务卡；已处理的旧事件不会重新启动任务。API 或清理失败保留进度，由 QStash 重试；重试耗尽需检查 DLQ 并重放原消息。所有 reaction 操作优先使用 `SLACK_BOT_TOKEN`，未配置时使用 `SLACK_USER_TOKEN`；选中的 token 需要 `reactions:read`、`reactions:write` 且能访问目标频道，调用失败不会切换身份。切换身份后，旧身份添加的表情会保留。无需订阅 `reaction_added`。

## 可靠交付与可选发送适配器

Relay 将触发消息转为任务；上下文读取由 Agent 的 Prompt 和 Skills 决定。每个事件的正文、附件安全元数据与配置快照冻结 24 小时供重试复用，按稳定 marker 搜索恢复任务；附件内容及 private URL 不进入队列。已有取消权限、固定 run 集合与 reaction 清理保持原有语义。

`scripts/slack-reply.py` 是可选的确定性发送入口，使用私有 JSON、来源 Issue/comment 校验和 at-most-once delivery ledger。现有最终回复 Skill 默认仍用原 Slack 发送流程；采用 adapter 时只保留一个发送入口，避免两种方式同时发。配置及升级步骤见 [搭建手册](SETUP-GUIDE.zh-CN.md)。

Cloudflare 使用 `pnpm dev:cf`、`pnpm build:cf` 和 `pnpm deploy:cf`，共享同一 consumer 与 QStash/Redis；构建推荐 Node.js 24。
