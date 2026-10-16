import { readFileSync } from 'node:fs';
import { expect, it, vi } from 'vitest';
import { runtime } from './runtime';
import './render';
import './process';
import './messages';
import './conversation_ui';
import type { Session } from './types/domain';
import type { ConversationUiDeps } from './conversation_ui';

it('mounts recent complete turns first and prepends history without replacing the visible tail', () => {
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
  const session = {
    id: 'history',
    title: 'History',
    messages: Array.from({ length: 200 }, (_, index) => [
      { type: 'user', id: 'user-' + index, text: '请求 ' + index },
      { type: 'asst', text: '回答 ' + index },
    ]).flat(),
  } as Session;
  runtime.Messages.init({
    getActiveId: () => session.id,
    getRunningSessionId: () => null,
    currentProjectPath: () => '',
  } as Parameters<typeof window.Messages.init>[0]);
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
    appendMessageNode: runtime.Messages.append,
  } as unknown as ConversationUiDeps);
  runtime.ConversationUI.renderChat();
  const chat = document.getElementById('chat')!;
  const initial = chat.querySelectorAll('.msg.user').length;
  expect(initial).toBeLessThanOrEqual(50);
  const tail = chat.querySelector('[data-message-id="user-199"]');
  chat.querySelector<HTMLButtonElement>('[data-load-history]')!.click();
  expect(chat.querySelectorAll('.msg.user').length).toBeGreaterThan(initial);
  expect(chat.querySelector('[data-message-id="user-199"]')).toBe(tail);
  chat.querySelector<HTMLButtonElement>('[data-show-all-history]')!.click();
  expect(chat.querySelectorAll('.msg.user')).toHaveLength(200);
  vi.unstubAllGlobals();
});
