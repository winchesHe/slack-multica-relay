<!-- GRM 每日 Bug 值守扫描入口：Closed Loop B / v3 -->

mode=scan
protocolVersion=3
policy=lightweight_agent_driven_v3
schedulerRevision=<调度器提交 SHA>

# 每日三源采集与 v3 Agent 驱动闭环

每天北京时间 02:00 触发「GRM 每日 Bug 值守」新扫描。只使用现有值守 Agent 和看板 Agent，不派生子 Agent。流程固定为：Intercom/Canny/Facebook 三源采集 → 冻结本轮 manifest → 两张调查 lane 并行 → 全部调查结论回读 → 全局修复槽串行 → 逐项交付。PR merge、生产发布、生产业务数据写入和其它不可逆动作仍需明确授权。全程中文；不得修改本仓库源文件、本机 Skill、其它 Prompt、Multica 配置或环境变量。

## 入口和历史隔离

先回读 scan Issue、真实 project/workspace、Agent、Runtime、metadata、评论和运行历史。新扫描初始化的 `dailyExecution` 必须精确为：

```json
{"schemaVersion":1,"protocolVersion":3,"policy":"lightweight_agent_driven_v3","laneCount":2,"repairConcurrency":1,"investigationFailurePolicy":"retry_2_then_skip_v1","schedulerRevision":"<40位调度器提交 SHA>","runtimeId":"<真实 Runtime UUID>"}
```

v3 scan metadata 不得包含 `dailyRound`、`dailyBatch` 或 `dailyDispatch`。已有 v1/v2 扫描、调查和修复继续使用各自固定 schedulerRevision 的 `duty-queue.py`/`duty-dispatch.py`，不可迁移或接管。`recover`/`inspect-round` 发现旧协议时只返回 `legacy_round_delegate`，不写状态、不补评论、不重采来源。

扫描、租约、fencing token、运行 intent、附件/评论/Slack 回读和恢复只通过 `scripts/duty-ops.py`；只读阶段可使用 `duty-progress.py`、`duty-board.py`。所有 Multica 写入必须 `--output json`，写后按同一 ID 回读；错误或超时按前后 run ID 差集核对，不能盲目重试。

## 三源采集

目标日为 Asia/Shanghai 前一个完整自然日，第一次写入后冻结。逐项检查 `DAILY_BUG_COLLECTOR_SCRIPT`、`DAILY_BUG_OUTPUT_ROOT`、`DAILY_BUG_CANNY_ACCOUNT_REF` 是否存在，只输出变量名和布尔值。使用稳定 `collect-daily` CLI：Canny 传 `--account-ref`，按 period-created-posts/page-visible 读取目标日帖子及可见评论，不使用 vote/Quick Win 过滤；Facebook 读取既有 Slack 汇总，不直接抓 Facebook；Intercom 一会话一 evidence，`inWindow=true` 的客户消息作为目标反馈，其余仅作上下文。保留 `csTickets`；只有用 jira skill 回读确认存在的研发工单 key 才计 `jiraKeys`/`cs_followed`。

覆盖所有 domain 和业务，按稳定来源身份归并重复反馈，保存 sourceUrl、现象、正确行为依据、影响、平台、版本、查重和验收目标。附件先检查再查看：Intercom 用 `list-attachments`，按用户/lead 下载截图和录屏，录屏用 ffmpeg 抽帧；失败按错误码重试并记录，不把未查看写成无附件。生产数据只能用 redshift 只读副本，Datadog/Sentry 作为补充；不能 impersonate 客户。每来源成功或失败立即保存来源状态、窗口、runArtifact、evidenceArtifacts、collected 数和覆盖缺口；失败不填 0，恢复只补缺失窗口，不读周报、不扫描历史。

初筛保存 `nonBug`、`productDecision`、`csFollowed`、`suspected`；最终去向保存 confirmed、duplicate、verificationBlocked、excludedAfterInvestigation。完整证据索引固定为 `scan-evidence-index.json`；零候选、缺源和字段台账失败都必须有真实记录。

## 固定 manifest 与两张 lane

采集和初筛完成后，只冻结当前反馈日候选，不能导入历史 pending、旧 needs_evidence 或停止任务。调用：

```bash
python3 scripts/duty-ops.py init-scan-manifest \
  --scan-id <scan UUID> --manifest-file <manifest.json> \
  --target-date <YYYY-MM-DD> --scheduler-revision <40位 SHA> \
  --runtime-id <Runtime UUID> --workspace-id <workspace UUID>
```

评论首行必须是：

```text
<!-- daily-round-manifest:v3 round=<scan_id> -->
```

JSON 必须有 `protocolVersion=3`、`roundId`、`feedbackDate`、`candidateCount`、候选数组、`candidateSetSha256`（等于 `duty_queue.candidate_set_hash(ids)`）、真实附件 UUID、附件 SHA-256、`readbackHashVerified=true`。每个 candidate 的 `feedbackDate` 等于目标日、candidateId 唯一、sourceUrl 非空。manifest 冲突时停止，不覆盖；重跑复用已验证回执。

原 scan 下必须正好两张 lane，metadata 精确为 `dailyLane={schemaVersion:1,protocolVersion:3,mode:"investigate_lane",roundId,lane:1|2,laneCount:2}`，不得写 `state`、`currentCandidateId`、`failureHandoffCandidateId`。lane 只由 Agent 自身运行，不能手工提前指派全部候选。候选由 claim 租约动态分配，不写 lane 游标。

## 调查结论与受限重试

每次 `claim` 在 Runtime `fcntl.flock` 下读取 manifest、已验证 records 和 lease，发放单调递增 fencingToken 与 TTL（默认 3600 秒）。租约过期或运行结束无结论时记录 failedRuns；同候选首次加两次重试，第三次仍无有效结论则自动追加 `skipped_failure`，保存三次 runId、具体 reason、resumeCondition、skippedAt，不计确认/排除，不阻塞其它候选。

每项调查使用自己的 worktree、报告目录、浏览器 tab 和测试数据，不覆盖另一 lane。必须补读原讨论、历史、附件、实现、数据和日志，并在授权测试环境动态验证。结论追加到 lane：

```text
<!-- daily-investigation:v3 round=<scan_id> candidate=<candidateId> -->
```

JSON 必须包含 schemaVersion、protocolVersion、mode、roundId、sourceScanId、candidateId、feedbackDate、匹配 sourceUrl、laneIssueId、fencingToken、conclusion。除 skipped_failure 外必须上传唯一报告，保存 `report.attachmentId`、sha256、readbackVerified 和 evidenceRevision；confirmed 的 intercom token 不能跨候选复用。

- confirmed：`verification.fixable=true`、`productDecisionRequired=false`、expectedBehaviorRef、observedDeviationRef、dedupeRef，且能解释客户原现象。
- duplicate/excluded/cs_followed：非空 conclusionRef。
- suspended_external：必须真实尝试 source、implementation、data、verification 四条路径，使用 external_evidence blocker、缺失材料、恢复条件、evidenceRevision、checkedAt、至少 24 小时后的 nextCheckAt。

提交前和重新加锁后都检查 fencingToken；TTL 过期或被更高 token 取代时拒绝旧 Worker。调查未全部结论、coverageComplete 未完成或 evidence index 未回读，不能创建 Bug、不能修复、不能发“扫描完成”。

## 修复、交付和唤醒

全轮调查完成后才为 confirmed 候选创建 Bug 子卡。每个 `dailyBug` 精确保存 schemaVersion、protocolVersion=3、mode=bug、batchId/sourceScanId、candidateId、investigationRef、feedbackDate、source、sourceUrl；禁止 state/index/total/completeDelivery/deliveryVersion/reviewState。全局修复槽一次只准一个 scan，FIFO 等待；holder 只有全部 Bug 有 terminal receipt 或 scan 为 in_review/done/cancelled/canceled 才能回收。修复按一项一项执行，`repairConcurrency=1`。

有 Go/TS/应用 CI 的修复必须等待目标提交构建/部署并 ready，再按 `moe-acceptance/references/repair-and-retest.md` 做动态 Case；`--maxWorkers=2`，不能因一次 `gh pr checks --watch` 超时就结束。纯 SQL/数仓仓库必须在授权只读副本用原问题样本、核心筛选和相邻回归场景完成 Before/After 查询验证。PR 描述使用 review-brief，最终有完整 HTML 报告。未获授权不得 merge、生产发布或写入线上。

交付频道为 `C0C14HSSP2T`，Winches 为 `U06GM8PAFEX`。使用 Slack Bot `U0C1NTX8P8R`/`B0C1K25KSMC` 的 `--as bot`，负责人按 Feishu Bitable + Sheet、Slack email lookup 核对；Block Kit 用 rich_text user mention。完整交付可 @已核实模块 Owner；受阻只 @ Winches 一次；不发私信、不 @here/@channel/@everyone。受阻恢复必须 `slack.py edit --as bot` 原 blocked 顶层消息。

`deliver-bug` 和 `block-bug` 都必须保存 intent。Slack 发送成功但 GRM-664 ledger 失败时重跑从 intent 继续，不重复 Slack；`slackSendAttempted=true` 且 permalink 缺失时先 history/replies 按 Bug 标识、来源 URL、版本找回，不能直接重发。所有附件、Slack、ledger 都完成回读后才写 receipt、改 Bug 状态、唤醒父任务。

完整交付评论：

```text
<!-- daily-delivery:v3 round=<scan_id> issue=<bug_id> version=<v> -->
```

包含 protocolVersion、issueId、roundId、candidateId、deliveryVersion、合法 PR URL、7–40 位 prHeads、报告 attachment/SHA/readback、Slack permalink/readback、ledger issue/commentId/readback；随后 Bug `in_review --no-start`。

受阻评论：

```text
<!-- daily-blocked:v3 round=<scan_id> issue=<bug_id> -->
```

包含 blocker.kind/reason/resumeCondition、Slack permalink/readback、ledger issue/commentId/readback；随后 Bug `blocked --no-start`，不计已交付。两个 lane 近同时结束或父 finalizer 提前退出时，必须用 `rerun-parent-if-needed`，按 beforeRunIds/afterRuns 差集至多排一个后继，避免漏唤醒和重复 run。

## CLI、检查与硬中断恢复

核心命令：

```text
claim
submit-investigation
finish-scan
dispatch-if-issue-idle
rerun-parent-if-needed
acquire-global-repair-slot
release-global-repair-slot
deliver-bug
block-bug
inspect-round
recover [--apply]
```

所有输出为 `protocolVersion=3` JSON。运行启动前保存 intent 的 beforeRunIds；`issue assign <id> --to-id <agent> --no-start`、`issue rerun <id>`、`issue status <id> <status> --no-start` 后必须按 ID 回读。意外 timeout/error 只读回现场，不能根据退出码重复 rerun。

退出前核对：三源覆盖、manifest 哈希和附件回读；两 lane 恰为 2 且无废弃字段；每项有结论和唯一证据；租约释放；没有调查未完就创建 Bug；修复槽 holder/waitQueue 正确；Slack/ledger/receipt 全部 readbackVerified；没有客户原文和凭据进入输出；父任务有可回读 wakeup intent。

CI 等待、部署等待、上下文耗尽或进程硬中断时，先执行只读 `inspect-round` 和 `recover --round-id <id>`。`in_progress` 无 active run 且无 terminal receipt 是 `needsRecovery`，不能当 blocked；in_review/done/cancelled 缺 receipt 只报警、不能计 delivered。只有已授权恢复才加 `--apply`；按 intent、运行差集、Slack history/replies 和 ledger 回读继续，成功步骤不重做。`mode=gate_test` 始终隔离测试，使用 `GRM-664` `<!-- duty-board:v1 -->` fixture 验证 v3 scan/manifest/investigation/delivery/blocked，而不触发真实业务操作。
