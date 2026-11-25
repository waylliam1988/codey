import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ModelState } from '../types/domain';

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
  const { Models } = await import('./models');
  const { ModelSettings } = await import('./model_settings');
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
  ModelSettings.init({ close: vi.fn() });
  await ModelSettings.open();
  document.querySelector<HTMLInputElement>('[aria-label="Use Two"]')?.click();
  const save = document.getElementById('model-settings-save') as HTMLButtonElement;
  expect(save.disabled).toBe(false);
  document.querySelector<HTMLButtonElement>('[data-action="login"]')?.click();
  await vi.advanceTimersByTimeAsync(2001);
  expect(document.querySelector<HTMLInputElement>('[aria-label="Use Two"]')?.checked).toBe(true);
  expect(document.querySelector<HTMLInputElement>('[aria-label="Use Three"]')?.checked).toBe(false);
  expect(save.disabled).toBe(false);
  expect(Models.snapshot().preferences.revision).toBe(7);
  expect(document.querySelector('.connection-auth')?.textContent).toContain('Connected');
  save.click();
  await vi.advanceTimersByTimeAsync(1);
  expect(sent[0]).toEqual({ base_revision: 7, sources: { fixture: { enabled: true, models: ['one', 'two'] } } });
  ModelSettings.close();
});

it('merges auth revision independently of catalog time and retains full model capabilities', async () => {
  const { Models } = await import('./models');
  Models.applySettings({
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
  Models.observeCatalog({
    id: 'fixture',
    checked_at: 10,
    models: [],
    state: 'unavailable',
    authentication: { kind: 'oauth_device', state: 'signed_out', revision: 6, actions: ['login'] },
  });
  let source = Models.snapshot().sources[0];
  expect(source.models[0].thinking_default_allowed).toBe(true);
  expect(source.authentication?.state).toBe('signed_out');
  expect(source.state).toBe('unavailable');
  Models.observeCatalog({
    id: 'fixture',
    checked_at: 110,
    models: [{ id: 'new' }],
    state: 'ready',
    authentication: { kind: 'oauth_device', state: 'ready', revision: 3, actions: ['logout'] },
  });
  source = Models.snapshot().sources[0];
  expect(source.models[0].id).toBe('new');
  expect(source.authentication?.revision).toBe(6);
  expect(source.state).toBe('unavailable');
  Models.observeCatalog(
    JSON.parse(
      JSON.stringify({
        id: 'fixture',
        checked_at: 120,
        models: [{ id: 'latest' }],
        state: 'ready',
        authentication: 'public',
      }),
    ),
  );
  source = Models.snapshot().sources[0];
  expect(source.models[0].id).toBe('latest');
  expect(source.authentication?.revision).toBe(6);
  expect(source.state).toBe('unavailable');
});

it.each(['public', null, { kind: 'oauth_device', state: 'ready', revision: 1 }])(
  'ignores non-interactive authentication in an initial settings response: %j',
  async (authentication) => {
    const { Models } = await import('./models');
    Models.applySettings(
      JSON.parse(
        JSON.stringify({
          preferences: { revision: 1, sources: {} },
          sources: [{ id: 'fixture', models: [{ id: 'one' }], authentication }],
          in_use: [],
        }),
      ),
    );
    expect(Models.snapshot().sources[0].authentication).toBeUndefined();
    expect(Models.snapshot().sources[0].models).toEqual([{ id: 'one' }]);
  },
);

it('opens all cached sources after a catalog supplies a transport authentication label', async () => {
  const { Models } = await import('./models');
  const { ModelSettings } = await import('./model_settings');
  document.body.innerHTML = `<button id="model-settings-save"></button><button id="model-settings-cancel"></button>
    <button id="model-settings-retry"></button><div id="model-settings-error"></div>
    <div id="model-settings-loading"></div><div id="model-settings-body"><div id="local-connection-editor"></div>
    <div id="model-source-list"></div></div>`;
  const sources = [
    { id: 'websites', label: 'Websites', models: [{ id: 'web' }] },
    { id: 'local', label: 'Local', models: [] },
    { id: 'zen', label: 'Zen', models: [{ id: 'zen-model' }] },
    {
      id: 'cline',
      label: 'Cline',
      models: [],
      authentication: { kind: 'oauth_device' as const, state: 'signed_out', revision: 1, actions: ['login' as const] },
    },
  ];
  const state: ModelState = {
    preferences: {
      revision: 1,
      sources: Object.fromEntries(sources.map((source) => [source.id, { enabled: false, models: [] }])),
    },
    sources,
    in_use: [],
  };
  Models.applySettings(state);
  // /api/api_models and /api/model_catalog include this transport label.
  Models.observeCatalog(JSON.parse(JSON.stringify({ ...sources[2], authentication: 'public' })));
  let finishRefresh!: (response: unknown) => void;
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise((resolve) => (finishRefresh = resolve))),
  );
  ModelSettings.init({ close: vi.fn() });
  const opening = ModelSettings.open();
  expect(document.getElementById('model-source-list')?.hidden).toBe(false);
  expect(Array.from(document.querySelectorAll<HTMLElement>('.model-source'), (node) => node.dataset.sourceId)).toEqual([
    'websites',
    'local',
    'zen',
    'cline',
  ]);
  expect(document.querySelector('[data-source-id="zen"] .connection-auth')?.hasAttribute('hidden')).toBe(true);
  expect(document.querySelector('[data-source-id="cline"] [data-action="login"]')).not.toBeNull();
  finishRefresh({ ok: true, json: async () => structuredClone(state) });
  await opening;
  expect(document.getElementById('model-settings-loading')?.hidden).toBe(true);
  expect(document.getElementById('model-settings-error')?.textContent).toBe('');
  ModelSettings.close();
});
