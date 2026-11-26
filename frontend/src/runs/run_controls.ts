import { requestResponse } from '../api/transport';
import { element } from '../app/element';
import type { ComposerContext } from '../app/composer_context';
import { Composer } from '../conversation/composer';
import { Messages } from '../conversation/messages';
import { statusRow } from '../conversation/status_row';
import { ProviderUI } from '../providers/provider_ui';
import { Sse } from '../sse';
import type { SessionStateStore } from '../state/session_state_store';
import { UiState } from '../state/ui_state';
import { Requests } from './requests';
import type { RunEventCoordinator } from './run_event_coordinator';
import type { createApprovalController } from './shell_approvals';
import type { UserQuestions } from './user_questions';

export class RunControls {
  constructor(
    readonly coordinator: RunEventCoordinator,
    readonly questions: UserQuestions,
    readonly approvals: ReturnType<typeof createApprovalController>,
    private readonly sessions: SessionStateStore,
    private readonly context: ComposerContext,
  ) {}

  mount(view: { render(): void; scroll(capture?: boolean): void }): void {
    Sse.init({
      forRun: Requests.forRun,
      setStatus: this.setStatus,
      applyRunState: (data) => this.coordinator.applyRunState(data),
      handleServerEvent: (event, cursor) => this.coordinator.handleServerEvent(event, 'event', cursor),
      refreshProviderStatus: ProviderUI.refreshStatus,
      acceptRun: (runId, sessionId) => {
        if (runId && sessionId)
          this.coordinator.applyLifecycle({ type: 'task_start', run_id: runId, session_id: sessionId }, 'admission');
      },
      setProviderBusy: (busy) => ProviderUI.setBusy(busy),
      updateSend: Composer.updateSend,
      updateComposerContext: this.context.update,
    });
    Requests.init({
      composer: Composer,
      $: element,
      getSessions: () => this.sessions.sessions,
      getActiveId: () => this.sessions.activeId,
      getRunningSessionId: this.runningSession,
      persistActiveNow: UiState.persistNow,
      persistActive: UiState.persist,
      pushMsgToSession: this.sessions.pushMessage.bind(this.sessions),
      addToSession: this.sessions.addMessage.bind(this.sessions),
      appendMessageNode: Messages.append,
      renderChat: view.render,
      scrollChat: view.scroll,
      statusRow,
    });
  }

  connect(): void {
    this.coordinator.connectEvents();
  }

  runningSession = (): string | null => this.coordinator.projection.primaryRun?.sessionId ?? null;

  approve = async (id: string, approved: boolean, scope = 'once'): Promise<void> => {
    await this.approvals.submit(id, approved, scope);
    void this.coordinator.reconcileRunState();
  };

  resumeTeaching = async (id: string, sessionId: string): Promise<void> => {
    if (!id) return;
    try {
      const response = await requestResponse('/api/teach/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      });
      if (response.ok)
        this.sessions.dropMessages(sessionId, (message) => message.type === 'teach' && message.id === id);
    } catch {}
  };

  sendError = (sessionId: string, eventKey = '', runId = ''): void => {
    this.sessions.addMessage(sessionId, {
      type: 'err',
      text: 'Could not send the message',
      sessionId,
      eventKey,
      runId,
    });
  };

  setStatus = (text: string, kind: string): void => {
    const status = element('status');
    status.className = `status ${kind}`;
    status.replaceChildren();
    if (!['run', 'warn', 'err'].includes(kind)) return;
    const marker = document.createElement('span');
    marker.className = kind === 'err' ? 'status-dot err' : 'spinner';
    status.append(marker);
    if (text) {
      const label = document.createElement('span');
      label.className = 'status-text';
      label.textContent = text;
      status.append(label);
    }
  };
}
