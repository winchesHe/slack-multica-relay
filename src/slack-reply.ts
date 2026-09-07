import type {
  MulticaTaskMessage,
  MulticaTaskRun,
  MulticaTaskUsage,
} from "./multica-api.js";

const SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage";
const SLACK_REPLIES_URL = "https://slack.com/api/conversations.replies";
const MAX_FALLBACK_BODY_LENGTH = 36_000;
const MAX_SECTION_LENGTH = 2_900;
const SLACK_BROADCAST_PATTERN =
  /<!(?:channel|here|everyone)(?:\|[^>]*)?>|<!subteam\^[^>|]+(?:\|[^>]*)?>/gu;

interface SlackMessageMetadata {
  event_type?: unknown;
  event_payload?: unknown;
}

interface SlackMessage {
  ts?: unknown;
  metadata?: SlackMessageMetadata;
}

export interface SlackRunReplyPayload {
  channel: string;
  thread_ts: string;
  text: string;
  blocks: unknown[];
  metadata: {
    event_type: "multica_run_reply";
    event_payload: { task_id: string; issue_id: string };
  };
}

export function buildRunStatsFooter(
  task: MulticaTaskRun,
  messages?: MulticaTaskMessage[],
): string | undefined {
  if (!task.usage?.length || !task.started_at || !task.completed_at)
    return;
  const usage = aggregateUsage(task.usage);
  const totalTokens =
    usage.inputTokens + usage.outputTokens + usage.cacheReadTokens;
  if (totalTokens <= 0) return;

  const durationMs =
    Date.parse(task.completed_at) - Date.parse(task.started_at);
  if (!Number.isFinite(durationMs) || durationMs < 0) return;

  const model = usage.models.length
    ? usage.models.map(escapeSlackText).join("+")
    : "unknown-model";
  let tokenSegment = `${model}: ${formatTokenCount(totalTokens)} tokens`;
  const cachedDenominator = usage.inputTokens + usage.cacheReadTokens;
  if (usage.cacheReadTokens > 0 && cachedDenominator > 0) {
    const cachedRate = Math.round(
      (usage.cacheReadTokens / cachedDenominator) * 100,
    );
    tokenSegment += ` (${cachedRate}% cached)`;
  }

  const segments = [`⏱ ${formatDuration(durationMs)}`, tokenSegment];
  if (messages) {
    const toolCount = messages.filter((message) => message.type === "tool_use")
      .length;
    segments.push(`${toolCount} tools`);
  }
  return segments.join(" · ");
}

export function buildSlackRunReplyPayload(input: {
  channelId: string;
  threadTs: string;
  issueId: string;
  taskId: string;
  output: string;
  footer: string;
}): SlackRunReplyPayload {
  const body = truncateBody(
    ensureAssistantPrefix(neutralizeSlackBroadcasts(input.output.trim())),
  );
  const text = `${body}\n\n${input.footer}`;
  const blocks: unknown[] = splitMrkdwn(body).map((chunk) => ({
    type: "section",
    text: { type: "mrkdwn", text: chunk, verbatim: true },
  }));
  blocks.push({
    type: "context",
    elements: [{ type: "mrkdwn", text: input.footer, verbatim: true }],
  });
  return {
    channel: input.channelId,
    thread_ts: input.threadTs,
    text,
    blocks,
    metadata: {
      event_type: "multica_run_reply",
      event_payload: { task_id: input.taskId, issue_id: input.issueId },
    },
  };
}

export async function findSlackRunReply(
  token: string,
  channelId: string,
  threadTs: string,
  taskId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<string | undefined> {
  let cursor = "";
  for (let page = 0; page < 10; page++) {
    const query = new URLSearchParams({
      channel: channelId,
      ts: threadTs,
      limit: "100",
      include_all_metadata: "true",
    });
    if (cursor) query.set("cursor", cursor);
    const response = await fetchImpl(`${SLACK_REPLIES_URL}?${query}`, {
      headers: { authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) throw new Error("slack_reply_lookup_failed");
    const body = (await response.json()) as {
      ok?: unknown;
      messages?: unknown;
      response_metadata?: { next_cursor?: unknown };
    };
    if (body.ok !== true || !Array.isArray(body.messages))
      throw new Error("slack_reply_lookup_failed");
    const match = (body.messages as SlackMessage[]).find((message) => {
      const payload = message.metadata?.event_payload;
      return (
        message.metadata?.event_type === "multica_run_reply" &&
        !!payload &&
        typeof payload === "object" &&
        !Array.isArray(payload) &&
        (payload as Record<string, unknown>).task_id === taskId &&
        typeof message.ts === "string"
      );
    });
    if (match && typeof match.ts === "string") return match.ts;
    const next = body.response_metadata?.next_cursor;
    if (typeof next !== "string" || !next) return;
    cursor = next;
  }
  throw new Error("slack_reply_lookup_limit");
}

export async function postSlackRunReply(
  token: string,
  payload: SlackRunReplyPayload,
  fetchImpl: typeof fetch = fetch,
): Promise<string> {
  const response = await fetchImpl(SLACK_POST_MESSAGE_URL, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json; charset=utf-8",
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(8_000),
  });
  if (!response.ok) throw new Error("slack_reply_post_failed");
  const body = (await response.json()) as { ok?: unknown; ts?: unknown };
  if (body.ok !== true || typeof body.ts !== "string")
    throw new Error("slack_reply_post_failed");
  return body.ts;
}

function aggregateUsage(rows: MulticaTaskUsage[]): {
  models: string[];
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
} {
  const models = new Set<string>();
  let inputTokens = 0;
  let outputTokens = 0;
  let cacheReadTokens = 0;
  for (const row of rows) {
    if (row.model.trim()) models.add(row.model.trim());
    inputTokens += row.input_tokens;
    outputTokens += row.output_tokens;
    cacheReadTokens += row.cache_read_tokens;
  }
  return { models: [...models], inputTokens, outputTokens, cacheReadTokens };
}

function formatTokenCount(tokens: number): string {
  if (tokens < 1_000) return String(Math.round(tokens));
  return `${(tokens / 1_000).toFixed(1)}k`;
}

function formatDuration(durationMs: number): string {
  const totalSeconds = durationMs / 1_000;
  if (totalSeconds < 60) {
    const roundedTenths = Math.round(totalSeconds * 10);
    if (roundedTenths < 600) return `${roundedTenths / 10}s`;
  }
  const roundedSeconds = Math.round(totalSeconds);
  const minutes = Math.floor(roundedSeconds / 60);
  const seconds = roundedSeconds % 60;
  return `${minutes}m ${seconds}s`;
}

function neutralizeSlackBroadcasts(body: string): string {
  return body.replace(SLACK_BROADCAST_PATTERN, (value) =>
    value.replace(/^</u, "&lt;"),
  );
}

function ensureAssistantPrefix(body: string): string {
  if (body.startsWith("🤖 自动化助手")) return body;
  return `🤖 自动化助手\n\n${body}`;
}

function truncateBody(body: string): string {
  if (body.length <= MAX_FALLBACK_BODY_LENGTH) return body;
  return `${body.slice(0, MAX_FALLBACK_BODY_LENGTH - 18)}\n\n…内容已截断`;
}

function splitMrkdwn(text: string): string[] {
  const chunks: string[] = [];
  let rest = text;
  while (rest.length > MAX_SECTION_LENGTH) {
    const candidate = rest.slice(0, MAX_SECTION_LENGTH);
    const newline = candidate.lastIndexOf("\n");
    const cut = newline > MAX_SECTION_LENGTH / 2 ? newline : MAX_SECTION_LENGTH;
    chunks.push(rest.slice(0, cut));
    rest = rest.slice(cut).replace(/^\n/u, "");
  }
  if (rest) chunks.push(rest);
  return chunks.length ? chunks : ["🤖 自动化助手"];
}

function escapeSlackText(value: string): string {
  return value
    .replace(/&/gu, "&amp;")
    .replace(/</gu, "&lt;")
    .replace(/>/gu, "&gt;");
}
