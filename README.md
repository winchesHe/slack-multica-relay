# Slack → Multica Relay

这个项目是一个部署到 Vercel 的 Slack HTTP Events API 中转层。它负责接收 Slack 事件、识别 @真人 / @User Group、给命中的消息添加 reaction，并触发 Multica Autopilot；最终 thread 回复由 Multica Agent 的 Slack skill 完成。

启用 `SLACK_THREAD_ROUTING_ENABLED=true` 后，Vercel 会以 Slack 根 `thread_ts` 作为会话键：第一次 mention 创建 Multica issue，后续同一 thread 的消息写入该 issue 的评论，因此任务看板只保留一张卡片。线程映射、并发锁和消息幂等状态保存在 Upstash/Vercel KV 中。

## 本地检查

在本目录安装依赖后运行：

```bash
pnpm test
pnpm lint
```

## Vercel 部署

1. 在 Vercel 创建一个新项目，Root Directory 选择本目录 `slack-multica-relay`。
2. 按 `.env.example` 配置环境变量。
3. 部署后，把以下地址填入 Slack App → Event Subscriptions → Request URL：

   ```text
   https://<你的域名>/api/slack/events
   ```

4. Slack 完成 URL verification 后，在 **Subscribe to events on behalf of users** 下订阅 `message.channels`、`message.groups`、`message.im`、`message.mpim`，并确保授权用户拥有对应的消息读取权限。
5. 在 Multica 创建 Autopilot Webhook，将完整 URL 配置到 `MULTICA_WEBHOOK_URL`。

线程归并还需要配置 `MULTICA_API_TOKEN`、`MULTICA_WORKSPACE_ID`、`MULTICA_API_BASE_URL` 和 KV REST 凭证，并将 `SLACK_THREAD_ROUTING_ENABLED` 设为 `true`。`MULTICA_API_TOKEN` 是 Multica Personal Access Token，不是 Slack Token。

## 事件处理约定

- 真人 mention 使用 `<@U...>` 匹配。
- User Group mention 使用 `<!subteam^S...>` 匹配。
- 用户级事件只会覆盖授权用户本身有权限看到的会话；Bot 不需要加入这些频道。
- 首条消息仍使用 `teamId:channelId:messageTs` 作为 Multica `Idempotency-Key`，保证 Slack 重试不会重复建卡；线程映射键使用 `teamId:channelId:threadTs`。
- 同一根 thread 的后续消息追加到同一 Multica issue；Multica 会按 issue 的原生评论规则合并或排队 follow-up run。
- Bot 消息、编辑/删除事件，以及不在已跟踪 thread 中且不命中目标 mention 的消息会被忽略。
- 不要用 Agent 的 User ID 做全局忽略条件：当 Agent 和人工操作者使用同一个 User Token 时，会把人工 mention 一并误过滤。Relay 继续依靠 Slack 的 `bot_id` / 编辑删除 subtype 过滤；若将来 Agent 必须以 User 身份发消息，应通过 Slack message metadata 增加可识别标记。
- payload 会把原始 `channelId`、`threadTs`、`messageTs`、发送者和文本交给 Multica Agent。
- 命中消息的 reaction 由 Vercel 使用 `SLACK_REACTION_TOKEN` 调用 `reactions.add` 添加；`already_reacted` 会按成功处理。

## 健康检查

```text
GET /api/health
```

预期返回：

```json
{"ok":true,"service":"slack-multica-relay"}
```
