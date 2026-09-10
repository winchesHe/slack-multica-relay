import { describe, expect, it, vi } from "vitest";
import {
  findComment,
  findIssue,
  createIssue,
  type ApiConfig,
} from "../src/multica-api.js";
const config: ApiConfig = {
  multicaApiBaseUrl: "https://multica.test",
  multicaApiToken: "test",
  multicaWorkspaceId: "ws",
  multicaProjectId: "project",
  multicaAgentId: "agent",
};
describe("Multica API contract", () => {
  it("recovers an Issue through a bounded marker search", async () => {
    const marker = `<!-- relay-thread:${"a".repeat(64)}:${"b".repeat(64)} -->`;
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({issues:[{
      id:"issue", title:"Slack", description:marker+"\n{}", project_id:"project",
      assignee_id:"agent", assignee_type:"agent",
    }]}));
    expect((await findIssue(config,marker,f))?.id).toBe("issue");
    const url=new URL(String(f.mock.calls[0]![0]));
    expect(url.pathname).toBe("/api/issues/search");
    expect(url.searchParams.get("q")).toBe("b".repeat(64));
    expect(url.searchParams.get("include_closed")).toBe("true");
  });
  it("uses ordinary Issue creation with unique title and no Autopilot", async () => {
    const f = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ id: "issue", title: "Slack C1 1" }));
    await createIssue(config, "Slack C1 1", "marker", f);
    expect(f.mock.calls[0]![0]).toBe("https://multica.test/api/issues");
    expect(JSON.parse(String(f.mock.calls[0]![1]?.body))).toMatchObject({
      project_id: "project",
      assignee_id: "agent",
      assignee_type: "agent",
    });
  });
  it("follows Multica comment cursor headers when recovering writes", async () => {
    let calls = 0;
    const f: typeof fetch = async (input) => {
      calls++;
      if (calls === 1)
        return Response.json([], {
          headers: {
            "X-Multica-Next-Before": "2026-09-01T00:00:00Z",
            "X-Multica-Next-Before-Id": "old",
          },
        });
      expect(String(input)).toContain("before_id=old");
      return Response.json([{ id: "c", content: "marker" }]);
    };
    expect((await findComment(config, "issue", "marker", f))?.id).toBe("c");
    expect(calls).toBe(2);
  });
  it("rejects malformed list instead of treating missing evidence as no match", async () => {
    await expect(
      findComment(config, "issue", "marker", async () =>
        Response.json({ unexpected: [] }),
      ),
    ).rejects.toThrow("invalid_multica_response");
  });
  it("accepts same-time cursor advancement with different IDs", async () => {
    let calls = 0;
    const fetcher: typeof fetch = async () => {
      calls++;
      return calls < 3
        ? Response.json([], {
            headers: {
              "X-Multica-Next-Before": "2026-09-01T00:00:00Z",
              "X-Multica-Next-Before-Id": "id-" + calls,
            },
          })
        : Response.json([{ id: "c", content: "marker" }]);
    };
    expect((await findComment(config, "issue", "marker", fetcher))?.id).toBe(
      "c",
    );
  });
});
