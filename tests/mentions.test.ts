import { describe, expect, it } from 'vitest';
import { findTargetMention } from '../src/mentions.js';

describe('findTargetMention', () => {
  it('matches a target user mention', () => {
    expect(findTargetMention('请看 <@U123|Alice>', new Set(['U123']), new Set())).toEqual({ type: 'user', id: 'U123' });
  });

  it('matches a target subteam mention', () => {
    expect(findTargetMention('<!subteam^S123|team> 处理一下', new Set(), new Set(['S123']))).toEqual({ type: 'subteam', id: 'S123' });
  });

  it('ignores non-target mentions', () => {
    expect(findTargetMention('<@U999>', new Set(['U123']), new Set())).toBeUndefined();
  });
});
