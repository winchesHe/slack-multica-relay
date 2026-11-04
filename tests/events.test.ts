import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { acceptSlack, consumeQueue, consumerFailure, ADMISSION_BUDGET_MS } from "../src/http.js";
vi.mock("@upstash/qstash", () => ({
  Receiver: class {
    verify = vi.fn().mockResolvedValue(true);
  },
}));
const env = {
  SLACK_SIGNING_SECRET: "test",
  SLACK_TEAM_ID: "T1",
  SLACK_TARGET_USER_IDS: "U1",
  SLACK_ALLOWED_CHANNEL_IDS: "C1",
  MULTICA_API_BASE_URL: "https://multica.test",
  MULTICA_API_TOKEN: "test",
  MULTICA_WORKSPACE_ID: "ws",
  MULTICA_PROJECT_ID: "project",
  MULTICA_AGENT_ID: "agent",
  SLACK_USER_TOKEN: "test",
  SLACK_REACTION_NAME: "eyes",
  KV_REST_API_URL: "https://kv.test",
  KV_REST_API_TOKEN: "test",
  QSTASH_TOKEN: "test",
  QSTASH_CURRENT_SIGNING_KEY: "test",
  QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume",
};
function request(event: unknown, teamId = "T1"): Request {
  const body = JSON.stringify({
      type: "event_callback",
      team_id: teamId,
      event,
    }),
    ts = String(Math.floor(Date.now() / 1000));
  return new Request("https://relay.test/api/slack/events", {
    method: "POST",
    headers: {
      "x-slack-request-timestamp": ts,
      "x-slack-signature":
        "v0=" +
        createHmac("sha256", "test")
          .update("v0:" + ts + ":" + body)
          .digest("hex"),
    },
    body,
  });
}
const event = {
  type: "message",
  channel: "C1",
  user: "U2",
  ts: "100.000001",
  text: "<@U1> test",
};
// Existing routing cases use a human author; dedicated admission cases below exercise identity failures.
function acceptHumanMessage(req: Request, config: NodeJS.ProcessEnv, fetcher: typeof fetch) {
  return acceptSlack(req, config, async (input, init) => {
    if (String(input).startsWith("https://slack.com/api/users.info?")) {
      const id = new URL(String(input)).searchParams.get("user");
      return Response.json({ ok: true, user: { id, is_bot: false } });
    }
    return fetcher(input, init);
  });
}
afterEach(() => vi.restoreAllMocks());
describe("author-based admission", () => {
  const sourceMetadata = [{}, { bot_id: "B1" }, { app_id: "A1" }, { subtype: "bot_message" },
    { bot_id: "B1", app_id: "A1", subtype: "bot_message" }];
  it.each(sourceMetadata)("admits human messages regardless of source metadata %j", async (metadata) => {
    const fetcher = vi.fn<typeof fetch>(async (input) =>
      String(input).includes("/users.info?")
        ? Response.json({ ok: true, user: { id: "U2", is_bot: false } })
        : Response.json({ messageId: "msg" }));
    const response = await acceptSlack(request({ ...event, ...metadata }), env, fetcher);
    expect(await response.json()).toEqual({ action: "accepted", queueMessageId: "msg" });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toMatchObject({ senderUserId: "U2", operation: "dispatch" });
  });
  it.each(sourceMetadata)("rejects bot authors regardless of source metadata %j", async (metadata) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ok: true, user: { id: "U2", is_bot: true } }));
    const response = await acceptSlack(request({ ...event, ...metadata }), env, fetcher);
    expect(await response.json()).toEqual({ action: "ignored", reason: "bot_author" });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0]?.[0])).toContain("/users.info?user=U2");
  });
  it.each([
    { ok: false, error: "missing_scope" },
    { ok: true, user: { id: "U2" } },
    { ok: true, user: { id: "UOTHER", is_bot: false } },
  ])("returns retryable 503 without publishing an unknown author %j", async (body) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(body));
    const response = await acceptSlack(request(event), env, fetcher);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "author_unavailable", retryable: true });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("does not publish after a network failure or leak its details", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error("private upstream data"));
    expect((await acceptSlack(request(event), env, fetcher)).status).toBe(503);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(warn.mock.calls)).not.toContain("private upstream data");
  });
  it("uses the configured Bot token for lookup when present", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ok: true, user: { id: "U2", is_bot: true } }));
    await acceptSlack(request(event), { ...env, SLACK_BOT_TOKEN: "bot-test" }, fetcher);
    expect(fetcher.mock.calls[0]?.[1]?.headers).toMatchObject({ authorization: "Bearer bot-test" });
  });
  it.each([1000, ADMISSION_BUDGET_MS])("limits queue work after %i ms spent before publishing", async (elapsed) => {
    let now = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const timeout = vi.spyOn(AbortSignal, "timeout");
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      if (String(input).includes("/users.info?")) {
        now += elapsed;
        return Response.json({ ok: true, user: { id: "U2", is_bot: false } });
      }
      return Response.json({ messageId: "msg" });
    });
    const response = await acceptSlack(request(event), env, fetcher);
    if (elapsed === ADMISSION_BUDGET_MS) {
      expect(response.status).toBe(503);
      expect(fetcher).toHaveBeenCalledTimes(1);
    } else {
      expect(response.status).toBe(200);
      expect(timeout).toHaveBeenLastCalledWith(ADMISSION_BUDGET_MS - elapsed);
    }
  });
});
describe("durable admission", () => {
  it.each(["invalid_event","invalid_thread_state","ambiguous_issue_mapping","invalid_issue_scope",
    "relay_payload_too_large","task_presentation_too_large","comment_lookup_limit"])("acknowledges permanent consumer rejection %s",async(code)=>{
    const response=consumerFailure(code);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({action:"rejected",error:code,retryable:false});
  });
  it("keeps transient consumer failures retryable",async()=>{
    expect(consumerFailure("timeout").status).toBe(503);
  });
  it("acknowledges a signed but invalid queued event without retrying",async()=>{
    const response=await consumeQueue(new Request(env.RELAY_CONSUMER_URL,{method:"POST",body:"{}"}),env,vi.fn<typeof fetch>());
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({action:"rejected",error:"invalid_event",retryable:false});
  });
  it("only publishes to queue before acknowledging", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptHumanMessage(request(event), env, fetcher);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      action: "accepted",
      queueMessageId: "msg",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0]![0])).toContain(
      "/v2/publish/https://relay.test/api/queue/consume",
    );
  });
  it("projects attachment metadata before publishing to QStash and hashes log identity", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "msg" }));
    const files=[{id:"F1",name:"photo.png",mimetype:"image/png",size:12,
      url_private:"https://private.test/file",thumb_360:"https://private.test/thumb",shares:{private:{}}},
      ...Array.from({length:5},(_,i)=>({id:`F${i+2}`,name:`${i+2}.png`,mimetype:"image/png"}))];
    await acceptHumanMessage(request({...event,files}),env,fetcher);
    const queued=JSON.parse(String(fetcher.mock.calls[0]![1]?.body));
    expect(queued.files).toHaveLength(5);expect(queued.files[0]).toEqual({id:"F1",name:"photo.png",mime:"image/png",size:12,contentStatus:"not_loaded"});
    expect(queued.filesTruncated).toBe(true);
    expect(JSON.stringify(queued)).not.toContain("private.test");
    expect(JSON.stringify(info.mock.calls)).not.toContain(event.text);
  });
  it.each([
    { channel: "C2" },
    { user: undefined },
    { text: "ordinary", thread_ts: "1.000001" },
    { subtype: "message_changed" },
  ])("no queue side effects for %j", async (change) => {
    const fetcher = vi.fn<typeof fetch>();
    await acceptHumanMessage(request({ ...event, ...change }), env, fetcher);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("rejects another Slack team", async () => {
    const f = vi.fn<typeof fetch>();
    await acceptHumanMessage(request(event, "T2"), env, f);
    expect(f).not.toHaveBeenCalled();
  });
  it("accepts all as an explicit channel allowlist", async () => {
    const f = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptHumanMessage(
      request({ ...event, channel: "C2" }),
      { ...env, SLACK_ALLOWED_CHANNEL_IDS: "all" },
      f,
    );
    expect(response.status).toBe(200);
    expect(f).toHaveBeenCalledTimes(1);
  });
  it.each([undefined, "", ", ,"])(
    "fails closed for a missing or empty channel allowlist %s",
    async (value) => {
      const f = vi.fn<typeof fetch>();
      const candidate = { ...env, SLACK_ALLOWED_CHANNEL_IDS: value };
      expect((await acceptHumanMessage(request(event), candidate, f)).status).toBe(500);
      expect(f).not.toHaveBeenCalled();
    },
  );
  it("blocks a channel even when the allowlist is all", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptHumanMessage(
      request({ ...event, channel: "C2" }),
      {
        ...env,
        SLACK_ALLOWED_CHANNEL_IDS: "all",
        SLACK_BLOCKED_CHANNEL_IDS: "C2",
      },
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "not_allowed",
    });
    expect(f).not.toHaveBeenCalled();
  });
  it("applies sender policy", async () => {
    const f = vi.fn<typeof fetch>();
    await acceptHumanMessage(
      request(event),
      { ...env, SLACK_ALLOWED_SENDER_IDS: "U3" },
      f,
    );
    expect(f).not.toHaveBeenCalled();
  });
  it("blocks a sender even when the sender allowlist is all", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptHumanMessage(
      request(event),
      {
        ...env,
        SLACK_ALLOWED_SENDER_IDS: "all",
        SLACK_BLOCKED_SENDER_IDS: "U2",
      },
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "not_allowed",
    });
    expect(f).not.toHaveBeenCalled();
  });
  it("keeps Slack retry ownership when queue publish fails", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = vi.fn<typeof fetch>().mockRejectedValue(new Error("secret body"));
    const response = await acceptHumanMessage(request(event), env, f);
    expect(response.status).toBe(503);
    expect(JSON.stringify(vi.mocked(console.warn).mock.calls)).not.toContain(
      "secret body",
    );
  });
  it("rejects invalid signature before network", async () => {
    const f = vi.fn<typeof fetch>();
    expect(
      (
        await acceptHumanMessage(
          new Request("https://relay.test", { method: "POST", body: "{}" }),
          env,
          f,
        )
      ).status,
    ).toBe(401);
    expect(f).not.toHaveBeenCalled();
  });
  it("rechecks channel policy for queued messages", async () => {
    const f = vi.fn<typeof fetch>();
    const queued = {
      teamId: "T1",
      channelId: "C2",
      senderUserId: "U2",
      messageTs: "1.000001",
      threadTs: "1.000001",
      text: "<@U1> test",
      mention: { type: "user", id: "U1" },
    };
    const response = await consumeQueue(
      new Request(env.RELAY_CONSUMER_URL, {
        method: "POST",
        body: JSON.stringify(queued),
      }),
      env,
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "policy_changed",
    });
    expect(f).not.toHaveBeenCalled();
  });
});

describe("取消指令的事件路由", () => {
  it("授权取消入队并保存指令类型", async () => {
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "cancel-msg" }));
    const response = await acceptHumanMessage(request({ ...event, user: "U1", text: "<@U1> CANCEL", thread_ts: event.ts }), env, f);
    expect(response.status).toBe(200);
    expect(JSON.parse(String(f.mock.calls[0]![1]!.body))).toMatchObject({ operation: "cancel", senderUserId: "U1" });
  });
  it("非目标用户的取消指令不会入队或作为新任务处理", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptHumanMessage(request({ ...event, text: "<@U1> 取消" }), env, f);
    expect(await response.json()).toEqual({ action: "ignored", reason: "cancel_not_allowed" });
    expect(f).not.toHaveBeenCalled();
  });
  it("自定义关键词替换默认值，普通讨论仍入队为 dispatch", async () => {
    for (const [text, operation] of [["<@U1> stop", "cancel"], ["<@U1> cancel", "dispatch"], ["<@U1> 帮我取消", "dispatch"]]) {
      const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "msg" }));
      await acceptHumanMessage(request({ ...event, user: "U1", text }), { ...env, SLACK_CANCEL_KEYWORDS: "stop,停止" }, f);
      expect(JSON.parse(String(f.mock.calls[0]![1]!.body)).operation).toBe(operation);
    }
  });
  it("消费时重新核对取消身份，不能借队列绕过权限", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify({
      teamId: "T1", channelId: "C1", senderUserId: "U2", messageTs: "102.000001", threadTs: "100.000001",
      text: "<@U1> cancel", mention: { type: "user", id: "U1" }, operation: "cancel",
    }) }), env, f);
    expect(await response.json()).toEqual({ action: "ignored", reason: "cancel_not_allowed" });
    expect(f).not.toHaveBeenCalled();
  });
  it.each(["cancel", undefined])("消费取消 %s 不建卡、不加 reaction；已分类取消不随关键词变更变成任务", async (operation) => {
    const calls: string[] = [];
    const kv = new Map<string, string>();
    const f: typeof fetch = async (input, init) => {
      const url = String(input); calls.push(url);
      if (url === env.KV_REST_API_URL) {
        const [command, key, value] = JSON.parse(String(init?.body));
        if (command === "GET") return Response.json({ result: kv.get(key) ?? null });
        if (command === "SET") { kv.set(key, value); return Response.json({ result: "OK" }); }
        if (command === "EVAL") return Response.json({ result: 1 });
      }
      if (url.includes("/api/issues/search?")) return Response.json({ issues: [] });
      throw new Error("不应调用建卡或 reaction 接口");
    };
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify({
      teamId: "T1", channelId: "C1", senderUserId: "U1", messageTs: "102.000001", threadTs: "100.000001",
      text: "<@U1> cancel", mention: { type: "user", id: "U1" }, operation,
    }) }), operation ? { ...env, SLACK_CANCEL_KEYWORDS: "stop" } : env, f);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ action: "ignored" });
    expect(calls.every((url) => url === env.KV_REST_API_URL || url.includes("/api/issues/search?"))).toBe(true);
  });
});

describe("Team configuration admission", () => {
  it("accepts a Team without the legacy agent variable", async () => {
    const { MULTICA_AGENT_ID: _legacy, ...base } = env;
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "m" }));
    expect((await acceptHumanMessage(request(event), { ...base, MULTICA_ASSIGNEE_TYPE: "squad", MULTICA_ASSIGNEE_ID: "team" }, f)).status).toBe(200);
  });
  it.each([
    { MULTICA_ASSIGNEE_TYPE: "unknown", MULTICA_ASSIGNEE_ID: "team" },
    { MULTICA_ASSIGNEE_TYPE: "squad" },
    { MULTICA_ASSIGNEE_TYPE: "squad", MULTICA_ASSIGNEE_ID: "team", MULTICA_LEGACY_AGENT_ID: "agent", MULTICA_THREAD_SCOPE_ID: "wrong" },
  ])("rejects invalid Team configuration %j", async (extra) => {
    const f = vi.fn<typeof fetch>();
    expect((await acceptHumanMessage(request(event), { ...env, ...extra }, f)).status).toBe(500);
    expect(f).not.toHaveBeenCalled();
  });
});
