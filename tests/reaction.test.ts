import { describe, expect, it, vi } from 'vitest';
import { addSlackReaction, clearOwnSlackReactions } from '../src/reaction.js';

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

describe('取消后的 reaction 清理', () => {
  it('仅移除 token 身份自己的表情', async () => {
    const removed: string[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      if (String(input).endsWith('auth.test')) return Response.json({ ok: true, user_id: 'U1' });
      if (String(input).endsWith('reactions.get')) return Response.json({ ok: true, message: { reactions: [
        { name: 'eyes', users: ['U1', 'U2'] }, { name: 'heart', users: ['U2'] },
      ] } });
      removed.push(JSON.parse(String(init?.body)).name);
      return Response.json({ ok: true });
    };
    await clearOwnSlackReactions('test', 'C1', '1.000001', fetcher);
    expect(removed).toEqual(['eyes']);
  });
  it('已删除消息不阻断取消收尾', async () => {
    const fetcher: typeof fetch = async (input) => Response.json(String(input).endsWith('auth.test')
      ? { ok: true, user_id: 'U1' } : { ok: false, error: 'message_not_found' });
    await expect(clearOwnSlackReactions('test', 'C1', '1.000001', fetcher)).resolves.toBeUndefined();
  });
  it('权限不足保留失败，不把未知数据当作已清理', async () => {
    const fetcher: typeof fetch = async (input) => Response.json(String(input).endsWith('auth.test')
      ? { ok: true, user_id: 'U1' } : { ok: false, error: 'missing_scope' });
    await expect(clearOwnSlackReactions('test', 'C1', '1.000001', fetcher)).rejects.toThrow('reaction_cleanup_failed');
  });
});
