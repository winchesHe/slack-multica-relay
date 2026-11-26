import { loadRelayConfig } from "../../src/config.js";
import { digest, threadKey, type SlackThreadEvent } from "../../src/thread-router.js";
import { UpstashThreadStore } from "../../src/thread-store.js";
import { formatTaskDescription } from "../../src/task-presentation.js";
import type { IssueRun, MulticaIssue } from "../../src/multica-api.js";

export const route = {
  id: "dorea", mode: "existing_thread_only",
  allowedChannelIds: ["C0CGU0V4W72", "C0B0DSCRBKM"],
  multicaWorkspaceId: "lab", multicaProjectId: "dorea", multicaAssigneeId: "owner",
  allowedAssigneeIds: ["reviewer"], ignoreTextSubstrings: ["[dorea-bug-agent]"],
};
export const env = {
  SLACK_SIGNING_SECRET: "test", SLACK_TEAM_ID: "T1", SLACK_TARGET_USER_IDS: "U1",
  SLACK_ALLOWED_CHANNEL_IDS: "all", SLACK_USER_TOKEN: "slack-test", SLACK_REACTION_NAME: "eyes",
  MULTICA_API_BASE_URL: "https://multica.test", MULTICA_API_TOKEN: "main-test",
  MULTICA_WORKSPACE_ID: "grm", MULTICA_PROJECT_ID: "router", MULTICA_AGENT_ID: "router-agent",
  KV_REST_API_URL: "https://kv.test", KV_REST_API_TOKEN: "kv-test",
  QSTASH_TOKEN: "queue-test", QSTASH_CURRENT_SIGNING_KEY: "test", QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume",
  RELAY_EXTRA_ROUTES_JSON: JSON.stringify([route]),
};
export const root: SlackThreadEvent = {
  teamId: "T1", channelId: "C0B0DSCRBKM", messageTs: "100.000001", threadTs: "100.000001",
  senderUserId: "U1", text: "<@U1> AI-123 Dorea", mention: { type: "user", id: "U1" },
};
export const reply = { ...root, messageTs: "102.000001", text: "<@U1> 批准方案 A" };

export function fixture(overrides: NodeJS.ProcessEnv = {}) {
  const config = loadRelayConfig({ ...env, ...overrides });
  const kv = new Map<string, string>();
  const issues: (MulticaIssue & { workspace_id: string })[] = [];
  const comments: { id: string; content: string }[] = [];
  const runs: IssueRun[] = [];
  const calls: { url: string; method: string; workspace: string | null; body: unknown; signal?: AbortSignal | null }[] = [];
  let ownReactions = true;
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    const workspace = new Headers(init?.headers).get("x-workspace-id");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, workspace, body, signal: init?.signal });
    if (url === env.KV_REST_API_URL) {
      const [command, key, value] = body;
      if (command === "GET") return Response.json({ result: kv.get(key) ?? null });
      if (command === "SET") {
        if (body.includes("NX") && kv.has(key)) return Response.json({ result: null });
        kv.set(key, value); return Response.json({ result: "OK" });
      }
      if (command === "EVAL") {
        if (kv.get(body[3]) === body[4]) kv.delete(body[3]);
        return Response.json({ result: 1 });
      }
    }
    if (url.includes("/api/issues/search?"))
      return Response.json({ issues: issues.filter((issue) => issue.workspace_id === workspace) });
    if (url.includes("/api/agents/"))
      return Response.json({ id: url.split("/").at(-1), workspace_id: workspace });
    if (url.endsWith("/api/issues") && method === "POST") {
      const issue = { ...body, id: "grm-created", workspace_id: workspace };
      issues.push(issue); return Response.json(issue);
    }
    const issue = issues.find((candidate) => url.endsWith("/api/issues/" + candidate.id));
    if (issue) return Response.json(issue);
    if (url.includes("/comments")) {
      if (method === "POST") { const comment = { id: "comment", ...body }; comments.push(comment); return Response.json(comment); }
      return Response.json(comments);
    }
    if (url.endsWith("/task-runs")) return Response.json(runs);
    if (url.endsWith("/cancel")) {
      runs.find((run) => url.includes("/tasks/" + run.id + "/"))!.status = "cancelled";
      return Response.json({});
    }
    if (url.endsWith("/auth.test")) return Response.json({ ok: true, user_id: "U1" });
    if (url.includes("/reactions.get?")) return Response.json({ ok: true, message: {
      reactions: ownReactions ? [{ name: "eyes", users: ["U1"] }] : [],
    } });
    if (url.endsWith("/reactions.remove")) ownReactions = false;
    if (url.endsWith("/reactions.add") || url.endsWith("/reactions.remove")) return Response.json({ ok: true });
    throw new Error("unexpected test endpoint: " + url);
  };
  const store = new UpstashThreadStore(env.KV_REST_API_URL, env.KV_REST_API_TOKEN, fetcher);
  const extra = config.extraRoutes[0]!;
  const scope = digest(`${extra.multicaWorkspaceId}:${extra.multicaProjectId}:${extra.multicaThreadScopeId ?? extra.multicaAssigneeId}`);
  const key = `relay:${scope}:thread:${digest(threadKey(root))}`;
  const marker = `<!-- relay-thread:${scope}:${digest(threadKey(root))} -->`;
  const bound = { id: "lab-issue", title: "AI-123", description: formatTaskDescription(root, marker),
    project_id: extra.multicaProjectId, assignee_type: "agent", assignee_id: extra.multicaAssigneeId!,
    workspace_id: extra.multicaWorkspaceId };
  return { config: { ...config, store }, kv, issues, comments, runs, calls, fetcher, bound, key, marker };
}
