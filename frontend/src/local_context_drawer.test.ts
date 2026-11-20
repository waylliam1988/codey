// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { runtime } from './runtime';
import type { LocalContextDeps } from './local_context_drawer';
import './local_context_drawer';

interface Drawer {
  init(deps: LocalContextDeps): void;
  open(): void;
  handleScopeChanged(): void;
}
function isDrawer(value: unknown): value is Drawer {
  return (
    typeof value === 'object' &&
    value !== null &&
    'init' in value &&
    typeof value.init === 'function' &&
    'open' in value &&
    typeof value.open === 'function' &&
    'handleScopeChanged' in value &&
    typeof value.handleScopeChanged === 'function'
  );
}
function drawer(): Drawer {
  const value: unknown = runtime.LocalContextDrawer;
  if (isDrawer(value)) return value;
  throw new Error('Drawer unavailable');
}
function element(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) throw new Error(id);
  return node;
}
function click(label: string): void {
  const node = Array.from(document.querySelectorAll('button')).find((button) => button.textContent === label);
  if (!node) throw new Error(label);
  node.click();
}
const actions: unknown[] = [];
let session = 's';
let failure = false;
let enabled = true;
const memory = {
  item_id: 'p',
  kind: 'preference',
  text: '<script>concise</script>',
  scope: 'project:1',
  source_refs: ['record:s:1'],
  slot: '',
  context: '',
  expires_at: '',
};
beforeEach(async () => {
  actions.length = 0;
  session = 's';
  failure = false;
  enabled = true;
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
      return Response.json(failure ? { ok: false, error: 'storage unavailable' } : { ok: true }, {
        status: failure ? 503 : 200,
      });
    }
    return Response.json({
      ok: true,
      enabled,
      scope: { session_id: 's', project: 'D:/project' },
      items: [memory],
      pending: [{ proposal_id: 'p', version: 'exact-content-version', item: memory }],
      controls: { accept_events: true, retrieve_projections: true, run_maintenance: true, admit_hints: true },
      records: [{ item: memory, state: 'active', superseded_by: '' }],
      exposures: [],
    });
  });
  drawer().init({
    $: element,
    getActiveId: () => session,
    currentProjectPath: () => 'D:/project',
    copyText: async () => true,
  });
  drawer().open();
  await vi.waitFor(() => expect(document.querySelectorAll('.local-context-row')).toHaveLength(2));
});
afterEach(() => vi.unstubAllGlobals());

it('renders memory as text and shows provenance', () => {
  expect(document.querySelector('script')).toBeNull();
  expect(element('local-context-body').textContent).toContain(memory.text);
  expect(element('local-context-body').textContent).toContain('record:s:1');
});
it('confirms the exact displayed proposal and version in the loaded scope', async () => {
  click('Confirm');
  await vi.waitFor(() =>
    expect(actions).toEqual([
      expect.objectContaining({
        action: 'accept',
        session_id: 's',
        project: 'D:/project',
        proposal_id: 'p',
        version: 'exact-content-version',
      }),
    ]),
  );
});
it('rejects without silently accepting the candidate', async () => {
  click('Reject');
  await vi.waitFor(() =>
    expect(actions).toEqual([expect.objectContaining({ action: 'reject', version: 'exact-content-version' })]),
  );
});
it('requires an explicit second action to delete the scope', async () => {
  click('Clear local context…');
  expect(actions).toEqual([]);
  click('Clear local context');
  await vi.waitFor(() =>
    expect(actions).toEqual([
      expect.objectContaining({ action: 'delete_scope', session_id: 's', project: 'D:/project' }),
    ]),
  );
});
it('submits a proposal that still requires a separate confirmation', async () => {
  const text = document.querySelector('textarea');
  if (!text) throw new Error('Missing text');
  text.value = 'Prefer concise replies';
  text.dispatchEvent(new Event('input'));
  click('Submit for review');
  await vi.waitFor(() =>
    expect(actions).toEqual([
      expect.objectContaining({
        action: 'propose',
        session_id: 's',
        project: 'D:/project',
        kind: 'preference',
        text: 'Prefer concise replies',
      }),
    ]),
  );
});
it('closes without mutation when the active scope changes', () => {
  session = 'other';
  click('Confirm');
  expect(actions).toEqual([]);
  expect(element('local-context-drawer').classList.contains('open')).toBe(false);
});
it('keeps storage failures visible', async () => {
  failure = true;
  click('Confirm');
  await vi.waitFor(() => expect(element('local-context-subtitle').textContent).toBe('storage unavailable'));
});
it('hides mutation forms when memory is disabled', async () => {
  enabled = false;
  element('local-context-refresh').click();
  await vi.waitFor(() => expect(element('local-context-subtitle').textContent).toBe('Local context disabled'));
  expect(document.querySelector('form')).toBeNull();
});

it('changes one memory control without changing the other switches', async () => {
  const label = Array.from(document.querySelectorAll('label')).find(
    (node) => node.textContent === 'Use context in answers',
  );
  const input = label?.querySelector('input');
  if (!input) throw new Error('Missing admission control');
  input.click();
  await vi.waitFor(() =>
    expect(actions).toEqual([expect.objectContaining({ action: 'control', control: 'admit_hints', enabled: false })]),
  );
});

it('reuses a command identity after an unconfirmed storage failure', async () => {
  failure = true;
  click('Confirm');
  await vi.waitFor(() => expect(actions).toHaveLength(1));
  failure = false;
  click('Confirm');
  await vi.waitFor(() => expect(actions).toHaveLength(2));
  expect(actions[0]).toEqual(actions[1]);
  expect(actions[0]).toEqual(expect.objectContaining({ request_id: expect.any(String) }));
});

it('submits conditioned expiring corrections for a separate confirmation', async () => {
  const text = document.querySelector('textarea');
  const context = document.querySelector<HTMLSelectElement>('[aria-label="Applies during"]');
  const expiry = document.querySelector<HTMLInputElement>('[aria-label="Expires at"]');
  const replaces = document.querySelector<HTMLSelectElement>('[aria-label="Replaces item"]');
  if (!text || !context || !expiry || !replaces) throw new Error('Missing lifecycle fields');
  text.value = 'Use concrete examples during research';
  text.dispatchEvent(new Event('input'));
  context.value = 'research';
  expiry.value = '2099-01-01T00:00';
  replaces.value = memory.item_id;
  click('Submit for review');
  await vi.waitFor(() =>
    expect(actions).toEqual([
      expect.objectContaining({
        action: 'propose',
        context: 'research',
        supersedes: [memory.item_id],
        expires_at: new Date(expiry.value).toISOString(),
      }),
    ]),
  );
});

it('supports user-wide proposals through an explicit scope selection', async () => {
  const selector = document.querySelector<HTMLSelectElement>('[aria-label="Memory scope"]');
  if (!selector) throw new Error('Missing scope selector');
  selector.value = 'user';
  selector.dispatchEvent(new Event('change'));
  await vi.waitFor(() =>
    expect(document.querySelector<HTMLSelectElement>('[aria-label="Memory scope"]')?.value).toBe('user'),
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  const text = document.querySelector('textarea');
  if (!text) throw new Error('Missing proposal');
  text.value = 'Use concise examples';
  text.dispatchEvent(new Event('input'));
  click('Submit for review');
  await vi.waitFor(() => expect(actions).toEqual([expect.objectContaining({ action: 'propose', scope_kind: 'user' })]));
});
