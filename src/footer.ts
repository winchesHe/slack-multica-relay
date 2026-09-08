import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import { Receiver } from "@upstash/qstash";
import {
  buildRunFooter,
  readRunLogStats,
  type LogStats,
} from "./footer-stats.js";
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
import { digest, STATE_TTL_SECONDS } from "./thread-router.js";
import {
  UpstashFooterStore,
  readRecoveryState,
  FALLBACK_DELAY_MS,
  MAX_RECOVERY_AGE_MS,
  MAX_RECOVERY_ERRORS,
  type FooterStore,
  type RecoveryState,
} from "./footer-store.js";

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
  source: string,
  fetchImpl: typeof fetch,
  dueAt = 0,
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
        ...(dueAt > Date.now()
          ? { "Upstash-Delay": `${Math.ceil((dueAt - Date.now()) / 1000)}s` }
          : {}),
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
    store: FooterStore,
    raw: string,
    fetcher: typeof fetch,
  ) => Promise<Response>,
  method = "POST",
) {
  if (request.method !== method)
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
  const store = new UpstashFooterStore(config, fetcher);
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
      await store.track(parseRunRef(ref), Date.now());
      if (
        !(await store.setIfAbsent(key + ":reply", encoded, STATE_TTL_SECONDS))
      ) {
        if ((await store.get(key + ":reply")) !== encoded)
          throw new Error("footer_reply_conflict");
      }
      // 两种到达顺序都闭合：完成通知已消费却尚未登记时，由登记端重新唤醒。
      if (isTerminal(run.status) || (await store.get(key + ":event")))
        await publish(config, parseRunRef(ref), "reply", fetcher);
      else
        await publish(
          config,
          parseRunRef(ref),
          "registered",
          fetcher,
          Date.now() + FALLBACK_DELAY_MS,
        );
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

async function stopRecovery(
  ref: RunRef,
  config: FooterConfig,
  store: FooterStore,
  reason: string,
) {
  await store.set(
    footerKey(config, ref) + ":stopped",
    JSON.stringify({ reason, stoppedAt: Date.now() }),
    STATE_TTL_SECONDS,
  );
  await store.finish(ref);
  console.warn("relay_footer_stopped", {
    taskId: ref.taskId,
    issueId: ref.issueId,
    reason,
  });
  return { action: "stopped", reason };
}

export async function processFooter(
  ref: RunRef,
  config: FooterConfig,
  store: FooterStore,
  fetchImpl: typeof fetch,
) {
  const key = footerKey(config, ref),
    owner = randomUUID();
  if (!(await store.setIfAbsent(key + ":lock", owner, 120)))
    throw new Error("footer_busy");
  let state: RecoveryState | undefined;
  try {
    if (await store.get(key + ":done")) {
      await store.finish(ref);
      return { action: "duplicate" };
    }
    const stopped = await store.get(key + ":stopped");
    if (stopped) {
      await store.finish(ref);
      return { action: "stopped", reason: JSON.parse(stopped).reason };
    }
    const raw = await store.get(key + ":reply");
    if (!raw) {
      const tracked = await store.get(key + ":recovery");
      if (
        tracked &&
        Date.now() - readRecoveryState(tracked).firstSeenAt >
          MAX_RECOVERY_AGE_MS
      )
        return await stopRecovery(ref, config, store, "reply_missing");
      if (!tracked) await store.finish(ref);
      return { action: "waiting_for_reply" };
    }
    const binding: Binding = JSON.parse(raw);
    parseReplyRef(binding);
    if (binding.taskId !== ref.taskId || binding.issueId !== ref.issueId)
      throw new Error("footer_scope_mismatch");
    await store.track(ref, Date.now());
    state = readRecoveryState(await store.get(key + ":recovery"));
    if (state.nextCheckAt > Date.now()) {
      await publish(
        config,
        ref,
        `stats-${state.statsReads}`,
        fetchImpl,
        state.nextCheckAt,
      );
      return { action: "deferred", reason: "stats_pending" };
    }
    const { run, event } = await readScopedRun(config, ref, fetchImpl);
    if (
      event.channelId !== binding.channelId ||
      event.threadTs !== binding.threadTs
    )
      throw new Error("footer_scope_mismatch");
    if (!isTerminal(run.status)) {
      if (Date.now() - state.firstSeenAt > MAX_RECOVERY_AGE_MS)
        return await stopRecovery(ref, config, store, "run_not_terminal");
      const dueAt =
        (Math.floor(Date.now() / FALLBACK_DELAY_MS) + 1) * FALLBACK_DELAY_MS;
      await store.schedule(ref, dueAt);
      await publish(config, ref, `running-${dueAt}`, fetchImpl, dueAt);
      return { action: "deferred", reason: "run_not_terminal" };
    }
    let footer = await store.get(key + ":stats");
    if (!footer) {
      state.statsReads++;
      state.nextCheckAt = 0;
      await store.set(
        key + ":recovery",
        JSON.stringify(state),
        STATE_TTL_SECONDS,
      );
      const cachedLogs = await store.get(key + ":logstats");
      let logs: LogStats = {};
      if (cachedLogs) logs = JSON.parse(cachedLogs);
      else if (state.statsReads <= 3)
        logs = await readRunLogStats(config, ref, fetchImpl);
      if (!cachedLogs && (logs.tools !== undefined || state.statsReads >= 3))
        await store.set(
          key + ":logstats",
          JSON.stringify(logs),
          STATE_TTL_SECONDS,
        );
      if (Array.isArray(run.usage) && run.usage.length)
        await store.set(
          key + ":usage",
          JSON.stringify(run.usage),
          STATE_TTL_SECONDS,
        );
      else {
        const cachedUsage = await store.get(key + ":usage");
        if (cachedUsage) run.usage = JSON.parse(cachedUsage);
      }
      const usageMissing = !Array.isArray(run.usage) || !run.usage.length;
      if ((usageMissing || logs.tools === undefined) && state.statsReads < 3) {
        state.nextCheckAt =
          Date.now() + (state.statsReads === 1 ? 30000 : 120000);
        await store.set(
          key + ":recovery",
          JSON.stringify(state),
          STATE_TTL_SECONDS,
        );
        await store.schedule(ref, state.nextCheckAt);
        await publish(
          config,
          ref,
          `stats-${state.statsReads}`,
          fetchImpl,
          state.nextCheckAt,
        );
        return { action: "deferred", reason: "stats_pending" };
      }
      footer = buildRunFooter(run, logs) ?? null;
      if (!footer)
        return await stopRecovery(ref, config, store, "missing_stats");
      // Slack 写前冻结展示；写响应丢失后的重试复用，不能重复取日志或改变统计。
      await store.set(key + ":stats", footer, STATE_TTL_SECONDS);
    }
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
      return await stopRecovery(ref, config, store, "unsupported_message");
    await store.set(
      key + ":done",
      JSON.stringify({ messageTs: binding.messageTs, footer }),
      STATE_TTL_SECONDS,
    );
    await store.finish(ref);
    return { action, messageTs: binding.messageTs };
  } catch (error) {
    const reason =
      error instanceof Error && /^footer_[a-z_]+$/u.test(error.message)
        ? error.message
        : "footer_unavailable";
    if (
      [
        "footer_scope_mismatch",
        "footer_author_mismatch",
        "footer_body_changed",
        "footer_message_invalid",
        "footer_recovery_invalid",
      ].includes(reason)
    )
      return await stopRecovery(ref, config, store, reason);
    if (state) {
      state.errors++;
      await store.set(
        key + ":recovery",
        JSON.stringify(state),
        STATE_TTL_SECONDS,
      );
      if (
        state.errors >= MAX_RECOVERY_ERRORS ||
        Date.now() - state.firstSeenAt > MAX_RECOVERY_AGE_MS
      )
        return await stopRecovery(ref, config, store, reason);
      await store.schedule(ref, Date.now() + FALLBACK_DELAY_MS);
    }
    throw error;
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

function validRecoveryToken(request: Request, env: NodeJS.ProcessEnv): boolean {
  const secret = env.CRON_SECRET?.trim() ?? "";
  return (
    secret.length >= 32 &&
    safeEqual(request.headers.get("authorization") ?? "", `Bearer ${secret}`)
  );
}

export async function recoverFooters(
  request: Request,
  env = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  if (!validRecoveryToken(request, env))
    return json({ error: "invalid_recovery_token" }, 401);
  if (env.RELAY_FOOTER_ENABLED !== "true") return json({ action: "disabled" });
  return withFooter(
    request,
    env,
    fetchImpl,
    async (config, store, _raw, fetcher) => {
      const refs = await store.claimDue(Date.now(), 20);
      let published = 0,
        finished = 0,
        failed = 0;
      for (let offset = 0; offset < refs.length; offset += 5) {
        const results = await Promise.allSettled(
          refs.slice(offset, offset + 5).map(async (ref) => {
            const key = footerKey(config, ref);
            if (
              (await store.get(key + ":done")) ||
              (await store.get(key + ":stopped"))
            ) {
              await store.finish(ref);
              return "finished";
            }
            await publish(
              config,
              ref,
              `recovery-${Math.floor(Date.now() / 3600000)}`,
              fetcher,
            );
            return "published";
          }),
        );
        for (const result of results) {
          if (result.status === "rejected") failed++;
          else if (result.value === "finished") finished++;
          else published++;
        }
      }
      console.info("relay_footer_recovery", {
        claimed: refs.length,
        published,
        finished,
        failed,
      });
      // 失败项仍由索引持有，下次领取或 Cron 重试继续；不把部分成功当全部完成。
      return json(
        { claimed: refs.length, published, finished, failed },
        failed ? 503 : 200,
      );
    },
    "GET",
  );
}

export async function manageFooterRecovery(
  request: Request,
  env = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  if (!validRecoveryToken(request, env))
    return json({ error: "invalid_recovery_token" }, 401);
  return withFooter(
    request,
    env,
    fetchImpl,
    async (config, store, raw, fetcher) => {
      const body: unknown = JSON.parse(raw);
      if (
        !isRecord(body) ||
        !["inspect", "retry"].includes(String(body.operation))
      )
        throw new Error("invalid_footer_request");
      const ref = parseRunRef(body),
        key = footerKey(config, ref);
      if (body.operation === "inspect") {
        const read = async (suffix: string) => {
          const value = await store.get(key + suffix);
          return value ? JSON.parse(value) : null;
        };
        const [state, stopped, done, binding] = await Promise.all([
          read(":recovery"),
          read(":stopped"),
          read(":done"),
          read(":reply"),
        ]);
        return json({
          ...ref,
          state,
          stopped,
          done: !!done,
          messageTs: binding?.messageTs ?? null,
        });
      }
      const owner = randomUUID();
      if (!(await store.setIfAbsent(key + ":lock", owner, 120)))
        throw new Error("footer_busy");
      try {
        if (await store.get(key + ":done"))
          return json({ action: "duplicate" });
        const bindingRaw = await store.get(key + ":reply");
        if (!bindingRaw) throw new Error("footer_message_missing");
        const binding: Binding = JSON.parse(bindingRaw);
        parseReplyRef(binding);
        const { event } = await readScopedRun(config, ref, fetcher);
        if (
          binding.taskId !== ref.taskId ||
          binding.issueId !== ref.issueId ||
          binding.channelId !== event.channelId ||
          binding.threadTs !== event.threadTs
        )
          throw new Error("footer_scope_mismatch");
        const message = await readOwnReply(config, binding, fetcher);
        if (replyBodyDigest(message, ref.taskId) !== binding.bodyDigest)
          throw new Error("footer_body_changed");
        const stopped = await store.get(key + ":stopped");
        const state: RecoveryState = {
          version: 1,
          firstSeenAt: Date.now(),
          errors: 0,
          statsReads: 0,
          nextCheckAt: 0,
        };
        await store.set(
          key + ":recovery",
          JSON.stringify(state),
          STATE_TTL_SECONDS,
        );
        if (stopped) await store.releaseIfOwner(key + ":stopped", stopped);
        await store.track(ref, Date.now());
        await store.schedule(ref, Date.now());
        await publish(config, ref, `manual-${owner}`, fetcher);
        return json({ action: "accepted", ...ref }, 202);
      } finally {
        await store.releaseIfOwner(key + ":lock", owner);
      }
    },
  );
}
