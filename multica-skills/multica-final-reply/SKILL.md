---
name: multica-final-reply
description: 在 Multica 的 Slack Task Router 完成业务任务后，获取当前运行的发送前快照，核对相关 PR 与分支，将结论和 Footer 一次回复原 Slack 线程。
---

# Multica 最终回复

仅供 Multica 的 Slack Task Router 使用。当前业务工作完成、已有最终结论且原线程回复已获授权时调用；此 Skill 不扩大业务写入权限。进度消息仍由 slack Skill 处理。

## 回复流程

1. 读取目标环境的 slack Skill。根据任务结果写清结论、实际完成的动作、必要证据和未完成事项。不要把过程流水账、工具参数或原始运行日志放进正文。
2. 使用本 Skill 的 `scripts/final_reply.py snapshot --issue <当前 Issue UUID> --output <快照文件>`。脚本从 `MULTICA_TASK_ID` 取得当前 run，只查询该 run；不要自行选择最近一次运行。快照只保存在当前任务的私有目录。
3. 读取快照中的 `messages`，选出本次实际处理的 PR。相关性由你结合业务任务判断，不能把示例、Skill 文档或讨论中顺带出现的链接当成果。输出 JSON 数组，每项为 `{"url":"https://github.com/owner/repo/pull/123","evidence_seq":12}`，其中序号必须指向本次真实 `gh pr` 调用或返回。没有相关 PR 时用 `[]`。脚本会核对日志证据，再通过 `gh pr view` 取得真实仓库、分支和 URL；无法证实时不猜。
4. 正文写入 UTF-8 fallback 文件，同时准备正文 Block Kit 数组；正文 blocks 不手写 Footer。执行 `scripts/final_reply.py prepare --snapshot <快照> --artifacts <PR 数组文件> --text-file <fallback> --blocks-file <正文 blocks> --output-dir <私有输出目录>`，检查返回的统计和最终文件。准备不会发送。
5. 使用 `scripts/final_reply.py send --bundle <输出目录/bundle.json> --channel <原 channelId> --thread-ts <根 threadTs>` 一次发送。`--dry-run` 只预检。脚本复用 Runtime 的 `RELAY_REPLY_SCRIPT` 和 `RELAY_SLACK_CLI`，固定 `SLACK_REPLY_ACTOR`，持久化发送回执；不登记旧 Footer 回调。

所有命令通过 `rtk proxy python3` 执行。发送后结束回复流程，不再追加或更新 Footer。重试必须使用同一 bundle；结果不明时核对回执与原线程，不换入口、删回执或重新准备后重发。

## 展示与数据口径

- 正文后使用 context 展示耗时、可确认的模型、非零 tools 和 skills；再按 PR 展示仓库、分支和 PR 链接。多 PR 去重，保留多仓库；没有 PR 时不显示空行。
- 耗时截至快照采集时刻；tools 是已返回日志中的 `tool_use` 数量；skills 是已成功读取、能与调用唯一对应的 Skill 名称去重数。Multica 日志可能尚在上传，快照不是任务结束后的总量，不轮询等待补齐。
- 模型读取当前 run 所属 Agent 的 `model` 配置，核对 Agent ID 与工作区归属。这是发送前读取的配置模型，不是运行结束后的实际 usage 模型；按用户选择展示配置值。读取失败或字段缺失时隐藏，不显示占位符或 0。token 与缓存率不展示。
- PR 分支从 GitHub 读取，不从标题猜测。暂未建立 PR 的分支放在正文说明并提供业务证据；第一阶段 Footer 只支持经 PR 核验的分支。

Runtime 依赖：已认证的 `multica`、`gh` CLI，显式 `MULTICA_SERVER_URL`、`MULTICA_WORKSPACE_ID`、`MULTICA_TASK_ID`，以及支持 `register_footer=False` 的 `RELAY_REPLY_SCRIPT`。缺失配置应报告，不能绕过脚本直接发送。
