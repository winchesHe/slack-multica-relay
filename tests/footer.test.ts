import { createHash, createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  acceptMulticaHook,
  consumeFooter,
  registerSlackReply,
} from "../src/footer.js";
import { buildDurationFooter } from "../src/footer-stats.js";
import { digest } from "../src/thread-router.js";
import { formatTaskDescription } from "../src/task-presentation.js";

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
  RELAY_FOOTER_CONSUMER_URL: "https://relay.test/api/queue/footer",
};
const ref = {
  version: 1,
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
function queued(
  value: unknown = { version: 1, issueId, taskId },
  key = env.QSTASH_CURRENT_SIGNING_KEY,
  url = env.RELAY_FOOTER_CONSUMER_URL,
) {
  const body = JSON.stringify(value),
    now = Math.floor(Date.now() / 1000);
  const encode = (v: unknown) =>
    Buffer.from(JSON.stringify(v)).toString("base64url");
  const token = `${encode({ alg: "HS256", typ: "JWT" })}.${encode({
    iss: "Upstash",
    sub: url,
    iat: now,
    nbf: now - 1,
    exp: now + 60,
    body: createHash("sha256").update(body).digest("base64url"),
  })}`;
  const signature = `${token}.${createHmac("sha256", key).update(token).digest("base64url")}`;
  return new Request(env.RELAY_FOOTER_CONSUMER_URL, {
    method: "POST",
    body,
    headers: { "upstash-signature": signature },
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
    usage: undefined as unknown,
  };
  const runs = [run];
  const logMessages: Record<string, unknown>[] = [];
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
    published: unknown[] = [],
    urls: string[] = [];
  let queueFails = false,
    loseUpdate = false,
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
    if (url.includes("/v2/publish/")) {
      if (queueFails)
        return Response.json({ error: "unavailable" }, { status: 503 });
      published.push(JSON.parse(String(init?.body)));
      return Response.json({ messageId: "queue-message" });
    }
    if (url === `${env.MULTICA_API_BASE_URL}/api/issues/${issueId}`)
      return Response.json(issue);
    if (url === `${env.MULTICA_API_BASE_URL}/api/issues/${issueId}/task-runs`)
      return Response.json(runs);
    if (url.includes("/messages")) return Response.json(logMessages);
    if (url.endsWith("/auth.test"))
      return Response.json({ ok: true, team_id: "T1", user_id: "U1" });
    if (url.endsWith("/conversations.replies")) {
      const body = JSON.parse(String(init?.body));
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
    message,
    messages,
    writes,
    published,
    urls,
    failQueue: () => {
      queueFails = true;
    },
    restoreQueue: () => {
      queueFails = false;
    },
    loseUpdate: () => {
      loseUpdate = true;
    },
    deleteMessage: () => {
      absentMessage = true;
    },
  };
}
afterEach(() => vi.restoreAllMocks());

describe("完成通知到原消息的集成链路", () => {
  it("登记、验签、队列消费后只给原消息添加耗时，并保留正文和附件", async () => {
    const f = fixture(),
      body = structuredClone(f.message.blocks);
    f.run.status = "running";
    expect(
      (await registerSlackReply(registration(), env, f.fetcher)).status,
    ).toBe(200);
    expect(f.published).toHaveLength(0);
    f.run.status = "completed";
    expect((await acceptMulticaHook(hook(), env, f.fetcher)).status).toBe(202);
    expect(f.published).toEqual([{ version: 1, issueId, taskId }]);
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(200);
    expect(f.writes).toHaveLength(1);
    expect(f.writes[0]!.blocks.slice(0, -1)).toEqual(body);
    expect(f.writes[0]!.text).toBe("正文 **必须保留**\n\n:agent_time: 6m 30s");
    expect(f.writes[0]!.attachments).toEqual(f.message.attachments);
    expect(f.writes[0]!.ts).toBe(ref.messageTs);
    expect([...f.kv.values()].join(" ")).not.toContain("DO-NOT-PERSIST");
    expect(f.urls.some((url) => url.includes("/api/agents/"))).toBe(false);
  });
  it("完整统计更新原消息，重复消费和响应丢失均复用统计快照", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    f.run.usage = [
      {
        model: "gpt-6-astra",
        provider: "codex",
        input_tokens: 132436,
        output_tokens: 9555,
        cache_read_tokens: 3114496,
        cache_write_tokens: 0,
      },
    ];
    f.logMessages.push(
      {
        seq: 1,
        task_id: taskId,
        issue_id: issueId,
        type: "tool_use",
        tool: "exec_command",
        input: { command: "cat /skills/slack/SKILL.md" },
      },
      {
        seq: 2,
        task_id: taskId,
        issue_id: issueId,
        type: "tool_result",
        tool: "exec_command",
        output: "---\nname: slack\n---\nprivate skill body",
      },
    );
    await registerSlackReply(registration(), env, f.fetcher);
    expect(f.urls.filter((url) => url.includes("/messages"))).toHaveLength(0);
    f.loseUpdate();
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(503);
    f.run.usage = [];
    f.logMessages.splice(0);
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(200);
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(200);
    expect(f.writes).toHaveLength(1);
    expect(f.writes[0]!.text).toBe(
      "正文 **必须保留**\n\n:agent_time: 6m 30s · :agent_mdi_robot_outline: gpt-6-astra: 3256.5k tokens (96% cached) · :agent_tool: 1 tools · :agent_skill: 1 skills",
    );
    expect(f.urls.filter((url) => url.includes("/messages"))).toHaveLength(1);
    expect([...f.kv.values()].join(" ")).not.toContain("private skill body");
  });
  it("完成通知先到且已消费，之后登记仍能补上 footer", async () => {
    const f = fixture();
    await acceptMulticaHook(hook(), env, f.fetcher);
    expect(
      await (await consumeFooter(queued(), env, f.fetcher)).json(),
    ).toEqual({ action: "waiting_for_reply" });
    expect(f.writes).toHaveLength(0);
    await registerSlackReply(registration(), env, f.fetcher);
    expect(f.published).toHaveLength(2);
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(200);
    expect(f.writes).toHaveLength(1);
  });
  it("重复通知和消费不会追加第二个 footer 或发送新消息", async () => {
    const f = fixture();
    await registerSlackReply(registration(), env, f.fetcher);
    await consumeFooter(queued(), env, f.fetcher);
    expect(
      await (await acceptMulticaHook(hook(), env, f.fetcher)).json(),
    ).toEqual({ action: "duplicate" });
    expect(
      await (await consumeFooter(queued(), env, f.fetcher)).json(),
    ).toEqual({ action: "duplicate" });
    expect(f.writes).toHaveLength(1);
    expect(f.urls.some((url) => url.endsWith("chat.postMessage"))).toBe(false);
  });
  it("Slack 更新成功但响应丢失时，下一次先回读确认，不重复更新", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    await registerSlackReply(registration(), env, f.fetcher);
    f.loseUpdate();
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(503);
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(200);
    expect(f.writes).toHaveLength(1);
  });
  it("入队失败保留事件且返回 503，重投后可消费", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    f.failQueue();
    expect((await acceptMulticaHook(hook(), env, f.fetcher)).status).toBe(503);
    expect([...f.kv.keys()].some((key) => key.endsWith(":event"))).toBe(true);
    f.restoreQueue();
    expect((await acceptMulticaHook(hook(), env, f.fetcher)).status).toBe(202);
  });
  it("登记已落盘但入队失败，相同回执重试不会冲突", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    f.failQueue();
    expect(
      (await registerSlackReply(registration(), env, f.fetcher)).status,
    ).toBe(503);
    f.restoreQueue();
    expect(
      (await registerSlackReply(registration(), env, f.fetcher)).status,
    ).toBe(200);
  });
  it("同一个 issue 的两次运行乱序完成时分别更新各自的消息", async () => {
    const f = fixture();
    const other = {
      ...ref,
      taskId: "77777777-7777-7777-7777-777777777777",
      messageTs: "1788862801.000000",
    };
    f.runs.push({ ...f.run, id: other.taskId });
    f.messages.set(other.messageTs, {
      ...structuredClone(f.message),
      ts: other.messageTs,
    });
    await registerSlackReply(registration(), env, f.fetcher);
    await registerSlackReply(registration(other), env, f.fetcher);
    await consumeFooter(
      queued({ version: 1, issueId, taskId: other.taskId }),
      env,
      f.fetcher,
    );
    await consumeFooter(queued(), env, f.fetcher);
    expect(f.writes.map((w) => w.ts)).toEqual([other.messageTs, ref.messageTs]);
  });
});

describe("身份、签名与正文边界", () => {
  it("Slack 返回对象字段顺序变化不会被误判为正文编辑", async () => {
    const f = fixture();
    await registerSlackReply(registration(), env, f.fetcher);
    const block = f.message.blocks[0];
    f.message.blocks[0] = {
      elements: block.elements,
      block_id: block.block_id,
      type: block.type,
    };
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(200);
    expect(f.writes).toHaveLength(1);
  });
  it.each(["expired", "tampered", "wrong-installation"])(
    "拒绝 %s Hook，且不调用外部服务",
    async (mode) => {
      const request = hook(
        {},
        mode === "expired" ? Math.floor(Date.now() / 1000) - 301 : undefined,
      );
      if (mode === "tampered")
        request.headers.set("x-multica-signature", "v1=wrong");
      if (mode === "wrong-installation")
        request.headers.set("x-multica-plugin-installation", "other");
      const f = fixture();
      expect((await acceptMulticaHook(request, env, f.fetcher)).status).toBe(
        401,
      );
      expect(f.urls).toHaveLength(0);
    },
  );
  it("忽略其他 Agent 的完成事件", async () => {
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
    expect(f.urls).toHaveLength(0);
  });
  it("使用真实 QStash Receiver 验证签名和目标 URL，不接受另一消费接口的签名", async () => {
    const f = fixture();
    expect(
      (await consumeFooter(queued(undefined, "wrong-key"), env, f.fetcher))
        .status,
    ).toBe(401);
    expect(
      (
        await consumeFooter(
          queued(
            undefined,
            env.QSTASH_CURRENT_SIGNING_KEY,
            env.RELAY_CONSUMER_URL,
          ),
          env,
          f.fetcher,
        )
      ).status,
    ).toBe(401);
    expect(f.urls).toHaveLength(0);
    expect(
      (
        await consumeFooter(
          queued(undefined, env.QSTASH_NEXT_SIGNING_KEY),
          env,
          f.fetcher,
        )
      ).status,
    ).toBe(200);
  });
  it("登记凭据不匹配时不查任务和 Slack", async () => {
    const f = fixture(),
      req = registration();
    req.headers.set("authorization", "Bearer wrong");
    expect((await registerSlackReply(req, env, f.fetcher)).status).toBe(401);
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
      const req = registration(
        mode === "channel" ? { ...ref, channelId: "C2" } : ref,
      );
      expect((await registerSlackReply(req, env, f.fetcher)).status).toBe(409);
      expect(f.writes).toHaveLength(0);
      expect([...f.kv.keys()].some((key) => key.endsWith(":reply"))).toBe(
        false,
      );
    },
  );
  it("不覆盖另一条最终回复的登记", async () => {
    const f = fixture();
    await registerSlackReply(registration(), env, f.fetcher);
    const other = { ...ref, messageTs: "1788862801.000000" };
    f.messages.set(other.messageTs, {
      ...structuredClone(f.message),
      ts: other.messageTs,
    });
    expect(
      (await registerSlackReply(registration(other), env, f.fetcher)).status,
    ).toBe(409);
  });
  it("正文在登记后被编辑时不覆盖新正文", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = fixture();
    await registerSlackReply(registration(), env, f.fetcher);
    f.message.text = "人工修订";
    expect((await consumeFooter(queued(), env, f.fetcher)).status).toBe(503);
    expect(f.writes).toHaveLength(0);
  });
  it("时间缺失时不生成占位 footer", async () => {
    const f = fixture();
    await registerSlackReply(registration(), env, f.fetcher);
    f.run.completed_at = "";
    expect(
      await (await consumeFooter(queued(), env, f.fetcher)).json(),
    ).toEqual({ action: "skipped", reason: "missing_stats" });
    expect(f.writes).toHaveLength(0);
  });
  it("已有 50 个 blocks 时不截断正文", async () => {
    const f = fixture();
    f.message.blocks = Array.from({ length: 50 }, () => ({ type: "divider" }));
    await registerSlackReply(registration(), env, f.fetcher);
    expect(
      await (await consumeFooter(queued(), env, f.fetcher)).json(),
    ).toEqual({ action: "skipped", reason: "unsupported_message" });
    expect(f.writes).toHaveLength(0);
  });
  it("关闭开关后新增接口不可用且不调用外部服务", async () => {
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
    expect(f.urls).toHaveLength(0);
  });
  it.each([
    ["2026-09-08T00:00:00Z", "2026-09-08T00:14:12Z", ":agent_time: 14m 12s"],
    ["2026-09-08T00:00:00Z", "2026-09-08T00:00:12Z", ":agent_time: 12s"],
    ["invalid", "invalid", undefined],
    ["2026-09-09", "2026-09-08", undefined],
  ])("耗时格式与边界 %s %s", (start, end, expected) => {
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
