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

## 取消与清理

本次按本地 Multica 服务端源码核对以下契约：`GET /api/issues/{id}/tasks` 返回运行数组；`POST /api/issues/{id}/tasks/{taskId}/cancel` 在服务端校验任务与 issue 归属，再执行用户取消。Relay 额外核对 issue 的 Project、Agent、来源 marker，以及运行的 workspace/issue。目标集合在首次写入前保存；重试先查询原集合，不重新选中新一轮运行。

取消结果以运行列表回读的终态为准，HTTP 成功本身不触发清理。服务端将状态改为 cancelled 并广播中断，但该接口未提供独立的 daemon 已停止确认。因此不承诺机器侧立即停止或撤回已经发生的外部操作；Agent 在收到中断前的在途行为仍由运行时控制。

清理使用 [reactions.get](https://docs.slack.dev/reference/methods/reactions.get/) 与 [reactions.remove](https://docs.slack.dev/reference/methods/reactions.remove/)，仅删除 token 对应身份在已记录触发消息上的 reaction，逐消息保存进度。需要 reactions:read 与 reactions:write。升级前没有 reaction 消息清单的 v2 状态会从卡片描述和带 relay marker 的分页评论恢复触发消息，逐条核对 Slack thread 来源；不会遍历整个 Slack thread 擦除无关消息的表情。新版本持续记录后续触发消息。

取消、普通派发和启动 reaction 添加复用 120 秒线程锁，消费者外部请求受 45 秒总 deadline 限制；继续依赖现有 QStash 持久化投递、三次重试和耗尽消息保留。失败返回 503，重试不依赖下一条 Slack 消息；重试耗尽后需人工检查并重放 DLQ。平台配置变更、租约过期后的在途外部请求、直接从 Multica 触发新运行均不构成跨系统原子事务。

取消期间到达或已发出的普通消息不会在收尾后启动任务；时间边界同时使用 Slack 消息时间和消费端结束时间，需要托管环境时钟准确。旧取消早于最新处理的任务消息时直接忽略。状态沿用 90 天 TTL；超过保留期限后来源 marker 能恢复卡片和触发消息，无法恢复已经过期的取消历史，不能保证任意久以前的手工重放仍具有相同语义。

回滚到不支持取消的版本前，应暂停消费并隔离队列中的 operation=cancel 消息；旧版消费者会把这些正文当作普通 mention。恢复投递时使用支持取消状态的版本。
