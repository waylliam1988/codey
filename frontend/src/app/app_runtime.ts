import { createContextMenus } from '../conversation/context_menus';
import { renameInputEl } from '../conversation/rename_input';
import { statusRow } from '../conversation/status_row';
import { createWorkspaceDocuments } from '../workspace/workspace_documents';
import { createProjectFolderPicker } from './project_folder';
import { requestResponse } from '../api/transport';
import { ChangesDrawer } from '../workspace/changes_drawer';
import { Composer } from '../conversation/composer';
import { ConversationUI } from '../conversation/conversation_ui';
import { LocalContextDrawer } from '../workspace/local_context_drawer';
import { Messages } from '../conversation/messages';
import { OperatorAuth } from './operator_auth';
import { Process } from '../runs/process';
import { ProviderUI } from '../providers/provider_ui';
import { Render } from '../render/render';
import { Requests } from '../runs/requests';
import { ResearchDrawer } from '../research/research_drawer';
import { createResearchPanel } from '../research/research_panel';
import { ResearchRuns } from '../research/research_runs';
import { RunDetails } from '../runs/run_details';
import { Sse } from '../sse';
import { UiState } from '../state/ui_state';
import { ConversationNav } from '../conversation/conversation_nav';
import { ModelSettings } from '../providers/model_settings';
import { Models } from '../providers/models';
import { ResearchGraph } from '../research/research_graph';
import { Settings } from '../providers/settings';
import { RunEventCoordinator } from '../runs/run_event_coordinator';
import { SessionStateStore } from '../state/session_state_store';
import { forgetSessionOnServer } from '../state/session_deletion';
import { SessionStateViews } from '../state/session_state_views';
import { ChatFork } from '../conversation/chat_fork';
import { HistoryPanel } from '../workspace/history_panel';
import { RecordedPanels } from '../workspace/recorded_panels';
import { Workspace } from '../workspace/workspace';
import '../conversation/chat_fork.css';
import '../workspace/workspace.css';
import { reportDesktopFailure } from './desktop_ready';
import { createApprovalController } from '../runs/shell_approvals';
import { createSidebar } from '../conversation/sidebar';
import { UserQuestions } from '../runs/user_questions';
import { bootApplication } from './application_boot';
import { ComposerApplication } from './composer_application';
import { ComposerContext } from './composer_context';
import { ConversationApplication } from '../conversation/conversation_application';
import { ProviderApplication } from '../providers/provider_application';
import { RunControls } from '../runs/run_controls';
import { RunPresentation } from '../runs/run_presentation';
import { SessionApplication } from './session_application';
import { mountApplicationShortcuts } from './application_shortcuts';
import './redesign.css';
import { decodeServerEvent } from '../domain/protocol';

export function mountApp(): void {
  // ============================ state & storage ============================
  type AppElement = HTMLElement & { disabled: boolean };
  const $ = <T extends HTMLElement = AppElement>(id: string): T => document.getElementById(id) as T;
  const workspace = new Workspace(['changes', 'files', 'research']);
  const { documentPane, openDocument, previewHtml } = createWorkspaceDocuments(workspace, () => sessionState.activeId);
  workspace.register('changes', 'Changes', $('changes-drawer'), (target) => {
    const project = sessionState.currentProjectPath();
    if (project && target.runId) void ChangesDrawer.openRun(project, sessionState.activeId, target.runId, target.path);
    else if (project) void ChangesDrawer.open(project, target.path || '');
    else {
      $('changes-body').textContent = 'Choose a project to inspect workspace changes.';
      $('changes-subtitle').textContent = 'No project';
      UiState.setDrawerOpen('changes-drawer', true);
    }
  });
  workspace.register('research', 'Research', $('research-drawer'), (target) => {
    const runId = target.runId || researchPanel.currentResearchRun(sessionState.activeId)?.runId;
    if (runId) ResearchDrawer.open(sessionState.activeId, runId);
    else {
      $('research-body').textContent = 'No research recorded for this conversation.';
      $('research-subtitle').textContent = 'No research yet';
      UiState.setDrawerOpen('research-drawer', true);
    }
  });
  const recordedPanels = new RecordedPanels({
    sessionId: () => sessionState.activeId,
    messages: () => activeSession()?.messages || [],
    openDocument,
    copy: (text) => Render.copyText(text),
  });
  workspace.register('review', 'Review', recordedPanels.review, (target) => {
    void recordedPanels.showReview(target);
  });
  workspace.register('outputs', 'Output', recordedPanels.outputs, () => recordedPanels.showOutputs());
  function adoptChatFork(
    result: { session_id: string; state: Record<string, unknown> },
    baseline: Record<string, unknown>,
    followIntent = true,
  ): void {
    Composer.captureDraft();
    const current = UiState.current();
    const follow = followIntent && current.active_id === baseline.active_id;
    UiState.accept(result.state);
    if (follow) switchSession(result.session_id);
    else {
      view.renderSidebar();
      view.render();
      persistActive();
    }
  }
  const chatFork = new ChatFork({
    activeId: () => sessionState.activeId,
    isWorktree: (id) =>
      !!sessionState.sessions.find((session) => session.id === id)?.projectId?.startsWith('worktree_fork_'),
    workspaceChanged: () => Composer.syncSession(),
    originRendered: () => {
      if (ConversationUI.isFollowing()) ConversationUI.scrollChat();
      ConversationUI.captureView();
    },
    saved: () => UiState.ensureSaved(),
    state: () => UiState.current(),
    adopt: adoptChatFork,
    switchSession: (id) => sessionState.switchSession(id),
  });
  const historyPanel = new HistoryPanel({
    session: () => sessionState.activeSession(),
    switchSession: (id) => sessionState.switchSession(id),
    changed: (id) => ConversationUI.refreshHistory(id),
    adoptFork: adoptChatFork,
  });
  workspace.register('history', 'History', historyPanel.element, (target) => {
    void historyPanel.show(target);
  });
  workspace.register('context', 'Local context', $('local-context-drawer'), () => LocalContextDrawer.open());
  document.body.append(workspace.element);
  document.querySelector('.topbar-actions')?.append(workspace.toggleButton);
  const userQuestions = new UserQuestions({
    instanceId: (() => {
      try {
        const key = 'codey:instance-id';
        const existing = window.localStorage.getItem(key);
        if (existing) return existing;
        const value = crypto.randomUUID();
        window.localStorage.setItem(key, value);
        return value;
      } catch {
        return 'origin-memory';
      }
    })(),
    observe: (question) => runEvents.observeUserQuestion(question),
    changed: () => {
      Composer.updateSend();
      view?.updateActivity();
    },
  });
  // Escape and receipt helpers are provided by the render module.
  const receiptSummary = Render.receiptSummary;
  const receiptChangedCount = Render.receiptChangedCount;
  const PROVIDERS = UiState.PROVIDERS;
  const sessionState: SessionStateStore = new SessionStateStore({
    ui: UiState,
    defaultProvider: () => UiState.DEFAULT_PROVIDER,
    isRenaming: (id) => sidebar.isEditingSession(id) || ConversationUI.isRenamingSession(id),
    invalidate: (change) => sessionViews.invalidate(change),
    forgetView: (id) => ConversationUI.forget(id),
    forgetOnServer: forgetSessionState,
    requestIdForRun: (sessionId, runId) => Requests.forRun(sessionId, runId)?.request.id,
    mountedStreamId: (sessionId, runId) => sessionViews.mountedStreamId(sessionId, runId),
    messageAdded: () => {
      Requests.refreshActions();
      ConversationUI.updateNotice();
    },
  });
  let view: ConversationApplication;
  const activeSession = sessionState.activeSession.bind(sessionState);
  const activeProject = sessionState.activeProject.bind(sessionState);
  const projectById = sessionState.projectById.bind(sessionState);
  const sessionProject = sessionState.sessionProject.bind(sessionState);
  const currentProjectPath = sessionState.currentProjectPath.bind(sessionState);
  const switchSession = sessionState.switchSession.bind(sessionState);
  const openProject = sessionState.openProject.bind(sessionState);
  const newSession = sessionState.newSession.bind(sessionState);
  const deleteSession = sessionState.deleteSession.bind(sessionState);
  const clearMessages = sessionState.clearMessages.bind(sessionState);
  const deleteProject = sessionState.deleteProject.bind(sessionState);
  const addToSession = sessionState.addMessage.bind(sessionState);
  const pushMsgToSession = sessionState.pushMessage.bind(sessionState);
  const replaceSessionMessage = sessionState.replaceMessage.bind(sessionState);
  const persistActive = UiState.persist;
  const persistActiveNow = UiState.persistNow;
  const sessionViews: SessionStateViews = new SessionStateViews({
    state: sessionState,
    chat: () => $('chat'),
    messages: Messages,
    process: Process,
    renderSidebar: () => view.renderSidebar(),
    renderChat: () => view.render(),
    scrollChat: (capture) => view.scroll(capture),
    appendMessageNode: Messages.append,
    refreshApprovals: () => ConversationUI.refreshApprovals(),
    scopeChanged: () => view.scopeChanged(),
    syncProvider: ProviderUI.sync,
    syncComposer: () => {
      Composer.syncSession();
      Composer.updateSend();
    },
    updateComposerContext: () => context.update(),
    closeLocalContext: LocalContextDrawer.close,
  });

  // ============================ inline rename & confirm ============================
  const { openProjectMenu, openSessionMenu, closeAllMenus } = createContextMenus({
    projectMenu: $('proj-menu'),
    sessionMenu: $('sess-menu'),
    deleteProject,
    deleteSession,
    newSession,
    renameProject: (id) => sidebar.beginProjectRename(id),
    renameSession: (id) => sidebar.beginSessionRename(id),
    menuOpened: ConversationUI.menuOpened,
    menusClosed: ConversationUI.menusClosed,
  });
  const sidebar = createSidebar({
    read: () => ({ sessions: sessionState.sessions, projects: sessionState.projects, activeId: sessionState.activeId }),
    switchSession,
    openProject,
    newSession: (projectId) => newSession(projectId),
    setProjectExpanded: (id, expanded) => {
      const project = projectById(id);
      if (!project) return;
      project.expanded = expanded;
      persistActive();
    },
    renameSession: sessionState.renameSession.bind(sessionState),
    renameProject: sessionState.renameProject.bind(sessionState),
    openSessionMenu,
    openProjectMenu,
    sessionProject,
    renameInputEl,
    search: {
      update: () => ConversationUI.updateSearch(),
      isSearching: () => ConversationUI.isSearching(),
      matchesSession: (session) => ConversationUI.matchesSession(session),
      matchesProject: (project) => ConversationUI.matchesProject(project),
      appendGroupLabel: (parent, label, hasMatches) => ConversationUI.appendGroupLabel(parent, label, hasMatches),
    },
  });

  // Markdown, copy, and tool-line helpers live in the render module.
  const copyText = Render.copyText;
  const hydrateSessionIndexes = UiState.hydrateSessionIndexes;

  async function forgetSessionState(id: string, preserveUi = false): Promise<boolean> {
    return forgetSessionOnServer(
      id,
      preserveUi,
      (state) => UiState.accept(state),
      (error) => setStatus(error, 'err'),
    );
  }
  const projectFolderPicker = createProjectFolderPicker({
    onBusy: (busy) => {
      $('btn-add-project').disabled = busy;
      context.update();
    },
    onError: (message) => setStatus(message, 'err'),
    initialPath: currentProjectPath,
    activeSessionId: () => sessionState.activeId,
    canAttach: (id) => {
      const session = sessionState.sessions.find((item) => item.id === id);
      return session !== undefined && !sessionProject(session);
    },
    hasRunningTask: () => runProjection.size > 0,
    ensureProject: (path, name) => sessionState.ensureProject(path, name),
    attachSession: (projectId, sessionId) => sessionState.attachSessionToProject(projectId, sessionId),
    newSession,
  });
  // ============================ SSE event ingestion ============================
  const researchPanel = createResearchPanel({
    workspace,
    element: $,
    activeId: () => sessionState.activeId,
    sessions: () => sessionState.sessions,
    receiptSummary,
    persistActive,
    persistActiveNow,
    openDocument,
    copyText,
    addToSession,
  });
  const runEvents: RunEventCoordinator = new RunEventCoordinator({
    state: sessionState,
    ui: UiState,
    history: ConversationUI,
    requests: Requests,
    composer: Composer,
    approvals: () => approvalController,
    questions: userQuestions,
    transport: Sse,
    providers: PROVIDERS,
    revealApproval: (sessionId, id) => {
      if (sessionId !== sessionState.activeId) return;
      requestAnimationFrame(() => {
        if (sessionId === sessionState.activeId && ConversationUI.isFollowing()) ConversationUI.revealApproval(id);
      });
    },
    setStatus: (text, kind) => runControls.setStatus(text, kind),
    updateSend: () => {
      Composer.updateSend();
      view.updateActivity();
    },
    updateComposerContext: () => context.update(),
    syncResearchUseProjectButton: () => context.updateProjectAction(),
    refreshProviderStatus: ProviderUI.refreshStatus,
    applyProviderStatus: ProviderUI.applyStatus,
    recordResearchRun: researchPanel.recordResearchRun,
    markResearchRestoreAvailability: researchPanel.markResearchRestoreAvailability,
    receiptSummary,
    receiptChangedCount,
    maybeAddChangesSummary: (sessionId, eventKey, runId) => activity.collectChanges(sessionId, eventKey, runId),
    settled: (sessionId, runId) => activity.settled(sessionId, runId),
    previewHtml,
  });
  const runProjection = runEvents.projection;

  const context = new ComposerContext(sessionState, runProjection, projectFolderPicker);
  const activity = new RunPresentation(sessionState, runProjection, userQuestions);

  const approvalController = createApprovalController({
    sessions: () => sessionState.sessions,
    changed: (sid) => {
      const session = sessionState.sessions.find((s) => s.id === sid);
      if (session) hydrateSessionIndexes(session);
      persistActive();
      if (sid === sessionState.activeId && !ConversationUI.refreshApprovals()) view.render();
      view.updateActivity();
    },
    fetch: (...args) => requestResponse(...args),
    focus: (id) => ConversationUI.revealApproval(id),
  });

  const runControls = new RunControls(runEvents, userQuestions, approvalController, sessionState, context);
  const setStatus = runControls.setStatus;
  view = new ConversationApplication({
    sessions: sessionState,
    sidebar,
    workspace,
    documents: documentPane,
    context,
    runs: runControls,
    activity,
    folder: projectFolderPicker,
    research: researchPanel,
    fork: chatFork,
    closeMenus: closeAllMenus,
    openDocument,
    previewHtml,
  });
  const sessions = new SessionApplication(sessionState, workspace, view, context);
  const provider = new ProviderApplication(sessionState);
  const composer = new ComposerApplication(sessionState, runControls, context, projectFolderPicker, chatFork);
  sessions.mount();
  mountApplicationShortcuts(sessionState, workspace, projectFolderPicker, {
    closeMenus: closeAllMenus,
    closeDrawers: view.closeDrawers,
  });

  // Transitional test bridge is opt-in so the shipped page does not expose
  // mutable application state or internal handlers by default.
  if (window.__TEST_BRIDGE__)
    Object.assign(window, {
      ChangesDrawer,
      Composer,
      ConversationNav,
      ConversationUI,
      LocalContextDrawer,
      Messages,
      ModelSettings,
      Models,
      OperatorAuth,
      Process,
      ProviderUI,
      Render,
      Requests,
      ResearchDrawer,
      ResearchGraph,
      ResearchRuns,
      RunDetails,
      Settings,
      Sse,
      UiState,
      Workspace: workspace,
      ChatFork: chatFork,
      UserQuestions: userQuestions,
      ShellApprovals: approvalController,
      activeSession,
      projectById,
      activeProject,
      currentProjectPath,
      renderSidebar: view.renderSidebar,
      renderChat: view.render,
      switchSession,
      openProject,
      newSession,
      setActiveProvider: Composer.setActiveProvider,
      continueTask: Composer.continueTask,
      addToSession,
      openChangesDrawer: ChangesDrawer.open,
      applyRunState: runEvents.applyRunState.bind(runEvents),
      clearMessages,
      deleteSession,
      appendMessageNode: Messages.append,
      replaceSessionMessage,
      handleServerEvent: (raw: unknown) => runEvents.handleServerEvent(decodeServerEvent(raw)),
      statusRow,
      pushMsgToSession,
      setStatus,
      updateSend: Composer.updateSend,
      updateComposerContext: context.update,
    });
  if (window.__TEST_BRIDGE__)
    Object.defineProperties(window, {
      sessions: { configurable: true, get: () => sessionState.sessions },
      projects: { configurable: true, get: () => sessionState.projects },
      activeId: { configurable: true, get: () => sessionState.activeId },
      runningSessionId: {
        configurable: true,
        get: () => runProjection.primaryRun?.sessionId ?? null,
      },
      runningRunId: {
        configurable: true,
        get: () => runProjection.primaryRun?.runId ?? null,
      },
    });
  bootApplication({ sessions, provider, runs: runControls, composer, view }).catch((error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    setStatus(message, 'warn');
    reportDesktopFailure(message);
  });
}
