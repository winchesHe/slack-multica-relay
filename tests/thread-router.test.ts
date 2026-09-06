import { describe, expect, it } from "vitest";
import {
  routeSlackThreadEvent,
  type SlackThreadEvent,
  type ThreadRouterConfig,
} from "../src/thread-router.js";
import { MemoryThreadStore } from "../src/thread-store.js";
const root: SlackThreadEvent = {
  teamId: "T1",
  channelId: "C1",
  messageTs: "100.000001",
  threadTs: "100.000001",
  senderUserId: "U2",
  text: "<@U1> test",
  mention: { type: "user", id: "U1" },
};
function fixture() {
  const issues: {
    id: string;
    title: string;
    description: string;
    project_id: string;
    assignee_type: string;
    assignee_id: string;
  }[] = [];
  const comments: { id: string; content: string }[] = [];
  let issuePosts = 0,
    commentPosts = 0,
    failIssue = false,
    failComment = false;
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.includes("/api/issues?")) return Response.json({ issues });
    if (url.endsWith("/api/issues")) {
      issuePosts++;
      const data = JSON.parse(String(init?.body));
      const row = {
        id: "issue-" + issuePosts,
        title: data.title,
        description: data.description,
        project_id: "project",
        assignee_type: "agent",
        assignee_id: "agent",
      };
      issues.push(row);
      if (failIssue) throw new DOMException("lost response", "TimeoutError");
      return Response.json(row, { status: 201 });
    }
    if (url.includes("/comments")) {
      if (init?.method === "POST") {
        commentPosts++;
        const row = {
          id: "comment-" + commentPosts,
          content: JSON.parse(String(init.body)).content,
        };
        comments.push(row);
        if (failComment)
          throw new DOMException("lost response", "TimeoutError");
        return Response.json(row, { status: 201 });
      }
      return Response.json(comments);
    }
    throw new Error("unexpected endpoint");
  };
  const config: ThreadRouterConfig = {
    multicaApiBaseUrl: "https://multica.test",
    multicaApiToken: "test",
    multicaWorkspaceId: "ws",
    multicaProjectId: "project",
    multicaAgentId: "agent",
    store: new MemoryThreadStore(),
  };
  return {
    config,
    fetcher,
    issues,
    comments,
    get issuePosts() {
      return issuePosts;
    },
    get commentPosts() {
      return commentPosts;
    },
    loseIssueResponse() {
      failIssue = true;
    },
    loseCommentResponse() {
      failComment = true;
    },
  };
}
describe("direct Issue routing", () => {
  it("creates distinct issues for two simultaneous different Slack threads", async () => {
    const f = fixture();
    await Promise.all([
      routeSlackThreadEvent(root, f.config, f.fetcher),
      routeSlackThreadEvent(
        { ...root, messageTs: "101.000001", threadTs: "101.000001" },
        f.config,
        f.fetcher,
      ),
    ]);
    expect(f.issuePosts).toBe(2);
    expect(f.issues[0]!.title).not.toBe(f.issues[1]!.title);
  });
  it("one thread creates once and appends once per followup", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    expect(
      (await routeSlackThreadEvent(root, f.config, f.fetcher)).action,
    ).toBe("duplicate");
    const next = { ...root, messageTs: "102.000001" };
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("recovers a committed Issue after lost response without second POST", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow();
    expect(
      (await routeSlackThreadEvent(root, f.config, f.fetcher)).issueId,
    ).toBe("issue-1");
    expect(f.issuePosts).toBe(1);
  });
  it("does not blindly replay an ambiguous unconfirmed Issue POST", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow();
    f.issues.splice(0);
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("ambiguous_issue_create");
    expect(f.issuePosts).toBe(1);
  });
  it("recovers a committed comment after lost response without duplicate followup", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.loseCommentResponse();
    const next = { ...root, messageTs: "102.000001" };
    await expect(
      routeSlackThreadEvent(next, f.config, f.fetcher),
    ).rejects.toThrow();
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.commentPosts).toBe(1);
  });
  it("recovers original root identity after KV mapping expires", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.config.store = new MemoryThreadStore();
    const result = await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001" },
      f.config,
      f.fetcher,
    );
    expect(result.action).toBe("comment_persisted");
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("does not POST while another worker owns the lock", async () => {
    const f = fixture();
    f.config.store.setIfAbsent = async () => false;
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("thread_lock_busy");
    expect(f.issuePosts).toBe(0);
  });
  it("retains both requests when a rejected first create is overtaken by a followup", async () => {
    const f = fixture();
    let reject = true;
    const fetcher: typeof fetch = async (input, init) => {
      if (reject && String(input).endsWith("/api/issues")) {
        reject = false;
        return Response.json({ error: "rate_limited" }, { status: 429 });
      }
      return f.fetcher(input, init);
    };
    await expect(
      routeSlackThreadEvent(root, f.config, fetcher),
    ).rejects.toThrow();
    await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001", text: "<@U1> B" },
      f.config,
      fetcher,
    );
    await routeSlackThreadEvent(root, f.config, fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
    expect(f.issues[0]!.description).toContain("<@U1> B");
    expect(f.comments[0]!.content).toContain("<@U1> test");
  });
  it("does not adopt a different configured Agent scope", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    await routeSlackThreadEvent(
      root,
      { ...f.config, multicaAgentId: "another-agent" },
      f.fetcher,
    );
    expect(f.issuePosts).toBe(2);
  });
  it("does not recover an Issue assigned to another Agent", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.issues[0]!.assignee_id = "another-agent";
    f.config.store = new MemoryThreadStore();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("invalid_issue_scope");
    expect(f.issuePosts).toBe(1);
  });
});
