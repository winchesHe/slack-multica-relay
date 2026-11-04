const SLACK_USERS_INFO_URL = "https://slack.com/api/users.info";
export const SLACK_AUTHOR_LOOKUP_BUDGET_MS = 700;

export class SlackAuthorLookupError extends Error {
  constructor(readonly code: "request_failed" | "http_error" | "invalid_response") {
    super(code);
    this.name = "SlackAuthorLookupError";
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

// 按作者身份判断，真人使用 User Token 发消息也可能带 bot_id/app_id 等来源标记。
export async function lookupSlackAuthor(
  userId: string,
  token: string,
  fetchImpl: typeof fetch = fetch,
): Promise<{ id: string; isBot: boolean }> {
  let response: Response;
  try {
    response = await fetchImpl(`${SLACK_USERS_INFO_URL}?user=${encodeURIComponent(userId)}`, {
      headers: { authorization: `Bearer ${token}`, accept: "application/json" },
      signal: AbortSignal.timeout(SLACK_AUTHOR_LOOKUP_BUDGET_MS),
    });
  } catch {
    throw new SlackAuthorLookupError("request_failed");
  }
  if (!response.ok) throw new SlackAuthorLookupError("http_error");
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new SlackAuthorLookupError("invalid_response");
  }
  if (
    !record(body) || body.ok !== true || !record(body.user) ||
    body.user.id !== userId || typeof body.user.is_bot !== "boolean"
  ) {
    throw new SlackAuthorLookupError("invalid_response");
  }
  return { id: userId, isBot: body.user.is_bot };
}
