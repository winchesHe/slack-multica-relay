import { describe, expect, it, vi } from 'vitest';
import { addSlackReaction } from '../src/reaction.js';

describe('addSlackReaction', () => {
  it('calls Slack reactions.add with the message identity', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    await addSlackReaction('xoxp-test', 'C123', '1700000000.000100', 'lark_onesecond', fetchMock);

    expect(fetchMock).toHaveBeenCalledWith(
      'https://slack.com/api/reactions.add',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ authorization: 'Bearer xoxp-test' }),
        body: JSON.stringify({ channel: 'C123', timestamp: '1700000000.000100', name: 'lark_onesecond' }),
      }),
    );
  });

  it('treats an existing reaction as success for Slack retries', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ ok: false, error: 'already_reacted' }), { status: 200 }));

    await expect(addSlackReaction('xoxp-test', 'C123', '1700000000.000100', 'lark_onesecond', fetchMock)).resolves.toBeUndefined();
  });
});
