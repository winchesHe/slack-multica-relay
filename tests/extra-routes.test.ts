import { execFileSync } from "node:child_process";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routeSlackThreadEventWithExtraRoutes as dispatch, type ThreadState } from "../src/thread-router.js";
import { findIssue, getIssue, validateMappedIssue } from "../src/multica-api.js";
import { formatTaskDescription, readTaskMessage } from "../src/task-presentation.js";
import { fixture, root, reply } from "./fixtures/extra-routes.js";

afterEach(() => vi.restoreAllMocks());

describe("A2 已有 thread 次级路由", () => {
  it.each([false, true])("Redis 映射命中或 marker 恢复，只评论原 Issue（cached=%s）", async (cached) => {
    const f = fixture(); f.issues.push(f.bound);
    if (cached) f.kv.set(f.key, JSON.stringify({ version: 2, rootMessageKey: "T1:C0B0DSCRBKM:100.000001",
      creating: false, issueId: f.bound.id }));
    expect(await dispatch(reply, f.config, f.fetcher)).toMatchObject({ action: "comment_persisted", issueId: "lab-issue", routeId: "dorea" });
    expect(await dispatch(reply, f.config, f.fetcher)).toMatchObject({ action: "duplicate", routeId: "dorea" });
    expect(f.comments).toHaveLength(1);
    expect(f.comments[0]!.content).toContain("批准方案 A");
    expect(f.calls.filter((call) => call.workspace === "grm")).toEqual([]);
    expect(f.calls.some((call) => call.url.endsWith("/reactions.add"))).toBe(true);
    if (cached) expect(f.calls.some((call) => call.url.includes("/search?"))).toBe(false);
    expect(f.issues).toHaveLength(1);
    expect(JSON.parse(f.kv.get(f.key)!)).toMatchObject({ issueId: "lab-issue", creating: false });
  });
  it.each(["agent", "squad"])("允许独立 Reviewer，但不放宽项目或归属类型（主类型 %s）", async (type) => {
    const f = fixture(); const config = { ...f.config.extraRoutes[0]!, multicaAssigneeType: type as "agent" | "squad" };
    const candidate = { ...f.bound, assignee_id: "reviewer" };
    const fetcher: typeof fetch = async (input) => String(input).includes("/search?")
      ? Response.json({ issues: [candidate] }) : Response.json(candidate);
    expect((await findIssue(config, f.marker, fetcher))?.id).toBe("lab-issue");
    await validateMappedIssue(config, "lab-issue", f.marker, fetcher);
    expect((await getIssue(config, "lab-issue", fetcher)).id).toBe("lab-issue");
    for (const patch of [{ project_id: "other" }, { assignee_id: "outsider" }, { assignee_type: "squad" }]) {
      const wrong: typeof fetch = async (input) => String(input).includes("/search?")
        ? Response.json({ issues: [{ ...candidate, ...patch }] }) : Response.json({ ...candidate, ...patch });
      await expect(findIssue(config, f.marker, wrong)).rejects.toThrow("invalid_issue_scope");
      await expect(validateMappedIssue(config, "lab-issue", f.marker, wrong)).rejects.toThrow("invalid_issue_scope");
      await expect(getIssue(config, "lab-issue", wrong)).rejects.toThrow("invalid_issue_scope");
    }
  });
  it("同频道普通 mention 先确认无映射，随后走主路由", async () => {
    const f = fixture();
    expect(await dispatch(reply, f.config, f.fetcher)).toMatchObject({ action: "created", issueId: "grm-created" });
    expect(f.calls.find((call) => call.workspace)?.workspace).toBe("lab");
    expect(f.issues[0]).toMatchObject({ workspace_id: "grm", assignee_id: "router-agent" });
  });
  it.each([false, true])("非配置频道或未启用时不探测次级路由（disabled=%s）", async (disabled) => {
    const f = fixture();
    await dispatch(disabled ? reply : { ...reply, channelId: "COTHER" },
      { ...f.config, ...(disabled ? { extraRoutes: [] } : {}) }, f.fetcher);
    expect(f.calls.some((call) => call.workspace === "lab")).toBe(false);
    expect(f.calls.filter((call) => call.url.includes("/search?"))).toHaveLength(1);
    expect(f.issues[0]?.workspace_id).toBe("grm");
  });
  it("按配置顺序探测，首次命中后停止", async () => {
    const f = fixture(); f.issues.push(f.bound);
    const configured = f.config.extraRoutes[0]!;
    const config = { ...f.config, extraRoutes: [
      { ...configured, id: "empty", multicaProjectId: "empty" }, configured,
      { ...configured, id: "never", multicaWorkspaceId: "never" },
    ] };
    expect((await dispatch(reply, config, f.fetcher)).routeId).toBe("dorea");
    expect(f.calls.some((call) => call.workspace === "never" || call.workspace === "grm")).toBe(false);
    expect(f.calls.filter((call) => call.url.includes("/search?"))).toHaveLength(3);
  });
  it.each(["5xx", "timeout", "network", "malformed", "redis"])("探测 %s 错误不得回落", async (kind) => {
    const f = fixture();
    const fetcher: typeof fetch = async (input, init) => {
      if ((kind === "redis" && String(input) === "https://kv.test") || String(input).includes("/search?")) {
        if (kind === "5xx" || kind === "redis") return Response.json({}, { status: 503 });
        if (kind === "malformed") return Response.json({});
        throw kind === "timeout" ? new DOMException("test", "TimeoutError") : new TypeError("network");
      }
      return f.fetcher(input, init);
    };
    // store 也使用故障注入 fetch，覆盖 Redis 探测故障。
    if (kind === "redis") f.config.store.get = async () => { throw new TypeError("redis unavailable"); };
    await expect(dispatch(reply, f.config, fetcher)).rejects.toThrow();
    expect(f.issues).toHaveLength(0);
    expect(f.calls.some((call) => call.workspace === "grm")).toBe(false);
  });
  it("探测后映射消失时仍禁止次级建卡与主路由回落", async () => {
    const f = fixture(); let searches = 0;
    const fetcher: typeof fetch = async (input, init) => String(input).includes("/search?")
      ? Response.json({ issues: searches++ === 0 ? [f.bound] : [] }) : f.fetcher(input, init);
    await expect(dispatch(reply, f.config, fetcher)).rejects.toThrow("extra_route_mapping_missing");
    expect(f.issues).toHaveLength(0); expect(f.comments).toHaveLength(0);
    expect(f.calls.some((call) => call.workspace === "grm")).toBe(false);
  });
  it.each(["{}", '{"version":2,"rootMessageKey":"x","creating":true}'])("不将损坏或未确认状态当作无映射 %s", async (state) => {
    const f = fixture(); f.kv.set(f.key, state);
    await expect(dispatch(reply, f.config, f.fetcher)).rejects.toThrow();
    expect(f.calls.some((call) => call.workspace === "grm")).toBe(false);
  });
  it("命中次级后忽略自发卡片，未命中时保留普通主路由语义", async () => {
    const f = fixture(); f.issues.push(f.bound);
    const event = { ...reply, text: "<@U1> 自发卡片 [dorea-bug-agent]" };
    expect(await dispatch(event, f.config, f.fetcher)).toEqual({ action: "ignored", reason: "route_ignored_text", routeId: "dorea" });
    expect(f.comments).toHaveLength(0); expect(f.kv.size).toBe(0);
    expect(f.calls.some((call) => call.method === "POST" && call.workspace)).toBe(false);
    f.issues.length = 0;
    expect((await dispatch(event, f.config, f.fetcher)).action).toBe("created");
  });
  it.each(["owner", "reviewer"])("取消指令在相同次级 Issue 上取消实际运行（%s）", async (assignee) => {
    const f = fixture(); f.issues.push({ ...f.bound, assignee_id: assignee });
    f.runs.push({ id: "lab-run", issue_id: "lab-issue", workspace_id: "lab", agent_id: assignee, status: "running" });
    expect(await dispatch({ ...reply, text: "<@U1> 取消", operation: "cancel" }, f.config, f.fetcher))
      .toMatchObject({ action: "cancelled", issueId: "lab-issue", routeId: "dorea" });
    expect(f.runs[0]?.status).toBe("cancelled");
    expect(f.calls.some((call) => call.url.endsWith("/tasks/lab-run/cancel") && call.workspace === "lab")).toBe(true);
    expect(f.calls.some((call) => call.workspace === "grm")).toBe(false);
    expect(f.comments).toHaveLength(0);
    expect((JSON.parse(f.kv.get(f.key)!) as ThreadState).cancellation?.phase).toBe("done");
  });
  it("Python 预绑定格式与 TypeScript 渲染、恢复完全互通", () => {
    const f = fixture();
    const text = "<@U1> 中文 &lt;tag&gt;\n```json\n<!-- relay-payload:v1 -->\n~~~~ @again";
    const rendered = JSON.parse(execFileSync("python3", ["scripts/bind-slack-thread.py", "--render-only",
      "--workspace-id", "lab", "--project-id", "dorea", "--assignee-id", "owner",
      "--team-id", "T1", "--channel-id", root.channelId, "--root-ts", root.threadTs,
      "--sender-user-id", "U1", "--mention-user-id", "U1", "--root-text", text], { encoding: "utf8" }));
    expect(rendered.marker).toBe(f.marker);
    expect(rendered.description).toBe(formatTaskDescription({ ...root, text }, f.marker));
    expect(readTaskMessage(rendered.description)).toMatchObject({ teamId: "T1", channelId: root.channelId, threadTs: root.threadTs, messageTs: root.messageTs });
  });
});
