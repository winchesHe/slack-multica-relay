import { describe, expect, it } from 'vitest';
import { triggerMultica, type RelayEventPayload } from '../src/multica.js';

const payload: RelayEventPayload = {
  event: 'slack.mention',
  eventPayload: {
    correlationId: 'T1:C1:100.000001',
    channelId: 'C1',
    messageTs: '100.000001',
    threadTs: '100.000001',
    mentionType: 'user',
    mentionId: 'U1',
    text: '<@U1> test',
  },
};

describe('triggerMultica', () => {
  it('retries an admitted webhook with the same idempotency key until the run is available', async () => {
    let calls = 0;
    const fetchImpl: typeof fetch = async () => {
      calls += 1;
      return new Response(JSON.stringify(
        calls === 1
          ? { status: 'accepted', delivery_id: 'delivery-1' }
          : { status: 'duplicate', autopilot_id: 'ap-1', run_id: 'run-1', delivery_id: 'delivery-1' },
      ));
    };

    await expect(triggerMultica('https://multica.example/api/webhooks/autopilots/ap-1', payload, fetchImpl))
      .resolves.toEqual({ status: 'duplicate', autopilotId: 'ap-1', runId: 'run-1', deliveryId: 'delivery-1' });
    expect(calls).toBe(2);
  });
});
