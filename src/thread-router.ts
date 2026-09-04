import { randomUUID } from 'node:crypto';
import { type MentionMatch } from './mentions.js';
import { triggerMultica, type MulticaDispatchResult, type RelayEventPayload } from './multica.js';
import { createIssueComment, listIssueComments, waitForIssueId } from './multica-api.js';
import { fetchSlackThread, findSeedMessage, type SlackThreadMessage } from './slack-thread.js';
import { type ThreadStore, UpstashThreadStore } from './thread-store.js';

export interface SlackThreadEvent {
  teamId?: string;
  channelId: string;
  messageTs: string;
  threadTs: string;
  senderUserId?: string;
  text: string;
  files?: unknown;
  mention?: MentionMatch;
}

export interface ThreadRouterConfig {
  multicaWebhookUrl: string;
  multicaApiBaseUrl: string;
  multicaApiToken: string;
  multicaWorkspaceId: string;
  multicaAutopilotId: string;
  slackReadToken: string;
  targetUserIds: ReadonlySet<string>;
  targetSubteamIds: ReadonlySet<string>;
  threadMappingTtlSeconds: number;
  threadLockTtlSeconds: number;
  store: ThreadStore;
}

export interface ThreadRouteResult {
  action: 'created' | 'continued' | 'duplicate' | 'ignored' | 'pending';
  issueId?: string;
}

interface StoredThreadState {
  version: 1;
  threadKey: string;
  rootMessageKey: string;
  autopilotId: string;
  runId: string;
  issueId?: string;
  pending: SlackThreadEvent[];
  processedMessageKeys: string[];
  updatedAt: string;
}

export function createConfiguredThreadStore(config: {
  kvRestApiUrl?: string;
  kvRestApiToken?: string;
}): ThreadStore {
  if (!config.kvRestApiUrl || !config.kvRestApiToken) {
    throw new Error('Thread routing requires KV_REST_API_URL and KV_REST_API_TOKEN');
  }
  return new UpstashThreadStore(config.kvRestApiUrl, config.kvRestApiToken);
}

export async function routeSlackThreadEvent(
  event: SlackThreadEvent,
  config: ThreadRouterConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<ThreadRouteResult> {
  const threadKey = makeThreadKey(event);
  const messageKey = makeMessageKey(event);
  const lockKey = `slack:thread-lock:${encodeKey(threadKey)}`;
  const lockOwner = randomUUID();
  if (!await config.store.setIfAbsent(lockKey, lockOwner, config.threadLockTtlSeconds)) {
    throw new Error('thread_lock_busy');
  }

  try {
    let state = await readState(config.store, threadKey);
    if (state?.processedMessageKeys.includes(messageKey)) {
      return { action: 'duplicate', ...(state.issueId ? { issueId: state.issueId } : {}) };
    }

    let seedEvent = event;
    let seedMention = event.mention;
    let created = false;
    if (!state) {
      if (event.threadTs !== event.messageTs) {
        try {
          const threadMessages = await fetchSlackThread(config.slackReadToken, event.channelId, event.threadTs, fetchImpl);
          const seed = findSeedMessage(threadMessages, config.targetUserIds, config.targetSubteamIds);
          if (!seed) return { action: 'ignored' };
          seedEvent = fromSlackThreadMessage(seed.message, event);
          seedMention = seed.mention;
        } catch (error) {
          // 当前消息已经明确 mention 了目标时，即使历史读取临时失败也可先建卡；
          // 后续评论会把根消息补进同一张卡，避免 Slack 事件因读取超时丢失。
          if (!seedMention) throw error;
          console.warn('Slack thread lookup failed; using current mention as seed', { reason: errorMessage(error) });
        }
      } else if (!seedMention) {
        // 根消息本身没有目标 mention 时，不应因为同一 thread 的历史内容而启动任务。
        return { action: 'ignored' };
      }
      const payload = toRelayPayload(seedEvent, seedMention!);
      const dispatch = await triggerMultica(config.multicaWebhookUrl, payload, fetchImpl, config.multicaAutopilotId);
      state = newThreadState(threadKey, seedEvent, dispatch);
      created = true;
      await writeState(config.store, state, config.threadMappingTtlSeconds);
    }

    // 线程归并只负责保持上下文；触发条件仍然是当前消息明确 mention 目标。
    // 这样普通讨论不会不断追加到任务卡，也不会再次唤醒 Agent。
    if (messageKey !== state.rootMessageKey && !event.mention) return { action: 'ignored' };

    // 兼容早期版本把 webhook 凭证误存为 autopilotId 的线程映射，避免旧 thread 永久无法查询 issue。
    if (state.autopilotId !== config.multicaAutopilotId) {
      state.autopilotId = config.multicaAutopilotId;
      await writeState(config.store, state, config.threadMappingTtlSeconds);
    }

    if (!state.issueId) {
      const issueId = await waitForIssueId(
        config.multicaApiBaseUrl,
        config.multicaApiToken,
        config.multicaWorkspaceId,
        state.autopilotId,
        state.runId,
        fetchImpl,
      );
      if (issueId) {
        state.issueId = issueId;
        await writeState(config.store, state, config.threadMappingTtlSeconds);
      }
    }

    if (!state.issueId) {
      if (messageKey !== state.rootMessageKey) addPending(state, event);
      addProcessed(state, state.rootMessageKey);
      await writeState(config.store, state, config.threadMappingTtlSeconds);
      return { action: 'pending' };
    }

    if (messageKey !== state.rootMessageKey) addPending(state, event);
    await flushPending(state, config, fetchImpl);
    await writeState(config.store, state, config.threadMappingTtlSeconds);
    return { action: created ? 'created' : 'continued', issueId: state.issueId };
  } finally {
    await config.store.releaseIfOwner(lockKey, lockOwner);
  }
}

function newThreadState(threadKey: string, seedEvent: SlackThreadEvent, dispatch: MulticaDispatchResult): StoredThreadState {
  if (!dispatch.autopilotId || !dispatch.runId) throw new Error('Multica webhook response did not include run identifiers');
  return {
    version: 1,
    threadKey,
    rootMessageKey: makeMessageKey(seedEvent),
    autopilotId: dispatch.autopilotId,
    runId: dispatch.runId,
    pending: [],
    processedMessageKeys: [],
    updatedAt: new Date().toISOString(),
  };
}

async function flushPending(state: StoredThreadState, config: ThreadRouterConfig, fetchImpl: typeof fetch): Promise<void> {
  if (!state.issueId || state.pending.length === 0) return;
  const pending = [...state.pending].sort((a, b) => a.messageTs.localeCompare(b.messageTs));
  const comments = await listIssueComments(config.multicaApiBaseUrl, config.multicaApiToken, config.multicaWorkspaceId, state.issueId, fetchImpl);
  for (const event of pending) {
    const messageKey = makeMessageKey(event);
    if (state.processedMessageKeys.includes(messageKey)) {
      removePending(state, messageKey);
      continue;
    }
    const marker = markerFor(messageKey);
    if (!comments.some((comment) => typeof comment.content === 'string' && comment.content.includes(marker))) {
      await createIssueComment(
        config.multicaApiBaseUrl,
        config.multicaApiToken,
        config.multicaWorkspaceId,
        state.issueId,
        formatFollowupComment(event, marker),
        messageKey,
        fetchImpl,
      );
    }
    addProcessed(state, messageKey);
    removePending(state, messageKey);
    state.updatedAt = new Date().toISOString();
  }
}

function toRelayPayload(event: SlackThreadEvent, mention: MentionMatch): RelayEventPayload {
  return {
    event: 'slack.mention',
    eventPayload: {
      correlationId: makeMessageKey(event),
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

function formatFollowupComment(event: SlackThreadEvent, marker: string): string {
  const sender = event.senderUserId ? `<@${event.senderUserId}>` : '未知用户';
  const files = event.files === undefined ? '' : `\n\n附件元数据：\n\`\`\`json\n${JSON.stringify(event.files)}\n\`\`\``;
  return `${marker}\n**Slack thread 后续消息**\n- 工作区：\`${event.teamId ?? 'unknown'}\`\n- 频道：\`${event.channelId}\`\n- 发言人：${sender}\n- 消息时间戳：\`${event.messageTs}\`\n- 根 thread：\`${event.threadTs}\`\n\n${event.text}${files}`;
}

function fromSlackThreadMessage(message: SlackThreadMessage, parent: SlackThreadEvent): SlackThreadEvent {
  const threadTs = message.thread_ts ?? parent.threadTs;
  return {
    teamId: parent.teamId,
    channelId: parent.channelId,
    messageTs: message.ts,
    threadTs,
    ...(message.user ? { senderUserId: message.user } : {}),
    text: message.text,
    ...(message.files !== undefined ? { files: message.files } : {}),
  };
}

function makeThreadKey(event: SlackThreadEvent): string {
  return `${event.teamId ?? 'unknown'}:${event.channelId}:${event.threadTs}`;
}

function makeMessageKey(event: SlackThreadEvent): string {
  return `${event.teamId ?? 'unknown'}:${event.channelId}:${event.messageTs}`;
}

function markerFor(messageKey: string): string {
  return `<!-- slack-relay:${messageKey} -->`;
}

function encodeKey(value: string): string {
  return Buffer.from(value, 'utf8').toString('base64url');
}

function addPending(state: StoredThreadState, event: SlackThreadEvent): void {
  const key = makeMessageKey(event);
  if (key === state.rootMessageKey || state.pending.some((item) => makeMessageKey(item) === key)) return;
  state.pending.push(event);
  if (state.pending.length > 100) state.pending.splice(0, state.pending.length - 100);
}

function removePending(state: StoredThreadState, messageKey: string): void {
  state.pending = state.pending.filter((item) => makeMessageKey(item) !== messageKey);
}

function addProcessed(state: StoredThreadState, messageKey: string): void {
  if (!state.processedMessageKeys.includes(messageKey)) state.processedMessageKeys.push(messageKey);
  if (state.processedMessageKeys.length > 500) state.processedMessageKeys.splice(0, state.processedMessageKeys.length - 500);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function readState(store: ThreadStore, threadKey: string): Promise<StoredThreadState | undefined> {
  const raw = await store.get(`slack:thread:${encodeKey(threadKey)}`);
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as StoredThreadState;
    return parsed.version === 1 ? parsed : undefined;
  } catch {
    return undefined;
  }
}

async function writeState(store: ThreadStore, state: StoredThreadState, ttlSeconds: number): Promise<void> {
  state.updatedAt = new Date().toISOString();
  await store.set(`slack:thread:${encodeKey(state.threadKey)}`, JSON.stringify(state), ttlSeconds);
}
