import { fetchChangeFile, fetchChanges, fetchRunChanges, restoreChanges } from '../api/changes';
import { Composer } from '../conversation/composer';
import { ConversationUI } from '../conversation/conversation_ui';
import { renameInputEl } from '../conversation/rename_input';
import type { ChatFork } from '../conversation/chat_fork';
import type { HistoryTask } from '../conversation/history_repository';
import { Messages } from '../conversation/messages';
import { ProviderUI } from '../providers/provider_ui';
import { Render } from '../render/render';
import { Process } from '../runs/process';
import { Requests } from '../runs/requests';
import { RunDetails } from '../runs/run_details';
import type { RunEventCoordinator } from '../runs/run_event_coordinator';
import type { createApprovalController } from '../runs/shell_approvals';
import type { UserQuestions } from '../runs/user_questions';
import type { SessionStateStore } from '../state/session_state_store';
import { UiState } from '../state/ui_state';
import { Sse } from '../sse';
import { ChangesDrawer } from '../workspace/changes_drawer';
import { LocalContextDrawer } from '../workspace/local_context_drawer';
import type { Workspace } from '../workspace/workspace';
import type { createResearchPanel } from '../research/research_panel';
import type { createProjectFolderPicker } from './project_folder';
import { mountCharacterPond } from './character_pond';
import { reportDesktopReady } from './desktop_ready';
import { OperatorAuth } from './operator_auth';

type ProviderInit = Parameters<typeof ProviderUI.init>[0];
type UiInit = Parameters<typeof ConversationUI.init>[0];
type SseInit = Parameters<typeof Sse.init>[0];
type RequestsInit = Parameters<typeof Requests.init>[0];
type ComposerInit = Parameters<typeof Composer.init>[0];
type MessagesInit = Parameters<typeof Messages.init>[0];

interface BootDependencies {
  $: <T extends HTMLElement = HTMLElement>(id: string) => T;
  workspace: Workspace;
  sessionState: SessionStateStore;
  runEvents: RunEventCoordinator;
  chatFork: ChatFork;
  userQuestions: UserQuestions;
  approvalController: ReturnType<typeof createApprovalController>;
  researchPanel: ReturnType<typeof createResearchPanel>;
  projectFolderPicker: ReturnType<typeof createProjectFolderPicker>;
  previewHtml: (sessionId: string, path: string, project: string) => void;
  currentProjectPath: UiInit['currentProjectPath'];
  currentProviderId: UiInit['currentProviderId'];
  setActiveProvider: ProviderInit['setActiveProvider'];
  activeSession: UiInit['activeSession'];
  persistActiveNow: ProviderInit['persistActiveNow'];
  setStatus: SseInit['setStatus'];
  refreshProviderStatus: SseInit['refreshProviderStatus'];
  updateSend: ProviderInit['updateSend'];
  updateComposerContext: UiInit['updateComposerContext'];
  persistActive: RequestsInit['persistActive'];
  pushMsgToSession: RequestsInit['pushMsgToSession'];
  addToSession: RequestsInit['addToSession'];
  appendMessageNode: RequestsInit['appendMessageNode'];
  renderChat: RequestsInit['renderChat'];
  scrollChat: RequestsInit['scrollChat'];
  statusRow: RequestsInit['statusRow'];
  isSessionRunning: MessagesInit['isSessionRunning'];
  sessionProject: ComposerInit['sessionProject'];
  sessionProjectPath: ComposerInit['sessionProjectPath'];
  currentIntentForSession: ComposerInit['currentIntentForSession'];
  addSendError: ComposerInit['addSendError'];
  acceptRunResponse: ComposerInit['acceptRunResponse'];
  syncProviderUI: ComposerInit['syncProviderUI'];
  switchSession: UiInit['switchSession'];
  openLocalProviderConfig: ComposerInit['openLocalProviderConfig'];
  attachCurrentChatToPickedProject: ComposerInit['attachCurrentChatToPickedProject'];
  observeQueuedInput: ComposerInit['observeQueuedInput'];
  openDocument: MessagesInit['openDocument'];
  approveCommand: MessagesInit['approveCommand'];
  continueTask: MessagesInit['continueTask'];
  resumeTeaching: MessagesInit['resumeTeaching'];
  activeProject: UiInit['activeProject'];
  syncResearchUseProjectButton: UiInit['syncResearchUseProjectButton'];
  renderSidebar: UiInit['renderSidebar'];
  closeAllMenus: UiInit['closeAllMenus'];
  closeOtherDrawers: UiInit['closeOtherDrawers'];
  commitSessionRename: UiInit['commitSessionRename'];
  resizeTask: () => void;
  syncResearchUI: () => void;
}

export async function bootApplication(deps: BootDependencies): Promise<void> {
  const {
    $,
    workspace,
    sessionState,
    runEvents,
    chatFork,
    userQuestions,
    approvalController,
    researchPanel,
    projectFolderPicker,
    previewHtml,
    currentProjectPath,
    currentProviderId,
    setActiveProvider,
    activeSession,
    persistActiveNow,
    setStatus,
    refreshProviderStatus,
    updateSend,
    updateComposerContext,
    persistActive,
    pushMsgToSession,
    addToSession,
    appendMessageNode,
    renderChat,
    scrollChat,
    statusRow,
    isSessionRunning,
    sessionProject,
    sessionProjectPath,
    currentIntentForSession,
    addSendError,
    acceptRunResponse,
    syncProviderUI,
    switchSession,
    openLocalProviderConfig,
    attachCurrentChatToPickedProject,
    observeQueuedInput,
    openDocument,
    approveCommand,
    continueTask,
    resumeTeaching,
    activeProject,
    syncResearchUseProjectButton,
    renderSidebar,
    closeAllMenus,
    closeOtherDrawers,
    commitSessionRename,
    resizeTask,
    syncResearchUI,
  } = deps;
  const runProjection = runEvents.projection;
  const { escapeHtml, copyText } = Render;

  const main = document.querySelector('main');
  if (main) mountCharacterPond(main);
  await OperatorAuth.authorize();
  await ProviderUI.adoptCatalog();
  UiState.bindPagehide(Composer.captureDraft);
  researchPanel.init();
  ChangesDrawer.init({
    workspace,
    $,
    fetchChanges,
    fetchRunChanges,
    fetchChangeFile,
    restoreChanges,
    escapeHtml,
    previewHtml: (project: string, path: string) => previewHtml(sessionState.activeId, path, project),
  });
  LocalContextDrawer.init({
    $,
    getActiveId: () => sessionState.activeId,
    currentProjectPath: () => currentProjectPath(),
    copyText,
  });
  RunDetails.init({ workspace, getActiveId: () => sessionState.activeId });
  ProviderUI.init({
    updateSend,
    $,
    escapeHtml,
    PROVIDERS: UiState.PROVIDERS,
    PROVIDER_LABELS: UiState.PROVIDER_LABELS,
    DEFAULT_PROVIDER: UiState.DEFAULT_PROVIDER,
    currentProviderId,
    setActiveProvider,
    activeSession,
    persistActiveNow,
  });
  Sse.init({
    forRun: Requests.forRun,
    setStatus,
    applyRunState: (data) => runEvents.applyRunState(data),
    handleServerEvent: (event, cursor) => runEvents.handleServerEvent(event, 'event', cursor),
    refreshProviderStatus,
    acceptRun: (runId: string | null, sessionId: string | null) => {
      if (runId && sessionId)
        runEvents.applyLifecycle({ type: 'task_start', run_id: runId, session_id: sessionId }, 'admission');
    },
    setProviderBusy: (busy: boolean) => {
      $<HTMLButtonElement>('provider-button').disabled = !!busy;
      $<HTMLButtonElement>('effort-button').disabled = !!busy;
    },
    updateSend,
    updateComposerContext,
  });
  Requests.init({
    composer: Composer,
    $,
    getSessions: () => sessionState.sessions,
    getActiveId: () => sessionState.activeId,
    getRunningSessionId: () => runProjection.primaryRun?.sessionId ?? null,
    persistActiveNow,
    persistActive,
    pushMsgToSession,
    addToSession,
    appendMessageNode,
    renderChat,
    scrollChat,
    statusRow,
  });
  Composer.init({
    chatFork,
    userQuestions,
    applyApproval: approvalController.apply,
    refreshHistory: ConversationUI.refreshHistory,
    updateNotice: ConversationUI.updateNotice,
    $,
    PROVIDERS: UiState.PROVIDERS,
    getActiveId: () => sessionState.activeId,
    getRunningSessionId: () => runProjection.primaryRun?.sessionId ?? null,
    getRunningRunId: () => runProjection.primaryRun?.runId ?? null,
    getRunningRunIdForSession: (sessionId: string) => runProjection.runIdForSession(sessionId),
    isSessionRunning,
    activeSession,
    findSession: (sessionId: string | null | undefined) =>
      sessionId ? sessionState.sessions.find((x) => x.id === sessionId) : undefined,
    sessionProject,
    sessionProjectPath,
    currentIntentForSession,
    currentProviderId,
    persistActive,
    persistActiveNow,
    updateComposerContext,
    renderChat,
    addToSession,
    addSendError,
    acceptRunResponse,
    syncProviderUI,
    switchSession,
    openLocalProviderConfig,
    projectPickerBusy: projectFolderPicker.busy,
    attachCurrentChatToPickedProject,
    observeQueuedInput,
  });
  Messages.init({
    chatFork,
    userQuestions,
    approvals: approvalController,
    composer: Composer,
    historyRevision: ConversationUI.historyRevision,
    isSessionRunning,
    openDocument,
    statusRow,
    currentProjectPath,
    attachCurrentChatToPickedProject,
    approveCommand,
    continueTask,
    resumeTeaching,
    getActiveId: () => sessionState.activeId,
    getRunningSessionId: () => runProjection.primaryRun?.sessionId ?? null,
    getSessions: () => sessionState.sessions,
  });
  Process.setLayoutListener(ConversationUI.updateLatest);
  ConversationUI.init({
    syncComposer: Composer.syncSession,
    chatFork,
    userQuestions,
    projectTasks: (tasks: HistoryTask[], sessionId: string) =>
      runEvents.projectTaskStates(tasks, new Map(runProjection.entries()), sessionId),
    activeSession,
    activeProject,
    getSessions: () => sessionState.sessions,
    getProjects: () => sessionState.projects,
    getActiveId: () => sessionState.activeId,
    getRunningSessionId: () => runProjection.primaryRun?.sessionId ?? null,
    sessionProject,
    currentProjectPath,
    currentProviderId,
    syncProviderUI,
    updateComposerContext,
    syncResearchUseProjectButton,
    appendMessageNode,
    switchSession,
    renderSidebar,
    closeAllMenus,
    closeOtherDrawers,
    renameInputEl,
    commitSessionRename,
  });
  renderSidebar();
  renderChat();
  resizeTask();
  updateSend();
  syncProviderUI(currentProviderId());
  syncResearchUseProjectButton();
  refreshProviderStatus(true);
  // Legacy form retained for callers without an immediate probe: refreshProviderStatus();
  syncResearchUI();
  setStatus('', 'ok');
  await UiState.restoreFromServer();
  await userQuestions.refresh();
  await Composer.refreshQueuedInputs();
  Composer.setReady();
  syncResearchUseProjectButton();
  runEvents.connectEvents();
  reportDesktopReady();
}
