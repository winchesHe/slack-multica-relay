export interface MulticaRunDetails {
  id: string;
  autopilot_id?: string;
  issue_id?: string | null;
  status?: string;
}

export interface MulticaComment {
  id?: string;
  content?: string;
}

export async function getAutopilotRun(
  baseUrl: string,
  token: string,
  workspaceId: string,
  autopilotId: string,
  runId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaRunDetails> {
  const response = await apiFetch(
    fetchImpl,
    `${trimBaseUrl(baseUrl)}/api/autopilots/${encodeURIComponent(autopilotId)}/runs/${encodeURIComponent(runId)}`,
    token,
    workspaceId,
  );
  return response.json() as Promise<MulticaRunDetails>;
}

export async function waitForIssueId(
  baseUrl: string,
  token: string,
  workspaceId: string,
  autopilotId: string,
  runId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<string | undefined> {
  // create_issue 的 issue 在 webhook admission 之后由 worker 创建，短暂为空是正常状态。
  const delays = [0, 150, 300, 600];
  for (const delay of delays) {
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
    const run = await getAutopilotRun(baseUrl, token, workspaceId, autopilotId, runId, fetchImpl);
    if (run.issue_id) return run.issue_id;
  }
  return undefined;
}

export async function listIssueComments(
  baseUrl: string,
  token: string,
  workspaceId: string,
  issueId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaComment[]> {
  const response = await apiFetch(
    fetchImpl,
    `${trimBaseUrl(baseUrl)}/api/issues/${encodeURIComponent(issueId)}/comments?recent=200`,
    token,
    workspaceId,
  );
  const body = await response.json() as unknown;
  if (Array.isArray(body)) return body as MulticaComment[];
  if (body && typeof body === 'object' && Array.isArray((body as { comments?: unknown }).comments)) {
    return (body as { comments: MulticaComment[] }).comments;
  }
  return [];
}

export async function createIssueComment(
  baseUrl: string,
  token: string,
  workspaceId: string,
  issueId: string,
  content: string,
  idempotencyKey: string,
  fetchImpl: typeof fetch = fetch,
): Promise<MulticaComment> {
  const response = await apiFetch(
    fetchImpl,
    `${trimBaseUrl(baseUrl)}/api/issues/${encodeURIComponent(issueId)}/comments`,
    token,
    workspaceId,
    {
      method: 'POST',
      headers: {
        'idempotency-key': idempotencyKey,
      },
      body: JSON.stringify({ content, type: 'comment' }),
    },
  );
  return response.json() as Promise<MulticaComment>;
}

async function apiFetch(
  fetchImpl: typeof fetch,
  url: string,
  token: string,
  workspaceId: string,
  init: RequestInit = {},
): Promise<Response> {
  const response = await fetchImpl(url, {
    ...init,
    headers: {
      authorization: `Bearer ${token}`,
      'content-type': 'application/json',
      'x-workspace-id': workspaceId,
      ...(init.headers ?? {}),
    },
    signal: AbortSignal.timeout(1_500),
  });
  if (!response.ok) throw new Error(`Multica API failed with HTTP ${response.status}`);
  return response;
}

function trimBaseUrl(value: string): string {
  return value.replace(/\/+$/u, '');
}
