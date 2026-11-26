import { createContextMenus } from '../conversation/context_menus';
import { renameInputEl } from '../conversation/rename_input';
import { statusRow } from '../conversation/status_row';
import { createWorkspaceDocuments } from '../workspace/workspace_documents';
import { fetchRunChanges } from '../api/changes';
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
import type { HistoryTask } from '../conversation/history_repository';
import { RecordedPanels } from '../workspace/recorded_panels';
import { Workspace } from '../workspace/workspace';
import '../conversation/chat_fork.css';
import { CommandPalette } from './command_palette';
import '../workspace/workspace.css';
import { showCommandPermissions } from '../runs/command_permissions';
import { reportDesktopFailure } from './desktop_ready';
import type { QueuedInput } from '../runs/input_outbox';
import type { LifecycleInput } from '../runs/run_lifecycle';
import { createApprovalController } from '../runs/shell_approvals';
import { createSidebar } from '../conversation/sidebar';
import { type UserQuestion, UserQuestions } from '../runs/user_questions';
import { bootApplication } from './application_boot';
import { decodeServerEvent, type ServerEvent } from '../domain/protocol';
import type { Message, Project, RunState, Session } from '../types/domain';

export function mountApp(): void {
  // ============================ state & storage ============================
  type AppElement = HTMLElement & { disabled: boolean };
  const $ = <T extends HTMLElement = AppElement>(id: string): T => document.getElementById(id) as T;
  const eventElement = (event: Event): Element | null => (event.target instanceof Element ? event.target : null);
  const workspace = new Workspace();
  const { documentPane, openDocument, previewHtml } = createWorkspaceDocuments(workspace, () => sessionState.activeId);
  workspace.register('changes', 'Changes', $('changes-drawer'), (target) => {
    const project = sessionProjectPath(sessionState.activeId);
    if (project) void ChangesDrawer.open(project, target.path || '');
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
      renderSidebar();
      renderChat();
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
    switchSession,
  });
  const historyPanel = new HistoryPanel({
    session: activeSession,
    switchSession,
    changed: (id) => ConversationUI.refreshHistory(id),
    adoptFork: adoptChatFork,
  });
  workspace.register('history', 'History', historyPanel.element, (target) => {
    void historyPanel.show(target);
  });
  workspace.register('context', 'Local context', $('local-context-drawer'), () => LocalContextDrawer.open());
  document.body.append(workspace.element);
  document.querySelector('.topbar-actions')?.append(workspace.toggleButton);
  const workspaceActions = [
    ['files', 'Files'],
    ['changes', 'Changes'],
    ['research', 'Research'],
    ['review', 'Recorded review'],
    ['outputs', 'Command output'],
    ['history', 'Branches and workflow'],
  ] as const;
  const commandPalette = new CommandPalette(() => [
    ...workspaceActions.map(([id, label]) => ({ id, label, run: () => workspace.select(id) })),
    { id: 'search-conversation', label: 'Search this conversation', run: () => ConversationUI.searchHistory() },
    { id: 'new-chat', label: 'New chat', run: () => $('btn-new-chat').click() },
    { id: 'settings', label: 'Settings', run: () => $('btn-settings').click() },
    {
      id: 'command-permissions',
      label: 'Manage saved permissions…',
      run: () => {
        void showCommandPermissions('', '', true);
      },
    },
  ]);
  document.body.append(commandPalette.element);
  document.addEventListener('keydown', (event) => {
    if (
      !event.isComposing &&
      (event.ctrlKey || event.metaKey) &&
      event.key.toLowerCase() === 'k' &&
      !document.querySelector('dialog[open]')
    ) {
      event.preventDefault();
      commandPalette.open();
    }
  });
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
    observe: observeUserQuestion,
    changed: () => Composer?.updateSend(),
  });
  // Escape and receipt helpers are provided by the render module.
  const escapeHtml = Render.escapeHtml;
  const receiptSummary = Render.receiptSummary;
  const receiptChangedCount = Render.receiptChangedCount;
  const PROVIDER_LABELS = UiState.PROVIDER_LABELS;
  const PROVIDERS = UiState.PROVIDERS;
  const sessionState: SessionStateStore = new SessionStateStore({
    ui: UiState,
    defaultProvider: liveDefaultProvider,
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
  const sessionViews: SessionStateViews = new SessionStateViews({
    state: sessionState,
    chat: () => $('chat'),
    messages: Messages,
    process: Process,
    renderSidebar,
    renderChat,
    scrollChat,
    appendMessageNode,
    refreshApprovals: () => ConversationUI.refreshApprovals(),
    scopeChanged: handleLocalContextScopeChanged,
    syncProvider: syncProviderUI,
    syncComposer: () => {
      Composer.syncSession();
      Composer.updateSend();
    },
    updateComposerContext,
    closeLocalContext: closeLocalContextDrawer,
  });

  function liveDefaultProvider() {
    return UiState.DEFAULT_PROVIDER;
  }

  const { apply: applyUiState, cache: cacheUiState, cached: cachedUiState } = UiState;
  const persistActive = UiState.persist;
  const persistActiveNow = UiState.persistNow;

  const bootCachedState = cachedUiState();

  UiState.init({
    workspace,
    getSessions: () => sessionState.sessions,
    setSessions: (value: Session[]) => {
      sessionState.sessions = value;
    },
    getProjects: () => sessionState.projects,
    setProjects: (value: Project[]) => {
      sessionState.projects = value;
    },
    getActiveId: () => sessionState.activeId,
    setActiveId: (value: string) => {
      sessionState.activeId = value;
    },
    getUpdatedAt: () => sessionState.updatedAt,
    setUpdatedAt: (value: number) => {
      sessionState.updatedAt = value;
    },
    getRevision: () => sessionState.revision,
    setRevision: (value: number) => {
      sessionState.revision = value;
    },
    activeSession,
    renderSidebar,
    renderChat,
    updateComposerContext,
    syncProviderUI,
    currentProviderId,
  });
  applyUiState(bootCachedState);
  cacheUiState();

  // ============================ helpers ============================
  function activeSession(): Session | undefined {
    return sessionState.activeSession();
  }
  function projectById(id: string): Project | null {
    return sessionState.projectById(id);
  }
  function sessionProject(s: Session | undefined): Project | null {
    return sessionState.sessionProject(s);
  }
  function activeProject() {
    return sessionState.activeProject();
  }
  function currentProjectPath() {
    return sessionState.currentProjectPath();
  }
  function currentProviderId() {
    return sessionState.currentProviderId();
  }
  // Provider picker UI lives in the provider_ui module.
  function syncProviderUI(providerId: string) {
    ProviderUI.sync(providerId);
  }
  function applyProviderStatus(providers: unknown, isSSE = true) {
    ProviderUI.applyStatus(providers, isSSE);
  }
  function refreshProviderStatus(immediate = false) {
    return ProviderUI.refreshStatus(immediate);
  }

  function updateComposerContext() {
    const el = $('composer-context');
    const s = activeSession();
    const p = activeProject();
    const proj = p ? p.name : 'Choose folder';
    const disabled =
      runProjection.primaryRun?.sessionId === sessionState.activeId || Composer.isSending(sessionState.activeId);
    const folderDisabled = disabled || !!p || projectFolderPicker.busy();
    const researchActive = !!(s && s.research);
    const pieces = [
      `<button class="ctx-token ctx-folder" id="ctx-folder" ${folderDisabled ? 'disabled' : ''}${p ? ` title="${escapeHtml(p.path)}"` : ''}>${escapeHtml(proj)}</button>`,
      `<span class="ctx-sep">·</span>`,
      `<button class="ctx-token ctx-research ${researchActive ? 'active' : ''}" id="ctx-research" ${disabled ? 'disabled' : ''} aria-pressed="${researchActive ? 'true' : 'false'}">Research</button>`,
    ];
    el.innerHTML = pieces.join('');
    el.title = '';
    syncResearchUI();
  }
  function syncResearchUI() {
    const btn = $('ctx-research');
    if (!btn) return;
    const s = activeSession();
    const active = !!(s && s.research);
    btn.classList.toggle('active', active);
    btn.disabled =
      runProjection.primaryRun?.sessionId === sessionState.activeId || Composer.isSending(sessionState.activeId);
    btn.setAttribute('aria-pressed', String(active));
  }
  function currentIntentForSession(sessionId: string): string {
    const s = sessionState.sessions.find((x) => x.id === sessionId);
    if (!s || !s.research) return 'auto';
    return sessionProjectPath(sessionId) ? 'hybrid' : 'research';
  }

  // ============================ inline rename & confirm ============================
  const { openProjectMenu, openSessionMenu, closeAllMenus } = createContextMenus({
    projectMenu: $('proj-menu'),
    sessionMenu: $('sess-menu'),
    deleteProject,
    deleteSession,
    newSession,
    renameProject,
    renameSession,
    menuOpened: ConversationUI.menuOpened,
    menusClosed: ConversationUI.menusClosed,
  });
  function commitSessionRename(id: string, value: string | undefined) {
    sessionState.renameSession(id, value);
  }
  function commitProjectRename(id: string, value: string | undefined) {
    sessionState.renameProject(id, value);
  }

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
    renameSession: (id, title) => commitSessionRename(id, title),
    renameProject: (id, name) => commitProjectRename(id, name),
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

  function renderSidebar(): void {
    sidebar.render();
  }
  // ============================ chat rendering ============================
  function renderChat() {
    workspace.setSession(sessionState.activeId);
    documentPane.setSession(sessionState.activeId);
    ConversationUI.renderChat();
  }

  // Markdown, copy, and tool-line helpers live in the render module.
  const messageCopyText = Render.messageCopyText;
  const copyText = Render.copyText;
  const addMessageCopyButton = Render.addMessageCopyButton;
  const renderMarkdown = Render.renderMarkdown;
  const FOLDABLE_TOOL_KINDS = Render.FOLDABLE_TOOL_KINDS;
  const standaloneToolEl = Render.standaloneToolEl;
  const appendOrFoldTool = Render.appendOrFoldTool;
  const hydrateSessionIndexes = UiState.hydrateSessionIndexes;

  function appendMessageNode(chat: HTMLElement, m: Message) {
    Messages.append(chat, m);
  }

  function scrollChat(capture = true) {
    ConversationUI.scrollChat(capture);
  }

  // ============================ session ops ============================
  function switchSession(id: string) {
    sessionState.switchSession(id);
  }
  function openProject(projectId: string) {
    sessionState.openProject(projectId);
  }
  function newSession(projectId: string | null = null) {
    sessionState.newSession(projectId);
  }
  async function forgetSessionState(id: string, preserveUi = false): Promise<boolean> {
    return forgetSessionOnServer(
      id,
      preserveUi,
      (state) => UiState.accept(state),
      (error) => setStatus(error, 'err'),
    );
  }
  async function deleteSession(id: string): Promise<void> {
    return sessionState.deleteSession(id);
  }
  function renameSession(id: string) {
    sidebar.beginSessionRename(id);
  }
  async function clearMessages(id: string): Promise<void> {
    return sessionState.clearMessages(id);
  }
  function renameProject(id: string) {
    sidebar.beginProjectRename(id);
  }
  async function deleteProject(id: string): Promise<void> {
    return sessionState.deleteProject(id);
  }
  const projectFolderPicker = createProjectFolderPicker({
    onBusy: (busy) => {
      $('btn-add-project').disabled = busy;
      updateComposerContext();
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
  const { pickProject, attachCurrentChatToPickedProject } = projectFolderPicker;

  function pushMsgToSession(sid: string, m: Message) {
    return sessionState.pushMessage(sid, m);
  }

  // ============================ context menus ============================
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
  const runEvents = new RunEventCoordinator({
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
    setStatus,
    updateSend,
    updateComposerContext,
    syncResearchUseProjectButton,
    refreshProviderStatus,
    applyProviderStatus,
    recordResearchRun: researchPanel.recordResearchRun,
    markResearchRestoreAvailability: researchPanel.markResearchRestoreAvailability,
    receiptSummary,
    receiptChangedCount,
    maybeAddChangesSummary,
    previewHtml,
  });
  const runProjection = runEvents.projection;

  function isSessionRunning(sessionId: string): boolean {
    return runProjection.isSessionRunning(sessionId);
  }

  function setStatus(text: string, kind: string) {
    const el = $('status');
    el.className = 'status ' + (kind || '');
    el.innerHTML = '';
    if (kind === 'run' || kind === 'warn') {
      const sp = document.createElement('span');
      sp.className = 'spinner';
      el.appendChild(sp);
    } else if (kind === 'err') {
      const d = document.createElement('span');
      d.className = 'status-dot err';
      el.appendChild(d);
    }
    if (text && (kind === 'run' || kind === 'warn' || kind === 'err')) {
      const t = document.createElement('span');
      t.className = 'status-text';
      t.textContent = text;
      el.appendChild(t);
    }
  }

  function applyLifecycle(
    data: ServerEvent,
    source: LifecycleInput['source'],
    active?: boolean,
    historySessionId?: string,
  ): boolean {
    return runEvents.applyLifecycle(data, source, active, historySessionId) !== false;
  }

  function handleServerEvent(data: ServerEvent, lifecycleSource: 'event' | 'snapshot' = 'event') {
    return runEvents.handleServerEvent(data, lifecycleSource);
  }

  function projectTaskStates(tasks: HistoryTask[], owners: Map<string, string>, historySessionId?: string): void {
    return runEvents.projectTaskStates(tasks, owners, historySessionId);
  }

  function applyRunState(data: RunState) {
    return runEvents.applyRunState(data);
  }

  function reconcileRunState() {
    return runEvents.reconcileRunState();
  }

  async function acceptRunResponse(response: Response, sessionId: string) {
    return runEvents.acceptRunResponse(response, sessionId);
  }

  function connectEvents() {
    return runEvents.connectEvents();
  }

  function addToSession(sid: string, m: Message): boolean {
    return sessionState.addMessage(sid, m);
  }

  function observeQueuedInput(input: QueuedInput): void {
    return runEvents.observeQueuedInput(input);
  }
  function observeUserQuestion(question: UserQuestion): void {
    return runEvents.observeUserQuestion(question);
  }

  function replaceSessionMessage(sid: string, predicate: (message: Message) => boolean, message: Message) {
    return sessionState.replaceMessage(sid, predicate, message);
  }

  function dropSessionMessages(sessionId: string, pred: (message: Message) => boolean) {
    sessionState.dropMessages(sessionId, pred);
  }
  function removeTeachMessage(sessionId: string, teachId: string) {
    dropSessionMessages(sessionId, (m) => m.type === 'teach' && (!teachId || m.id === teachId));
  }
  function removeShellRequest(sessionId: string, approvalId: string) {
    dropSessionMessages(sessionId, (m) => m.type === 'shell_request' && m.id === approvalId);
  }

  function addSendError(sessionId: string, eventKey = '', runId = '') {
    addToSession(sessionId, { type: 'err', text: 'Could not send the message', sessionId, eventKey, runId });
  }

  async function resumeTeaching(teachId: string, sessionId: string) {
    if (!teachId) return;
    try {
      const r = await requestResponse('/api/teach/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: teachId }),
      });
      if (r.ok) removeTeachMessage(sessionId, teachId);
    } catch {}
  }

  function sessionProjectPath(sid: string) {
    const s = sessionState.sessions.find((x) => x.id === sid);
    const p = sessionProject(s);
    return p ? p.path : '';
  }

  async function maybeAddChangesSummary(sid: string, eventKey = '', runId = '') {
    const project = sessionProjectPath(sid);
    if (!project || !runId) return;
    try {
      const data = await fetchRunChanges(sid, runId);
      if (!data.ok || !data.changed_count) return;
      const files = Array.isArray(data.files) ? data.files.slice(0, 3) : [];
      addToSession(sid, {
        type: 'changes',
        count: data.changed_count,
        files,
        project,
        eventKey,
        runId,
        sessionId: sid,
      });
    } catch {}
  }

  // Changes drawer UI lives in the changes_drawer module; these thin wrappers
  // keep existing call sites and event handlers stable.
  function openChangesDrawer(project: string) {
    return ChangesDrawer.open(project);
  }

  function closeChangesDrawer() {
    ChangesDrawer.close();
  }

  function closeLocalContextDrawer() {
    LocalContextDrawer.close();
  }

  function handleLocalContextScopeChanged() {
    LocalContextDrawer?.handleScopeChanged();
    workspace.suspend();
    closeChangesDrawer();
    ResearchDrawer.close();
  }

  function closeOtherDrawers() {
    workspace.close();
    documentPane.close();
    closeChangesDrawer();
    ResearchDrawer.close();
    closeLocalContextDrawer();
  }

  const approvalController = createApprovalController({
    sessions: () => sessionState.sessions,
    changed: (sid) => {
      const session = sessionState.sessions.find((s) => s.id === sid);
      if (session) hydrateSessionIndexes(session);
      persistActive();
      if (sid === sessionState.activeId && !ConversationUI.refreshApprovals()) renderChat();
    },
    fetch: (...args) => requestResponse(...args),
    focus: (id) => ConversationUI.revealApproval(id),
  });

  async function approveCommand(id: string, approved: boolean, scope = 'once') {
    await approvalController.submit(id, approved, scope);
    // The approval response/SSE event is authoritative for this card. A full
    // state snapshot can involve unrelated run work, so keep it off the click
    // path and let it reconcile in the background.
    void reconcileRunState();
  }

  // ============================ composer ============================
  function resizeTask() {
    return Composer.resizeTask();
  }
  function updateSend() {
    return Composer.updateSend();
  }

  function toggleResearchForActive() {
    return Composer.toggleResearchForActive();
  }

  // Local provider config popover lives in the provider_ui module.
  function openLocalProviderConfig() {
    return ProviderUI.openLocalConfig();
  }

  function closeLocalProviderConfig() {
    ProviderUI.closeLocalConfig();
  }

  function setActiveProvider(id: string) {
    return Composer.setActiveProvider(id);
  }
  // Provider menu handlers are bound by ProviderUI.init().

  function continueTask(sessionId: string, sourceRunId: string) {
    return Composer.continueTask(sessionId, sourceRunId);
  }
  $('changes-close').onclick = closeChangesDrawer;
  const researchClose = $('research-close');
  if (researchClose) researchClose.onclick = () => ResearchDrawer.close();
  const researchRestore = $('research-restore');
  if (researchRestore) researchRestore.onclick = researchPanel.restoreResearchRun;
  function syncResearchUseProjectButton() {
    const btn = $('research-use-project');
    if (!btn) return;
    const s = activeSession();
    const busy = runProjection.primaryRun?.sessionId === sessionState.activeId;
    const unusable = busy || !s || !!sessionProject(s);
    btn.disabled = unusable;
    btn.title = busy ? 'Busy' : !s ? 'No chat' : sessionProject(s) ? 'Already in a project' : '';
  }
  $('research-use-project').onclick = () => attachCurrentChatToPickedProject();
  $('btn-new-chat').onclick = () => newSession(null);
  $('btn-add-project').onclick = pickProject;
  $('toggle-sidebar').onclick = () => {
    document.body.classList.add('sidebar-collapsed');
    $('show-sidebar').style.display = '';
    $('aside').inert = true;
    $('show-sidebar').focus();
  };
  $('show-sidebar').onclick = () => {
    document.body.classList.remove('sidebar-collapsed');
    $('show-sidebar').style.display = 'none';
    $('aside').inert = false;
    $('toggle-sidebar').focus();
  };
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'n' || e.key === 'N')) {
      if (eventElement(e)?.closest('.rename-input')) return;
      e.preventDefault();
      newSession(null);
      return;
    }
    if (e.key === 'Escape') {
      if (eventElement(e)?.closest('.rename-input')) return;
      closeAllMenus();
      closeOtherDrawers();
      ProviderUI.closeMenu($('provider-menu').classList.contains('open'));
      if (ProviderUI) ProviderUI.closeLocalConfig();
    }
  });
  // ============================ boot ============================
  function boot(): Promise<void> {
    return bootApplication({
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
    });
  }

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
      renderSidebar,
      renderChat,
      switchSession,
      openProject,
      newSession,
      setActiveProvider,
      continueTask,
      addToSession,
      openChangesDrawer,
      applyRunState,
      clearMessages,
      deleteSession,
      appendMessageNode,
      replaceSessionMessage,
      handleServerEvent: (raw: unknown) => handleServerEvent(decodeServerEvent(raw)),
      statusRow,
      pushMsgToSession,
      setStatus,
      updateSend,
      updateComposerContext,
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
  boot().catch((error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    setStatus(message, 'warn');
    reportDesktopFailure(message);
  });
}
