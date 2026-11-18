// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { runtime } from './runtime';
import type { LocalContextDeps } from './local_context_drawer';
import './local_context_drawer';

interface Drawer {
  init(deps: LocalContextDeps): void;
  open(): void;
}

function drawer(): Drawer {
  const value: unknown = runtime.LocalContextDrawer;
  if (isDrawer(value)) return value;
  throw new Error('Drawer unavailable');
}

function isDrawer(value: unknown): value is Drawer {
  return (
    typeof value === 'object' &&
    value !== null &&
    'init' in value &&
    typeof value.init === 'function' &&
    'open' in value &&
    typeof value.open === 'function'
  );
}

function element(id: string): HTMLElement {
  const value = document.getElementById(id);
  if (!value) throw new Error(id);
  return value;
}

function select(label: string): HTMLSelectElement {
  const value = document.querySelector(`select[aria-label="${label}"]`);
  if (!(value instanceof HTMLSelectElement)) throw new Error(label);
  return value;
}

const actions: unknown[] = [];
let activeSession = 'new-session';
const observation = (id: string, session: string, summary: string) => ({
  id,
  session_id: session,
  project: 'D:/project',
  type: 'observation',
  summary,
  reply_digest: 'a'.repeat(64),
  status: 'committed',
});

beforeEach(async () => {
  actions.length = 0;
  activeSession = 'new-session';
  document.body.innerHTML = ['drawer', 'close', 'export', 'refresh', 'menu', 'subtitle', 'body']
    .map((id) => `<div id="local-context-${id}"></div>`)
    .join('');
  runtime.UiState = {
    setDrawerOpen: (id: string, open: boolean): void => {
      element(id).classList.toggle('open', open);
    },
  };
  vi.stubGlobal('fetch', async (url: string, init?: RequestInit): Promise<Response> => {
    if (url === '/api/ghost/action') {
      const body: unknown = JSON.parse(String(init?.body));
      actions.push(body);
      return Response.json({ ok: true });
    }
    return Response.json({
      ok: true,
      available: true,
      enabled: true,
      counts: {},
      scope: { session_id: 'new-session', project: 'D:/project' },
      observations: [
        observation('correction', 'old-session', 'Prioritize accessibility.'),
        observation('earlier', 'earlier-session', 'An earlier proposal.'),
      ],
      health: {},
    });
  });
  drawer().init({
    $: element,
    getActiveId: () => activeSession,
    currentProjectPath: () => 'D:/project',
    copyText: async (_text: string): Promise<boolean> => true,
  });
  drawer().open();
  await vi.waitFor(() => expect(document.querySelectorAll('.local-context-row')).toHaveLength(2));
  const more = document.querySelector('.local-context-more');
  if (!(more instanceof HTMLButtonElement)) throw new Error('More missing');
  more.click();
  const remember = document.querySelector('[data-act="propose_preference"]');
  if (!(remember instanceof HTMLButtonElement)) throw new Error('Remember missing');
  remember.click();
});

afterEach(() => vi.unstubAllGlobals());

function submit(): void {
  const form = document.querySelector('form');
  if (!(form instanceof HTMLFormElement)) throw new Error('Form missing');
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
}

it('offers warm tone and defaults to all local chats/projects with source session authority', async () => {
  expect(select('Where to remember').value).toBe('user');
  expect(select('Where to remember').textContent).toContain('All chats and projects');
  select('Preference').value = 'tone:warm';
  submit();
  await vi.waitFor(() => expect(actions).toHaveLength(1));
  expect(actions[0]).toMatchObject({
    action: 'propose_preference',
    scope: 'user',
    session_id: 'old-session',
    conflict_key: 'tone',
    value_key: 'warm',
  });
});

it('submits a generic grounded project goal and the exact earlier reply binding', async () => {
  const preference = select('Preference');
  preference.value = 'project_goal';
  preference.dispatchEvent(new Event('change'));
  expect(select('Where to remember').value).toBe('project');
  expect(select('Where to remember').disabled).toBe(true);
  const replies = select('Earlier exchange this updates');
  expect(replies.hidden).toBe(false);
  expect(Array.from(replies.options).map((option) => option.value)).toEqual(['', 'earlier']);
  replies.value = 'earlier';
  submit();
  await vi.waitFor(() => expect(actions).toHaveLength(1));
  expect(actions[0]).toMatchObject({
    action: 'propose_project_goal',
    scope: 'project',
    project: 'D:/project',
    session_id: 'old-session',
    goal: 'Prioritize accessibility.',
    goal_key: 'focus',
    reply_run_id: 'earlier',
    reply_digest: 'a'.repeat(64),
  });
});

it('does not submit stale forms after switching sessions', () => {
  activeSession = 'changed-session';
  select('Preference').value = 'tone:warm';
  submit();
  expect(actions).toEqual([]);
  expect(element('local-context-drawer').classList.contains('open')).toBe(false);
});
