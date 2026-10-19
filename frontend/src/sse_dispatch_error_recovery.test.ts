import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class FakeEventSource {
  static OPEN = 1;
  readyState = FakeEventSource.OPEN;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(_url: string) {
    (globalThis as unknown as { __lastEventSource?: FakeEventSource }).__lastEventSource = this;
  }
  close(): void {}
}

describe('SSE dispatch validation and recovery', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete (globalThis as { __lastEventSource?: FakeEventSource }).__lastEventSource;
  });

  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  it('scopes the persisted commit cursor to a database and accepts a reset handshake', async () => {
    localStorage.setItem('codey:durable-cursor', JSON.stringify({ database_id: 'old', commit_seq: 90 }));
    const handled = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    await import('./sse');
    window.Sse.init({
      setStatus: vi.fn(),
      applyRunState: vi.fn(),
      handleServerEvent: handled,
      acceptRun: vi.fn(),
      setProviderBusy: vi.fn(),
      updateSend: vi.fn(),
      updateComposerContext: vi.fn(),
      refreshProviderStatus: vi.fn(),
    });
    window.Sse.connect();
    const source = (globalThis as unknown as { __lastEventSource: FakeEventSource }).__lastEventSource;
    source.onmessage?.(
      new MessageEvent('message', { data: JSON.stringify({ type: 'hello', database_id: 'new', after_commit_seq: 0 }) }),
    );
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    await Promise.resolve();
    source.onmessage?.(
      new MessageEvent('message', {
        lastEventId: '1:1',
        data: JSON.stringify({
          type: 'durable_commit',
          session_id: 'chat',
          database_id: 'new',
          commit_seq: 1,
          entry_ids: ['entry'],
        }),
      }),
    );
    await vi.waitFor(() => expect(handled).toHaveBeenCalledTimes(1));
    expect(JSON.parse(localStorage.getItem('codey:durable-cursor')!)).toEqual({ database_id: 'new', commit_seq: 1 });
  });

  it('does not acknowledge a failed durable projection or skip it with a later cursor', async () => {
    localStorage.setItem('codey:durable-cursor', JSON.stringify({ database_id: 'db', commit_seq: 4 }));
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {})),
    );
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    await import('./sse');
    window.Sse.init({
      setStatus: vi.fn(),
      applyRunState: vi.fn(),
      handleServerEvent: vi.fn(() => {
        throw new Error('projection failed');
      }),
      acceptRun: vi.fn(),
      setProviderBusy: vi.fn(),
      updateSend: vi.fn(),
      updateComposerContext: vi.fn(),
      refreshProviderStatus: vi.fn(),
    });
    window.Sse.connect();
    const source = (globalThis as unknown as { __lastEventSource: FakeEventSource }).__lastEventSource;
    for (const seq of [5, 6])
      source.onmessage?.(
        new MessageEvent('message', {
          lastEventId: `1:${seq}`,
          data: JSON.stringify({
            type: 'durable_commit',
            session_id: 'chat',
            database_id: 'db',
            commit_seq: seq,
            entry_ids: ['entry'],
          }),
        }),
      );
    expect(JSON.parse(localStorage.getItem('codey:durable-cursor')!).commit_seq).toBe(4);
  });

  it('does not consume an invalid event id and reconciles handler failures', async () => {
    const handleServerEvent = vi.fn(() => {
      throw new Error('render failed');
    });
    let resolveFirst!: () => void;
    const firstReconcile = new Promise<void>((resolve) => {
      resolveFirst = resolve;
    });
    const fetchMock = vi.fn(async () => {
      if (fetchMock.mock.calls.length === 1) {
        resolveFirst();
      }
      return { ok: true, json: async () => ({}) };
    });
    vi.stubGlobal('fetch', fetchMock);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    window.Sse = {} as typeof window.Sse;
    await import('./sse');
    window.Sse.init({
      setStatus: vi.fn(),
      applyRunState: vi.fn(),
      handleServerEvent,
      acceptRun: vi.fn(),
      setProviderBusy: vi.fn(),
      updateSend: vi.fn(),
      updateComposerContext: vi.fn(),
      refreshProviderStatus: vi.fn(),
    });
    window.Sse.connect();
    const source = (globalThis as unknown as { __lastEventSource?: FakeEventSource }).__lastEventSource!;
    source.onmessage?.(new MessageEvent('message', { data: JSON.stringify(null), lastEventId: '7' }));
    source.onmessage?.(
      new MessageEvent('message', { data: JSON.stringify({ type: 'reply', event_id: 7 }), lastEventId: '7' }),
    );
    await firstReconcile;
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(handleServerEvent).toHaveBeenCalledTimes(1);
    expect(errorSpy).toHaveBeenCalledWith('SSE event handling failed', expect.any(Error));
  });
});
