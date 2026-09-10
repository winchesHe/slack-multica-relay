# Configuration

**English** | [简体中文](CONFIGURATION.md)

The Relay and the Multica Agent Runtime are separate trust domains. Relay variables belong to Vercel, EdgeOne, or Cloudflare Workers. Agent Runtime variables belong to the machine or Pod that executes the selected Multica Agent. Do not copy one environment wholesale into the other.

## Relay deployment environment

Configure these variables on the deployment platform. Credentials and tokens belong in its secret store. Slack IDs and service URLs are ordinary configuration values, although storing them as secrets is acceptable.

Slack IDs are comma-separated uppercase identifiers. `SLACK_TARGET_USER_IDS` and `SLACK_TARGET_SUBTEAM_IDS` may each be empty, but at least one must contain a target. Blocklists always take precedence over allowlists.

| Variable | Required | Value and source |
| --- | --- | --- |
| `SLACK_SIGNING_SECRET` | Yes | Slack App → Basic Information → App Credentials → Signing Secret. |
| `SLACK_TEAM_ID` | Yes | Slack Workspace ID, normally beginning with `T`. |
| `SLACK_TARGET_USER_IDS` | Conditional | User IDs whose explicit mention triggers the Relay. |
| `SLACK_TARGET_SUBTEAM_IDS` | Conditional | User Group IDs whose explicit mention triggers the Relay. |
| `SLACK_ALLOWED_CHANNEL_IDS` | Yes | Comma-separated Channel IDs. Use `all` only when every channel is intentionally allowed; missing or empty values prevent startup. |
| `SLACK_BLOCKED_CHANNEL_IDS` | No | Channel IDs that must never trigger, including when the allowlist is `all`. |
| `SLACK_ALLOWED_SENDER_IDS` | No | User IDs allowed to trigger. Missing or empty values mean `all`; an owner-only deployment should set the exact owner ID. |
| `SLACK_BLOCKED_SENDER_IDS` | No | User IDs that must never trigger. |
| `SLACK_BOT_TOKEN` | Conditional | Preferred identity for reaction add/read/remove; requires reactions:read and reactions:write. |
| `SLACK_USER_TOKEN` | Conditional | Used for reactions when Bot is not configured. |
| `SLACK_CANCEL_KEYWORDS` | No | Comma-separated whole-command words; defaults to cancel,取消. |
| `SLACK_CONTEXT_TOKEN` | Yes | User token with history scopes for the selected conversation types. The consumer uses it for bounded history/reply reads; `users:read` enables optional participant names through `users.info`. It may contain the same authorized token as the reaction credential, but configure it explicitly. |
| `SLACK_REACTION_NAME` | Yes | Slack emoji shortcode without colons, such as `eyes`. The reaction confirms Relay persistence, not Agent completion. |

The Slack App Request URL is `https://<deployment-host>/api/slack/events`. Subscribe only to message events required by the selected conversation types. Install the App in private channels when needed.

| Variable | Required | Value and source |
| --- | --- | --- |
| `MULTICA_API_BASE_URL` | Yes | HTTPS base URL of the target Multica Server, without a path suffix, query, or fragment. |
| `MULTICA_API_TOKEN` | Yes | Dedicated Multica PAT with access to the target Workspace, Project, and Agent. |
| `MULTICA_WORKSPACE_ID` | Yes | UUID of the Workspace that owns the Relay Project. |
| `MULTICA_PROJECT_ID` | Yes | UUID of the dedicated Project in which Relay Issues are created. |
| `MULTICA_AGENT_ID` | Yes | UUID of the Agent assigned to Relay Issues. The Agent must belong to the same Workspace and be bound to a Runtime. |

The Relay calls ordinary Issue and Comment APIs. It does not require a Multica Autopilot or webhook URL.

| Variable | Required | Value and source |
| --- | --- | --- |
| `KV_REST_API_URL` | Yes | HTTPS REST URL of a Redis database dedicated to this Relay. |
| `KV_REST_API_TOKEN` | Yes | REST token for the same Redis database. |
| `QSTASH_URL` | No | QStash API base URL. Defaults to `https://qstash.upstash.io`. |
| `QSTASH_TOKEN` | Yes | QStash publish token. |
| `QSTASH_CURRENT_SIGNING_KEY` | Yes | Current signing key used to verify QStash delivery. |
| `QSTASH_NEXT_SIGNING_KEY` | Yes | Next key used for verification during rotation. |
| `RELAY_CONSUMER_URL` | Yes | Exact public URL `https://<deployment-host>/api/queue/consume`. It must match the consumer URL used for QStash delivery and signature verification. |

The Redis database must be dedicated to this Relay. It stores thread mappings, locks, and write-confirmation state. QStash stores queued Slack payloads and retries failed consumers; configure its access and retention for the sensitivity of Slack content.

Redis also keeps scoped message-fingerprint indexes for 24 hours, up to 500 messages, to select follow-up context. These records describe persisted content rather than Agent Session memory. Prepared envelopes remain in Redis for 24 hours, including delivered copies until TTL expiry. A new mention refreshes the timeline; only retries of the same event reuse a frozen snapshot. Legacy background-cache keys are no longer read or written and expire under their existing TTL. Multica retains delivered envelopes under its own policy; Redis expiry does not delete them.

Before publishing to QStash, the Relay projects every Slack file object to `id`, `name`, `mime`, `size`, and `contentStatus`, plus a fingerprint derived from the complete safe fields. Downloaded content, private URLs, thumbnails, shares, and credentials never enter the queue payload. Transient or unknown-result failures return 503 for bounded QStash retry. An invalid queued payload, corrupt persisted state, scope violation, unresolvable mapping ambiguity, or deterministic size overflow returns `rejected` with `retryable: false`. Access failures and bounded-read omissions are represented by the context contract.

## Cancellation and reaction identity

A target mention followed by the complete `cancel` or `取消` keyword in the original task thread stops the associated runs. A non-empty `SLACK_CANCEL_KEYWORDS` list replaces those defaults. Only senders listed in `SLACK_TARGET_USER_IDS` may cancel; user-group mentions do not grant that permission. Admission is checked before enqueueing and again during consumption. Cancellation creates no Agent task and reads no context tree.

Redis persists cancellation intent, the fixed run-ID set, and remaining reaction cleanup with the thread mapping. Retries keep the original targets. Ordinary messages received during cancellation are ignored; later mentions can continue the same Issue. Terminal-state readback precedes cleanup of only the selected identity's own reactions. Multica's cancelled state and interruption request do not independently confirm daemon termination or undo completed external writes.

Reaction add/read/remove consistently select a non-empty `SLACK_BOT_TOKEN`, otherwise `SLACK_USER_TOKEN`. The chosen identity needs `reactions:read`, `reactions:write`, and channel access; API failure never switches identity. Context uses `SLACK_CONTEXT_TOKEN`, while the Agent reply uses its separate Runtime user token.

The old `SLACK_REACTION_TOKEN` and `SLACK_REACTION_READ_TOKEN` are no longer read. Configure the new variable for the existing identity before switching code; retain old variables for rollback if needed. Switching from User to Bot leaves historical User reactions intact. Drain or isolate queued `operation=cancel` events before rolling back, because an old consumer would dispatch them as ordinary tasks. Exhausted retries remain in QStash's DLQ; do not delete Redis state to force a resend.

## Multica Agent Runtime

The Relay does not read the variables below. Configure them only on the Runtime or Agent when Agent Instructions or the local Slack Skill require them.

| Variable | Purpose |
| --- | --- |
| `RELAY_OWNER_SLACK_USER_ID` | Owner ID used for authorization decisions. Derive it from the verified Slack event author, never from a display name. |
| `RELAY_SKILL_ROOT` | Absolute path to the approved local Skill directory. It is machine-specific and does not belong in this repository. |
| `SLACK_SKILL_ALLOWED_CHANNELS` | Channel allowlist enforced by the Slack Skill for reads and replies. Keep it aligned with Relay policy. |
| `SLACK_USER_TOKEN` | Owner user token used by the Agent Slack Skill and local reply adapter. |
| `SLACK_TOKEN` | Compatibility variable for Skills that require it. Point it explicitly at the intended owner identity. |
| `SLACK_BOT_TOKEN` | Leave empty when replies must use the owner identity; otherwise the Slack Skill may prefer the Bot token. |

The Agent does not need a separate `RELAY_ALLOWED_CHANNEL_ID`. The Relay validates Team, channel, sender, and mention policy before enqueueing and checks them again during consumption. `SLACK_SKILL_ALLOWED_CHANNELS` applies only to calls made through the Slack Skill. The local reply adapter is a separate path: it reads the destination from the source Issue/comment in the configured Multica Project and sends directly with `SLACK_USER_TOKEN`.

## Configuration ownership and activation

| Surface | Source of truth | How changes become active |
| --- | --- | --- |
| Relay admission, queueing, and persistence | Deployment variables listed above | Redeploy the tested commit with the updated environment, then read back health and policy behavior. |
| Reply display name and fixed routing identity | Private Runtime JSON passed to `scripts/slack-reply.py` | The adapter reads it on every invocation; a new `displayName` applies to the next send. |
| Agent personality and operating instructions | Multica Agent Instructions | A private local prompt may hold the candidate, but it must be synchronized explicitly with `multica agent update --instructions` and read back from the same Agent ID. Editing the file alone has no Runtime effect. |
| Slack and other local tools | Private Runtime Skills and environment | Update the Runtime binding/configuration and read back the bound Skill and environment. This repository does not load those files. |

## Deployment verification

1. Run `pnpm install --frozen-lockfile`, `pnpm test`, and `pnpm lint`.
2. Deploy the same immutable commit that passed validation.
3. Confirm `/api/health` on the production domain.
4. Confirm Slack accepts the Request URL.
5. Send one authorized Slack event and verify QStash acceptance, Multica Issue creation, reaction author, Agent execution, and the final Slack thread reply as separate stages.
6. Verify that a blocked channel and blocked sender produce no QStash or Multica side effect.

An HTTP 200, acknowledgement reaction, created Issue, or ready deployment proves only its corresponding stage. None proves the complete Agent reply path.

## Optional model footer

During preparation, the consumer uses the existing Multica PAT to read the selected Agent once, with a two-second deadline and identity validation. No new credential is required. An unavailable, inherited, or empty model becomes `null`, omitting only the model portion of the footer. The reply adapter always appends the configured automation identity. The footer is a configuration snapshot, not evidence of the model or billing tier used by the run, and it never changes Agent model settings. See [Slack Context Assembly](CONTEXT-ASSEMBLY-DESIGN_EN.md) for the frozen-snapshot and rendering contract.

## Local reply adapter

Run `python3 scripts/slack-reply.py --help` for the CLI. Bind the adapter in the private Runtime Skill and provide private JSON containing `displayName`, `agentId`, `workspaceId`, `projectId`, `teamId`, and `serverUrl`. The JSON contains no token. The adapter reuses the Runtime's Multica CLI authentication and `SLACK_USER_TOKEN`. The Agent supplies body text through `--text-file`; route and model metadata are read back from the source Issue/comment. The adapter sends body sections plus an attribution context block, independent of model text or persona instructions.

The final-reply Skill can use its read-only helper to create current-run context before sending and selects only GitHub PRs supported by the business task evidence. The adapter accepts optional `--run-context-file` and `--github-context-file` inputs, validates Issue ownership, statistic provenance, and repository/branch/PR URL consistency, then appends run statistics and GitHub association rows. It omits fields that cannot be verified. The Agent does not control routing, delivery markers, or retries.

The adapter derives a stable delivery block ID from the source Issue/comment and stores a private delivery ledger in `.slack-reply-state/` beside the JSON configuration. The directory is ignored by Git. Its directory and atomic JSON files use owner-only permissions; file and parent-directory state are both synced before the POST. State advances through `attempting`, `accepted`, and `sent`. Slack's returned message timestamp narrows post-send verification to the new reply, and only successful thread readback advances to `sent`. Later Runtime retries return the persisted result without scanning old history.

If the POST result is unknown, a retry searches from five minutes before the local attempt time to allow for clock skew. A found delivery block advances to `sent`. An empty read returns `slack_delivery_unknown` and never repeats the POST automatically, because absence does not prove Slack failed to commit. Independently verify the original thread before clearing that state. Concurrent execution for the same source returns `reply_delivery_busy` immediately rather than waiting on a blocking lock. In addition to `chat:write`, `SLACK_USER_TOKEN` needs the history scope required by `conversations.replies`.

Footer appearance is read from the private configuration on every send. `displayName` is the complete attribution label and may contain Unicode emoji or Slack shortcodes. A change applies to the next send without a Runtime restart or Relay redeploy; existing messages remain unchanged. Preserve every other routing and identity field.

```json
{
  "displayName": "🤖 Example"
}
```

The example shows only the appearance field. Preserve the remaining required fields in the real configuration.

## Final replies and private style configuration

The existing final-reply Skill keeps its Slack Skill sender by default. To adopt the optional adapter, configure its final send step to run `python3 <adapterPath> --config <private JSON> --issue-id <current Issue> --text-file <body> --run-context-file <current statistics>`, plus the current `--comment-id` for follow-ups and `--github-context-file` for verified results. Use one sender only. `FINAL_REPLY_CONFIG` can point to private JSON whose optional fields below the Skill may read; the adapter does not load personal style or emoji resources.

```json
{
  "adapterPath": "/path/to/relay/scripts/slack-reply.py",
  "styleGuide": "/path/to/private/communication/SKILL.md",
  "emojiGuide": "/path/to/private/emoji/favorites.md",
  "emojiCatalog": "/path/to/private/emoji/catalog.json",
  "icons": {"time": "⏱️", "model": "🤖", "tools": "🔧", "skills": "🪄", "github": "🔗", "multica": "↗️"}
}
```

Retain `displayName`, `agentId`, `workspaceId`, `projectId`, `teamId`, and `serverUrl`. Icons may use verified workspace shortcodes. The final-reply Skill controls style and catalog loading; the adapter only renders and sends messages.

Agent variables `FINAL_REPLY_APP_URL` and `FINAL_REPLY_WORKSPACE_SLUG` provide the Multica HTTPS web origin and workspace slug. Explicit `--app-url`, `--workspace-slug`, and `--issue-identifier` arguments take precedence; missing values use read-only discovery, and incomplete links are omitted. Isolated tasks should receive these values explicitly instead of relying on personal host CLI configuration.

`--run-context-file` carries the current Issue identifier/link alongside statistics. After checking Issue and current-run identity, the adapter appends the task link to the statistics line. `--github-context-file` uses `version: 1`, a `pullRequests` array, and an optional `branches` array. Branch entries have `repository` (owner/repo) and `branch`; PR entries also carry `number` and the corresponding full GitHub `url`, with at most five entries overall. Each entry has its own context block and equivalent fallback text. Valid current-run statistics replace the older envelope model display; attribution and the delivery marker remain.

Read back published Skill files and Agent bindings. Existing tasks may retain their startup copy; a new task proves loading. Configuration persistence, read-only dry-run, and real Slack delivery are separate validation stages.

## Cloudflare Workers

The Worker entry is `src/cloudflare.ts`, configured by `wrangler.jsonc`, with the same three `/api/` routes as the other platforms. It forwards the original Request and `env`, preserving Slack/QStash signature bytes, and does not acknowledge queue consumption early through `waitUntil`. `/api/health` checks endpoint availability, not external configuration.

Wrangler development and tests support Node.js 22.12+; Node.js 24 LTS is recommended and selected by `.nvmrc` and `.node-version`. Set `NODE_VERSION=24` in Cloudflare Workers Builds. Workers executes on workerd: this Node.js version controls build tools, not the Worker runtime. The configured compatibility date, `2026-09-08`, enables Node.js compatibility by default. Shared code explicitly imports `node:buffer` and reuses `node:crypto`. See [Cloudflare Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/).

1. Install dependencies, run `pnpm exec wrangler login` for the intended Cloudflare account, and set your Worker name in `wrangler.jsonc`.
2. Configure every required Relay variable listed above in the Worker's Variables and Secrets. Maintain ordinary IDs/URLs in Wrangler `vars` so later CLI deployments do not overwrite dashboard-only plain variables; tokens, PATs, and signing keys must use Secrets. Alternatively, enter credentials interactively with commands such as `pnpm exec wrangler secret put SLACK_SIGNING_SECRET`; never put values in command arguments or Git.
3. Set `RELAY_CONSUMER_URL` to the actual public endpoint, such as `https://<worker>.<subdomain>.workers.dev/api/queue/consume`. QStash must reach it directly without an interactive login, Cloudflare Access, or CAPTCHA.
4. For local development, put test values in the ignored `.dev.vars` file and run `pnpm dev:cf`. Local mode still calls configured external services, so use isolated test resources. Credential-free automated tests use fake bindings without live service calls.
5. Run `pnpm test`, `pnpm lint`, and `pnpm build:cf`. The last command builds and validates the deployment bundle without publishing.
6. After configuration, run `pnpm deploy:cf`. Configure the public Slack Events URL and perform the staged deployment verification above. Domain changes require updating the consumer URL.

Cloudflare retains QStash and Upstash Redis; it does not replace them with Cloudflare Queues, KV, or D1. The Python reply adapter and durable ledger stay on the private Multica Runtime. Account CPU and subrequest limits must accommodate context reads and recovery paths. Local tests and dry-run builds do not prove a live end-to-end deployment.
