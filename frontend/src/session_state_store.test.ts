import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionStateStore, type SessionStateChange } from './session_state_store';
import type { Message, Session } from './types/domain';
import { runtime } from './runtime';
import type { UiStateApi } from './ui_state';
import './ui_state';

function session(id: string): Session {
  return {
    id,
    title: 'New chat',
    messages: [],
    provider: '',
    draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
  };
}

function setup() {
  const changes: SessionStateChange[] = [];
  const ui = runtime.UiState as UiStateApi;
  const forget = vi.fn(async (_id: string, _preserve: boolean) => true);
  const store = new SessionStateStore({
    ui: { ...ui, persist: vi.fn(), persistNow: vi.fn() },
    defaultProvider: () => 'default',
    isRenaming: () => false,
    invalidate: (change) => changes.push(change),
    forgetView: vi.fn(),
    forgetOnServer: forget,
    requestIdForRun: (_id, runId) => (runId === 'run' ? 'request' : undefined),
    mountedStreamId: () => undefined,
    messageAdded: vi.fn(),
  });
  store.sessions = [session('a'), session('b')];
  store.activeId = 'a';
  return { store, changes, forget };
}

describe('SessionStateStore ownership', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('preserves an existing session with no selected provider', () => {
    const { store } = setup();
    expect(store.currentProviderId()).toBe('');
    store.activeId = 'missing';
    expect(store.currentProviderId()).toBe('default');
  });

  it('does not erase local state when server deletion fails', async () => {
    const { store, forget, changes } = setup();
    forget.mockResolvedValue(false);
    await store.deleteSession('a');
    expect(store.sessions.map((item) => item.id)).toEqual(['a', 'b']);
    expect(store.activeId).toBe('a');
    expect(changes).toEqual([]);
  });

  it('resolves deletion against state accepted while the request was in flight', async () => {
    const { store, forget } = setup();
    let accept: (ok: boolean) => void = () => {};
    forget.mockImplementation(
      () =>
        new Promise<boolean>((resolve) => {
          accept = resolve;
        }),
    );
    const deleted = store.deleteSession('a');
    store.sessions = [session('a'), session('c')];
    store.activeId = 'c';
    accept(true);
    await deleted;
    expect(store.sessions.map((item) => item.id)).toEqual(['c']);
    expect(store.activeId).toBe('c');
  });

  it('keeps one answer and preserves stream identity when the terminal answer arrives', () => {
    const { store } = setup();
    store.addMessage('a', { type: 'asst', runId: 'run', streamId: 'stream', streamState: 'partial', text: 'partial' });
    store.addMessage('a', { type: 'asst', runId: 'run', text: 'final', eventKey: 'answer' });
    expect(store.activeSession()?.messages).toEqual([
      expect.objectContaining({ text: 'final', streamId: 'stream', streamState: 'complete', finalAnswer: true }),
    ]);
  });

  it('inserts run messages before their request status and deduplicates repeated delivery', () => {
    const { store, changes } = setup();
    store.addMessage('a', { type: 'request_status', requestId: 'request' });
    const tool: Message = { type: 'tool', runId: 'run', toolKey: 'run:call', eventKey: 'event' };
    expect(store.addMessage('a', tool)).toBe(true);
    expect(store.addMessage('a', tool)).toBe(false);
    expect(store.activeSession()?.messages.map((item) => item.type)).toEqual(['tool', 'request_status']);
    expect(changes.at(-1)).toMatchObject({ beforeRequestId: 'request' });
    store.dropMessages('a', (item) => item === tool);
    expect(store.activeSession()?._toolKeys?.has('run:call')).toBe(false);
    expect(store.addMessage('a', tool)).toBe(true);
  });

  it('merges the shell result into its request instead of appending another card', () => {
    const { store } = setup();
    store.addMessage('a', { type: 'shell_request', id: 'approval', command: 'echo hi', runId: 'run' });
    store.addMessage('a', { type: 'shell_result', id: 'approval', output: 'hi', approved: true });
    expect(store.activeSession()?.messages).toHaveLength(1);
    expect(store.activeSession()?.messages[0]).toMatchObject({
      type: 'shell_result',
      command: 'echo hi',
      output: 'hi',
    });
  });

  it('does not duplicate queued user messages already represented by their input id', () => {
    const { store } = setup();
    store.addMessage('a', { type: 'user', id: 'input', text: 'hello' });
    store.addMessage('a', { type: 'user', queuedInputId: 'input', text: 'hello' });
    expect(store.activeSession()?.messages).toHaveLength(1);
  });
});
