import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ModelState } from './types/domain';

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

it('OAuth completion preserves unsaved model checkboxes and preference revision', async () => {
  const { runtime } = await import('./runtime');
  await import('./models');
  await import('./model_settings');
  document.body.innerHTML = `<button id="model-settings-save"></button><button id="model-settings-cancel"></button>
    <button id="model-settings-retry"></button><div id="model-settings-error"></div>
    <div id="model-settings-loading"></div><div id="model-settings-body"><div id="local-connection-editor"></div>
    <div id="model-source-list"></div></div>`;
  const state = {
    preferences: { revision: 7, sources: { fixture: { enabled: true, models: ['one'] } } },
    sources: [
      {
        id: 'fixture',
        label: 'Fixture',
        discoverable: true,
        models: [
          { id: 'one', name: 'One' },
          { id: 'two', name: 'Two' },
        ],
        authentication: {
          kind: 'oauth_device' as const,
          state: 'signed_out',
          revision: 1,
          actions: ['login' as const],
        },
      },
    ],
    in_use: [],
  } satisfies ModelState;
  const sent: Array<Record<string, unknown>> = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/connection_action')
        return {
          ok: true,
          json: async () => ({
            authentication: {
              kind: 'oauth_device',
              state: 'awaiting_authorization',
              revision: 2,
              attempt_id: 'attempt',
              actions: ['cancel_login'],
            },
          }),
        };
      if (url.startsWith('/api/connection_auth'))
        return {
          ok: true,
          json: async () => ({
            authentication: { kind: 'oauth_device', state: 'ready', revision: 3, actions: ['logout'] },
          }),
        };
      if (url === '/api/model_catalog')
        return {
          ok: true,
          json: async () => ({
            source: {
              ...structuredClone(state.sources[0]),
              authentication: { kind: 'oauth_device', state: 'ready', revision: 3, actions: ['logout'] },
              models: [...state.sources[0].models, { id: 'three', name: 'Three' }],
            },
          }),
        };
      if (init?.method === 'POST') sent.push(JSON.parse(String(init.body)));
      return { ok: true, json: async () => structuredClone(state) };
    }),
  );
  runtime.ModelSettings.init({ close: vi.fn() });
  await runtime.ModelSettings.open();
  document.querySelector<HTMLInputElement>('[aria-label="Use Two"]')?.click();
  const save = document.getElementById('model-settings-save') as HTMLButtonElement;
  expect(save.disabled).toBe(false);
  document.querySelector<HTMLButtonElement>('[data-action="login"]')?.click();
  await vi.advanceTimersByTimeAsync(2001);
  expect(document.querySelector<HTMLInputElement>('[aria-label="Use Two"]')?.checked).toBe(true);
  expect(document.querySelector<HTMLInputElement>('[aria-label="Use Three"]')?.checked).toBe(false);
  expect(save.disabled).toBe(false);
  expect(runtime.Models.snapshot().preferences.revision).toBe(7);
  expect(document.querySelector('.connection-auth')?.textContent).toContain('Connected');
  save.click();
  await vi.advanceTimersByTimeAsync(1);
  expect(sent[0]).toEqual({ base_revision: 7, sources: { fixture: { enabled: true, models: ['one', 'two'] } } });
  runtime.ModelSettings.close();
});

it('merges auth revision independently of catalog time and retains full model capabilities', async () => {
  const { runtime } = await import('./runtime');
  await import('./models');
  runtime.Models.applySettings({
    preferences: { revision: 4, sources: {} },
    in_use: [],
    sources: [
      {
        id: 'fixture',
        checked_at: 100,
        models: [{ id: 'm', efforts: [], thinking_toggle: true, thinking_default_allowed: true }],
        state: 'ready',
        authentication: { kind: 'oauth_device', state: 'ready', revision: 5, actions: ['logout'] },
      },
    ],
  });
  runtime.Models.observeCatalog({
    id: 'fixture',
    checked_at: 10,
    models: [],
    state: 'unavailable',
    authentication: { kind: 'oauth_device', state: 'signed_out', revision: 6, actions: ['login'] },
  });
  let source = runtime.Models.snapshot().sources[0];
  expect(source.models[0].thinking_default_allowed).toBe(true);
  expect(source.authentication.state).toBe('signed_out');
  expect(source.state).toBe('unavailable');
  runtime.Models.observeCatalog({
    id: 'fixture',
    checked_at: 110,
    models: [{ id: 'new' }],
    state: 'ready',
    authentication: { kind: 'oauth_device', state: 'ready', revision: 3, actions: ['logout'] },
  });
  source = runtime.Models.snapshot().sources[0];
  expect(source.models[0].id).toBe('new');
  expect(source.authentication.revision).toBe(6);
  expect(source.state).toBe('unavailable');
});
