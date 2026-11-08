import { afterEach, expect, it, vi } from 'vitest';
import { type HistoryIdentity, type RunValidation } from './history_repository';
import { RunVisibility } from './run_visibility';

const identity: HistoryIdentity = { epoch: 'database', pathGeneration: 1, revision: 10 };
const result = (runs: string[], visible: boolean, revision = 10): RunValidation => ({
  ...identity,
  revision,
  runs: runs.map((run_id) => ({ run_id, visible })),
});

afterEach(() => vi.useRealTimers());

it('holds previously unseen events and discards an excluded task after bounded validation', async () => {
  const accept = vi.fn();
  const retired = vi.fn();
  const validate = vi.fn(async (runs: string[]) => result(runs, false));
  const visibility = new RunVisibility(validate, vi.fn(), retired);
  visibility.update(identity, []);
  visibility.admit('never-cached-old-task', accept);
  expect(accept).not.toHaveBeenCalled();
  await vi.waitFor(() => expect(retired).toHaveBeenCalledWith(['never-cached-old-task']));
  expect(accept).not.toHaveBeenCalled();
  expect(visibility.accepts('never-cached-old-task')).toBe(false);
  visibility.reset();
});

it('preserves event order while batching an unknown visible task', async () => {
  const accepted: number[] = [];
  const validate = vi.fn(async (runs: string[]) => result(runs, true));
  const visibility = new RunVisibility(validate, vi.fn(), vi.fn());
  visibility.update(identity, []);
  visibility.admit('run', () => accepted.push(1));
  visibility.admit('run', () => accepted.push(2));
  await vi.waitFor(() => expect(accepted).toEqual([1, 2]));
  expect(validate).toHaveBeenCalledOnce();
  visibility.reset();
});

it('checks cached task identities in batches of at most 200', async () => {
  const validate = vi.fn(async (runs: string[]) => result(runs, true));
  const visibility = new RunVisibility(validate, vi.fn(), vi.fn());
  visibility.update(identity, []);
  visibility.check(Array.from({ length: 451 }, (_, index) => `task-${index}`));
  await vi.waitFor(() => expect(validate).toHaveBeenCalledTimes(3));
  expect(validate.mock.calls.map(([runs]) => runs.length)).toEqual([200, 200, 51]);
  expect(visibility.accepts('task-450')).toBe(true);
  visibility.reset();
});

it('discards an old validation response and its events after a path rewrite', async () => {
  let release!: (validation: RunValidation) => void;
  const validate = vi.fn(
    () =>
      new Promise<RunValidation>((resolve) => {
        release = resolve;
      }),
  );
  const visibility = new RunVisibility(validate, vi.fn(), vi.fn());
  const accept = vi.fn();
  visibility.update(identity, []);
  const previousVersion = visibility.version();
  visibility.admit('old', accept);
  await vi.waitFor(() => expect(validate).toHaveBeenCalledOnce());
  visibility.update({ ...identity, pathGeneration: 2 }, ['new']);
  release(result(['old'], true));
  await Promise.resolve();
  expect(accept).not.toHaveBeenCalled();
  expect(visibility.accepts('old')).toBe(false);
  expect(visibility.accepts('new')).toBe(true);
  expect(visibility.version()).not.toBe(previousVersion);
  visibility.reset();
});

it('rechecks a result overtaken by an append without retiring the newly visible run', async () => {
  let release!: (validation: RunValidation) => void;
  const validate = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<RunValidation>((resolve) => {
          release = resolve;
        }),
    )
    .mockImplementationOnce(async (runs: string[]) => result(runs, true, 11));
  const retired = vi.fn();
  const accept = vi.fn();
  const visibility = new RunVisibility(validate, vi.fn(), retired);
  visibility.update(identity, []);
  visibility.admit('new-run', accept);
  await vi.waitFor(() => expect(validate).toHaveBeenCalledOnce());
  visibility.observe(1, 11);
  release(result(['new-run'], false));
  await vi.waitFor(() => expect(accept).toHaveBeenCalledOnce());
  expect(retired).not.toHaveBeenCalledWith(['new-run']);
  visibility.reset();
});

it('expires unknown event buffers and resynchronizes instead of growing indefinitely', () => {
  vi.useFakeTimers();
  const resync = vi.fn();
  const accept = vi.fn();
  const visibility = new RunVisibility(async (runs) => result(runs, true), resync, vi.fn());
  visibility.update(identity, []);
  visibility.admit('unknown', accept);
  vi.advanceTimersByTime(5000);
  expect(resync).toHaveBeenCalledOnce();
  expect(accept).not.toHaveBeenCalled();
  visibility.reset();
});

it('bounds queued events even when validation is stalled', () => {
  const resync = vi.fn();
  const accept = vi.fn();
  const visibility = new RunVisibility(async (runs) => result(runs, true), resync, vi.fn());
  visibility.update(identity, []);
  for (let index = 0; index < 513; index++) visibility.admit('unknown', accept);
  expect(resync).toHaveBeenCalledOnce();
  expect(accept).not.toHaveBeenCalled();
  visibility.reset();
});
