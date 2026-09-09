# Multica 最终回复 Skill

## 目标与数据口径

Agent 完成业务工作后读取当前运行，分析相关成果，将结论与 Footer 一次发送到原 Slack 根线程。统计采用发送前快照，不计发送及后续调用，不等待结束事件或更新消息。

- 模型：读取当前 run 所属 Agent 的 `model` 配置，核对 Agent ID 和工作区；这是配置模型，快照记录 `model_source=agent_config`。
- 耗时、tools、skills：取发送前快照。日志尚未上传或证据无法唯一配对时隐藏无法确认的字段；零次 tools/skills 隐藏。
- PR 与分支：AI 选择本次任务直接相关的 PR，脚本核对当前运行中的工具证据，再从 GitHub 查询真实分支。每个 PR 一行，仓库只显示 repo，完整 owner/repo 用于查询、去重和链接。
- 使用 `:agent_mdi_robot_outline_muted:` 和 `:agent_mdi_github:`；不展示 token 与缓存率。

## 当前交付

`multica-skills/multica-final-reply` 是独立 Skill 包：

- `SKILL.md` 定义何时回复、如何选择成果、数据来源和展示规则。
- `scripts/final_reply.py` 提供 snapshot、prepare、send 三个步骤，将正文和 Footer 组装成同一条消息。
- `scripts/slack_sender.py` 调用现有 Slack Skill，预检身份和线程、持久化运行回执；相同 bundle 重复调用返回 duplicate，发送结果不明时停止重发。

包内发送器不依赖仓库外的回复脚本。Runtime 提供已认证的 Multica/GitHub CLI、Slack Skill 入口和固定发送身份；回执目录跨运行保留。Skill 仅供 Multica 使用，不安装到本地全局。

## 验证

单测覆盖运行归属、Agent 配置模型、日志缺失和并发读取、PR 证据、多仓库、分支回读、正文内容绑定、发送失败与持久化去重。集成测试从 bundle 调用包内发送器，并验证重复执行不再次调用 Slack。

2026-09-09 的[真实运行验收](https://moegoworkspace.slack.com/archives/C0B0DSCRBKM/p1788923887786569?thread_ts=1788882331.909789)确认正文、`25s · gpt-6-astra · 5 tools · 2 skills` 与 PR／分支一次发送。该消息使用发送器收进包内之前的实现；包内入口另由集成测试验证。随后调整为只展示 repo，旧测试消息未编辑。

## 后续阶段

第一阶段以独立 Skill 包和草稿 PR 交付。按项目分阶段规则，确认后再进行生产切换：

1. 补齐没有 PR 时的分支证据与仓库归属校验。
2. 通过 Multica CLI 发布 Skill，仅绑定 Slack Task Router，更新并回读目标 Agent 的最终回复指令。
3. 停用当前线上旧 Footer 插件及配置，验证正式任务一次发送、重复执行不重发。
