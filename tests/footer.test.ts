import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  acceptMulticaHook,
  processFooter,
  retireFooter,
  registerSlackReply,
} from "../src/footer.js";
import { loadFooterConfig } from "../src/footer-config.js";
import { UpstashThreadStore } from "../src/thread-store.js";
import { buildDurationFooter } from "../src/footer-stats.js";
import { digest } from "../src/thread-router.js";
import { formatTaskDescription } from "../src/task-presentation.js";

const background = vi.hoisted(() => ({ jobs: [] as Promise<unknown>[] }));
vi.mock("@vercel/functions", () => ({
  waitUntil: (job: Promise<unknown>) => background.jobs.push(job),
}));

const workspace = "11111111-1111-1111-1111-111111111111";
const agent = "22222222-2222-2222-2222-222222222222";
const project = "33333333-3333-3333-3333-333333333333";
const issueId = "44444444-4444-4444-4444-444444444444";
const taskId = "55555555-5555-5555-5555-555555555555";
const env = {
  SLACK_SIGNING_SECRET: "unused",
  SLACK_TEAM_ID: "T1",
  SLACK_TARGET_USER_IDS: "U1",
  MULTICA_API_BASE_URL: "https://multica.test",
  MULTICA_API_TOKEN: "api-test",
  MULTICA_WORKSPACE_ID: workspace,
  MULTICA_PROJECT_ID: project,
  MULTICA_AGENT_ID: agent,
  SLACK_REACTION_TOKEN: "reaction-test",
  SLACK_REACTION_NAME: "eyes",
  KV_REST_API_URL: "https://kv.test",
  KV_REST_API_TOKEN: "kv-test",
  QSTASH_TOKEN: "queue-test",
  QSTASH_CURRENT_SIGNING_KEY: "current-signing-key",
  QSTASH_NEXT_SIGNING_KEY: "next-signing-key",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume",
  RELAY_FOOTER_ENABLED: "true",
  MULTICA_PLUGIN_INSTALLATION_ID: "66666666-6666-6666-6666-666666666666",
  MULTICA_PLUGIN_SIGNING_SECRET: "whsec_" + "ab".repeat(32),
  RELAY_REPLY_TOKEN: "r".repeat(32),
  SLACK_REPLY_TOKEN: "reply-test",
  SLACK_REPLY_ACTOR: "user",
};
const ref = {
  version: 1 as const,
  issueId,
  taskId,
  channelId: "C1",
  threadTs: "1788862400.000000",
  messageTs: "1788862800.000000",
};

function hook(bodyChange = {}, timestamp = Math.floor(Date.now() / 1000)) {
  const body = JSON.stringify({
    version: 1,
    invocation_id: "invocation",
    installation_id: env.MULTICA_PLUGIN_INSTALLATION_ID,
    workspace_id: workspace,
    issue_id: issueId,
    trigger: "event",
    hook_key: "slack-run-footer",
    event_type: "task.completed",
    input: {
      task_id: taskId,
      agent_id: agent,
      issue_id: issueId,
      status: "completed",
    },
    callback_token: "DO-NOT-PERSIST",
    ...bodyChange,
  });
  return new Request("https://relay.test/api/multica/events", {
    method: "POST",
    body,
    headers: {
      "x-multica-timestamp": String(timestamp),
      "x-multica-plugin-installation": env.MULTICA_PLUGIN_INSTALLATION_ID,
      "x-multica-signature":
        "v1=" +
        createHmac("sha256", Buffer.from("ab".repeat(32), "hex"))
          .update(`${timestamp}.${body}`)
          .digest("hex"),
    },
  });
}
function registration(value = ref) {
  return new Request("https://relay.test/api/slack/replies", {
    method: "POST",
    body: JSON.stringify(value),
    headers: { authorization: `Bearer ${env.RELAY_REPLY_TOKEN}` },
  });
}
function fixture() {
  const kv = new Map<string, string>();
  const scope = digest(`${workspace}:${project}:${agent}`);
  const event = {
    teamId: "T1",
    channelId: "C1",
    senderUserId: "U2",
    threadTs: ref.threadTs,
    messageTs: ref.threadTs,
    text: "<@U1> review",
    mention: { type: "user" as const, id: "U1" },
  };
  const issue = {
    id: issueId,
    workspace_id: workspace,
    project_id: project,
    assignee_id: agent,
    assignee_type: "agent",
    description: formatTaskDescription(
      event,
      `<!-- relay-thread:${scope}:${digest(`T1:C1:${ref.threadTs}`)} -->`,
    ),
  };
  const run = {
    id: taskId,
    issue_id: issueId,
    workspace_id: workspace,
    agent_id: agent,
    status: "completed",
    started_at: "2026-09-08T10:15:02Z",
    completed_at: "2026-09-08T10:21:32Z",
    usage: [
      {
        model: "gpt-6-astra",
        provider: "codex",
        input_tokens: 1000,
        output_tokens: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
      },
    ] as unknown,
  };
  const runs = [run];
  const logMessages: Record<string, unknown>[] = [
    {
      task_id: taskId,
      issue_id: issueId,
      seq: 1,
      type: "text",
      content: "done",
    },
  ];
  const runLogs = new Map([[taskId, logMessages]]);
  const message: Record<string, any> = {
    ts: ref.messageTs,
    thread_ts: ref.threadTs,
    user: "U1",
    text: "正文 **必须保留**",
    blocks: [
      {
        type: "rich_text",
        block_id: "body",
        elements: [
          {
            type: "rich_text_section",
            elements: [{ type: "text", text: "正文 必须保留" }],
          },
        ],
      },
    ],
    attachments: [{ fallback: "附件", text: "保留附件" }],
  };
  const messages = new Map<string, Record<string, any>>([
    [ref.messageTs, message],
  ]);
  const writes: Record<string, any>[] = [],
    urls: string[] = [];
  let loseUpdate = false,
    absentMessage = false;
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    urls.push(url);
    if (url === env.KV_REST_API_URL) {
      const command = JSON.parse(String(init?.body));
      if (command[0] === "GET")
        return Response.json({ result: kv.get(command[1]) ?? null });
      if (command[0] === "SET") {
        if (command[3] === "NX" && kv.has(command[1]))
          return Response.json({ result: null });
        kv.set(command[1], command[2]);
        return Response.json({ result: "OK" });
      }
      if (command[0] === "EVAL") {
        if (kv.get(command[3]) === command[4]) kv.delete(command[3]);
        return Response.json({ result: 1 });
      }
      throw new Error("unexpected Redis command");
    }
    if (url === `${env.MULTICA_API_BASE_URL}/api/issues/${issueId}`)
      return Response.json(issue);
    if (url === `${env.MULTICA_API_BASE_URL}/api/issues/${issueId}/task-runs`)
      return Response.json(runs);
    if (url.includes("/messages"))
      return Response.json(
        runLogs.get(new URL(url).pathname.split("/")[3]!) ?? [],
      );
    if (url.endsWith("/auth.test"))
      return Response.json({ ok: true, team_id: "T1", user_id: "U1" });
    if (new URL(url).pathname === "/api/conversations.replies") {
      expect(init?.method).toBe("GET");
      expect(init?.body).toBeUndefined();
      const body = Object.fromEntries(new URL(url).searchParams);
      expect(body.channel).toBe(ref.channelId);
      expect(body.ts).toBe(ref.threadTs);
      expect(body.inclusive).toBe("true");
      expect(body.limit).toBe("2");
      expect(body.latest).toBe(body.oldest);
      return Response.json({
        ok: true,
        messages: absentMessage ? [] : [messages.get(body.oldest)],
      });
    }
    if (url.endsWith("/chat.update")) {
      const body = JSON.parse(String(init?.body));
      writes.push(body);
      const existing = messages.get(body.ts)!;
      Object.assign(existing, { text: body.text, blocks: body.blocks });
      if (loseUpdate) {
        loseUpdate = false;
        throw new Error("response lost");
      }
      return Response.json({ ok: true, channel: body.channel, ts: body.ts });
    }
    throw new Error(`unexpected endpoint ${url}`);
  };
  return {
    fetcher,
    kv,
    issue,
    run,
    runs,
    logMessages,
    runLogs,
    message,
    messages,
    writes,
    urls,
    loseUpdate: () => {
      loseUpdate = true;
    },
    deleteMessage: () => {
      absentMessage = true;
    },
  };
}

async function drain() {
  await Promise.all(background.jobs.splice(0));
}
async function register(f: ReturnType<typeof fixture>) {
  const status = f.run.status;
  f.run.status = "running";
  const response = await registerSlackReply(registration(), env, f.fetcher);
  f.run.status = status;
  expect(response.status).toBe(200);
}
function worker(
  f: ReturnType<typeof fixture>,
  value = ref,
  fetcher = f.fetcher,
) {
  return processFooter(
    value,
    loadFooterConfig(env),
    new UpstashThreadStore(env.KV_REST_API_URL, env.KV_REST_API_TOKEN, fetcher),
    fetcher,
  );
}
afterEach(async () => {
  await drain();
  vi.restoreAllMocks();
});

describe("快速确认与单次后台执行", () => {
  it("回调不等待后台网络，200 返回后继续更新原消息", async () => {
    const f = fixture();
    await register(f);
    const original = structuredClone(f.message);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let blocked = false;
    const delayed: typeof fetch = async (url, init) => {
      if (String(url) === env.KV_REST_API_URL && !blocked) {
        blocked = true;
        await gate;
      }
      return f.fetcher(url, init);
    };
    try {
      const response = await acceptMulticaHook(hook(), env, delayed);
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ action: "accepted" });
      expect(background.jobs).toHaveLength(1);
      expect(f.writes).toHaveLength(0);
    } finally {
      release();
      await drain();
    }
    expect(f.writes).toHaveLength(1);
    expect(f.message.blocks.slice(0, -1)).toEqual(original.blocks);
    expect(f.writes[0]!.attachments).toEqual(original.attachments);
    expect(f.writes[0]!.text).toBe(
      "正文 **必须保留**\n\n:agent_time: 6m 30s · :agent_mdi_robot_outline_muted: gpt-6-astra: 1.0k tokens (0% cached) · :agent_tool: 0 tools · :agent_skill: 0 skills",
    );
    expect(
      f.urls.some(
        (url) => url.includes("qstash") || url.includes("/v2/publish/"),
      ),
    ).toBe(false);
    expect([...f.kv.values()].join(" ")).not.toContain("DO-NOT-PERSIST");
  });
  it("worker 失败不改变 200，不安排重试，重复事件也不再次更新", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    await register(f);
    const failing: typeof fetch = async (url, init) => {
      if (String(url).endsWith("/chat.update"))
        throw new Error("private upstream failure");
      return f.fetcher(url, init);
    };
    expect((await acceptMulticaHook(hook(), env, failing)).status).toBe(200);
    await drain();
    expect(warn).toHaveBeenCalledWith(
      "relay_footer_worker",
      expect.objectContaining({
        action: "failed",
        reason: "footer_unavailable",
      }),
    );
    expect(JSON.stringify(warn.mock.calls)).not.toContain(
      "private upstream failure",
    );
    await acceptMulticaHook(hook(), env, f.fetcher);
    await drain();
    expect(f.writes).toHaveLength(0);
    expect([...f.kv.keys()].some((key) => key.endsWith(":attempt"))).toBe(true);
  });
  it("事件落盘失败也只记录后台失败，回调仍确认成功", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const failing = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new Error("private"));
    expect((await acceptMulticaHook(hook(), env, failing)).status).toBe(200);
    await drain();
    expect(failing).toHaveBeenCalledTimes(1);
  });
  it("运行中登记只保存消息，漏掉 Hook 时不启动定时补偿", async () => {
    const f = fixture();
    await register(f);
    expect(background.jobs).toHaveLength(0);
    expect(f.writes).toHaveLength(0);
    expect(
      [...f.kv.keys()].some(
        (key) => key.endsWith(":recovery") || key.endsWith("pending"),
      ),
    ).toBe(false);
  });
  it("先完成后登记时，由登记请求启动一次后台处理", async () => {
    const f = fixture();
    await acceptMulticaHook(hook(), env, f.fetcher);
    await drain();
    expect(f.writes).toHaveLength(0);
    expect(
      (await registerSlackReply(registration(), env, f.fetcher)).status,
    ).toBe(200);
    await drain();
    expect(f.writes).toHaveLength(1);
  });
  it("并发重复回调仅领取一次更新机会", async () => {
    const f = fixture();
    await register(f);
    await Promise.all([
      acceptMulticaHook(hook(), env, f.fetcher),
      acceptMulticaHook(hook(), env, f.fetcher),
    ]);
    await drain();
    expect(f.writes).toHaveLength(1);
    await acceptMulticaHook(hook(), env, f.fetcher);
    await drain();
    expect(f.writes).toHaveLength(1);
  });
  it("更新响应丢失后不自动再写，原消息只有一个 footer", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    await register(f);
    f.loseUpdate();
    await acceptMulticaHook(hook(), env, f.fetcher);
    await drain();
    f.message.text = f.message.text.replace(
      "\n\n:agent_time:",
      "  :agent_time:",
    );
    await acceptMulticaHook(hook(), env, f.fetcher);
    await drain();
    expect(f.writes).toHaveLength(1);
    expect(f.message.blocks).toHaveLength(2);
    expect(
      (await registerSlackReply(registration(), env, f.fetcher)).status,
    ).toBe(200);
    await drain();
    expect(f.writes).toHaveLength(1);
  });
  it("旧队列和恢复入口直接返回 disabled", async () => {
    expect(await (await retireFooter()).json()).toEqual({ action: "disabled" });
    expect(background.jobs).toHaveLength(0);
  });
});

describe("统计缺失与范围保护", () => {
  it.each(["usage", "logs", "all"])(
    "%s 缺失时只展示当前可用数据，不延迟补查",
    async (missing) => {
      const f = fixture();
      await register(f);
      if (missing !== "logs") f.run.usage = undefined;
      if (missing !== "usage") f.logMessages.splice(0);
      if (missing === "all") f.run.completed_at = "";
      const result = await worker(f);
      expect(f.urls.filter((url) => url.includes("/messages"))).toHaveLength(1);
      expect(background.jobs).toHaveLength(0);
      if (missing === "all") {
        expect(result).toEqual({ action: "skipped", reason: "missing_stats" });
        expect(f.writes).toHaveLength(0);
      } else {
        expect(f.writes).toHaveLength(1);
        expect(f.writes[0]!.text.includes("tokens")).toBe(missing !== "usage");
        expect(f.writes[0]!.text.includes(":agent_tool:")).toBe(
          missing !== "logs",
        );
      }
    },
  );
  it("同一 issue 两次运行分别关联原回复", async () => {
    const f = fixture();
    const other = {
      ...ref,
      taskId: "77777777-7777-7777-7777-777777777777",
      messageTs: "1788862801.000000",
    };
    f.runs.push({ ...f.run, id: other.taskId });
    f.runLogs.set(
      other.taskId,
      f.logMessages.map((m) => ({ ...m, task_id: other.taskId })),
    );
    f.messages.set(other.messageTs, {
      ...structuredClone(f.message),
      ts: other.messageTs,
    });
    await register(f);
    await registerSlackReply(registration(other), env, f.fetcher);
    await drain();
    await worker(f);
    expect(f.writes.map((w) => w.ts)).toEqual([other.messageTs, ref.messageTs]);
  });
  it.each(["completed", "failed"])("%s 回调更新已登记回复", async (status) => {
    const f = fixture();
    await register(f);
    f.run.status = status;
    await acceptMulticaHook(
      hook({ event_type: `task.${status}` }),
      env,
      f.fetcher,
    );
    await drain();
    expect(f.writes).toHaveLength(1);
  });
  it("终态尚未可读时直接省略，不轮询", async () => {
    const f = fixture();
    await register(f);
    f.run.status = "running";
    expect(await worker(f)).toEqual({
      action: "skipped",
      reason: "run_not_terminal",
    });
    f.run.status = "completed";
    expect(await worker(f)).toEqual({ action: "duplicate" });
    expect(f.writes).toHaveLength(0);
  });
  it.each(["expired", "tampered", "wrong-installation"])(
    "%s Hook 不触发后台任务",
    async (mode) => {
      const request = hook(
        {},
        mode === "expired" ? Math.floor(Date.now() / 1000) - 301 : undefined,
      );
      if (mode === "tampered")
        request.headers.set("x-multica-signature", "wrong");
      if (mode === "wrong-installation")
        request.headers.set("x-multica-plugin-installation", "other");
      const f = fixture();
      expect((await acceptMulticaHook(request, env, f.fetcher)).status).toBe(
        401,
      );
      expect(f.urls).toHaveLength(0);
      expect(background.jobs).toHaveLength(0);
    },
  );
  it("其他 Agent 和取消事件忽略，不调用后台", async () => {
    const f = fixture();
    expect(
      await (
        await acceptMulticaHook(
          hook({ input: { agent_id: "other" } }),
          env,
          f.fetcher,
        )
      ).json(),
    ).toEqual({ action: "ignored" });
    expect(
      await (
        await acceptMulticaHook(
          hook({ event_type: "task.cancelled" }),
          env,
          f.fetcher,
        )
      ).json(),
    ).toEqual({ action: "ignored" });
    expect(background.jobs).toHaveLength(0);
  });
  it("错误登记凭据不访问上游", async () => {
    const f = fixture(),
      request = registration();
    request.headers.set("authorization", "Bearer wrong");
    expect((await registerSlackReply(request, env, f.fetcher)).status).toBe(
      401,
    );
    expect(f.urls).toHaveLength(0);
  });
  it.each(["project", "task-agent", "channel", "author", "missing-run"])(
    "拒绝 %s 不匹配的登记",
    async (mode) => {
      const f = fixture();
      if (mode === "project") f.issue.project_id = "other";
      if (mode === "task-agent") f.run.agent_id = "other";
      if (mode === "author") f.message.user = "OTHER";
      if (mode === "missing-run") f.runs.splice(0);
      expect(
        (
          await registerSlackReply(
            registration(
              mode === "channel" ? { ...ref, channelId: "C2" } : ref,
            ),
            env,
            f.fetcher,
          )
        ).status,
      ).toBe(409);
      expect(f.writes).toHaveLength(0);
      expect(background.jobs).toHaveLength(0);
    },
  );
  it.each(["body", "author", "scope"])(
    "登记后 %s 变化时不覆盖",
    async (change) => {
      const f = fixture();
      await register(f);
      if (change === "body") f.message.text = "人工修订";
      if (change === "author") f.message.user = "OTHER";
      if (change === "scope") f.issue.project_id = "OTHER";
      await expect(worker(f)).rejects.toThrow(
        change === "body"
          ? "footer_body_changed"
          : change === "author"
            ? "footer_author_mismatch"
            : "footer_scope_mismatch",
      );
      expect(f.writes).toHaveLength(0);
    },
  );
  it("字段顺序变化不视为人工编辑", async () => {
    const f = fixture();
    await register(f);
    const block = f.message.blocks[0];
    f.message.blocks[0] = {
      elements: block.elements,
      block_id: block.block_id,
      type: block.type,
    };
    expect(await worker(f)).toMatchObject({ action: "updated" });
  });
  it("同一运行不能登记另一条最终回复", async () => {
    const f = fixture();
    await register(f);
    const other = { ...ref, messageTs: "1788862801.000000" };
    f.messages.set(other.messageTs, {
      ...structuredClone(f.message),
      ts: other.messageTs,
    });
    expect(
      (await registerSlackReply(registration(other), env, f.fetcher)).status,
    ).toBe(409);
    expect(background.jobs).toHaveLength(0);
  });
  it.each(["empty", "full", "text"])(
    "%s 容量限制保留完整原文",
    async (mode) => {
      const f = fixture();
      if (mode === "empty") f.message.blocks = [];
      if (mode === "full")
        f.message.blocks = Array.from({ length: 50 }, () => ({
          type: "divider",
        }));
      if (mode === "text") f.message.text = "x".repeat(40000);
      await register(f);
      const original = structuredClone(f.message);
      expect(await worker(f)).toEqual({
        action: "skipped",
        reason: "unsupported_message",
      });
      expect(f.message).toEqual(original);
    },
  );
  it("开关关闭后不运行后台任务", async () => {
    const f = fixture();
    expect(
      (
        await acceptMulticaHook(
          hook(),
          { ...env, RELAY_FOOTER_ENABLED: "false" },
          f.fetcher,
        )
      ).status,
    ).toBe(404);
    expect(background.jobs).toHaveLength(0);
    expect(f.urls).toHaveLength(0);
  });
  it.each([
    ["2026-09-08T00:00:00Z", "2026-09-08T00:14:12Z", ":agent_time: 14m 12s"],
    ["invalid", "invalid", undefined],
  ])("耗时格式 %s %s", (start, end, expected) => {
    expect(
      buildDurationFooter({
        id: taskId,
        status: "completed",
        started_at: start,
        completed_at: end,
      }),
    ).toBe(expected);
  });
});
