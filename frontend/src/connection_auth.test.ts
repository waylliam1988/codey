import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mountConnectionAuthentication } from './connection_auth';
import type { ConnectionAuthentication, ModelSource } from './types/domain';

const signedOut: ConnectionAuthentication = {
  kind: 'oauth_device',
  state: 'signed_out',
  revision: 1,
  actions: ['login'],
};
const waiting: ConnectionAuthentication = {
  ...signedOut,
  state: 'awaiting_authorization',
  revision: 2,
  attempt_id: 'attempt-A',
  user_code: '<img src=x onerror=alert(1)>',
  actions: ['cancel_login', 'reopen_login'],
};
beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

function panel(authentication = signedOut) {
  const source: ModelSource = { id: 'fixture', models: [{ id: 'm' }], authentication };
  const parent = document.createElement('div');
  document.body.appendChild(parent);
  const publish = vi.fn();
  const error = vi.fn();
  const mounted = mountConnectionAuthentication(parent, source, { current: () => true, publish, error });
  return { parent, publish, error, mounted };
}

it('renders third party code as text, polls once, and stops after authorization', async () => {
  const fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      authentication: {
        ...signedOut,
        state: 'ready',
        revision: 3,
        actions: ['logout'],
      },
    }),
  });
  vi.stubGlobal('fetch', fetch);
  const { parent, mounted, publish } = panel(waiting);
  expect(parent.querySelector('img')).toBeNull();
  expect(parent.textContent).toContain(waiting.user_code);
  await vi.advanceTimersByTimeAsync(2000);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(publish).toHaveBeenCalledTimes(1);
  expect(parent.textContent).toContain('Connected');
  await vi.advanceTimersByTimeAsync(6000);
  expect(fetch).toHaveBeenCalledTimes(1);
  mounted.close();
});

it('closed pages and replaced attempts discard in-flight poll responses', async () => {
  let reply: ((value: unknown) => void) | undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(
      () =>
        new Promise((resolve) => {
          reply = resolve;
        }),
    ),
  );
  const { mounted, publish } = panel(waiting);
  await vi.advanceTimersByTimeAsync(2000);
  mounted.update({ ...waiting, attempt_id: 'attempt-B', revision: 4 });
  reply?.({ ok: true, json: async () => ({ authentication: { ...signedOut, state: 'ready', revision: 3 } }) });
  await vi.advanceTimersByTimeAsync(1);
  expect(publish).not.toHaveBeenCalled();
  mounted.close();
  await vi.advanceTimersByTimeAsync(4000);
  expect(publish).not.toHaveBeenCalled();
});

it('connection 401 stays a local control error and is never an operator logout', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ code: 'reauth_required', error: 'Sign in again.' }),
    }),
  );
  const { parent, error, mounted } = panel();
  parent.querySelector<HTMLButtonElement>('button')?.click();
  await vi.advanceTimersByTimeAsync(1);
  expect(error).toHaveBeenCalledWith('Sign in again.');
  expect(parent.textContent).toContain('Sign in');
  mounted.close();
});

it('ignores older auth snapshots regardless of catalog timestamps', () => {
  const { parent, mounted } = panel({ ...signedOut, state: 'ready', revision: 9, actions: ['logout'] });
  mounted.update(waiting);
  expect(parent.textContent).toContain('Connected');
  expect(parent.textContent).not.toContain('USER');
  mounted.close();
});
