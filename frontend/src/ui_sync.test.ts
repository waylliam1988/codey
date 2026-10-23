import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { UiStateApi } from './ui_state';

let ui: UiStateApi;
let state: any;
beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  localStorage.clear();
  document.body.innerHTML =
    '<textarea id="task"></textarea><div id="ui-save-notice" hidden><span id="ui-save-message"></span><button id="ui-save-retry"></button></div>';
  const { runtime } = await import('./runtime');
  await import('./ui_state');
  ui = runtime.UiState;
  state = { sessions: [], projects: [], active_id: '', revision: 0, updated_at: 0 };
  ui.init({
    getSessions: () => state.sessions,
    setSessions: (v) => {
      state.sessions = v;
    },
    getProjects: () => state.projects,
    setProjects: (v) => {
      state.projects = v;
    },
    getActiveId: () => state.active_id,
    setActiveId: (v) => {
      state.active_id = v;
    },
    getRevision: () => state.revision,
    setRevision: (v) => {
      state.revision = v;
    },
    getUpdatedAt: () => state.updated_at,
    setUpdatedAt: (v) => {
      state.updated_at = v;
    },
  });
  ui.accept({
    sessions: [{ id: 'a', title: 'Alpha', draft: { text: 'draft' } }],
    projects: [],
    active_id: 'a',
    revision: 1,
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it('rebases a conflict and continues saving without a global blocked state', async () => {
  const remote = structuredClone(ui.current());
  remote.revision = 2;
  const posts: any[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      if (!options?.method) return { ok: true, json: async () => ({ state: remote }) };
      const body = JSON.parse(options.body);
      posts.push(body);
      return posts.length === 1
        ? { status: 409, ok: false, json: async () => ({ revision: 2 }) }
        : { status: 200, ok: true, json: async () => ({ revision: posts.length + 1 }) };
    }),
  );
  expect(await ui.ensureSaved()).toBe(true);
  expect(posts.map((post) => post.base_revision)).toEqual([1, 2]);
  state.sessions[0].draft.text = 'new draft';
  expect(await ui.ensureSaved()).toBe(true);
  expect(posts.at(-1).state.sessions[0].draft.text).toBe('new draft');
  expect(document.getElementById('ui-save-notice')!.hidden).toBe(true);
});

it('only acknowledges the edits captured by an in-flight save', async () => {
  let resolve!: (value: any) => void;
  const posts: any[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((_url, options) => {
      posts.push(JSON.parse(options.body));
      return posts.length === 1
        ? new Promise((r) => {
            resolve = r;
          })
        : Promise.resolve({ ok: true, json: async () => ({ revision: 3 }) });
    }),
  );
  const pending = ui.ensureSaved();
  state.sessions[0].draft.text = 'typed during save';
  resolve({ ok: true, json: async () => ({ revision: 2 }) });
  expect(await pending).toBe(true);
  expect(posts).toHaveLength(2);
  expect(posts[1].state.sessions[0].draft.text).toBe('typed during save');
});

it('does not resurrect a remotely deleted chat when restoring an unsaved draft', async () => {
  ui.cache();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) =>
      options?.method
        ? { ok: true, json: async () => ({ revision: 3 }) }
        : { ok: true, json: async () => ({ state: { sessions: [], projects: [], active_id: '', revision: 2 } }) },
    ),
  );
  await ui.restoreFromServer();
  await ui.ensureSaved();
  expect(state.sessions.some((s: any) => s.id === 'a')).toBe(false);
});

it('keeps transient failures silent and restores saving after reconnect', async () => {
  const fetch = vi.fn().mockRejectedValue(new Error('offline'));
  vi.stubGlobal('fetch', fetch);
  ui.bindPagehide();
  expect(await ui.ensureSaved()).toBe(false);
  expect(document.getElementById('ui-save-notice')!.hidden).toBe(true);
  expect(await ui.ensureSaved()).toBe(false);
  expect(await ui.ensureSaved()).toBe(false);
  expect(document.getElementById('ui-save-message')!.textContent).toBe('Draft changes couldn’t be saved');
  expect(document.getElementById('ui-save-notice')!.hidden).toBe(false);
  fetch.mockResolvedValue({ ok: true, json: async () => ({ revision: 2 }) });
  expect(await ui.ensureSaved()).toBe(true);
  expect(document.getElementById('ui-save-notice')!.hidden).toBe(true);
});

it('atomically caches edits with their matching base and migrates old cache only once', () => {
  ui.cache();
  const envelope = JSON.parse(localStorage.getItem('codey:ui-cache')!);
  expect(envelope.state.sessions[0].draft.text).toBe('draft');
  expect(envelope.confirmed.revision).toBe(1);
  expect(envelope.state.sessions[0]).not.toHaveProperty('messages');
  localStorage.removeItem('codey:ui-cache');
  localStorage.setItem('codey:sessions', JSON.stringify([{ id: 'legacy', draft: { text: 'recover' } }]));
  localStorage.setItem('codey:active', 'legacy');
  expect((ui.cached().sessions as any[])[0].draft.text).toBe('recover');
  expect(localStorage.getItem('codey:sessions')).toBeNull();
  expect(JSON.parse(localStorage.getItem('codey:ui-cache')!).confirmed).toBeNull();
});

it('ignores an older mutation response after accepting a newer inventory', async () => {
  let resolve!: (value: any) => void;
  let calls = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      ++calls === 1
        ? new Promise((r) => {
            resolve = r;
          })
        : Promise.resolve({ ok: true, json: async () => ({ revision: 5 }) }),
    ),
  );
  const pending = ui.ensureSaved();
  ui.accept({ ...ui.current(), revision: 4, sessions: [] });
  resolve({ ok: true, json: async () => ({ revision: 2 }) });
  expect(await pending).toBe(true);
  expect(state.sessions.some((s: any) => s.id === 'a')).toBe(false);
  expect(state.revision).toBe(5);
});
