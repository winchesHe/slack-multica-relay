# Footer 展示

组装完整正文、Footer context blocks 和包含同样信息的 fallback。每个 PR 单独一行，PR 去重；仓库只显示 repo，查询、去重和链接使用完整 owner/repo。保留每个 PR 对应的分支，无成果时省略对应行。

- 统计行：`:agent_time: 耗时 · :agent_mdi_robot_outline_muted: 模型 · :agent_tool: N tools · :agent_skill: N skills`。按实际有效字段拼接，缺失及零次 tools/skills 隐藏，不展示 token 和缓存率。
- PR 行：`:agent_mdi_github: repo · 分支 · <完整 PR URL|PR #编号>`。分支放行内代码；普通文本按 Slack mrkdwn 转义，不改变真实链接。
- `duration_seconds` 截至采集时刻；tools 是已返回日志中的 tool_use 数量；skills 是可唯一配对的成功 Skill 读取，按名称去重。统计不包含采集后的分析与发送，也不代表运行结束后的完整总量。
- 模型来自当前 run 所属 Agent 的 `model` 配置，`model_source=agent_config`；展示的是配置模型。不能使用模型自我介绍、历史快照或示例补值。
- 运行记录只供组织回复，不将原始日志、凭据或私有路径发到 Slack。脚本返回的缺失字段不需要在面向用户的 Footer 中解释或占位。
