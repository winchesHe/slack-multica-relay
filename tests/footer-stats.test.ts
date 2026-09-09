import { describe, expect, it } from "vitest";
import {
  buildRunFooter,
  readRunLogStats,
  summarizeRunMessages,
} from "../src/footer-stats.js";
import type { FooterConfig } from "../src/footer-config.js";

const ref = { version: 1 as const, issueId: "issue", taskId: "task" };
const run = {
  id: ref.taskId,
  status: "completed",
  started_at: "2026-09-08T00:00:00Z",
  completed_at: "2026-09-08T00:14:12Z",
};
const usage = {
  model: "gpt-5.6-sol",
  provider: "codex",
  input_tokens: 161994,
  output_tokens: 0,
  cache_read_tokens: 5237806,
  cache_write_tokens: 0,
};
function messages(rows: Record<string, unknown>[]) {
  return rows.map((row, index) => ({
    task_id: ref.taskId,
    issue_id: ref.issueId,
    seq: index + 1,
    ...row,
  }));
}
const read = (path = "/skills/slack/SKILL.md") => ({
  type: "tool_use",
  tool: "exec_command",
  input: { command: `/bin/zsh -lc 'cat ${path}'` },
});
const result = (name = "slack") => ({
  type: "tool_result",
  tool: "exec_command",
  output: `---\nname: ${name}\ndescription: test\n---\n正文`,
});

describe("运行 footer 统计与显示", () => {
  it("严格符合完整格式，tokens 包含缓存且不重复扣除 input", () => {
    expect(
      buildRunFooter({ ...run, usage: [usage] }, { tools: 27, skills: 2 }),
    ).toBe(
      ":agent_time: 14m 12s · :agent_mdi_robot_outline_muted: gpt-5.6-sol: 5399.8k tokens (97% cached) · :agent_tool: 27 tools · :agent_skill: 2 skills",
    );
  });
  it("与已核对的真实运行 usage 数值一致", () => {
    expect(
      buildRunFooter(
        {
          ...run,
          completed_at: "2026-09-08T00:06:30Z",
          usage: [
            {
              ...usage,
              model: "gpt-6-astra",
              input_tokens: 132436,
              output_tokens: 9555,
              cache_read_tokens: 3114496,
            },
          ],
        },
        { tools: 51, skills: 4 },
      ),
    ).toBe(
      ":agent_time: 6m 30s · :agent_mdi_robot_outline_muted: gpt-6-astra: 3256.5k tokens (96% cached) · :agent_tool: 51 tools · :agent_skill: 4 skills",
    );
  });
  it("多个模型去重，聚合全部 usage，不串到其他 run", () => {
    const row = { ...usage, input_tokens: 1000, cache_read_tokens: 9000 };
    expect(
      buildRunFooter({
        ...run,
        usage: [row, { ...row, model: "another" }, row],
      }),
    ).toContain("gpt-5.6-sol, another: 30.0k tokens (90% cached)");
  });
  it.each([undefined, [], null, [null]])(
    "缺失 usage %j 不生成占位值",
    (value) => {
      expect(buildRunFooter({ ...run, usage: value })).toBe(
        ":agent_time: 14m 12s",
      );
    },
  );
  it("没有耗时仍保留可靠统计；全部缺失才省略 footer", () => {
    expect(
      buildRunFooter(
        { id: "task", status: "completed" },
        { tools: 0, skills: 0 },
      ),
    ).toBe(":agent_tool: 0 tools · :agent_skill: 0 skills");
    expect(buildRunFooter({ id: "task", status: "completed" })).toBeUndefined();
  });
  it("真实零 tokens 可显示，零分母不生成 cached", () => {
    expect(
      buildRunFooter({
        ...run,
        usage: [{ ...usage, input_tokens: 0, cache_read_tokens: 0 }],
      }),
    ).toContain("0.0k tokens");
    expect(
      buildRunFooter({
        ...run,
        usage: [{ ...usage, input_tokens: 0, cache_read_tokens: 0 }],
      }),
    ).not.toContain("cached");
  });
  it.each(["claude-code", undefined, "unknown"])(
    "未验证 provider %s 只隐藏缓存百分比",
    (provider) => {
      const footer = buildRunFooter({
        ...run,
        usage: [{ ...usage, provider, cache_write_tokens: 1000 }],
      });
      expect(footer).toContain("5400.8k tokens");
      expect(footer).not.toContain("cached");
    },
  );
  it.each([-1, 1.5, "100", undefined, Number.MAX_SAFE_INTEGER])(
    "无效计数 %s 隐藏总量但保留模型",
    (input_tokens) => {
      expect(
        buildRunFooter({ ...run, usage: [{ ...usage, input_tokens }] }),
      ).toBe(":agent_time: 14m 12s · :agent_mdi_robot_outline_muted: gpt-5.6-sol");
    },
  );
  it("非法模型不进入 Slack 文本，可靠 tokens 仍保留", () => {
    expect(
      buildRunFooter({ ...run, usage: [{ ...usage, model: "<!channel>" }] }),
    ).toBe(
      ":agent_time: 14m 12s · :agent_mdi_robot_outline_muted: 5399.8k tokens (97% cached)",
    );
  });
});

describe("完整日志与成功读取 Skills 证据", () => {
  it("只计 tool_use，按 frontmatter name 去重成功读取", () => {
    expect(
      summarizeRunMessages(
        messages([
          read(),
          result(),
          read("/other/slack/SKILL.md"),
          result(),
          read("/skills/jira/SKILL.md"),
          result("jira"),
          { type: "text", content: "I used 20 skills" },
        ]),
        ref,
      ),
    ).toEqual({ tools: 3, skills: 2 });
  });
  it("支持引号路径和 rtk proxy；不执行 shell", () => {
    const call = {
      ...read(),
      input: { cmd: 'rtk proxy cat -- "/my skills/slack/SKILL.md"' },
    };
    expect(
      summarizeRunMessages(messages([call, result('"slack"')]), ref),
    ).toEqual({ tools: 1, skills: 1 });
  });
  it("搜索命中路径与失败读取不算加载", () => {
    expect(
      summarizeRunMessages(
        messages([
          { ...read(), input: { command: "rg --files /skills -g SKILL.md" } },
          { ...result(), output: "/skills/slack/SKILL.md" },
          read(),
          {
            ...result(),
            output: "cat: /skills/slack/SKILL.md: No such file or directory\n",
          },
        ]),
        ref,
      ),
    ).toEqual({ tools: 2, skills: 0 });
  });
  it.each([
    [],
    [{ type: "text", seq: 2 }],
    [{ type: "text", task_id: "other" }],
    [{ type: "text", issue_id: "other" }],
    [
      { type: "text", seq: 1 },
      { type: "text", seq: 1 },
    ],
    [{ type: "new-protocol-type" }],
  ])("缺失/不完整/跨运行日志隐藏所有日志统计 %j", (...rows) => {
    expect(summarizeRunMessages(messages(rows), ref)).toEqual({});
  });
  it("并发同名调用没有 call_id 时不把结果错配为 Skills", () => {
    expect(
      summarizeRunMessages(
        messages([
          read(),
          read("/skills/jira/SKILL.md"),
          result(),
          result("jira"),
        ]),
        ref,
      ),
    ).toEqual({ tools: 2 });
  });
  it.each([
    [read(), { type: "text" }, result()],
    [read()],
    [read(), { ...result(), output: "truncated output" }],
    [
      {
        ...read(),
        input: { command: "cat /skills/slack/SKILL.md; echo fake" },
      },
      result(),
    ],
    [
      {
        ...read(),
        input: { command: "cat /skills/slack/SKILL.md /skills/jira/SKILL.md" },
      },
      result(),
    ],
  ])("不完整或无法核实的读取隐藏 Skills %j", (...rows) => {
    expect(summarizeRunMessages(messages(rows), ref)).toEqual({ tools: 1 });
  });
  it("先前并发未配对时，后续相邻结果也不能恢复可信 Skills 计数", () => {
    const other = { ...read(), input: { command: "echo test" } };
    expect(
      summarizeRunMessages(
        messages([other, other, result(), read(), result()]),
        ref,
      ),
    ).toEqual({ tools: 3 });
  });
  it("未验证的原生读取工具不显示零 Skills", () => {
    expect(
      summarizeRunMessages(
        messages([
          {
            type: "tool_use",
            tool: "Read",
            input: { file_path: "/skills/slack/SKILL.md" },
          },
          { ...result(), tool: "Read" },
        ]),
        ref,
      ),
    ).toEqual({ tools: 1 });
  });
  it("只有文本的完整日志可证明零工具调用", () => {
    expect(
      summarizeRunMessages(messages([{ type: "text", content: "done" }]), ref),
    ).toEqual({ tools: 0, skills: 0 });
  });
});

describe("日志 API 读取边界", () => {
  const config = {
    multicaApiBaseUrl: "https://multica.test",
    multicaApiToken: "test",
    multicaWorkspaceId: "ws",
  } as FooterConfig;
  it("读取精确 task 的完整数组，不传不存在的分页参数", async () => {
    let calls = 0;
    const stats = await readRunLogStats(config, ref, async (url, init) => {
      calls++;
      expect(url).toBe("https://multica.test/api/tasks/task/messages");
      expect(init?.headers).toEqual({
        authorization: "Bearer test",
        "x-workspace-id": "ws",
      });
      expect(init?.redirect).toBe("error");
      return Response.json(messages([read(), result()]));
    });
    expect(calls).toBe(1);
    expect(stats).toEqual({ tools: 1, skills: 1 });
  });
  it.each(["failure", "json", "oversized", "envelope", "transport"])(
    "日志 %s 不伪造计数",
    async (mode) => {
      expect(
        await readRunLogStats(config, ref, async () => {
          if (mode === "transport") throw new Error("private detail");
          if (mode === "failure") return new Response("error", { status: 503 });
          if (mode === "json") return new Response("bad json");
          if (mode === "oversized")
            return new Response("x".repeat(4 * 1024 * 1024 + 1));
          return Response.json({ messages: [], has_more: true });
        }),
      ).toEqual({});
    },
  );
});
