import { expect, it } from 'vitest';
import { reconcileHistory } from './conversation_history';
import type { Message } from './types/domain';

it('reconciles live entries using task identity while preserving identical messages from different inputs', () => {
  const rows = reconcileHistory(
    [
      { type: 'user', id: 'browser', text: 'same', attempts: [{ runId: 'task-2', state: 'done' }] },
      { type: 'asst', runId: 'task-2', text: 'answer', finalAnswer: true },
    ],
    [
      { id: 'entry-1', type: 'user', text: 'same', runId: 'task-1' },
      { id: 'entry-2', type: 'user', text: 'same', runId: 'task-2' },
      { id: 'entry-3', type: 'asst', text: 'answer', runId: 'task-2' },
    ],
  );
  expect(rows.map((row) => row.id)).toEqual(['entry-1', 'entry-2', 'entry-3']);
});

it('drops replaced branch entries and retains a transient partial reply', () => {
  const partial = { type: 'asst', text: 'streaming', finalAnswer: false, runId: 'running' };
  expect(
    reconcileHistory(
      [{ id: 'old', entryId: 'old', type: 'user' }, partial],
      [{ id: 'new', type: 'user', text: 'branched' }],
    ),
  ).toEqual([{ id: 'new', entryId: 'new', type: 'user', text: 'branched' }, partial]);
});

it('does not duplicate a streamed answer when its durable entry arrives first', () => {
  const streamed: Message = {
    type: 'asst',
    runId: 'run-1',
    streamId: 'stream-1',
    streamState: 'partial',
    finalAnswer: false,
    text: 'partial',
  };
  expect(
    reconcileHistory(
      [streamed],
      [
        { id: 'entry-user', type: 'user', runId: 'run-1', text: 'request' },
        { id: 'entry-answer', type: 'asst', runId: 'run-1', text: 'partial' },
      ],
    ),
  ).toEqual([{ id: 'entry-user', type: 'user', runId: 'run-1', text: 'request', entryId: 'entry-user' }, streamed]);
});
