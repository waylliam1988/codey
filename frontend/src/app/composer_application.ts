import type { ChatFork } from '../conversation/chat_fork';
import { Composer } from '../conversation/composer';
import { ConversationUI } from '../conversation/conversation_ui';
import { ProviderUI } from '../providers/provider_ui';
import type { RunControls } from '../runs/run_controls';
import type { SessionStateStore } from '../state/session_state_store';
import { UiState } from '../state/ui_state';
import type { ComposerContext } from './composer_context';
import { element } from './element';
import type { createProjectFolderPicker } from './project_folder';

export class ComposerApplication {
  constructor(
    private readonly sessions: SessionStateStore,
    private readonly runs: RunControls,
    private readonly context: ComposerContext,
    private readonly folder: ReturnType<typeof createProjectFolderPicker>,
    private readonly fork: ChatFork,
  ) {}

  mount(view: { render(): void }): void {
    const { projection } = this.runs.coordinator;
    UiState.bindPagehide(Composer.captureDraft);
    Composer.init({
      $: element,
      PROVIDERS: UiState.PROVIDERS,
      chatFork: this.fork,
      userQuestions: this.runs.questions,
      applyApproval: this.runs.approvals.apply,
      refreshHistory: ConversationUI.refreshHistory,
      updateNotice: ConversationUI.updateNotice,
      getActiveId: () => this.sessions.activeId,
      getRunningSessionId: this.runs.runningSession,
      getRunningRunId: () => projection.primaryRun?.runId ?? null,
      getRunningRunIdForSession: projection.runIdForSession.bind(projection),
      isSessionRunning: projection.isSessionRunning.bind(projection),
      activeSession: this.sessions.activeSession.bind(this.sessions),
      findSession: (id) => this.sessions.sessions.find((session) => session.id === id),
      sessionProject: this.sessions.sessionProject.bind(this.sessions),
      sessionProjectPath: this.sessions.projectPathFor.bind(this.sessions),
      currentIntentForSession: this.context.intent,
      currentProviderId: this.sessions.currentProviderId.bind(this.sessions),
      persistActive: UiState.persist,
      persistActiveNow: UiState.persistNow,
      updateComposerContext: this.context.update,
      renderChat: view.render,
      addToSession: this.sessions.addMessage.bind(this.sessions),
      addSendError: this.runs.sendError,
      acceptRunResponse: this.runs.coordinator.acceptRunResponse.bind(this.runs.coordinator),
      syncProviderUI: ProviderUI.sync,
      switchSession: this.sessions.switchSession.bind(this.sessions),
      openLocalProviderConfig: ProviderUI.openLocalConfig,
      projectPickerBusy: this.folder.busy,
      attachCurrentChatToPickedProject: this.folder.attachCurrentChatToPickedProject,
      observeQueuedInput: this.runs.coordinator.observeQueuedInput.bind(this.runs.coordinator),
    });
  }

  prime(): void {
    Composer.resizeTask();
    Composer.updateSend();
    this.context.update();
    this.context.updateProjectAction();
  }

  async restoreAndEnable(): Promise<void> {
    await this.runs.questions.refresh();
    await Composer.refreshQueuedInputs();
    Composer.setReady();
    this.context.updateProjectAction();
  }
}
