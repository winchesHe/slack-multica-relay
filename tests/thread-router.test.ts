import {readTaskEnvelope} from '../src/task-presentation.js';
import { describe, expect, it, vi } from "vitest";
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
    if (url.includes("/api/issues/search?")) return Response.json({ issues });
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
    readContext: async (event) => ({ anchorTs: event.threadTs, cutoffTs: event.messageTs, capturedAt: '2026-01-01T00:00:00Z', timeline: { status: 'complete', messages: [] } }),
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
    expect(readTaskEnvelope(f.issues[0]!.description).eventPayload.text).toBe("<@U1> B");
    expect(readTaskEnvelope(f.comments[0]!.content).eventPayload.text).toBe("<@U1> test");
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


it('keeps cumulative sent branch fingerprints across compact follow-ups, edits and lost responses',async()=>{
  const f=fixture();let text='original';let reads=0;
  f.config.readContext=async e=>{reads++;return {anchorTs:e.threadTs,cutoffTs:e.messageTs,capturedAt:'fixed',timeline:{status:'complete',messages:[{ts:'99.000001',authorId:'U1',origin:'unknown',text,files:[]},{ts:e.threadTs,authorId:'U2',origin:'unknown',text:'root',files:[]}]}};};
  const decode=(s:string):any=>readTaskEnvelope(s);
  await routeSlackThreadEvent(root,f.config,f.fetcher);
  for(const ts of ['101.000001','102.000001'])await routeSlackThreadEvent({...root,messageTs:ts},f.config,f.fetcher);
  expect(f.comments.every(c=>decode(c.content).context.timeline.messages.length===1)).toBe(true);
  text='edited';f.loseCommentResponse();
  const e={...root,messageTs:'103.000001'};
  await expect(routeSlackThreadEvent(e,f.config,f.fetcher)).rejects.toThrow();
  const before=reads;await routeSlackThreadEvent(e,f.config,f.fetcher);expect(reads).toBe(before);
  expect(decode(f.comments.at(-1)!.content).context.timeline.messages[0].text).toBe('edited');
  const fetcher:typeof fetch=async(input,init)=>{
    if(init?.method==='POST'){const value={id:'last',content:JSON.parse(String(init.body)).content};f.comments.push(value);return Response.json(value);}
    return f.fetcher(input,init);
  };
  await routeSlackThreadEvent({...root,messageTs:'104.000001'},f.config,fetcher);
  expect(decode(f.comments.at(-1)!.content).context.timeline.messages).toHaveLength(1);
  await routeSlackThreadEvent({...root,messageTs:'102.500001'},f.config,fetcher);
  expect(decode(f.comments.at(-1)!.content).context.selection.baseline).toBe('unavailable');
  await routeSlackThreadEvent({...root,messageTs:'105.000001'},f.config,fetcher);
  expect(decode(f.comments.at(-1)!.content).context.timeline.messages).toHaveLength(1);
});

it('freezes the Agent configuration snapshot across retries and refreshes it for new messages',async()=>{
  const f=fixture();let calls=0;let model='model-one';
  const fetcher:typeof fetch=async(input,init)=>{
    if(String(input).includes('/api/agents/')){calls++;return Response.json({id:'agent',workspace_id:'ws',model,service_tier:'priority'});}
    return f.fetcher(input,init);
  };
  f.loseIssueResponse();
  await expect(routeSlackThreadEvent(root,f.config,fetcher)).rejects.toThrow();
  model='model-two';await routeSlackThreadEvent(root,f.config,fetcher);
  expect(calls).toBe(1);
  expect(readTaskEnvelope(f.issues[0]!.description).replyContext).toMatchObject({model:'model-one'});
  await routeSlackThreadEvent({...root,messageTs:'101.000001'},f.config,fetcher);
  expect(calls).toBe(2);
  expect(readTaskEnvelope(f.comments[0]!.content).replyContext).toMatchObject({model:'model-two'});
});

it('retains necessary predecessor context when a side thread gains a reply',async()=>{
  const f=fixture();let added=false;
  f.config.readContext=async e=>({anchorTs:e.threadTs,cutoffTs:e.messageTs,capturedAt:'fixed',timeline:{status:'complete',messages:[
    {ts:'90.000001',authorId:'U1',origin:'unknown',text:'side-root',files:[],replies:{status:'complete',messages:[
      {ts:'91.000001',authorId:'U1',origin:'unknown',text:'old-reply',files:[]},
      ...(added?[{ts:'100.500001',authorId:'U1',origin:'unknown' as const,text:'new-reply',files:[]}]:[])
    ]}},
    {ts:e.threadTs,authorId:'U2',origin:'unknown',text:'current-root',files:[]}
  ]}});
  await routeSlackThreadEvent(root,f.config,f.fetcher);added=true;
  await routeSlackThreadEvent({...root,messageTs:'101.000001'},f.config,f.fetcher);
  const second=readTaskEnvelope(f.comments[0]!.content) as any;
  expect(second.context.timeline.messages[0].replies.messages.map((m:any)=>m.text)).toEqual(['old-reply','new-reply']);
  expect(f.comments[0]!.content).toContain('旁支变化：新增 1 条，更新 0 条');
  await routeSlackThreadEvent({...root,messageTs:'102.000001'},f.config,f.fetcher);
  expect((readTaskEnvelope(f.comments[1]!.content) as any).context.timeline.messages).toHaveLength(1);
});


it('logs content-free clipping measurements',async()=>{
  const f=fixture();const info=vi.spyOn(console,'info').mockImplementation(()=>{});
  try {
    f.config.readContext=async e=>({anchorTs:e.threadTs,cutoffTs:e.messageTs,capturedAt:'fixed',readStats:{slackCalls:3,rawMessages:5,messageReadMs:7,nameLookupCalls:2,nameReadMs:3},timeline:{status:'complete',messages:[{ts:e.threadTs,authorId:'U2',origin:'unknown',text:'PRIVATE_BODY_MARKER',files:[]}]}});
    await routeSlackThreadEvent(root,f.config,f.fetcher);
    const row=info.mock.calls.find(c=>c[0]==='relay_context')![1] as Record<string,unknown>;
    expect(row).toMatchObject({slackCalls:3,rawMessages:5,candidateRoots:1,candidateMessages:1,retainedRoots:1,retainedMessages:1,omittedMessages:0});
    expect(row).toMatchObject({messageReadMs:7,nameLookupCalls:2,nameReadMs:3});
    expect(row.agentConfigMs).toBeGreaterThanOrEqual(0);expect(row.envelopeBytes).toBeGreaterThan(0);
    const log=JSON.stringify(row);expect(log).not.toContain('PRIVATE_BODY_MARKER');expect(log).not.toContain(root.text);expect(log).not.toContain('multicaApiToken');
  } finally {info.mockRestore();}
});
