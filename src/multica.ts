import type { MentionMatch } from './mentions.js';

export interface RelayEventPayload {
  event: 'slack.mention';
  eventPayload: {
    correlationId: string;
    teamId?: string;
    channelId: string;
    messageTs: string;
    threadTs: string;
    sourcePermalink?: string;
    senderUserId?: string;
    mentionType: MentionMatch['type'];
    mentionId: string;
    text: string;
    files?: unknown;
  };
}

export interface MulticaDispatchResult {
  status: 'accepted' | 'duplicate';
  autopilotId?: string;
  runId?: string;
  deliveryId?: string;
}

export async function triggerMultica(
  webhookUrl: string,
  payload: RelayEventPayload,
  fetchImpl: typeof fetch = fetch,
  fallbackAutopilotId?: string,
): Promise<MulticaDispatchResult> {
  const body = await postWebhook(webhookUrl, payload, fetchImpl);
  const firstResult = toDispatchResult(body, fallbackAutopilotId);
  if (firstResult) return firstResult;

  // Multica 先确认 admission、后生成 run；用相同幂等键重读一次可以拿到已创建 run，且不会重复建卡。
  if (body.status === 'accepted' || body.status === 'duplicate') {
    await new Promise((resolve) => setTimeout(resolve, 250));
    const retryBody = await postWebhook(webhookUrl, payload, fetchImpl);
    const retryResult = toDispatchResult(retryBody, fallbackAutopilotId);
    if (retryResult) return retryResult;
  }
  if (body.status !== 'accepted' && body.status !== 'duplicate') {
    throw new Error('Multica webhook did not create a run');
  }
  throw new Error('Multica webhook response did not include run identifiers');
}

interface MulticaWebhookBody {
  status?: unknown;
  autopilot_id?: unknown;
  run_id?: unknown;
  delivery_id?: unknown;
}

async function postWebhook(
  webhookUrl: string,
  payload: RelayEventPayload,
  fetchImpl: typeof fetch,
): Promise<MulticaWebhookBody> {
  const response = await fetchImpl(webhookUrl, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'idempotency-key': payload.eventPayload.correlationId,
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(2_500),
  });
  if (!response.ok) throw new Error(`Multica webhook failed with HTTP ${response.status}`);
  return response.json() as Promise<MulticaWebhookBody>;
}

function toDispatchResult(body: MulticaWebhookBody, fallbackAutopilotId?: string): MulticaDispatchResult | undefined {
  if (body.status !== 'accepted' && body.status !== 'duplicate') return undefined;
  const autopilotId = typeof body.autopilot_id === 'string' ? body.autopilot_id : fallbackAutopilotId;
  const runId = typeof body.run_id === 'string' ? body.run_id : undefined;
  if (!autopilotId || !runId) return undefined;
  return {
    status: body.status,
    autopilotId,
    runId,
    ...(typeof body.delivery_id === 'string' ? { deliveryId: body.delivery_id } : {}),
  };
}
