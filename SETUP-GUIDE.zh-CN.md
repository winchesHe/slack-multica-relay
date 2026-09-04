# Slack 到 Multica 中转站搭建手册

这份手册用于让同事快速搭建一套 Slack mention 到 Multica Agent 的自动化链路。整套系统由 Slack App、Vercel 中转层和 Multica 三部分组成。

## 一、链路结构

```text
Slack mention
    ↓
Slack App Event Subscriptions
    ↓
Vercel Slack Relay
    ↓
Multica Autopilot Webhook
    ↓
Multica Agent
    ↓
对应 Skills
    ↓
任务卡和 Slack thread 回复
```

Vercel 负责接收事件、校验 Slack 签名、识别目标用户或团队、添加 reaction 和调用 Multica。Agent 负责读取上下文、调用 Skills、执行任务并回复原 Slack thread。

## 二、创建 Slack App

打开 Slack API。

```text
https://api.slack.com/apps
```

创建 Slack App 并安装到目标 Workspace。

### 配置 User Token Scopes

按实际功能配置 User Token Scopes。常用权限如下。

```text
channels:history
groups:history
im:history
mpim:history
chat:write
reactions:write
```

这些权限用于读取消息和 thread、以 User 身份回复以及添加 reaction。

新增权限后必须重新安装 Slack App，并使用重新生成的 User Token。旧 Token 不会自动获得新增权限。

### 配置 Event Subscriptions

打开 Event Subscriptions，启用事件订阅，并填写 Vercel Production 地址。

```text
https://你的域名.vercel.app/api/slack/events
```

订阅以下事件。

```text
message.channels
message.groups
message.im
message.mpim
```

完成 Slack URL verification 后保存配置。

### 保存 Slack 凭证

需要保存以下凭证。

```text
SLACK_SIGNING_SECRET
SLACK_USER_TOKEN
```

凭证只能通过 Secret 环境变量保存，不能提交到 Git 或发到 Slack。

## 三、部署 Vercel 中转层

可以直接复用本项目目录。

```text
slack-multica-relay
```

在 Vercel 创建项目，Root Directory 指向该目录，并配置以下 Production 环境变量。

```text
SLACK_SIGNING_SECRET=Slack Signing Secret
SLACK_TARGET_USER_IDS=目标Slack用户ID
SLACK_TARGET_SUBTEAM_IDS=目标Slack团队ID
MULTICA_WEBHOOK_URL=Multica Autopilot Webhook地址
SLACK_REACTION_TOKEN=用于添加reaction的Slack User Token
SLACK_REACTION_NAME=reaction名称
```

多个用户或团队 ID 用逗号分隔。当前方案不需要 `SLACK_ALLOWED_CHANNEL_IDS`。

部署后检查健康地址。

```text
https://你的域名.vercel.app/api/health
```

预期返回。

```json
{"ok":true,"service":"slack-multica-relay"}
```

修改环境变量后需要重新部署。Slack Event Subscription 必须使用 Production 地址。

### 开启同一 thread 复用同一张任务卡

如果希望同一个 Slack thread 的后续提问进入同一张 Multica 卡片，需要额外配置：

```text
SLACK_THREAD_ROUTING_ENABLED=true
MULTICA_API_BASE_URL=https://multica.devops.moego.dev
MULTICA_API_TOKEN=Multica Personal Access Token
MULTICA_WORKSPACE_ID=Multica 工作区 ID
KV_REST_API_URL=Upstash/Vercel KV REST URL
KV_REST_API_TOKEN=Upstash/Vercel KV REST Token
```

`MULTICA_API_TOKEN` 只放在 Vercel Secret 中，不要使用 Slack User Token 替代。KV 保存
`teamId:channelId:threadTs → issueId` 映射、消息幂等记录和线程锁；没有共享 KV 时，多个
Vercel 实例可能同时建卡。

## 四、配置 Multica Runtime 和 Agent

### 选择 Runtime

选择一个长期在线的 Codex Runtime。团队共享时建议使用统一维护的云端 Runtime，个人测试可以使用自己的 Codex Runtime。

Runtime 必须在线，并且 Agent owner 有权限绑定。

### 创建 Agent

建议名称。

```text
Slack Task Router
```

Agent Prompt 至少包含以下规则。

```text
你负责处理由 Slack relay 发送的 Slack mention 任务。

eventPayload.text 是用户原始任务。
eventPayload.channelId 是回复频道。
eventPayload.threadTs 是原始 thread。
eventPayload.messageTs 是触发消息。
eventPayload.senderUserId 是发起人。
eventPayload.mentionType 和 eventPayload.mentionId 用于识别个人或团队 mention。

先理解原始任务，再选择对应 Skill。
最终结果必须回复到原 Slack thread。
Slack 回复必须使用 User actor，也就是 --user，或当前 Skill 中等价的 --as user。
禁止使用 --bot 或 --as bot。
禁止自动合并 PR、发布、部署或推送默认分支。

如果任务明确要求 Review PR，发现问题时必须直接在准确的 changed line 上提交 GitHub inline comment。
每个独立问题单独评论，写入后回读确认。
Slack 频道只回复简洁结论、问题数量和 Review comment 状态。
没有问题时直接在 PR 上提交 Approve，再在频道回复简洁的通过结论。发现问题、证据不足或结论不确定时不得 Approve。
```

### 绑定 Skills

按团队需要绑定常用 Skills。

```text
slack
github-workflow
review-brief
gather-moego-context
jira
lark-skills
moe-opc
moe-stack
datadog
sentry
moe-grey
moe-mis
growthbook
```

Skill 更新后要确认 Agent 使用的是最新版本。

### 配置 Agent 环境变量

Slack 相关变量放在 Agent 的 Secret 环境变量中。

```text
SLACK_USER_TOKEN=Slack User Token
SLACK_BOT_TOKEN=可选的备用 Bot Token
SLACK_READ_ACTOR=auto
SLACK_WRITE_ACTOR=auto
```

Slack Skill 应优先使用 User 身份写入，Bot 只作为备用路径。其它 Skill 所需的 Jira、GitHub、Datadog、Sentry、GrowthBook、飞书和 MoeMIS 凭证，也要按各自 Skill 文档补齐。

## 五、创建 Autopilot

创建 Webhook 类型的 Autopilot，并把 Agent 设置为 Slack Task Router。

建议创建专用项目。

```text
Slack Task Router 任务看板
```

Autopilot 使用以下设置。

```text
Execution Mode
create_issue

Project
Slack Task Router 任务看板

Issue Title Template
Slack mention 自动任务 · {{date}}
```

`create_issue` 会让每条 Slack mention 生成一张长期保留的任务卡，任务卡中可以查看状态、执行过程、结果和失败原因。

将 Autopilot Webhook 地址填入 Vercel 的 `MULTICA_WEBHOOK_URL`。

## 六、测试链路

在目标 Slack 频道发送测试消息。

```text
@目标用户 测试 Slack mention 路由
```

依次确认以下结果。

1. Slack 消息出现配置好的 reaction。
2. Vercel 日志显示事件校验成功。
3. Multica 任务看板创建任务卡。
4. Agent 开始执行。
5. Slack 原 thread 收到 User 身份回复。

测试 PR Review 时附上 PR 链接，并确认问题直接出现在 PR 的 inline comment 中，Slack 频道只收到简洁结论。

## 七、并发和幂等

首条消息使用 `teamId:channelId:messageTs` 生成唯一幂等键，线程映射使用
`teamId:channelId:threadTs`。同一 thread 的后续消息写入同一 issue 的评论，由 Multica
原生评论路由继续唤醒 Agent。两个人同时回复时，线程锁会串行处理，消息幂等键和评论中的
隐藏 marker 会避免 Slack 重试产生重复评论。

如果第一条 issue 还在异步创建，后续消息会暂存在该 thread 的 KV 状态中，拿到 issue ID
后再按 Slack 时间戳顺序补写评论；因此不会因为并发到达而丢失上下文。

当前 Agent 并发上限建议设置为 3。超过并发上限时任务会排队等待，不会因为排队直接丢失。

如果希望任务出现在项目任务看板，Autopilot 必须使用 `create_issue`。`run_only` 只执行任务，不创建长期任务卡。

## 八、常见问题

### 没有 reaction

检查 Slack App 是否重新安装、User Token 是否有 `reactions:write`、Vercel 是否使用了新 Token，以及 Production 是否重新部署。

### 没有生成任务卡

检查 mention 的用户或团队 ID、Vercel Webhook 地址、Autopilot 状态和项目绑定。确认 Execution Mode 是 `create_issue`。

### 任务一直排队

检查 Agent 绑定的 Runtime 是否在线。任务显示 queued 且没有 started_at 时，通常是 Runtime 不可用或 Agent 绑定到了错误的本地 Runtime。

### PR 没有 inline comment

检查 GitHub 写权限、PR 链接和 changed line 是否可以定位。写入失败时 Agent 必须报告真实阻断原因，不能声称评论已经提交。

### 任务卡看不到原始聊天内容

打开任务详情，在描述中的 `Webhook payload` 查看原始触发消息。任务列表只显示标题和状态，Autopilot 运行列表不会直接展示完整 payload。

## 九、安全要求

- 所有 Token、Signing Secret 和 Webhook URL 都使用 Secret 环境变量保存。
- 不要把凭证提交到 Git、写入 Slack 或放入任务输出。
- 曾经在聊天中直接粘贴过的 Token 应立即轮换。
- 使用最小权限配置 Slack App 和各类 Skill。
- 定期检查 Vercel 日志，确认没有打印认证信息。
- 成员权限变化后及时轮换相关凭证。
