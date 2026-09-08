# Slack Footer 快速异步执行方案

## 目标与验收

Agent 先发送最终回复并登记实际消息 ID。Multica 完成/失败 Hook 在本地验签后立即返回 200，Relay 使用 Vercel `waitUntil` 在同一次调用内异步执行 worker。以尽快展示 Footer 为优先，允许后台失败、通知丢失或函数终止导致 Footer 缺失。

验收：回调不等待 Redis、Multica 或 Slack 网络；后台成功时只更新原消息；后台失败不改变已经返回的 200；重复回调最多领取一次更新机会；无队列投递、延迟补查或定时恢复。

## 链路

1. Runtime 的 `scripts/reply.py` 调用既有 Slack Skill，预览后发送完整 blocks 和 fallback，保存本地回执并登记 issue/run/message 关联。
2. `/api/slack/replies` 核对运行范围、原作者、thread 和正文摘要，保存绑定。运行已结束或先收到 Hook 时直接启动后台任务；运行中登记仅等待完成事件。
3. `/api/multica/events` 校验 HMAC、时间窗口、安装、工作区、事件和 Agent 后，交给 `waitUntil` 并返回 200。回调临时 token 不进入后台或持久化。
4. worker 保存最小事件标识，读取绑定；没有回复时保持静默。有绑定后用 Redis SET NX 领取一次执行机会；领取后即使失败也不释放，重复回调不能重试。
5. 查询精确 issue/run，校验来源与终态；并行读取日志和 Slack 原消息。立即用当前可得数据生成 Footer，校验正文后更新同一消息，并保存 done 标记。

## 展示与统计

单个 context block，顺序固定，以下数字仅示例：

```text
:agent_time: 14m 12s · :agent_mdi_robot_outline: gpt-5.6-sol: 5399.8k tokens (97% cached) · :agent_tool: 27 tools · :agent_skill: 2 skills
```

缺失项隐藏，不等统计齐备，不添加状态或未知值占位。模型取当前 run usage；Tokens 合计 input/output/cache_read/cache_write，以 k 保留一位小数。已验证 Codex input 不含缓存，cached 为 cache_read/(input+cache_read)，其他 provider 或出现 cache_write 时隐藏比例。

Tools 计完整日志的 tool_use；Skills 只计可配对的成功读取 SKILL.md 输出中的 frontmatter name，按名称去重。绑定数量、搜索命中、Agent 自报不算使用证据。复杂读取、并发无法配对或读取结果不明时隐藏 Skills。日志须为完整连续 seq 数组，超过 4 MiB/10000 条、空数组、跨运行或协议不符时隐藏日志统计；不做分页或自动补查。

## 状态与消息保护

Redis 使用既有 workspace/project/agent 隔离的 footer key，保留 reply、event、attempt、done 90 天。reply 保存正文摘要而非正文，完整日志只在进程内解析。没有回复不会领取 attempt，后续登记仍可触发一次处理；已领取 attempt 的运行不会因重复登记或 Hook 自动重做。

发送回执仍区分 sending、sent、registered。正文已发送但登记失败可用相同参数补登记；发送结果不明时先人工核对，不另发正文。

更新必须匹配原作者、根 thread 与正文摘要。保留原 blocks、附件及 fallback，只追加一个稳定标识 context；兼容 Slack 将分隔双换行变为两个空格。没有 blocks、已有 50 blocks 或文本超限时省略，不截断正文。每次运行支持一条最终回复；进度消息不登记。

## 部署与迁移

仅 Vercel 提供 Footer 后台执行，使用 `@vercel/functions` 的 `waitUntil`。Hook 函数时限为 60 秒，后台沿用 45 秒总请求预算；返回 200 不表示 Footer 更新成功。

删除 Footer 的 QStash 发布、统计补查、恢复索引、Cron 配置和手动重放脚本。旧 `/api/queue/footer`、`/api/cron/footer`、`/api/footer/recovery` 暂留为无副作用的 200 disabled 入口，确认历史投递；旧 Redis 恢复记录保留到自然过期，不再扫描。

插件 manifest 的回调 URL、事件和权限无需变更，仍使用 v0.2.0。Vercel 不再使用 RELAY_FOOTER_CONSUMER_URL 与 CRON_SECRET；Slack 入站任务使用的原 QStash 配置继续保留。Runtime 脚本与 Prompt 的发送契约沿用现有版本。

部署与排障见 [Footer 运维](FOOTER-OPERATIONS.zh-CN.md)。
