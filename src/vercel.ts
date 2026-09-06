import type { IncomingMessage, ServerResponse } from "node:http";

export type VercelLikeRequest = IncomingMessage;
type RequestWithParsedBody = VercelLikeRequest & { body?: unknown };
export type VercelLikeResponse = ServerResponse & {
  status(code: number): VercelLikeResponse;
  send(body: string): void;
  json(body: unknown): void;
};
export async function serve(
  req: VercelLikeRequest,
  res: VercelLikeResponse,
  handler: (request: Request) => Promise<Response>,
): Promise<void> {
  const headers = new Headers();
  for (const [key, value] of Object.entries(req.headers))
    if (value !== undefined)
      headers.set(key, Array.isArray(value) ? value[0]! : value);
  const rawBody = await readBody(req);
  if (rawBody.byteLength > 256 * 1024) {
    res.status(413).json({ error: "body_too_large" });
    return;
  }
  const method = req.method ?? "GET";
  const result = await handler(
    new Request("https://relay.invalid" + (req.url ?? "/"), {
      method,
      headers,
      ...(!["GET", "HEAD"].includes(method)
        ? { body: rawBody.toString("utf8") }
        : {}),
    }),
  );
  res.status(result.status);
  result.headers.forEach((value, key) => res.setHeader(key, value));
  res.send(await result.text());
}

async function readBody(req: RequestWithParsedBody): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > 256 * 1024) return Buffer.alloc(size);
    chunks.push(bytes);
  }
  if (chunks.length > 0) return Buffer.concat(chunks);

  const fallback = req.body;
  if (Buffer.isBuffer(fallback)) return fallback;
  if (typeof fallback === "string") return Buffer.from(fallback);
  if (fallback !== undefined && fallback !== null) {
    console.warn("vercel_adapter", {
      reason: "raw_body_unavailable",
      bodyType: typeof fallback,
    });
  }
  return Buffer.alloc(0);
}
