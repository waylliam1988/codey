import { expect, it } from 'vitest';
import { streamMessage, finalStreamMessage } from './reply_stream';
import type { Session } from './types/domain';

it('replaces a bounded display prefix with the complete final answer', () => {
  const text = 'a'.repeat(64000) + 'the end';
  const session = {
    messages: [{ type: 'asst', runId: 'r', streamId: 's', streamState: 'complete', text: text.slice(0, 64000) }],
  } as unknown as Session;
  expect(finalStreamMessage(session, 'r', text, 'final')?.text).toBe(text);
});

it('deduplicates snapshots and settles the same message at the terminal answer', () => {
  const session = { messages: [] } as unknown as Session;
  const event = { stream_id: 's', run_id: 'r', sequence: 1, state: 'partial', text: '第一段' };
  const first = streamMessage(session, event)!;
  session.messages.push(first);
  expect(streamMessage(session, event)).toBeNull();
  const second = streamMessage(session, { ...event, sequence: 2, state: 'complete', text: '第一段\n\n第二段' })!;
  session.messages[0] = second;
  const final = finalStreamMessage(session, 'r', '第一段\n\n第二段', 'terminal:r:answer')!;
  expect(final.streamId).toBe('s');
  expect(final.finalAnswer).toBe(true);
  expect(final.eventKey).toBe('terminal:r:answer');
  session.messages[0] = final;
  expect(streamMessage(session, { ...event, sequence: 3, text: 'late' })).toBeNull();
});
