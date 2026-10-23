import { afterEach, expect, it, vi } from 'vitest';
import { ConversationHistory, reconcileHistory } from './conversation_history';
import type { Message, Session } from './types/domain';

afterEach(() => vi.unstubAllGlobals());

function page(items: object[], inactive: string[], revision = 5) {
  return new Response(
    JSON.stringify({
      schema_version: 1,
      session_id: 'chat',
      epoch: 'db',
      snapshot_revision: revision,
      items,
      has_more: false,
      inactive_run_ids: inactive,
    }),
  );
}

it('replaces removed Work and replies while retaining new live output and the prefix', async () => {
  const session = {
    id: 'chat',
    messages: [
      { type: 'user', entryId: 'old', runId: 'old-run' },
      { type: 'tool', runId: 'old-run' },
      { type: 'pause', runId: 'old-run' },
      { type: 'asst', runId: 'new-run', streamId: 'new-stream', finalAnswer: false, text: 'new live answer' },
    ],
  } as Session;
  const history = new ConversationHistory(() => {});
  history.retireRuns(session, ['old-run']);
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      page(
        [
          { id: 'prefix', type: 'user', runId: 'prefix-run', text: 'retained' },
          { id: 'new', type: 'user', runId: 'new-run', text: 'replacement' },
        ],
        ['old-run'],
      ),
    ),
  );
  await history.refresh(session);
  expect(session.messages.map((message) => message.text)).toEqual(['retained', 'replacement', 'new live answer']);
  expect(history.revision('chat')).toBe(5);
  expect(history.acceptsRun('chat', 'old-run')).toBe(false);
  expect(history.acceptsRun('chat', 'new-run')).toBe(true);
});

it('ignores a history response started before the edit boundary', async () => {
  let resolve!: (response: Response) => void;
  const fetch = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    )
    .mockResolvedValueOnce(page([{ id: 'new', type: 'user', runId: 'new-run' }], ['old-run'], 10));
  vi.stubGlobal('fetch', fetch);
  const session = { id: 'chat', messages: [] } as unknown as Session;
  const history = new ConversationHistory(() => {});
  const stale = history.refresh(session);
  history.retireRuns(session, ['old-run']);
  await history.refresh(session);
  resolve(page([{ id: 'old', type: 'user', runId: 'old-run' }], [], 1));
  await stale;
  expect(session.messages.map((message) => message.entryId)).toEqual(['new']);
  expect(history.revision('chat')).toBe(10);
  expect(history.acceptsRun('chat', 'old-run')).toBe(false);
});

it('rebuilds event visibility from canonical history when returning to a saved branch', async () => {
  const session = { id: 'chat', messages: [] } as unknown as Session;
  const history = new ConversationHistory(() => {});
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(page([{ id: 'new', type: 'user', runId: 'new-run' }], ['old-run']))
    .mockResolvedValueOnce(page([{ id: 'old', type: 'user', runId: 'old-run' }], ['new-run'], 6));
  vi.stubGlobal('fetch', fetch);
  await history.refresh(session);
  expect(history.acceptsRun('chat', 'old-run')).toBe(false);
  await history.refresh(session);
  expect(history.acceptsRun('chat', 'old-run')).toBe(true);
  expect(history.acceptsRun('chat', 'new-run')).toBe(false);
});

it('keeps approval and execution records above their final answer and before the next turn', () => {
  const rows = reconcileHistory(
    [
      { type: 'shell_result', id: 'approval', runId: 'one' },
      { type: 'shell_approval_status', id: 'approval', runId: 'one' },
      { type: 'run_state', runId: 'one', state: 'done' },
    ],
    [
      { id: 'u1', type: 'user', runId: 'one' },
      { id: 'a1', type: 'asst', runId: 'one', text: 'first answer' },
      { id: 'u2', type: 'user', runId: 'two' },
      { id: 'a2', type: 'asst', runId: 'two', text: 'second answer' },
    ],
  );
  expect(rows.map((row) => row.type)).toEqual([
    'user',
    'shell_result',
    'shell_approval_status',
    'run_state',
    'asst',
    'user',
    'asst',
  ]);
});

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
