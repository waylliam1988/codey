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
      { action: 'accept', session_id: 's', project: 'D:/project', proposal_id: 'p', version: 'exact-content-version' },
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
  await vi.waitFor(() => expect(actions).toEqual([{ action: 'delete_scope', session_id: 's', project: 'D:/project' }]));
});
it('submits a proposal that still requires a separate confirmation', async () => {
  const text = document.querySelector('textarea');
  if (!text) throw new Error('Missing text');
  text.value = 'Prefer concise replies';
  text.dispatchEvent(new Event('input'));
  click('Submit for review');
  await vi.waitFor(() =>
    expect(actions).toEqual([
      { action: 'propose', session_id: 's', project: 'D:/project', kind: 'preference', text: 'Prefer concise replies' },
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
