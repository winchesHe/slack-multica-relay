import { consumeQueue } from "../../../src/http.js";
import { serveEdgeOne, type EdgeOneRequest } from "../../../src/edgeone.js";
export default function onRequest(context: {
  request: EdgeOneRequest;
  env: NodeJS.ProcessEnv;
}): Promise<Response> {
  return serveEdgeOne(context.request, (request) => consumeQueue(request, context.env));
}
