import type { FooterConfig } from "./footer-config.js";
import { isRecord, type FooterRun, type RunRef } from "./footer-data.js";

interface RunMessage {
  seq: number;
  type: string;
  tool?: string;
  input?: Record<string, unknown>;
  output?: string;
}
export interface LogStats {
  tools?: number;
  skills?: number;
}

export function buildDurationFooter(run: FooterRun): string | undefined {
  if (!run.started_at || !run.completed_at) return;
  const ms = Date.parse(run.completed_at) - Date.parse(run.started_at);
  if (!Number.isFinite(ms) || ms < 0) return;
  const seconds = Math.round(ms / 1000);
  return `:agent_time: ${seconds >= 60 ? `${Math.floor(seconds / 60)}m ` : ""}${seconds % 60}s`;
}

function usageFooter(usage: unknown): string | undefined {
  if (!Array.isArray(usage) || !usage.length) return;
  const models = new Set<string>();
  let total = 0,
    input = 0,
    read = 0;
  let validTokens = true,
    validModels = true,
    codex = true;
  for (const row of usage) {
    if (!isRecord(row)) return;
    const model = typeof row.model === "string" ? row.model.trim() : "";
    if (/^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$/u.test(model))
      models.add(model);
    else validModels = false;
    const counts = [
      row.input_tokens,
      row.output_tokens,
      row.cache_read_tokens,
      row.cache_write_tokens,
    ];
    if (
      counts.every(
        (n) => typeof n === "number" && Number.isSafeInteger(n) && n >= 0,
      )
    ) {
      const [i, o, r, w] = counts as number[];
      total += i! + o! + r! + w!;
      input += i!;
      read += r!;
      if (!Number.isSafeInteger(total)) validTokens = false;
    } else validTokens = false;
    // 当前已验证 Codex 的 input 不含缓存；其他 provider 不推断缓存口径。
    if (row.provider !== "codex" || row.cache_write_tokens !== 0) codex = false;
  }
  const modelText = validModels ? [...models].join(", ") : "";
  let tokenText = validTokens ? `${(total / 1000).toFixed(1)}k tokens` : "";
  if (tokenText && codex && input + read > 0)
    tokenText += ` (${Math.round((read / (input + read)) * 100)}% cached)`;
  const value = [modelText, tokenText].filter(Boolean).join(": ");
  // Slack context 文本有容量限制；不截断模型名称或伪造剩余模型的统计。
  if (!value || value.length > 1500) return;
  return `:agent_mdi_robot_outline_muted: ${value}`;
}

export function buildRunFooter(
  run: FooterRun,
  logs: LogStats = {},
): string | undefined {
  const parts = [buildDurationFooter(run), usageFooter(run.usage)];
  if (logs.tools !== undefined && logs.tools > 0) parts.push(`:agent_tool: ${logs.tools} tools`);
  if (logs.skills !== undefined && logs.skills > 0)
    parts.push(`:agent_skill: ${logs.skills} skills`);
  return parts.filter(Boolean).join(" · ") || undefined;
}

// 只解析已验证的简单 shell 读取语法，不执行命令，也不猜测脚本/管道的行为。
function shellWords(command: string): string[] | undefined {
  const words: string[] = [];
  const pattern = /\s*(?:'([^']*)'|"([^"$`\\]*)"|([^\s'"\\;&|<>$`()]+))/gy;
  let index = 0;
  while (index < command.trimEnd().length) {
    pattern.lastIndex = index;
    const match = pattern.exec(command);
    if (!match) return;
    words.push(match[1] ?? match[2] ?? match[3]!);
    index = pattern.lastIndex;
    if (index < command.length && !/\s/u.test(command[index]!)) return;
  }
  return words;
}

function skillPath(message: RunMessage): string | undefined {
  if (message.tool !== "exec_command" || !message.input) return;
  const command = message.input.command ?? message.input.cmd;
  if (typeof command !== "string") return;
  let words = shellWords(command);
  if (!words) return;
  if (
    /^(?:\/bin\/)?(?:zsh|bash|sh)$/u.test(words[0] ?? "") &&
    ["-lc", "-c"].includes(words[1] ?? "") &&
    words.length === 3
  )
    words = shellWords(words[2]!);
  if (!words) return;
  if (words[0] === "rtk") words = words.slice(words[1] === "proxy" ? 2 : 1);
  if (words[0] !== "cat" && words[0] !== "/bin/cat") return;
  words = words.slice(1);
  if (words[0] === "--") words = words.slice(1);
  if (
    words.length !== 1 ||
    !/^(?:\/|~\/)[^\n\r*?\[\]]+\/SKILL\.md$/u.test(words[0]!)
  )
    return;
  return words[0];
}

function loadedName(output: string): string | undefined {
  const frontmatter = output
    .replace(/\r\n/g, "\n")
    .match(/^---\n([\s\S]*?)\n---(?:\n|$)/u)?.[1];
  if (!frontmatter) return;
  const names = frontmatter
    .split("\n")
    .filter((line) => line.startsWith("name:"));
  if (names.length !== 1) return;
  const value = names[0]!.slice(5).trim();
  return value
    .match(
      /^(?:([A-Za-z0-9][A-Za-z0-9_:/.-]{0,199})|'([A-Za-z0-9][A-Za-z0-9_:/.-]{0,199})'|"([A-Za-z0-9][A-Za-z0-9_:/.-]{0,199})")$/u,
    )
    ?.slice(1)
    .find(Boolean);
}

export function summarizeRunMessages(body: unknown, ref: RunRef): LogStats {
  // 当前 API 为完整、按 seq 排序的数组；空数组可能表示日志未上传，不能声称零调用。
  if (!Array.isArray(body) || !body.length || body.length > 10000) return {};
  if (
    body.some(
      (m, index) =>
        !isRecord(m) ||
        m.task_id !== ref.taskId ||
        m.issue_id !== ref.issueId ||
        m.seq !== index + 1 ||
        !["text", "tool_use", "tool_result", "error"].includes(String(m.type)),
    )
  )
    return {};
  const messages = body as RunMessage[];
  const names = new Set<string>();
  const pending: RunMessage[] = [];
  let skillsKnown = true;
  for (const message of messages) {
    if (message.type === "tool_use") pending.push(message);
    if (message.type !== "tool_result") continue;
    // 协议没有 call_id；仅接受唯一待返回调用且紧邻的同名结果，避免并发错配。
    const call = pending.length === 1 ? pending[0] : undefined;
    const candidates = pending.filter((p) => p.tool === message.tool);
    if (
      candidates.some((p) => skillPath(p)) &&
      (!call || call.seq + 1 !== message.seq)
    )
      skillsKnown = false;
    if (
      call &&
      call.tool === message.tool &&
      call.seq + 1 === message.seq &&
      skillPath(call)
    ) {
      const output = message.output;
      const name = typeof output === "string" ? loadedName(output) : undefined;
      if (name) names.add(name);
      else if (
        typeof output !== "string" ||
        !/^(?:cat: .*: (?:No such file or directory|Permission denied))/u.test(
          output,
        )
      )
        skillsKnown = false;
    }
    // 每条结果只消耗一个待返回项，保留并发数量，避免过早认定后续读取已经串行。
    if (candidates[0]) pending.splice(pending.indexOf(candidates[0]), 1);
  }
  if (pending.some((p) => skillPath(p))) skillsKnown = false;
  // 出现无法解析的 SKILL.md 读取意图时不输出不完整的 Skills 数；路径搜索不算加载。
  for (const m of messages) {
    const command = m.input?.command ?? m.input?.cmd;
    if (
      m.type === "tool_use" &&
      /(?:read|Read)/u.test(m.tool ?? "") &&
      JSON.stringify(m.input ?? {}).includes("SKILL.md")
    )
      skillsKnown = false;
    if (
      m.type === "tool_use" &&
      typeof command === "string" &&
      command.includes("SKILL.md") &&
      /\b(?:cat|head|sed|read_file)\b/u.test(command) &&
      !skillPath(m)
    )
      skillsKnown = false;
  }
  return {
    tools: messages.filter((m) => m.type === "tool_use").length,
    ...(skillsKnown ? { skills: names.size } : {}),
  };
}

export async function readRunLogStats(
  config: FooterConfig,
  ref: RunRef,
  fetchImpl: typeof fetch,
): Promise<LogStats> {
  try {
    const response = await fetchImpl(
      `${config.multicaApiBaseUrl}/api/tasks/${ref.taskId}/messages`,
      {
        headers: {
          authorization: `Bearer ${config.multicaApiToken}`,
          "x-workspace-id": config.multicaWorkspaceId,
        },
        redirect: "error",
        signal: AbortSignal.timeout(8000),
      },
    );
    if (!response.ok || !response.body) return {};
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let bytes = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.length;
      if (bytes > 4 * 1024 * 1024) {
        await reader.cancel();
        return {};
      }
      chunks.push(value);
    }
    return summarizeRunMessages(
      JSON.parse(Buffer.concat(chunks).toString("utf8")),
      ref,
    );
  } catch {
    // 可选日志统计不可用时仍展示已确认的 usage/耗时，不记录原始日志或响应正文。
    return {};
  }
}
