import { findTargetMention, type MentionMatch } from './mentions.js';

export interface SlackThreadMessage {
  ts: string;
  thread_ts?: string;
  user?: string;
  text: string;
  files?: unknown;
}

export async function fetchSlackThread(
  token: string,
  channelId: string,
  threadTs: string,
  fetchImpl: typeof fetch = fetch,
): Promise<SlackThreadMessage[]> {
  const query = new URLSearchParams({ channel: channelId, ts: threadTs, limit: '1000' });
  const response = await fetchImpl(`https://slack.com/api/conversations.replies?${query}`, {
    headers: { authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(1_500),
  });
  if (!response.ok) throw new Error(`Slack thread lookup failed with HTTP ${response.status}`);
  const body = await response.json() as { ok?: unknown; error?: unknown; messages?: unknown };
  if (body.ok !== true || !Array.isArray(body.messages)) {
    throw new Error(`Slack thread lookup failed with ${String(body.error ?? 'unknown_error')}`);
  }
  return body.messages.filter(isSlackThreadMessage);
}

export function findSeedMessage(
  messages: readonly SlackThreadMessage[],
  targetUserIds: ReadonlySet<string>,
  targetSubteamIds: ReadonlySet<string>,
): { message: SlackThreadMessage; mention: MentionMatch } | undefined {
  for (const message of messages) {
    const mention = findTargetMention(message.text, targetUserIds, targetSubteamIds);
    if (mention) return { message, mention };
  }
  return undefined;
}

function isSlackThreadMessage(value: unknown): value is SlackThreadMessage {
  if (!value || typeof value !== 'object') return false;
  const message = value as Partial<SlackThreadMessage>;
  return typeof message.ts === 'string' && typeof message.text === 'string';
}
