import { createHmac } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { serveEdgeOne } from "../src/edgeone.js";
import { verifySlackSignature } from "../src/signature.js";

describe("EdgeOne request adapter", () => {
  it("verifies original escaped bytes despite a parsed body property", async () => {
    const raw = '{"text":"\\u4f60\\u597d"}';
    const timestamp = String(Math.floor(Date.now() / 1000));
    const signature = "v0=" + createHmac("sha256", "test")
      .update(`v0:${timestamp}:${raw}`).digest("hex");
    const request = new Request("https://relay.test/api/slack/events", {
      method: "POST", body: raw,
    });
    Object.defineProperty(request, "body", { value: JSON.parse(raw) });
    const result = await serveEdgeOne(request, async (normalized) => {
      const text = await normalized.text();
      expect(text).toBe(raw);
      expect(verifySlackSignature(text, timestamp, signature, "test")).toBe(true);
      return new Response("ok");
    });
    expect(result.status).toBe(200);
  });

  it("rejects oversized bodies without invoking business logic", async () => {
    const handler = vi.fn();
    const result = await serveEdgeOne(new Request("https://relay.test", {
      method: "POST", body: "x".repeat(256 * 1024 + 1),
    }), handler);
    expect(result.status).toBe(413);
    expect(handler).not.toHaveBeenCalled();
  });

  it("reports consumed bodies as unavailable rather than oversized", async () => {
    const request = new Request("https://relay.test", { method: "POST", body: "{}" });
    await request.text();
    const handler = vi.fn();
    const result = await serveEdgeOne(request, handler);
    expect(result.status).toBe(400);
    expect(await result.json()).toEqual({ error: "raw_body_unavailable" });
    expect(handler).not.toHaveBeenCalled();
  });
});
