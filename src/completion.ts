import type { RelayConfig } from "./config.js";
import {
  listTaskMessages,
  listTaskRuns,
  type MulticaTaskRun,
} from "./multica-api.js";
import {
  buildRunStatsFooter,
  buildSlackRunReplyPayload,
  findSlackRunReply,
  postSlackRunReply,
} from "./slack-reply.js";
import {
  digest,
  messageKey,
  STATE_TTL_SECONDS,
  type SlackThreadEvent,
} from "./thread-router.js";
import type { ThreadStore } from "./thread-store.js";

export const COMPLETION_MAX_ATTEMPTS = 120;

export interface CompletionJob {
  version: 1;
  issueId: string;
  triggerCommentId?: string;
  event: SlackThreadEvent;
  attempt: number;
}

export type CompletionResult =
  | { action: "waiting"; reason: string }
  | { action: "posted" | "duplicate"; taskId: string; messageTs?: string }
  | { action: "terminal_without_reply"; taskId: string; status: string };

export function parseCompletionJob(value: unknown): CompletionJob {
  const job = value as Record<string, unknown>;
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    job.version !== 1 ||
    typeof job.issueId !== "string" ||
    !job.issueId.trim() ||
    !Number.isInteger(job.attempt) ||
    (job.attempt as number) < 0 ||
    (job.triggerCommentId !== undefined &&
      (typeof job.triggerCommentId !== "string" ||
        !job.triggerCommentId.trim())) ||
    !job.event ||
    typeof job.event !== "object" ||
    Array.isArray(job.event)
  )
    throw new Error("invalid_completion_job");
  return value as CompletionJob;
}

export async function publishCompletionCheck(
  config: RelayConfig,
  job: CompletionJob,
  delaySeconds: number,
  fetchImpl: typeof fetch = fetch,
): Promise<string> {
  const response = await fetchImpl(
    `${config.queueUrl}/v2/publish/${config.completionUrl}`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${config.queueToken}`,
        "content-type": "application/json",
        "Upstash-Deduplication-Id": digest(
          `${config.completionUrl}:${messageKey(job.event)}:${job.attempt}`,
        ),
        "Upstash-Delay": `${delaySeconds}s`,
        "Upstash-Retries": "3",
        "Upstash-Timeout": "50s",
      },
      body: JSON.stringify(job),
      signal: AbortSignal.timeout(5_000),
    },
  );
  if (!response.ok) throw new Error("completion_schedule_failed");
  const body = (await response.json()) as { messageId?: unknown };
  if (typeof body.messageId !== "string")
    throw new Error("completion_schedule_failed");
  return body.messageId;
}

export async function processCompletionCheck(
  job: CompletionJob,
  config: RelayConfig,
  store: ThreadStore,
  fetchImpl: typeof fetch = fetch,
): Promise<CompletionResult> {
  const tasks = await listTaskRuns(config, job.issueId, fetchImpl);
  const task = selectTask(tasks, job.triggerCommentId);
  if (!task) return { action: "waiting", reason: "task_not_created" };
  if (!["completed", "failed", "cancelled"].includes(task.status))
    return { action: "waiting", reason: "task_not_terminal" };
  if (task.status !== "completed")
    return {
      action: "terminal_without_reply",
      taskId: task.id,
      status: task.status,
    };

  const output = taskOutput(task.result);
  if (!output) return { action: "waiting", reason: "task_output_not_ready" };

  let messages;
  try {
    messages = await listTaskMessages(config, task.id, fetchImpl);
  } catch {
    // tools 是附加统计；读取失败时仍可交付包含真实 tokens 的最终回复。
  }
  const footer = buildRunStatsFooter(task, messages);
  if (!footer) return { action: "waiting", reason: "task_usage_not_ready" };

  const payload = buildSlackRunReplyPayload({
    channelId: job.event.channelId,
    threadTs: job.event.threadTs,
    issueId: job.issueId,
    taskId: task.id,
    output,
    footer,
  });
  const scope = digest(
    `${config.multicaWorkspaceId}:${config.multicaProjectId}:${config.multicaAgentId}`,
  );
  const replyStateKey = `relay:${scope}:reply:${digest(task.id)}`;
  const existingState = await store.get(replyStateKey);
  if (existingState) {
    const state = JSON.parse(existingState) as {
      phase?: string;
      messageTs?: string;
    };
    if (state.phase === "done")
      return {
        action: "duplicate",
        taskId: task.id,
        messageTs: state.messageTs,
      };
  }

  const recovered = await findSlackRunReply(
    config.slackReactionToken,
    job.event.channelId,
    job.event.threadTs,
    task.id,
    fetchImpl,
  );
  if (recovered) {
    await store.set(
      replyStateKey,
      JSON.stringify({ phase: "done", messageTs: recovered }),
      STATE_TTL_SECONDS,
    );
    return { action: "duplicate", taskId: task.id, messageTs: recovered };
  }
  if (existingState) throw new Error("ambiguous_slack_reply");

  const acquired = await store.setIfAbsent(
    replyStateKey,
    JSON.stringify({ phase: "writing" }),
    STATE_TTL_SECONDS,
  );
  if (!acquired) {
    const concurrentState = await store.get(replyStateKey);
    if (concurrentState) {
      const state = JSON.parse(concurrentState) as {
        phase?: string;
        messageTs?: string;
      };
      if (state.phase === "done")
        return {
          action: "duplicate",
          taskId: task.id,
          messageTs: state.messageTs,
        };
    }
    throw new Error("ambiguous_slack_reply");
  }
  const messageTs = await postSlackRunReply(
    config.slackReactionToken,
    payload,
    fetchImpl,
  );
  await store.set(
    replyStateKey,
    JSON.stringify({ phase: "done", messageTs }),
    STATE_TTL_SECONDS,
  );
  return { action: "posted", taskId: task.id, messageTs };
}

function selectTask(
  tasks: MulticaTaskRun[],
  triggerCommentId?: string,
): MulticaTaskRun | undefined {
  const matches = triggerCommentId
    ? tasks.filter(
        (task) =>
          task.trigger_comment_id === triggerCommentId ||
          task.coalesced_comment_ids?.includes(triggerCommentId) ||
          task.delivered_comment_ids?.includes(triggerCommentId),
      )
    : tasks.filter((task) => !task.trigger_comment_id);
  return [...matches].sort(
    (left, right) => Date.parse(left.created_at) - Date.parse(right.created_at),
  )[0];
}

function taskOutput(result: unknown): string | undefined {
  if (!result || typeof result !== "object" || Array.isArray(result)) return;
  const output = (result as Record<string, unknown>).output;
  return typeof output === "string" && output.trim() ? output : undefined;
}
