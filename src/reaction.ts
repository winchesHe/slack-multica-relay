const SLACK_API_URL = 'https://slack.com/api/reactions.add';

export async function addSlackReaction(
  token: string,
  channelId: string,
  messageTs: string,
  reactionName: string,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  const response = await fetchImpl(SLACK_API_URL, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${token}`,
      'content-type': 'application/json; charset=utf-8',
    },
    body: JSON.stringify({
      channel: channelId,
      timestamp: messageTs,
      name: reactionName,
    }),
    signal: AbortSignal.timeout(2_000),
  });
  if (!response.ok) throw new Error(`Slack reaction failed with HTTP ${response.status}`);
  const body = await response.json() as { ok?: unknown; error?: unknown };
  if (body.ok === true || body.error === 'already_reacted') return;
  throw new Error(`Slack reaction failed with ${String(body.error ?? 'unknown_error')}`);
}
