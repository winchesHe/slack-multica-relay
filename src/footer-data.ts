import type { FooterConfig } from "./footer-config.js";
import { readTaskMessage } from "./task-presentation.js";
import { digest } from "./thread-router.js";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
export function isId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/iu.test(value)
  );
}

export interface RunRef {
  version: 1;
  issueId: string;
  taskId: string;
}
export interface ReplyRef extends RunRef {
  channelId: string;
  threadTs: string;
  messageTs: string;
}
export interface FooterRun {
  id: string;
  status: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export function parseRunRef(value: unknown): RunRef {
  if (
    !isRecord(value) ||
    value.version !== 1 ||
    !isId(value.issueId) ||
    !isId(value.taskId)
  )
    throw new Error("invalid_footer_request");
  return { version: 1, issueId: value.issueId, taskId: value.taskId };
}

export function parseReplyRef(value: unknown): ReplyRef {
  const ref = parseRunRef(value);
  if (
    !isRecord(value) ||
    typeof value.channelId !== "string" ||
    !/^[CGD][A-Z0-9]+$/u.test(value.channelId) ||
    ["threadTs", "messageTs"].some(
      (key) =>
        typeof value[key] !== "string" ||
        !/^\d+\.\d{6}$/u.test(value[key] as string),
    )
  )
    throw new Error("invalid_footer_request");
  return {
    ...ref,
    channelId: value.channelId,
    threadTs: value.threadTs as string,
    messageTs: value.messageTs as string,
  };
}

export function footerKey(config: FooterConfig, ref: RunRef): string {
  return `relay:${digest(`${config.multicaWorkspaceId}:${config.multicaProjectId}:${config.multicaAgentId}`)}:footer:${ref.taskId}`;
}

// 从 Issue 原始路由恢复目标，而不是信任发送端传来的频道或 Hook 内的可变字段。
export async function readScopedRun(
  config: FooterConfig,
  ref: RunRef,
  fetchImpl: typeof fetch,
) {
  const get = async (path: string): Promise<unknown> => {
    const response = await fetchImpl(config.multicaApiBaseUrl + path, {
      headers: {
        authorization: `Bearer ${config.multicaApiToken}`,
        "x-workspace-id": config.multicaWorkspaceId,
      },
      redirect: "error",
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) throw new Error("footer_multica_unavailable");
    return response.json();
  };
  const [issue, runs] = await Promise.all([
    get(`/api/issues/${ref.issueId}`),
    get(`/api/issues/${ref.issueId}/task-runs`),
  ]);
  if (
    !isRecord(issue) ||
    issue.id !== ref.issueId ||
    issue.project_id !== config.multicaProjectId ||
    issue.assignee_type !== "agent" ||
    issue.assignee_id !== config.multicaAgentId ||
    issue.workspace_id !== config.multicaWorkspaceId ||
    typeof issue.description !== "string" ||
    !Array.isArray(runs)
  )
    throw new Error("footer_scope_mismatch");
  const matches = runs.filter((run) => isRecord(run) && run.id === ref.taskId);
  const run = matches[0];
  if (
    matches.length !== 1 ||
    !isRecord(run) ||
    run.issue_id !== ref.issueId ||
    run.agent_id !== config.multicaAgentId ||
    run.workspace_id !== config.multicaWorkspaceId ||
    typeof run.status !== "string"
  )
    throw new Error("footer_scope_mismatch");
  const event = readTaskMessage(issue.description);
  const scope = digest(
    `${config.multicaWorkspaceId}:${config.multicaProjectId}:${config.multicaAgentId}`,
  );
  if (
    !issue.description.startsWith(
      `<!-- relay-thread:${scope}:${digest(`${event.teamId}:${event.channelId}:${event.threadTs}`)} -->\n`,
    ) ||
    event.teamId !== config.teamId ||
    (!config.allowAllChannels &&
      !config.allowedChannelIds.has(event.channelId)) ||
    config.blockedChannelIds.has(event.channelId)
  )
    throw new Error("footer_scope_mismatch");
  return { run: run as unknown as FooterRun, event };
}
