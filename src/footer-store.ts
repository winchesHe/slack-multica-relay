import type { FooterConfig } from "./footer-config.js";
import { footerKey, parseRunRef, type RunRef } from "./footer-data.js";
import { STATE_TTL_SECONDS } from "./thread-router.js";
import { UpstashThreadStore, type ThreadStore } from "./thread-store.js";

export interface RecoveryState {
  version: 1;
  firstSeenAt: number;
  errors: number;
  statsReads: number;
  nextCheckAt: number;
}
export interface FooterStore extends ThreadStore {
  track(ref: RunRef, now: number): Promise<void>;
  schedule(ref: RunRef, dueAt: number): Promise<void>;
  finish(ref: RunRef): Promise<void>;
  claimDue(now: number, limit: number): Promise<RunRef[]>;
}
export const FALLBACK_DELAY_MS = 15 * 60 * 1000;
export const MAX_RECOVERY_AGE_MS = 7 * 24 * 60 * 60 * 1000;
export const MAX_RECOVERY_ERRORS = 12;

export class UpstashFooterStore
  extends UpstashThreadStore
  implements FooterStore
{
  constructor(
    private readonly config: FooterConfig,
    fetchImpl: typeof fetch,
  ) {
    super(config.kvRestApiUrl, config.kvRestApiToken, fetchImpl);
  }
  private get indexKey(): string {
    return (
      footerKey(this.config, { version: 1, issueId: "", taskId: "" }) +
      "pending"
    );
  }
  async track(ref: RunRef, now: number): Promise<void> {
    const key = footerKey(this.config, ref);
    const state: RecoveryState = {
      version: 1,
      firstSeenAt: now,
      errors: 0,
      statsReads: 0,
      nextCheckAt: 0,
    };
    // 索引和恢复起点原子保存；先登记恢复责任再写回复映射，进程中断也能被扫描发现。
    await this.command([
      "EVAL",
      `-- footer_track
if redis.call('exists', KEYS[3]) == 1 or redis.call('exists', KEYS[4]) == 1 then return 0 end
redis.call('set', KEYS[2], ARGV[2], 'NX', 'EX', ARGV[4])
redis.call('zadd', KEYS[1], 'NX', ARGV[3], ARGV[1])
redis.call('expire', KEYS[1], ARGV[4])
return 1`,
      "4",
      this.indexKey,
      key + ":recovery",
      key + ":done",
      key + ":stopped",
      JSON.stringify(parseRunRef(ref)),
      JSON.stringify(state),
      String(now + FALLBACK_DELAY_MS),
      String(STATE_TTL_SECONDS),
    ]);
  }
  async schedule(ref: RunRef, dueAt: number): Promise<void> {
    const key = footerKey(this.config, ref);
    await this.command([
      "EVAL",
      `-- footer_schedule
if redis.call('exists', KEYS[2]) == 1 or redis.call('exists', KEYS[3]) == 1 then return 0 end
redis.call('zadd', KEYS[1], ARGV[2], ARGV[1])
redis.call('expire', KEYS[1], ARGV[3])
return 1`,
      "3",
      this.indexKey,
      key + ":done",
      key + ":stopped",
      JSON.stringify(parseRunRef(ref)),
      String(dueAt),
      String(STATE_TTL_SECONDS),
    ]);
  }
  async finish(ref: RunRef): Promise<void> {
    await this.command([
      "ZREM",
      this.indexKey,
      JSON.stringify(parseRunRef(ref)),
    ]);
  }
  async claimDue(now: number, limit: number): Promise<RunRef[]> {
    // 扫描只领取少量到期项；并发扫描互不重复，发布中断后一小时即可重新领取。
    const rows = await this.command([
      "EVAL",
      `-- footer_claim
local rows = redis.call('zrangebyscore', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, member in ipairs(rows) do redis.call('zadd', KEYS[1], ARGV[3], member) end
return rows`,
      "1",
      this.indexKey,
      String(now),
      String(limit),
      String(now + 60 * 60 * 1000),
    ]);
    if (!Array.isArray(rows) || rows.some((v) => typeof v !== "string"))
      throw new Error("footer_recovery_invalid");
    return rows.map((row) => parseRunRef(JSON.parse(row as string)));
  }
}

export function readRecoveryState(raw: string | null): RecoveryState {
  if (!raw) throw new Error("footer_recovery_missing");
  let state: RecoveryState;
  try {
    state = JSON.parse(raw) as RecoveryState;
  } catch {
    throw new Error("footer_recovery_invalid");
  }
  if (
    !state ||
    state.version !== 1 ||
    [state.firstSeenAt, state.errors, state.statsReads, state.nextCheckAt].some(
      (v) => !Number.isSafeInteger(v) || v < 0,
    )
  )
    throw new Error("footer_recovery_invalid");
  return state;
}
