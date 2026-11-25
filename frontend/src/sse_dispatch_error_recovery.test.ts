import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class FakeEventSource {
  static OPEN = 1;
  static instances: FakeEventSource[] = [];
  readyState = FakeEventSource.OPEN;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  close(): void {}
}

describe('SSE dispatch validation and recovery', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  beforeEach(() => {
    vi.resetModules();
    FakeEventSource.instances = [];
    localStorage.clear();
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  it('scopes the persisted commit cursor to a database and accepts a reset handshake', async () => {
    localStorage.setItem('codey:durable-cursor', JSON.stringify({ database_id: 'old', commit_seq: 90 }));
    const handled = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    const { Sse } = await import('./sse');
    Sse.init({
      forRun: () => null,
      setStatus: vi.fn(),
      applyRunState: vi.fn(),
      handleServerEvent: handled,
      acceptRun: vi.fn(),
      setProviderBusy: vi.fn(),
      updateSend: vi.fn(),
      updateComposerContext: vi.fn(),
      refreshProviderStatus: vi.fn(),
    });
    Sse.connect();
    const [source] = FakeEventSource.instances;
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
    const { Sse } = await import('./sse');
    Sse.init({
      forRun: () => null,
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
    Sse.connect();
    const [source] = FakeEventSource.instances;
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
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(localStorage.getItem('codey:durable-cursor')).toBe('{"database_id":"db","commit_seq":4}');
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
    const { Sse } = await import('./sse');
    Sse.init({
      forRun: () => null,
      setStatus: vi.fn(),
      applyRunState: vi.fn(),
      handleServerEvent,
      acceptRun: vi.fn(),
      setProviderBusy: vi.fn(),
      updateSend: vi.fn(),
      updateComposerContext: vi.fn(),
      refreshProviderStatus: vi.fn(),
    });
    Sse.connect();
    const [source] = FakeEventSource.instances;
    source.onmessage?.(new MessageEvent('message', { data: JSON.stringify(null), lastEventId: '7' }));
    source.onmessage?.(
      new MessageEvent('message', { data: JSON.stringify({ type: 'reply', event_id: 7 }), lastEventId: '7' }),
    );
    await firstReconcile;
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(handleServerEvent).toHaveBeenCalledTimes(1);
    expect(errorSpy).toHaveBeenCalledWith('SSE event handling failed', expect.any(Error));
  });

  it('replays an interrupted async delivery from the confirmed cursors', async () => {
    vi.useFakeTimers();
    localStorage.setItem('codey:durable-cursor', JSON.stringify({ database_id: 'db', commit_seq: 4 }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ busy: false }))));
    let finish: () => void = () => {};
    const delivered = vi.fn().mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const { Sse } = await import('./sse');
    Sse.init({
      forRun: () => null,
      setStatus: vi.fn(),
      applyRunState: vi.fn(),
      handleServerEvent: delivered,
      acceptRun: vi.fn(),
      setProviderBusy: vi.fn(),
      updateSend: vi.fn(),
      updateComposerContext: vi.fn(),
      refreshProviderStatus: vi.fn(),
    });
    Sse.connect();
    const [source] = FakeEventSource.instances;
    const commit = new MessageEvent('message', {
      lastEventId: '10:5',
      data: JSON.stringify({ type: 'durable_commit', session_id: 'chat', commit_seq: 5 }),
    });
    const reply = new MessageEvent('message', {
      lastEventId: '7:5',
      data: JSON.stringify({ type: 'reply', session_id: 'chat', text: 'replayed' }),
    });
    source.onmessage?.(commit);
    await Promise.resolve();
    expect(delivered).toHaveBeenCalledTimes(1);
    source.onmessage?.(reply);
    source.onerror?.();
    finish();
    await vi.advanceTimersByTimeAsync(1000);
    const replay = FakeEventSource.instances[1];
    expect(replay.url).toBe('/api/events?database_id=db&after_commit_seq=4');
    replay.onmessage?.(commit);
    replay.onmessage?.(reply);
    await vi.advanceTimersByTimeAsync(0);
    expect(delivered.mock.calls.map(([event, cursor]: unknown[]) => [event, cursor])).toEqual([
      [expect.objectContaining({ type: 'durable_commit' }), '10:5'],
      [expect.objectContaining({ type: 'durable_commit' }), '10:5'],
      [expect.objectContaining({ type: 'reply' }), '7:5'],
    ]);
    expect(localStorage.getItem('codey:durable-cursor')).toBe('{"database_id":"db","commit_seq":5}');
  });

  async function streamFixture() {
    const handled = vi.fn();
    const applyRunState = vi.fn();
    const { Sse } = await import('./sse');
    Sse.init({
      forRun: () => null,
      setStatus: vi.fn(),
      applyRunState,
      handleServerEvent: handled,
      acceptRun: vi.fn(),
      setProviderBusy: vi.fn(),
      updateSend: vi.fn(),
      updateComposerContext: vi.fn(),
      refreshProviderStatus: vi.fn(),
    });
    Sse.connect();
    const [source] = FakeEventSource.instances;
    const emit = (id: string, event: object) => {
      source.onmessage?.(new MessageEvent('message', { lastEventId: id, data: JSON.stringify(event) }));
    };
    return { Sse, source, emit, handled, applyRunState };
  }

  it('deduplicates transient events and accepts a replay reset', async () => {
    const { emit, handled } = await streamFixture();
    const reply = { type: 'reply', session_id: 'session', text: 'response' };
    emit('9', reply);
    emit('9', reply);
    emit('8', reply);
    emit('0', { type: 'resync_required', cursor: 0, reason: 'sse_cursor_ahead' });
    emit('1', reply);
    emit('1', reply);

    await vi.waitFor(() =>
      expect(handled.mock.calls.map(([event, cursor]: unknown[]) => [event, cursor])).toEqual([
        [expect.objectContaining({ type: 'reply' }), '9'],
        [expect.objectContaining({ type: 'resync_required' }), '0'],
        [expect.objectContaining({ type: 'reply' }), '1'],
      ]),
    );
  });

  it('advances durable and transient cursors independently', async () => {
    const { Sse, emit, handled } = await streamFixture();
    for (const [cursor, commit] of [
      ['9:10', 10],
      ['9:10', 10],
      ['9:11', 11],
      ['8:12', 12],
    ] as const) {
      emit(cursor, { type: 'durable_commit', session_id: 'session', commit_seq: commit });
    }
    emit('10:11', { type: 'reply', session_id: 'session', text: 'response' });
    await vi.waitFor(() =>
      expect(handled.mock.calls.map(([, cursor]: unknown[]) => cursor)).toEqual(['9:10', '9:11', '8:12', '10:11']),
    );
    expect(Sse.eventKey('9:10')).toBe('sse:9:10');
  });

  it('drops an incomplete reconciliation buffer and refreshes the state again', async () => {
    const pending: Array<(response: Response) => void> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>((resolve) => pending.push(resolve))),
    );
    const { Sse, emit, handled, applyRunState } = await streamFixture();
    const first = Sse.reconcileRunState();
    for (let id = 1; id <= 101; id++) {
      emit(String(id), { type: 'reply', session_id: 'session', text: 'response' });
    }
    expect(pending).toHaveLength(1);
    pending.shift()?.(new Response(JSON.stringify({ busy: false, run_status: 'first' })));
    await first;
    expect(handled).not.toHaveBeenCalled();
    expect(pending).toHaveLength(1);
    pending.shift()?.(new Response(JSON.stringify({ busy: false, run_status: 'second' })));
    await vi.waitFor(() =>
      expect(applyRunState.mock.calls.map(([state]: unknown[]) => state)).toEqual([
        { busy: false, run_status: 'first' },
        { busy: false, run_status: 'second' },
      ]),
    );
    expect(handled).not.toHaveBeenCalled();
  });
});
