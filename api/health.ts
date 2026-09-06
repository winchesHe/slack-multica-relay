import type { VercelLikeRequest, VercelLikeResponse } from '../src/vercel.js';

export default function handler(_request: VercelLikeRequest, response: VercelLikeResponse): void {
  response.status(200).json({ ok: true, service: 'slack-multica-relay' });
}
