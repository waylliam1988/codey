import { Render } from './render';
import { beforeEach, describe, expect, it, vi } from 'vitest';

it('clears stale approval after execution settled and continuation started', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  Process.append(chat, { type: 'shell_request', runId: 'approved' }, vi.fn());
  Process.append(
    chat,
    {
      type: 'shell_approval_status',
      runId: 'approved',
      approval: {
        type: 'shell_approval_status',
        id: 'approval',
        session_id: 'chat',
        revision: 1,
        decision: 'allow',
        execution: 'settled',
        continuation: 'started',
      },
    },
    vi.fn(),
  );
  expect(chat.querySelector<HTMLElement>('.process-approval-status')?.hidden).toBe(true);
  expect(chat.querySelector('.process-title')?.textContent).not.toContain('Waiting for approval');
  Process.append(chat, { type: 'run_state', runId: 'approved', state: 'done' }, vi.fn());
  expect(chat.querySelector('.process-title')?.textContent).toBe('Worked');
});

it('inserts a late process record before its already mounted answer', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  chat.innerHTML = '<div class="msg asst" data-run-id="late">Answer</div>';
  Process.append(chat, { type: 'run_state', runId: 'late', state: 'done' }, vi.fn());
  expect(chat.firstElementChild?.classList.contains('process-block')).toBe(true);
  expect(chat.lastElementChild?.textContent).toBe('Answer');
});

it('settles reconstructed progress from the authoritative final answer', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  Process.append(chat, { type: 'shell_request', runId: 'restored' }, vi.fn());
  Process.append(
    chat,
    {
      type: 'asst',
      runId: 'restored',
      finalAnswer: true,
      contentRole: 'final',
      text: 'Finished',
    },
    vi.fn(),
  );
  expect(chat.querySelector('.process-title')?.textContent).toBe('Worked');
  expect(chat.querySelector('.spinner')).toBeNull();
});

it('keeps a late older process out of the newer turn', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  chat.innerHTML =
    '<div class="msg user">Old question</div><div class="msg asst" data-run-id="old">Old answer</div><div class="msg user">New question</div>';
  Process.append(chat, { type: 'run_state', runId: 'new', state: 'running' }, vi.fn());
  Process.append(chat, { type: 'run_state', runId: 'old', state: 'done' }, vi.fn());
  expect(chat.querySelectorAll('.process-block')).toHaveLength(2);
  expect(chat.children[1].getAttribute('data-run-id')).toBe('old');
  expect(chat.querySelector('[data-run-id="new"] .process-title')?.textContent).toContain('Working');
});

it('shows approval as a static status and restores disclosure when execution resumes', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  Process.append(chat, { type: 'shell_request', runId: 'approval-static' }, render);
  const status = chat.querySelector<HTMLElement>('.process-approval-status');
  expect(status?.textContent).toBe('Waiting for approval');
  expect(status?.hidden).toBe(false);
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.hidden).toBe(true);
  expect(status?.querySelector('button, summary, .spinner')).toBeNull();
  Process.append(chat, { type: 'run_state', runId: 'approval-static', state: 'running' }, render);
  expect(status?.hidden).toBe(true);
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.hidden).toBe(false);
});

it('advances a retried request from sending to running in the same process block', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  Process.append(chat, { type: 'run_state', runId: 'first', state: 'failed' }, render);
  Process.append(chat, { type: 'request_activity', runId: 'retry', state: 'sending' }, render);
  Process.append(chat, { type: 'request_activity', runId: 'retry', state: 'running' }, render);
  expect(chat.querySelectorAll('.process-block')).toHaveLength(1);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Working · Waiting for reply');
});

it('keeps Sending static without a meaningless disclosure arrow', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  Process.append(chat, { type: 'request_activity', runId: 'sending', state: 'sending' }, vi.fn());
  expect(chat.querySelector('.process-title')?.textContent).toBe('Sending…');
  expect((chat.querySelector('.process-group') as HTMLDetailsElement | null)?.hidden).toBe(false);
  expect(chat.querySelector('.process-block')?.classList.contains('process-sending')).toBe(true);
  expect(chat.querySelector('.process-summary')?.textContent).toBe('Sending…');
});

it('keeps the current retry active when rendering an earlier attempt checkpoint', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  Process.append(
    chat,
    { type: 'request_activity', runId: 'retry', state: 'sending', supersededRunIds: ['first'] },
    render,
  );
  Process.append(chat, { type: 'run_state', runId: 'first', state: 'error' }, render);
  Process.append(
    chat,
    { type: 'request_activity', runId: 'retry', state: 'running', supersededRunIds: ['first'] },
    render,
  );
  expect(chat.querySelectorAll('.process-block')).toHaveLength(1);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Working · Waiting for reply');
  Process.append(chat, { type: 'run_state', runId: 'retry', state: 'done' }, render);
  Process.append(chat, { type: 'run_state', runId: 'first', state: 'error' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Worked');
});

it('owns one progress indicator from initial request through approval and completion', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  Process.append(chat, { type: 'request_activity', runId: 'activity', state: 'running' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Working · Waiting for reply');
  expect(chat.querySelectorAll('.process-summary .spinner')).toHaveLength(1);
  Process.append(chat, { type: 'shell_request', runId: 'activity' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Waiting for approval');
  expect(chat.querySelector('.process-summary .spinner')).toBeNull();
  Process.append(chat, { type: 'request_activity', runId: 'activity', state: 'paused' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Waiting for approval');
  Process.append(chat, { type: 'run_state', runId: 'continued', state: 'running' }, render);
  expect(chat.querySelectorAll('.process-block')).toHaveLength(1);
  expect(chat.querySelectorAll('.process-summary .spinner')).toHaveLength(1);
  Process.append(chat, { type: 'run_state', runId: 'continued', state: 'done' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Worked');
  expect(chat.querySelector('.process-summary .spinner')).toBeNull();
});

it('switches approval progress to execution without waiting for a continuation run', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  Process.append(chat, { type: 'shell_request', runId: 'approved' }, vi.fn());
  Process.append(
    chat,
    {
      type: 'shell_approval_status',
      runId: 'approved',
      approval: {
        type: 'shell_approval_status',
        id: 'approval',
        session_id: 'chat',
        revision: 1,
        decision: 'allow',
        execution: 'running',
        continuation: 'waiting_for_result',
      },
    } as Parameters<typeof Process.append>[1],
    vi.fn(),
  );
  expect(chat.querySelector('.process-title')?.textContent).toBe('Working · Running command');
  expect(chat.querySelectorAll('.process-summary .spinner')).toHaveLength(1);
});

describe('process thinking DOM', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="chat"></div>';
  });

  it('renders thinking inside process details without a duplicate per-fragment action row', async () => {
    const { Process } = await import('./process');
    const chat = document.querySelector<HTMLElement>('#chat')!;
    Process.append(chat, { type: 'thinking', runId: 'run-1', reasoning: 'Inspect the source.' }, vi.fn());

    const details = chat.querySelector<HTMLDetailsElement>('.thinking');
    expect(details).not.toBeNull();
    expect(details?.querySelector('summary')?.textContent).toBe('Thinking');
    expect(details?.querySelector('.thinking-body')?.textContent).toContain('Inspect the source.');
    expect(details?.querySelector('.copy-message')).toBeNull();
  });
});

it('keeps one quiet process disclosure and one thinking entry for repeated turns', async () => {
  const { Process } = await import('./process');

  const chat = document.createElement('div');
  for (let n = 1; n <= 12; n++)
    Process.append(chat, { type: 'thinking', runId: 'run', n, reasoning: `Thought ${n}` }, vi.fn());
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.open).toBe(false);
  expect(chat.querySelectorAll('.thinking')).toHaveLength(1);
  expect(chat.querySelector('.thinking-body')?.textContent).toContain('Thought 12');
  expect(chat.querySelector('.process-steps')).toBeNull();
});

it('keeps local maintenance warnings in collapsed diagnostics without claiming the task paused', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  for (let n = 0; n < 3; n++)
    Process.append(
      chat,
      { type: 'local_update_warning', runId: 'run', text: 'Local memory update unavailable' },
      render,
    );
  expect(render).not.toHaveBeenCalled();
  expect(chat.querySelectorAll('.process-block')).toHaveLength(1);
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.open).toBe(false);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Activity');
  expect(chat.querySelectorAll('.process-diagnostic')).toHaveLength(1);
  expect(chat.querySelector('.process-diagnostic')?.textContent).toContain('Local memory update unavailable');
});

it('folds saved local-update info rows into the same diagnostics without losing the record', async () => {
  const { Process } = await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  Process.append(chat, { type: 'info', text: 'Local update paused', runId: 'old-run' }, render);
  expect(render).not.toHaveBeenCalled();
  expect(chat.querySelector('.process-diagnostic')?.textContent).toBe('Local memory update unavailable');
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.open).toBe(false);
});
