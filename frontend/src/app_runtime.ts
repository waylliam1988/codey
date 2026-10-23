import { mergeCommandResult } from './command_result';
import { DocumentPane, type DocumentRef } from './document_pane';
import { Workspace } from './workspace';
import { RecordedPanels } from './recorded_panels';
import { HistoryPanel } from './history_panel';
import { ChatFork } from './chat_fork';
import './chat_fork.css';
import { mergeForkState } from './fork_state';
import { CommandPalette } from './command_palette';
import { iconButton } from './icon_button';
import './workspace.css';
import { hydrateDocumentContent } from './document_content';
import { streamMessage, finalStreamMessage } from './reply_stream';
import { reportDesktopFailure, reportDesktopReady } from './desktop_ready';
import { FrameCoalescer } from './frame_coalescer';
import { terminalAnswerMessage } from './terminal_answer';
import {
  RunLifecycleProjection,
  eventSequence,
  lifecycleState,
  lastRunState,
  type LifecycleInput,
} from './run_lifecycle';
import type { QueuedInput } from './input_outbox';
import { UserQuestions, type UserQuestion } from './user_questions';
import { createApprovalController } from './shell_approvals';
import { showCommandPermissions } from './command_permissions';
import { runtime } from './runtime';
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
import type { ChangesData, Message, Project, ResearchPayload, RestoreChangesResult, Session } from './types/domain';
import {
  decodeChangeFilePreview,
  decodeChangesData,
  decodeResearchPayload,
  decodeRunState,
  decodeServerEvent,
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
  preview?: import('./render').ToolPreview;
  elapsed_ms?: number;
  stream_id: string;
  sequence: number;
  state: string;
  type: string;
  event_id: number | string;
  cursor: number | string;
  run_id: string;
  execution_id: string;
  session_id: string;
  status: string;
  providers: unknown;
  turn: number;
  note: string;
  reasoning: string;
  text: string;
  tool_id: string;
  call_id: string;
  parent_call_id: string;
  output_bytes: number;
  output_stored_bytes: number;
  kind: string;
  tool_name: string;
  path: string;
  activity: string;
  ok: boolean;
  changed: boolean;
  command: string;
  result: string;
  detail: string;
  detail_truncated: boolean;
  output: string;
  truncated: boolean;
  error_ref: string;
  error_type: string;
  stage: string;
  id: string;
  summary: string;
  stop_reason: string;
  final_delivery: string;
  research: ResearchPayload;
  receipt: unknown;
  provider_failure: ProviderFailure;
  failure: import('./failure_presentation').FailureInfo;
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
  project_rule_available?: boolean;
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
workspace.register('files', 'Files', documentPane.element, () => documentPane.show(activeId));
workspace.register('changes', 'Changes', $('changes-drawer'), (target) => {
  const project = sessionProjectPath(activeId);
  if (project) void runtime.ChangesDrawer.open(project, target.path || '');
  else {
    $('changes-body').textContent = 'Choose a project to inspect workspace changes.';
    $('changes-subtitle').textContent = 'No project';
    runtime.UiState.setDrawerOpen('changes-drawer', true);
  }
});
workspace.register('research', 'Research', $('research-drawer'), (target) => {
  const runId = target.runId || currentResearchRun(activeId)?.runId;
  if (runId) runtime.ResearchDrawer.open(activeId, runId);
  else {
    $('research-body').textContent = 'No research recorded for this conversation.';
    $('research-subtitle').textContent = 'No research yet';
    runtime.UiState.setDrawerOpen('research-drawer', true);
  }
});
const recordedPanels = new RecordedPanels({
  sessionId: () => activeId,
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
  const state = mergeForkState(result.state, baseline, current);
  runtime.UiState.apply(state);
  runtime.UiState.cache();
  if (follow) switchSession(result.session_id);
  else {
    renderSidebar();
    renderChat();
    persistActive();
  }
}
const chatFork = new ChatFork({
  activeId: () => activeId,
  isWorktree: (id) => !!sessions.find((session) => session.id === id)?.projectId?.startsWith('worktree_fork_'),
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
    label: 'Saved command permissions',
    run: () => {
      void showCommandPermissions('', '', true);
    },
  },
]);
$('btn-command-permissions')?.addEventListener('click', () => {
  void showCommandPermissions('', '', true);
});
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
      activeId,
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
function liveDefaultProvider() {
  return runtime.UiState.DEFAULT_PROVIDER;
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
} = runtime.UiState;
const persistActive = runtime.UiState.persist;
const persistActiveNow = runtime.UiState.persistNow;
const bindUiStatePagehide = runtime.UiState.bindPagehide;
const restoreUiStateFromServer = runtime.UiState.restoreFromServer;

const bootCachedState = cachedUiState();
const bootHadMeaningfulUiState = hasMeaningfulUiState(bootCachedState);

runtime.UiState.init({
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
  const disabled = runningSessionId === activeId || runtime.Composer.isSending(activeId);
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
  btn.disabled = runningSessionId === activeId || runtime.Composer.isSending(activeId);
  btn.setAttribute('aria-pressed', String(active));
}
function currentIntentForSession(sessionId: string): string {
  const s = sessions.find((x) => x.id === sessionId);
  if (!s || !s.research) return 'auto';
  return sessionProjectPath(sessionId) ? 'hybrid' : 'research';
}

// ============================ sidebar rendering ============================
const expandedProjectChats = new Set<string>();
const PROJECT_CHAT_PREVIEW = 5;
function renderSidebar() {
  const list = $('session-list');
  const previousArrows = new Map(
    Array.from(list.querySelectorAll<HTMLElement>('[data-project-toggle]')).map((button) => [
      button.dataset.projectToggle,
      button.getAttribute('aria-expanded') === 'true',
    ]),
  );
  list.innerHTML = '';
  runtime.ConversationUI.updateSearch();

  runtime.ConversationUI.appendGroupLabel(list, 'PROJECTS', projects.some(runtime.ConversationUI.matchesProject));

  if (!projects.length && !runtime.ConversationUI.isSearching()) {
    const empty = document.createElement('div');
    empty.className = 'project-empty';
    empty.textContent = 'No projects';
    list.appendChild(empty);
  }

  for (const p of projects) {
    if (!runtime.ConversationUI.matchesProject(p)) continue;
    const node = document.createElement('div');
    node.className = 'project-node';

    const row = document.createElement('div');
    row.className = 'project-row';
    const activeS = activeSession();
    if (activeS && activeS.projectId === p.id) row.classList.add('has-active');
    row.title = p.path;

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'project-toggle';
    toggle.dataset.projectToggle = p.id;
    const expanded = p.expanded || runtime.ConversationUI.isSearching();
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} ${p.name}`);
    const chevron = document.createElement('span');
    chevron.className = 'disclosure-chevron' + (expanded ? '' : ' is-collapsed');
    chevron.setAttribute('aria-hidden', 'true');
    toggle.append(chevron);
    toggle.onclick = (e) => {
      e.stopPropagation();
      p.expanded = !p.expanded;
      persistActive();
      renderSidebar();
      list
        .querySelector<HTMLButtonElement>(`[data-project-toggle="${CSS.escape(p.id)}"]`)
        ?.focus({ preventScroll: true });
    };
    row.appendChild(toggle);

    if (p.id === editingProjectId) {
      row.appendChild(
        renameInputEl(p.name || '', (val: string | undefined) => commitProjectRename(p.id, val), cancelProjectRename),
      );
    } else {
      const main = document.createElement('button');
      main.type = 'button';
      main.className = 'project-main';
      main.textContent = p.name;
      row.appendChild(main);

      row.onclick = () => openProject(p.id);
    }

    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'project-more';
    more.setAttribute('aria-label', 'More');
    more.textContent = '⋯';
    more.onclick = (e) => {
      e.stopPropagation();
      openProjectMenu(e.currentTarget as HTMLElement, p.id);
    };
    row.appendChild(more);
    const create = document.createElement('button');
    create.className = 'project-new';
    create.type = 'button';
    create.setAttribute('aria-label', `New chat in ${p.name}`);
    create.innerHTML = '<span class="new-chat-icon" aria-hidden="true"></span>';
    create.onclick = (event) => {
      event.stopPropagation();
      p.expanded = true;
      newSession(p.id);
    };
    row.appendChild(create);

    node.appendChild(row);

    if (p.expanded || runtime.ConversationUI.isSearching()) {
      const children = document.createElement('div');
      children.className = 'project-children';
      const projectSessions = sessions.filter((s) => s.projectId === p.id && runtime.ConversationUI.matchesSession(s));
      if (!projectSessions.length) {
        const empty = document.createElement('div');
        empty.className = 'project-empty';
        empty.textContent = 'No chats';
        children.appendChild(empty);
      } else {
        const searching = runtime.ConversationUI.isSearching();
        const expanded = expandedProjectChats.has(p.id);
        const preview = projectSessions.slice(0, PROJECT_CHAT_PREVIEW);
        const active = projectSessions.find((session) => session.id === activeId);
        if (active && !preview.includes(active)) preview[preview.length - 1] = active;
        for (const session of searching || expanded ? projectSessions : preview)
          children.appendChild(sessionNode(session));
        if (!searching && projectSessions.length > PROJECT_CHAT_PREVIEW) {
          const reveal = document.createElement('button');
          reveal.type = 'button';
          reveal.className = 'project-reveal';
          reveal.textContent = expanded ? 'Show less' : 'Show more';
          reveal.setAttribute('aria-expanded', String(expanded));
          reveal.onclick = () => {
            if (expanded) expandedProjectChats.delete(p.id);
            else expandedProjectChats.add(p.id);
            renderSidebar();
            list.querySelector<HTMLButtonElement>(`[data-project-reveal="${CSS.escape(p.id)}"]`)?.focus();
          };
          reveal.dataset.projectReveal = p.id;
          children.appendChild(reveal);
        }
      }
      node.appendChild(children);
    }
    list.appendChild(node);
    const previous = previousArrows.get(p.id);
    if (previous !== undefined && previous !== expanded) {
      chevron.classList.toggle('is-collapsed', !previous);
      chevron.getBoundingClientRect();
      chevron.classList.toggle('is-collapsed', !expanded);
    }
  }

  const loose = sessions.filter((s) => !sessionProject(s) && runtime.ConversationUI.matchesSession(s));
  runtime.ConversationUI.appendGroupLabel(list, 'CHATS', loose.length);
  if (!loose.length && !runtime.ConversationUI.isSearching()) {
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
  title.type = 'button';
  title.className = 'session-title';
  title.textContent = s.title || 'Untitled';
  title.onclick = () => switchSession(s.id);
  item.appendChild(title);
  const more = document.createElement('button');
  more.type = 'button';
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
  workspace.setSession(activeId);
  documentPane.setSession(activeId);
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
const ensureSessionIndexes = runtime.UiState.ensureSessionIndexes;

function appendMessageNode(chat: HTMLElement, m: Message) {
  runtime.Messages.append(chat, m);
}

function scrollChat(capture = true) {
  runtime.ConversationUI.scrollChat(capture);
}

// ============================ session ops ============================
function switchSession(id: string) {
  activeId = id;
  persistActiveNow();
  handleLocalContextScopeChanged();
  renderSidebar();
  renderChat();
  // Session scoped provider/model settings, including effort, must be
  // reflected immediately when the active conversation changes.
  syncProviderUI(currentProviderId());
  runtime.Composer.syncSession();
  runtime.Composer.updateSend();
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
  runtime.Composer.syncSession();
  runtime.Composer.updateSend();
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
      body: JSON.stringify({ session_id: id, preserve_ui: false }),
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
  runtime.ConversationUI.forget(id);
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
  sessions.filter((s) => s.projectId === id).forEach((s) => runtime.ConversationUI.forget(s.id));
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
  const s = sessions.find((x) => x.id === sid);
  if (!s) return false;
  const wasEmpty = s.messages.length === 0;
  s.messages.push(m);
  if (
    s.title === 'New chat' &&
    !s.titleRenamed &&
    sid !== editingSessionId &&
    !runtime.ConversationUI.isRenamingSession(sid) &&
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
      scrollChat(false);
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
    armDanger(btn, () => clearMessages(activeId));
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
let runningSessionId: string | null = null;
let runningRunId: string | null = null;
const activeRuns = new Map<string, string>();
function isSessionRunning(sessionId: string): boolean {
  return [...activeRuns.values()].includes(sessionId);
}
let turnCounter = 0;
const turnIds = new Map<string, string>();
const replyStreamFrames = new FrameCoalescer<ServerEventData>((key, data) => {
  if (!runtime.ConversationUI.acceptsRun(data.session_id || '', data.run_id || '')) return;
  handleReplyStream(data);
});

function turnIdForRun(sessionId: string, runId: string): string {
  const existing = turnIds.get(runId);
  if (existing) return existing;
  const request = runtime.Requests.forRun(sessionId, runId)?.request;
  if (!request?.id) return '';
  const id = `turn:${request.id}`;
  turnIds.set(runId, id);
  return id;
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

const sseEventKey = (data: ServerEvent, fallback = '') => runtime.Sse.eventKey(data, fallback);
const runLifecycle = new RunLifecycleProjection();

function applyLifecycle(data: ServerEvent, source: LifecycleInput['source'], active?: boolean): boolean {
  if (data.run_id && runtime.ConversationUI.acceptsRun?.(data.session_id || '', data.run_id) === false) return false;
  const runId = data.run_id || '',
    sessionId = data.session_id || '';
  const session = sessions.find((item) => item.id === sessionId);
  if (!session || !runId) return false;
  if (session.terminalRuns?.includes(runId)) runLifecycle.restoreTerminal(sessionId, runId);
  const request = runtime.Requests.forRun(sessionId, runId);
  if (source !== 'snapshot' && request && request.request.attempts.at(-1) !== request.attempt) return false;
  const state = lifecycleState(
    data.task_status,
    data.stop_reason,
    data.type === 'task_start' ? 'running' : data.stop_reason || 'done',
  );
  const view = runLifecycle.transition({
    runId,
    sessionId,
    state,
    active: active ?? (state === 'running' ? true : undefined),
    revision: data.task_revision,
    sequence: eventSequence(data.event_id),
    source,
  });
  if (!view) return false;
  // Every lifecycle write follows the one transition decision above. These are
  // derived UI indexes; the durable task and its revision remain authoritative.
  if (view.terminal)
    session.terminalRuns = [...(session.terminalRuns || []).filter((id) => id !== runId), runId].slice(-32);
  else if (source === 'snapshot') session.terminalRuns = (session.terminalRuns || []).filter((id) => id !== runId);
  hydrateSessionIndexes(session);
  if (view.active) activeRuns.set(runId, sessionId);
  else activeRuns.delete(runId);
  const primary = [...activeRuns.entries()].at(-1);
  runningRunId = primary?.[0] ?? null;
  runningSessionId = primary?.[1] ?? null;
  if (view.active) turnIdForRun(sessionId, runId);
  if (view.active) runtime.Composer.observeRun?.(sessionId, runId);
  runtime.Requests.projectLifecycle(data as ServerEventData, view.state);
  if (view.state !== 'idle') recordRunState(sessionId, runId, view.state, data);
  if (data.type === 'task_done') data.stop_reason = view.state;
  persistActive();
  if (activeRuns.size) runtime.Sse.displayRunStatus('running');
  else setStatus('', 'ok');
  updateSend();
  updateComposerContext();
  syncResearchUseProjectButton();
  return true;
}

function handleReplyStream(data: ServerEventData) {
  const sid = data.session_id || '';
  const runId = (data.run_id || '').toString();
  const runIsActive = (id: string) => activeRuns.has(id) || id === runningRunId;
  if (!runId || !runIsActive(runId)) return;
  if (!runtime.Requests.acceptsEvent(data)) return;
  const session = sessions.find((session) => session.id === sid);
  const message = session && streamMessage(session, data);
  if (
    message &&
    !replaceSessionMessage(sid, (item) => item.streamId === message.streamId && item.runId === runId, message)
  )
    addToSession(sid, message);
}

let inputRefreshTimer: ReturnType<typeof setTimeout> | undefined;
function handleServerEvent(raw: unknown, lifecycleSource: 'event' | 'snapshot' = 'event') {
  const data = decodeServerEvent(raw) as ServerEventData;
  if (data.run_id && runtime.ConversationUI.acceptsRun?.(data.session_id || '', data.run_id) === false) return;
  if (data.type === 'conversation_inventory_changed') {
    void runtime.UiState.refreshInventory?.();
    return;
  }
  if (data.type === 'durable_commit') {
    void runtime.ConversationUI.observeCommit(decodeServerEvent(raw));
    if (inputRefreshTimer === undefined)
      inputRefreshTimer = setTimeout(() => {
        inputRefreshTimer = undefined;
        void runtime.Composer.refreshQueuedInputs();
      }, 200);
    return;
  }
  if (data.type === 'reply_stream') {
    const key = `${data.run_id || ''}:${data.stream_id || ''}`;
    replyStreamFrames.enqueue(key, data);
    return;
  }
  // Some legacy source tests execute only this dispatcher slice without the
  // frame coalescer declaration. The production bundle always has it, while
  // the optional guard keeps the isolated dispatcher usable.
  if (typeof replyStreamFrames !== 'undefined') replyStreamFrames.flush();
  if ((data.type === 'task_start' || data.type === 'task_done') && !applyLifecycle(data, lifecycleSource)) return;
  if (data.type === 'user_question' || data.type === 'user_question_state') {
    userQuestions.apply((raw as { question: unknown }).question);
    return;
  }
  if (data.type === 'conversation_input') {
    runtime.Composer.applyQueuedInput((raw as { input: unknown }).input);
    return;
  }
  if (data.type === 'task_start') {
    const input = runtime.Composer.queuedInputForRun(data.run_id);
    if (input) observeQueuedInput({ ...input, state: 'dispatching' });
  }
  if (data.type === 'shell_approval_status') {
    approvalController.apply(raw);
    void reconcileRunState();
    return;
  }
  const runId = (data.run_id || '').toString();
  const sid = data.session_id || '';
  if (data.type !== 'task_start' && data.type !== 'task_done' && !runtime.Requests.acceptsEvent(data)) return;
  const mayOutliveRun = [
    'task_start',
    'task_done',
    'shell_request',
    'shell_result',
    'teach_request',
    'reply',
    'ghost_post_turn_warning',
    'resync_required',
  ];
  const runIsActive = (id: string) => (typeof activeRuns !== 'undefined' && activeRuns.has(id)) || id === runningRunId;
  if (runId && !mayOutliveRun.includes(data.type) && !runIsActive(runId)) return;
  // Server dropped queued events for this client: re-pull authoritative state.
  if (data.type === 'resync_required') {
    reconcileRunState();
    refreshProviderStatus(true);
    return;
  }
  if (data.type === 'status') {
    runtime.Sse.displayRunStatus(data.status);
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
    const rawToolId = (data.call_id || data.tool_id || '').toString();
    const toolKey = rawToolId ? `${runId}:${rawToolId}` : '';
    const s = sessions.find((x) => x.id === sid);
    if (s && toolKey) {
      ensureSessionIndexes(s);
      if (s._toolKeys!.has(toolKey)) return;
    }
    addToSession(sid, {
      type: 'tool_pending',
      callId: data.call_id || '',
      parentCallId: data.parent_call_id || '',
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
    const rawToolId = (data.call_id || data.tool_id || '').toString();
    const toolKey = rawToolId ? `${runId}:${rawToolId}` : '';
    const toolStatus = (data.status || (data.ok === false ? 'error' : 'ok')).toString();
    const message = {
      type: 'tool',
      callId: data.call_id || '',
      parentCallId: data.parent_call_id || '',
      toolKey,
      eventKey: toolKey ? `tool:${toolKey}:final` : '',
      kind: data.kind || '',
      toolName: data.tool_name || '',
      turn: data.turn || 0,
      changed: data.changed === true,
      command: data.command || '',
      path: data.path || '',
      result: (data.detail ?? data.result ?? '').toString(),
      preview: data.preview,
      outputTruncated: data.detail_truncated || data.truncated,
      outputBytes: data.output_bytes,
      outputStoredBytes: data.output_stored_bytes,
      failure: data.failure,
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
    if (runtime.Requests.warning(sid, runId)) return;
    const detail = [data.stage, data.error_type, data.error_ref].filter(Boolean).join(' · ');
    addToSession(sid, {
      type: 'local_update_warning',
      text: 'Local memory update unavailable' + (detail ? ` · ${detail}` : ''),
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
    turnCounter = 0;
    return;
  }
  if (data.type === 'task_done') {
    const research = data.research;
    const summary = (data.summary || '').toString();
    const reason = (data.stop_reason || 'done').toString();
    if (reason === 'running' || reason === 'waiting' || reason === 'idle') return;
    const requestFailed = runtime.Requests.forRun(sid, runId)?.attempt.state === 'failed';
    const terminalKey = runId ? `terminal:${runId}` : '';
    const answerKey = terminalKey ? `${terminalKey}:answer` : '';
    const receiptKey = terminalKey ? `${terminalKey}:receipt` : '';
    const project = sessionProjectPath(sid);
    let shownReceipt = false;
    const researchRun = research ? recordResearchRun(sid, runId, research, data.receipt) : null;
    if (reason === 'error' && data.provider_failure && data.provider_failure.action === 'connect') {
      if (data.provider && PROVIDERS.includes(data.provider)) {
        applyProviderStatus([{ id: data.provider, available: false }]);
      }
      if (!requestFailed) {
        // Preserve the established send-error hook for status bookkeeping.
        // addSendError(sid, terminalKey, runId)
        addToSession(sid, {
          type: 'err',
          text: summary || 'Could not connect to the provider',
          sessionId: sid,
          runId,
          eventKey: terminalKey,
          failure: data.failure,
        });
      }
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
        failure: data.failure,
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
    } else if (reason === 'approval' || reason === 'question') {
      // shell approval card is emitted separately
    } else if (reason === 'stopped') {
      const turns = data.turns || 0;
      const label = turns ? `Stopped after ${turns} turn${turns === 1 ? '' : 's'}` : 'Stopped';
      addToSession(sid, { type: 'pause', text: label, sessionId: sid, runId, eventKey: terminalKey });
    } else if (reason === 'error' || reason === 'provider_failure') {
      const text = summary || 'Task failed';
      addToSession(sid, { type: 'err', text, sessionId: sid, runId, eventKey: terminalKey, failure: data.failure });
    } else if (reason === 'done') {
      const terminalAnswer = terminalAnswerMessage(data, runId, turnIds.get(runId) || '');
      if (data.mode === 'research') {
        if (summary) addToSession(sid, terminalAnswer || { type: 'asst', text: summary, runId, eventKey: answerKey });
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
        if (summary) addToSession(sid, terminalAnswer || { type: 'asst', text: summary, runId, eventKey: answerKey });
      } else if (project && summary) {
        addToSession(sid, terminalAnswer || { type: 'asst', text: summary, runId, eventKey: answerKey });
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
    if (reason === 'done' && sid && runId) void runtime.ConversationUI.syncAnswerIdentity(sid, runId);
    if (reason !== 'error' && reason !== 'approval' && data.changed && !shownReceipt)
      maybeAddChangesSummary(sid, terminalKey, runId);
    flushUiState();
    return;
  }
  if (data.type === 'shell_request') {
    if (approvalController.decided(data.id || '')) return;
    const isNew = !sessions
      .find((session) => session.id === sid)
      ?.messages.some((message) => message.id === data.id && message.type === 'shell_request');
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
      projectRuleAvailable: data.project_rule_available,
      sessionId: sid,
      runId,
      eventKey: `shell:${data.id}`,
    });
    if (sid === activeId && isNew)
      requestAnimationFrame(() => {
        if (sid === activeId && runtime.ConversationUI.isFollowing())
          runtime.ConversationUI.revealApproval(data.id || '');
      });
    return;
  }
  if (data.type === 'shell_result') {
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
      project: data.project,
      output: data.output,
      outputTruncated: !!data.truncated,
      exitCode: data.exit_code,
      executionId: data.execution_id,
      failure: data.failure,
      sessionId: sid,
      runId,
      eventKey: `shell-result:${data.id}`,
    });
    return;
  }
  if (data.type === 'reply') {
    const answerKey = runId ? `terminal:${runId}:answer` : '';
    if (data.text) {
      const final = terminalAnswerMessage(
        { summary: data.text, stop_reason: 'done', final_delivery: 'success' },
        runId,
        turnIds.get(runId) || '',
      );
      addToSession(sid, final || { type: 'asst', text: data.text, runId, eventKey: answerKey });
    }
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

function recordResearchRun(sessionId: string, runId: string, research: ResearchPayload, receipt: unknown) {
  const item = runtime.ResearchRuns.record(sessionId, runId, research, receipt);
  if (!item) return null;
  researchDrawerTab = 'evidence';
  researchNoteFocusId = '';
  runtime.ResearchDrawer.invalidateNoteCache(runtime.ResearchDrawer.noteIdsForRun(item));
  return item;
}

function currentResearchRun(sessionId = activeId) {
  return runtime.ResearchRuns.current(sessionId);
}

async function restoreResearchRun() {
  const restore = $('research-restore');
  if (restore) restore.disabled = true;
  const ownerSessionId = activeId;
  try {
    const result = await runtime.ResearchRuns.restore(ownerSessionId);
    if (!result) return;
    const subtitle = activeId === ownerSessionId ? $('research-subtitle') : null;
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
    if (activeId === result.ownerSessionId) runtime.ResearchDrawer.render(activeId);
  } finally {
    const current = currentResearchRun();
    if (restore) restore.disabled = !current || !current.restoreable;
  }
}

function markResearchRestoreAvailability(runIds: unknown) {
  const changed = runtime.ResearchRuns.markRestoreAvailability(runIds);
  const drawer = $('research-drawer');
  if (changed && drawer && drawer.classList.contains('open')) {
    runtime.ResearchDrawer.render(activeId);
  }
}

function applyRunState(data: RunState) {
  const owners = new Map((data.active_runs || []).map((run) => [run.run_id, run.session_id]));
  if (data.busy && data.run_id && data.session_id) owners.set(data.run_id, data.session_id);
  const versioned = new Set<string>();
  for (const row of data.task_states || []) {
    versioned.add(row.run_id);
    const state = lifecycleState(row.task_status, row.stop_reason, 'idle');
    applyLifecycle(
      { ...row, type: state === 'running' ? 'task_start' : 'task_done' },
      'snapshot',
      state === 'running' || (owners.has(row.run_id) && ['approval', 'question', 'waiting'].includes(state)),
    );
  }
  // Compatibility for older servers and admission-only test adapters. Actual
  // task revisions always win over this unversioned registry projection.
  for (const [runId, sessionId] of owners)
    if (!versioned.has(runId))
      applyLifecycle({ type: 'task_start', run_id: runId, session_id: sessionId }, 'snapshot', true);
  for (const [runId, sessionId] of [...activeRuns])
    if (!owners.has(runId) && !versioned.has(runId) && data.last_terminal_event?.run_id !== runId)
      applyLifecycle(
        { type: 'task_done', run_id: runId, session_id: sessionId, stop_reason: 'idle' },
        'snapshot',
        false,
      );
  (data.shell_approvals || []).forEach(approvalController.apply);
  if (data.last_shell_result) handleServerEvent(data.last_shell_result);
  if (
    data.last_terminal_event &&
    !(owners.has(data.last_terminal_event.run_id || '') && data.last_terminal_event.task_revision === undefined)
  )
    handleServerEvent(data.last_terminal_event, 'snapshot');
  if (data.pending_event) handleServerEvent(data.pending_event);
  markResearchRestoreAvailability(data.research_restore_runs || []);
  runtime.Requests.reconcile({
    session_id: data.session_id ?? '',
    run_id: data.run_id ?? '',
    busy: activeRuns.size > 0,
    active_runs: [...activeRuns].map(([run_id, session_id]) => ({ run_id, session_id })),
    response_ack_run_id: data.response_ack_run_id,
  });
  updateSend();
  updateComposerContext();
  syncResearchUseProjectButton();
}

function reconcileRunState() {
  return runtime.Sse.reconcileRunState();
}

async function acceptRunResponse(response: Response, sessionId: string) {
  return runtime.Sse.acceptRunResponse(response, sessionId);
}

function connectEvents() {
  return runtime.Sse.connect();
}

function addToSession(sid: string, m: Message) {
  if (m.type === 'user' && (m.queuedInputId || m.id)) {
    const session = sessions.find((item) => item.id === sid);
    const duplicate = session?.messages.some(
      (item) =>
        item.type === 'user' &&
        ((m.queuedInputId && item.queuedInputId === m.queuedInputId) ||
          (m.queuedInputId && item.id === m.queuedInputId)),
    );
    if (duplicate) return true;
  }
  if (m.type === 'shell_result' && m.id) {
    const s = sessions.find((item) => item.id === sid);
    const replaced = replaceSessionMessage(
      sid,
      (item) => ['shell_request', 'shell_result'].includes(item.type) && item.id === m.id,
      mergeCommandResult(
        s?.messages.find((item) => item.id === m.id && ['shell_request', 'shell_result'].includes(item.type)),
        m,
      ),
    );
    if (replaced) return true;
  }
  if (m.type === 'asst' && !m.streamId && m.runId && m.text) {
    const session = sessions.find((session) => session.id === sid);
    const existing = session?.messages
      .slice()
      .reverse()
      .find((item) => item.type === 'asst' && item.runId === m.runId);
    if (existing) {
      // A durable history refresh can replace the transient stream record
      // before the terminal event arrives. Keep one logical answer and retain
      // the stream identity so the mounted DOM node can be updated in place.
      const settled = existing.streamId
        ? { ...existing, ...m, streamId: existing.streamId, streamState: 'complete' as const, finalAnswer: true }
        : m;
      return replaceSessionMessage(sid, (item) => item === existing, settled);
    }
    if (sid === activeId) {
      const streamNode = Array.from(document.querySelectorAll<HTMLElement>('.msg.asst[data-stream-id]')).find(
        (node) => node.dataset.runId === m.runId,
      );
      const streamId = streamNode?.dataset.streamId;
      if (streamId && session) {
        const settled = { ...m, streamId, streamState: 'complete' as const, finalAnswer: true };
        session.messages.push(settled);
        runtime.UiState.trackSessionMessage(session, settled);
        persistActive();
        if (runtime.Messages.updateReply($('chat'), settled)) return true;
      }
    }
    const final = session && finalStreamMessage(session, m.runId, m.text, m.eventKey || '');
    if (final)
      return replaceSessionMessage(sid, (item) => item.streamId === final.streamId && item.runId === m.runId, final);
  }
  return runtime.Requests.addToSession(sid, m);
}
function observeQueuedInput(input: QueuedInput): void {
  const sid = input.request.session_id,
    runId = input.request.run_id;
  const session = sessions.find((session) => session.id === sid);
  if (!session) return;
  if (input.state === 'consumed') {
    void runtime.ConversationUI.refreshHistory(sid, true, true);
    return;
  }
  const represented = session.messages.some(
    (message) =>
      message.type === 'user' &&
      (message.queuedInputId === input.id ||
        message.id === input.id ||
        message.runId === runId ||
        message.attempts?.some((attempt) => attempt.runId === runId)),
  );
  if (['dispatching', 'submitted', 'completed', 'unconfirmed'].includes(input.state) && !represented) {
    addToSession(sid, {
      type: 'user',
      id: input.id,
      queuedInputId: input.id,
      text: input.request.task,
      attempts: [
        { runId, state: input.state === 'completed' ? 'done' : input.state === 'submitted' ? 'running' : 'sending' },
      ],
    });
    addToSession(sid, { type: 'request_status', sessionId: sid, requestId: input.id });
  }
  if (['failed', 'unconfirmed'].includes(input.state))
    runtime.Requests.fail(sid, runId, input.error || 'Delivery could not be confirmed', {
      sendFailure: 'queued_input',
    });
  if (input.state === 'completed' && input.terminal) handleServerEvent(input.terminal);
}
function observeUserQuestion(question: UserQuestion): void {
  const session = sessions.find((session) => session.id === question.session_id);
  if (!session) return;
  if (!session.messages.some((message) => message.type === 'user_question' && message.id === question.id))
    addToSession(session.id, {
      type: 'user_question',
      id: question.id,
      runId: question.run_id,
      eventKey: 'question:' + question.id,
    });
  if (question.state !== 'pending')
    addToSession(session.id, {
      type: 'run_state',
      runId: question.run_id,
      state: question.state === 'cancelled' ? 'stopped' : question.state === 'answered' ? 'done' : 'blocked',
      eventKey: `question-state:${question.id}:${question.revision}`,
    });
}

function replaceSessionMessage(sid: string, predicate: (message: Message) => boolean, message: Message) {
  const s = sessions.find((x) => x.id === sid);
  if (!s) return false;
  ensureSessionIndexes(s);
  const index = s.messages.findIndex(predicate);
  if (index < 0) return false;
  s.messages[index] = message;
  runtime.UiState.trackSessionMessage(s, message);
  persistActive();
  if (sid === activeId) {
    if (runtime.Messages.updateReply($('chat'), message)) {
      scrollChat(false);
      return true;
    }
    if (['shell_result', 'shell_approval_status'].includes(message.type) && runtime.ConversationUI.refreshApprovals())
      return true;
    if (runtime.Process.replace($('chat'), message)) {
      scrollChat(false);
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
        scrollChat(false);
        return true;
      }
    }
    if (message.type === 'asst' && message.runId) {
      const existingAssistant = Array.from(document.querySelectorAll<HTMLElement>('.msg.asst')).find(
        (node) => node.dataset.runId === message.runId,
      );
      if (existingAssistant) {
        const streamId = existingAssistant.dataset.streamId;
        if (streamId) {
          runtime.Messages.updateReply($('chat'), {
            ...message,
            streamId,
            streamState: 'complete',
            finalAnswer: true,
          });
        }
        // The mounted answer already represents this run. Do not rebuild the
        // entire conversation merely because its durable identity changed.
        scrollChat(false);
        return true;
      }
    }
    renderChat();
    scrollChat();
  }
  return true;
}

function recordRunState(sid: string, runId: string, state: string, data: ServerEvent & { elapsed_ms?: unknown }) {
  const session = sessions.find((item) => item.id === sid);
  if (!session || lastRunState(session, runId) === state) return;
  const revision = session.messages.filter((item) => item.type === 'run_state' && item.runId === runId).length;
  addToSession(sid, {
    type: 'run_state',
    state,
    runId,
    elapsedMs: typeof data.elapsed_ms === 'number' ? data.elapsed_ms : undefined,
    eventKey: sseEventKey(data, `run-state:${runId}:${revision}:${state}`),
  });
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
  sessions: () => sessions,
  changed: (sid) => {
    const session = sessions.find((s) => s.id === sid);
    if (session) hydrateSessionIndexes(session);
    persistActive();
    if (sid === activeId && !runtime.ConversationUI.refreshApprovals()) renderChat();
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
  const busy = runningSessionId === activeId;
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
    getActiveId: () => activeId,
    getSessions: () => sessions,
    activeSession,
    receiptSummary,
    persistActive,
    persistActiveNow,
  });
  runtime.ResearchDrawer.init({
    openDocument,
    $,
    getActiveId: () => activeId,
    getSessionTitle: (id: string) => (sessions.find((s) => s.id === id) || {}).title || 'Chat',
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
    getActiveId: () => activeId,
    currentProjectPath: () => currentProjectPath(),
    openMenuAt,
    closeAllMenus,
    closeOtherDrawers,
    copyText,
  });
  runtime.RunDetails.init({ getActiveId: () => activeId });
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
  runtime.Composer.init({
    $,
    PROVIDERS,
    getActiveId: () => activeId,
    getRunningSessionId: () => runningSessionId,
    getRunningRunId: () => runningRunId,
    getRunningRunIdForSession: (sessionId: string) =>
      [...activeRuns.entries()].find(([, owner]) => owner === sessionId)?.[0] || null,
    isSessionRunning,
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
    getActiveId: () => activeId,
    getRunningSessionId: () => runningSessionId,
    getSessions: () => sessions,
  });
  runtime.ConversationUI.init({
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
boot().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  setStatus(message, 'warn');
  reportDesktopFailure(message);
});
