import { describe, expect, it, vi } from "vitest";
import { lookupSlackAuthor, SlackAuthorLookupError } from "../src/slack-author.js";

describe("Slack author lookup", () => {
  it.each([true, false])("validates and returns is_bot=%s", async (isBot) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json({ ok: true, user: { id: "U1", is_bot: isBot } }),
    );
    expect(await lookupSlackAuthor("U1", "test", fetcher)).toEqual({ id: "U1", isBot });
    expect(fetcher.mock.calls[0]?.[1]?.headers).toMatchObject({ authorization: "Bearer test" });
    expect(fetcher.mock.calls[0]?.[1]?.signal).toBeInstanceOf(AbortSignal);
  });
  it.each([
    { ok: false, error: "missing_scope" },
    { ok: true },
    { ok: true, user: { id: "U1" } },
    { ok: true, user: { id: "U1", is_bot: "false" } },
    { ok: true, user: { id: "UOTHER", is_bot: false } },
  ])("rejects an unconfirmed author %j", async (body) => {
    await expect(lookupSlackAuthor("U1", "test", vi.fn<typeof fetch>().mockResolvedValue(Response.json(body))))
      .rejects.toMatchObject({ code: "invalid_response" });
  });
  it.each([
    new Response("rate limited", { status: 429 }),
    new Response("not json"),
  ])("rejects HTTP and decoding failures", async (response) => {
    await expect(lookupSlackAuthor("U1", "test", vi.fn<typeof fetch>().mockResolvedValue(response)))
      .rejects.toBeInstanceOf(SlackAuthorLookupError);
  });
  it("rejects a timed out lookup", async () => {
    const fetcher: typeof fetch = async (_input, init) => new Promise((_resolve, reject) => {
      const signal = init!.signal!;
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    });
    await expect(lookupSlackAuthor("U1", "test", fetcher)).rejects.toMatchObject({ code: "request_failed" });
  });
});
