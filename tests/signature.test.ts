import { createHmac } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { verifySlackSignature } from '../src/signature.js';

describe('verifySlackSignature', () => {
  it('verifies a fresh Slack signature', () => {
    const body = '{"type":"event_callback"}';
    const timestamp = '1700000000';
    const signature = `v0=${createHmac('sha256', 'secret').update(`v0:${timestamp}:${body}`).digest('hex')}`;
    expect(verifySlackSignature(body, timestamp, signature, 'secret', 1700000000)).toBe(true);
  });

  it('rejects stale signatures', () => {
    expect(verifySlackSignature('body', '1700000000', 'v0=' + '0'.repeat(64), 'secret', 1700000401)).toBe(false);
  });
});
