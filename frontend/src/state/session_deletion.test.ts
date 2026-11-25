import { afterEach, describe, expect, it, vi } from 'vitest';
import { forgetSessionOnServer } from './session_deletion';

describe('server conversation deletion', () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    [409, { ok: false, error: 'conversation has forked children' }],
    [
      503,
      { ok: false, error: 'Conversation cleanup incomplete; retry deletion.', unpurged_stores: ['managed_outputs'] },
    ],
    [200, { ok: true, warnings: ['cleanup failed'], unpurged_stores: ['durable'] }],
  ])('keeps sidebar state on an incomplete deletion (%s)', async (status, body) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })));
    const accept = vi.fn();
    const error = vi.fn();
    expect(await forgetSessionOnServer('parent', false, accept, error)).toBe(false);
    expect(accept).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalledOnce();
  });

  it('accepts the server state only after successful deletion', async () => {
    const state = { revision: 2, sessions: [{ id: 'child' }] };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, state }), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const accept = vi.fn();
    const error = vi.fn();
    expect(await forgetSessionOnServer('parent', false, accept, error)).toBe(true);
    expect(accept).toHaveBeenCalledWith(state);
    expect(error).not.toHaveBeenCalled();
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ session_id: 'parent', preserve_ui: false });
  });

  it('reports network failures without accepting state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    const accept = vi.fn();
    const error = vi.fn();
    expect(await forgetSessionOnServer('parent', false, accept, error)).toBe(false);
    expect(accept).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalledOnce();
  });
});
