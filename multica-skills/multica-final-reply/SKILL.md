---
name: multica-final-reply
description: 在 Multica 的 Slack Task Router 完成业务任务后，整理当前运行记录中的统计、PR 和分支证据，组织最终结论与 Footer，并通过现有 Slack Skill 回复原线程。
---

# Multica 最终回复

仅供 Multica 的 Slack Task Router 使用。业务工作完成且原线程回复已获授权后调用。此 Skill 指导回复内容，`scripts/run_context.py` 只读整理资料；最终发送统一使用现有 slack Skill。

## 回复流程

1. 读取目标环境的 slack Skill，以及 [个人回复风格](references/reply-style.md) 和 [Emoji 使用指南](references/emoji-guide.md)。按个人风格组织结论、实际完成的动作、必要证据和未完成事项；需要更多自定义表情时才检索本地完整目录，不逐次联网拉取。
2. 执行 `rtk proxy python3 <本 Skill 路径>/scripts/run_context.py --issue <当前 Issue UUID> --output <任务私有目录/context.json>`。脚本使用真实 `MULTICA_TASK_ID`，查询当前 run、所属 Agent 配置和 run-messages，输出 `statistics` 与带日志序号的 `code_evidence`。不选择最近一次 run，不轮询等日志补齐。
3. 读取输出，根据业务任务从 `code_evidence` 的调用与结果中提取实际处理的 PR、仓库和分支。证据是候选，不等于已完成的成果；排除仅讨论、示例及失败操作。缺失或截断的输出不能当完整证据，必要时按 github-workflow 只读补查。没有 PR 的分支也可展示，但必须有明确的仓库归属和实际分支证据。
4. 由你组装最终正文、Footer context blocks 和包含同样信息的 fallback。每个 PR 单独一行，PR 去重；仓库只显示 repo，查询、去重和链接使用完整 owner/repo。保留每个 PR 对应的分支，无成果时省略对应行。
5. 按 slack Skill 的发送流程，以 User actor 向原 `channelId` 和根 `threadTs` 一次发送完整消息。直接使用它的 `send --as user --channel ... --thread-ts ... --text-file ... --blocks-file ...`，预检与确认参数以该 Skill 当前版本为准。发送成功后结束，不再追加或更新 Footer。结果不明时先回读原线程，遵守 Slack Skill 的失败处理规则，不自动重发。

## Footer 展示

- 统计行：`:agent_time: 耗时 · :agent_mdi_robot_outline_muted: 模型 · :agent_tool: N tools · :agent_skill: N skills`。按实际有效字段拼接，缺失及零次 tools/skills 隐藏，不展示 token 和缓存率。
- PR 行：`:agent_mdi_github: repo · 分支 · <完整 PR URL|PR #编号>`。分支放行内代码；普通文本按 Slack mrkdwn 转义，不改变真实链接。
- `duration_seconds` 截至采集时刻；tools 是已返回日志中的 tool_use 数量；skills 是可唯一配对的成功 Skill 读取，按名称去重。统计不包含采集后的分析与发送，也不代表运行结束后的完整总量。
- 模型来自当前 run 所属 Agent 的 `model` 配置，`model_source=agent_config`；展示的是配置模型。不能使用模型自我介绍、历史快照或示例补值。
- 运行记录只供组织回复，不将原始日志、凭据或私有路径发到 Slack。脚本返回的缺失字段不需要在面向用户的 Footer 中解释或占位。

资料脚本依赖已认证的 Multica CLI，以及 `MULTICA_SERVER_URL`、`MULTICA_WORKSPACE_ID`、`MULTICA_TASK_ID`。发送凭据、身份预检、消息格式和错误处理全部遵循现有 slack Skill；本 Skill 不提供发送脚本、回执存储或消息更新服务。
