export default function onRequest(): Response {
  return Response.json({ ok: true, service: "slack-multica-relay" });
}
