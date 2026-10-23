import { runtime } from './runtime';
import { mergeUiState, sameUiContent, type UiSnapshot } from './ui_state_merge';
import type { Draft, Message, Project, ResearchRun, Session, UiDeps } from './types/domain';
type Loose = Record<string, unknown>;
export interface UiStateApi {
  init(deps: UiDeps): void;
  setDrawerOpen(id: string, open: boolean): void;
  DEFAULT_PROVIDER: string;
  PROVIDER_LABELS: Record<string, string>;
  PROVIDERS: string[];
  setProviders(ids: string[], labels: Record<string, string> | null, defaultId?: string): void;
  setDefaultProvider(id: string): void;
  uid(): string;
  defaultSession(projectId?: string | null, provider?: string | null): Session;
  hydrateSessionIndexes(session: Session): Session;
  ensureSessionIndexes(session: Session): Session;
  trackSessionMessage(session: Session, message: Message): void;
  untrackSessionMessages(session: Session, removed: Message[]): void;
  pathName(path: string): string;
  pathKey(path: string): string;
  apply(state: Loose): void;
  accept(state: Loose): void;
  current(): Loose;
  cache(): void;
  cached(): Loose;
  markDirty(): void;
  flush(): void;
  persist(): void;
  persistNow(): void;
  bindPagehide(): void;
  restoreFromServer(): Promise<void>;
  refreshInventory(): Promise<void>;
  ensureSaved(): Promise<boolean>;
}
/* Codey UI state runtime: local cache, durable snapshot sync, and versioning. */
(function () {
  'use strict';

  function isRecord(value: unknown): value is Loose {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  }

  let deps: UiDeps = {};
  const LS_CACHE = 'codey:ui-cache';
  const LS_SESSIONS = 'codey:sessions';
  const LS_ACTIVE = 'codey:active';
  const LS_PROJECTS = 'codey:projects';
  const LS_UI_UPDATED = 'codey:ui-updated';
  const LS_UI_REVISION = 'codey:ui-revision';
  let DEFAULT_PROVIDER: string = 'deepseek';
  let PROVIDER_LABELS: Record<string, string> = {
    deepseek: 'DeepSeek',
    mimo: 'MiMo',
    stepfun: 'StepFun',
    qwen: 'Qwen',
    glm: 'GLM',
    local: 'Local',
  };
  let PROVIDERS = Object.keys(PROVIDER_LABELS);

  function setProviders(ids: string[], labels: Record<string, string> | null, defaultId?: string): void {
    // Mutate in place so every module holding PROVIDERS/PROVIDER_LABELS
    // references (index.html boot consts) stays live without re-wiring.
    if (Array.isArray(ids) && ids.length) {
      const nextLabels: Record<string, string> = {};
      for (const id of ids) {
        const key = id.trim();
        if (!key) continue;
        const label = labels && labels[key] ? String(labels[key]) : PROVIDER_LABELS[key] || key;
        nextLabels[key] = label;
      }
      const keys = Object.keys(nextLabels);
      if (keys.length) {
        for (const key of Object.keys(PROVIDER_LABELS)) delete PROVIDER_LABELS[key];
        for (const key of keys) PROVIDER_LABELS[key] = nextLabels[key];
        PROVIDERS.splice(0, PROVIDERS.length, ...keys);
      }
    }
    if (typeof defaultId === 'string' && PROVIDER_LABELS[defaultId]) {
      DEFAULT_PROVIDER = defaultId;
      runtime.UiState.DEFAULT_PROVIDER = defaultId;
    }
  }

  function setDefaultProvider(id: string): void {
    const key = id.trim();
    if (!key || !PROVIDER_LABELS[key]) return;
    DEFAULT_PROVIDER = key;
    runtime.UiState.DEFAULT_PROVIDER = key;
  }

  function init(nextDeps: UiDeps): void {
    deps = nextDeps || {};
  }

  function getSessions(): Session[] {
    return deps.getSessions ? deps.getSessions() : [];
  }
  function setSessions(value: Session[]): void {
    if (deps.setSessions) deps.setSessions(value);
  }
  function getProjects(): Project[] {
    return deps.getProjects ? deps.getProjects() : [];
  }
  function setProjects(value: Project[]): void {
    if (deps.setProjects) deps.setProjects(value);
  }
  function getActiveId(): string {
    return deps.getActiveId ? deps.getActiveId() : '';
  }
  function setActiveId(value: string): void {
    if (deps.setActiveId) deps.setActiveId(value);
  }
  function getUpdatedAt(): number {
    return deps.getUpdatedAt ? deps.getUpdatedAt() : 0;
  }
  function setUpdatedAt(value: number): void {
    if (deps.setUpdatedAt) deps.setUpdatedAt(value);
  }
  function getRevision(): number {
    return deps.getRevision ? deps.getRevision() : 0;
  }
  function setRevision(value: number): void {
    if (deps.setRevision) deps.setRevision(value);
  }

  function uid() {
    return Math.random().toString(36).slice(2, 10);
  }
  function loadJson(key: string, fallback: unknown): unknown {
    try {
      return JSON.parse(localStorage.getItem(key) || JSON.stringify(fallback));
    } catch {
      return fallback;
    }
  }
  function safeLocalSet(key: string, value: string): void {
    try {
      localStorage.setItem(key, value);
    } catch {}
  }
  function sessionMetadata(arr: Session[]) {
    return arr.map(({ messages: _messages, ...metadata }) => metadata);
  }
  function defaultSession(projectId: string | null = null, provider: string | null = null) {
    const previous = deps.activeSession?.();
    const candidate = provider || previous?.provider || '';
    const selection = previous?.provider === candidate ? previous.modelSelection : undefined;
    const enabled = runtime.Models?.allows(candidate, selection?.model);
    const connection = runtime.Models?.connections().some((source: { id: string }) => source.id === candidate);
    const resolved = enabled || connection ? candidate : '';
    return hydrateSessionIndexes({
      ...(selection?.model && enabled
        ? { modelSelection: { ...selection, efforts: { ...(selection.efforts || {}) } } }
        : {}),
      id: uid(),
      title: 'New chat',
      draft: normalizeDraft(),
      messages: [],
      terminalRuns: [],
      researchRuns: [],
      createdAt: Date.now(),
      projectId,
      provider: resolved,
      research: false,
    });
  }
  function pathName(path: string): string {
    const trimmed = (path || '').replace(/[\\\/]+$/, '');
    return trimmed.split(/[\\\/]/).pop() || trimmed || 'project';
  }
  function pathKey(path: string): string {
    return (path || '').replace(/[\\\/]+$/, '').toLowerCase();
  }

  function normalizeStoredResearchRun(value: unknown): Loose {
    const run: Loose = isRecord(value) ? value : {};
    return {
      runId: String(run.runId || ''),
      synthesisId: String(run.synthesisId || ''),
      notesCreated: Array.isArray(run.notesCreated) ? run.notesCreated.map(String) : [],
      notesUpdated: Array.isArray(run.notesUpdated) ? run.notesUpdated.map(String) : [],
      sourceUrls: Array.isArray(run.sourceUrls) ? run.sourceUrls.map(String) : [],
      sourcesRead: Number(run.sourcesRead || 0),
      queries: Array.isArray(run.queries) ? run.queries.map(String) : [],
      searchResults: Array.isArray(run.searchResults) ? run.searchResults : [],
      openedSources: Array.isArray(run.openedSources) ? run.openedSources : [],
      coverage: run.coverage && typeof run.coverage === 'object' ? run.coverage : {},
      citationMap: Array.isArray(run.citationMap) ? run.citationMap : [],
      evidenceItems: Array.isArray(run.evidenceItems) ? run.evidenceItems : [],
      counterpoints: Array.isArray(run.counterpoints) ? run.counterpoints.map(String) : [],
      qualityWarnings: Array.isArray(run.qualityWarnings) ? run.qualityWarnings.map(String) : [],
      receipt: run.receipt ? String(run.receipt) : '',
      restoreable: false,
      createdAt: Number(run.createdAt || 0) || Date.now(),
    };
  }

  function defineSessionIndexes(s: Session): Session {
    // Non-enumerable so JSON.stringify(localStorage/server) stays clean.
    if (!s._eventKeys || !(s._eventKeys instanceof Set)) {
      Object.defineProperty(s, '_eventKeys', {
        value: new Set(),
        writable: true,
        enumerable: false,
        configurable: true,
      });
    }
    if (!s._toolKeys || !(s._toolKeys instanceof Set)) {
      Object.defineProperty(s, '_toolKeys', {
        value: new Set(),
        writable: true,
        enumerable: false,
        configurable: true,
      });
    }
    if (!s._terminalRunSet || !(s._terminalRunSet instanceof Set)) {
      Object.defineProperty(s, '_terminalRunSet', {
        value: new Set(),
        writable: true,
        enumerable: false,
        configurable: true,
      });
    }
    return s;
  }

  function hydrateSessionIndexes(s: Session): Session {
    if (!s || typeof s !== 'object') return s;
    defineSessionIndexes(s);
    s._eventKeys!.clear();
    s._toolKeys!.clear();
    s._terminalRunSet!.clear();
    for (const m of Array.isArray(s.messages) ? s.messages : []) {
      if (m && m.eventKey) s._eventKeys!.add(m.eventKey);
      if (m && m.toolKey) s._toolKeys!.add(m.toolKey);
    }
    for (const runId of Array.isArray(s.terminalRuns) ? s.terminalRuns : []) {
      if (runId) s._terminalRunSet!.add(runId);
    }
    return s;
  }

  function ensureSessionIndexes(s: Session): Session {
    if (!s || typeof s !== 'object') return s;
    if (!(s._eventKeys instanceof Set) || !(s._toolKeys instanceof Set) || !(s._terminalRunSet instanceof Set)) {
      return hydrateSessionIndexes(s);
    }
    return s;
  }

  function trackSessionMessage(s: Session, m: Message): void {
    ensureSessionIndexes(s);
    if (m && m.eventKey) s._eventKeys!.add(m.eventKey);
    if (m && m.toolKey) s._toolKeys!.add(m.toolKey);
  }

  function untrackSessionMessages(s: Session, removed: Message[]): void {
    // Removal paths (filter) are rare; rebuild is cheaper than diffing.
    if (!s) return;
    if (Array.isArray(removed) && removed.length === 0) return;
    hydrateSessionIndexes(s);
  }

  function normalizeDraft(value: unknown = {}) {
    const draft = value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
    const text = typeof draft.text === 'string' ? draft.text : '';
    const integer = (value: unknown): number => (Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : 0);
    const start = Math.min(text.length, integer(draft.start));
    return {
      text,
      start,
      end: Math.max(start, Math.min(text.length, integer(draft.end))),
      direction: ['none', 'forward', 'backward'].includes(String(draft.direction))
        ? (String(draft.direction) as Draft['direction'])
        : 'none',
      revision: integer(draft.revision),
    };
  }
  function normalizeSessions(value: unknown): Session[] {
    return (Array.isArray(value) ? value : []).map((raw) => {
      const item = isRecord(raw) ? raw : {};
      const s = {
        id: typeof item.id === 'string' && item.id ? item.id : uid(),
        draft: normalizeDraft(item.draft),
        title: typeof item.title === 'string' && item.title ? item.title : 'New chat',
        ...(item.titleRenamed === true ? { titleRenamed: true } : {}),
        messages: [],
        terminalRuns: Array.isArray(item.terminalRuns)
          ? item.terminalRuns.filter((id): id is string => typeof id === 'string').slice(-32)
          : [],
        researchRuns: Array.isArray(item.researchRuns)
          ? (item.researchRuns.slice(-32).map(normalizeStoredResearchRun) as unknown as ResearchRun[])
          : [],
        createdAt: typeof item.createdAt === 'number' ? item.createdAt : Date.now(),
        projectId: typeof item.projectId === 'string' ? item.projectId : null,
        provider: typeof item.provider === 'string' && item.provider.length <= 100 ? item.provider : '',
        research: item.research === true,
        ...(isRecord(item.modelSelection)
          ? {
              modelSelection: {
                connection_id: String(item.modelSelection.connection_id || ''),
                base_url: String(item.modelSelection.base_url || ''),
                model: String(item.modelSelection.model || ''),
                ...(typeof item.modelSelection.name === 'string'
                  ? { name: item.modelSelection.name.slice(0, 160) }
                  : {}),
                effort: ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].includes(
                  String(item.modelSelection.effort),
                )
                  ? String(item.modelSelection.effort)
                  : null,
                ...(typeof item.modelSelection.thinking === 'boolean'
                  ? { thinking: item.modelSelection.thinking }
                  : {}),
                efforts: Object.fromEntries(
                  Object.entries(isRecord(item.modelSelection.efforts) ? item.modelSelection.efforts : {})
                    .slice(0, 50)
                    .filter(
                      ([key, value]) =>
                        key.length <= 1000 &&
                        ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].includes(String(value)),
                    ),
                ) as Record<string, string>,
              },
            }
          : {}),
      };
      return hydrateSessionIndexes(s);
    });
  }
  function normalizeProjects(value: unknown): Project[] {
    return (Array.isArray(value) ? value : [])
      .map((raw) => {
        const p = isRecord(raw) ? raw : {};
        return {
          id: typeof p.id === 'string' && p.id ? p.id : uid(),
          name: typeof p.name === 'string' && p.name ? p.name : pathName(typeof p.path === 'string' ? p.path : ''),
          path: typeof p.path === 'string' ? p.path : '',
          expanded: p.expanded !== false,
          createdAt: typeof p.createdAt === 'number' ? p.createdAt : Date.now(),
        };
      })
      .filter((p) => p.path);
  }
  function applyUiState(state: Loose): void {
    let nextSessions = normalizeSessions(state && state.sessions);
    const currentSessions = new Map((deps.getSessions?.() || []).map((session) => [session.id, session]));
    // UI state is metadata only. Preserve the in-memory durable projection and
    // runtime facts when a server save races a history load; replacing a
    // session with the metadata form would erase the conversation just loaded
    // from durable history.
    nextSessions = nextSessions.map((session) => {
      const current = currentSessions.get(session.id);
      if (!current) return session;
      // Keep the identity held by the composer and the history repository.
      // Metadata acknowledgement must not detach the currently edited draft.
      if (!session.modelSelection) delete current.modelSelection;
      if (!session.titleRenamed) delete current.titleRenamed;
      const facts = {
        messages: current.messages,
        terminalRuns: current.terminalRuns,
        researchRuns: current.researchRuns,
      };
      return ensureSessionIndexes(Object.assign(current, session, facts));
    });
    const nextProjects = normalizeProjects(state && state.projects);
    let nextActiveId = state && state.active_id ? String(state.active_id) : '';
    if (!nextSessions.length) {
      const s = defaultSession();
      nextSessions = [s];
      nextActiveId = s.id;
    }
    if (!nextSessions.find((s) => s.id === nextActiveId)) nextActiveId = nextSessions[0].id;
    setSessions(nextSessions);
    setProjects(nextProjects);
    setActiveId(nextActiveId);
    setUpdatedAt(Number((state && state.updated_at) || 0) || 0);
    setRevision(Number((state && state.revision) || 0) || 0);
  }
  function currentUiState() {
    return {
      active_id: getActiveId(),
      sessions: sessionMetadata(getSessions()),
      projects: getProjects(),
      updated_at: getUpdatedAt(),
      revision: getRevision(),
    };
  }
  function cacheUiState() {
    // One atomic localStorage value binds pending edits to their exact confirmed base.
    safeLocalSet(LS_CACHE, JSON.stringify({ schema: 1, state: currentUiState(), confirmed }));
  }
  function cachedUiState(): Loose {
    const cached = loadJson(LS_CACHE, null);
    if (isRecord(cached) && cached.schema === 1 && isSnapshot(cached.state)) {
      confirmed = isSnapshot(cached.confirmed) ? asSnapshot(cached.confirmed) : null;
      return cached.state;
    }
    // One-time migration of existing installations, never a second live cache writer.
    confirmed = null;
    const previous = {
      active_id: localStorage.getItem(LS_ACTIVE) || '',
      sessions: loadJson(LS_SESSIONS, []),
      projects: loadJson(LS_PROJECTS, []),
      updated_at: Number(localStorage.getItem(LS_UI_UPDATED) || 0) || 0,
      revision: Number(localStorage.getItem(LS_UI_REVISION) || 0) || 0,
    };
    const envelope = JSON.stringify({ schema: 1, state: previous, confirmed: null });
    safeLocalSet(LS_CACHE, envelope);
    try {
      if (localStorage.getItem(LS_CACHE) === envelope)
        for (const key of [LS_SESSIONS, LS_PROJECTS, LS_ACTIVE, LS_UI_UPDATED, LS_UI_REVISION])
          localStorage.removeItem(key);
    } catch {
      /* Storage is optional; do not discard unmigrated drafts. */
    }
    return previous;
  }
  const asSnapshot = (state: Loose): UiSnapshot => ({
    ...state,
    sessions: Array.isArray(state.sessions) ? state.sessions : [],
    projects: Array.isArray(state.projects) ? state.projects : [],
  });
  let confirmed: UiSnapshot | null = null;
  let savePromise: Promise<boolean> | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let failures = 0;
  function acknowledge(state: Loose): void {
    confirmed = structuredClone(asSnapshot(state));
    setRevision(Number(state.revision || 0));
  }
  function repaint(): void {
    deps.renderSidebar?.();
    deps.renderChat?.();
    deps.updateComposerContext?.();
    if (deps.currentProviderId) deps.syncProviderUI?.(deps.currentProviderId());
  }
  function accept(state: Loose): void {
    if (confirmed && Number(state.revision || 0) < Number(confirmed.revision || 0)) return;
    const current = asSnapshot(currentUiState());
    const merged = confirmed ? mergeUiState(confirmed, current, asSnapshot(state)) : asSnapshot(state);
    applyUiState(merged);
    acknowledge(state);
    cacheUiState();
    repaint();
  }
  function saveNotice(failed: boolean): void {
    const notice = document.getElementById('ui-save-notice');
    if (!notice) return;
    if (!failed && notice.contains(document.activeElement))
      document.getElementById('task')?.focus({ preventScroll: true });
    notice.hidden = !failed;
    const message = document.getElementById('ui-save-message');
    if (message) message.textContent = 'Draft changes couldn’t be saved';
  }
  function isSnapshot(value: unknown): value is UiSnapshot {
    return (
      isRecord(value) &&
      Array.isArray(value.sessions) &&
      Array.isArray(value.projects) &&
      typeof value.active_id === 'string' &&
      Number.isSafeInteger(value.revision) &&
      Number(value.revision) >= 0
    );
  }
  async function readServer(): Promise<Loose> {
    const response = await fetch('/api/ui_state', { cache: 'no-store', signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error('Could not load chat settings');
    const state = (await response.json()).state;
    if (!isSnapshot(state)) throw new Error('Invalid chat settings response');
    return state;
  }
  function saveUiStateToServer(): Promise<boolean> {
    if (savePromise) return savePromise;
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    savePromise = (async () => {
      try {
        if (!confirmed) accept(await readServer());
        // Bounded conflict reconciliation; a hot competing writer cannot spin forever.
        for (let attempt = 0; attempt < 5; attempt++) {
          const sent = structuredClone(asSnapshot(currentUiState()));
          const response = await fetch('/api/ui_state', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ state: sent, base_revision: confirmed!.revision || 0 }),
            signal: AbortSignal.timeout(10000),
          });
          const data = await response.json();
          if (response.status === 409) {
            accept(await readServer());
            continue;
          }
          if (!response.ok || !Number.isSafeInteger(data.revision) || data.revision < 0)
            throw new Error('Could not save chat settings');
          const remote = asSnapshot({ ...sent, revision: data.revision });
          // The request only acknowledges its captured edits, never edits made while it was in flight.
          if (Number(remote.revision || 0) >= Number(confirmed!.revision || 0)) {
            const pending = mergeUiState(sent, asSnapshot(currentUiState()), remote);
            applyUiState(pending);
            acknowledge(remote);
          }
          cacheUiState();
          if (sameUiContent(asSnapshot(currentUiState()), confirmed!)) {
            failures = 0;
            saveNotice(false);
            return true;
          }
        }
        throw new Error('Chat settings are changing in another window');
      } catch {
        failures++;
        saveNotice(failures >= 3 && getSessions().some((session) => !!session.draft.text));
        retryTimer = setTimeout(
          () => {
            retryTimer = null;
            void saveUiStateToServer();
          },
          Math.min(30000, 1000 * 2 ** failures),
        );
        return false;
      } finally {
        savePromise = null;
      }
    })();
    return savePromise;
  }

  let uiStatePersistTimer: ReturnType<typeof setTimeout> | null = null;
  const UI_STATE_PERSIST_DELAY = 400;

  function markUiStateDirty() {
    setUpdatedAt(Math.max(Date.now(), getUpdatedAt()));
  }

  function flushUiState() {
    if (uiStatePersistTimer !== null) {
      clearTimeout(uiStatePersistTimer);
      uiStatePersistTimer = null;
    }
    cacheUiState();
    saveUiStateToServer();
  }
  async function ensureSaved(): Promise<boolean> {
    flushUiState();
    while (savePromise) if (!(await savePromise)) return false;
    return true;
  }

  function persistActive() {
    markUiStateDirty();
    if (uiStatePersistTimer !== null) return;
    uiStatePersistTimer = setTimeout(() => {
      uiStatePersistTimer = null;
      cacheUiState();
      saveUiStateToServer();
    }, UI_STATE_PERSIST_DELAY);
  }

  function persistActiveNow() {
    markUiStateDirty();
    flushUiState();
  }

  function setDrawerOpen(id: string, open: boolean): void {
    if (runtime.Workspace && (id === 'changes-drawer' || id === 'research-drawer')) {
      const panel = id === 'changes-drawer' ? 'changes' : 'research';
      const element = document.getElementById(id);
      element?.classList.toggle('open', open);
      element?.setAttribute('aria-hidden', String(!open));
      if (element) element.inert = !open;
      if (open) runtime.Workspace.reveal(panel);
      else runtime.Workspace.panelClosed(panel);
      return;
    }
    // Shared drawer toggle so the three drawers stay mutually exclusive
    // without triplicating classList/aria handling. Existing open/close
    // wrappers keep their names; they delegate here.
    const el = document.getElementById(id) as (HTMLElement & { _returnFocus?: Element | null }) | null;
    if (!el) return;
    const opening = open && !el.classList.contains('open');
    if (opening) {
      el._returnFocus = document.activeElement;
      el.inert = false;
    }
    el.classList.toggle('open', !!open);
    el.setAttribute('aria-hidden', open ? 'false' : 'true');
    el.inert = !open;
    if (opening) {
      const close = el.querySelector('[id$="-close"]');
      if (close instanceof HTMLElement) close.focus({ preventScroll: true });
    } else if (!open && el.contains(document.activeElement) && el._returnFocus && el._returnFocus.isConnected) {
      if (el._returnFocus instanceof HTMLElement) el._returnFocus.focus({ preventScroll: true });
    }
  }

  function bindUiStatePagehide(): void {
    const retry = document.getElementById('ui-save-retry');
    window.addEventListener('online', () => {
      void saveUiStateToServer();
    });
    if (retry)
      retry.onclick = () => {
        flushUiState();
      };
    window.addEventListener('pagehide', () => {
      runtime.Composer.captureDraft();
      if (uiStatePersistTimer !== null) {
        clearTimeout(uiStatePersistTimer);
        uiStatePersistTimer = null;
      }
      cacheUiState();
      if (!confirmed || savePromise) return;
      const base = confirmed.revision || 0;
      const payload = JSON.stringify({ state: currentUiState(), base_revision: base });
      try {
        if (navigator.sendBeacon) {
          navigator.sendBeacon('/api/ui_state', new Blob([payload], { type: 'application/json' }));
          return;
        }
      } catch {}
      saveUiStateToServer();
    });
  }

  async function refreshInventory(): Promise<void> {
    accept(await readServer());
  }

  async function restoreUiStateFromServer() {
    try {
      const local = asSnapshot(currentUiState());
      const remote = asSnapshot(await readServer());
      // A fresh boot can attach to a reset store; old cache revisions cannot fence that store.
      if (confirmed && Number(remote.revision || 0) < Number(confirmed.revision || 0)) confirmed = null;
      if (!confirmed) {
        // Without a confirmed base, old cache rows cannot establish new conversations.
        // Recover drafts only for identities that still exist on the server.
        const cached = new Map(local.sessions.map((row) => [row.id, row]));
        const recovered = {
          ...remote,
          sessions: remote.sessions.map((row) => {
            const draft = cached.get(row.id)?.draft;
            return isRecord(draft) && typeof draft.text === 'string' && draft.text ? { ...row, draft } : row;
          }),
        };
        applyUiState(recovered);
        acknowledge(remote);
      } else accept(remote);
      cacheUiState();
      repaint();
      void saveUiStateToServer();
    } catch {
      void saveUiStateToServer();
    }
  }

  runtime.UiState = {
    init,
    setDrawerOpen,
    DEFAULT_PROVIDER,
    PROVIDER_LABELS,
    PROVIDERS,
    setProviders,
    setDefaultProvider,
    uid,
    defaultSession,
    hydrateSessionIndexes,
    ensureSessionIndexes,
    trackSessionMessage,
    untrackSessionMessages,
    pathName,
    pathKey,
    apply: applyUiState,
    accept,
    current: currentUiState,
    cache: cacheUiState,
    cached: cachedUiState,
    markDirty: markUiStateDirty,
    flush: flushUiState,
    persist: persistActive,
    persistNow: persistActiveNow,
    bindPagehide: bindUiStatePagehide,
    restoreFromServer: restoreUiStateFromServer,
    refreshInventory,
    ensureSaved,
  };
})();
