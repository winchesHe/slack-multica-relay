import { afterEach, describe, expect, it, vi } from "vitest";
import type { SlackThreadEvent } from "../src/thread-router.js";
import { consumeQueue } from "../src/http.js";
import { env, fixture, reply } from "./fixtures/extra-routes.js";
vi.mock("@upstash/qstash", () => ({ Receiver: class { verify = vi.fn().mockResolvedValue(true); } }));
afterEach(() => vi.restoreAllMocks());
function request(event: SlackThreadEvent = reply) {
  return new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify(event) });
}
describe("消费端 A2 路由与重试", () => {
  it("真实消费链路选择次级 Issue，并对探测与写请求应用有界 signal", async () => {
    const f = fixture(); f.issues.push(f.bound);
    const response = await consumeQueue(request(), env, f.fetcher);
    expect(await response.json()).toMatchObject({ action: "comment_persisted", routeId: "dorea", issueId: "lab-issue" });
    expect(f.calls.filter((call) => call.workspace).every((call) => call.workspace === "lab" && !!call.signal)).toBe(true);
  });
  it.each(["5xx", "timeout", "network", "redis"])("探测 %s 返回可重试 503，绝不触碰主路由", async (kind) => {
    const f = fixture();
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetcher: typeof fetch = async (input, init) => {
      if ((kind === "redis" && String(input) === env.KV_REST_API_URL) || String(input).includes("/search?")) {
        if (kind === "5xx" || kind === "redis") return Response.json({}, { status: 503 });
        throw kind === "timeout" ? new DOMException("test", "TimeoutError") : new TypeError("network");
      }
      return f.fetcher(input, init);
    };
    const response = await consumeQueue(request(), env, fetcher);
    expect(response.status).toBe(503); expect(await response.json()).toMatchObject({ retryable: true });
    expect(f.calls.some((call) => call.workspace === "grm")).toBe(false);
    expect(f.issues).toHaveLength(0);
  });
  it("次级配置不放宽 Slack 频道与取消发送者准入", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const response = await consumeQueue(request(), { ...env, SLACK_BLOCKED_CHANNEL_IDS: reply.channelId }, fetcher);
    expect(await response.json()).toMatchObject({ action: "ignored", reason: "policy_changed" });
    const cancel = await consumeQueue(request({ ...reply, senderUserId: "U2", text: "<@U1> cancel", operation: "cancel" }), env, fetcher);
    expect(await cancel.json()).toMatchObject({ action: "ignored", reason: "cancel_not_allowed" });
    expect(fetcher).not.toHaveBeenCalled();
  });
});
