import { readFileSync } from 'node:fs';
import { expect, it, vi } from 'vitest';
import { runtime } from './runtime';
import './conversation_ui';
import type { ConversationUiDeps } from './conversation_ui';

it('keeps conversation following while reading the middle of command output', () => {
  document.documentElement.innerHTML = readFileSync('index.html', 'utf8');
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {} }));
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  runtime.ConversationNav = { init() {} };
  runtime.ConversationUI.init({
    getActiveId: () => 'chat',
    getSessions: () => [],
    getProjects: () => [],
  } as unknown as ConversationUiDeps);
  const output = document.createElement('pre');
  output.className = 'shell-output';
  output.dataset.scrollable = '';
  Object.defineProperties(output, { scrollHeight: { value: 300 }, clientHeight: { value: 100 } });
  output.scrollTop = 50;
  document.getElementById('chat')!.append(output);
  expect(runtime.ConversationUI.isFollowing()).toBe(true);
  output.dispatchEvent(new WheelEvent('wheel', { deltaY: -20, bubbles: true }));
  expect(runtime.ConversationUI.isFollowing()).toBe(true);
  output.scrollTop = 0;
  output.dispatchEvent(new WheelEvent('wheel', { deltaY: -20, bubbles: true }));
  expect(runtime.ConversationUI.isFollowing()).toBe(false);
  vi.unstubAllGlobals();
});
