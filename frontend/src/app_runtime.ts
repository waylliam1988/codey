import { RunEventCoordinator } from './run_event_coordinator';
import { SessionStateStore } from './session_state_store';
import { SessionStateViews } from './session_state_views';
import { ChatFork } from './chat_fork';
import { DocumentPane, type DocumentRef } from './document_pane';
import { HistoryPanel } from './history_panel';
import type { HistoryTask } from './history_repository';
import { RecordedPanels } from './recorded_panels';
import { Workspace } from './workspace';
import './chat_fork.css';
import { CommandPalette } from './command_palette';
import { iconButton } from './icon_button';
import './workspace.css';
import { showCommandPermissions } from './command_permissions';
import { reportDesktopFailure, reportDesktopReady } from './desktop_ready';
import { hydrateDocumentContent } from './document_content';
import type { QueuedInput } from './input_outbox';
import type { LifecycleInput } from './run_lifecycle';
import { runtime } from './runtime';
import { createApprovalController } from './shell_approvals';
import { createSidebar } from './sidebar';
import { type UserQuestion, UserQuestions } from './user_questions';
import './operator_auth';
import './render';
import './process';
import './messages';
import './research_graph';
import './research_drawer';
import './research_runs';
import './changes_drawer';
import './local_context_drawer';
import './run_details';
import './models';
import './model_settings';
import './settings';
import './provider_ui';
import './ui_state';
import './sse';
import './requests';
import './composer';
import { mountCharacterPond } from './character_pond';
import './conversation_nav';
import './conversation_ui';
import './domain/protocol';
import './api/transport';
import { decodeChangeFilePreview, decodeChangesData, type ServerEvent } from './domain/protocol';
import type { Message, Project, ResearchPayload, RestoreChangesResult, RunState, Session } from './types/domain';

interface PickFolderResult {
  ok?: boolean;
  path?: string;
  name?: string;
  error?: string;
}
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

// The existing application entry is migrated into the TypeScript bundle.
// It remains behavior-compatible while later steps replace window globals with imports.
// ============================ state & storage ============================
type AppElement = HTMLElement & { disabled: boolean };
const $ = <T extends HTMLElement = AppElement>(id: string): T => document.getElementById(id) as T;
const eventElement = (event: Event): Element | null => (event.target instanceof Element ? event.target : null);
const workspace = new Workspace();
runtime.Workspace = workspace;
const documentPane = new DocumentPane({
  docked: true,
  visibility: (visible) => (visible ? workspace.reveal('files') : workspace.panelClosed('files')),
  read: async (sessionId, ref, workspace) => {
    const response = await fetch('/api/documents/read', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, expected_workspace: workspace, ...ref }),
    });
    const data = await response.json();
    if (!response.ok || data.ok !== true) throw new Error(data.message || data.error || 'Could not load document');
    if (typeof data.content !== 'string' || typeof data.path !== 'string' || typeof data.version !== 'string')
      throw new Error('Invalid document response');
    return data;
  },
  markdown: (container, text, scope) => {
    runtime.Render.renderMarkdown(container, text);
    if (scope)
      void hydrateDocumentContent(container, {
        sessionId: scope.sessionId,
        workspace: scope.workspace.startsWith('research:') ? undefined : scope.workspace,
        parent: scope.path.split('/').slice(0, -1).join('/'),
      });
  },
  copy: (text) => runtime.Render.copyText(text),
});
workspace.register('files', 'Files', documentPane.element, () => documentPane.show(sessionState.activeId));
workspace.register('changes', 'Changes', $('changes-drawer'), (target) => {
  const project = sessionProjectPath(sessionState.activeId);
  if (project) void runtime.ChangesDrawer.open(project, target.path || '');
  else {
    $('changes-body').textContent = 'Choose a project to inspect workspace changes.';
    $('changes-subtitle').textContent = 'No project';
    runtime.UiState.setDrawerOpen('changes-drawer', true);
  }
});
workspace.register('research', 'Research', $('research-drawer'), (target) => {
  const runId = target.runId || currentResearchRun(sessionState.activeId)?.runId;
  if (runId) runtime.ResearchDrawer.open(sessionState.activeId, runId);
  else {
    $('research-body').textContent = 'No research recorded for this conversation.';
    $('research-subtitle').textContent = 'No research yet';
    runtime.UiState.setDrawerOpen('research-drawer', true);
  }
});
const recordedPanels = new RecordedPanels({
  sessionId: () => sessionState.activeId,
  messages: () => activeSession()?.messages || [],
  openDocument,
  copy: (text) => runtime.Render.copyText(text),
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
  runtime.Composer.captureDraft();
  const current = runtime.UiState.current();
  const follow = followIntent && current.active_id === baseline.active_id;
  runtime.UiState.accept(result.state);
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
  workspaceChanged: () => runtime.Composer.syncSession(),
  originRendered: () => {
    if (runtime.ConversationUI.isFollowing()) runtime.ConversationUI.scrollChat();
    runtime.ConversationUI.captureView();
  },
  saved: () => runtime.UiState.ensureSaved(),
  state: () => runtime.UiState.current(),
  adopt: adoptChatFork,
  switchSession,
});
runtime.ChatFork = chatFork;
const historyPanel = new HistoryPanel({
  session: activeSession,
  switchSession,
  changed: (id) => runtime.ConversationUI.refreshHistory(id),
  adoptFork: adoptChatFork,
});
workspace.register('history', 'History', historyPanel.element, (target) => {
  void historyPanel.show(target);
});
document.body.append(workspace.element);
const workspaceMenu = document.createElement('div');
workspaceMenu.className = 'workspace-picker';
workspaceMenu.hidden = true;
workspaceMenu.setAttribute('role', 'menu');
const workspaceButton = iconButton('Open workspace', 'workspace', () => {
  workspaceMenu.hidden = !workspaceMenu.hidden;
  workspaceButton.setAttribute('aria-expanded', String(!workspaceMenu.hidden));
  if (!workspaceMenu.hidden) workspaceMenu.querySelector<HTMLElement>('button')?.focus();
});
workspaceButton.setAttribute('aria-haspopup', 'menu');
workspaceButton.setAttribute('aria-expanded', 'false');
const workspaceActions = [
  ['files', 'Files'],
  ['changes', 'Changes'],
  ['research', 'Research'],
  ['review', 'Recorded review'],
  ['outputs', 'Command output'],
  ['history', 'Branches and workflow'],
] as const;
for (const [id, label] of workspaceActions) {
  const item = document.createElement('button');
  item.type = 'button';
  item.setAttribute('role', 'menuitem');
  item.textContent = label;
  item.onclick = () => {
    workspaceMenu.hidden = true;
    workspaceButton.setAttribute('aria-expanded', 'false');
    workspace.select(id);
    workspace.element.querySelector<HTMLElement>(`[role="tab"][aria-selected="true"]`)?.focus();
  };
  workspaceMenu.append(item);
}
workspaceMenu.onkeydown = (event) => {
  const rows = Array.from(workspaceMenu.querySelectorAll('button'));
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    workspaceMenu.hidden = true;
    workspaceButton.setAttribute('aria-expanded', 'false');
    workspaceButton.focus();
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    const index = rows.indexOf(document.activeElement as HTMLButtonElement);
    rows[
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? rows.length - 1
          : (index + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length
    ]?.focus();
  }
};
const workspaceChooser = document.createElement('div');
workspaceChooser.className = 'workspace-chooser';
workspaceChooser.append(workspaceButton, workspaceMenu);
document.querySelector('.topbar-actions')?.append(workspaceChooser);
const commandPalette = new CommandPalette(() => [
  ...workspaceActions.map(([id, label]) => ({ id, label, run: () => workspace.select(id) })),
  { id: 'search-conversation', label: 'Search this conversation', run: () => runtime.ConversationUI.searchHistory() },
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
document.addEventListener('pointerdown', (event) => {
  if (event.target instanceof Node && !workspaceChooser.contains(event.target)) {
    workspaceMenu.hidden = true;
    workspaceButton.setAttribute('aria-expanded', 'false');
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
  changed: () => runtime.Composer?.updateSend(),
});
runtime.UserQuestions = userQuestions;
function openDocument(sessionId: string, ref: DocumentRef): void {
  closeOtherDrawers('document');
  void documentPane.open(sessionId, ref);
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
const escapeHtml = runtime.Render.escapeHtml;
const receiptSummary = runtime.Render.receiptSummary;
const receiptChangedCount = runtime.Render.receiptChangedCount;
let DEFAULT_PROVIDER = runtime.UiState.DEFAULT_PROVIDER;
const PROVIDER_LABELS = runtime.UiState.PROVIDER_LABELS;
const PROVIDERS = runtime.UiState.PROVIDERS;
const sessionState: SessionStateStore = new SessionStateStore({
  ui: runtime.UiState,
  defaultProvider: liveDefaultProvider,
  isRenaming: (id) => sidebar.isEditingSession(id) || runtime.ConversationUI.isRenamingSession(id),
  invalidate: (change) => sessionViews.invalidate(change),
  forgetView: (id) => runtime.ConversationUI.forget(id),
  forgetOnServer: forgetSessionState,
  requestIdForRun: (sessionId, runId) => runtime.Requests.forRun(sessionId, runId)?.request.id,
  mountedStreamId: (sessionId, runId) => sessionViews.mountedStreamId(sessionId, runId),
  messageAdded: () => {
    runtime.Requests.refreshActions();
    runtime.ConversationUI.updateNotice();
  },
});
const sessionViews: SessionStateViews = new SessionStateViews({
  state: sessionState,
  chat: () => $('chat'),
  messages: runtime.Messages,
  process: runtime.Process,
  renderSidebar,
  renderChat,
  scrollChat,
  appendMessageNode,
  refreshApprovals: () => runtime.ConversationUI.refreshApprovals(),
  scopeChanged: handleLocalContextScopeChanged,
  syncProvider: syncProviderUI,
  syncComposer: () => {
    runtime.Composer.syncSession();
    runtime.Composer.updateSend();
  },
  updateComposerContext,
  closeLocalContext: closeLocalContextDrawer,
});

function liveDefaultProvider() {
  return runtime.UiState.DEFAULT_PROVIDER;
}

const { apply: applyUiState, cache: cacheUiState, cached: cachedUiState } = runtime.UiState;
const persistActive = runtime.UiState.persist;
const persistActiveNow = runtime.UiState.persistNow;
const bindUiStatePagehide = runtime.UiState.bindPagehide;
const restoreUiStateFromServer = runtime.UiState.restoreFromServer;

const bootCachedState = cachedUiState();

runtime.UiState.init({
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
  runtime.ProviderUI.sync(providerId);
}
function applyProviderStatus(providers: unknown, isSSE = true) {
  runtime.ProviderUI.applyStatus(providers, isSSE);
}
function refreshProviderStatus(immediate = false) {
  return runtime.ProviderUI.refreshStatus(immediate);
}

function updateComposerContext() {
  const el = $('composer-context');
  const s = activeSession();
  const p = activeProject();
  const proj = p ? p.name : 'Choose folder';
  const disabled =
    runProjection.primaryRun?.sessionId === sessionState.activeId || runtime.Composer.isSending(sessionState.activeId);
  const folderDisabled = disabled || !!p || projectPickerBusy;
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
    runProjection.primaryRun?.sessionId === sessionState.activeId || runtime.Composer.isSending(sessionState.activeId);
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
    update: () => runtime.ConversationUI.updateSearch(),
    isSearching: () => runtime.ConversationUI.isSearching(),
    matchesSession: (session) => runtime.ConversationUI.matchesSession(session),
    matchesProject: (project) => runtime.ConversationUI.matchesProject(project),
    appendGroupLabel: (parent, label, hasMatches) => runtime.ConversationUI.appendGroupLabel(parent, label, hasMatches),
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
  runtime.ConversationUI.renderChat();
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
const messageCopyText = runtime.Render.messageCopyText;
const copyText = runtime.Render.copyText;
const addMessageCopyButton = runtime.Render.addMessageCopyButton;
const renderMarkdown = runtime.Render.renderMarkdown;
const FOLDABLE_TOOL_KINDS = runtime.Render.FOLDABLE_TOOL_KINDS;
const standaloneToolEl = runtime.Render.standaloneToolEl;
const appendOrFoldTool = runtime.Render.appendOrFoldTool;
const hydrateSessionIndexes = runtime.UiState.hydrateSessionIndexes;

function appendMessageNode(chat: HTMLElement, m: Message) {
  runtime.Messages.append(chat, m);
}

function scrollChat(capture = true) {
  runtime.ConversationUI.scrollChat(capture);
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
  try {
    const response = await fetch('/api/new_chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: id, preserve_ui: preserveUi }),
    });
    if (!response.ok) return false;
    runtime.UiState.accept((await response.json()).state);
    return true;
  } catch {
    return false;
  }
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
function ensureProject(path: string, name: string) {
  return sessionState.ensureProject(path, name);
}
let projectPickerBusy = false;
async function pickProjectPath(mode = 'open', initial = '') {
  if (projectPickerBusy) return null;
  projectPickerBusy = true;
  $('btn-add-project').disabled = true;
  updateComposerContext();
  try {
    const r = await fetch('/api/pick_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, initial }),
    });
    let data: PickFolderResult = {};
    try {
      data = await r.json();
    } catch {}
    if (!r.ok) {
      setStatus('Could not add project', 'err');
      return null;
    }
    if (!data.ok || !data.path) return null;
    return data;
  } finally {
    projectPickerBusy = false;
    $('btn-add-project').disabled = false;
    updateComposerContext();
  }
}
function attachSessionToProject(projectId: string | null, sessionId: string = sessionState.activeId) {
  return sessionState.attachSessionToProject(projectId, sessionId);
}
async function pickProject() {
  const s = activeSession();
  const shouldAttach = !!s && !sessionProject(s);
  const data = await pickProjectPath(shouldAttach ? 'new' : 'open', currentProjectPath());
  if (!data) return;
  if (typeof data.path !== 'string') return;
  const p = ensureProject(data.path, typeof data.name === 'string' ? data.name : data.path);
  if (shouldAttach) attachSessionToProject(p.id, s.id);
  else newSession(p.id);
}

async function attachCurrentChatToPickedProject() {
  if (runProjection.size) return;
  const sid = sessionState.activeId;
  const s = sessionState.sessions.find((x) => x.id === sid);
  if (!s || sessionProject(s)) return;
  const data = await pickProjectPath('new', currentProjectPath());
  if (!data) return;
  if (typeof data.path !== 'string') return;
  const p = ensureProject(data.path, typeof data.name === 'string' ? data.name : data.path);
  if (!attachSessionToProject(p.id, sid)) return;
}

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
  runtime.ConversationUI.menuOpened(menu, anchor);
}
function closeAllMenus() {
  disarmDangerButtons();
  document.querySelectorAll('.ctx-menu.open').forEach((m) => m.classList.remove('open'));
  runtime.ConversationUI.menusClosed();
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
function openTopMenu(anchor: HTMLElement) {
  closeAllMenus();
  openMenuAt($('top-menu'), anchor);
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
$('top-menu').addEventListener('click', (e) => {
  const btn = eventElement(e)?.closest<HTMLButtonElement>('button[data-act]');
  if (!btn) return;
  const act = btn.dataset.act;
  if (act === 'clear') {
    armDanger(btn, () => clearMessages(sessionState.activeId));
    return;
  }
  closeAllMenus();
  if (act === 'local-context') openLocalContextDrawer();
  if (act === 'search-history') runtime.ConversationUI.searchHistory();
  if (act === 'commands') commandPalette.open();
});
document.addEventListener('click', (e) => {
  if (eventElement(e)?.closest('.ctx-menu')) return;
  if (eventElement(e)?.closest('.project-more, .session-more, #topbar-more')) return;
  closeAllMenus();
});

// ============================ SSE event ingestion ============================
const runEvents = new RunEventCoordinator({
  state: sessionState,
  ui: runtime.UiState,
  history: runtime.ConversationUI,
  requests: runtime.Requests,
  composer: runtime.Composer,
  approvals: () => approvalController,
  questions: userQuestions,
  transport: runtime.Sse,
  providers: PROVIDERS,
  revealApproval: (sessionId, id) => {
    if (sessionId !== sessionState.activeId) return;
    requestAnimationFrame(() => {
      if (sessionId === sessionState.activeId && runtime.ConversationUI.isFollowing())
        runtime.ConversationUI.revealApproval(id);
    });
  },
  setStatus,
  updateSend,
  updateComposerContext,
  syncResearchUseProjectButton,
  refreshProviderStatus,
  applyProviderStatus,
  recordResearchRun,
  markResearchRestoreAvailability,
  receiptSummary,
  receiptChangedCount,
  maybeAddChangesSummary,
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
  return runEvents.applyLifecycle(data, source, active, historySessionId);
}

function handleServerEvent(raw: unknown, lifecycleSource: 'event' | 'snapshot' = 'event') {
  return runEvents.handleServerEvent(raw, lifecycleSource);
}

let researchDrawerTab = 'evidence';
let researchGraphDepth = 1;
let researchNoteFocusId = '';

function recordResearchRun(sessionId: string, runId: string, research: ResearchPayload, receipt: unknown) {
  const item = runtime.ResearchRuns.record(sessionId, runId, research, receipt);
  if (!item) return null;
  researchDrawerTab = 'evidence';
  researchNoteFocusId = '';
  runtime.ResearchDrawer.invalidateNoteCache(runtime.ResearchDrawer.noteIdsForRun(item));
  return item;
}

function currentResearchRun(sessionId = sessionState.activeId) {
  return runtime.ResearchRuns.current(sessionId);
}

async function restoreResearchRun() {
  const restore = $('research-restore');
  if (restore) restore.disabled = true;
  const ownerSessionId = sessionState.activeId;
  try {
    const result = await runtime.ResearchRuns.restore(ownerSessionId);
    if (!result) return;
    const subtitle = sessionState.activeId === ownerSessionId ? $('research-subtitle') : null;
    if (subtitle) subtitle.textContent = String(result.message || '');
    if (result.run) {
      runtime.ResearchDrawer.invalidateNoteCache(runtime.ResearchDrawer.noteIdsForRun(result.run));
    }
    if (result.ok) {
      addToSession(result.ownerSessionId, {
        type: 'done',
        text: `Research restored: ${result.restored.length} files`,
        eventKey: `research-restore:${String(result.run.runId || '')}`,
      });
    }
    if (sessionState.activeId === result.ownerSessionId) runtime.ResearchDrawer.render(sessionState.activeId);
  } finally {
    const current = currentResearchRun();
    if (restore) restore.disabled = !current || !current.restoreable;
  }
}

function markResearchRestoreAvailability(runIds: unknown) {
  const changed = runtime.ResearchRuns.markRestoreAvailability(runIds);
  const drawer = $('research-drawer');
  if (changed && drawer && drawer.classList.contains('open')) {
    runtime.ResearchDrawer.render(sessionState.activeId);
  }
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
    const r = await fetch('/api/teach/resume', {
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

async function fetchChanges(project: string) {
  const r = await fetch('/api/changes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project }),
  });
  const data = decodeChangesData(await r.json());
  if (!r.ok) throw new Error(data.error || 'Could not load changes');
  return data;
}

async function fetchChangeFile(project: string, path: string) {
  const r = await fetch('/api/changes/file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project, path }),
  });
  const data = decodeChangeFilePreview(await r.json());
  if (!r.ok || !data.ok) throw new Error(data.error || 'Could not load file diff');
  return data;
}

async function restoreChanges(project: string) {
  const r = await fetch('/api/changes/restore', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project }),
  });
  let data: RestoreChangesResult = {};
  try {
    data = await r.json();
  } catch {}
  if (!r.ok) data.ok = false;
  return data;
}

async function maybeAddChangesSummary(sid: string, eventKey = '', runId = '') {
  const project = sessionProjectPath(sid);
  if (!project) return;
  try {
    const data = await fetchChanges(project);
    if (!data.ok || !data.changed_count) return;
    const files = Array.isArray(data.files) ? data.files.slice(0, 3) : [];
    addToSession(sid, { type: 'changes', count: data.changed_count, files, project, eventKey, runId });
  } catch {}
}

// Changes drawer UI lives in the changes_drawer module; these thin wrappers
// keep existing call sites and event handlers stable.
function openChangesDrawer(project: string) {
  return runtime.ChangesDrawer.open(project);
}

function closeChangesDrawer() {
  runtime.ChangesDrawer.close();
}

function openLocalContextDrawer() {
  runtime.LocalContextDrawer.open();
}

function closeLocalContextDrawer() {
  runtime.LocalContextDrawer.close();
}

function handleLocalContextScopeChanged() {
  workspace.suspend();
  closeChangesDrawer();
  runtime.ResearchDrawer.close();
  if (runtime.LocalContextDrawer) runtime.LocalContextDrawer.handleScopeChanged();
}

function closeOtherDrawers(active: string | undefined) {
  if (active && active !== 'local_context') {
    closeLocalContextDrawer();
    return;
  }
  workspace.close();
  if (active !== 'document') documentPane.close();
  if (active !== 'changes') closeChangesDrawer();
  if (active !== 'research') runtime.ResearchDrawer.close();
  if (active !== 'local_context') closeLocalContextDrawer();
}

const approvalController = createApprovalController({
  sessions: () => sessionState.sessions,
  changed: (sid) => {
    const session = sessionState.sessions.find((s) => s.id === sid);
    if (session) hydrateSessionIndexes(session);
    persistActive();
    if (sid === sessionState.activeId && !runtime.ConversationUI.refreshApprovals()) renderChat();
  },
  fetch: (...args) => fetch(...args),
  focus: (id) => runtime.ConversationUI.revealApproval(id),
});
runtime.ShellApprovals = approvalController;

async function approveCommand(id: string, approved: boolean, scope = 'once') {
  await approvalController.submit(id, approved, scope);
  // The approval response/SSE event is authoritative for this card. A full
  // state snapshot can involve unrelated run work, so keep it off the click
  // path and let it reconcile in the background.
  void reconcileRunState();
}

// ============================ composer ============================
function resizeTask() {
  return runtime.Composer.resizeTask();
}
function updateSend() {
  return runtime.Composer.updateSend();
}

function toggleResearchForActive() {
  return runtime.Composer.toggleResearchForActive();
}

// Local provider config popover lives in the provider_ui module.
function openLocalProviderConfig() {
  return runtime.ProviderUI.openLocalConfig();
}

function closeLocalProviderConfig() {
  runtime.ProviderUI.closeLocalConfig();
}

function setActiveProvider(id: string) {
  return runtime.Composer.setActiveProvider(id);
}
// Provider menu handlers are bound by runtime.ProviderUI.init().

function continueTask(sessionId: string, sourceRunId: string) {
  return runtime.Composer.continueTask(sessionId, sourceRunId);
}
$('changes-close').onclick = closeChangesDrawer;
const researchClose = $('research-close');
if (researchClose) researchClose.onclick = () => runtime.ResearchDrawer.close();
const researchRestore = $('research-restore');
if (researchRestore) researchRestore.onclick = restoreResearchRun;
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
$('topbar-more').onclick = (e) => {
  e.stopPropagation();
  openTopMenu(e.currentTarget as HTMLElement);
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
    closeOtherDrawers(undefined);
    runtime.ProviderUI.closeMenu($('provider-menu').classList.contains('open'));
    if (runtime.ProviderUI) runtime.ProviderUI.closeLocalConfig();
  }
});
// ============================ boot ============================
async function boot() {
  const main = document.querySelector('main');
  if (main) mountCharacterPond(main);
  await runtime.OperatorAuth.authorize();
  await runtime.ProviderUI.adoptCatalog();
  DEFAULT_PROVIDER = runtime.UiState.DEFAULT_PROVIDER;
  bindUiStatePagehide();
  runtime.ResearchRuns.init({
    getActiveId: () => sessionState.activeId,
    getSessions: () => sessionState.sessions,
    activeSession,
    receiptSummary,
    persistActive,
    persistActiveNow,
  });
  runtime.ResearchDrawer.init({
    openDocument,
    $,
    getActiveId: () => sessionState.activeId,
    getSessionTitle: (id: string) => (sessionState.sessions.find((s) => s.id === id) || {}).title || 'Chat',
    getCurrentRun: (id: string, runId: string) =>
      runtime.ResearchRuns.forRun(id, runId) as import('./research_drawer').ResearchRunView | null,
    getResearchGraphDepth: () => Number(workspace.getTarget('research').depth) || 1,
    setResearchGraphDepth: (value: number) => {
      researchGraphDepth = value;
      workspace.target('research', { depth: String(value) });
    },
    getResearchNoteFocusId: () => workspace.getTarget('research').note || '',
    setResearchNoteFocusId: (value: string) => {
      researchNoteFocusId = value;
      workspace.target('research', { note: value });
    },
    getResearchDrawerTab: () => workspace.getTarget('research').tab || 'evidence',
    setResearchDrawerTab: (value: string) => {
      researchDrawerTab = value;
      workspace.target('research', { tab: value });
    },
    copyText,
    closeOtherDrawers,
  });
  runtime.ChangesDrawer.init({
    $,
    fetchChanges,
    fetchChangeFile,
    restoreChanges,
    escapeHtml,
    closeOtherDrawers,
  });
  runtime.LocalContextDrawer.init({
    $,
    getActiveId: () => sessionState.activeId,
    currentProjectPath: () => currentProjectPath(),
    openMenuAt,
    closeAllMenus,
    closeOtherDrawers,
    copyText,
  });
  runtime.RunDetails.init({ getActiveId: () => sessionState.activeId });
  runtime.ProviderUI.init({
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
  runtime.Sse.init({
    setStatus,
    applyRunState,
    handleServerEvent,
    refreshProviderStatus,
    acceptRun: (runId: string | null, sessionId: string | null) => {
      if (runId && sessionId) applyLifecycle({ type: 'task_start', run_id: runId, session_id: sessionId }, 'admission');
    },
    setProviderBusy: (busy: boolean) => {
      $('provider-button').disabled = !!busy;
      $('effort-button').disabled = !!busy;
    },
    updateSend,
    updateComposerContext,
  });
  runtime.Requests.init({
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
  runtime.Composer.init({
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
    projectPickerBusy: () => projectPickerBusy,
    attachCurrentChatToPickedProject,
    observeQueuedInput,
  });
  runtime.Messages.init({
    isSessionRunning,
    openDocument,
    statusRow,
    currentProjectPath,
    openChangesDrawer,
    attachCurrentChatToPickedProject,
    approveCommand,
    continueTask,
    resumeTeaching,
    getActiveId: () => sessionState.activeId,
    getRunningSessionId: () => runProjection.primaryRun?.sessionId ?? null,
    getSessions: () => sessionState.sessions,
  });
  runtime.ConversationUI.init({
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
  await runtime.Composer.refreshQueuedInputs();
  runtime.Composer.setReady();
  syncResearchUseProjectButton();
  connectEvents();
  reportDesktopReady();
}

// Transitional test bridge is opt-in so the shipped page does not expose
// mutable application state or internal handlers by default.
const testBridgeWindow = window as Window & { __TEST_BRIDGE__?: boolean };
if (testBridgeWindow.__TEST_BRIDGE__)
  Object.assign(window, {
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
    handleServerEvent,
    statusRow,
    pushMsgToSession,
    setStatus,
    updateSend,
    updateComposerContext,
  });
if (testBridgeWindow.__TEST_BRIDGE__)
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
boot().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  setStatus(message, 'warn');
  reportDesktopFailure(message);
});
