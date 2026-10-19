import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { runtime } from './runtime';
import './ui_state';
import './process';
import './conversation_ui';
import type { ConversationUiDeps } from './conversation_ui';
import type { Session } from './types/domain';

let session: Session;
let id = 0;
function response(items: object[], more = false) {
  return {
    ok: true,
    json: async () => ({
      schema_version: 1,
      session_id: session.id,
      epoch: 'db',
      snapshot_revision: 1,
      items,
      has_more: more,
      before_cursor: more ? 'older' : null,
    }),
  };
}
beforeEach(() => {
  document.documentElement.innerHTML = readFileSync('index.html', 'utf8');
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {} }));
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  runtime.Composer = { syncSession() {} };
  runtime.ConversationNav = { init() {} };
  session = { id: `durable-${++id}`, title: 'History', messages: [] } as unknown as Session;
  runtime.ConversationUI.init({
    activeSession: () => session,
    activeProject: () => null,
    getSessions: () => [session],
    getProjects: () => [],
    getActiveId: () => session.id,
    getRunningSessionId: () => null,
    currentProviderId: () => 'local',
    syncProviderUI() {},
    updateComposerContext() {},
    syncResearchUseProjectButton() {},
    appendMessageNode(chat: HTMLElement, message: { text?: string }) {
      const node = document.createElement('div');
      node.textContent = message.text || '';
      chat.append(node);
    },
  } as unknown as ConversationUiDeps);
});
afterEach(() => {
  runtime.ConversationUI.forget(session.id);
  vi.unstubAllGlobals();
});

it('loads authoritative history after restart even when the UI has no messages', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(response([{ id: 'entry', entry_id: 'entry', type: 'user', text: 'restored' }])),
  );
  runtime.ConversationUI.renderChat();
  await vi.waitFor(() => expect(document.getElementById('chat')!.textContent).toContain('restored'));
  expect(session.messages).toHaveLength(1);
});

it('replaces a branched path even when the new path has the same length', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(response([{ id: 'old', entry_id: 'old', type: 'user', text: 'old branch' }]))
    .mockResolvedValueOnce(response([{ id: 'new', entry_id: 'new', type: 'user', text: 'new branch' }]));
  vi.stubGlobal('fetch', fetcher);
  runtime.ConversationUI.renderChat();
  await vi.waitFor(() => expect(session.messages[0]?.id).toBe('old'));
  await runtime.ConversationUI.refreshHistory(session.id);
  expect(session.messages.map((item) => item.id)).toEqual(['new']);
  expect(document.getElementById('chat')!.textContent).toContain('new branch');
});

it('can load an earlier durable page when the mounted page starts at zero', async () => {
  const tail = Array.from({ length: 100 }, (_, i) => ({ id: `tail-${i}`, type: 'user', text: `tail ${i}` }));
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce(response(tail, true))
      .mockResolvedValueOnce(response([{ id: 'ancestor', entry_id: 'ancestor', type: 'user', text: 'ancestor' }])),
  );
  runtime.ConversationUI.renderChat();
  await vi.waitFor(() => expect(session.messages).toHaveLength(100));
  const button = document.querySelector<HTMLButtonElement>('[data-load-history]');
  expect(button).not.toBeNull();
  button!.click();
  await vi.waitFor(() => expect(session.messages).toHaveLength(101));
  expect(document.getElementById('chat')!.textContent).toContain('ancestor');
});

it('offers a visible retry when durable history cannot be loaded', async () => {
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(response([{ id: 'entry', entry_id: 'entry', type: 'user', text: 'restored' }]));
  vi.stubGlobal('fetch', fetcher);
  runtime.ConversationUI.renderChat();
  await vi.waitFor(() => expect(document.querySelector('[data-history-retry]')).not.toBeNull());
  document.querySelector<HTMLButtonElement>('[data-history-retry]')!.click();
  await vi.waitFor(() => expect(session.messages).toHaveLength(1));
});

it('renders history retry as a quiet text action', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
  runtime.ConversationUI.renderChat();
  await vi.waitFor(() => expect(document.querySelector('[data-history-retry]')).not.toBeNull());
  expect(document.querySelector<HTMLButtonElement>('[data-history-retry]')?.className).toBe('link-btn');
});

it('refreshes from a replayed durable commit without an active run', async () => {
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response([{ id: 'answer', entry_id: 'answer', type: 'asst', text: 'committed answer' }])),
  );
  await runtime.ConversationUI.refreshHistory(session.id);
  await runtime.ConversationUI.observeCommit({
    type: 'durable_commit',
    session_id: session.id,
    kind: 'mutation',
    commit_seq: 7,
    entry_ids: ['answer'],
  });
  expect(document.getElementById('chat')!.textContent).toContain('committed answer');
});
