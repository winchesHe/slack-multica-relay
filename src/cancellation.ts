import {
  findIssue,
  getIssue,
  listIssueRuns,
  cancelIssueRun,
  isActiveRun,
  listRelayMessageContents,
} from "./multica-api.js";
import { compareTs } from "./context-envelope.js";
import { readTaskEnvelope } from "./task-presentation.js";
import { clearOwnSlackReactions } from "./reaction.js";
import type {
  SlackThreadEvent,
  ThreadState,
  ThreadRouterConfig,
  ThreadRouteResult,
} from "./thread-router.js";

export interface CancellationState {
  messageTs: string;
  phase: "cancelling" | "cleaning" | "done";
  runIds?: string[];
  remainingMessages?: string[];
  outcome?: "cancelled" | "no_active_run" | "ignored";
}

export async function cancelThread(
  event: SlackThreadEvent,
  state: ThreadState,
  marker: string,
  config: ThreadRouterConfig,
  save: () => Promise<void>,
  fetchImpl: typeof fetch,
): Promise<ThreadRouteResult> {
  const previous = state.cancellation;
  const resumingCleanup = previous?.phase === "cleaning";
  if (previous?.phase === "done" && previous.messageTs === event.messageTs)
    return { action: previous.outcome!, issueId: state.issueId };
  if (
    (!previous || previous.phase === "done") &&
    ((state.lastMessageTs &&
      compareTs(event.messageTs, state.lastMessageTs) < 0) ||
      (state.ignoredThrough &&
        compareTs(event.messageTs, state.ignoredThrough) <= 0))
  )
    return { action: "ignored", issueId: state.issueId };

  if (!previous || previous.phase === "done")
    state.cancellation = { messageTs: event.messageTs, phase: "cancelling" };
  const cancellation = state.cancellation!;
  state.ignoredThrough = maxTimestamp(state.ignoredThrough, event.messageTs);
  await save();
  const finish = async (outcome: "cancelled" | "no_active_run" | "ignored") => {
    cancellation.phase = "done";
    cancellation.outcome = outcome;
    // 已在取消期间发出的消息不能因队列延迟在收尾后重新启动任务。
    state.ignoredThrough = maxTimestamp(
      state.ignoredThrough,
      (Date.now() / 1000).toFixed(6),
    );
    if (outcome === "cancelled") state.reactionMessages = [];
    await save();
    return { action: outcome, issueId: state.issueId };
  };

  if (!state.issueId) {
    const existing = await findIssue(config, marker, fetchImpl);
    if (!existing) {
      // 未确认的建卡可能已经提交，保留取消意图，让持久化队列继续回读。
      if (state.creating) throw new Error("cancellation_pending");
      return finish("ignored");
    }
    const original = readTaskEnvelope(existing.description!).eventPayload;
    if (
      original.teamId !== event.teamId ||
      original.channelId !== event.channelId ||
      original.threadTs !== event.threadTs
    )
      throw new Error("invalid_thread_state");
    if (compareTs(event.messageTs, original.messageTs) < 0)
      return finish("ignored");
    state.rootMessageKey = `${original.teamId}:${original.channelId}:${original.messageTs}`;
    state.issueId = existing.id;
    state.creating = false;
    await save();
  }
  const issue = await getIssue(config, state.issueId, fetchImpl);
  if (!issue.description?.startsWith(marker + "\n"))
    throw new Error("invalid_issue_scope");
  if (cancellation.phase === "cancelling") {
    const runs = await listIssueRuns(config, state.issueId, fetchImpl);
    if (!cancellation.runIds) {
      if (!runs.length) throw new Error("cancellation_pending");
      const active = runs.filter(isActiveRun);
      if (!active.length) return finish("no_active_run");
      if (active.some((run) => run.agent_id !== config.multicaAgentId))
        throw new Error("invalid_issue_scope");
      cancellation.runIds = active.map((run) => run.id);
      await save();
    }
    for (const id of cancellation.runIds) {
      const run = runs.find((candidate) => candidate.id === id);
      if (!run) throw new Error("cancellation_run_missing");
      // 重试先读状态；只对仍活动的同一个 ID 重试，绝不重新抓取下一轮作为目标。
      if (isActiveRun(run))
        await cancelIssueRun(config, state.issueId, id, fetchImpl);
    }
    const verified = await listIssueRuns(config, state.issueId, fetchImpl);
    const targets = cancellation.runIds.map((id) =>
      verified.find((run) => run.id === id),
    );
    if (targets.some((run) => !run || isActiveRun(run)))
      throw new Error("cancellation_pending");
    if (verified.some(isActiveRun)) throw new Error("cancellation_new_run");
    if (!targets.some((run) => run!.status === "cancelled"))
      return finish("no_active_run");
    if (!state.reactionHistoryKnown) {
      const source = readTaskEnvelope(issue.description!).eventPayload;
      const messages = [
        source,
        ...(
          await listRelayMessageContents(config, state.issueId, fetchImpl)
        ).map((content) => readTaskEnvelope(content).eventPayload),
      ];
      if (
        messages.some(
          (message) =>
            message.teamId !== event.teamId ||
            message.channelId !== event.channelId ||
            message.threadTs !== event.threadTs,
        )
      )
        throw new Error("invalid_thread_state");
      state.reactionMessages = [
        ...new Set([
          ...(state.reactionMessages ?? []),
          ...messages.map((message) => message.messageTs),
        ]),
      ];
      state.reactionHistoryKnown = true;
    }
    cancellation.phase = "cleaning";
    cancellation.remainingMessages = [
      ...new Set(
        state.reactionMessages ?? [state.rootMessageKey.split(":").at(-1)!],
      ),
    ];
    await save();
  }
  if (
    resumingCleanup &&
    (await listIssueRuns(config, state.issueId, fetchImpl)).some(isActiveRun)
  )
    throw new Error("cancellation_new_run");
  if (!config.slackReactionToken) throw new Error("reaction_cleanup_failed");
  for (const ts of [...cancellation.remainingMessages!]) {
    await clearOwnSlackReactions(
      config.slackReactionToken,
      event.channelId,
      ts,
      fetchImpl,
    );
    cancellation.remainingMessages = cancellation.remainingMessages!.filter(
      (value) => value !== ts,
    );
    await save();
  }
  return finish("cancelled");
}

export function maxTimestamp(a: string | undefined, b: string): string {
  return a && compareTs(a, b) > 0 ? a : b;
}
