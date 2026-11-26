import { createHash, randomUUID } from "node:crypto";
import {
  ApiError,
  threadScopeId,
  validateMappedIssue,
  createIssue,
  findIssue,
  createComment,
  findComment,
  getSlackReplyContext,
  type ApiConfig,
  type SlackReplyContext,
} from "./multica-api.js";
import type { ExtraRouteConfig } from "./config.js";
import type { MentionMatch } from "./mentions.js";
import { addSlackReaction, reactionErrorDetails } from "./reaction.js";
import {
  cancelThread,
  compareTimestamp,
  maxTimestamp,
  type CancellationState,
} from "./cancellation.js";
import { type ThreadStore } from "./thread-store.js";
import {
  formatTaskTitle,
  formatTaskDescription,
  readTaskMessage,
  compactEvent,
} from "./task-presentation.js";

export interface SlackThreadEvent {
  teamId: string;
  channelId: string;
  messageTs: string;
  threadTs: string;
  senderUserId: string;
  text: string;
  mention: MentionMatch;
  files?: unknown;
  filesTruncated?: boolean;
  operation?: "dispatch" | "cancel";
}
export interface ThreadRouterConfig extends ApiConfig {
  store: ThreadStore;
  existingThreadOnly?: boolean;
  slackReactionToken?: string;
  slackReactionName?: string;
}
export interface ThreadState {
  version: 2;
  rootMessageKey: string;
  issueId?: string;
  creating: boolean;
  lastMessageTs?: string;
  ignoredThrough?: string;
  reactionMessages?: string[];
  reactionHistoryKnown?: boolean;
  cancellation?: CancellationState;
}
interface MessageState {
  phase: "writing" | "done" | "rejected";
}
export interface ThreadRouteResult {
  action:
    | "created"
    | "comment_persisted"
    | "duplicate"
    | "cancelled"
    | "no_active_run"
    | "ignored";
  issueId?: string;
  routeId?: string;
  reason?: "route_ignored_text";
}
export const STATE_TTL_SECONDS = 90 * 24 * 60 * 60;
export function threadKey(event: SlackThreadEvent): string {
  return `${event.teamId}:${event.channelId}:${event.threadTs}`;
}
export function messageKey(event: SlackThreadEvent): string {
  return `${event.teamId}:${event.channelId}:${event.messageTs}`;
}
export function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function routeScope(config: ApiConfig): string {
  return digest(`${config.multicaWorkspaceId}:${config.multicaProjectId}:${threadScopeId(config)}`);
}

export async function routeSlackThreadEventWithExtraRoutes(
  event: SlackThreadEvent,
  config: ThreadRouterConfig & { extraRoutes?: ExtraRouteConfig[] },
  fetchImpl: typeof fetch = fetch,
): Promise<ThreadRouteResult> {
  for (const route of config.extraRoutes ?? []) {
    if (!route.allowedChannelIds.includes(event.channelId)) continue;
    // 不继承主路由的 legacy Agent、scope 或归属白名单，防止跨路由采纳映射。
    const extraConfig: ThreadRouterConfig = {
      ...route, store: config.store, existingThreadOnly: true,
      slackReactionToken: config.slackReactionToken,
      slackReactionName: config.slackReactionName,
    };
    const scope = routeScope(extraConfig);
    const hash = digest(threadKey(event));
    const marker = `<!-- relay-thread:${scope}:${hash} -->`;
    const raw = await config.store.get(`relay:${scope}:thread:${hash}`);
    const state = raw ? parseThreadState(raw) : undefined;
    // 已有未确认写入同样不能被解释成“无映射”。错误交由消费端重试，绝不回落建卡。
    if (state?.creating && !state.issueId) throw new Error("ambiguous_issue_create");
    const existing = state?.issueId || (await findIssue(extraConfig, marker, fetchImpl))?.id;
    if (!existing) continue;
    if (route.ignoreTextSubstrings?.some((text) => event.text.includes(text)))
      return { action: "ignored", reason: "route_ignored_text", routeId: route.id };
    const result = await routeSlackThreadEvent(event, extraConfig, fetchImpl);
    return { ...result, routeId: route.id };
  }
  return routeSlackThreadEvent(event, config, fetchImpl);
}

function parseThreadState(raw: string): ThreadState {
  const state = JSON.parse(raw) as ThreadState | null;
  if (!state || state.version !== 2 || typeof state.rootMessageKey !== "string" ||
    typeof state.creating !== "boolean" ||
    (state.issueId !== undefined && (typeof state.issueId !== "string" || !state.issueId)))
    throw new Error("invalid_thread_state");
  return state;
}

export async function routeSlackThreadEvent(
  event: SlackThreadEvent,
  config: ThreadRouterConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<ThreadRouteResult> {
  const scope = routeScope(config);
  const key = `relay:${scope}:thread:${digest(threadKey(event))}`;
  const msgKey = `relay:${scope}:message:${digest(messageKey(event))}`;
  const lockKey = key + ":lock",
    owner = randomUUID();
  if (!(await config.store.setIfAbsent(lockKey, owner, 120)))
    throw new Error("thread_lock_busy");
  try {
    const raw = await config.store.get(key);
    let state: ThreadState;
    if (raw) state = parseThreadState(raw);
    else
      state = {
        version: 2,
        rootMessageKey: messageKey(event),
        creating: false,
      };
    const marker = `<!-- relay-thread:${scope}:${digest(threadKey(event))} -->`;
    const save = () =>
      config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
    if (event.operation === "cancel")
      return await cancelThread(event, state, marker, config, save, fetchImpl);
    if (state.cancellation && state.cancellation.phase !== "done") {
      state.ignoredThrough = maxTimestamp(
        state.ignoredThrough,
        event.messageTs,
      );
      await save();
      return { action: "ignored", issueId: state.issueId };
    }
    if (
      state.ignoredThrough &&
      compareTimestamp(event.messageTs, state.ignoredThrough) <= 0
    )
      return { action: "ignored", issueId: state.issueId };
    if (state.issueId) await validateMappedIssue(config, state.issueId, marker, fetchImpl);
    state.lastMessageTs = maxTimestamp(state.lastMessageTs, event.messageTs);
    // 保存触发消息，再尝试外部写入，取消恢复才能找到响应丢失的消息。
    state.reactionMessages ??= raw
      ? [state.rootMessageKey.split(":").at(-1)!]
      : [];
    if (!state.reactionMessages.includes(event.messageTs))
      state.reactionMessages.push(event.messageTs);
    await save();
    const finish = async (
      action: "created" | "comment_persisted" | "duplicate",
    ): Promise<ThreadRouteResult> => {
      // 添加和取消清理共用线程锁，避免迟到的启动 reaction 出现在已取消任务上。
      if (config.slackReactionToken && config.slackReactionName) {
        try {
          await addSlackReaction(
            config.slackReactionToken,
            event.channelId,
            event.messageTs,
            config.slackReactionName,
            fetchImpl,
          );
        } catch (error) {
          console.warn("relay_reaction", {
            eventId: digest(messageKey(event)),
            reason: "reaction_failed",
            ...reactionErrorDetails(error),
          });
        }
      }
      return { action, issueId: state.issueId };
    };
    const prepare = async (descriptionMarker: string, followup: boolean): Promise<string> => {
      const preparedKey = msgKey + ":envelope";
      const previous = await config.store.get(preparedKey);
      const envelope = previous
        ? JSON.parse(previous) as { eventPayload: SlackThreadEvent; replyContext: SlackReplyContext }
        : { eventPayload: compactEvent(event), replyContext: await getSlackReplyContext(config, fetchImpl) };
      // Freeze the event, not its role as an Issue or comment: a rejected first
      // create can be overtaken by a follow-up before it is retried.
      const description = formatTaskDescription(envelope.eventPayload, descriptionMarker, followup, envelope.replyContext);
      if (!previous)
        await config.store.set(preparedKey, JSON.stringify(envelope), 24 * 60 * 60);
      return description;
    };
    if (!state.issueId) {
      // Recover by immutable description marker before any write. A POST whose
      // result is unknown must never be repeated blindly.
      const existing = await findIssue(config, marker, fetchImpl);
      if (existing) {
        state.issueId = existing.id;
        if (!raw) {
          const original = readTaskMessage(existing.description!);
          if (
            `${original.teamId}:${original.channelId}:${original.threadTs}` !==
            threadKey(event)
          )
            throw new Error("invalid_thread_state");
          state.rootMessageKey = `${original.teamId}:${original.channelId}:${original.messageTs}`;
          if (!state.reactionMessages!.includes(original.messageTs))
            state.reactionMessages!.push(original.messageTs);
        }
      } else {
        if (config.existingThreadOnly) throw new Error("extra_route_mapping_missing");
        if (state.creating) throw new Error("ambiguous_issue_create");
        const description = await prepare(marker, false);
        state.rootMessageKey = messageKey(event);
        state.creating = true;
        await config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
        try {
          const created = await createIssue(
            config,
            formatTaskTitle(event, scope),
            description,
            fetchImpl,
          );
          state.issueId = created.id;
          state.reactionHistoryKnown = true;
        } catch (error) {
          // Definite request rejection permits a later retry; 5xx/transport or
          // malformed success may have committed, so retain the write intent.
          if (
            error instanceof ApiError &&
            error.status >= 400 &&
            error.status < 500
          ) {
            state.creating = false;
            await config.store.set(
              key,
              JSON.stringify(state),
              STATE_TTL_SECONDS,
            );
          }
          throw error;
        }
      }
      state.creating = false;
      await config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
    }
    const previous = await config.store.get(msgKey);
    if (previous && (JSON.parse(previous) as MessageState).phase === "done")
      return await finish("duplicate");
    if (messageKey(event) === state.rootMessageKey) {
      await config.store.set(
        msgKey,
        JSON.stringify({ phase: "done" }),
        STATE_TTL_SECONDS,
      );
      return await finish("created");
    }
    const messageMarker = `<!-- relay-message:${digest(messageKey(event))} -->`;
    const existingComment = await findComment(
      config,
      state.issueId,
      messageMarker,
      fetchImpl,
    );
    if (!existingComment) {
      if (
        previous &&
        (JSON.parse(previous) as MessageState).phase === "writing"
      )
        throw new Error("ambiguous_comment_create");
      const description = await prepare(messageMarker, true);
      await config.store.set(
        msgKey,
        JSON.stringify({ phase: "writing" }),
        STATE_TTL_SECONDS,
      );
      try {
        await createComment(
          config,
          state.issueId,
          description,
          fetchImpl,
        );
      } catch (error) {
        // Do not clear ambiguous writes. Explicit rejections are retried by
        // the queue after storing a non-writing phase.
        if (
          error instanceof ApiError &&
          error.status >= 400 &&
          error.status < 500
        )
          await config.store.set(
            msgKey,
            JSON.stringify({ phase: "rejected" }),
            STATE_TTL_SECONDS,
          );
        throw error;
      }
    }
    await config.store.set(
      msgKey,
      JSON.stringify({ phase: "done" }),
      STATE_TTL_SECONDS,
    );
    return await finish("comment_persisted");
  } finally {
    await config.store.releaseIfOwner(lockKey, owner);
  }
}
