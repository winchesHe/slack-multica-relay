import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import { waitUntil } from "@vercel/functions";
import { buildRunFooter, readRunLogStats } from "./footer-stats.js";
import { loadFooterConfig, type FooterConfig } from "./footer-config.js";
import {
  footerKey,
  isRecord,
  parseRunRef,
  parseReplyRef,
  readScopedRun,
  type RunRef,
  type ReplyRef,
} from "./footer-data.js";
import {
  readOwnReply,
  replyBodyDigest,
  updateReplyFooter,
} from "./footer-slack.js";
import { json, readBody } from "./http.js";
import { STATE_TTL_SECONDS } from "./thread-router.js";
import { UpstashThreadStore, type ThreadStore } from "./thread-store.js";

interface Binding extends ReplyRef {
  bodyDigest: string;
}
function safeEqual(left: string, right: string): boolean {
  const a = Buffer.from(left),
    b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function verifyMulticaHook(
  raw: string,
  headers: Headers,
  config: FooterConfig,
  now = Date.now(),
): boolean {
  const ts = headers.get("x-multica-timestamp") ?? "";
  if (
    !/^\d+$/u.test(ts) ||
    Math.abs(now / 1000 - Number(ts)) > 300 ||
    headers.get("x-multica-plugin-installation") !== config.pluginInstallationId
  )
    return false;
  const expected =
    "v1=" +
    createHmac(
      "sha256",
      Buffer.from(config.pluginSigningSecret.slice(6), "hex"),
    )
      .update(`${ts}.${raw}`)
      .digest("hex");
  return safeEqual(headers.get("x-multica-signature") ?? "", expected);
}

// 只延长本次函数生命周期，不另发 HTTP 请求，也不安排失败重试。
function startFooter(
  config: FooterConfig,
  store: ThreadStore,
  ref: RunRef,
  fetcher: typeof fetch,
  recordEvent = false,
): void {
  waitUntil(
    (async () => {
      try {
        if (recordEvent)
          await store.set(
            footerKey(config, ref) + ":event",
            JSON.stringify(ref),
            STATE_TTL_SECONDS,
          );
        const result = await processFooter(ref, config, store, fetcher);
        console.info("relay_footer_worker", { ...ref, ...result });
      } catch (error) {
        const reason =
          error instanceof Error && /^footer_[a-z_]+$/u.test(error.message)
            ? error.message
            : "footer_unavailable";
        console.warn("relay_footer_worker", {
          ...ref,
          action: "failed",
          reason,
        });
      }
    })(),
  );
}

async function withFooter(
  request: Request,
  env: NodeJS.ProcessEnv,
  fetchImpl: typeof fetch,
  handler: (
    config: FooterConfig,
    store: ThreadStore,
    raw: string,
    fetcher: typeof fetch,
  ) => Promise<Response>,
) {
  if (request.method !== "POST")
    return json({ error: "method_not_allowed" }, 405);
  let config: FooterConfig;
  try {
    config = loadFooterConfig(env);
  } catch (error) {
    const disabled =
      error instanceof Error && error.message === "footer_disabled";
    return json(
      { error: disabled ? "footer_disabled" : "footer_not_configured" },
      disabled ? 404 : 500,
    );
  }
  let raw: string;
  try {
    raw = await readBody(request);
  } catch {
    return json({ error: "body_too_large" }, 413);
  }
  const deadline = AbortSignal.timeout(45000);
  const fetcher: typeof fetch = (input, init = {}) =>
    fetchImpl(input, {
      ...init,
      signal: init.signal ? AbortSignal.any([deadline, init.signal]) : deadline,
    });
  const store = new UpstashThreadStore(
    config.kvRestApiUrl,
    config.kvRestApiToken,
    fetcher,
  );
  const startedAt = Date.now();
  try {
    const result = await handler(config, store, raw, fetcher);
    console.info("relay_footer_http", {
      status: result.status,
      durationMs: Date.now() - startedAt,
    });
    return result;
  } catch (error) {
    const code =
      error instanceof Error && /^footer_[a-z_]+$/u.test(error.message)
        ? error.message
        : "footer_unavailable";
    if (
      error instanceof SyntaxError ||
      (error instanceof Error && error.message === "invalid_footer_request")
    )
      return json({ error: "invalid_footer_request" }, 400);
    if (
      [
        "footer_scope_mismatch",
        "footer_author_mismatch",
        "footer_reply_conflict",
      ].includes(code)
    )
      return json({ error: code }, 409);
    console.warn("relay_footer", { reason: code });
    return json({ error: code, retryable: true }, 503);
  }
}

export async function acceptMulticaHook(
  request: Request,
  env = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  return withFooter(
    request,
    env,
    fetchImpl,
    async (config, store, raw, fetcher) => {
      if (!verifyMulticaHook(raw, request.headers, config))
        return json({ error: "invalid_multica_signature" }, 401);
      const body: unknown = JSON.parse(raw);
      if (
        !isRecord(body) ||
        body.version !== 1 ||
        body.installation_id !== config.pluginInstallationId ||
        body.workspace_id !== config.multicaWorkspaceId ||
        body.hook_key !== "slack-run-footer" ||
        body.trigger !== "event"
      )
        throw new Error("invalid_footer_request");
      if (!["task.completed", "task.failed"].includes(String(body.event_type)))
        return json({ action: "ignored" });
      if (
        !isRecord(body.input) ||
        body.input.agent_id !== config.multicaAgentId
      )
        return json({ action: "ignored" });
      const ref = parseRunRef({
        version: 1,
        taskId: body.input.task_id,
        issueId: body.issue_id,
      });
      if (body.input.issue_id !== ref.issueId)
        throw new Error("invalid_footer_request");
      // 网络调用全部属于后台任务；不持久化会在回调结束后失效的 callback_token。
      startFooter(config, store, ref, fetcher, true);
      return json({ action: "accepted" });
    },
  );
}

export async function registerSlackReply(
  request: Request,
  env = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  return withFooter(
    request,
    env,
    fetchImpl,
    async (config, store, raw, fetcher) => {
      if (
        !safeEqual(
          request.headers.get("authorization") ?? "",
          `Bearer ${config.replyRegistrationToken}`,
        )
      )
        return json({ error: "invalid_reply_token" }, 401);
      const ref = parseReplyRef(JSON.parse(raw));
      const key = footerKey(config, ref);
      const { run, event } = await readScopedRun(config, ref, fetcher);
      if (
        event.channelId !== ref.channelId ||
        event.threadTs !== ref.threadTs ||
        !run.started_at ||
        !Number.isFinite(Date.parse(run.started_at)) ||
        Number(ref.messageTs) * 1000 < Date.parse(run.started_at)
      )
        throw new Error("footer_scope_mismatch");
      const message = await readOwnReply(config, ref, fetcher);
      const binding: Binding = {
        ...ref,
        bodyDigest: replyBodyDigest(message, ref.taskId),
      };
      const encoded = JSON.stringify(binding);
      if (
        !(await store.setIfAbsent(key + ":reply", encoded, STATE_TTL_SECONDS))
      ) {
        if ((await store.get(key + ":reply")) !== encoded)
          throw new Error("footer_reply_conflict");
      }
      // 登记晚于终态通知时补一次后台执行；运行中登记不轮询，等待完成 Hook。
      if (isTerminal(run.status) || (await store.get(key + ":event")))
        startFooter(config, store, parseRunRef(ref), fetcher);
      return json({
        action: "registered",
        taskId: ref.taskId,
        messageTs: ref.messageTs,
      });
    },
  );
}

function isTerminal(status: string): boolean {
  return ["completed", "failed", "cancelled"].includes(status);
}

export async function processFooter(
  ref: RunRef,
  config: FooterConfig,
  store: ThreadStore,
  fetchImpl: typeof fetch,
) {
  const key = footerKey(config, ref);
  const [done, raw] = await Promise.all([
    store.get(key + ":done"),
    store.get(key + ":reply"),
  ]);
  if (done) return { action: "duplicate" };
  if (!raw) return { action: "waiting_for_reply" };
  const binding: Binding = JSON.parse(raw);
  parseReplyRef(binding);
  if (binding.taskId !== ref.taskId || binding.issueId !== ref.issueId)
    throw new Error("footer_scope_mismatch");
  // 有回复后领取单次机会；失败或终态数据尚未可读也不释放，重复回调不能隐式重试。
  if (
    !(await store.setIfAbsent(
      key + ":attempt",
      randomUUID(),
      STATE_TTL_SECONDS,
    ))
  )
    return { action: "duplicate" };
  const { run, event } = await readScopedRun(config, ref, fetchImpl);
  if (
    event.channelId !== binding.channelId ||
    event.threadTs !== binding.threadTs
  )
    throw new Error("footer_scope_mismatch");
  if (!isTerminal(run.status))
    return { action: "skipped", reason: "run_not_terminal" };
  const [logs, message] = await Promise.all([
    readRunLogStats(config, ref, fetchImpl),
    readOwnReply(config, binding, fetchImpl),
  ]);
  const footer = buildRunFooter(run, logs);
  if (!footer) return { action: "skipped", reason: "missing_stats" };
  if (replyBodyDigest(message, ref.taskId) !== binding.bodyDigest)
    throw new Error("footer_body_changed");
  const action = await updateReplyFooter(
    config,
    binding,
    message,
    footer,
    fetchImpl,
  );
  if (action === "unsupported")
    return { action: "skipped", reason: "unsupported_message" };
  await store.set(
    key + ":done",
    JSON.stringify({ messageTs: binding.messageTs, footer }),
    STATE_TTL_SECONDS,
  );
  return { action, messageTs: binding.messageTs };
}

// 旧队列、Cron 与运维调用仅确认退役；已排队消息不能重新触发 Footer。
export async function retireFooter(): Promise<Response> {
  return json({ action: "disabled" });
}
