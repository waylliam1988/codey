import type { MessagesApi } from './messages';
import type { ProcessApi } from './process';
import type { SessionStateChange, SessionStateStore } from './session_state_store';
import type { Message } from './types/domain';

interface SessionViewDeps {
  state: Pick<SessionStateStore, 'activeId' | 'activeProject' | 'currentProviderId'>;
  chat(): HTMLElement;
  messages: Pick<MessagesApi, 'updateReply'>;
  process: Pick<ProcessApi, 'replace'>;
  renderSidebar(): void;
  renderChat(): void;
  scrollChat(capture?: boolean): void;
  appendMessageNode(chat: HTMLElement, message: Message): void;
  refreshApprovals(): boolean;
  scopeChanged(): void;
  syncProvider(provider: string): void;
  syncComposer(): void;
  updateComposerContext(): void;
  closeLocalContext(): void;
}

/** Interpret state invalidations using the existing views; this owner never changes business state. */
export class SessionStateViews {
  constructor(private readonly deps: SessionViewDeps) {}

  mountedStreamId(sessionId: string, runId: string): string | undefined {
    if (sessionId !== this.deps.state.activeId) return undefined;
    return Array.from(this.deps.chat().querySelectorAll<HTMLElement>('.msg.asst[data-stream-id]')).find(
      (node) => node.dataset.runId === runId,
    )?.dataset.streamId;
  }

  invalidate(change: SessionStateChange): void {
    const d = this.deps;
    if (change.kind === 'message') {
      if (change.action === 'title') d.renderSidebar();
      if (change.sessionId !== d.state.activeId) return;
      if (change.action === 'replace' && change.message) this.replaceMessage(change.message);
      else if (change.action === 'append' && change.message) {
        const chat = d.chat(),
          before = new Set(chat.children);
        d.appendMessageNode(chat, change.message);
        const anchor =
          change.beforeRequestId &&
          Array.from(chat.querySelectorAll<HTMLElement>('.request-status')).find(
            (node) => node.dataset.requestId === change.beforeRequestId,
          );
        if (anchor)
          for (const node of Array.from(chat.children)) {
            if (!before.has(node) && node !== anchor) chat.insertBefore(node, anchor);
          }
        d.scrollChat(false);
      } else d.renderChat();
      return;
    }
    if (change.kind === 'project') {
      if (change.action === 'delete') d.scopeChanged();
      d.renderSidebar();
      if (change.action === 'delete' || d.state.activeProject()?.id === change.projectId) d.renderChat();
      return;
    }
    const active = change.sessionId === d.state.activeId;
    if (change.action === 'clear') {
      if (active) {
        d.closeLocalContext();
        d.renderChat();
      }
      return;
    }
    if (change.action === 'rename') {
      d.renderSidebar();
      if (active) d.renderChat();
      return;
    }
    if (change.action !== 'attach' || active) d.scopeChanged();
    d.renderSidebar();
    if (change.action !== 'attach' || active) d.renderChat();
    else d.updateComposerContext();
    if (change.action === 'switch') d.syncProvider(d.state.currentProviderId());
    if (change.action === 'switch' || change.action === 'create') d.syncComposer();
  }

  private replaceMessage(message: Message): void {
    const d = this.deps,
      chat = d.chat();
    if (d.messages.updateReply(chat, message)) {
      d.scrollChat(false);
      return;
    }
    if (['shell_result', 'shell_approval_status'].includes(message.type) && d.refreshApprovals()) return;
    if (d.process.replace(chat, message)) {
      d.scrollChat(false);
      return;
    }
    const existing =
      message.toolKey &&
      Array.from(chat.children).find(
        (node): node is HTMLElement => node instanceof HTMLElement && node.dataset.toolKey === message.toolKey,
      );
    if (existing) {
      const temporary = document.createElement('div');
      d.appendMessageNode(temporary, message);
      if (temporary.firstElementChild) {
        existing.replaceWith(temporary.firstElementChild);
        d.scrollChat(false);
        return;
      }
    }
    if (message.type === 'asst' && message.runId) {
      const assistant = Array.from(chat.querySelectorAll<HTMLElement>('.msg.asst')).find(
        (node) => node.dataset.runId === message.runId,
      );
      if (assistant) {
        const streamId = assistant.dataset.streamId;
        if (streamId)
          d.messages.updateReply(chat, { ...message, streamId, streamState: 'complete', finalAnswer: true });
        d.scrollChat(false);
        return;
      }
    }
    d.renderChat();
    d.scrollChat();
  }
}
