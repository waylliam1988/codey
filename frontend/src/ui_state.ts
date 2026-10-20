import { runtime } from './runtime';
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
  setRecommended(id: unknown): void;
  uid(): string;
  defaultSession(projectId?: string | null, provider?: string | null): Session;
  hydrateSessionIndexes(session: Session): Session;
  ensureSessionIndexes(session: Session): Session;
  trackSessionMessage(session: Session, message: Message): void;
  untrackSessionMessages(session: Session, removed: Message[]): void;
  pathName(path: string): string;
  pathKey(path: string): string;
  apply(state: Loose): void;
  current(): Loose;
  cache(): void;
  cached(): Loose;
  hasMeaningful(state: Loose): boolean;
  hasStored(state: Loose): boolean;
  isNewer(a: Loose, b: Loose): boolean;
  markDirty(): void;
  flush(): void;
  persist(): void;
  persistNow(): void;
  bindPagehide(): void;
  restoreFromServer(): Promise<void>;
  ensureSaved(): Promise<boolean>;
}
/* Codey UI state runtime: local cache, durable snapshot sync, and versioning. */
(function () {
  'use strict';

  function isRecord(value: unknown): value is Loose {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  }

  let deps: UiDeps = {};
  const LS_SESSIONS = 'codey:sessions';
  const LS_ACTIVE = 'codey:active';
  const LS_PROJECTS = 'codey:projects';
  const LS_UI_UPDATED = 'codey:ui-updated';
  const LS_UI_REVISION = 'codey:ui-revision';
  let DEFAULT_PROVIDER: string = 'deepseek';
  let RECOMMENDED_PROVIDER = '';
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
  function isDirtySinceBoot(): boolean {
    return !!(deps.isDirtySinceBoot && deps.isDirtySinceBoot());
  }
  function setDirtySinceBoot(value: boolean): void {
    if (deps.setDirtySinceBoot) deps.setDirtySinceBoot(value);
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
  function saveSessions(arr: Session[]): void {
    safeLocalSet(LS_SESSIONS, JSON.stringify(sessionMetadata(arr)));
  }
  function sessionMetadata(arr: Session[]) {
    return arr.map(({ messages: _messages, ...metadata }) => metadata);
  }
  function saveProjects(arr: Project[]): void {
    safeLocalSet(LS_PROJECTS, JSON.stringify(arr));
  }
  function setRecommended(id: unknown): void {
    // Backend cold-start hint for brand-new sessions only; stored sessions
    // keep their own provider and never read this.
    const key = String(id || '').trim();
    if (key && PROVIDER_LABELS[key]) RECOMMENDED_PROVIDER = key;
  }
  function defaultSession(projectId: string | null = null, provider: string | null = null) {
    const resolved = provider || RECOMMENDED_PROVIDER || DEFAULT_PROVIDER;
    return hydrateSessionIndexes({
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
        provider:
          typeof item.provider === 'string' && item.provider.trim() && item.provider.length <= 100
            ? item.provider
            : DEFAULT_PROVIDER,
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
      return hydrateSessionIndexes({
        ...session,
        messages: current.messages,
        terminalRuns: current.terminalRuns,
        researchRuns: current.researchRuns,
      });
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
    serverRevision = Number((state && state.revision) || 0) || 0;
    syncConflict = false;
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
    saveSessions(getSessions());
    saveProjects(getProjects());
    safeLocalSet(LS_ACTIVE, getActiveId());
    safeLocalSet(LS_UI_UPDATED, String(getUpdatedAt()));
    safeLocalSet(LS_UI_REVISION, String(getRevision()));
  }
  function cachedUiState(): Loose {
    return {
      active_id: localStorage.getItem(LS_ACTIVE) || '',
      sessions: loadJson(LS_SESSIONS, []),
      projects: loadJson(LS_PROJECTS, []),
      updated_at: Number(localStorage.getItem(LS_UI_UPDATED) || 0) || 0,
      revision: Number(localStorage.getItem(LS_UI_REVISION) || 0) || 0,
    };
  }
  function uiStateVersion(state: Loose): [number, number] {
    return [Number((state && state.updated_at) || 0) || 0, Number((state && state.revision) || 0) || 0];
  }
  function isUiStateNewer(a: Loose, b: Loose): boolean {
    const av = uiStateVersion(a);
    const bv = uiStateVersion(b);
    return av[0] > bv[0] || (av[0] === bv[0] && av[1] > bv[1]);
  }
  function hasStoredUiState(state: Loose): boolean {
    return !!(
      state &&
      ((Array.isArray(state.sessions) && state.sessions.length) ||
        (Array.isArray(state.projects) && state.projects.length) ||
        state.active_id)
    );
  }
  function hasMeaningfulUiState(state: Loose): boolean {
    if (!state) return false;
    if (Array.isArray(state.projects) && state.projects.length) return true;
    if (!Array.isArray(state.sessions)) return false;
    return state.sessions.some(
      (s) =>
        (s.draft && typeof s.draft.text === 'string' && s.draft.text.length) ||
        (Array.isArray(s.messages) && s.messages.length) ||
        (Array.isArray(s.terminalRuns) && s.terminalRuns.length) ||
        (s.title && s.title !== 'New chat'),
    );
  }
  let saveInFlight = false;
  let savePromise: Promise<boolean> | null = null;
  let saveQueued = false;
  let serverRevision: number | null = null;
  let syncConflict = false;
  function saveNotice(status: string): void {
    const notice = document.getElementById('ui-save-notice');
    const retry = document.getElementById('ui-save-retry');
    if (!notice || !retry) return;
    const saving = status === 'saving';
    // Retain keyboard focus while Retry is unavailable; return it only if the
    // disappearing status still owns focus, never after the user has moved on.
    if (status === 'saved' && notice.contains(document.activeElement)) {
      document.getElementById('task')?.focus({ preventScroll: true });
    }
    notice.hidden = status === 'saved';
    const message = document.getElementById('ui-save-message');
    const detail = document.getElementById('ui-save-detail');
    if (message) message.textContent = saving ? 'Saving…' : 'Not saved';
    if (detail) detail.textContent = saving ? 'Saving local changes' : 'Could not save local changes';
    retry.setAttribute('aria-disabled', String(saving));
  }

  function saveUiStateToServer() {
    if (syncConflict) return;
    if (saveInFlight) {
      saveQueued = true;
      return;
    }
    saveInFlight = true;
    saveQueued = false;
    // Routine saves stay silent. Only an already-visible failure gets progress.
    if (!document.getElementById('ui-save-notice')?.hidden) saveNotice('saving');
    const base = serverRevision === null ? getRevision() : serverRevision;
    savePromise = fetch('/api/ui_state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: currentUiState(), base_revision: base }),
    })
      .then(async (r) => {
        const data = await r.json();
        if (r.ok) {
          const next = Number((data && data.revision) || 0) || 0;
          if (next) {
            serverRevision = next;
            setRevision(next);
          }
          syncConflict = false;
          if (!saveQueued && uiStatePersistTimer === null) saveNotice('saved');
          return true;
        }
        if (r.status === 409) {
          const current = Number((data && data.revision) || 0) || 0;
          if (current) serverRevision = current;
          if (!syncConflict) {
            syncConflict = true;
            saveNotice('failed');
          }
          return false;
        }
        // Other failures: keep local content, do not pretend success.
        saveNotice('failed');
        return false;
      })
      .catch(() => {
        saveNotice('failed');
        return false;
      })
      .finally(() => {
        saveInFlight = false;
        savePromise = null;
        if (saveQueued && !syncConflict) {
          saveQueued = false;
          saveUiStateToServer();
        } else {
          saveQueued = false;
        }
      });
  }

  let uiStatePersistTimer: ReturnType<typeof setTimeout> | null = null;
  const UI_STATE_PERSIST_DELAY = 400;

  function markUiStateDirty() {
    setUpdatedAt(Math.max(Date.now(), getUpdatedAt()));
    setRevision(getRevision() + 1);
    setDirtySinceBoot(true);
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
    if (syncConflict) return false;
    flushUiState();
    while (savePromise) if (!(await savePromise)) return false;
    return !syncConflict;
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
    if (!retry) return;
    retry.onclick = () => {
      if (saveInFlight) return;
      syncConflict = false;
      flushUiState();
    };
    window.addEventListener('pagehide', () => {
      runtime.Composer.captureDraft();
      if (uiStatePersistTimer !== null) {
        clearTimeout(uiStatePersistTimer);
        uiStatePersistTimer = null;
      }
      cacheUiState();
      if (syncConflict) return;
      const base = serverRevision === null ? getRevision() : serverRevision;
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

  async function restoreUiStateFromServer() {
    try {
      const r = await fetch('/api/ui_state', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      if (!hasStoredUiState(data.state)) {
        saveUiStateToServer();
        return;
      }
      if (
        ((deps.bootHadMeaningfulUiState && deps.bootHadMeaningfulUiState()) || isDirtySinceBoot()) &&
        isUiStateNewer(currentUiState(), data.state)
      ) {
        saveUiStateToServer();
        return;
      }
      applyUiState(data.state);
      cacheUiState();
      if (deps.renderSidebar) deps.renderSidebar();
      if (deps.renderChat) deps.renderChat();
      if (deps.updateComposerContext) deps.updateComposerContext();
      if (deps.syncProviderUI && deps.currentProviderId) deps.syncProviderUI(deps.currentProviderId());
    } catch {}
  }

  runtime.UiState = {
    init,
    setDrawerOpen,
    DEFAULT_PROVIDER,
    PROVIDER_LABELS,
    PROVIDERS,
    setProviders,
    setDefaultProvider,
    setRecommended,
    uid,
    defaultSession,
    hydrateSessionIndexes,
    ensureSessionIndexes,
    trackSessionMessage,
    untrackSessionMessages,
    pathName,
    pathKey,
    apply: applyUiState,
    current: currentUiState,
    cache: cacheUiState,
    cached: cachedUiState,
    hasMeaningful: hasMeaningfulUiState,
    hasStored: hasStoredUiState,
    isNewer: isUiStateNewer,
    markDirty: markUiStateDirty,
    flush: flushUiState,
    persist: persistActive,
    persistNow: persistActiveNow,
    bindPagehide: bindUiStatePagehide,
    restoreFromServer: restoreUiStateFromServer,
    ensureSaved,
  };
})();
