<!-- 交付频道与审查人 ID 已替换为占位符，真实值以线上 Autopilot 描述为准；同步到 Multica 前先替换占位符。 -->

mode=scan
protocolVersion=3
policy=lightweight_agent_driven_v3
schedulerRevision=<调度器提交 SHA>

# GRM 每日 Bug 值守｜02:00 每日扫描入口（Closed Loop B / v3）

每天北京时间 02:00 触发本扫描任务。执行本轮全流程均遵循值守 Agent（`387e0620-8a22-44f5-919c-4a3e6296fcfb`）完整规范：Intercom/Canny/Facebook 三源采集 → 冻结本轮 manifest → 两张 `investigate_lane` 并行取证 → 全部候选结论回读 → 全局修复槽串行 → 逐项完整交付。全程中文；不派生其它 Agent；禁止修改本仓库源文件、本机 Skill、其它 Prompt、Multica 配置或环境变量；PR 合并、生产发布与生产数据写入需额外明确授权。

## 1. 入口校验与历史隔离

1. 用 `multica --server-url https://multica.devops.moego.dev --workspace-id 1b6d114a-8407-4ba8-adea-d31209caabe5 issue get <scan_id> --output json` 回读当前任务、项目（`724dce94-a8b9-44e7-8390-9c3cbdac7bc0`）、Runtime（`<真实 Runtime UUID>`）、metadata、评论和运行历史。
2. 新 v3 扫描仅允许写入 `dailyExecution`（与 `dailyScan`）：
   `{"schemaVersion":1,"protocolVersion":3,"policy":"lightweight_agent_driven_v3","laneCount":2,"repairConcurrency":1,"investigationFailurePolicy":"retry_2_then_skip_v1","schedulerRevision":"<40位调度器提交 SHA>","runtimeId":"<真实 Runtime UUID>"}`
   严禁在 v3 扫描或子任务写入 `dailyRound`、`dailyBatch`、`dailyDispatch`、`dailyLane.state/currentCandidateId/failureHandoffCandidateId` 或 `dailyBug.state/index/total/completeDelivery/deliveryVersion/reviewState`。
3. 若发现既有轮次带有旧字段或 `protocolVersion != 3`，保持只读隔离（`inspect-round`/`recover` 返回 `legacy_round_delegate`），绝不迁移或覆写旧轮。
4. 脚本固定引导（严禁搜索或运行 `.t3/worktrees/...`，必须同时导出 `duty-ops.py` 与 `duty-queue.py`）：

```bash
TMP_OPS=$(mktemp -d)
git -C /Users/moego-winches/Desktop/Company/person/slack-multica-relay fetch origin <40位调度器提交 SHA>
git -C /Users/moego-winches/Desktop/Company/person/slack-multica-relay show <40位调度器提交 SHA>:scripts/duty-ops.py > "$TMP_OPS/duty-ops.py"
git -C /Users/moego-winches/Desktop/Company/person/slack-multica-relay show <40位调度器提交 SHA>:scripts/duty-queue.py > "$TMP_OPS/duty-queue.py"
```

   v3 所有写入、锁、附件回读、租约、唤醒和恢复统一调用 `"$TMP_OPS/duty-ops.py"`（JSON 输出含 `protocolVersion=3`）；写后按同 ID 与 run ID 差集核对，超时或错误不盲目重发。

## 2. 三源采集与固定 Manifest

1. 确定 Asia/Shanghai 前一个完整自然日作为 `targetDate`，立即调用 `duty-ops.py update-scan-status --workspace-id 1b6d114a-8407-4ba8-adea-d31209caabe5 --round-id <scan_id> --target-date <YYYY-MM-DD> --coverage-complete false` 冻结目标日；恢复时复用已保存日期。
2. 用 Python `os.environ` 逐项确认 `DAILY_BUG_COLLECTOR_SCRIPT`、`DAILY_BUG_OUTPUT_ROOT`、`DAILY_BUG_CANNY_ACCOUNT_REF` 存在且非空（只输出变量名与布尔值，禁止打印值）。按 `moe-feedback-collector` `collect-daily` 依次采集 `canny`（首调传 `--account-ref`；`period-created-posts / page-visible`，不用 vote 过滤）、`facebook`（既有 Slack 汇总）与 `intercom`（`inWindow=true` 客户消息为目标日反馈；暂存会话文件于清单固定后删除，后续用 `intercom` Skill 重读）。每来源完成或失败立即调用 `update-scan-status --source <src> --source-status success|failed --collected ... --counts ...`（失败不填 0，恢复只补失败源窗口）。
3. 附件按 `list-attachments` → `download-attachments` 到临时目录逐个查看（录屏抽帧），调查结束后删除原件。结合 `jira` 只读查 `csTickets`/`jiraKeys`（命中的标为 `cs_followed`），结合历史任务与 PR 查重。计算 `nonBug`、`productDecision`、`csFollowed`、`suspected`，形成含 `targetDate`、`identifier` 与 `records` 数组的 `scan-evidence-index.json` 初筛基线（零候选亦须有证据，全部调查完成后补齐最终 `records`）。
4. 仅冻结当前反馈日疑似 Bug 为 manifest JSON（`candidateId` 唯一、`feedbackDate == targetDate`、`sourceUrl` 非空、`candidateSetSha256 == duty_queue.candidate_set_hash(ids)`），执行：

```bash
python3 "$TMP_OPS/duty-ops.py" init-scan-manifest \
  --server-url https://multica.devops.moego.dev \
  --workspace-id 1b6d114a-8407-4ba8-adea-d31209caabe5 \
  --project-id 724dce94-a8b9-44e7-8390-9c3cbdac7bc0 \
  --agent-id 387e0620-8a22-44f5-919c-4a3e6296fcfb \
  --runtime-id <真实 Runtime UUID> \
  --scan-id <scan_id> \
  --manifest-file <manifest.json> \
  --target-date <YYYY-MM-DD> \
  --scheduler-revision <40位调度器提交 SHA>
```

   确认 `<!-- daily-round-manifest:v3 round=<scan_id> -->`、附件 SHA-256 及两张 `dailyLane`（工位 1/2、2/2）回读通过后，仅通过 `dispatch-if-issue-idle` 启动两条 lane 并行取证（不逐条建调查任务、不手工指派全部候选），然后**本轮初始扫描必须立即结束运行退出**！等待 lane 完成后通过 `rerun-parent-if-needed` 唤醒再执行第 3 节；**严禁**父 scan 调用 `claim` / `submit-investigation`、轮询等待子任务或编写批处理脚本代调查。

## 3. 并行调查汇总与 `finish-scan`（仅由 lane 收口唤醒或恢复后进入）

1. 两张 `dailyLane` 各自独立运行，通过 `duty-ops.py claim` 逐条领取候选，按真实原会话/附件、`jira` 与 `$REPOS_DIR`（`/Users/moego-winches/Desktop/Company/AI-Agent/agent-workspace/repo`）或 `MoeGolibrary/*` 真实代码完成深查后按 `fencingToken` 提交 `submit-investigation`（`confirmed` 须可解释原现象并记录 Feishu Bitable/Sheet 归属线索；`suspended_external` 须四条取证路径均实试并 `nextCheckAt >= +24h`；首次+2 次重试无结论由 `claim` 自动写 `skipped_failure`）。父 scan 被唤醒后先跑 `inspect-round`，若仍有未结案候选则只重派空闲/退出 lane 后退出，绝不代劳。
2. 当且仅当 manifest 全部候选均有有效 v3 结论或 `skipped_failure`、`coverageComplete=true` 且包含完整 `records` 数组（每条含规范 `state`、`source`、`summary`、`reason`、`sourceUrl` 等，`excluded` 写为 `excluded_after_investigation`、`suspended_external` 写为 `verification_blocked`）的 `scan-evidence-index.json` 就绪时，生成 8 行内汇总正文，调用 `finish-scan --dry-run` 预检通过后执行：

```bash
python3 "$TMP_OPS/duty-ops.py" finish-scan \
  --workspace-id 1b6d114a-8407-4ba8-adea-d31209caabe5 \
  --agent-id 387e0620-8a22-44f5-919c-4a3e6296fcfb \
  --round-id <scan_id> \
  --evidence-file <scan-evidence-index.json> \
  --ledger-issue GRM-664 \
  --channel <交付频道 ID> \
  --text-file <scan-summary.txt>
```

   8 行模板见值守 Agent 正文；有 `skipped_failure` 须明确标明“执行失败跳过 N，未确认”；未完成全轮调查绝不发“扫描完成”。

## 4. 全局修复槽、交付与硬中断恢复

1. 本轮存在 `confirmed` Bug 时，`finish-scan` 自动创建对应 `dailyBug` 子卡；先执行 `acquire-global-repair-slot --round-id <scan_id>`。若返回 `busy`，保存现场结束运行等待前序持有者释放唤醒；获得槽后一次指派一项启动（`repairConcurrency=1`），严格遵循 `GRM-0`、增量测试 `--maxWorkers=2`、PR CI/部署等待或纯 SQL Before/After 实测、`review-brief`、完整 HTML 报告、Feishu Bitable/Sheet + Slack email 核实负责人、`deliver-bug` / `block-bug`（先上传/edit 受阻根消息/Slack 附件与回读/`GRM-664` 台账/v3 终态评论/`in_review` or `blocked --no-start`/`rerun-parent-if-needed`）。
2. 全部 v3 Bug 均获 terminal receipt 后（或无可修 Bug），调用 `release-global-repair-slot` 唤醒队列中下一个非终态轮次。
3. 任何进程中断或状态不明均先运行 `inspect-round` 与不带 `--apply` 的 `recover`；仅在已授权自动恢复时运行 `recover --apply`。`in_progress` 且无活动运行/无回执为 `needsRecovery`（不可判 `blocked`），终态缺 receipt 只报警不伪造交付。`mode=gate_test` 仅用隔离数据与 `GRM-664` fixture 测试，不触碰线上业务。
