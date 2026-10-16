import { beforeEach, describe, expect, it, vi } from 'vitest';

it('shows approval as a static status and restores disclosure when execution resumes', async () => {
  await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  window.Process.append(chat, { type: 'shell_request', runId: 'approval-static' }, render);
  const status = chat.querySelector<HTMLElement>('.process-approval-status');
  expect(status?.textContent).toBe('Waiting for approval');
  expect(status?.hidden).toBe(false);
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.hidden).toBe(true);
  expect(status?.querySelector('button, summary, .spinner')).toBeNull();
  window.Process.append(chat, { type: 'run_state', runId: 'approval-static', state: 'running' }, render);
  expect(status?.hidden).toBe(true);
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.hidden).toBe(false);
});

it('advances a retried request from sending to running in the same process block', async () => {
  await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  window.Process.append(chat, { type: 'run_state', runId: 'first', state: 'failed' }, render);
  window.Process.append(chat, { type: 'request_activity', runId: 'retry', state: 'sending' }, render);
  window.Process.append(chat, { type: 'request_activity', runId: 'retry', state: 'running' }, render);
  expect(chat.querySelectorAll('.process-block')).toHaveLength(1);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Working · Waiting for reply');
});

it('keeps Sending static without a meaningless disclosure arrow', async () => {
  await import('./process');
  const chat = document.createElement('div');
  window.Process.append(chat, { type: 'request_activity', runId: 'sending', state: 'sending' }, vi.fn());
  expect(chat.querySelector('.process-title')?.textContent).toBe('Sending…');
  expect((chat.querySelector('.process-group') as HTMLDetailsElement | null)?.hidden).toBe(false);
  expect(chat.querySelector('.process-block')?.classList.contains('process-sending')).toBe(true);
  expect(chat.querySelector('.process-summary')?.textContent).toBe('Sending…');
});

it('owns one progress indicator from initial request through approval and completion', async () => {
  await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  window.Process.append(chat, { type: 'request_activity', runId: 'activity', state: 'running' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Working · Waiting for reply');
  expect(chat.querySelectorAll('.process-summary .spinner')).toHaveLength(1);
  window.Process.append(chat, { type: 'shell_request', runId: 'activity' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Waiting for approval');
  expect(chat.querySelector('.process-summary .spinner')).toBeNull();
  window.Process.append(chat, { type: 'request_activity', runId: 'activity', state: 'paused' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Waiting for approval');
  window.Process.append(chat, { type: 'run_state', runId: 'continued', state: 'running' }, render);
  expect(chat.querySelectorAll('.process-block')).toHaveLength(1);
  expect(chat.querySelectorAll('.process-summary .spinner')).toHaveLength(1);
  window.Process.append(chat, { type: 'run_state', runId: 'continued', state: 'done' }, render);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Worked');
  expect(chat.querySelector('.process-summary .spinner')).toBeNull();
});

it('switches approval progress to execution without waiting for a continuation run', async () => {
  await import('./process');
  const chat = document.createElement('div');
  window.Process.append(chat, { type: 'shell_request', runId: 'approved' }, vi.fn());
  window.Process.append(
    chat,
    {
      type: 'shell_approval_status',
      runId: 'approved',
      approval: { decision: 'allow', execution: 'running', continuation: 'waiting_for_result' },
    } as Parameters<typeof window.Process.append>[1],
    vi.fn(),
  );
  expect(chat.querySelector('.process-title')?.textContent).toBe('Working · Running command');
  expect(chat.querySelectorAll('.process-summary .spinner')).toHaveLength(1);
});

describe('process thinking DOM', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="chat"></div>';
    window.Render = {
      renderMarkdown: vi.fn((element: HTMLElement, text: string) => {
        element.textContent = text;
      }),
      addMessageCopyButton: vi.fn((element: HTMLElement) => {
        const button = document.createElement('button');
        button.className = 'copy-message';
        element.append(button);
      }),
    } as unknown as typeof window.Render;
  });

  it('renders thinking inside process details without a duplicate per-fragment action row', async () => {
    await import('./process');
    const chat = document.querySelector<HTMLElement>('#chat')!;
    window.Process.append(chat, { type: 'thinking', runId: 'run-1', reasoning: 'Inspect the source.' }, vi.fn());

    const details = chat.querySelector<HTMLDetailsElement>('.thinking');
    expect(details).not.toBeNull();
    expect(details?.querySelector('summary')?.textContent).toBe('Thinking');
    expect(details?.querySelector('.thinking-body')?.textContent).toContain('Inspect the source.');
    expect(details?.querySelector('.copy-message')).toBeNull();
  });
});

it('keeps one quiet process disclosure and one thinking entry for repeated turns', async () => {
  await import('./process');
  window.Render = {
    renderMarkdown: (el: HTMLElement, text: string) => {
      el.textContent = text;
    },
    addMessageCopyButton: vi.fn(),
  } as unknown as typeof window.Render;
  const chat = document.createElement('div');
  for (let n = 1; n <= 12; n++)
    window.Process.append(chat, { type: 'thinking', runId: 'run', n, reasoning: `Thought ${n}` }, vi.fn());
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.open).toBe(false);
  expect(chat.querySelectorAll('.thinking')).toHaveLength(1);
  expect(chat.querySelector('.thinking-body')?.textContent).toContain('Thought 12');
  expect(chat.querySelector('.process-steps')).toBeNull();
});

it('keeps local maintenance warnings in collapsed diagnostics without claiming the task paused', async () => {
  await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  for (let n = 0; n < 3; n++)
    window.Process.append(
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
  await import('./process');
  const chat = document.createElement('div');
  const render = vi.fn();
  window.Process.append(chat, { type: 'info', text: 'Local update paused', runId: 'old-run' }, render);
  expect(render).not.toHaveBeenCalled();
  expect(chat.querySelector('.process-diagnostic')?.textContent).toBe('Local memory update unavailable');
  expect(chat.querySelector<HTMLDetailsElement>('.process-group')?.open).toBe(false);
});
