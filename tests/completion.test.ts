import { describe, expect, it, vi } from "vitest";
import {
  parseCompletionJob,
  processCompletionCheck,
  type CompletionJob,
} from "../src/completion.js";
import type { RelayConfig } from "../src/config.js";
import {
  MemoryThreadStore,
  type ThreadStore,
} from "../src/thread-store.js";

const config: RelayConfig = {
  signingSecret: "test",
  teamId: "T1",
  targetUserIds: new Set(["U1"]),
  targetSubteamIds: new Set(),
  allowedChannelIds: new Set(["C1"]),
  allowAllChannels: false,
  blockedChannelIds: new Set(),
  allowedSenderIds: new Set(),
  allowAllSenders: true,
  blockedSenderIds: new Set(),
  multicaApiBaseUrl: "https://multica.test",
  multicaApiToken: "multica-token",
  multicaWorkspaceId: "ws",
  multicaProjectId: "project",
  multicaAgentId: "agent",
  slackReactionToken: "slack-token",
  slackReactionName: "eyes",
  kvRestApiUrl: "https://kv.test",
  kvRestApiToken: "kv-token",
  queueUrl: "https://qstash.test",
  queueToken: "queue-token",
  queueCurrentSigningKey: "current",
  queueNextSigningKey: "next",
  consumerUrl: "https://relay.test/api/queue/consume",
  completionUrl: "https://relay.test/api/queue/complete",
  completionReplyEnabled: true,
};

const job: CompletionJob = {
  version: 1,
  issueId: "issue-1",
  event: {
    teamId: "T1",
    channelId: "C1",
    messageTs: "100.000001",
    threadTs: "100.000001",
    senderUserId: "U2",
    text: "<@U1> test",
    mention: { type: "user", id: "U1" },
  },
  attempt: 1,
};

const taskRunFixture = {
  id: "task-1",
  issue_id: "issue-1",
  status: "completed",
  started_at: "2026-09-07T00:00:00.000Z",
  completed_at: "2026-09-07T00:00:12.300Z",
  created_at: "2026-09-07T00:00:00.000Z",
  usage: [
    {
      provider: "openai",
      model: "gpt-5.6-sol",
      input_tokens: 100,
      output_tokens: 50,
      cache_read_tokens: 850,
      cache_write_tokens: 0,
    },
  ],
};

describe("completion reply sink", () => {
  it.each([
    { ...job, attempt: -1 },
    { ...job, attempt: 1.5 },
    { ...job, triggerCommentId: 1 },
    { ...job, triggerCommentId: "" },
  ])("rejects an invalid completion job: %j", (value) => {
    expect(() => parseCompletionJob(value)).toThrow("invalid_completion_job");
  });

  it("posts terminal output with token stats and deduplicates by task", async () => {
    const posted: unknown[] = [];
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/api/issues/issue-1/task-runs"))
        return Response.json([
          {
            ...taskRunFixture,
            result: { output: "*完成*" },
          },
        ]);
      if (url.endsWith("/api/tasks/task-1/messages"))
        return Response.json([
          { type: "tool_use", tool: "functions.exec_command" },
          { type: "text" },
        ]);
      if (url.startsWith("https://slack.com/api/conversations.replies"))
        return Response.json({ ok: true, messages: [], response_metadata: {} });
      if (url === "https://slack.com/api/chat.postMessage") {
        posted.push(JSON.parse(String(init?.body)));
        return Response.json({ ok: true, ts: "101.000001" });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    const store = new MemoryThreadStore();

    expect(await processCompletionCheck(job, config, store, fetcher)).toEqual({
      action: "posted",
      taskId: "task-1",
      messageTs: "101.000001",
    });
    expect(posted).toHaveLength(1);
    expect(posted[0]).toMatchObject({
      text: expect.stringContaining("1.0k tokens (89% cached)"),
      metadata: {
        event_type: "multica_run_reply",
        event_payload: { task_id: "task-1", issue_id: "issue-1" },
      },
    });

    expect(await processCompletionCheck(job, config, store, fetcher)).toEqual({
      action: "duplicate",
      taskId: "task-1",
      messageTs: "101.000001",
    });
    expect(posted).toHaveLength(1);
  });

  it("waits rather than posting fallback text without tokens", async () => {
    const fetcher: typeof fetch = async (input) => {
      if (String(input).endsWith("/task-runs"))
        return Response.json([
          {
            id: "task-1",
            issue_id: "issue-1",
            status: "completed",
            started_at: "2026-09-07T00:00:00.000Z",
            completed_at: "2026-09-07T00:00:12.300Z",
            created_at: "2026-09-07T00:00:00.000Z",
            result: { output: "完成" },
          },
        ]);
      throw new Error("Slack should not be called");
    };

    await expect(
      processCompletionCheck(job, config, new MemoryThreadStore(), fetcher),
    ).resolves.toEqual({ action: "waiting", reason: "task_usage_not_ready" });
  });

  it("does not post when another completion worker wins the reply state", async () => {
    let stateReads = 0;
    const store: ThreadStore = {
      get: async () => {
        stateReads += 1;
        return stateReads === 1 ? null : JSON.stringify({ phase: "writing" });
      },
      set: async () => {},
      setIfAbsent: async () => false,
      releaseIfOwner: async () => {},
    };
    const fetcher: typeof fetch = async (input) => {
      const url = String(input);
      if (url.endsWith("/api/issues/issue-1/task-runs"))
        return Response.json([
          { ...taskRunFixture, result: { output: "完成" } },
        ]);
      if (url.endsWith("/api/tasks/task-1/messages"))
        return Response.json([]);
      if (url.startsWith("https://slack.com/api/conversations.replies"))
        return Response.json({ ok: true, messages: [], response_metadata: {} });
      throw new Error(`Slack post should not be called: ${url}`);
    };

    await expect(
      processCompletionCheck(job, config, store, fetcher),
    ).rejects.toThrow("ambiguous_slack_reply");
  });
});
