---
name: multica-final-reply
description: 在 Multica 的 Slack Task Router 完成业务任务后，整理当前运行记录中的统计、PR 和分支证据，组织最终结论与 Footer，并通过现有 Slack Skill 回复原线程。
---

# Multica 最终回复

仅供 Multica 的 Slack Task Router 使用。业务工作完成且原线程回复已获授权后调用。此 Skill 指导回复内容，`scripts/run_context.py` 只读整理资料；最终发送统一使用现有 slack Skill。

## 回复流程

1. 读取目标环境的 slack Skill，以及 [个人回复风格](references/reply-style.md) 和 [Emoji 使用指南](references/emoji-guide.md)。按个人风格组织结论、实际完成的动作、必要证据和未完成事项；需要更多自定义表情时才检索本地完整目录，不逐次联网拉取。PR Review 任务另读 [PR Review 回执](references/pr-review-receipt.md)。
2. 执行 `rtk proxy python3 <本 Skill 路径>/scripts/run_context.py --issue <当前 Issue UUID> --output <任务私有目录/context.json>`。脚本使用真实 `MULTICA_TASK_ID`，查询当前 run、所属 Agent 配置和 run-messages，输出 `statistics` 与带日志序号的 `code_evidence`。不选择最近一次 run，不轮询等日志补齐。
3. 读取输出，根据业务任务从 `code_evidence` 的调用与结果中提取实际处理的 PR、仓库和分支。证据是候选，不等于已完成的成果；排除仅讨论、示例及失败操作。缺失或截断的输出不能当完整证据，必要时按 github-workflow 只读补查。没有 PR 的分支也可展示，但必须有明确的仓库归属和实际分支证据。
4. 读取 [Footer 展示](references/footer-display.md)，按其规则组装最终正文、Footer context blocks 和包含同样信息的 fallback。
5. 按 slack Skill 的发送流程，以 User actor 向原 `channelId` 和根 `threadTs` 一次发送完整消息。直接使用它的 `send --as user --channel ... --thread-ts ... --text-file ... --blocks-file ...`，预检与确认参数以该 Skill 当前版本为准。发送成功后结束，不再追加或更新 Footer。结果不明时先回读原线程，遵守 Slack Skill 的失败处理规则，不自动重发。

发送身份固定为 User，不因 Bot 未安装或发送失败改用 Bot。当前任务已获授权的普通最终答复无需再次确认发送；本 Skill 不决定是否介入、不扩大业务操作权限，静默条件及外部写入授权遵循 Agent Prompt。

资料脚本依赖已认证的 Multica CLI，以及 `MULTICA_SERVER_URL`、`MULTICA_WORKSPACE_ID`、`MULTICA_TASK_ID`。发送凭据、身份预检、消息格式和错误处理全部遵循现有 slack Skill；本 Skill 不提供发送脚本、回执存储或消息更新服务。
