import { describe, expect, it } from 'vitest';
import { routeSlackThreadEvent, type ThreadRouterConfig } from '../src/thread-router.js';
import { MemoryThreadStore } from '../src/thread-store.js';

function createConfig(fetchImpl: typeof fetch): ThreadRouterConfig {
  return {
    multicaWebhookUrl: 'https://multica.example/webhook',
    multicaApiBaseUrl: 'https://multica.example',
    multicaApiToken: 'mul_test',
    multicaWorkspaceId: 'workspace-1',
    multicaAutopilotId: 'ap-1',
    slackReadToken: 'xoxp-test',
    targetUserIds: new Set(['U123']),
    targetSubteamIds: new Set(['S123']),
    threadMappingTtlSeconds: 3600,
    threadLockTtlSeconds: 30,
    store: new MemoryThreadStore(),
  };
}

function fakeFetch() {
  const comments: string[] = [];
  let webhookCalls = 0;
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url === 'https://multica.example/webhook') {
      webhookCalls += 1;
      return jsonResponse({ status: 'accepted', autopilot_id: 'ap-1', run_id: 'run-1' });
    }
    if (url.endsWith('/api/autopilots/ap-1/runs/run-1')) {
      return jsonResponse({ id: 'run-1', autopilot_id: 'ap-1', issue_id: 'issue-1', status: 'queued' });
    }
    if (url.includes('/api/issues/issue-1/comments') && (init?.method ?? 'GET') === 'GET') {
      return jsonResponse(comments.map((content, index) => ({ id: `comment-${index + 1}`, content })));
    }
    if (url.endsWith('/api/issues/issue-1/comments')) {
      const body = JSON.parse(String(init?.body)) as { content: string };
      comments.push(body.content);
      return jsonResponse({ id: `comment-${comments.length}`, content: body.content }, 201);
    }
    throw new Error(`Unexpected URL ${url}`);
  };
  return { fetchImpl, comments, get webhookCalls() { return webhookCalls; } };
}

describe('routeSlackThreadEvent', () => {
  it('creates one issue and only appends later messages that mention the target', async () => {
    const fake = fakeFetch();
    const config = createConfig(fake.fetchImpl);
    const root = {
      teamId: 'T1', channelId: 'C1', messageTs: '100.000001', threadTs: '100.000001',
      senderUserId: 'U999', text: '<@U123> 请分析', mention: { type: 'user' as const, id: 'U123' },
    };
    const ordinaryFollowup = {
      teamId: 'T1', channelId: 'C1', messageTs: '101.000001', threadTs: '100.000001',
      senderUserId: 'U999', text: '补充一下复现步骤',
    };
    const mentionedFollowup = {
      teamId: 'T1', channelId: 'C1', messageTs: '102.000001', threadTs: '100.000001',
      senderUserId: 'U999', text: '<@U123> 请继续分析', mention: { type: 'user' as const, id: 'U123' },
    };

    await expect(routeSlackThreadEvent(root, config, fake.fetchImpl)).resolves.toMatchObject({ action: 'created', issueId: 'issue-1' });
    await expect(routeSlackThreadEvent(ordinaryFollowup, config, fake.fetchImpl)).resolves.toMatchObject({ action: 'ignored' });
    await expect(routeSlackThreadEvent(mentionedFollowup, config, fake.fetchImpl)).resolves.toMatchObject({ action: 'continued', issueId: 'issue-1' });

    expect(fake.webhookCalls).toBe(1);
    expect(fake.comments).toHaveLength(1);
    expect(fake.comments[0]).toContain('请继续分析');
    expect(fake.comments[0]).toContain('102.000001');
  });

  it('keeps different root threads in different cards', async () => {
    const fake = fakeFetch();
    const config = createConfig(fake.fetchImpl);
    const first = {
      teamId: 'T1', channelId: 'C1', messageTs: '100.000001', threadTs: '100.000001',
      text: '<@U123> one', mention: { type: 'user' as const, id: 'U123' },
    };
    const second = {
      teamId: 'T1', channelId: 'C1', messageTs: '200.000001', threadTs: '200.000001',
      text: '<!subteam^S123> two', mention: { type: 'subteam' as const, id: 'S123' },
    };

    await routeSlackThreadEvent(first, config, fake.fetchImpl);
    await routeSlackThreadEvent(second, config, fake.fetchImpl);
    expect(fake.webhookCalls).toBe(2);
  });

  it('can recover the root mention when a reply arrives before the mapping', async () => {
    const fake = fakeFetch();
    const fetchImpl: typeof fetch = async (input, init) => {
      const url = String(input);
      if (url.startsWith('https://slack.com/api/conversations.replies')) {
        return jsonResponse({ ok: true, messages: [{ ts: '300.000001', text: '<@U123> 根问题', user: 'U999' }] });
      }
      return fake.fetchImpl(input, init);
    };
    const config = createConfig(fetchImpl);
    const reply = {
      teamId: 'T1', channelId: 'C1', messageTs: '301.000001', threadTs: '300.000001',
      senderUserId: 'U888', text: '<@U123> 我补充一个现象', mention: { type: 'user' as const, id: 'U123' },
    };

    await expect(routeSlackThreadEvent(reply, config, fetchImpl)).resolves.toMatchObject({ action: 'created', issueId: 'issue-1' });
    expect(fake.webhookCalls).toBe(1);
    expect(fake.comments[0]).toContain('我补充一个现象');
  });
});

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
}
