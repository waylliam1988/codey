import './operator_auth.ts';
import './render.ts';
import './process.ts';
import './messages.ts';
import './research_graph.ts';
import './research_drawer.ts';
import './research_runs.ts';
import './changes_drawer.ts';
import './local_context_drawer.ts';
import './run_details.ts';
import './models.ts';
import './model_settings.ts';
import './settings.ts';
import './provider_ui.ts';
import './ui_state.ts';
import './sse.ts';
import './requests.ts';
import './composer.ts';
import './conversation_nav.ts';
import './conversation_ui.ts';
import './domain/protocol';
import type { ChangesData, Message, Project, ResearchPayload, RestoreChangesResult, Session } from './types/domain';
import {
  decodeChangeFilePreview,
  decodeChangesData,
  decodeResearchPayload,
  decodeRunState,
  type ServerEvent,
} from './domain/protocol';
import type { RunState } from './types/domain';
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
interface ProviderFailure {
  action?: string;
  kind?: string;
  provider?: string;
  stage?: string;
  message?: string;
}
interface ServerEventData {
  type: string;
  event_id: number | string;
  cursor: number | string;
  run_id: string;
  session_id: string;
  status: string;
  providers: unknown;
  turn: number;
  note: string;
  reasoning: string;
  text: string;
  tool_id: string;
  kind: string;
  tool_name: string;
  path: string;
  activity: string;
  ok: boolean;
  changed: boolean;
  command: string;
  result: string;
  output: string;
  error_ref: string;
  id: string;
  summary: string;
  stop_reason: string;
  research: ResearchPayload;
  receipt: unknown;
  provider_failure: ProviderFailure;
  provider: string;
  turns: number;
  max_turns: number;
  mode: string;
  changes: ChangesData;
  command_sha256: string;
  command_chars: number;
  command_truncated: boolean;
  cwd: string;
  project: string;
  risk_label: string;
  risk_title: string;
  risk_detail: string;
  approved: boolean;
  exit_code: number;
  last_shell_result: ServerEventData;
  last_terminal_event: ServerEventData;
  pending_event: ServerEventData;
  busy: boolean;
  run_status: string;
  research_restore_runs: unknown;
  continuation_requested: boolean;
  continued: boolean;
  stopped: boolean;
  retry_after: number;
}
type Loose = ServerEventData;

// The existing application entry is migrated into the TypeScript bundle.
// It remains behavior-compatible while later steps replace window globals with imports.
// ============================ state & storage ============================
type AppElement = HTMLElement & { disabled: boolean };
const $ = <T extends HTMLElement = AppElement>(id: string): T => document.getElementById(id) as T;
const eventElement = (event: Event): Element | null => (event.target instanceof Element ? event.target : null);
// escapeHtml and the receipt schema helpers live in /assets/render.js.
const escapeHtml = window.Render.escapeHtml;
const receiptSummary = window.Render.receiptSummary;
const receiptChangedCount = window.Render.receiptChangedCount;
let DEFAULT_PROVIDER = window.UiState.DEFAULT_PROVIDER;
const PROVIDER_LABELS = window.UiState.PROVIDER_LABELS;
const PROVIDERS = window.UiState.PROVIDERS;
function liveDefaultProvider() {
  return window.UiState.DEFAULT_PROVIDER;
}

let sessions: Session[] = [];
let projects: Project[] = [];
let activeId = '';
let uiStateUpdatedAt = 0;
let uiStateRevision = 0;
let uiStateDirtySinceBoot = false;

const {
  uid,
  defaultSession,
  pathName,
  pathKey,
  apply: applyUiState,
  current: currentUiState,
  cache: cacheUiState,
  cached: cachedUiState,
  hasMeaningful: hasMeaningfulUiState,
  hasStored: hasStoredUiState,
  isNewer: isUiStateNewer,
  flush: flushUiState,
} = window.UiState;
const persistActive = window.UiState.persist;
const persistActiveNow = window.UiState.persistNow;
const bindUiStatePagehide = window.UiState.bindPagehide;
const restoreUiStateFromServer = window.UiState.restoreFromServer;

const bootCachedState = cachedUiState();
const bootHadMeaningfulUiState = hasMeaningfulUiState(bootCachedState);

window.UiState.init({
  getSessions: () => sessions,
  setSessions: (value: Session[]) => {
    sessions = value;
  },
  getProjects: () => projects,
  setProjects: (value: Project[]) => {
    projects = value;
  },
  getActiveId: () => activeId,
  setActiveId: (value: string) => {
    activeId = value;
  },
  getUpdatedAt: () => uiStateUpdatedAt,
  setUpdatedAt: (value: number) => {
    uiStateUpdatedAt = value;
  },
  getRevision: () => uiStateRevision,
  setRevision: (value: number) => {
    uiStateRevision = value;
  },
  isDirtySinceBoot: () => uiStateDirtySinceBoot,
  setDirtySinceBoot: (value: boolean) => {
    uiStateDirtySinceBoot = !!value;
  },
  bootHadMeaningfulUiState: () => bootHadMeaningfulUiState,
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
  return sessions.find((s) => s.id === activeId);
}
function projectById(id: string): Project | null {
  return projects.find((p) => p.id === id) || null;
}
function sessionProject(s: Session | undefined): Project | null {
  return s && s.projectId ? projectById(s.projectId) : null;
}
function activeProject() {
  return sessionProject(activeSession());
}
function currentProjectPath() {
  const p = activeProject();
  return p ? p.path : '';
}
function currentProviderId() {
  const s = activeSession();
  return s?.provider || liveDefaultProvider();
}
// Provider picker UI lives in /assets/provider_ui.js.
function syncProviderUI(providerId: string) {
  window.ProviderUI.sync(providerId);
}
function applyProviderStatus(providers: unknown, isSSE = true) {
  window.ProviderUI.applyStatus(providers, isSSE);
}
function refreshProviderStatus(immediate = false) {
  return window.ProviderUI.refreshStatus(immediate);
}

function updateComposerContext() {
  const el = $('composer-context');
  const s = activeSession();
  const p = activeProject();
  const proj = p ? p.name : 'Choose folder';
  const disabled = !!runningSessionId || window.Composer.isSending();
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
  btn.disabled = !!runningSessionId || window.Composer.isSending();
  btn.setAttribute('aria-pressed', String(active));
}
function currentIntentForSession(sessionId: string): string {
  const s = sessions.find((x) => x.id === sessionId);
  if (!s || !s.research) return 'auto';
  return sessionProjectPath(sessionId) ? 'hybrid' : 'research';
}

// ============================ sidebar rendering ============================
function renderSidebar() {
  const list = $('session-list');
  list.innerHTML = '';
  window.ConversationUI.updateSearch();

  window.ConversationUI.appendGroupLabel(list, 'PROJECTS', projects.some(window.ConversationUI.matchesProject));

  if (!projects.length && !window.ConversationUI.isSearching()) {
    const empty = document.createElement('div');
    empty.className = 'project-empty';
    empty.textContent = 'No projects';
    list.appendChild(empty);
  }

  for (const p of projects) {
    if (!window.ConversationUI.matchesProject(p)) continue;
    const node = document.createElement('div');
    node.className = 'project-node';

    const row = document.createElement('div');
    row.className = 'project-row';
    const activeS = activeSession();
    if (activeS && activeS.projectId === p.id) row.classList.add('has-active');
    row.title = p.path;

    const toggle = document.createElement('button');
    toggle.className = 'project-toggle';
    toggle.innerHTML =
      p.expanded || window.ConversationUI.isSearching()
        ? '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>'
        : '<svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>';
    toggle.onclick = (e) => {
      e.stopPropagation();
      p.expanded = !p.expanded;
      persistActive();
      renderSidebar();
    };
    row.appendChild(toggle);

    if (p.id === editingProjectId) {
      row.appendChild(
        renameInputEl(p.name || '', (val: string | undefined) => commitProjectRename(p.id, val), cancelProjectRename),
      );
    } else {
      const main = document.createElement('button');
      main.className = 'project-main';
      main.textContent = p.name;
      row.appendChild(main);

      row.onclick = () => openProject(p.id);
    }

    const more = document.createElement('button');
    more.className = 'project-more';
    more.title = '';
    more.setAttribute('aria-label', 'More');
    more.textContent = '⋯';
    more.onclick = (e) => {
      e.stopPropagation();
      openProjectMenu(e.currentTarget as HTMLElement, p.id);
    };
    row.appendChild(more);

    node.appendChild(row);

    if (p.expanded || window.ConversationUI.isSearching()) {
      const children = document.createElement('div');
      children.className = 'project-children';
      const projectSessions = sessions.filter((s) => s.projectId === p.id && window.ConversationUI.matchesSession(s));
      if (!projectSessions.length) {
        const empty = document.createElement('div');
        empty.className = 'project-empty';
        empty.textContent = 'No chats';
        children.appendChild(empty);
      } else {
        for (const s of projectSessions) children.appendChild(sessionNode(s));
      }
      node.appendChild(children);
    }
    list.appendChild(node);
  }

  const loose = sessions.filter((s) => !sessionProject(s) && window.ConversationUI.matchesSession(s));
  window.ConversationUI.appendGroupLabel(list, 'CHATS', loose.length);
  if (!loose.length && !window.ConversationUI.isSearching()) {
    const empty = document.createElement('div');
    empty.className = 'project-empty';
    empty.style.marginLeft = '10px';
    empty.textContent = 'No chats';
    list.appendChild(empty);
  }
  for (const s of loose) list.appendChild(sessionNode(s));
}

function sessionNode(s: Session) {
  const item = document.createElement('div');
  item.className = 'session-item' + (s.id === activeId ? ' active' : '');
  if (s.id === editingSessionId) {
    item.appendChild(
      renameInputEl(s.title || '', (val: string | undefined) => commitSessionRename(s.id, val), cancelSessionRename),
    );
    return item;
  }
  item.onclick = (e) => {
    if (!eventElement(e)?.closest('button')) switchSession(s.id);
  };
  const title = document.createElement('button');
  title.className = 'session-title';
  title.textContent = s.title || 'Untitled';
  title.onclick = () => switchSession(s.id);
  item.appendChild(title);
  const more = document.createElement('button');
  more.className = 'session-more';
  more.textContent = '⋯';
  more.setAttribute('aria-label', 'More');
  more.onclick = (e) => {
    e.stopPropagation();
    openSessionMenu(e.currentTarget as HTMLElement, s.id);
  };
  item.appendChild(more);
  return item;
}

// ============================ inline rename & confirm ============================
let editingSessionId = '';
let editingProjectId = '';
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
function focusRenameInput() {
  const input = document.querySelector<HTMLInputElement>('.rename-input');
  if (input) {
    input.focus();
    input.select();
  }
}
function commitSessionRename(id: string, value: string | undefined) {
  editingSessionId = '';
  const s = sessions.find((x) => x.id === id);
  if (!s) {
    renderSidebar();
    return;
  }
  const title = (value || '').trim();
  if (title && title !== s.title) {
    s.title = title.slice(0, 80);
    s.titleRenamed = true;
    persistActiveNow();
  }
  renderSidebar();
  if (id === activeId) renderChat();
}
function cancelSessionRename() {
  editingSessionId = '';
  renderSidebar();
}
function commitProjectRename(id: string, value: string | undefined) {
  editingProjectId = '';
  const p = projectById(id);
  if (!p) {
    renderSidebar();
    return;
  }
  const name = (value || '').trim();
  if (name && name !== p.name) {
    p.name = name.slice(0, 80);
    persistActiveNow();
  }
  renderSidebar();
  const current = activeProject();
  if (current && current.id === id) renderChat();
}
function cancelProjectRename() {
  editingProjectId = '';
  renderSidebar();
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
  window.ConversationUI.renderChat();
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

// Markdown / copy / tool-line helpers live in /assets/render.js (window.Render).
const messageCopyText = window.Render.messageCopyText;
const copyText = window.Render.copyText;
const addMessageCopyButton = window.Render.addMessageCopyButton;
const renderMarkdown = window.Render.renderMarkdown;
const FOLDABLE_TOOL_KINDS = window.Render.FOLDABLE_TOOL_KINDS;
const standaloneToolEl = window.Render.standaloneToolEl;
const appendOrFoldTool = window.Render.appendOrFoldTool;
const hydrateSessionIndexes = window.UiState.hydrateSessionIndexes;
const ensureSessionIndexes = window.UiState.ensureSessionIndexes;

function appendMessageNode(chat: HTMLElement, m: Message) {
  window.Messages.append(chat, m);
}

function scrollChat() {
  window.ConversationUI.scrollChat();
}

// ============================ session ops ============================
function switchSession(id: string) {
  activeId = id;
  persistActiveNow();
  handleLocalContextScopeChanged();
  renderSidebar();
  renderChat();
}
function openProject(projectId: string) {
  const p = projectById(projectId);
  if (!p) return;
  p.expanded = true;
  const first = sessions.find((s) => s.projectId === projectId);
  if (first) switchSession(first.id);
  else newSession(projectId);
}
function newSession(projectId: string | null = null) {
  const provider = currentProviderId();
  const s = defaultSession(projectId, provider);
  const previous = activeSession()?.modelSelection;
  if (previous?.connection_id === provider) {
    s.modelSelection = { ...previous!, efforts: { ...(previous!.efforts || {}) } };
  }
  sessions.unshift(s);
  activeId = s.id;
  persistActiveNow();
  handleLocalContextScopeChanged();
  renderSidebar();
  renderChat();
  fetch('/api/new_chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: s.id }),
  }).catch(() => {});
}
async function forgetSessionState(id: string): Promise<boolean> {
  try {
    const response = await fetch('/api/new_chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: id }),
    });
    return response.ok;
  } catch {
    return false;
  }
}
async function deleteSession(id: string): Promise<void> {
  if (!(await forgetSessionState(id))) return;
  const fallbackProvider = currentProviderId();
  sessions = sessions.filter((s) => s.id !== id);
  window.ConversationUI.forget(id);
  if (!sessions.length) {
    const s = defaultSession(null, fallbackProvider);
    sessions = [s];
  }
  if (!sessions.find((s) => s.id === activeId)) activeId = sessions[0].id;
  persistActiveNow();
  handleLocalContextScopeChanged();
  renderSidebar();
  renderChat();
}
function renameSession(id: string) {
  const s = sessions.find((x) => x.id === id);
  if (!s) return;
  editingProjectId = '';
  editingSessionId = id;
  if (s.projectId) {
    const p = projectById(s.projectId);
    if (p) p.expanded = true;
  }
  renderSidebar();
  focusRenameInput();
}
async function clearMessages(id: string): Promise<void> {
  const s = sessions.find((x) => x.id === id);
  if (!s) return;
  if (!(await forgetSessionState(id))) return;
  s.messages = [];
  s.terminalRuns = [];
  s.researchRuns = [];
  persistActiveNow();
  if (id === activeId) {
    closeLocalContextDrawer();
    renderChat();
  }
}
function renameProject(id: string) {
  const p = projectById(id);
  if (!p) return;
  editingSessionId = '';
  editingProjectId = id;
  renderSidebar();
  focusRenameInput();
}
async function deleteProject(id: string): Promise<void> {
  const p = projectById(id);
  if (!p) return;
  const cleared = await Promise.all(sessions.filter((s) => s.projectId === id).map((s) => forgetSessionState(s.id)));
  if (!cleared.every(Boolean)) return;
  projects = projects.filter((x) => x.id !== id);
  sessions.filter((s) => s.projectId === id).forEach((s) => window.ConversationUI.forget(s.id));
  sessions = sessions.filter((s) => s.projectId !== id);
  if (!sessions.length) sessions = [defaultSession(null, liveDefaultProvider())];
  if (!sessions.find((s) => s.id === activeId)) activeId = sessions[0].id;
  persistActiveNow();
  handleLocalContextScopeChanged();
  renderSidebar();
  renderChat();
}
function ensureProject(path: string, name: string) {
  const existing = projects.find((p) => pathKey(p.path) === pathKey(path));
  if (existing) {
    existing.expanded = true;
    return existing;
  }
  const p = {
    id: uid(),
    name: name || pathName(path),
    path,
    expanded: true,
    createdAt: Date.now(),
  };
  projects.unshift(p);
  persistActive();
  return p;
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
function attachSessionToProject(projectId: string | null, sessionId: string = activeId) {
  const s = sessions.find((x) => x.id === sessionId);
  const p = projectId ? projectById(projectId) : null;
  if (!s || !p || sessionProject(s)) return false;
  s.projectId = p.id;
  s.research = false;
  p.expanded = true;
  persistActiveNow();
  if (sessionId === activeId) handleLocalContextScopeChanged();
  renderSidebar();
  if (sessionId === activeId) renderChat();
  else updateComposerContext();
  return true;
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
  if (runningSessionId) return;
  const sid = activeId;
  const s = sessions.find((x) => x.id === sid);
  if (!s || sessionProject(s)) return;
  const data = await pickProjectPath('new', currentProjectPath());
  if (!data) return;
  if (typeof data.path !== 'string') return;
  const p = ensureProject(data.path, typeof data.name === 'string' ? data.name : data.path);
  if (!attachSessionToProject(p.id, sid)) return;
}

function pushMsgToSession(sid: string, m: Message) {
  if (sid === activeId) window.ConversationUI.captureView();
  const s = sessions.find((x) => x.id === sid);
  if (!s) return false;
  const wasEmpty = s.messages.length === 0;
  s.messages.push(m);
  if (
    s.title === 'New chat' &&
    !s.titleRenamed &&
    sid !== editingSessionId &&
    !window.ConversationUI.isRenamingSession(sid) &&
    m.type === 'user' &&
    m.text
  ) {
    s.title = m.text.slice(0, 28) + (m.text.length > 28 ? '…' : '');
    persistActiveNow();
    renderSidebar();
    if (sid === activeId) renderChat();
    return true;
  }
  persistActiveNow();
  if (sid === activeId) {
    if (wasEmpty) renderChat();
    else {
      appendMessageNode($('chat'), m);
      scrollChat();
    }
  }
  return true;
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
  window.ConversationUI.menuOpened(menu, anchor);
}
function closeAllMenus() {
  disarmDangerButtons();
  document.querySelectorAll('.ctx-menu.open').forEach((m) => m.classList.remove('open'));
  window.ConversationUI.menusClosed();
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
    armDanger(btn, () => clearMessages(activeId));
    return;
  }
  closeAllMenus();
  if (act === 'local-context') openLocalContextDrawer();
});
document.addEventListener('click', (e) => {
  if (eventElement(e)?.closest('.ctx-menu')) return;
  if (eventElement(e)?.closest('.project-more, .session-more, #topbar-more')) return;
  closeAllMenus();
});

// ============================ SSE event ingestion ============================
let runningSessionId: string | null = null;
let runningRunId: string | null = null;
let turnCounter = 0;

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

const sseEventKey = (data: ServerEvent, fallback = '') => window.Sse.eventKey(data, fallback);

function handleServerEvent(raw: ServerEvent) {
  const data = raw as ServerEventData;
  const runId = (data.run_id || '').toString();
  const sid = data.session_id || '';
  if (data.type === 'task_done' && (!runId || runId === runningRunId)) {
    runningRunId = null;
    runningSessionId = null;
    setStatus('', 'ok');
    updateSend();
    updateComposerContext();
    syncResearchUseProjectButton();
  }
  if (!window.Requests.acceptsEvent(data)) return;
  const mayOutliveRun = [
    'task_start',
    'task_done',
    'shell_request',
    'shell_result',
    'teach_request',
    'resync_required',
  ];
  if (runId && runningRunId && !mayOutliveRun.includes(data.type) && runId !== runningRunId) return;
  // Server dropped queued events for this client: re-pull authoritative state.
  if (data.type === 'resync_required') {
    reconcileRunState();
    refreshProviderStatus(true);
    return;
  }
  if (data.type === 'status') {
    window.Sse.displayRunStatus(data.status);
    return;
  }
  if (data.type === 'providers') {
    applyProviderStatus(data.providers);
    return;
  }
  if (data.type === 'turn') {
    addToSession(sid, {
      type: 'turn',
      n: data.turn || 0,
      note: (data.note || '').toString(),
      reasoning: typeof data.reasoning === 'string' ? data.reasoning : '',
      runId,
      eventKey: sseEventKey(data, runId ? `turn:${runId}:${data.turn || 0}` : ''),
    });
    return;
  }
  if (data.type === 'reasoning') {
    if (typeof data.text === 'string' && data.text.trim())
      addToSession(sid, {
        type: 'thinking',
        text: data.text,
        turn: data.turn || 0,
        runId,
        eventKey: sseEventKey(data, `reasoning:${runId}:${data.turn || 0}`),
      });
    return;
  }
  if (data.type === 'tool_started') {
    const rawToolId = (data.tool_id || '').toString();
    const toolKey = rawToolId ? `${runId}:${rawToolId}` : '';
    const s = sessions.find((x) => x.id === sid);
    if (s && toolKey) {
      ensureSessionIndexes(s);
      if (s._toolKeys!.has(toolKey)) return;
    }
    addToSession(sid, {
      type: 'tool_pending',
      toolKey,
      eventKey: toolKey ? `tool:${toolKey}:start` : '',
      kind: data.kind || '',
      toolName: data.tool_name || '',
      turn: data.turn || 0,
      path: data.path || '',
      activity: (data.activity || '').toString().slice(0, 200),
      pending: true,
      runId,
    });
    return;
  }
  if (data.type === 'tool') {
    const rawToolId = (data.tool_id || '').toString();
    const toolKey = rawToolId ? `${runId}:${rawToolId}` : '';
    const toolStatus = (data.status || (data.ok === false ? 'error' : 'ok')).toString();
    const message = {
      type: 'tool',
      toolKey,
      eventKey: toolKey ? `tool:${toolKey}:final` : '',
      kind: data.kind || '',
      toolName: data.tool_name || '',
      turn: data.turn || 0,
      changed: data.changed === true,
      command: data.command || '',
      path: data.path || '',
      result: (data.result || '').toString().slice(0, 200),
      status: toolStatus,
      error: toolStatus === 'error',
      runId,
    };
    if (
      toolKey &&
      replaceSessionMessage(sid, (item) => item.type === 'tool_pending' && item.toolKey === toolKey, message)
    )
      return;
    addToSession(sid, message);
    return;
  }
  if (data.type === 'info') {
    if (data.text) addToSession(sid, { type: 'info', text: data.text, runId, eventKey: sseEventKey(data) });
    return;
  }
  if (data.type === 'ghost_post_turn_warning') {
    if (window.Requests.warning(sid, runId)) return;
    addToSession(sid, {
      type: 'info',
      text: 'Local update paused',
      sessionId: sid,
      runId,
      eventKey: sseEventKey(data, runId ? `local-health:${runId}:${data.error_ref || ''}` : ''),
    });
    return;
  }
  if (data.type === 'teach_request') {
    removeTeachMessage(sid, data.id);
    addToSession(sid, {
      type: 'teach',
      id: data.id,
      text: data.text,
      sessionId: sid,
      runId,
      eventKey: `teach:${data.id}`,
    });
    return;
  }
  if (data.type === 'task_start') {
    window.Requests.running(sid, runId);
    runningRunId = runId;
    runningSessionId = sid;
    turnCounter = 0;
    window.Sse.displayRunStatus('running');
    $('send').disabled = true;
    $('provider-button').disabled = true;
    updateSend();
    updateComposerContext();
    syncResearchUseProjectButton();
    addToSession(sid, { type: 'run_state', state: 'running', runId, eventKey: `run-start:${runId}` });
    return;
  }
  if (data.type === 'task_done') {
    if (!markTerminalRun(sid, runId)) return;
    const summary = (data.summary || '').toString();
    const reason = (data.stop_reason || 'done').toString();
    const requestFailed = window.Requests.finish(data);
    addToSession(sid, { type: 'run_state', state: reason, runId, eventKey: `run-end:${runId}` });
    const terminalKey = runId ? `terminal:${runId}` : '';
    const answerKey = terminalKey ? `${terminalKey}:answer` : '';
    const receiptKey = terminalKey ? `${terminalKey}:receipt` : '';
    const project = sessionProjectPath(sid);
    let shownReceipt = false;
    const researchRun = data.research ? recordResearchRun(sid, runId, data.research, data.receipt) : null;
    if (reason === 'error' && data.provider_failure && data.provider_failure.action === 'connect') {
      if (data.provider && PROVIDERS.includes(data.provider)) {
        applyProviderStatus([{ id: data.provider, available: false }]);
      }
      if (!requestFailed) addSendError(sid, terminalKey, runId);
      flushUiState();
      return;
    }
    if (requestFailed) {
      // The request's status position already contains this attempt's failure.
    } else if (summary.startsWith('ERROR:')) {
      addToSession(sid, {
        type: 'err',
        text: summary.replace(/^ERROR:\s*/i, ''),
        sessionId: sid,
        runId,
        eventKey: terminalKey,
      });
    } else if (reason === 'max_turns') {
      const turns = data.turns || data.max_turns || 0;
      const label = turns ? `Reached turn limit (${turns})` : 'Reached turn limit';
      addToSession(sid, { type: 'limit', text: label, sessionId: sid, runId, eventKey: terminalKey });
    } else if (reason === 'no_progress') {
      addToSession(sid, {
        type: 'pause',
        text: 'Paused after several turns without new file writes',
        sessionId: sid,
        runId,
        eventKey: terminalKey,
      });
    } else if (reason === 'approval') {
      // shell approval card is emitted separately
    } else if (reason === 'stopped') {
      const turns = data.turns || 0;
      const label = turns ? `Stopped after ${turns} turn${turns === 1 ? '' : 's'}` : 'Stopped';
      addToSession(sid, { type: 'pause', text: label, sessionId: sid, runId, eventKey: terminalKey });
    } else if (reason === 'error') {
      const text = summary || 'Task failed';
      addToSession(sid, { type: 'err', text, sessionId: sid, runId, eventKey: terminalKey });
    } else if (reason === 'done') {
      if (data.mode === 'research') {
        if (summary) addToSession(sid, { type: 'asst', text: summary, runId, eventKey: answerKey });
        if (receiptSummary(data.receipt)) {
          addToSession(sid, {
            type: 'research_done',
            text: receiptSummary(data.receipt),
            runId,
            sessionId: sid,
            eventKey: receiptKey,
          });
          shownReceipt = true;
        }
      } else if (data.mode === 'chat') {
        if (summary) addToSession(sid, { type: 'asst', text: summary, runId, eventKey: answerKey });
      } else if (project && summary) {
        addToSession(sid, { type: 'asst', text: summary, runId, eventKey: answerKey });
      }
      const changes = data.changes || {};
      const changed = !!data.changed;
      const changedCount = receiptChangedCount(data.receipt, changes);
      if (changed && changedCount > 0 && receiptSummary(data.receipt)) {
        addToSession(sid, {
          type: 'done',
          text: receiptSummary(data.receipt),
          project: changes.project || project,
          changed: true,
          runId,
          sessionId: sid,
          eventKey: receiptKey,
        });
        shownReceipt = true;
      } else if (!project && summary && data.mode !== 'research' && data.mode !== 'chat') {
        addToSession(sid, { type: 'done', text: summary, sessionId: sid, runId, eventKey: terminalKey });
      }
    } else if (summary) {
      addToSession(sid, { type: 'done', text: summary, sessionId: sid, runId, eventKey: terminalKey });
    }
    if (reason !== 'error' && reason !== 'approval' && data.changed && !shownReceipt)
      maybeAddChangesSummary(sid, terminalKey, runId);
    flushUiState();
    return;
  }
  if (data.type === 'shell_request') {
    addToSession(sid, {
      type: 'shell_request',
      id: data.id,
      command: data.command,
      commandSha256: data.command_sha256,
      commandChars: data.command_chars,
      commandTruncated: !!data.command_truncated,
      cwd: data.cwd,
      project: data.project,
      riskLabel: data.risk_label,
      riskTitle: data.risk_title,
      riskDetail: data.risk_detail,
      runId,
      eventKey: `shell:${data.id}`,
    });
    return;
  }
  if (data.type === 'shell_result') {
    removeShellRequest(sid, data.id);
    addToSession(sid, {
      type: 'shell_result',
      id: data.id,
      approved: !!data.approved,
      shellStatus: data.status || '',
      command: data.command,
      commandSha256: data.command_sha256,
      commandChars: data.command_chars,
      commandTruncated: !!data.command_truncated,
      cwd: data.cwd,
      output: data.output,
      exitCode: data.exit_code,
      runId,
      eventKey: `shell-result:${data.id}`,
    });
    return;
  }
  if (data.type === 'reply') {
    const answerKey = runId ? `terminal:${runId}:answer` : '';
    if (data.text) addToSession(sid, { type: 'asst', text: data.text, runId, eventKey: answerKey });
    return;
  }
  if (data.type === 'review') {
    if (data.text)
      addToSession(sid, { type: 'review', text: data.text, sessionId: sid, runId, eventKey: sseEventKey(data) });
    return;
  }
}

let researchDrawerTab = 'evidence';
let researchGraphDepth = 1;
let researchNoteFocusId = '';

function recordResearchRun(sessionId: string, runId: string, research: unknown, receipt: unknown) {
  const payload: ResearchPayload = decodeResearchPayload(research);
  const item = window.ResearchRuns.record(sessionId, runId, payload, receipt);
  if (!item) return null;
  researchDrawerTab = 'evidence';
  researchNoteFocusId = '';
  window.ResearchDrawer.invalidateNoteCache(window.ResearchDrawer.noteIdsForRun(item));
  return item;
}

function currentResearchRun(sessionId = activeId) {
  return window.ResearchRuns.current(sessionId);
}

async function restoreResearchRun() {
  const restore = $('research-restore');
  if (restore) restore.disabled = true;
  const ownerSessionId = activeId;
  try {
    const result = await window.ResearchRuns.restore(ownerSessionId);
    if (!result) return;
    const subtitle = activeId === ownerSessionId ? $('research-subtitle') : null;
    if (subtitle) subtitle.textContent = String(result.message || '');
    if (result.run) {
      window.ResearchDrawer.invalidateNoteCache(window.ResearchDrawer.noteIdsForRun(result.run));
    }
    if (result.ok) {
      addToSession(result.ownerSessionId, {
        type: 'done',
        text: `Research restored: ${result.restored.length} files`,
        eventKey: `research-restore:${String(result.run.runId || '')}`,
      });
    }
    if (activeId === result.ownerSessionId) window.ResearchDrawer.render(activeId);
  } finally {
    const current = currentResearchRun();
    if (restore) restore.disabled = !current || !current.restoreable;
  }
}

function markResearchRestoreAvailability(runIds: unknown) {
  const changed = window.ResearchRuns.markRestoreAvailability(runIds);
  const drawer = $('research-drawer');
  if (changed && drawer && drawer.classList.contains('open')) {
    window.ResearchDrawer.render(activeId);
  }
}

function applyRunState(data: RunState) {
  if (data.last_shell_result) handleServerEvent(data.last_shell_result);
  if (data.last_terminal_event) handleServerEvent(data.last_terminal_event);
  if (data.pending_event) handleServerEvent(data.pending_event);

  const busy = !!data.busy && !!data.run_id;
  runningRunId = busy ? (data.run_id ?? null) : null;
  runningSessionId = busy ? (data.session_id ?? null) : null;
  if (busy) {
    window.Sse.displayRunStatus(data.run_status === 'connecting' ? 'connecting' : 'running');
  } else {
    setStatus('', 'ok');
  }
  $('provider-button').disabled = $('effort-button').disabled = busy;
  markResearchRestoreAvailability(data.research_restore_runs || []);
  window.Requests.reconcile({
    session_id: data.session_id ?? '',
    run_id: data.run_id ?? '',
    busy: !!data.busy,
  });
  updateSend();
  updateComposerContext();
  syncResearchUseProjectButton();
}

function reconcileRunState() {
  return window.Sse.reconcileRunState();
}

async function acceptRunResponse(response: Response, sessionId: string) {
  return window.Sse.acceptRunResponse(response, sessionId);
}

function connectEvents() {
  return window.Sse.connect();
}

function addToSession(sid: string, m: Message) {
  return window.Requests.addToSession(sid, m);
}

function replaceSessionMessage(sid: string, predicate: (message: Message) => boolean, message: Message) {
  if (sid === activeId) window.ConversationUI.captureView();
  const s = sessions.find((x) => x.id === sid);
  if (!s) return false;
  ensureSessionIndexes(s);
  if (message.eventKey && s._eventKeys!.has(message.eventKey)) return false;
  const index = s.messages.findIndex(predicate);
  if (index < 0) return false;
  s.messages[index] = message;
  window.UiState.trackSessionMessage(s, message);
  persistActive();
  if (sid === activeId) {
    if (window.Process.replace($('chat'), message)) {
      scrollChat();
      return true;
    }
    const existingNode =
      message.toolKey &&
      (Array.from($('chat').children) as HTMLElement[]).find(
        (el) => el.dataset && el.dataset.toolKey === message.toolKey,
      );
    if (existingNode) {
      const temp = document.createElement('div');
      appendMessageNode(temp, message);
      if (temp.firstElementChild) {
        existingNode.replaceWith(temp.firstElementChild);
        scrollChat();
        return true;
      }
    }
    renderChat();
    scrollChat();
  }
  return true;
}

function markTerminalRun(sid: string, runId: string | null) {
  if (!runId) return true;
  const s = sessions.find((x) => x.id === sid);
  if (!s) return false;
  ensureSessionIndexes(s);
  if (s._terminalRunSet!.has(runId)) return false;
  s.terminalRuns = [...(s.terminalRuns || []), runId].slice(-32);
  hydrateSessionIndexes(s);
  persistActive();
  return true;
}

function dropSessionMessages(sessionId: string, pred: (message: Message) => boolean) {
  const s = sessions.find((x) => x.id === sessionId);
  if (!s) return;
  const before = s.messages.length;
  s.messages = s.messages.filter((m) => !pred(m));
  if (s.messages.length === before) return;
  hydrateSessionIndexes(s);
  persistActive();
  if (sessionId === activeId) renderChat();
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
  const s = sessions.find((x) => x.id === sid);
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

// Changes drawer UI lives in /assets/changes_drawer.js; these thin wrappers
// keep existing call sites and event handlers stable.
function openChangesDrawer(project: string) {
  return window.ChangesDrawer.open(project);
}

function closeChangesDrawer() {
  window.ChangesDrawer.close();
}

function openLocalContextDrawer() {
  window.LocalContextDrawer.open();
}

function closeLocalContextDrawer() {
  window.LocalContextDrawer.close();
}

function handleLocalContextScopeChanged() {
  closeChangesDrawer();
  window.ResearchDrawer.close();
  if (window.LocalContextDrawer) window.LocalContextDrawer.handleScopeChanged();
}

function closeOtherDrawers(active: string | undefined) {
  if (active !== 'changes') closeChangesDrawer();
  if (active !== 'research') window.ResearchDrawer.close();
  if (active !== 'local_context') closeLocalContextDrawer();
}

function showCommandApprovalError(errorEl: HTMLElement | null) {
  if (errorEl) {
    errorEl.textContent = 'Could not send approval';
    errorEl.hidden = false;
  }
  setStatus('Could not send approval', 'err');
}

function showCommandContinuationError(errorEl: HTMLElement | null, retryAfterSeconds = 15) {
  const retryAfter = Number(retryAfterSeconds) || 15;
  const message = `Run slot busy; retry after ${retryAfter}s`;
  if (errorEl) {
    errorEl.textContent = message;
    errorEl.hidden = false;
  }
  setStatus(message, 'warn');
}

async function approveCommand(
  id: string,
  approved: boolean,
  approveBtn: HTMLButtonElement,
  denyBtn: HTMLButtonElement,
  errorEl: HTMLElement,
) {
  if (!id) return;
  approveBtn.disabled = true;
  denyBtn.disabled = true;
  if (errorEl) {
    errorEl.textContent = '';
    errorEl.hidden = true;
  }
  try {
    const r = await fetch('/api/shell_approval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, approved }),
    });
    if (!r.ok) {
      showCommandApprovalError(errorEl);
      approveBtn.disabled = false;
      denyBtn.disabled = false;
      return;
    }
    const data = await r.json();
    if (data.event) handleServerEvent(data.event);
    if (approved && data.continuation_requested && !data.continued && !data.stopped) {
      showCommandContinuationError(errorEl, data.retry_after);
    }
    await reconcileRunState();
  } catch (err) {
    showCommandApprovalError(errorEl);
    approveBtn.disabled = false;
    denyBtn.disabled = false;
  }
}

// ============================ composer ============================
function resizeTask() {
  return window.Composer.resizeTask();
}
function updateSend() {
  return window.Composer.updateSend();
}

function toggleResearchForActive() {
  return window.Composer.toggleResearchForActive();
}

// Local provider config popover lives in /assets/provider_ui.js.
function openLocalProviderConfig() {
  return window.ProviderUI.openLocalConfig();
}

function closeLocalProviderConfig() {
  window.ProviderUI.closeLocalConfig();
}

function setActiveProvider(id: string) {
  return window.Composer.setActiveProvider(id);
}
// Provider menu handlers are bound by window.ProviderUI.init().

function continueTask(sessionId: string) {
  return window.Composer.continueTask(sessionId);
}
$('changes-close').onclick = closeChangesDrawer;
const researchClose = $('research-close');
if (researchClose) researchClose.onclick = () => window.ResearchDrawer.close();
const researchRestore = $('research-restore');
if (researchRestore) researchRestore.onclick = restoreResearchRun;
function syncResearchUseProjectButton() {
  const btn = $('research-use-project');
  if (!btn) return;
  const s = activeSession();
  const busy = !!runningSessionId;
  const unusable = busy || !s || !!sessionProject(s);
  btn.disabled = unusable;
  btn.title = busy ? 'Busy' : !s ? 'No chat' : sessionProject(s) ? 'Already in a project' : '';
}
$('research-use-project').onclick = () => attachCurrentChatToPickedProject();
$('btn-new-chat').onclick = () => {
  const p = activeProject();
  newSession(p ? p.id : null);
};
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
    const p = activeProject();
    newSession(p ? p.id : null);
    return;
  }
  if (e.key === 'Escape') {
    if (eventElement(e)?.closest('.rename-input')) return;
    closeAllMenus();
    closeOtherDrawers(undefined);
    window.ProviderUI.closeMenu($('provider-menu').classList.contains('open'));
    if (window.ProviderUI) window.ProviderUI.closeLocalConfig();
  }
});
// ============================ boot ============================
async function boot() {
  await window.OperatorAuth.authorize();
  await window.ProviderUI.adoptCatalog();
  DEFAULT_PROVIDER = window.UiState.DEFAULT_PROVIDER;
  bindUiStatePagehide();
  window.ResearchRuns.init({
    getActiveId: () => activeId,
    getSessions: () => sessions,
    activeSession,
    receiptSummary,
    persistActive,
    persistActiveNow,
  });
  window.ResearchDrawer.init({
    $,
    getActiveId: () => activeId,
    getSessionTitle: (id: string) => (sessions.find((s) => s.id === id) || {}).title || 'Chat',
    getCurrentRun: (id: string) => currentResearchRun(id) as import('./research_drawer').ResearchRunView | null,
    getResearchGraphDepth: () => researchGraphDepth,
    setResearchGraphDepth: (value: number) => {
      researchGraphDepth = value;
    },
    getResearchNoteFocusId: () => researchNoteFocusId,
    setResearchNoteFocusId: (value: string) => {
      researchNoteFocusId = value;
    },
    getResearchDrawerTab: () => researchDrawerTab,
    setResearchDrawerTab: (value: string) => {
      researchDrawerTab = value;
    },
    copyText,
    closeOtherDrawers,
  });
  window.ChangesDrawer.init({
    $,
    fetchChanges,
    fetchChangeFile,
    restoreChanges,
    escapeHtml,
    closeOtherDrawers,
  });
  window.LocalContextDrawer.init({
    $,
    getActiveId: () => activeId,
    currentProjectPath: () => currentProjectPath(),
    openMenuAt,
    closeAllMenus,
    closeOtherDrawers,
    copyText,
  });
  window.RunDetails.init({ getActiveId: () => activeId });
  window.ProviderUI.init({
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
  window.Sse.init({
    setStatus,
    applyRunState,
    handleServerEvent,
    refreshProviderStatus,
    acceptRun: (runId: string | null, sessionId: string | null) => {
      runningRunId = runId || null;
      runningSessionId = runningRunId ? sessionId : null;
    },
    setProviderBusy: (busy: boolean) => {
      $('provider-button').disabled = !!busy;
      $('effort-button').disabled = !!busy;
    },
    updateSend,
    updateComposerContext,
  });
  window.Requests.init({
    $,
    getSessions: () => sessions,
    getActiveId: () => activeId,
    getRunningSessionId: () => runningSessionId,
    persistActiveNow,
    persistActive,
    pushMsgToSession,
    addToSession,
    appendMessageNode,
    renderChat,
    scrollChat,
    statusRow,
  });
  window.Composer.init({
    $,
    PROVIDERS,
    getActiveId: () => activeId,
    getRunningSessionId: () => runningSessionId,
    activeSession,
    findSession: (sessionId: string | null | undefined) =>
      sessionId ? sessions.find((x) => x.id === sessionId) : undefined,
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
  });
  window.Messages.init({
    statusRow,
    currentProjectPath,
    openChangesDrawer,
    attachCurrentChatToPickedProject,
    approveCommand,
    continueTask,
    resumeTeaching,
    getActiveId: () => activeId,
    getRunningSessionId: () => runningSessionId,
  });
  window.ConversationUI.init({
    activeSession,
    activeProject,
    getSessions: () => sessions,
    getProjects: () => projects,
    getActiveId: () => activeId,
    getRunningSessionId: () => runningSessionId,
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
  $('task').disabled = false;
  updateSend();
  syncResearchUseProjectButton();
  connectEvents();
}

// Transitional test and embedding bridge. Product code uses the module entry;
// existing Playwright fixtures can still inspect live state while they migrate
// to user-facing actions and typed test helpers.
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
Object.defineProperties(window, {
  sessions: { configurable: true, get: () => sessions },
  projects: { configurable: true, get: () => projects },
  activeId: { configurable: true, get: () => activeId },
  runningSessionId: {
    configurable: true,
    get: () => runningSessionId,
    set: (value) => {
      runningSessionId = value;
    },
  },
  runningRunId: {
    configurable: true,
    get: () => runningRunId,
    set: (value) => {
      runningRunId = value;
    },
  },
});
boot().catch((error) => setStatus(error.message, 'warn'));
