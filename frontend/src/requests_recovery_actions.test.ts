import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from './types/domain';

it('rejects a replayed start after history restoration removed the browser attempt', async () => {
  await import('./requests');
  const restored = {
    id: 'restored',
    messages: [{ type: 'user', runId: 'finished' }],
    terminalRuns: ['finished'],
  } as Session;
  window.Requests.init({ getSessions: () => [restored] } as Parameters<typeof window.Requests.init>[0]);
  expect(window.Requests.acceptsEvent({ session_id: 'restored', run_id: 'finished', type: 'task_start' })).toBe(false);
  expect(window.Requests.acceptsEvent({ session_id: 'restored', run_id: 'finished', type: 'task_done' })).toBe(true);
});

it('renders pending request progress only in the process summary, keeping failure recovery separate', async () => {
  await import('./process');
  await import('./requests');
  const chat = document.createElement('div');
  const session = {
    id: 'single',
    messages: [{ type: 'user', id: 'question', text: 'test', attempts: [{ runId: 'run', state: 'running' }] }],
  } as Session;
  window.Requests.init({
    $: () => chat,
    getSessions: () => [session],
    getActiveId: () => session.id,
    statusRow: () => {
      const wrapper = document.createElement('div');
      wrapper.innerHTML = '<div><span></span></div>';
      return wrapper;
    },
  } as unknown as Parameters<typeof window.Requests.init>[0]);
  window.Requests.appendStatus(chat, { type: 'request_status', sessionId: 'single', requestId: 'question' });
  expect(chat.querySelector<HTMLElement>('.request-status')?.hidden).toBe(true);
  expect(chat.querySelector('.process-title')?.textContent).toBe('Working · Waiting for reply');
  expect(chat.querySelectorAll('.spinner')).toHaveLength(1);
});

describe('request recovery actions', () => {
  beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = '<div id="task"></div><div id="chat"></div>';
  });

  it('preserves failure context so model and busy recovery actions are offered', async () => {
    const session: Session = {
      id: 'session-1',
      title: 'Chat',
      draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
      messages: [
        {
          type: 'user',
          id: 'request-1',
          text: 'hello',
          attempts: [{ runId: 'run-1', state: 'failed', error: 'No model', sendFailure: 'model', runOwner: 'other' }],
        },
      ],
    };
    const recoveryActions = vi.fn((attempt: Record<string, unknown>) =>
      attempt.sendFailure === 'model'
        ? [{ label: 'Choose model', onclick: vi.fn() }]
        : [{ label: 'Open', onclick: vi.fn() }],
    );
    window.Composer = { recoveryActions, isSending: () => false } as unknown as typeof window.Composer;
    window.Models = { allows: () => true } as unknown as typeof window.Models;
    window.ProviderUI = { runSelection: () => ({ model: '' }) } as unknown as typeof window.ProviderUI;
    window.Requests = {} as typeof window.Requests;
    const deps = {
      $: () => document.querySelector<HTMLElement>('#task')!,
      getSessions: () => [session],
      getActiveId: () => session.id,
      getRunningSessionId: () => null,
      persistActive: vi.fn(),
      persistActiveNow: vi.fn(),
      pushMsgToSession: vi.fn(),
      addToSession: vi.fn(),
      appendMessageNode: vi.fn(),
      renderChat: vi.fn(),
      scrollChat: vi.fn(),
      statusRow: vi.fn((_p, _t, options) => {
        const row = document.createElement('div');
        for (const action of options.actions ?? []) {
          const button = document.createElement('button');
          button.textContent = action.label;
          row.append(button);
        }
        return row;
      }),
    };
    await import('./process');
    await import('./requests');
    window.Requests.init(deps);
    window.Requests.appendStatus(document.querySelector('#chat')!, {
      type: 'request_status',
      sessionId: session.id,
      requestId: 'request-1',
    });
    expect(recoveryActions).toHaveBeenCalledWith(
      expect.objectContaining({ sendFailure: 'model', runOwner: 'other', type: 'err' }),
    );
  });
});
