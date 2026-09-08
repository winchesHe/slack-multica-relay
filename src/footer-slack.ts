import type { FooterConfig } from "./footer-config.js";
import { isRecord, type ReplyRef } from "./footer-data.js";
import { digest } from "./thread-router.js";

interface SlackMessage {
  ts: string;
  thread_ts?: string;
  user: string;
  bot_id?: string;
  text: string;
  blocks?: Record<string, unknown>[];
  attachments?: unknown[];
}

async function slack(
  config: FooterConfig,
  method: string,
  params: Record<string, unknown>,
  fetchImpl: typeof fetch,
  token = config.slackReplyToken,
) {
  const url = new URL(`https://slack.com/api/${method}`);
  const readThread = method === "conversations.replies";
  // conversations.replies 从查询参数读取目标，不接受 JSON POST 中的 channel/ts。
  if (readThread) {
    for (const [key, value] of Object.entries(params))
      url.searchParams.set(key, String(value));
  }
  const response = await fetchImpl(url.toString(), {
    method: readThread ? "GET" : "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json; charset=utf-8",
    },
    ...(readThread ? {} : { body: JSON.stringify(params) }),
    redirect: "error",
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error("footer_slack_unavailable");
  const body: unknown = await response.json();
  if (!isRecord(body) || body.ok !== true)
    throw new Error("footer_slack_unavailable");
  return body;
}

export async function readOwnReply(
  config: FooterConfig,
  ref: ReplyRef,
  fetchImpl: typeof fetch,
): Promise<SlackMessage> {
  const identity = await slack(config, "auth.test", {}, fetchImpl);
  if (
    identity.team_id !== config.teamId ||
    typeof identity.user_id !== "string" ||
    (config.slackReplyActor === "bot") !==
      (typeof identity.bot_id === "string" && !!identity.bot_id)
  )
    throw new Error("footer_author_mismatch");
  if (config.slackReadToken !== config.slackReplyToken) {
    const reader = await slack(
      config,
      "auth.test",
      {},
      fetchImpl,
      config.slackReadToken,
    );
    if (reader.team_id !== config.teamId)
      throw new Error("footer_author_mismatch");
  }
  const body = await slack(
    config,
    "conversations.replies",
    {
      channel: ref.channelId,
      ts: ref.threadTs,
      oldest: ref.messageTs,
      latest: ref.messageTs,
      inclusive: true,
      limit: 2,
    },
    fetchImpl,
    config.slackReadToken,
  );
  if (!Array.isArray(body.messages))
    throw new Error("footer_slack_unavailable");
  const message = body.messages.find(
    (m) => isRecord(m) && m.ts === ref.messageTs,
  );
  if (!isRecord(message)) throw new Error("footer_message_missing");
  if (
    message.user !== identity.user_id ||
    (config.slackReplyActor === "bot" && message.bot_id !== identity.bot_id) ||
    message.thread_ts !== ref.threadTs ||
    message.ts === ref.threadTs
  )
    throw new Error("footer_author_mismatch");
  if (
    typeof message.text !== "string" ||
    (message.blocks !== undefined &&
      (!Array.isArray(message.blocks) || !message.blocks.every(isRecord))) ||
    (message.attachments !== undefined && !Array.isArray(message.attachments))
  )
    throw new Error("footer_message_invalid");
  return message as unknown as SlackMessage;
}

export function footerBlockId(taskId: string): string {
  return `relay_footer_${digest(taskId).slice(0, 24)}_v1`;
}

function originalBody(message: SlackMessage, taskId: string) {
  const blockId = footerBlockId(taskId);
  const blocks = [...(message.blocks ?? [])];
  const footers = blocks.filter((block) => block.block_id === blockId);
  let text = message.text;
  if (footers.length) {
    const block = footers[0]!;
    const elements = block.elements;
    if (
      footers.length !== 1 ||
      blocks.at(-1) !== block ||
      !Array.isArray(elements) ||
      elements.length !== 1 ||
      !isRecord(elements[0]) ||
      typeof elements[0].text !== "string" ||
      !text.endsWith(`\n\n${elements[0].text}`)
    )
      throw new Error("footer_body_changed");
    text = text.slice(0, -`\n\n${elements[0].text}`.length);
    blocks.pop();
  }
  if (
    blocks.some(
      (block) =>
        typeof block.block_id === "string" &&
        block.block_id.startsWith("relay_footer_"),
    )
  )
    throw new Error("footer_body_changed");
  return {
    text,
    blocks,
    ...(message.attachments ? { attachments: message.attachments } : {}),
  };
}

export function replyBodyDigest(message: SlackMessage, taskId: string): string {
  return digest(JSON.stringify(canonical(originalBody(message, taskId))));
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (isRecord(value))
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonical(value[key])]),
    );
  return value;
}

export async function updateReplyFooter(
  config: FooterConfig,
  ref: ReplyRef,
  message: SlackMessage,
  footer: string,
  fetchImpl: typeof fetch,
): Promise<"updated" | "duplicate" | "unsupported"> {
  const blockId = footerBlockId(ref.taskId);
  const last = message.blocks?.at(-1);
  if (
    last?.block_id === blockId &&
    Array.isArray(last.elements) &&
    isRecord(last.elements[0]) &&
    last.elements[0].text === footer
  )
    return "duplicate";
  const original = originalBody(message, ref.taskId);
  // 不为了附加统计截断正文，也不重编译现有富文本。仅支持已有 blocks 的回复。
  if (
    !original.blocks.length ||
    original.blocks.length >= 50 ||
    original.text.length + footer.length + 2 > 40000
  )
    return "unsupported";
  const result = await slack(
    config,
    "chat.update",
    {
      channel: ref.channelId,
      ts: ref.messageTs,
      text: `${original.text}\n\n${footer}`,
      blocks: [
        ...original.blocks,
        {
          type: "context",
          block_id: blockId,
          elements: [{ type: "mrkdwn", text: footer, verbatim: true }],
        },
      ],
      ...(original.attachments ? { attachments: original.attachments } : {}),
    },
    fetchImpl,
  );
  if (result.ts !== ref.messageTs || result.channel !== ref.channelId)
    throw new Error("footer_update_unconfirmed");
  return "updated";
}
