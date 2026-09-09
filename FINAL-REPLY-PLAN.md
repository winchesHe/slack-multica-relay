# Multica 最终回复 Skill 交付计划

## 目标与验收标准

Agent 完成业务工作后自行读取当前运行，分析相关成果，将结论与 Footer 一次发送到原 Slack 根线程。统计采用发送前快照，不计发送及后续调用；去掉 token 和缓存率，保留能确认的模型、耗时、非零 tools/skills 及相关仓库、PR、分支。不依赖完成回调、队列或消息二次更新。

Skill 只发布到 Multica 并绑定 Slack Task Router，不安装到本地全局 Skill 目录。保留现有发送身份、线程目标、未知结果不重发和持久化幂等约束。

## 第一阶段：独立可运行骨架

- 在仓库内提供 `multica-skills/multica-final-reply`，通过 Multica CLI 采集当前 run/messages；AI 选择相关 PR，脚本校验证据并通过 GitHub 查询真实分支。
- 复用现有 `scripts/reply.py` 的身份预检、发送与回执，增加仅供新 Skill 使用的不登记发送模式；旧调用默认行为不变。
- 正文 blocks 和 Footer context 一次提交；快照、正文、bundle 只落当前任务私有目录，重复执行同一 bundle 不重复发送。
- 完成失败路径与幂等测试、真实运行发送前采集及一次发送验收，并以草稿 PR 交付。
- 阶段边界：不修改生产 Agent prompt、Skill 绑定或完成插件。第一阶段只展示经 PR 查询证实的分支；无 PR 分支支持在第二阶段完成，当前可在业务正文中说明。

## 第二阶段：正式切换与完整成果覆盖

第一阶段验收并获得继续确认后执行：

- 补充无 PR 分支的当前运行证据与仓库归属校验；覆盖多仓库、多 PR、只有分支及没有代码成果。
- 使用 Multica CLI 发布 Skill 包并仅绑定目标 Agent，部署 Runtime 脚本，更新并回读 Prompt 真源与线上 instructions。
- 停用旧 Footer 插件和登记／更新路径，移除相应代码、配置和过时文档；保留 Slack 入站 Relay 功能。
- 在真实任务核对正文与 Footer 一次发送、后续无回调更新、重复调用不重发，完成测试并更新草稿 PR。

阶段确认来自项目 AGENTS.md 的分阶段交付规则；每阶段交付可运行产物和验证结论，不以第一阶段替代整体需求。

## 第一阶段验证结果

2026-09-09：`pnpm test` 的 136 项 TypeScript 与 26 项 Python 测试通过，`pnpm lint` 和 Skill 结构校验通过。覆盖当前 run 归属、日志缺失、跨运行日志拒绝、PR 证据排除、多仓库 PR、分支回读、正文绑定、发送未知结果与重复执行。

真实运行 `01a0841e-8d67-7287-b4eb-0fbf46d899cf` 在运行中通过 Multica CLI 取得前 10 条日志；快照时间为 `2026-09-09T03:03:43.488682+00:00`。同一快照中定位 PR #6 的真实查询，再回读 GitHub 分支为 `feat/completion-hook`。首次发送返回 `sent`，相同 bundle 重复执行返回 `duplicate`；Slack 独立回读确认一条消息包含完整正文和两个 context，正文未被截断。

[真实验收消息](https://moegoworkspace.slack.com/archives/C0B0DSCRBKM/p1788923068395629?thread_ts=1788882331.909789) 展示 `33s · 5 tools` 和 PR／分支。该消息发送时批量读取 Skill 尚未识别，因此隐藏了 skills；随后补充批量 `cat` 支持，使用同一真实快照离线复算得到 `2 skills`，未为此重发或编辑消息。

运行 API 当前未提供实际模型字段，因而隐藏模型；第二阶段需核对 Runtime 是否有可验证的实际模型来源。模型来源未确认前不以 Agent 配置冒充。普通无 PR 分支的 Footer 和生产链路切换仍属于第二阶段。
