# Footer 运维与上线验收

## 当前状态

生产已启用 Slack 运行统计插件 v0.2.0，安装 ID 为 `44353b2a-7501-41d8-a980-154754aa708d`；Vercel 验签、登记和消费函数、Runtime 发送脚本及 Agent Prompt 已接通。CLI 暂无插件管理能力，本次按用户明确授权通过页面安装、官方 API 获取签名凭据。凭据不写入仓库。

2026-09-09 验收：150 个 TypeScript 测试、14 个 Python 测试与类型检查通过。受控 [Slack 测试线程](https://moegoworkspace.slack.com/archives/C0B0DSCRBKM/p1788882331909789) 中两次运行分别更新各自原回复，均只有一个 footer，原正文块、作者及 thread 保持一致，恢复状态均为 done。

- 首次运行验证发送成功、登记失败后的补登记：只恢复原回执，没有重发正文。
- 第二次运行 `01a081bd-a0af-7d38-91c9-cc7a0eafe1d9` 正常自动完成；Multica 于 15:58:54 UTC 结束，Slack 编辑时间为 15:58:56 UTC，观察延迟约 2 秒。统计为 50s、gpt-6-astra、197.9k tokens、96% cached、7 tools、1 skills。
- Slack 会把追加 footer 的 fallback 双换行规范化为两个空格。代码兼容这两种精确后缀，仍校验完整正文摘要；已用集成测试验证更新响应丢失后回读去重，以及人工改动正文时停止覆盖。
- 失败/取消、Hook 丢失、统计迟到、Cron、重试耗尽等故障组合目前有本地集成测试覆盖，未在生产逐一注入故障。上述约 2 秒是一次正常运行观测，未区分冷/热启动；平台 CPU、流量和存储/队列用量增量尚未测量，不作为上线完成的性能承诺。

## 自动恢复

| 情况 | 处理 |
| --- | --- |
| 运行中登记最终回复 | 先原子保存恢复索引，再保存回复映射，安排 15 分钟后的检查 |
| 完成/失败 Hook 丢失 | 延迟任务重新读取已登记的精确 run；取消也通过此路径发现 |
| usage 或日志未就绪 | 最多查询三次，后两次延迟 30 秒和 120 秒；完整日志统计只读取一次 |
| 暂时故障、队列重试耗尽 | 保留索引，每日 Cron 重新发布；一条消息仍使用 QStash 的三次重试 |
| 十二次错误或运行七天仍未结束 | 停止自动恢复，保存停止原因 90 天 |
| 正文、作者、范围或容量不满足条件 | 停止更新，保留原消息 |
| 更新已成功但回执丢失 | 根据稳定 footer 标识和正文摘要确认，保留冻结后的同一份统计 |

索引按 workspace/project/agent 隔离，只记录已登记运行，不扫描整个工作区或全部 Redis key。Cron 每次最多领取 20 条、并发发布 5 条，领取租约一小时；发布失败不丢弃索引。运行恢复和 Hook 并发时使用相同 task 锁。

Vercel Cron 配置为每日 03:00 UTC（北京时间 11:00），需要至少 32 字符的 `CRON_SECRET`。Hobby Cron 允许每日运行，但存在小时级时间误差，不能视为准点 SLA。[Vercel Cron 限制](https://vercel.com/docs/cron-jobs/usage-and-pricing)

`CRON_SECRET` 仅在 Vercel 和运维机器配置，不放入 Agent Runtime。关闭 `RELAY_FOOTER_ENABLED` 时，经过认证的 Cron 返回 disabled，新事件和消费者入口停用，Agent 继续发送正文。

## 查询与单运行重放

在运维环境设置准确的 `RELAY_RECOVERY_URL=https://实际域名/api/footer/recovery` 和 `CRON_SECRET`，然后使用完整 issue/run UUID：

```bash
rtk proxy python3 scripts/footer-recovery.py inspect --issue '<issue-uuid>' --task '<run-uuid>'
rtk proxy python3 scripts/footer-recovery.py retry --issue '<issue-uuid>' --task '<run-uuid>'
```

`inspect` 返回恢复预算、停止原因、是否完成及已登记的 message ts，不返回正文、token 或运行日志。`retry` 会先查询现状，服务器再核对 issue 范围、Slack 原作者、thread 和正文摘要，最后按同一 ID 回读。重放重置恢复预算，保留回复映射和已冻结的 footer；没有登记、已完成、正文已变更或范围不匹配时不会重发正文。

网络中断或结果不明时先重新 inspect，不直接重复 retry。若正文已经人工修改，需要人工判断如何处置，不能清空摘要来绕过保护。发送脚本处于 sending 状态时，先对照 Slack 核对是否发送成功；恢复接口不能修复缺失的发送回执。

## QStash DLQ

QStash 在单条消息重试耗尽后保留 DLQ 记录，保留时长由套餐决定；平台允许按消息重放或删除。[QStash DLQ](https://upstash.com/docs/qstash/features/dlq)

1. 在 QStash 中筛选准确的 `/api/queue/footer` 目标地址，核对消息体的 issueId/taskId。不要批量操作其他 Relay 入站队列。
2. 用上述 inspect 查询同一运行。done=true 表示更新已确认；stopped 表示需要先处理记录中的原因。
3. 修复临时故障后，可用单运行 retry 或 QStash 对该消息执行 Retry。后者仍受消费者的停止状态约束；已经 stopped 的运行须先用单运行 retry 重置预算。已完成的运行会直接去重。
4. 先回读 inspect 和实际 Slack 消息，再决定是否处理残留 DLQ 记录；不自动批量删除。登记索引承担兜底恢复，不能因为 DLQ 有记录就认定任务仍失败。

`relay_footer_stopped` 日志包含精确 issueId/taskId 与经过过滤的原因；`relay_footer_recovery` 提供领取、发布、清理和失败计数；`relay_footer_http` 提供处理状态和耗时。排障不需要输出完整运行日志或上游响应正文。

## 上线步骤与真实验收

1. 补齐可用的 Multica 插件管理入口。项目规定使用 CLI；若需要浏览器安装，须由用户明确指定这一操作方式。
2. 将 manifest 的实际域名和回调地址填齐，订阅 task.completed/task.failed，创建安装后回读 ID 与配置。取消没有对应插件事件，使用恢复检查。
3. 在 Vercel 配置安装 ID、签名密钥、回复作者 token、登记凭据和 CRON_SECRET，保持 footer 开关关闭；Runtime 使用同一发送作者，部署发送脚本。
4. 对照最新线上 instructions 更新 Prompt，保留未涉及规则；协调两端开关，只在明确允许的测试 thread 执行验收。
5. 核对正常完成、失败/取消、Hook 丢失、登记失败、统计迟到、重复事件和响应丢失。每条最终回复保持正文/附件不变，统计只出现在原消息的一个 context block，无新统计消息。
6. 检查 Cron 到期领取、队列延迟投递、停止记录和单运行重放。回退时关闭两端 footer 开关，保留状态用于恢复，不删除映射或重发正文。

正常链路已观测一次约 2 秒的更新延迟，冷/热启动分组与用量增量尚未测量。验收时记录从 Multica 终态到 Slack 更新的时间，区分冷启动、连续运行和补偿路径；同时查看 Vercel Function Invocations、Active CPU、流量及 Redis/QStash 请求增量。正常链路除登记、Hook 和消费外，登记时的兜底延迟任务可能再产生一次去重消费；异常路径另有有限补查和恢复调用。不要用本地测试耗时或之前的流量估算代替这些结果。
