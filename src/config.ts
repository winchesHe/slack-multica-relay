export interface RelayConfig {
  signingSecret: string;
  multicaWebhookUrl: string;
  multicaApiBaseUrl?: string;
  multicaApiToken?: string;
  multicaWorkspaceId?: string;
  multicaAutopilotId?: string;
  slackReactionToken: string;
  slackReadToken: string;
  slackReactionName: string;
  targetUserIds: Set<string>;
  targetSubteamIds: Set<string>;
  threadRoutingEnabled: boolean;
  threadMappingTtlSeconds: number;
  threadLockTtlSeconds: number;
  kvRestApiUrl?: string;
  kvRestApiToken?: string;
}

export function loadRelayConfig(env: NodeJS.ProcessEnv = process.env): RelayConfig {
  const threadRoutingEnabled = parseBoolean(env.SLACK_THREAD_ROUTING_ENABLED, false);
  return {
    signingSecret: required(env.SLACK_SIGNING_SECRET, 'SLACK_SIGNING_SECRET'),
    multicaWebhookUrl: required(env.MULTICA_WEBHOOK_URL, 'MULTICA_WEBHOOK_URL'),
    slackReactionToken: required(env.SLACK_REACTION_TOKEN, 'SLACK_REACTION_TOKEN'),
    slackReadToken: required(env.SLACK_READ_TOKEN?.trim() || env.SLACK_REACTION_TOKEN, 'SLACK_READ_TOKEN'),
    slackReactionName: normalizeReactionName(required(env.SLACK_REACTION_NAME, 'SLACK_REACTION_NAME')),
    targetUserIds: csvSet(env.SLACK_TARGET_USER_IDS),
    targetSubteamIds: csvSet(env.SLACK_TARGET_SUBTEAM_IDS),
    threadRoutingEnabled,
    ...(threadRoutingEnabled ? {
      multicaApiBaseUrl: (env.MULTICA_API_BASE_URL ?? 'https://multica.devops.moego.dev').replace(/\/+$/u, ''),
      multicaApiToken: required(env.MULTICA_API_TOKEN, 'MULTICA_API_TOKEN'),
      multicaWorkspaceId: required(env.MULTICA_WORKSPACE_ID, 'MULTICA_WORKSPACE_ID'),
      multicaAutopilotId: required(env.MULTICA_AUTOPILOT_ID, 'MULTICA_AUTOPILOT_ID'),
      kvRestApiUrl: required(env.KV_REST_API_URL ?? env.UPSTASH_REDIS_REST_URL, 'KV_REST_API_URL').replace(/\/+$/u, ''),
      kvRestApiToken: required(env.KV_REST_API_TOKEN ?? env.UPSTASH_REDIS_REST_TOKEN, 'KV_REST_API_TOKEN'),
    } : {}),
    threadMappingTtlSeconds: positiveInt(env.SLACK_THREAD_MAPPING_TTL_SECONDS, 90 * 24 * 60 * 60),
    threadLockTtlSeconds: positiveInt(env.SLACK_THREAD_LOCK_TTL_SECONDS, 30),
  };
}

function normalizeReactionName(value: string): string {
  return value.replace(/^:+|:+$/gu, '');
}

function required(value: string | undefined, name: string): string {
  if (!value?.trim()) throw new Error(`Missing ${name}`);
  return value.trim();
}

function csvSet(value: string | undefined): Set<string> {
  return new Set((value ?? '').split(',').map((item) => item.trim()).filter(Boolean));
}

function parseBoolean(value: string | undefined, fallback: boolean): boolean {
  if (value === undefined || value.trim() === '') return fallback;
  return ['1', 'true', 'yes', 'on'].includes(value.trim().toLowerCase());
}

function positiveInt(value: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}
