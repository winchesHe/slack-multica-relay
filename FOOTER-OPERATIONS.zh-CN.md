# Footer 运维

## 运行方式

Footer 使用 Vercel `waitUntil` 直接异步执行。合法 Multica Hook 立即返回 200，后台尝试一次更新原 Slack 消息。失败只记录结果，不重试、不延迟补查、不定时恢复。事件漏掉、运行数据未就绪或函数被终止时允许缺少统计，正文已经独立发送。

插件为 Slack 运行统计 v0.2.0，安装 ID 为 `44353b2a-7501-41d8-a980-154754aa708d`。安装使用既有回调 `/api/multica/events`，本次执行方式调整不需要重新安装或重新生成凭据。

## 发布

1. 在任务 worktree 执行 `rtk proxy pnpm install --frozen-lockfile`、`rtk proxy pnpm test`、`rtk proxy pnpm lint`，提交并推送。
2. 部署 Vercel，核对精确提交与生产别名。Hook 函数时限为 60 秒，后台总请求预算 45 秒；200 响应不等待这个预算结束。
3. 保持 Vercel 与 Runtime 的 `RELAY_FOOTER_ENABLED` 一致，核对登记凭据及 Slack 同一作者。发送脚本和 Prompt 不因本次 worker 调整而需要重新发布。
4. 在已授权测试 thread 观察回调响应及原消息统计，确认后台执行没有 QStash 投递。

旧 Footer 的 RELAY_FOOTER_CONSUMER_URL、CRON_SECRET、RELAY_RECOVERY_URL 不再使用，可在确认无其他用途后清理；不要删除 Slack 入站任务仍使用的 QStash 配置。Cron 已从 vercel.json 移除。

旧 Footer 队列、Cron 和恢复 HTTP 入口只返回 `{"action":"disabled"}`，帮助历史投递结束，不执行 worker。旧 pending/recovery/stats Redis 记录不再消费，保留到自然过期；不要批量清理其他 Relay 状态。

## 排障

先确定精确 issue ID、run ID 与实际 Slack 消息，不采用 issue 的最新 run 猜关联。

- **有正文，无登记**：检查 Runtime `reply.py` 返回值及本地回执。sent 状态可用原参数补登记；sending 表示发送结果不明，先核对 Slack，禁止清空回执后重发。
- **已登记，无 Footer**：检查 Vercel 的 `relay_footer_worker` 日志，按 issueId/taskId 关联。updated 表示完成写入；duplicate 表示已有完成或执行机会被领取；waiting_for_reply 表示尚无绑定；skipped 表示本次不展示；failed 包含有限错误码。
- **只有部分统计**：本次数据或日志不可用的字段直接隐藏，不等待或稍后补齐。
- **回调已返回 200**：只表示通过本地校验并交给后台，不证明执行成功。日志缺失也可能是函数中止，不能据此判定成功。

不再提供自动或手动 worker 重放入口。Redis 的 attempt 是一次执行机会，即使更新失败也保留；不能为了补 Footer 清除 attempt、正文摘要或发送回执。新的用户请求对应新的 run，可正常生成新的最终回复。

## 回退

紧急停用时关闭 Vercel 与 Runtime 两端 `RELAY_FOOTER_ENABLED` 并使新配置生效。Agent 继续直接回复正文，不附加统计。保留原回执和映射；不要为了回退重发 Slack 消息。

## 验收记录

此前队列版在受控线程完成了正常自动更新和登记失败恢复，正常运行观测约 2 秒补齐统计；该数字不能用于证明本次直接异步方案的延迟。

本次本地测试覆盖：回调先于后台网络完成而返回、后台失败仍 200、无重试/队列发布、并发重复回调、事件先于登记、统计缺失立即降级、范围/作者/正文保护和旧入口退役。生产直接异步链路需在部署后单独记录。
