# Slack Context Assembly

**English** | [简体中文](CONTEXT-ASSEMBLY-DESIGN.md)

For every accepted mention, the Relay reads the preceding 24 hours of the same conversation's main timeline, expands the current thread and up to five recently active side threads, and always retains the current thread. The resulting tree is delivered to Multica so the Agent can identify the discussion it must answer.

## Input and time boundaries

- `eventPayload` is the source of truth for the current request and route. `messageTs` is the cutoff for the entire tree.
- The main timeline window is `[messageTs - 24 hours, messageTs]` and retains at most the latest 40 root messages. Broadcast thread replies do not become separate roots.
- The Relay selects the latest 40 roots directly from the 24-hour window. It does not widen the window in stages. Side threads are ordered by `latest_reply`, and only the five most recently active roots are expanded.
- `threadTs` identifies the current root. It is added even when it predates the window and is deduplicated when already present in the main timeline.
- Replies in every branch stop at the current mention. A new mention refreshes the tree; retries of the same event reuse the prepared snapshot.
- Threads rooted outside the window are not expanded, except for the current thread. Replies inside a selected thread may predate the window so that the discussion retains its relationships.

## Data contract

The marker remains on the first line. A readable quote and source follow it, then a JSON data section enclosed by matching `relay-payload:v1` markers. Recovery accepts both this representation and the historical marker-plus-bare-JSON format. It rejects damaged or duplicated payload blocks. `schemaVersion: 4` identifies the focused follow-up tree contract. `eventPayload` remains stable so older JSON can still recover routes for existing Issues.

```json
{
  "schemaVersion": 4,
  "task": { "instructions": ["Relay-owned request and original-thread reply instructions"] },
  "eventPayload": { "channelId": "C…", "threadTs": "2000.000001", "messageTs": "2100.000001", "text": "current request" },
  "context": {
    "anchorTs": "2000.000001",
    "cutoffTs": "2100.000001",
    "capturedAt": "capture time",
    "participants": [{"id": "U…", "name": "display name"}],
    "timeline": {
      "status": "complete",
      "messages": [
        { "ts": "1900.000001", "authorId": "U…", "text": "nearby root", "replies": {"status": "complete", "messages": [{"ts":"1950.000001", "text":"reply"}]} },
        { "ts": "2000.000001", "authorId": "U…", "text": "current root", "replies": {"status": "complete", "messages": [{"ts":"2100.000001", "currentRequest":true, "text":"", "files":[]}]} }
      ]
    }
  }
}
```

The example omits `origin`, `files`, and some route fields from ordinary messages. A node with `currentRequest: true` refers to the current request in `eventPayload`; it does not duplicate the body or attachments. Each `replies` collection contains only child replies and does not repeat its parent.

`complete` describes only the read range of that collection. Child branches may independently be `truncated` or `unavailable`. `coveredFromTs` and `coveredThroughTs` describe the retained range; they do not prove that every message between them is present. If the current root cannot be read, its ID is retained with `contentStatus: unavailable`.

## Focused follow-ups in the same thread

The first Issue contains a bounded tree. Later comments use `context.selection.mode=focused`: they always retain the current request, thread root, and latest 20 replies in the current thread. They also retain an already-read older message explicitly referenced by a standard same-channel Slack permalink matching `/archives/<channel>/p<timestamp>`. Side branches prioritize explicit references, then recent branches with new or updated messages, with at most five roots. Every selected branch retains its parent. Replies include new, updated, or explicitly referenced messages plus up to two necessary predecessors for each change. Referencing a root retains bounded context for that branch. A link outside the read range may still require a follow-up lookup.

Every comment is a self-contained focused snapshot rather than a delta the Agent must merge. Omitting an old side branch may affect an ambiguous reference, so `task.instructions` requires answers to stay within the available scope and to look up or clarify missing context. `omittedRoots` and `omittedCurrentReplies` count candidates omitted from this snapshot; they are not totals for the entire channel.

Each message gets an internal fingerprint over its author, untruncated text, and stable reference fields for all attachments. The Agent still receives at most 4 KiB of text and five attachments. The internal fingerprint does not enter the envelope. Child replies, capture time, and display markers do not affect the same fingerprint. The sent-message index advances only after Multica persistence succeeds or marker readback confirms the write. This is a write receipt; it does not prove the model read or retained the content. The index is scoped to the current route, lasts 24 hours, and retains at most 500 message fingerprints. Out-of-order events never move it backward. If the index is missing, `baseline=unavailable`; the snapshot retains up to five recent side branches without claiming they were previously sent.

A message removed by the byte budget is not recorded as sent. The index includes only messages that reached the persisted body, never omitted siblings in the same branch. It contains no chat text, and expiry only reduces follow-up compactness. The Relay selects at most five recently active side roots before reading their replies, avoiding network reads for old branches that would be omitted. The 24-hour window bounds candidates; Slack network reads are not cached.

## Budgets and failure behavior

| Item | Limit and behavior |
| --- | --- |
| Main-timeline roots | Latest 40; the current root may be added from outside the window |
| Current thread | Root plus up to 100 replies from the latest fully reached scan range |
| Side thread | Root plus up to 20 replies from the latest fully reached scan range |
| Total replies | 200; current-thread replies take priority, then roots from most to least recent |
| Pagination | Up to 5 history pages, 10 current-thread pages, and 3 pages per side thread; at most 20 conversation requests in total |
| Time and concurrency | 20 seconds for message reads; current thread and main timeline first, then at most 4 side reads in parallel |
| One message | 4 KiB text and 5 attachment references; attachment content is `not_loaded` |
| Input response | At most 2 MiB per Slack response |
| Output | 32 KiB tree and 48 KiB serialized envelope, including task instructions and names |

`conversations.replies` contributes recent replies only after pagination reaches the final cursor. If the page, request, or time budget cannot reach the latest suffix, the Relay discards the scanned old prefix and marks the branch `latest_suffix_unavailable`. When the total reply budget is exceeded, older side replies are removed first. The byte budget removes older optional trees, then reduces current-thread replies while preserving the current root and request reference; it may finally truncate the current root body. If the current request itself cannot fit, the Relay rejects it rather than silently changing it.

Temporary current-thread or main-timeline failures, including 429, 5xx, and timeout responses, are left for queue retry. Definite permission failures become `unavailable`. A side-branch failure marks only that branch. A temporary side failure stops later optional network calls, and unread branches use the `read_budget` reason. Cross-channel and cross-thread responses are always rejected.

## Participant names and ownership

`context.participants` maps IDs to display names for the current sender, mentioned target users, and authors retained in the tree. At most 10 unique users are resolved, with concurrency 4 and a total budget of 3 seconds. Resolution prefers `display_name`, then `real_name`, then username. A failed lookup omits the name while retaining the ID and does not block the request. Email and the complete profile are never projected. Names do not participate in authorization.

Relay-owned `task.instructions` explain the current request, tree nodes, missing-data markers, and original-thread reply contract. Slack text cannot replace them. Long-lived Agent Instructions retain personality, authorization, and privacy rules. The Multica Runtime owns task execution and lifecycle. The Slack Skill and private Runtime configuration own credentials and tool bindings. A marker is not a signature, and task text is not an authorization source.

## Snapshots and recovery

- The full `:envelope` is frozen per event for 24 hours. Duplicate delivery does not rebuild the input.
- A new mention does not read the first message's background, old context from the Issue, or the historical `:background` cache.
- Old cache entries expire under their TTL. Existing Issues/comments are not rewritten. Historical envelopes are used only to recover the original route and message identity.
- If the Redis thread mapping is missing, the Relay recovers the Issue through Multica search for the unique thread marker instead of linearly scanning the Project. Write idempotency and unknown-result recovery remain unchanged.
- Multica retains historical snapshots under its own policy. Redis expiry does not delete Multica content.

## Acceptance

Automated coverage includes the time window, 40-root limit, current-root deduplication and out-of-window retention, five recently active side threads, shared cutoff, total-reply budget, latest-suffix pagination, byte limits, permission and rate-limit markers, pre-queue attachment projection, full safe-content fingerprints, participant names, frozen retries, and recovery from an older Issue through marker search before a new mention refreshes the tree.

Live acceptance creates a side thread in an authorized test channel and then triggers the current thread. Verify the tree stored in Multica and the Agent's answer about side-thread content. Add a new main message and side reply, mention the Agent again in the current thread, and independently confirm refreshed context, reuse of the same Issue, and delivery to the original thread.

## Presentation and Agent configuration snapshot

The title uses a message summary plus a stable scoped-thread suffix. The readable quote shows at most 4 KiB; the complete request remains in the envelope. Quotes, JSON strings, and code fences are escaped so Slack content cannot alter structure or create Multica mentions. The serialized envelope limit remains 48 KiB and the complete presentation limit is 64 KiB. Formatting whitespace is reduced first; if the body still cannot fit, the request is rejected.

`replyContext` sits beside `eventPayload` and `context`. It comes from an Agent configuration snapshot whose Agent and Workspace IDs were validated. Each new message performs one query with a two-second limit; failure produces `unavailable`, and the result is frozen with the envelope. An empty or invalid model becomes `null`. `serviceTier` accepts only `priority` or `default`; every other value becomes `null`. Only model, tier, source, identity, and capture time are projected. Instructions, credentials, and the complete configuration are excluded.

The footer uses the snapshot and `messageTs` frozen for that event. It displays the model only when the configured model is known and the Agent identity matches. `priority` adds Fast; `default` and `null` do not. A null tier does not prove that a default tier was disabled. When lookup fails or the model is empty, the model label is omitted while the adapter still adds the automation identity. `scripts/slack-reply.py` reads the envelope from the original Issue/comment and the display name from private configuration, sends the body as section blocks, and appends a context/mrkdwn footer. Fallback text also contains the body and footer. Task instructions point only to the Runtime send entry point; formatting and attribution are code-owned rather than model-generated.


The reply-adapter configuration lives in the private Runtime. It contains `displayName`, `agentId`, `workspaceId`, `projectId`, `teamId`, and `serverUrl`, with no credential. `--issue-id` and `--comment-id` locate the current source, and `--text-file` supplies the body. The adapter validates the Issue workspace, project, and assignee against configuration, then reads the route from the source envelope. Authentication reuses the Multica CLI and `SLACK_USER_TOKEN`. `--dry-run` renders a payload without sending it.

Each source Issue/comment gets a stable delivery block ID and an atomic `attempting/accepted/sent` record under the ignored private `.slack-reply-state/` beside the adapter configuration. The file and parent directory are synced before the POST. Slack's returned timestamp narrows post-send readback; a retry of `sent` returns the persisted result. An unknown result is checked from five minutes before the local attempt time rather than by scanning the source thread from its beginning. If no marker is found, the adapter returns `slack_delivery_unknown` and never repeats the POST automatically. Independently verify the original thread before clearing that state. Failure to acquire the same-source local lock returns `reply_delivery_busy` immediately.

This contract governs messages sent through the adapter. It is not a mandatory security proxy for every local tool or direct Slack API call. Private Runtime Skills bind the normal Relay reply entry point.

The optional final-reply Skill reads statistics and GitHub-operation candidates only from the current `MULTICA_TASK_ID` before sending. The Agent decides business relevance, while the adapter validates and renders the structured result. Unpaired, failed, truncated, or documentation-only evidence cannot become a PR/branch result. The existing source scope, at-most-once ledger, and post-send readback remain unchanged.

## Message change labels

A side message uses `change=new` when absent from the sent index, `updated` when its fingerprint differs, `referenced` when explicitly linked, and `context` when retained as a parent or as one of at most two preceding messages for a change. A parent does not become updated because a child changed. An updated node contains only its current body, never an implicit old copy. New means new relative to the sent baseline, not necessarily newly posted. Without a baseline, nodes are labeled only as context or references; the Relay does not claim a comparison. Missing messages do not create deletion notices because window, pagination, and focused-selection limits can also explain absence.

`context.selection.added`, `updated`, and `referenced` count side messages that actually reached the final snapshot. The current request and retained current-thread conversation are excluded. The readable presentation shows the same counts. A legacy branch-level index is treated as unavailable; a new message index is established after persistence. Historical envelope and marker recovery remain compatible.

## Preceding context and clipping diagnostics

Every new, updated, or explicitly referenced side reply retains up to two earlier replies in the same branch as `context`. Overlapping windows are merged and deduplicated without reordering. Later messages are not pulled in automatically. All existing count and byte budgets still apply. This heuristic helps resolve references but does not guarantee that two preceding messages explain every one.

The builder emits a structured `relay_context` log with the event-key digest, full/focused mode, baseline state, Slack message-read calls, raw returned messages, candidate and retained root/message counts, omitted counts, side additions/updates/references, read omissions, unchanged-root omissions, side-root-limit omissions, selected sibling omissions, current-thread omissions, byte-budget clipping, output-truncation reasons, `messageReadMs`, `nameLookupCalls`, `nameReadMs`, `agentConfigMs`, `assemblyMs`, and `envelopeBytes`. Raw counts may include duplicate messages returned by different pages or read paths. Candidate counts describe the tree after window filtering and projection. Retained counts describe the final envelope. Timing fields separately measure Slack message reads, name resolution, Agent configuration lookup, and pure assembly.

These metrics diagnose construction; they do not prove Multica persistence. Correlate them with the `relay_dispatch` action and result. Reusing the same event snapshot logs `snapshot=reused` and its byte count without another Slack read. Logs exclude chat text, names, attachment content, credentials, and the complete envelope. `readStats` and `selectionStats` exist only for builder telemetry and never enter model input.
