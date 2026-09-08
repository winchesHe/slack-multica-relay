# 完成 Hook 与 Slack Footer 实施计划

## 目标与最终验收

Agent 使用统一发送入口正常回复，代码自动关联 Multica task/run 与实际 Slack message ts。Multica 完成 Hook 只携带运行标识；Vercel 验签入队，消费函数读取真实运行数据并更新同一条消息。正文、发送身份与 thread 不变。只给最终回复加统计，无回复的运行保持静默。

最终格式如下，数字仅为展示示例：

```text
:agent_time: 14m 12s · :agent_mdi_robot_outline: gpt-5.6-sol: 5399.8k tokens (97% cached) · :agent_tool: 27 tools · :agent_skill: 2 skills
```

全部位于一个 context block。顺序固定，缺失项隐藏，分隔符仅连接实际存在的项目。不增加状态、任务链接、Skills 名称、未知值占位或客户端签名。Tokens 固定以 k 显示一位小数；缓存比例四舍五入到整数。零值只有在有可靠数据证明时显示。

运行详情提供耗时、实际模型、usage；Tokens 合计 input/output/cache_read/cache_write。Codex 缓存比例为 cache_read/(input+cache_read)，不重复扣除缓存。Tools 为完整运行消息中的 tool_use 数量。Skills 为从成功读取 SKILL.md 并取得 frontmatter name 的证据中识别、去重的数量；不能把绑定、搜索路径、Agent 自报、失败读取或无法配对的日志当使用证据。日志缺失或无法判断完整性时隐藏相应统计。其他 provider 的缓存口径未验证时隐藏 cached。

## 阶段与边界

| 阶段                      | 范围                                                                                                   | 验收                                                                                  |
| ------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| 1：端到端最小链路（已交付代码） | 回复脚本、登记、Hook 验签、QStash 投递、Worker 按 taskId 更新原消息；仅展示耗时；配置开关与上线候选    | 本地集成测试覆盖正常流程、重复与乱序、作者/范围校验、未知写结果；真实受控链路另行验收 |
| 2：完整统计（当前）       | 模型、Tokens、缓存、Tools、Skills 解析，日志分页与数据缺失；最终移除旧 replyContext 查询和兼容渲染代码 | 与真实运行逐项对齐，严格符合最终格式，缺失项不显示，完整正文保留                      |
| 3：恢复与上线             | 有限补查、失败/取消终态、低频漏事件补偿、DLQ 运维、消息版本与容量边界、真实冷/热延迟及用量             | 丢失事件和临时故障可恢复，续问不串消息，关闭开关不影响正文                            |

各阶段独立验收，完成后等待确认再进入下一阶段。前两阶段已实现并本地验证；第三阶段的恢复机制和真实上线验收仍待交付。

## 当前实现契约

- Vercel：`POST /api/multica/events` 接收 Hook，`POST /api/slack/replies` 登记已发送回复，`POST /api/queue/footer` 消费 QStash。复用既有 QStash SDK、Redis REST 存储、Vercel 原始请求适配器和任务来源解析器。
- Hook：只接受已配置安装、工作区、hook key、目标 Agent 的 task.completed。使用 whsec 十六进制密钥对 timestamp + '.' + 原始 body 做 HMAC-SHA256，校验五分钟窗口。事件持久化与队列接收确认后返回 202；失败保留重试责任。callback_token 不保存、不传给队列。
- 队列只携带 version/issueId/taskId。投递源（Hook 或登记补触发）区分去重键，最终更新仍按 task 去重。真实 QStash Receiver 校验消息体、消费 URL 和 current/next signing key。
- 消费和登记查询精确 issue/run，校验工作区、项目、Agent、Issue 来源 marker、原 thread 与当前频道策略，不使用最新 run 猜关联。当前只支持 Relay 直接创建且具有来源 marker 的 issue。
- 登记与更新使用 Slack auth.test 验证配置的写作者和工作区，并读取精确目标消息确认作者/根 thread。线上当前为 User，默认保持 User；支持显式 Bot 配置，但禁止自动切换。读写 token 不同时校验二者工作区一致。
- 每个 run 只登记一条最终回复。Redis 保存关联、正文摘要、完成事件和更新完成标记，保留 90 天；不存正文或完整运行日志。已登记正文发生变更时停止覆盖。读取现有 blocks 并追加一个有稳定标识的 context block，保留附件和 fallback text。无 blocks、已有 50 blocks 或文本超限时省略，不截断正文。
- Runtime 脚本调用既有 Slack Skill CLI，先 dry-run，带相同 preview digest 发送，随后自动登记。持久化发送意图与回执；发送成功、登记失败时重跑只补登记。发送结果不明时停止自动重发。每 run 一条最终回复，长正文拆分/多次最终编辑暂不支持。
- 完成通知先于登记到达时，Worker 返回 waiting_for_reply；随后登记发现完成状态或已有事件会重新投递。没有最终回复不会单独发统计。
- 更新超时或响应不明时，队列重试先回读同一条消息，通过 footer block 与正文摘要确认是否已更新，不再次发送正文。
- `RELAY_FOOTER_ENABLED` 默认关闭。新建与续问均已删除旧 replyContext 查询和封装；无论开关状态都不再 GET Agent 配置。关闭时 Agent 直接回复正文，不附加统计。
- 本阶段只新增 Vercel 入口；现有 EdgeOne 入站功能不改，不宣称 EdgeOne 支持新 footer。

## 验证与尚未完成的部分

2026-09-08：本地已通过 129 个 TypeScript 测试、9 个 Python 发送回执测试，以及 pnpm lint。QStash 验签测试使用真实 Receiver 与本地签名 JWT；外部 Multica、Redis、队列投递和 Slack 请求使用可观测的模拟边界，覆盖完整 HTTP 处理链路。它不等于托管平台真实端到端验收。

真实 Multica 运行已只读核对：issue 包含 workspace/project/assignee 与 Relay 来源 marker，run 包含运行 ID、起止时间与 usage。样本可计算 6m 30s、3256.5k tokens、96% cached、51 tools，并找到 4 个成功加载的 Skills；第二阶段代码通过只读 CLI 实时读取同一样本验证，完整 footer 与这六项数据一致；原始运行日志未保存到仓库或 Redis。

当前 CLI 的 `multica plugin --help` 返回 unknown command。按项目规定不得因此改用浏览器或绕过 CLI 创建安装。本阶段提供安装 manifest 示例，未创建插件安装、同步线上 Prompt、配置 Secret、部署生产或发送真实 Slack 测试消息。发布前需要补齐可用的 CLI 安装入口，并指定受控验收目标。

Webhook 分发在检查的 Multica 源码中使用内存队列，可能丢失；第三阶段需要补偿。当前队列重试耗尽后仍需人工查看和重放，暂不提供定时恢复。发送成功但本地回执未写完的进程崩溃会保留 sending 状态，需人工核对，不承诺 exactly-once。

## 第二阶段统计与数据边界

- 实际模型来自当前 run 的 usage，多个模型按首次出现顺序去重；总 Tokens 合计所有 usage 行。计数缺失、负数、小数或超出安全整数范围时隐藏总量，不能把部分行当完整总量。模型字段异常时隐藏模型名称，可靠总量仍可展示。
- 已验证 Codex 的 input 不含 cache_read；缓存比例使用 cache_read/(input+cache_read)，分母为零、provider 未验证或 Codex 出现 cache_write 时隐藏 cached。模型/usage 缺失不会阻止耗时和日志统计展示。
- 实际接口 `GET /api/tasks/{taskId}/messages` 返回完整有序数组，支持 since 增量而没有分页游标。因此当前实现一次读取完整数组，不发送虚构的 limit/offset；将计划中的“日志分页”落实为真实接口的完整性与容量校验。超过 4 MiB、10000 条、序号不从 1 连续递增、跨 issue/task、未知消息类型、空数组或查询失败时隐藏 Tools/Skills。
- Tools 计每条 tool_use，含失败调用但不重复计 tool_result。Skills 按成功读取输出的 frontmatter name 去重；只识别简单 cat 单个 SKILL.md，支持 shell 包装、rtk/proxy 和引号路径。搜索路径与明确失败读取不计数；并发无法配对、未返回或复杂读取无法核实时隐藏 Skills。此值代表本次日志中已证实加载的 Skills，不代表历史上下文复用或实际调用次数。
- 最终展示文本写入 Redis 统计快照后才更新 Slack，重试复用同一快照，不重新取日志。此阶段查询失败会隐藏相应项，之后不自动补齐迟到数据；有限补查纳入第三阶段。完整日志只在消费进程内存中解析。
- 第二阶段未新增线上写入。插件安装、Prompt 发布、生产部署与受控 Slack 端到端验收仍需第三阶段完成。
