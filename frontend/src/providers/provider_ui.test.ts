import { readFileSync } from 'node:fs';
import { afterEach, expect, it, vi } from 'vitest';
import { Models } from './models';
import { ProviderUI } from './provider_ui';
import { UiState } from '../state/ui_state';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.replaceChildren();
});

it('loads the boot catalog without probing provider availability', async () => {
  const fetch = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          providers: [{ id: 'deepseek', label: 'DeepSeek' }],
          default: 'deepseek',
        }),
      ),
  );
  vi.stubGlobal('fetch', fetch);
  await ProviderUI.adoptCatalog();
  expect(fetch).toHaveBeenCalledExactlyOnceWith(
    '/api/provider_catalog',
    expect.objectContaining({ cache: 'no-store' }),
  );
  expect(UiState.PROVIDERS).toEqual(['deepseek']);
  expect(UiState.DEFAULT_PROVIDER).toBe('deepseek');
});

it('removes unavailable choices and adopts a changed catalog default without reloading the page', async () => {
  document.documentElement.innerHTML = readFileSync('index.html', 'utf8');
  vi.useFakeTimers();
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(null, { status: 404 })),
  );
  Models.applySettings({
    preferences: { revision: 0, sources: { websites: { enabled: true, models: ['deepseek', 'mimo'] } } },
    sources: [
      {
        id: 'websites',
        label: 'Websites',
        models: [
          { id: 'deepseek', name: 'DeepSeek' },
          { id: 'mimo', name: 'MiMo' },
        ],
      },
    ],
    in_use: [],
  });
  const providers = [
    { id: 'deepseek', label: 'DeepSeek' },
    { id: 'mimo', label: 'MiMo' },
  ];
  UiState.setProviders(['deepseek', 'mimo'], { deepseek: 'DeepSeek', mimo: 'MiMo' }, 'deepseek');
  const choose = (id: string) => {
    const element = document.getElementById(id);
    if (!element) throw new Error('Missing UI element: ' + id);
    return element;
  };
  ProviderUI.init({
    $: choose,
    escapeHtml: String,
    currentProviderId: () => UiState.DEFAULT_PROVIDER,
    setActiveProvider: UiState.setDefaultProvider,
    PROVIDERS: UiState.PROVIDERS,
    PROVIDER_LABELS: UiState.PROVIDER_LABELS,
    DEFAULT_PROVIDER: UiState.DEFAULT_PROVIDER,
    activeSession: () => undefined,
    updateSend: vi.fn(),
    persistActiveNow: vi.fn(),
  });
  const visible = () =>
    Array.from(document.querySelectorAll<HTMLButtonElement>('.provider-item'), (row) => row.dataset.provider);
  expect(visible()).toEqual(['deepseek', 'mimo']);
  expect(ProviderUI.applyConfig({ providers: providers.slice(0, 1), default: 'deepseek' })).toBe(true);
  expect(UiState.PROVIDERS).toEqual(['deepseek']);
  expect(visible()).toEqual(['deepseek']);

  expect(ProviderUI.applyConfig({ providers, default: 'mimo' })).toBe(true);
  expect(UiState.DEFAULT_PROVIDER).toBe('mimo');
  expect(visible()).toEqual(['deepseek', 'mimo']);
  expect(ProviderUI.applyConfig({ providers, default: 'deepseek' })).toBe(true);
  expect(UiState.DEFAULT_PROVIDER).toBe('deepseek');
  expect(visible()).toEqual(['deepseek', 'mimo']);
});
