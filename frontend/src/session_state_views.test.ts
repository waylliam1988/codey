import { expect, it, vi } from 'vitest';
import { SessionStateViews } from './session_state_views';
import type { Message } from './types/domain';

function setup() {
  const chat = document.createElement('div');
  const renderChat = vi.fn(),
    append = vi.fn((target: HTMLElement, message: Message) => {
      const node = document.createElement('div');
      node.textContent = message.text || '';
      target.append(node);
    });
  const views = new SessionStateViews({
    state: { activeId: 'a', activeProject: () => null, currentProviderId: () => '' },
    chat: () => chat,
    messages: { updateReply: () => false },
    process: { replace: () => false },
    renderSidebar: vi.fn(),
    renderChat,
    scrollChat: vi.fn(),
    appendMessageNode: append,
    refreshApprovals: () => false,
    scopeChanged: vi.fn(),
    syncProvider: vi.fn(),
    syncComposer: vi.fn(),
    updateComposerContext: vi.fn(),
    closeLocalContext: vi.fn(),
  });
  return { views, chat, renderChat, append };
}

it('does not render the active transcript for an inactive session message', () => {
  const { views, renderChat, append } = setup();
  views.invalidate({
    kind: 'message',
    action: 'append',
    sessionId: 'b',
    message: { type: 'info', text: 'background' },
  });
  expect(renderChat).not.toHaveBeenCalled();
  expect(append).not.toHaveBeenCalled();
});

it('keeps newly mounted run content before its existing request status node', () => {
  const { views, chat } = setup();
  const status = document.createElement('div');
  status.className = 'request-status';
  status.dataset.requestId = 'request';
  chat.append(status);
  views.invalidate({
    kind: 'message',
    action: 'append',
    sessionId: 'a',
    message: { type: 'info', text: 'content' },
    beforeRequestId: 'request',
  });
  expect(chat.firstElementChild?.textContent).toBe('content');
  expect(chat.lastElementChild).toBe(status);
});
