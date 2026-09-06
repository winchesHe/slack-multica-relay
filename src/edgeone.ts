// EdgeOne Cloud Functions overrides Request.body with parsed JSON. The
// standard body methods retain the original bytes required by webhook HMACs.
export type EdgeOneRequest = Omit<Request, "body"> & { body: unknown };

export async function serveEdgeOne(
  request: EdgeOneRequest,
  handler: (request: Request) => Promise<Response>,
): Promise<Response> {
  const method = request.method;
  const hasBody = !["GET", "HEAD"].includes(method);
  if (Number(request.headers.get("content-length")) > 256 * 1024)
    return Response.json({ error: "body_too_large" }, { status: 413 });
  let body: ArrayBuffer | undefined;
  try {
    if (hasBody) body = await request.arrayBuffer();
  } catch {
    return Response.json({ error: "raw_body_unavailable" }, { status: 400 });
  }
  if (body && body.byteLength > 256 * 1024)
    return Response.json({ error: "body_too_large" }, { status: 413 });
  return handler(new Request(request.url, {
    method,
    headers: request.headers,
    ...(body ? { body } : {}),
  }));
}
