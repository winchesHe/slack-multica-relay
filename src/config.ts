import type { ApiConfig } from "./multica-api.js";
export interface ExtraRouteConfig extends ApiConfig {
  id: string;
  mode: "existing_thread_only";
  allowedChannelIds: string[];
  ignoreTextSubstrings?: string[];
}
export interface RelayConfig extends ApiConfig {
  extraRoutes: ExtraRouteConfig[];
  signingSecret: string;
  teamId: string;
  targetUserIds: Set<string>;
  targetSubteamIds: Set<string>;
  cancelKeywords: Set<string>;
  allowedChannelIds: Set<string>;
  allowAllChannels: boolean;
  blockedChannelIds: Set<string>;
  allowedSenderIds: Set<string>;
  allowAllSenders: boolean;
  blockedSenderIds: Set<string>;
  slackReactionToken: string;
  slackReactionName: string;
  kvRestApiUrl: string;
  kvRestApiToken: string;
  queueUrl: string;
  queueToken: string;
  queueCurrentSigningKey: string;
  queueNextSigningKey: string;
  consumerUrl: string;
}
export function loadRelayConfig(
  env: NodeJS.ProcessEnv = process.env,
): RelayConfig {
  const allowedChannels = policyIds(required(env, "SLACK_ALLOWED_CHANNEL_IDS"));
  const blockedChannelIds = ids(env.SLACK_BLOCKED_CHANNEL_IDS);
  const allowedSenders = policyIds(env.SLACK_ALLOWED_SENDER_IDS || "all");
  const blockedSenderIds = ids(env.SLACK_BLOCKED_SENDER_IDS);
  const targetUserIds = ids(env.SLACK_TARGET_USER_IDS);
  const targetSubteamIds = ids(env.SLACK_TARGET_SUBTEAM_IDS);
  if (!targetUserIds.size && !targetSubteamIds.size)
    throw new Error("missing_mention_target");
  const type = env.MULTICA_ASSIGNEE_TYPE?.trim() || "agent";
  if (type !== "agent" && type !== "squad") throw new Error("invalid_assignee_type");
  const targetId = env.MULTICA_ASSIGNEE_ID?.trim() || (type === "agent" ? env.MULTICA_AGENT_ID?.trim() : undefined);
  if (!targetId) throw new Error("relay_not_configured");
  const legacy = env.MULTICA_LEGACY_AGENT_ID?.trim() || undefined;
  const scope = env.MULTICA_THREAD_SCOPE_ID?.trim() || undefined;
  if (legacy && (type !== "squad" || scope !== legacy)) throw new Error("invalid_migration_scope");
  return {
    extraRoutes: extraRoutes(env),
    signingSecret: required(env, "SLACK_SIGNING_SECRET"),
    teamId: required(env, "SLACK_TEAM_ID"),
    allowedChannelIds: allowedChannels.ids,
    allowAllChannels: allowedChannels.all,
    blockedChannelIds,
    allowedSenderIds: allowedSenders.ids,
    allowAllSenders: allowedSenders.all,
    blockedSenderIds,
    targetUserIds,
    targetSubteamIds,
    cancelKeywords: cancelKeywords(env.SLACK_CANCEL_KEYWORDS),
    multicaApiBaseUrl: https(required(env, "MULTICA_API_BASE_URL")),
    multicaApiToken: required(env, "MULTICA_API_TOKEN"),
    multicaWorkspaceId: required(env, "MULTICA_WORKSPACE_ID"),
    multicaProjectId: required(env, "MULTICA_PROJECT_ID"),
    multicaAssigneeType: type,
    multicaAssigneeId: targetId,
    multicaThreadScopeId: scope,
    multicaLegacyAgentId: legacy,
    slackReactionToken:
      env.SLACK_BOT_TOKEN?.trim() || required(env, "SLACK_USER_TOKEN"),
    slackReactionName: required(env, "SLACK_REACTION_NAME").replace(
      /^:+|:+$/gu,
      "",
    ),
    kvRestApiUrl: https(required(env, "KV_REST_API_URL")),
    kvRestApiToken: required(env, "KV_REST_API_TOKEN"),
    queueUrl: https(env.QSTASH_URL?.trim() || "https://qstash.upstash.io"),
    queueToken: required(env, "QSTASH_TOKEN"),
    queueCurrentSigningKey: required(env, "QSTASH_CURRENT_SIGNING_KEY"),
    queueNextSigningKey: required(env, "QSTASH_NEXT_SIGNING_KEY"),
    consumerUrl: https(required(env, "RELAY_CONSUMER_URL")),
  };
}
function required(env: NodeJS.ProcessEnv, key: string): string {
  const value = env[key]?.trim();
  if (!value) throw new Error("relay_not_configured");
  return value;
}
function ids(value: string | undefined): Set<string> {
  const values = (value ?? "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
  if (values.some((x) => !/^[A-Z][A-Z0-9]+$/u.test(x)))
    throw new Error("invalid_identifier");
  return new Set(values);
}
function policyIds(value: string): { ids: Set<string>; all: boolean } {
  const normalized = value.trim();
  if (normalized.toLowerCase() === "all") return { ids: new Set(), all: true };
  const parsed = ids(normalized);
  if (!parsed.size) throw new Error("invalid_allowlist");
  return { ids: parsed, all: false };
}
function https(value: string): string {
  const url = new URL(value);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.hash ||
    url.search
  )
    throw new Error("invalid_service_url");
  return value.replace(/\/+$/u, "");
}

function cancelKeywords(value: string | undefined): Set<string> {
  const keywords = (value ?? "")
    .split(",")
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);
  return new Set(keywords.length ? keywords : ["cancel", "取消"]);
}

function extraRoutes(env: NodeJS.ProcessEnv): ExtraRouteConfig[] {
  const raw = env.RELAY_EXTRA_ROUTES_JSON?.trim();
  if (!raw) return [];
  const fail = (field: string): never => {
    // 只报告字段位置，禁止将可能含凭据的 JSON 或变量值写进错误。
    throw new Error(`invalid_extra_routes:${field}`);
  };
  let routes: unknown;
  try { routes = JSON.parse(raw); } catch { return fail("json"); }
  if (!Array.isArray(routes)) return fail("array");
  const seen = new Set<string>();
  const fields = new Set([
    "id", "mode", "allowedChannelIds", "multicaWorkspaceId", "multicaProjectId",
    "multicaAssigneeType", "multicaAssigneeId", "allowedAssigneeIds",
    "multicaThreadScopeId", "multicaApiBaseUrl", "multicaApiTokenEnv", "ignoreTextSubstrings",
  ]);
  return routes.map((value: unknown, index: number) => {
    const prefix = `[${index}]`;
    if (!value || typeof value !== "object" || Array.isArray(value)) return fail(prefix);
    const route = value as Record<string, unknown>;
    if (Object.keys(route).some((key) => !fields.has(key))) return fail(`${prefix}.unknown_field`);
    const string = (field: string): string => {
      const item = route[field];
      if (typeof item !== "string" || !item.trim() || item !== item.trim()) return fail(`${prefix}.${field}`);
      return item;
    };
    const strings = (field: string, nonempty: boolean): string[] => {
      const items = route[field];
      if (!Array.isArray(items) || (nonempty && !items.length) ||
        items.some((item) => typeof item !== "string" || !item.trim()))
        return fail(`${prefix}.${field}`);
      return items as string[];
    };
    const id = string("id");
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/u.test(id) || seen.has(id)) return fail(`${prefix}.id`);
    seen.add(id);
    if (route.mode !== "existing_thread_only") return fail(`${prefix}.mode`);
    const channels = strings("allowedChannelIds", true);
    if (channels.some((channel) => !/^[A-Z][A-Z0-9]+$/u.test(channel)))
      return fail(`${prefix}.allowedChannelIds`);
    const type = route.multicaAssigneeType === undefined ? "agent" : route.multicaAssigneeType;
    if (type !== "agent" && type !== "squad") return fail(`${prefix}.multicaAssigneeType`);
    let base = required(env, "MULTICA_API_BASE_URL");
    if (route.multicaApiBaseUrl !== undefined) base = string("multicaApiBaseUrl");
    try { base = https(base); } catch { return fail(`${prefix}.multicaApiBaseUrl`); }
    let token = required(env, "MULTICA_API_TOKEN");
    if (route.multicaApiTokenEnv !== undefined) {
      const key = string("multicaApiTokenEnv");
      if (!/^[A-Z][A-Z0-9_]*$/u.test(key)) return fail(`${prefix}.multicaApiTokenEnv`);
      const referenced = env[key]?.trim();
      if (!referenced) return fail(`${prefix}.multicaApiTokenEnv:missing_env:${key}`);
      token = referenced;
    }
    return {
      id, mode: "existing_thread_only", allowedChannelIds: channels,
      multicaApiBaseUrl: base, multicaApiToken: token,
      multicaWorkspaceId: string("multicaWorkspaceId"),
      multicaProjectId: string("multicaProjectId"),
      multicaAssigneeType: type, multicaAssigneeId: string("multicaAssigneeId"),
      ...(route.allowedAssigneeIds !== undefined
        ? { multicaAllowedAssigneeIds: strings("allowedAssigneeIds", true) } : {}),
      ...(route.multicaThreadScopeId !== undefined
        ? { multicaThreadScopeId: string("multicaThreadScopeId") } : {}),
      ...(route.ignoreTextSubstrings !== undefined
        ? { ignoreTextSubstrings: strings("ignoreTextSubstrings", false) } : {}),
    };
  });
}
