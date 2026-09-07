import { describe, expect, it } from "vitest";
import {
  buildRunStatsFooter,
  buildSlackRunReplyPayload,
} from "../src/slack-reply.js";
import type { MulticaTaskRun } from "../src/multica-api.js";

const task: MulticaTaskRun = {
  id: "task-1",
  issue_id: "issue-1",
  status: "completed",
  started_at: "2026-09-07T00:00:00.000Z",
  completed_at: "2026-09-07T00:14:12.000Z",
  created_at: "2026-09-07T00:00:00.000Z",
  result: { output: "完成" },
  usage: [
    {
      provider: "openai",
      model: "gpt-5.6-sol",
      input_tokens: 160_000,
      output_tokens: 20_000,
      cache_read_tokens: 5_219_800,
      cache_write_tokens: 0,
    },
  ],
};

describe("Slack run stats footer", () => {
  it("includes tokens in both fallback text and the context block", () => {
    const footer = buildRunStatsFooter(
      task,
      Array.from({ length: 88 }, () => ({ type: "tool_use" })),
    );
    expect(footer).toBe(
      "⏱ 14m 12s · gpt-5.6-sol: 5399.8k tokens (97% cached) · 88 tools",
    );

    const payload = buildSlackRunReplyPayload({
      channelId: "C1",
      threadTs: "100.000001",
      issueId: "issue-1",
      taskId: "task-1",
      output: "处理完成",
      footer: footer!,
    });
    expect(payload.text).toBe(`🤖 自动化助手\n\n处理完成\n\n${footer}`);
    expect(payload.blocks.at(-1)).toEqual({
      type: "context",
      elements: [{ type: "mrkdwn", text: footer, verbatim: true }],
    });
  });

  it("does not invent a token figure before usage is available", () => {
    expect(buildRunStatsFooter({ ...task, usage: undefined })).toBeUndefined();
  });

  it("normalizes a rounded 60-second duration", () => {
    expect(
      buildRunStatsFooter({
        ...task,
        completed_at: "2026-09-07T00:00:59.960Z",
      }),
    ).toBe("⏱ 1m 0s · gpt-5.6-sol: 5399.8k tokens (97% cached)");
  });

  it("neutralizes broadcast and user-group markup in Agent output", () => {
    const footer = buildRunStatsFooter(task)!;
    const payload = buildSlackRunReplyPayload({
      channelId: "C1",
      threadTs: "100.000001",
      issueId: "issue-1",
      taskId: "task-1",
      output: "不要触发 <!channel>、<!here> 或 <!subteam^S1|ops>",
      footer,
    });
    expect(payload.text).toContain(
      "不要触发 &lt;!channel>、&lt;!here> 或 &lt;!subteam^S1|ops>",
    );
    expect(JSON.stringify(payload.blocks)).not.toContain("<!channel>");
  });
});
