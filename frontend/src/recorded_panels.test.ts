import { afterEach, expect, it, vi } from 'vitest';
import { RecordedPanels } from './recorded_panels';

afterEach(() => vi.unstubAllGlobals());
it('does not show a late recorded review in another conversation and only reads saved facts', async () => {
  let session = 'one';
  let resolve: (value: unknown) => void = () => {};
  const fetcher = vi.fn().mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  vi.stubGlobal('fetch', fetcher);
  const panel = new RecordedPanels({
    sessionId: () => session,
    messages: () => [],
    openDocument: vi.fn(),
    copy: vi.fn(),
  });
  const loading = panel.showReview({ runId: 'run_one' });
  session = 'two';
  resolve({
    ok: true,
    text: async () => JSON.stringify({ ok: true, available: true, review: { summary: 'private review' } }),
  });
  await loading;
  expect(panel.review.textContent).not.toContain('private review');
  expect(fetcher.mock.calls[0][0]).toContain('session_id=one');
  expect(fetcher.mock.calls[0][1].method).toBeUndefined();
});

it('shows a recorded finding and opens only its new-file line, not an old-diff line', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      text: async () =>
        JSON.stringify({
          ok: true,
          available: true,
          review: {
            status: 'complete',
            verdict: 'changes_requested',
            summary: 'An issue',
            findings: [{ path: 'a.py', old_line: 5, issue: '<literal>' }],
          },
        }),
    }),
  );
  const open = vi.fn();
  const panel = new RecordedPanels({ sessionId: () => 'one', messages: () => [], openDocument: open, copy: vi.fn() });
  await panel.showReview({ runId: 'run_one' });
  const path = panel.review.querySelector<HTMLButtonElement>('.review-finding button')!;
  expect(path.textContent).toBe('a.py');
  path.click();
  expect(open).toHaveBeenCalledWith('one', { kind: 'workspace', path: 'a.py' });
  expect(panel.review.querySelector('literal')).toBeNull();
});

it('uses the latest command fact when a settled output replaces an earlier observation', () => {
  const panel = new RecordedPanels({
    sessionId: () => 'one',
    messages: () => [
      {
        type: 'shell_result',
        runId: 'run_one',
        executionId: 'execution_one',
        command: 'check',
        shellStatus: 'running',
        approved: true,
      },
      {
        type: 'shell_result',
        runId: 'run_one',
        executionId: 'execution_one',
        command: 'check',
        shellStatus: 'exit',
        exitCode: 1,
        approved: true,
        output: 'failed check',
      },
    ],
    openDocument: vi.fn(),
    copy: vi.fn(),
  });
  panel.showOutputs();
  expect(panel.outputs.querySelectorAll('.command-card')).toHaveLength(1);
  expect(panel.outputs.querySelector('.command-card')?.getAttribute('data-status')).toBe('failed');
  expect(panel.outputs.textContent).toContain('failed check');
});
