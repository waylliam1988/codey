import { expect, it } from 'vitest';
import { RunLifecycleProjection, isRunSuspension, isTerminalRun, type LifecycleInput } from './run_lifecycle';
import { streamMessage } from './reply_stream';
import type { Session } from './types/domain';

it('does not reinterpret terminal ownership from display-message order', () => {
  const session = {
    messages: [{ type: 'run_state', state: 'approval', runId: 'r' }],
    terminalRuns: [],
  } as unknown as Session;
  expect(isRunSuspension('approval')).toBe(true);
  expect(isRunSuspension('question')).toBe(true);
  expect(isRunSuspension('stopped')).toBe(false);
  expect(isTerminalRun(session, 'r')).toBe(false);
  expect(
    streamMessage(session, { run_id: 'r', stream_id: 's', sequence: 1, state: 'partial', text: 'answer' }),
  ).not.toBeNull();
  session.terminalRuns = ['r'];
  expect(isTerminalRun(session, 'r')).toBe(true);
  expect(
    streamMessage(session, { run_id: 'r', stream_id: 's', sequence: 1, state: 'partial', text: 'late' }),
  ).toBeNull();
});

const input = (
  state: string,
  revision: number | undefined,
  source: LifecycleInput['source'] = 'event',
): LifecycleInput => ({ runId: 'r', sessionId: 's', state, revision, source, active: state === 'running' });

it('fences buffered waiting events using the durable task revision', () => {
  const projection = new RunLifecycleProjection();
  projection.transition({ ...input('approval', 2), sequence: 20 });
  projection.transition(input('running', 3, 'snapshot'));
  expect(projection.transition({ ...input('approval', 2), sequence: 20 })).toBeNull();
  expect(projection.transition(input('approval', undefined))).toBeNull();
  expect(projection.current('r')?.active).toBe(true);
});

it('compares task revisions across event-bus restarts and rejects equal-version conflicts', () => {
  const projection = new RunLifecycleProjection();
  projection.transition({ ...input('approval', 5), sequence: 1000 });
  projection.transition(input('running', 8, 'snapshot'));
  expect(projection.transition({ ...input('approval', 5), sequence: 2000 })).toBeNull();
  expect(projection.transition(input('approval', 8, 'snapshot'))).toBeNull();
  expect(projection.transition(input('approval', undefined))).toBeNull();
  expect(projection.transition({ ...input('approval', 10), sequence: 1 })?.state).toBe('approval');
});

it('latches actual terminal states against newer/older parks and active snapshots', () => {
  for (const terminal of ['done', 'stopped', 'error']) {
    const projection = new RunLifecycleProjection();
    projection.transition(input(terminal, 9));
    for (const source of ['event', 'snapshot'] as const)
      for (const revision of [undefined, 8, 9, 10]) {
        expect(projection.transition(input('approval', revision, source))).toBeNull();
        expect(projection.transition(input('running', revision, source))).toBeNull();
      }
    expect(projection.current('r')?.state).toBe(terminal);
  }
});

it('repairs a legacy terminal cache only at the snapshot restore boundary', () => {
  const projection = new RunLifecycleProjection();
  projection.restoreTerminal('s', 'r');
  expect(projection.transition(input('running', 10))).toBeNull();
  expect(projection.transition(input('running', 10, 'snapshot'))?.active).toBe(true);
  projection.transition(input('done', 12));
  expect(projection.transition(input('running', 13, 'snapshot'))).toBeNull();
});

it('keeps active waiting ownership when a same-version waiting event is replayed after a snapshot', () => {
  const projection = new RunLifecycleProjection();
  projection.transition({ ...input('approval', 7, 'snapshot'), active: true });
  projection.transition({ ...input('approval', 7), sequence: 20 });
  expect(projection.current('r')?.active).toBe(true);
});

it('does not inherit the prior revision bus cursor into a newer snapshot', () => {
  const projection = new RunLifecycleProjection();
  projection.transition({ ...input('approval', 5), sequence: 1000 });
  projection.transition(input('running', 8, 'snapshot'));
  expect(projection.current('r')?.sequence).toBeUndefined();
  expect(projection.transition({ ...input('running', 8), sequence: 1 })?.state).toBe('running');
});

it.each(['approval', 'question', 'waiting'])(
  'distinguishes missing ownership from an explicit snapshot removal for %s',
  (state) => {
    const projection = new RunLifecycleProjection();
    projection.transition({ ...input(state, 7, 'snapshot'), active: true });
    expect(projection.transition({ ...input(state, 7, 'snapshot'), active: undefined })?.active).toBe(true);
    expect(projection.transition({ ...input(state, 7), active: undefined, sequence: 1 })?.active).toBe(true);
    expect(projection.transition({ ...input(state, 7, 'snapshot'), active: false })?.active).toBe(false);
  },
);

it('lets a new revision replace waiting ownership without retaining the previous snapshot barrier', () => {
  const projection = new RunLifecycleProjection();
  projection.transition({ ...input('approval', 7, 'snapshot'), active: true, sequence: 1000 });
  const view = projection.transition({ ...input('approval', 8), sequence: 1 });
  expect(view?.active).toBe(false);
  expect(view?.snapshotBarrier).toBe(false);
});
