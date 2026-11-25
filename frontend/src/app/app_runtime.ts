import { fetchChangeFile, fetchChanges, fetchRunChanges, restoreChanges } from '../api/changes';
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
import { DocumentPane, type DocumentRef } from '../workspace/document_pane';
import { HistoryPanel } from '../workspace/history_panel';
import type { HistoryTask } from '../conversation/history_repository';
import { RecordedPanels } from '../workspace/recorded_panels';
import { Workspace } from '../workspace/workspace';
import '../conversation/chat_fork.css';
import { CommandPalette } from './command_palette';
import '../workspace/workspace.css';
import { showCommandPermissions } from '../runs/command_permissions';
import { reportDesktopFailure, reportDesktopReady } from './desktop_ready';
import { hydrateDocumentContent } from '../workspace/document_content';
import type { QueuedInput } from '../runs/input_outbox';
import type { LifecycleInput } from '../runs/run_lifecycle';
import { createApprovalController } from '../runs/shell_approvals';
import { createSidebar } from '../conversation/sidebar';
import { type UserQuestion, UserQuestions } from '../runs/user_questions';
import { mountCharacterPond } from './character_pond';
import { decodeServerEvent, isRecord, type ServerEvent } from '../domain/protocol';
import type { Message, Project, RunState, Session } from '../types/domain';

type RenameCommit = (value: string | undefined) => void;
interface StatusAction {
  label: string;
  disabled?: boolean;
  onclick: () => void;
}
interface StatusRowOptions {
  kind?: string;
  err?: boolean;
  actions?: StatusAction[];
  action?: StatusAction;
  copyText?: string;
}

export function mountApp(): void {
  // ============================ state & storage ============================
  type AppElement = HTMLElement & { disabled: boolean };
  const $ = <T extends HTMLElement = AppElement>(id: string): T => document.getElementById(id) as T;
  const eventElement = (event: Event): Element | null => (event.target instanceof Element ? event.target : null);
  const workspace = new Workspace();
  const documentPane = new DocumentPane({
    docked: true,
    visibility: (visible) => (visible ? workspace.reveal('files') : workspace.panelClosed('files')),
    read: async (sessionId, ref, workspace) => {
      const response = await requestResponse('/api/documents/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, expected_workspace: workspace, ...ref }),
      });
      const data: unknown = await response.json();
      if (!isRecord(data)) throw new Error('Invalid document response');
      if (!response.ok || data.ok !== true)
        throw new Error(
          typeof data.message === 'string'
            ? data.message
            : typeof data.error === 'string'
              ? data.error
              : 'Could not load document',
        );
      const { content, path, version, title, format, workspace: scope } = data;
      if (
        typeof content !== 'string' ||
        typeof path !== 'string' ||
        typeof version !== 'string' ||
        typeof title !== 'string' ||
        typeof format !== 'string' ||
        typeof scope !== 'string'
      )
        throw new Error('Invalid document response');
      return { content, path, version, title, format, workspace: scope };
    },
    markdown: (container, text, scope) => {
      Render.renderMarkdown(container, text);
      if (scope)
        void hydrateDocumentContent(container, {
          sessionId: scope.sessionId,
          workspace: scope.workspace.startsWith('research:') ? undefined : scope.workspace,
          parent: scope.path.split('/').slice(0, -1).join('/'),
        });
    },
    copy: (text) => Render.copyText(text),
  });
  workspace.register('files', 'Files', documentPane.element, () => documentPane.show(sessionState.activeId));
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
  function openDocument(sessionId: string, ref: DocumentRef): void {
    void documentPane.open(sessionId, ref);
  }
  function previewHtml(sessionId: string, path: string, project: string): void {
    if (sessionId === sessionState.activeId) {
      void documentPane.open(
        sessionId,
        { kind: 'workspace', path },
        { workspace: project || undefined, preview: true },
      );
    }
  }
  document.addEventListener('click', (event) => {
    const link = eventElement(event)?.closest<HTMLElement>('[data-document-path]');
    if (!link) return;
    event.preventDefault();
    const path = link.dataset.documentPath;
    if (path) {
      const line = Number(link.dataset.documentLine) || undefined;
      openDocument(
        sessionState.activeId,
        link.closest('.document-pane') ? documentPane.fileLink(path, line) : { kind: 'workspace', path, line },
      );
    }
  });
  // Escape and receipt helpers are provided by the render module.
  const escapeHtml = Render.escapeHtml;
  const receiptSummary = Render.receiptSummary;
  const receiptChangedCount = Render.receiptChangedCount;
  let DEFAULT_PROVIDER = UiState.DEFAULT_PROVIDER;
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
  const bindUiStatePagehide = UiState.bindPagehide;
  const restoreUiStateFromServer = UiState.restoreFromServer;

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
  let armedDangerTimer: ReturnType<typeof setTimeout> | null = null;
  function renameInputEl(value: string, commit: RenameCommit, cancel: () => void) {
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'rename-input';
    input.value = value || '';
    input.maxLength = 80;
    input.setAttribute('aria-label', 'Rename');
    let settled = false;
    const finish = (fn: RenameCommit | (() => void), arg?: string) => {
      if (settled) return;
      settled = true;
      fn(arg);
    };
    input.onmousedown = (e) => e.stopPropagation();
    input.onclick = (e) => e.stopPropagation();
    input.onkeydown = (e) => {
      if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        finish(commit, input.value);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        finish(cancel, undefined);
      }
    };
    input.onblur = () => finish(commit, input.value);
    return input;
  }
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
  function disarmDangerButtons() {
    if (armedDangerTimer !== null) {
      clearTimeout(armedDangerTimer);
      armedDangerTimer = null;
    }
    document.querySelectorAll<HTMLButtonElement>('.ctx-menu button.arming').forEach((b) => {
      const button = b as HTMLButtonElement;
      button.classList.remove('arming');
      if (button.dataset.label) button.textContent = button.dataset.label;
    });
  }
  function armDanger(btn: HTMLButtonElement, run: () => void) {
    if (btn.classList.contains('arming')) {
      disarmDangerButtons();
      closeAllMenus();
      run();
      return;
    }
    disarmDangerButtons();
    btn.dataset.label = btn.textContent;
    btn.textContent = btn.dataset.confirm || 'Confirm';
    btn.classList.add('arming');
    armedDangerTimer = setTimeout(disarmDangerButtons, 3000);
  }

  // ============================ chat rendering ============================
  function renderChat() {
    workspace.setSession(sessionState.activeId);
    documentPane.setSession(sessionState.activeId);
    ConversationUI.renderChat();
  }

  function statusRow(prefix: string, text: string, opts: StatusRowOptions = {}) {
    const div = document.createElement('div');
    div.className = 'msg ' + (opts.kind || '');
    const row = document.createElement('div');
    row.className = 'status-row' + (opts.err ? ' err' : '');
    const pre = document.createElement('span');
    pre.className = 'sr-prefix';
    pre.textContent = prefix;
    const txt = document.createElement('span');
    txt.className = 'sr-text';
    let body = text || '';
    if (opts.err) body = body.replace(/^ERROR:\s*/i, '');
    txt.textContent = body;
    row.append(pre, txt);
    const actions = Array.isArray(opts.actions) ? opts.actions.slice() : [];
    if (opts.action) actions.push(opts.action);
    for (const action of actions) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'link-btn';
      btn.textContent = action.label;
      btn.disabled = !!action.disabled;
      btn.onclick = action.onclick;
      row.appendChild(btn);
    }
    div.appendChild(row);
    if (opts.copyText) addMessageCopyButton(div, opts.copyText);
    return div;
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
  function openMenuAt(menu: HTMLElement, anchor: HTMLElement) {
    const rect = anchor.getBoundingClientRect();
    menu.classList.add('open');
    const mr = menu.getBoundingClientRect();
    let left = rect.right - mr.width;
    if (left < 8) left = rect.left;
    let top = rect.bottom + 4;
    if (top + mr.height > window.innerHeight - 8) top = rect.top - mr.height - 4;
    menu.style.left = Math.max(8, left) + 'px';
    menu.style.top = Math.max(8, top) + 'px';
    ConversationUI.menuOpened(menu, anchor);
  }
  function closeAllMenus() {
    disarmDangerButtons();
    document.querySelectorAll('.ctx-menu.open').forEach((m) => m.classList.remove('open'));
    ConversationUI.menusClosed();
  }
  function openProjectMenu(anchor: HTMLElement, projectId: string) {
    closeAllMenus();
    const menu = $('proj-menu');
    menu.dataset.projectId = projectId;
    openMenuAt(menu, anchor);
  }
  function openSessionMenu(anchor: HTMLElement, sessionId: string) {
    closeAllMenus();
    const menu = $('sess-menu');
    menu.dataset.sessionId = sessionId;
    openMenuAt(menu, anchor);
  }
  $('proj-menu').addEventListener('click', (e) => {
    const btn = eventElement(e)?.closest<HTMLButtonElement>('button[data-act]');
    if (!btn) return;
    const id = $('proj-menu').dataset.projectId;
    const act = btn.dataset.act;
    if (act === 'remove') {
      if (id) armDanger(btn, () => deleteProject(id));
      return;
    }
    closeAllMenus();
    if (!id) return;
    if (act === 'newchat') newSession(id);
    else if (act === 'rename') renameProject(id);
  });
  $('sess-menu').addEventListener('click', (e) => {
    const btn = eventElement(e)?.closest<HTMLButtonElement>('button[data-act]');
    if (!btn) return;
    const id = $('sess-menu').dataset.sessionId;
    const act = btn.dataset.act;
    if (act === 'delete') {
      if (id) armDanger(btn, () => deleteSession(id));
      return;
    }
    closeAllMenus();
    if (!id) return;
    if (act === 'rename') renameSession(id);
  });
  document.addEventListener('click', (e) => {
    if (eventElement(e)?.closest('.ctx-menu')) return;
    if (eventElement(e)?.closest('.project-more, .session-more')) return;
    closeAllMenus();
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
  async function boot() {
    const main = document.querySelector('main');
    if (main) mountCharacterPond(main);
    await OperatorAuth.authorize();
    await ProviderUI.adoptCatalog();
    DEFAULT_PROVIDER = UiState.DEFAULT_PROVIDER;
    bindUiStatePagehide(Composer.captureDraft);
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
      PROVIDERS,
      PROVIDER_LABELS,
      DEFAULT_PROVIDER,
      currentProviderId,
      setActiveProvider,
      activeSession,
      persistActiveNow,
    });
    Sse.init({
      forRun: Requests.forRun,
      setStatus,
      applyRunState,
      handleServerEvent: (event, cursor) => runEvents.handleServerEvent(event, 'event', cursor),
      refreshProviderStatus,
      acceptRun: (runId: string | null, sessionId: string | null) => {
        if (runId && sessionId)
          applyLifecycle({ type: 'task_start', run_id: runId, session_id: sessionId }, 'admission');
      },
      setProviderBusy: (busy: boolean) => {
        $('provider-button').disabled = !!busy;
        $('effort-button').disabled = !!busy;
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
      PROVIDERS,
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
        projectTaskStates(tasks, new Map(runProjection.entries()), sessionId),
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
    await restoreUiStateFromServer();
    await userQuestions.refresh();
    await Composer.refreshQueuedInputs();
    Composer.setReady();
    syncResearchUseProjectButton();
    connectEvents();
    reportDesktopReady();
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
