export interface MulticaIssue {
  id: string;
  title: string;
  description?: string | null;
  project_id?: string | null;
  assignee_id?: string | null;
  assignee_type?: string | null;
}
export interface MulticaComment {
  id: string;
  content: string;
  trigger_outcomes?: unknown;
}
export interface ApiConfig {
  multicaApiBaseUrl: string;
  multicaApiToken: string;
  multicaWorkspaceId: string;
  multicaProjectId: string;
  multicaAgentId: string;
}
export class ApiError extends Error {
  constructor(readonly status: number) {
    super("multica_http_error");
  }
}
function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function issue(value: unknown): MulticaIssue {
  if (
    !object(value) ||
    typeof value.id !== "string" ||
    typeof value.title !== "string"
  )
    throw new Error("invalid_multica_response");
  return value as unknown as MulticaIssue;
}
async function api(
  config: ApiConfig,
  path: string,
  init: RequestInit = {},
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  return fetchImpl(config.multicaApiBaseUrl + path, {
    ...init,
    headers: {
      authorization: `Bearer ${config.multicaApiToken}`,
      "content-type": "application/json",
      "x-workspace-id": config.multicaWorkspaceId,
      ...init.headers,
    },
    signal: AbortSignal.timeout(8000),
  });
}
export async function findIssue(
  config: ApiConfig,
  marker: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaIssue | undefined> {
  for (let offset = 0; offset < 10000; offset += 100) {
    const query = new URLSearchParams({
      project_id: config.multicaProjectId,
      limit: "100",
      offset: String(offset),
      sort: "created_at",
      direction: "asc",
    });
    const response = await api(config, "/api/issues?" + query, {}, fetchImpl);
    if (!response.ok) throw new ApiError(response.status);
    const body: unknown = await response.json();
    if (!object(body) || !Array.isArray(body.issues))
      throw new Error("invalid_multica_response");
    const rows = body.issues.map(issue);
    const matches = rows.filter((x) =>
      x.description?.startsWith(marker + "\n"),
    );
    if (matches.length > 1) throw new Error("ambiguous_issue_mapping");
    if (matches[0]) {
      const candidate = matches[0];
      if (
        candidate.project_id !== config.multicaProjectId ||
        candidate.assignee_type !== "agent" ||
        candidate.assignee_id !== config.multicaAgentId
      )
        throw new Error("invalid_issue_scope");
      return candidate;
    }
    if (rows.length < 100) return;
  }
  throw new Error("issue_lookup_limit");
}
export async function createIssue(
  config: ApiConfig,
  title: string,
  description: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaIssue> {
  const response = await api(
    config,
    "/api/issues",
    {
      method: "POST",
      body: JSON.stringify({
        title,
        description,
        project_id: config.multicaProjectId,
        assignee_type: "agent",
        assignee_id: config.multicaAgentId,
        status: "todo",
      }),
    },
    fetchImpl,
  );
  if (response.status === 409) {
    const body: unknown = await response.json();
    if (
      object(body) &&
      body.code === "active_duplicate_issue" &&
      object(body.issue)
    ) {
      const existing = issue(body.issue),
        marker = description.split("\n")[0]!;
      if (
        existing.project_id === config.multicaProjectId &&
        existing.assignee_type === "agent" &&
        existing.assignee_id === config.multicaAgentId &&
        existing.description?.startsWith(marker + "\n")
      )
        return existing;
    }
    throw new ApiError(409);
  }
  if (!response.ok) throw new ApiError(response.status);
  return issue(await response.json());
}
export async function findComment(
  config: ApiConfig,
  issueId: string,
  marker: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaComment | undefined> {
  let before = "",
    beforeId = "";
  for (let page = 0; page < 50; page++) {
    const query = new URLSearchParams({ recent: "100" });
    if (before) {
      query.set("before", before);
      query.set("before_id", beforeId);
    }
    const response = await api(
      config,
      `/api/issues/${encodeURIComponent(issueId)}/comments?${query}`,
      {},
      fetchImpl,
    );
    if (!response.ok) throw new ApiError(response.status);
    const body: unknown = await response.json();
    if (
      !Array.isArray(body) ||
      body.some(
        (x) =>
          !object(x) ||
          typeof x.id !== "string" ||
          typeof x.content !== "string",
      )
    )
      throw new Error("invalid_multica_response");
    const match = (body as MulticaComment[]).find((x) =>
      x.content.includes(marker),
    );
    if (match) return match;
    const next = response.headers.get("X-Multica-Next-Before");
    const nextId = response.headers.get("X-Multica-Next-Before-Id");
    if (!next && !nextId) return;
    if (!next || !nextId || (next === before && nextId === beforeId))
      throw new Error("invalid_comment_cursor");
    before = next;
    beforeId = nextId;
  }
  throw new Error("comment_lookup_limit");
}
export async function createComment(
  config: ApiConfig,
  issueId: string,
  content: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaComment> {
  const response = await api(
    config,
    `/api/issues/${encodeURIComponent(issueId)}/comments`,
    { method: "POST", body: JSON.stringify({ content, type: "comment" }) },
    fetchImpl,
  );
  if (!response.ok) throw new ApiError(response.status);
  const body: unknown = await response.json();
  if (
    !object(body) ||
    typeof body.id !== "string" ||
    typeof body.content !== "string"
  )
    throw new Error("invalid_multica_response");
  return body as unknown as MulticaComment;
}
