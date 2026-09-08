# Multica 接口契约与验收边界

源码核对：Multica Server v0.4.40；本地 daemon v0.4.36。日期2026-09-05。配置会变化，部署前刷新版本和身份。

## 创建

[CreateIssue](https://github.com/multica-ai/multica/blob/v0.4.40/server/internal/handler/issue.go#L2788) 支持 project/agent 指派。Relay 使用包含thread和配置scope的标题、描述中的来源marker，避开 [Autopilot同标题60秒去重](https://github.com/multica-ai/multica/blob/v0.4.40/server/internal/service/autopilot.go#L675)。

普通API返回active_duplicate_issue/409时，仅在Project、Agent及marker全部匹配后采用已有Issue。其他409不能当成功。

## 恢复

Issue查询扫描专用Project，读取description marker；评论查询按服务端的(time,id)游标翻页。写入前记录intent，响应不明时查回读。无法确认时保留错误进入队列重试/失败保留，不盲目再写。

不保证跨任意故障的exactly-once：Multica评论没有服务端幂等键，租约/网络/人工编辑仍有边界。线程并发由QStash flow control与Redis锁共同约束。来源字段被修改、状态过期或检索超出上限时应人工核对。

## 接收与执行

QStash持久化接收后才能ACK Slack；后台消费失败由队列重试，耗尽可在DLQ检查和重放。QStash、Redis和托管函数是需要分别配置与验收的外部组件。

Relay 的频道和发送者准入同时支持白名单与黑名单。白名单可使用 `all`，黑名单优先；入站和队列消费使用同一套策略。目标用户/User Group 配置仍用于判断是否触发任务。

评论HTTP201表示已保存，可能不触发Agent。Relay返回comment_persisted，不能当作任务执行证明。最终结果以Multica任务和Slack原thread双重回读为准。

## 本地Codex

daemon将Agent instructions写入工作目录AGENTS.md。Skills由本地配置及Workspace指派合入任务环境；不是复制桌面聊天。

[Codex审批处理](https://github.com/multica-ai/multica/blob/v0.4.36/server/pkg/agent/codex.go#L2558)会自动接受命令/文件请求。当前Prompt明确只读默认和逐项授权，但不声称具备强制工具审批。Project不要未经核对就绑定整个个人代码目录为local_directory。

## 本地验证与剩余验收

回归覆盖准入、队列接收失败、两个thread、重复消息、响应丢失、429后消息交错、Agent scope隔离、评论游标。

还需要托管平台真实冷/热延迟、QStash签名/重试/DLQ、Redis网络故障、Mac离线恢复和真实Slack事件订阅验收。未完成这些步骤不能宣布公网接管已可用。

官方平台合同：[Vercel Functions](https://vercel.com/docs/functions)、[EdgeOne Node Functions](https://pages.edgeone.ai/document/node-functions)、[QStash](https://upstash.com/docs/qstash/overall/getstarted)。

## Footer 新增边界

完成 Hook 本地校验安装、工作区、Agent、原始 HMAC 和时间窗口后立即返回 200，使用 Vercel waitUntil 在同一次调用中执行后台任务。临时 callback_token 不保存。Redis 事件标记和 worker 网络调用均在后台进行，失败不会改变回调响应。

登记接口使用独立凭据，查询真实 issue/run 并核对来源 marker、目标 thread 和原作者。Redis 保存原文摘要而非正文。一条 run 绑定一条最终回复；无绑定时不领取执行机会，后续登记可补触发。领取 attempt 后不释放或自动重试，包括终态尚未可读、网络失败和响应丢失。

Footer 的队列、Cron、延迟补查与恢复索引已移除。worker 并行读取日志及原消息，立即展示可得字段；缺失项隐藏。保留原正文/附件，只有摘要和作者匹配时更新。旧队列与恢复入口只返回 disabled，确认历史消息但不执行更新。

模型配置快照已删除。运行日志需完整连续 seq，超过 4 MiB/10000 条或协议不符时隐藏日志统计。Skills 只使用可配对的简单 cat 读取证据，不能从绑定列表或自报推断。发送结果不明仍由 Runtime 回执阻止重复发正文。后台函数终止、Hook 丢失或上游未就绪时允许 Footer 缺失，200 不代表统计写入成功。
