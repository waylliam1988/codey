import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from './types/domain';

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
