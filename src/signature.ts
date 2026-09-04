import { createHmac, timingSafeEqual } from 'node:crypto';

export function verifySlackSignature(
  rawBody: string,
  timestamp: string | undefined,
  signature: string | undefined,
  signingSecret: string,
  nowSeconds = Math.floor(Date.now() / 1000),
): boolean {
  if (!timestamp || !signature || !/^v0=[0-9a-f]{64}$/u.test(signature)) return false;
  const timestampSeconds = Number(timestamp);
  if (!Number.isInteger(timestampSeconds) || Math.abs(nowSeconds - timestampSeconds) > 300) return false;
  const base = `v0:${timestamp}:${rawBody}`;
  const expected = `v0=${createHmac('sha256', signingSecret).update(base).digest('hex')}`;
  return timingSafeEqual(Buffer.from(expected), Buffer.from(signature));
}
