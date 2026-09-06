import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { acceptSlack, consumeQueue } from "../src/http.js";
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
  SLACK_REACTION_TOKEN: "test",
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
afterEach(() => vi.restoreAllMocks());
describe("durable admission", () => {
  it("only publishes to queue before acknowledging", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptSlack(request(event), env, fetcher);
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
  it.each([
    { channel: "C2" },
    { user: undefined },
    { text: "ordinary", thread_ts: "1.000001" },
    { bot_id: "B1" },
    { subtype: "message_changed" },
  ])("no queue side effects for %j", async (change) => {
    const fetcher = vi.fn<typeof fetch>();
    await acceptSlack(request({ ...event, ...change }), env, fetcher);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("rejects another Slack team", async () => {
    const f = vi.fn<typeof fetch>();
    await acceptSlack(request(event, "T2"), env, f);
    expect(f).not.toHaveBeenCalled();
  });
  it("accepts all as the channel allowlist", async () => {
    const f = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptSlack(
      request({ ...event, channel: "C2" }),
      { ...env, SLACK_ALLOWED_CHANNEL_IDS: "all" },
      f,
    );
    expect(response.status).toBe(200);
    expect(f).toHaveBeenCalledTimes(1);
  });
  it("defaults the channel allowlist to all when omitted", async () => {
    const f = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const { SLACK_ALLOWED_CHANNEL_IDS: _ignored, ...withoutChannelAllowlist } = env;
    const response = await acceptSlack(
      request({ ...event, channel: "C2" }),
      withoutChannelAllowlist,
      f,
    );
    expect(response.status).toBe(200);
    expect(f).toHaveBeenCalledTimes(1);
  });
  it("blocks a channel even when the allowlist is all", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptSlack(
      request({ ...event, channel: "C2" }),
      { ...env, SLACK_ALLOWED_CHANNEL_IDS: "all", SLACK_BLOCKED_CHANNEL_IDS: "C2" },
      f,
    );
    expect(await response.json()).toEqual({ action: "ignored", reason: "not_allowed" });
    expect(f).not.toHaveBeenCalled();
  });
  it("applies sender policy", async () => {
    const f = vi.fn<typeof fetch>();
    await acceptSlack(
      request(event),
      { ...env, SLACK_ALLOWED_SENDER_IDS: "U3" },
      f,
    );
    expect(f).not.toHaveBeenCalled();
  });
  it("blocks a sender even when the sender allowlist is all", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptSlack(
      request(event),
      { ...env, SLACK_ALLOWED_SENDER_IDS: "all", SLACK_BLOCKED_SENDER_IDS: "U2" },
      f,
    );
    expect(await response.json()).toEqual({ action: "ignored", reason: "not_allowed" });
    expect(f).not.toHaveBeenCalled();
  });
  it("keeps Slack retry ownership when queue publish fails", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = vi.fn<typeof fetch>().mockRejectedValue(new Error("secret body"));
    const response = await acceptSlack(request(event), env, f);
    expect(response.status).toBe(503);
    expect(warning).toHaveBeenCalledWith("relay_admission", {
      messageKey: "T1:C1:100.000001",
      reason: "upstream_failed",
      errorType: "Error",
      abortTimeoutSupported: true,
      durationMs: expect.any(Number),
    });
    expect(JSON.stringify(warning.mock.calls)).not.toContain("secret body");
  });
  it("logs the QStash status without logging its response body", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response("sensitive upstream body", { status: 401 }));
    const response = await acceptSlack(request(event), env, f);
    expect(response.status).toBe(503);
    expect(warning).toHaveBeenCalledWith("relay_admission", {
      messageKey: "T1:C1:100.000001",
      reason: "queue_publish_failed",
      errorType: "Error",
      queueStatus: 401,
      abortTimeoutSupported: true,
      durationMs: expect.any(Number),
    });
    expect(JSON.stringify(warning.mock.calls)).not.toContain(
      "sensitive upstream body",
    );
  });
  it("classifies request-construction errors without logging their message", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("invalid header contains a secret"));
    await acceptSlack(request(event), env, f);
    expect(warning).toHaveBeenCalledWith("relay_admission", {
      messageKey: "T1:C1:100.000001",
      reason: "upstream_failed",
      errorType: "TypeError",
      typeErrorCategory: "invalid_header",
      abortTimeoutSupported: true,
      durationMs: expect.any(Number),
    });
    expect(JSON.stringify(warning.mock.calls)).not.toContain(
      "invalid header contains a secret",
    );
  });
  it("rejects invalid signature before network", async () => {
    const f = vi.fn<typeof fetch>();
    expect(
      (
        await acceptSlack(
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
