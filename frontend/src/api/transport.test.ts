import { describe, expect, it, vi } from 'vitest';
import { ApiError, requestJson } from './transport';

describe('requestJson', () => {
  it('decodes JSON and preserves same-origin defaults', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await expect(requestJson<{ ok: boolean }>('/api/state')).resolves.toEqual({ ok: true });
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
    await expect(requestJson('/api/state')).rejects.toMatchObject({ status: 403, payload: { error: 'denied' } });
    fetchMock.mockRestore();
  });
});
