# 配置说明

[English](CONFIGURATION_EN.md) | **简体中文**

Relay 与 Multica Agent Runtime 是两个独立的信任域。Relay 变量配置在 Vercel、EdgeOne 或 Cloudflare Workers；Agent Runtime 变量配置在执行目标 Multica Agent 的机器或 Pod。不要把一侧的完整环境变量复制到另一侧。

## Relay 部署环境

以下变量配置在部署平台中。凭据和 token 必须放入平台的 Secret Store。Slack ID 和服务 URL 属于普通配置，也可以作为 Secret 保存。

Slack ID 使用逗号分隔的大写标识符。`SLACK_TARGET_USER_IDS` 与 `SLACK_TARGET_SUBTEAM_IDS` 可以分别留空，但至少一项必须包含目标。blocklist 的优先级始终高于 allowlist。

| 变量 | 必填 | 取值与来源 |
| --- | --- | --- |
| `SLACK_SIGNING_SECRET` | 是 | Slack App → Basic Information → App Credentials → Signing Secret。 |
| `SLACK_TEAM_ID` | 是 | Slack Workspace ID，通常以 `T` 开头。 |
| `SLACK_TARGET_USER_IDS` | 条件必填 | 显式 mention 后会触发 Relay 的 User ID。 |
| `SLACK_TARGET_SUBTEAM_IDS` | 条件必填 | 显式 mention 后会触发 Relay 的 User Group ID。 |
| `SLACK_ALLOWED_CHANNEL_IDS` | 是 | 允许触发的 Channel ID，逗号分隔。只有确实允许所有频道时才填写 `all`；缺失或留空会拒绝启动。 |
| `SLACK_BLOCKED_CHANNEL_IDS` | 否 | 永不触发的 Channel ID；即使 allowlist 为 `all` 也优先阻止。 |
| `SLACK_ALLOWED_SENDER_IDS` | 否 | 允许触发的 User ID。缺失或留空等同 `all`；owner-only 部署应填写 owner 的准确 ID。 |
| `SLACK_BLOCKED_SENDER_IDS` | 否 | 永不允许触发的 User ID。 |
| `SLACK_BOT_TOKEN` | 条件必填 | reaction 添加、查询和清理优先使用的 Bot 身份，需 reactions:read/write。 |
| `SLACK_USER_TOKEN` | 条件必填 | 未配置 Bot 时用于 reaction 的 User 身份。 |
| `SLACK_CANCEL_KEYWORDS` | 否 | 逗号分隔的完整取消关键词，默认 cancel,取消。 |
| `SLACK_CONTEXT_TOKEN` | 是 | 带目标会话 history scopes 的 user token。consumer 用它读取有界 history/replies；如需通过 `users.info` 补充参与者姓名，还需 `users:read`。可以与 reaction 使用同一个已授权 token，但必须显式配置。 |
| `SLACK_REACTION_NAME` | 是 | 不带冒号的 Slack emoji shortcode，例如 `eyes`。reaction 只确认 Relay 已持久化，不证明 Agent 已完成。 |

Slack App Request URL 为 `https://<deployment-host>/api/slack/events`。只订阅目标频道类型需要的 message events；读取私有频道时必须把 App 安装到对应频道。

| 变量 | 必填 | 取值与来源 |
| --- | --- | --- |
| `MULTICA_API_BASE_URL` | 是 | 目标 Multica Server 的 HTTPS base URL，不含额外路径、query 或 fragment。 |
| `MULTICA_API_TOKEN` | 是 | 能访问目标 Workspace、Project 和 Agent 的专用 Multica PAT。 |
| `MULTICA_WORKSPACE_ID` | 是 | Relay Project 所属 Workspace 的 UUID。 |
| `MULTICA_PROJECT_ID` | 是 | Relay 创建 Issue 的专用 Project UUID。 |
| `MULTICA_AGENT_ID` | 是 | Relay Issue 指派的 Agent UUID。Agent 必须属于同一 Workspace，并已绑定 Runtime。 |

Relay 调用普通 Issue 和 Comment API，不需要 Multica Autopilot 或 webhook URL。

| 变量 | 必填 | 取值与来源 |
| --- | --- | --- |
| `KV_REST_API_URL` | 是 | Relay 专用 Upstash Redis 的 HTTPS REST URL。 |
| `KV_REST_API_TOKEN` | 是 | 同一个 Redis 数据库的 REST token。 |
| `QSTASH_URL` | 否 | QStash API base URL，默认 `https://qstash.upstash.io`。 |
| `QSTASH_TOKEN` | 是 | QStash publish token。 |
| `QSTASH_CURRENT_SIGNING_KEY` | 是 | 验证当前 QStash 投递的 signing key。 |
| `QSTASH_NEXT_SIGNING_KEY` | 是 | key rotation 期间用于验证下一把 key。 |
| `RELAY_CONSUMER_URL` | 是 | 准确的公开 URL：`https://<deployment-host>/api/queue/consume`，必须与 QStash 验签使用的 URL 完全一致。 |

Redis 必须由该 Relay 独占，用于保存线程映射、锁和写入确认状态。QStash 保存排队的 Slack payload，并重试失败的 consumer；其访问控制与保留时间应按 Slack 消息内容的敏感程度配置。

Redis 还会保存 24 小时的 scoped message fingerprint index（最多 500 条）用于选择 follow-up 上下文。这些记录表示已持久化内容，不代表 Agent Session 记忆。准备好的 envelope 也保存 24 小时，包括已投递副本，直到 TTL 到期。每次新 mention 都重新读取 timeline；只有同一事件的重试复用冻结快照。旧 background cache key 不再读写，按原 TTL 自然过期。Multica 按自己的保留策略保存已投递 envelope；Redis 到期不会删除 Multica 内容。

Relay 在发布到 QStash 前，把每个 Slack file object 投影为 `id`、`name`、`mime`、`size` 和 `contentStatus`，并额外携带由完整安全字段生成的 fingerprint。下载内容、private URL、thumbnail、shares 和凭据不会进入队列 payload。暂时性或结果不明的失败返回 503 交给 QStash 有限重试；无效队列 payload、损坏的持久状态、scope 违反、无法消除的映射歧义和确定性体积超限返回 `rejected` 与 `retryable: false`。访问失败和有界读取缺失按上下文合同表达。

## 取消与 reaction 身份

在原任务 thread 发送目标 mention 加完整的 `cancel` 或 `取消` 可停止关联任务。`SLACK_CANCEL_KEYWORDS` 的非空列表替换默认词；只有 `SLACK_TARGET_USER_IDS` 中的发送者可取消，用户组 mention 不授予取消权，入队与消费时都复核准入。取消不创建新的 Agent 任务，也不读取上下文树。

取消意图、run ID 集合和清理进度与原线程映射一起保存在 Redis；重试只处理原 run 集合。取消期间收到的普通消息被忽略；结束后新 mention 可继续原 Issue。回读 run 终态后仅清理所选身份自己的 reaction，其他人的表情保留。`cancelled` 证明 Multica 状态及中断请求，不是独立的 daemon 停止确认，也不会撤销已完成的外部写入。

reaction 添加、查询和清理统一使用非空 `SLACK_BOT_TOKEN`，未配置时使用 `SLACK_USER_TOKEN`。所选身份需要 `reactions:read`、`reactions:write` 和目标频道访问权；调用失败不会换身份。上下文仍用 `SLACK_CONTEXT_TOKEN`，Agent 最终回复仍使用 Runtime 自己的 user token。

旧 `SLACK_REACTION_TOKEN` / `SLACK_REACTION_READ_TOKEN` 不再读取。升级前将现有凭据按原身份配置到新变量，确认后再切换代码；旧变量可保留给旧部署回滚使用。若从 User 切到 Bot，历史 User reaction 保留。回滚取消功能前应先处理队列内 `operation=cancel` 的消息，避免旧消费者将其当普通请求。QStash 重试耗尽时保留 DLQ 责任，不删除 Redis 状态来强行重发。

## Multica Agent Runtime

Relay 不读取以下变量。只有 Agent Instructions 或本地 Slack Skill 需要时，才在 Runtime 或 Agent 环境中配置。

| 变量 | 用途 |
| --- | --- |
| `RELAY_OWNER_SLACK_USER_ID` | 授权判断使用的 owner ID。必须来自已验证 Slack event author，不能使用 display name。 |
| `RELAY_SKILL_ROOT` | 已批准本地 Skill 目录的绝对路径。该路径与机器绑定，不属于本仓库。 |
| `SLACK_SKILL_ALLOWED_CHANNELS` | Slack Skill 读写时执行的 Channel allowlist，应与 Relay policy 保持一致。 |
| `SLACK_USER_TOKEN` | Agent Slack Skill 和本地 reply adapter 使用的 owner user token。 |
| `SLACK_TOKEN` | Slack Skill 需要时使用的兼容变量，必须显式指向预期的 owner identity。 |
| `SLACK_BOT_TOKEN` | 回复必须使用 owner identity 时留空，否则 Slack Skill 可能优先选择 Bot token。 |

Agent 不需要单独的 `RELAY_ALLOWED_CHANNEL_ID`。Relay 会在入队前校验 Team、channel、sender 和 mention policy，并在消费时再次校验。`SLACK_SKILL_ALLOWED_CHANNELS` 只约束经过 Slack Skill 的调用。本地 reply adapter 是独立路径：它从指定 Multica Project 的来源 Issue/comment 读取目标，并使用 `SLACK_USER_TOKEN` 直接发送。

## 配置真源与生效方式

| 配置面 | 责任真源 | 如何生效 |
| --- | --- | --- |
| Relay 准入、队列和持久化 | 上述部署环境变量 | 使用更新后的环境重新部署已测试 commit，再回读 health 与 policy 行为。 |
| 回复显示名和固定路由身份 | 传给 `scripts/slack-reply.py` 的私有 Runtime JSON | adapter 每次执行都会读取；新的 `displayName` 从下一次发送起生效。 |
| Agent 人格和操作指令 | Multica Agent Instructions | 可以用本地私有 prompt 维护候选，但必须通过 `multica agent update --instructions` 显式同步，并回读同一个 Agent ID。只修改本地 prompt 文件不会影响 Runtime。 |
| Slack 等本地工具 | 私有 Runtime Skills 与环境变量 | 更新 Runtime binding/configuration，并回读已绑定 Skill 和环境。本仓库不会自动加载这些文件。 |

## 部署验收

1. 运行 `pnpm install --frozen-lockfile`、`pnpm test` 和 `pnpm lint`。
2. 部署与测试一致的 immutable commit。
3. 确认生产域名上的 `/api/health` 正常响应。
4. 确认 Slack Request URL 验证通过。
5. 发送一条已授权 Slack event，分别核验 QStash 接收、Multica Issue 创建、reaction author、Agent 执行和最终 Slack thread 回复。
6. 验证 blocked channel 与 blocked sender 不会产生 QStash 或 Multica 副作用。

HTTP 200、确认 reaction、Issue 创建或部署完成都只能证明对应阶段，不能单独证明整条 Agent 回复链路成功。

## 可选模型 footer

consumer 在准备消息时使用现有 Multica PAT 读取一次目标 Agent 配置，deadline 为两秒，并校验身份，不需要新增凭据。model 不可用、继承或为空时记为 `null`，footer 省略 model 部分；reply adapter 始终追加配置的自动化身份。footer 表示配置快照，不代表该次 run 实际使用的 model 或计费 tier，也不会修改 Agent model 设置。冻结快照和 text/Block Kit 渲染合同见 [Slack 上下文组装](CONTEXT-ASSEMBLY-DESIGN.md)。

## 本地 reply adapter

运行 `python3 scripts/slack-reply.py --help` 查看 CLI。把 adapter 绑定到私有 Runtime Skill，并提供包含 `displayName`、`agentId`、`workspaceId`、`projectId`、`teamId` 和 `serverUrl` 的私有 JSON。该配置不包含 token；adapter 沿用 Runtime 已有的 Multica CLI 认证与 `SLACK_USER_TOKEN`。Agent 通过 `--text-file` 提供正文；路由和 model metadata 从来源 Issue/comment 回读。adapter 用 Slack section blocks 发送正文，再追加 attribution context block，格式不依赖模型正文或 persona instructions。

最终回复 Skill 可在发送前用其只读脚本生成当前 run context，并按业务证据选择相关 PR。adapter 通过可选的 `--run-context-file` 和 `--github-context-file` 校验 Issue 归属、统计来源以及 GitHub repository/branch/PR URL 的一致性，再追加运行统计和 GitHub 关联行；没有可核验成果时省略对应行。Agent 不直接控制路由、delivery marker 或重试。

adapter 根据来源 Issue/comment 生成稳定 delivery block ID，并在配置 JSON 旁的 `.slack-reply-state/` 保存私有 delivery ledger。该目录已被 Git 忽略，目录和原子 JSON 使用 owner-only 权限；文件和父目录状态都在 POST 前完成 `fsync`。状态依次为 `attempting`、`accepted`、`sent`：Slack 返回的 message timestamp 将发送后验证收窄到新回复，只有 thread readback 成功才进入 `sent`。后续 Runtime 重试直接返回已持久化结果，不扫描旧线程历史。

如果 POST 结果未知，重试从本机尝试时间前五分钟开始查找，以容纳 clock skew。找到 delivery block 后进入 `sent`；未找到则返回 `slack_delivery_unknown`，绝不自动重复 POST，因为“查询为空”不能证明 Slack 没有提交。清理这条状态前必须在原线程独立核对。同一来源的并发执行无法取得本机锁时立即返回 `reply_delivery_busy`，不会阻塞等待。`SLACK_USER_TOKEN` 除 `chat:write` 外，还需要 `conversations.replies` 对应的 history scope。

footer appearance 每次发送时都从私有配置读取。`displayName` 是完整 attribution label，可以包含 Unicode emoji 或 Slack shortcode。修改后从下一次发送生效，无需重启 Runtime 或重新部署 Relay；已有消息不会改变。修改时必须保留其他路由和身份字段。

```json
{
  "displayName": "🤖 Example"
}
```

以上示例只展示 appearance 字段；真实配置必须保留其余必填字段。

## 最终回复与私有风格配置

现有最终回复 Skill 默认沿用 Slack Skill 发送。采用可选 adapter 时，将最终发送步骤配置为 `python3 <adapterPath> --config <private JSON> --issue-id <当前 Issue> --text-file <正文> --run-context-file <本次统计>`；follow-up 使用本次 `--comment-id`，有成果时提供 `--github-context-file`。以 `FINAL_REPLY_CONFIG` 指向私有 JSON，并按需让最终回复 Skill 读取下列配置。两种发送入口只选一种。个人风格和表情目录保存在私有配置，公共文件只描述接口。

```json
{
  "adapterPath": "/path/to/relay/scripts/slack-reply.py",
  "styleGuide": "/path/to/private/communication/SKILL.md",
  "emojiGuide": "/path/to/private/emoji/favorites.md",
  "emojiCatalog": "/path/to/private/emoji/catalog.json",
  "icons": {"time": "⏱️", "model": "🤖", "tools": "🔧", "skills": "🪄", "github": "🔗", "multica": "↗️"}
}
```

这些是附加字段，保留原 JSON 的 `displayName`、`agentId`、`workspaceId`、`projectId`、`teamId`、`serverUrl`。图标可使用当前 workspace 已确认存在的 shortcode。风格读取由最终回复 Skill 控制；adapter 只负责渲染和发送，不加载这些个人资料。

Agent 环境的 `FINAL_REPLY_APP_URL` 为 Multica 网页 HTTPS 根地址，`FINAL_REPLY_WORKSPACE_SLUG` 为工作区 slug。采集器支持等价的 `--app-url`、`--workspace-slug` 及 `--issue-identifier` 显式参数，优先于环境；未提供时只读补查，缺失则省略链接。隔离运行应显式配置环境值，不依赖宿主个人 CLI 配置。

`--run-context-file` 除统计外携带当前任务的编号与链接；adapter 核对 Issue 和当前 run 后在统计行末尾渲染任务入口。`--github-context-file` 使用 `version: 1`、`pullRequests` 数组及可选 `branches` 数组；分支项包含 `repository`（owner/repo）与 `branch`，PR 项另含 `number` 和对应的完整 GitHub `url`，合计最多五项。每项单独一个 context block，fallback 保留同样的信息。有效本轮统计不再叠加旧 envelope 的模型行，attribution 和 delivery marker 仍保留。

发布后回读 Skill 全部文件及 Agent 绑定；已有任务可能继续使用其启动时复制的旧 Skill，新任务才能证明加载。配置保存、只读 dry-run 和真实 Slack 发送分别验收。

## Cloudflare Workers

Worker 入口为 `src/cloudflare.ts`，配置为 `wrangler.jsonc`，提供与其他平台相同的三个 `/api/` 路由。它直接传递原始 Request 和 `env`，保留 Slack/QStash 签名字节，不使用后台 `waitUntil` 提前确认队列消费。`/api/health` 只确认入口可达，不检查外部配置。

Wrangler 本地开发和测试支持 Node.js 22.12+，推荐 Node.js 24 LTS；`.nvmrc` 和 `.node-version` 默认使用 24。Cloudflare Workers Builds 的 `NODE_VERSION` 设置为 `24`。Workers 执行环境是 workerd，Node.js 版本只控制构建工具，不改变 Worker 运行时。配置的 compatibility date 为 `2026-09-08`，默认启用 Node.js compatibility；共享代码显式导入 `node:buffer`，复用 `node:crypto`。参见 [Cloudflare Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/)。

1. 安装依赖后，运行 `pnpm exec wrangler login` 连接预期 Cloudflare 账号；将 `wrangler.jsonc` 中 `name` 改为自己的 Worker 名称。
2. 根据本文件 Relay 变量表，在 Cloudflare Worker 的 Variables and Secrets 中配置全部必填值。普通 ID/URL 在 Wrangler `vars` 中维护，避免后续 CLI 部署覆盖仅存在于控制台的普通变量；token、PAT 和 signing key 使用 Secret。也可通过 `pnpm exec wrangler secret put SLACK_SIGNING_SECRET` 交互输入，其他凭据同理，不要把值放在命令参数或 Git 中。
3. `RELAY_CONSUMER_URL` 填写实际公开地址，例如 `https://<worker>.<subdomain>.workers.dev/api/queue/consume`。QStash 必须能直接访问该地址，不能被交互登录、Cloudflare Access 或验证码拦截。
4. 本地开发可以将测试环境值写入已忽略的 `.dev.vars`，再运行 `pnpm dev:cf`。本地模式仍会调用所配置的外部服务，必须使用独立测试资源；无需凭据的自动测试使用虚构绑定且不调用 live 服务。
5. 运行 `pnpm test`、`pnpm lint` 和 `pnpm build:cf`。最后一项仅构建并验证部署包，不发布。
6. 配置完成后运行 `pnpm deploy:cf`。在 Slack 中设置公开 Events URL，再按上方部署验收逐阶段验证。生产域名变更必须同步 consumer URL。

Cloudflare 沿用现有 QStash 和 Upstash Redis，不使用 Cloudflare Queues、KV 或 D1。Python reply adapter 和持久 ledger 继续运行在私有 Multica Runtime。账号的 CPU 和 subrequest 限制仍须适配上下文读取与恢复路径；本地测试和 dry-run 不代表线上完整链路验收。
