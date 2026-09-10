import { acceptSlack, consumeQueue, json } from './http.js';

export default {
  async fetch(request: Request, env: Record<string, string | undefined>): Promise<Response> {
    const path = new URL(request.url).pathname;
    if (path === '/api/slack/events') return acceptSlack(request, env);
    if (path === '/api/queue/consume') return consumeQueue(request, env);
    if (path === '/api/health') {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
      return json({ ok: true, service: 'slack-multica-relay' });
    }
    return json({ error: 'not_found' }, 404);
  },
};
