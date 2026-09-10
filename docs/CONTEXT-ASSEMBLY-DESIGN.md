# Slack 上下文组装

[English](CONTEXT-ASSEMBLY-DESIGN_EN.md) | **简体中文**

每次接受 mention，Relay 获取同一会话最近 24 小时的主时间线消息，展开当前线程和最多五个最近活跃的旁支，并始终保留当前线程。数据按树传入 Multica，让 Agent 知道回复属于哪个讨论。

## 输入与时间边界

- 当前请求和路由真源为 `eventPayload`。`messageTs` 是整棵树的截止时间。
- 主时间线窗口为 `[messageTs - 24 小时, messageTs]`，最多保留最近 40 条根消息。广播的线程回复不另作根节点。
- 直接从 24 小时窗口选择最近 40 根，不分阶段扩窗；旁支按 `latest_reply` 选择最近活跃的五根后再读取回复。
- 当前线程根以 `threadTs` 标识，即使早于窗口也加入；已经位于主时间线时去重。
- 所有线程回复都截止于本次 mention。每次新 mention 刷新树，同一次事件的重试复用已准备的快照。
- 窗口外其他线程不会自动展开；窗口内根节点的线程可包含窗口前的回复，以保留这段讨论的关系。

## 数据契约

marker 保留第一行，之后依次展示原文引用、来源及 JSON 数据区；`relay-payload:v1` 成对标记包围数据围栏。恢复解析器同时支持历史 marker + 裸 JSON，损坏或重复区块拒绝恢复。`schemaVersion: 4` 表示支持精选 follow-up 的树结构；`eventPayload` 保持不变，旧 JSON 仍能用于已有 Issue 的路由恢复。

```json
{
  "schemaVersion": 4,
  "task": { "instructions": ["Relay 固定生成的任务解释和原线程回复约定"] },
  "eventPayload": { "channelId": "C…", "threadTs": "2000.000001", "messageTs": "2100.000001", "text": "本次请求" },
  "context": {
    "anchorTs": "2000.000001",
    "cutoffTs": "2100.000001",
    "capturedAt": "采集时间",
    "participants": [{"id": "U…", "name": "显示名"}],
    "timeline": {
      "status": "complete",
      "messages": [
        { "ts": "1900.000001", "authorId": "U…", "text": "附近主消息", "replies": {"status": "complete", "messages": [{"ts":"1950.000001", "text":"该消息的回复"}]} },
        { "ts": "2000.000001", "authorId": "U…", "text": "当前线程根", "replies": {"status": "complete", "messages": [{"ts":"2100.000001", "currentRequest":true, "text":"", "files":[]}]} }
      ]
    }
  }
}
```

示例省略普通消息的 `origin`、`files` 及部分路由字段。`currentRequest: true` 节点引用 `eventPayload`，不重复正文与附件。每个 replies 只含子回复，不重复父节点。

`complete` 只描述对应列表的读取范围；子分支可能独立为 `truncated` 或 `unavailable`。`coveredFromTs/coveredThroughTs` 描述实际保留消息的时间范围，不证明中间毫无缺失。无法读取当前线程根时保留 ID，并标记 `contentStatus: unavailable`。

## 同线程 follow-up 精选

首次 Issue 提供有界树；后续 comment 使用 `context.selection.mode=focused`：始终保留本次请求、线程根和最近 20 条线程回复，并保留当前请求中标准 Slack 消息 permalink（`/archives/<channel>/p<timestamp>`）所引用的、已读取的同频道较早回复。旁支优先选择明确链接引用，再选有新增/更新消息的较近分支，最多 5 根。每个入选分支保留父节点；回复带新增、更新或明确引用的消息，以及每条变化前最多两条必要前文。引用根节点时保留该分支的有界上下文。链接不在已读取范围时仍可能需要补查。

每条 comment 都是独立可读的精选快照，不是需要 Agent 合并的 delta。省略旧旁支可能影响模糊指代的解释，task.instructions 明确要求按实际范围回答，缺少依据时补查或澄清。`omittedRoots/omittedCurrentReplies` 标记从本次候选读取中省略的数量，不代表全频道总数。

程序按单条消息对作者、未裁剪正文和全部附件的稳定引用字段生成内部指纹；传给 Agent 的正文仍最多 4 KiB、附件仍最多五个，内部指纹不进入 envelope。子回复、采集时间和展示标记不混入同一指纹。只有成功写入 Multica（或 marker 回读确认成功）后才推进已发送消息索引；这是写入回执，不证明模型读过或仍记得。索引按当前路由 scope 保存 24 小时，最多 500 条消息，跨轮保留已发送消息指纹，避免已发送旧消息下一轮重新出现。乱序事件不回退索引；索引丢失时明确 baseline=unavailable，保留最近最多 5 个旁支，不假定它们已发送。

字节裁剪后未进入最终正文的消息不登记为已发送；只索引本次实际写入的消息，不顺带确认同分支被省略的兄弟回复。索引不包含聊天正文，过期只降低精简程度。Slack 先从主时间线选择最多五个最近活跃旁支再展开，避免为最终会省略的旧旁支发起网络读取；精简继续减少 Issue 正文。24 小时窗口用于候选读取，当前未实现 Slack 网络读取缓存。

## 预算与失败

| 项目 | 上限与策略 |
| --- | --- |
| 主时间线根节点 | 最近 40 条；窗口外当前根可额外加入 |
| 当前线程 | 根 + 最近扫描范围内 100 条回复 |
| 旁支线程 | 每个根 + 最近扫描范围内 20 条回复 |
| 回复总量 | 200 条；当前线程优先，再按根消息从近到远分配 |
| 分页 | history 最多 5 页、当前线程最多 10 页、旁支最多 3 页；conversation 请求总计最多 20 次 |
| 时间与并发 | 消息读取共 20 秒；先当前线程和主时间线，再从最近活跃旁支中最多并发读取 4 个 |
| 单消息 | 正文 4 KiB、附件最多 5 个；附件仅引用、内容 not_loaded |
| 输入响应 | 每个 Slack 响应最多 2 MiB |
| 输出 | 树 32 KiB、完整序列化 envelope 48 KiB；包含任务说明和姓名 |

`conversations.replies` 必须读到游标末尾才保留最近回复；若页数、调用数或时间预算内到不了末尾，则丢弃已扫描的旧回复前缀并标记 `latest_suffix_unavailable`。总量超限优先丢弃较旧旁支的回复。字节超限先移除较旧可选树，再缩减当前线程回复，保留当前根和本次请求引用；必要时截断当前根正文。当前请求本身放不下则拒绝发送，不静默修改请求。

当前线程或主时间线的 429、5xx、超时等暂时失败由队列重试。明确权限失败标记 unavailable。旁支失败只标记该分支，暂时错误会停止后续旁支网络调用，未读取分支标记 read_budget；跨频道或跨线程的响应始终拒绝。

## 姓名与职责

`context.participants` 覆盖当前发送者、目标用户 mention 及树内作者。每次最多解析 10 个去重用户、并发 4、总预算 3 秒；优先 display_name，其次 real_name、用户名。读取失败省略 name，保留 ID，不阻塞请求；不投影邮箱或完整 profile。姓名不参与授权。

Relay 的固定 `task.instructions` 解释当前请求、树节点、缺失标记和原线程回复约定，不能由 Slack 文本改写。Agent 长期 Instructions 保留人格、授权和隐私规则；Multica Runtime 负责任务定位与生命周期；Slack Skill/私有 Runtime 配置负责凭据和工具绑定。marker 不是签名，任务正文不是权限真源。

## 快照与恢复

- `:envelope` 按事件冻结完整正文 24 小时；重复事件不重建输入。
- 新 mention 不读取首次背景、Issue 中的旧 context 或旧 `:background` 缓存。
- 旧缓存自然按 TTL 过期；已存 Issue/Comment 不回写。旧 envelope 仅用于恢复原路由与消息身份。
- Redis thread mapping 缺失时，通过唯一 thread marker 的 Multica search 恢复 Issue，不再线性扫描整个 Project。幂等状态和写入结果不明时的恢复规则保持不变。
- Multica 保存的历史快照按其自身保留政策处理，Redis 到期不删除 Multica 内容。

## 验收

自动化验证窗口、40 根上限、当前根去重与窗口外保留、五个最近活跃旁支、统一截止、回复总量、最新后缀分页、字节预算、权限和限流标记、入队前附件投影、完整内容指纹、姓名、重试冻结，以及通过 marker search 从旧 Issue 恢复后新 mention 刷新树。

真实验收在授权测试频道建立旁支线程，再触发当前线程；检查 Multica 中的树和 Agent 对旁支内容的回答。随后增加新的主消息及旁支回复，在当前线程再次 mention，确认新背景被带入、复用同一 Issue，且最终回复落在当前线程。


## 展示与模型配置快照

标题使用消息摘要 + 稳定 scoped thread 短标识。正文引用仅展示前 4 KiB，完整请求在 envelope 中保留；安全转义引用、JSON 字符串和数据围栏，避免 Slack 文本改变结构或触发 Multica mention。原有 envelope 48 KiB 上限不变，展示正文整体上限 64 KiB，超出时先减少 JSON 排版空白，仍放不下则拒绝发送。

`replyContext` 与 eventPayload/context 同级，来自经过 Agent/Workspace ID 校验的 Agent 配置快照。每次新消息准备时查询一次，最多 2 秒，失败标记 unavailable；随 envelope 冻结。模型为空/非法时为 null，serviceTier 只接受 priority/default，否则为 null。仅投影模型、档位、来源、身份和采集时间，不传 instructions、凭据或完整配置。

footer 使用每个新事件各自冻结的快照和 `messageTs`；只有配置模型已知且 Agent 匹配时显示。priority 追加 Fast，default/null 不追加 Fast，null 不证明默认档位关闭。查询失败或模型为空时省略模型字段，自动化标识仍由发送适配器追加。scripts/slack-reply.py 从原 Issue/Comment 回读 envelope，从私有配置读取固定显示名，将正文发送为 section blocks，并追加 context/mrkdwn footer；fallback text 同样包含正文和 footer。任务说明只指向运行时发送入口，格式与标识由代码负责，人格 Instructions 不要求模型生成标识。



发送适配器配置保存在私有 Runtime 中，字段为 displayName（包含 emoji 的完整显示文字）、agentId、workspaceId、projectId、teamId、serverUrl，不包含凭据。--issue-id/--comment-id 定位本次来源，--text-file 提供正文；适配器核对 Issue 的 workspace/project/assignee 与配置一致，路由取自原 envelope。认证沿用 multica CLI 和 `SLACK_USER_TOKEN`。--dry-run 只生成 payload，不发消息。每个 source Issue/comment 生成稳定的 delivery block ID，并在配置旁、已忽略的私有 `.slack-reply-state/` 原子记录 `attempting/accepted/sent`；文件和父目录均在 POST 前同步。Slack 返回的消息时间用于窄范围发送后回读；`sent` 重跑直接返回持久结果。结果不明时从本机尝试时间前五分钟开始核对，不从原请求扫描整条长线程；查不到 marker 时返回 `slack_delivery_unknown`，不得自动重复 POST，清理该状态前必须在原线程独立核对。本机同源锁不可获得时立即返回 `reply_delivery_busy`。

这里保证经适配器发送的回复格式；它不是对全部本地工具或直接 Slack API 调用的强制安全代理。普通 Relay 回复的调用入口通过私有 Skills 配置绑定。

可选的最终回复 Skill 在发送前只读采集当前 `MULTICA_TASK_ID` 的运行统计和 GitHub 操作候选。业务相关性由 Agent 判断，结构化结果交给 adapter 校验并渲染；未唯一配对、失败、截断或仅出现在文档示例中的记录不能作为 PR/分支成果。现有 source scope、at-most-once ledger 和发送后回读保持不变。


## 消息变化标记

旁支消息的 change 为 new（相对已发送索引未出现）、updated（指纹变化）、referenced（明确链接引用）、context（必要背景）。父节点不因子回复变化就标为更新；更新只提供当前正文，不默认附旧文本。新增不等同于此刻刚发布。基线不可用时只标背景/引用，避免声称已完成变化比较。窗口、分页和精选裁剪都可能让消息缺席，因此缺席不生成删除通知。

context.selection 的 added/updated/referenced 只统计最终实际携带的旁支消息，当前请求和当前线程保留的对话不计入。可读正文展示这组统计。旧分支级索引没有消息级版本，按 unavailable 处理，新快照持久化后建立消息索引；旧 envelope 和 marker 的恢复协议不变。


## 前文窗口与裁剪诊断

旁支每条新增、更新或明确引用的回复，额外保留同分支中前面最多 2 条已读取回复，标记为 context。多个窗口合并去重，顺序不变，后文不自动带入，仍受已有总量与字节预算约束。它是帮助指代的轻量启发式，不保证两条前文足以解释所有指代。

构建时输出结构化 relay_context 日志：eventId（消息键摘要）、full/focused、baseline、Slack 消息读取调用数、原始返回消息条数、候选与保留的根数/消息数、省略条数、旁支新增/更新/引用数量、读取缺失原因、未变化根省略数、旁支根数量上限省略数、入选分支中未携带的兄弟回复数、当前线程回复上限省略数、字节预算额外裁剪数与输出截断原因、messageReadMs、nameLookupCalls、nameReadMs、agentConfigMs、assemblyMs 和 envelopeBytes。原始条数包含分页和不同读取路径的重复；候选条数是窗口过滤与投影后的树，保留条数是最终 envelope。各耗时字段分别对应 Slack 消息读取、姓名解析、Agent 配置快照和纯组装阶段。

这些指标用于诊断构建，不代表 Multica 写入成功；与 relay_dispatch 的 action/result 对照。重用同一事件快照记录 snapshot=reused 和字节数，不重复读取。日志不含聊天正文、姓名、附件内容、凭据或完整 envelope。readStats/selectionStats 只供构建端统计，不进入模型输入。
