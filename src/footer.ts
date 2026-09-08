import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import { Receiver } from "@upstash/qstash";
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
  buildDurationFooter,
  readOwnReply,
  replyBodyDigest,
  updateReplyFooter,
} from "./footer-slack.js";
import { json, readBody } from "./http.js";
import { digest, STATE_TTL_SECONDS } from "./thread-router.js";
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

async function publish(
  config: FooterConfig,
  ref: RunRef,
  source: "hook" | "reply",
  fetchImpl: typeof fetch,
) {
  const response = await fetchImpl(
    `${config.queueUrl}/v2/publish/${config.footerConsumerUrl}`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${config.queueToken}`,
        "content-type": "application/json",
        "Upstash-Deduplication-Id": digest(
          `${config.footerConsumerUrl}:${ref.taskId}:${source}:v1`,
        ),
        "Upstash-Retries": "3",
        "Upstash-Timeout": "50s",
        "Upstash-Flow-Control-Key": digest(
          `${config.footerConsumerUrl}:${ref.taskId}`,
        ),
        "Upstash-Flow-Control-Value": "parallelism=1",
      },
      body: JSON.stringify(ref),
      signal: AbortSignal.timeout(2000),
      redirect: "error",
    },
  );
  if (!response.ok) throw new Error("footer_queue_unavailable");
  const body: unknown = await response.json();
  if (!isRecord(body) || typeof body.messageId !== "string")
    throw new Error("footer_queue_unavailable");
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
  try {
    return await handler(config, store, raw, fetcher);
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
      if (body.event_type !== "task.completed")
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
      const key = footerKey(config, ref);
      if (await store.get(key + ":done")) return json({ action: "duplicate" });
      // 不保存 callback_token；它在此次 HTTP 返回后即失效。持久化事实与队列确认分开。
      await store.set(key + ":event", JSON.stringify(ref), STATE_TTL_SECONDS);
      await publish(config, ref, "hook", fetcher);
      return json({ action: "accepted" }, 202);
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
      // 两种到达顺序都闭合：完成通知已消费却尚未登记时，由登记端重新唤醒。
      if (run.status === "completed" || (await store.get(key + ":event")))
        await publish(config, parseRunRef(ref), "reply", fetcher);
      return json({
        action: "registered",
        taskId: ref.taskId,
        messageTs: ref.messageTs,
      });
    },
  );
}

export async function processFooter(
  ref: RunRef,
  config: FooterConfig,
  store: ThreadStore,
  fetchImpl: typeof fetch,
) {
  const key = footerKey(config, ref),
    owner = randomUUID();
  if (!(await store.setIfAbsent(key + ":lock", owner, 120)))
    throw new Error("footer_busy");
  try {
    if (await store.get(key + ":done")) return { action: "duplicate" };
    const raw = await store.get(key + ":reply");
    if (!raw) return { action: "waiting_for_reply" };
    const binding: Binding = JSON.parse(raw);
    parseReplyRef(binding);
    if (binding.taskId !== ref.taskId || binding.issueId !== ref.issueId)
      throw new Error("footer_scope_mismatch");
    const { run, event } = await readScopedRun(config, ref, fetchImpl);
    if (
      event.channelId !== binding.channelId ||
      event.threadTs !== binding.threadTs
    )
      throw new Error("footer_scope_mismatch");
    if (run.status !== "completed") throw new Error("footer_run_not_completed");
    const footer = buildDurationFooter(run);
    if (!footer) return { action: "skipped", reason: "missing_duration" };
    const message = await readOwnReply(config, binding, fetchImpl);
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
  } finally {
    await store.releaseIfOwner(key + ":lock", owner);
  }
}

export async function consumeFooter(
  request: Request,
  env = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  return withFooter(
    request,
    env,
    fetchImpl,
    async (config, store, raw, fetcher) => {
      try {
        if (
          !(await new Receiver({
            currentSigningKey: config.queueCurrentSigningKey,
            nextSigningKey: config.queueNextSigningKey,
            devMode: false,
          }).verify({
            body: raw,
            signature: request.headers.get("upstash-signature") ?? "",
            url: config.footerConsumerUrl,
          }))
        )
          return json({ error: "invalid_queue_signature" }, 401);
      } catch {
        return json({ error: "invalid_queue_signature" }, 401);
      }
      return json(
        await processFooter(
          parseRunRef(JSON.parse(raw)),
          config,
          store,
          fetcher,
        ),
      );
    },
  );
}
