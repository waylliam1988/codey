import { afterEach, expect, it, vi } from 'vitest';
import { OperatorAuth } from './operator_auth';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  history.replaceState(null, '', '/');
});

it('removes the bootstrap credential before exchanging it', async () => {
  history.replaceState(null, '', '/?project=code#codey_bootstrap=secret');
  const fetch = vi.fn(async () => {
    expect(location.hash).toBe('');
    expect(location.search).toBe('?project=code');
    return new Response(null);
  });
  vi.stubGlobal('fetch', fetch);

  await OperatorAuth.authorize();

  expect(fetch).toHaveBeenCalledExactlyOnceWith(
    '/api/operator_session',
    expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ token: 'secret' }),
    }),
  );
});

it.each([true, false])('verifies the existing session when authorized is %s', async (authorized) => {
  const fetch = vi.fn(async () => new Response(null, { status: authorized ? 200 : 403 }));
  vi.stubGlobal('fetch', fetch);

  if (authorized) await expect(OperatorAuth.authorize()).resolves.toBeUndefined();
  else await expect(OperatorAuth.authorize()).rejects.toThrow('authorize this browser');

  expect(fetch).toHaveBeenCalledExactlyOnceWith(
    '/api/operator_session',
    expect.objectContaining({ credentials: 'same-origin' }),
  );
  expect(fetch.mock.calls).toHaveLength(1);
});
