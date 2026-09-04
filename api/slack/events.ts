import type { VercelRequest, VercelResponse } from '@vercel/node';
import { loadRelayConfig } from '../../src/config.js';
import { findTargetMention, isSupportedMessage, type SlackMessageEvent } from '../../src/mentions.js';
import { triggerMultica, type RelayEventPayload } from '../../src/multica.js';
import { addSlackReaction } from '../../src/reaction.js';
import { verifySlackSignature } from '../../src/signature.js';
import {
  createConfiguredThreadStore,
  routeSlackThreadEvent,
  type SlackThreadEvent,
  type ThreadRouteResult,
} from '../../src/thread-router.js';

export const config = { api: { bodyParser: false } };

export default async function handler(request: VercelRequest, response: VercelResponse): Promise<void> {
  if (request.method !== 'POST') {
    response.status(405).json({ error: 'method_not_allowed' });
    return;
  }

  const rawBody = await readRawBody(request);
  let relayConfig;
  try {
    relayConfig = loadRelayConfig();
  } catch {
    response.status(500).json({ error: 'relay_not_configured' });
    return;
  }
  if (!verifySlackSignature(rawBody, header(request, 'x-slack-request-timestamp'), header(request, 'x-slack-signature'), relayConfig.signingSecret)) {
    response.status(401).json({ error: 'invalid_slack_signature' });
    return;
  }

  let body: SlackEnvelope;
  try {
    body = JSON.parse(rawBody) as SlackEnvelope;
  } catch {
    response.status(400).json({ error: 'invalid_json' });
    return;
  }
  if (body.type === 'url_verification' && typeof body.challenge === 'string') {
    response.status(200).json({ challenge: body.challenge });
    return;
  }

  const event = body.event;
  if (!event || !isSupportedMessage(event)) {
    response.status(200).json({ ok: true, ignored: true });
    return;
  }
  const channelId = event.channel as string;
  const mention = findTargetMention(event.text as string, relayConfig.targetUserIds, relayConfig.targetSubteamIds);
  const senderUserId = typeof event.user === 'string' ? event.user : undefined;

  const messageTs = event.ts as string;
  const threadTs = (typeof event.thread_ts === 'string' ? event.thread_ts : messageTs);
  const teamId = typeof body.team_id === 'string'
    ? body.team_id
    : (typeof event.team === 'string' ? event.team : undefined);
  const threadEvent: SlackThreadEvent = {
    ...(teamId ? { teamId } : {}),
    channelId,
    messageTs,
    threadTs,
    ...(senderUserId ? { senderUserId } : {}),
    text: event.text as string,
    ...(mention ? { mention } : {}),
    ...(event.files !== undefined ? { files: event.files } : {}),
  };

  const reactionPromise = mention
    ? addSlackReaction(
      relayConfig.slackReactionToken,
      channelId,
      messageTs,
      relayConfig.slackReactionName,
    )
    : Promise.resolve();

  const dispatchPromise = relayConfig.threadRoutingEnabled
    ? routeSlackThreadEvent(threadEvent, {
      multicaWebhookUrl: relayConfig.multicaWebhookUrl,
          multicaApiBaseUrl: relayConfig.multicaApiBaseUrl!,
          multicaApiToken: relayConfig.multicaApiToken!,
          multicaWorkspaceId: relayConfig.multicaWorkspaceId!,
          multicaAutopilotId: relayConfig.multicaAutopilotId!,
      slackReadToken: relayConfig.slackReadToken,
      targetUserIds: relayConfig.targetUserIds,
      targetSubteamIds: relayConfig.targetSubteamIds,
      threadMappingTtlSeconds: relayConfig.threadMappingTtlSeconds,
      threadLockTtlSeconds: relayConfig.threadLockTtlSeconds,
      store: createConfiguredThreadStore(relayConfig),
    })
    : mention
      ? triggerMultica(relayConfig.multicaWebhookUrl, toRelayPayload(threadEvent, mention)).then((result): ThreadRouteResult => ({
        action: result.status === 'accepted' ? 'created' as const : 'duplicate' as const,
      }))
      : Promise.resolve<ThreadRouteResult>({ action: 'ignored' });

  const [reactionResult, dispatchResult] = await Promise.allSettled([reactionPromise, dispatchPromise]);
  if (dispatchResult.status === 'fulfilled') {
    // reaction 是幂等的提示动作；即使权限或消息状态导致失败，也不能阻断任务派发。
    if (reactionResult.status === 'rejected') {
      console.warn('Slack reaction failed', { reason: errorMessage(reactionResult.reason) });
    }
    response.status(200).json({
      ok: true,
      dispatched: dispatchResult.value.action !== 'ignored',
      action: dispatchResult.value.action,
      ...(dispatchResult.value.issueId ? { issueId: dispatchResult.value.issueId } : {}),
      reacted: reactionResult.status === 'fulfilled',
    });
    return;
  }
  console.warn('Multica dispatch failed', { reason: errorMessage(dispatchResult.reason) });
  response.status(500).json({ error: 'multica_dispatch_failed' });
}

function toRelayPayload(event: SlackThreadEvent, mention: NonNullable<SlackThreadEvent['mention']>): RelayEventPayload {
  return {
    event: 'slack.mention',
    eventPayload: {
      correlationId: `${event.teamId ?? 'unknown'}:${event.channelId}:${event.messageTs}`,
      ...(event.teamId ? { teamId: event.teamId } : {}),
      channelId: event.channelId,
      messageTs: event.messageTs,
      threadTs: event.threadTs,
      ...(event.senderUserId ? { senderUserId: event.senderUserId } : {}),
      mentionType: mention.type,
      mentionId: mention.id,
      text: event.text,
      ...(event.files !== undefined ? { files: event.files } : {}),
    },
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

interface SlackEnvelope {
  type?: unknown;
  challenge?: unknown;
  team_id?: unknown;
  event?: SlackMessageEvent;
}

function header(request: VercelRequest, name: string): string | undefined {
  const value = request.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

async function readRawBody(request: VercelRequest): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  return Buffer.concat(chunks).toString('utf8');
}
