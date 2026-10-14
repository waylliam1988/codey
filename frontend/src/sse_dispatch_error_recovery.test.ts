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
    vi.stubGlobal('EventSource', FakeEventSource);
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
