import { expect, it, vi } from 'vitest';
import { reconcileHistory } from './conversation_history';

import { SessionStateStore } from './session_state_store';
import { SessionStateViews } from './session_state_views';
import type { Session } from './types/domain';
import { UiState } from './ui_state';
import { Process } from './process';
import './render';
import { Messages } from './messages';

it('keeps the final stream mounted when history refresh and terminal replay follow tool streams', () => {
  const chat = document.createElement('div');
  const session = { id: 'chat', title: 'Chat', messages: [] } as unknown as Session;
  const renderChat = () => {
    chat.replaceChildren();
    for (const message of session.messages) Messages.append(chat, message);
  };
  const store: SessionStateStore = new SessionStateStore({
    ui: { ...UiState, persist: vi.fn(), persistNow: vi.fn() },
    defaultProvider: () => '',
    isRenaming: () => false,
    invalidate: (change) => views.invalidate(change),
    forgetView: vi.fn(),
    forgetOnServer: async () => true,
    requestIdForRun: () => undefined,
    mountedStreamId: (sessionId, runId) => views.mountedStreamId(sessionId, runId),
    messageAdded: vi.fn(),
  });
  store.sessions = [session];
  store.activeId = session.id;
  const views: SessionStateViews = new SessionStateViews({
    state: store,
    chat: () => chat,
    messages: Messages,
    process: Process,
    renderSidebar: vi.fn(),
    renderChat,
    scrollChat: vi.fn(),
    appendMessageNode: (target, message) => Messages.append(target, message),
    refreshApprovals: () => false,
    scopeChanged: vi.fn(),
    syncProvider: vi.fn(),
    syncComposer: vi.fn(),
    updateComposerContext: vi.fn(),
    closeLocalContext: vi.fn(),
  });
  Messages.init({
    getActiveId: () => session.id,
    getRunningSessionId: () => null,
    isSessionRunning: () => false,
    currentProjectPath: () => '',
    openDocument: vi.fn(),
    attachCurrentChatToPickedProject: vi.fn(),
    approveCommand: vi.fn(),
    continueTask: vi.fn(),
    resumeTeaching: vi.fn(),
    statusRow: () => document.createElement('div'),
  });
  store.addMessage(session.id, {
    type: 'asst' as const,
    runId: 'run',
    streamId: 'tool-stream',
    streamState: 'complete',
    finalAnswer: false,
    text: '',
  });
  store.addMessage(session.id, {
    type: 'asst' as const,
    runId: 'run',
    streamId: 'answer-stream',
    streamState: 'partial',
    finalAnswer: false,
    text: 'Game created.',
  });
  const final = {
    type: 'asst' as const,
    runId: 'run',
    text: 'Game created.',
    eventKey: 'terminal:run:answer',
    finalAnswer: true,
    contentRole: 'final' as const,
    answerId: 'terminal:run:answer',
    turnId: 'turn:request',
  };
  store.addMessage(session.id, final);
  const answer = chat.querySelector('[data-stream-id="answer-stream"]');
  session.messages = reconcileHistory(session.messages, [
    {
      id: 'saved-answer',
      type: 'asst' as const,
      runId: 'run',
      text: final.text,
      finalAnswer: true,
      contentRole: 'final',
    },
  ]);
  store.addMessage(session.id, final);
  expect(chat.querySelector('[data-stream-id="tool-stream"]')?.hasAttribute('hidden')).toBe(true);
  expect(chat.querySelectorAll('.msg.asst:not([hidden])')).toHaveLength(1);
  expect(chat.querySelector('.msg.asst:not([hidden])')).toBe(answer);
  expect(chat.querySelectorAll('.msg-copy')).toHaveLength(1);
  expect(session.messages.find((message) => message.finalAnswer)?.streamId).toBe('answer-stream');
  expect(chat.querySelectorAll('.msg-fork')).toHaveLength(1);
  store.pushMessage(session.id, { type: 'user', text: 'What could be improved?' });
  renderChat();
  expect(chat.querySelectorAll('.msg.asst:not([hidden])')).toHaveLength(1);
  expect(chat.querySelectorAll('.msg-copy')).toHaveLength(2);
  expect(chat.querySelectorAll('.msg-fork')).toHaveLength(1);
});
