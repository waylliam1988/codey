import { describe, expect, it, vi } from 'vitest';
import { ApiError, requestJson } from './transport';

describe('requestJson', () => {
  const decode = (value: unknown): { ok: boolean } => {
    if (typeof value !== 'object' || value === null || !('ok' in value) || typeof value.ok !== 'boolean')
      throw new Error('Invalid response');
    return { ok: value.ok };
  };
  it('decodes JSON and preserves same-origin defaults', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await expect(requestJson('/api/state', decode)).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/state',
      expect.objectContaining({
        credentials: 'same-origin',
        cache: 'no-store',
      }),
    );
    fetchMock.mockRestore();
  });

  it('classifies HTTP errors with status and payload', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ error: 'denied' }), { status: 403 }));
    await expect(requestJson('/api/state', decode)).rejects.toMatchObject({
      status: 403,
      payload: { error: 'denied' },
    });
    fetchMock.mockRestore();
  });
});
