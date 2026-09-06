/* Codey UI state runtime: local cache, durable snapshot sync, and versioning. */
(function () {
'use strict';

let deps = {};
const LS_SESSIONS = 'codey:sessions';
const LS_ACTIVE = 'codey:active';
const LS_PROJECTS = 'codey:projects';
const LS_UI_UPDATED = 'codey:ui-updated';
const LS_UI_REVISION = 'codey:ui-revision';
const DEFAULT_PROVIDER = 'deepseek';
const PROVIDER_LABELS = { deepseek: 'DeepSeek', mimo: 'MiMo', stepfun: 'StepFun', qwen: 'Qwen', glm: 'GLM', local: 'Local' };
const PROVIDERS = Object.keys(PROVIDER_LABELS);

function init(nextDeps) {
  deps = nextDeps || {};
}

function getSessions() { return deps.getSessions ? deps.getSessions() : []; }
function setSessions(value) { if (deps.setSessions) deps.setSessions(value); }
function getProjects() { return deps.getProjects ? deps.getProjects() : []; }
function setProjects(value) { if (deps.setProjects) deps.setProjects(value); }
function getActiveId() { return deps.getActiveId ? deps.getActiveId() : ''; }
function setActiveId(value) { if (deps.setActiveId) deps.setActiveId(value); }
function getUpdatedAt() { return deps.getUpdatedAt ? deps.getUpdatedAt() : 0; }
function setUpdatedAt(value) { if (deps.setUpdatedAt) deps.setUpdatedAt(value); }
function getRevision() { return deps.getRevision ? deps.getRevision() : 0; }
function setRevision(value) { if (deps.setRevision) deps.setRevision(value); }
function isDirtySinceBoot() { return !!(deps.isDirtySinceBoot && deps.isDirtySinceBoot()); }
function setDirtySinceBoot(value) { if (deps.setDirtySinceBoot) deps.setDirtySinceBoot(value); }

function uid() { return Math.random().toString(36).slice(2, 10); }
function loadJson(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key) || JSON.stringify(fallback)); } catch { return fallback; }
}
function safeLocalSet(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}
function saveSessions(arr) { safeLocalSet(LS_SESSIONS, JSON.stringify(arr)); }
function saveProjects(arr) { safeLocalSet(LS_PROJECTS, JSON.stringify(arr)); }
function defaultSession(projectId = null, provider = DEFAULT_PROVIDER) {
  return { id: uid(), title: 'New chat', messages: [], terminalRuns: [], researchRuns: [], createdAt: Date.now(), projectId, provider, research: false };
}
function pathName(path) {
  const trimmed = (path || '').replace(/[\\\/]+$/, '');
  return trimmed.split(/[\\\/]/).pop() || trimmed || 'project';
}
function pathKey(path) { return (path || '').replace(/[\\\/]+$/, '').toLowerCase(); }

function normalizeStoredResearchRun(run) {
  run = run && typeof run === 'object' ? run : {};
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

function normalizeSessions(value) {
  return (Array.isArray(value) ? value : []).map(s => ({
    id: s.id || uid(),
    title: s.title || 'New chat',
    messages: Array.isArray(s.messages) ? s.messages : [],
    terminalRuns: Array.isArray(s.terminalRuns) ? s.terminalRuns.slice(-32) : [],
    researchRuns: Array.isArray(s.researchRuns)
      ? s.researchRuns.slice(-32).map(normalizeStoredResearchRun)
      : [],
    createdAt: s.createdAt || Date.now(),
    projectId: s.projectId || null,
    provider: PROVIDERS.includes(s.provider) ? s.provider : DEFAULT_PROVIDER,
    research: !!s.research,
  }));
}
function normalizeProjects(value) {
  return (Array.isArray(value) ? value : []).map(p => ({
    id: p.id || uid(),
    name: p.name || pathName(p.path),
    path: p.path || '',
    expanded: p.expanded !== false,
    createdAt: p.createdAt || Date.now(),
  })).filter(p => p.path);
}
function applyUiState(state) {
  let nextSessions = normalizeSessions(state && state.sessions);
  const nextProjects = normalizeProjects(state && state.projects);
  let nextActiveId = state && state.active_id ? String(state.active_id) : '';
  if (!nextSessions.length) {
    const s = defaultSession();
    nextSessions = [s];
    nextActiveId = s.id;
  }
  if (!nextSessions.find(s => s.id === nextActiveId)) nextActiveId = nextSessions[0].id;
  setSessions(nextSessions);
  setProjects(nextProjects);
  setActiveId(nextActiveId);
  setUpdatedAt(Number(state && state.updated_at || 0) || 0);
  setRevision(Number(state && state.revision || 0) || 0);
}
function currentUiState() {
  return {
    active_id: getActiveId(),
    sessions: getSessions(),
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
function cachedUiState() {
  return {
    active_id: localStorage.getItem(LS_ACTIVE) || '',
    sessions: loadJson(LS_SESSIONS, []),
    projects: loadJson(LS_PROJECTS, []),
    updated_at: Number(localStorage.getItem(LS_UI_UPDATED) || 0) || 0,
    revision: Number(localStorage.getItem(LS_UI_REVISION) || 0) || 0,
  };
}
function uiStateVersion(state) {
  return [
    Number(state && state.updated_at || 0) || 0,
    Number(state && state.revision || 0) || 0,
  ];
}
function isUiStateNewer(a, b) {
  const av = uiStateVersion(a);
  const bv = uiStateVersion(b);
  return av[0] > bv[0] || (av[0] === bv[0] && av[1] > bv[1]);
}
function hasStoredUiState(state) {
  return !!(
    state &&
    ((Array.isArray(state.sessions) && state.sessions.length) ||
      (Array.isArray(state.projects) && state.projects.length) ||
      state.active_id)
  );
}
function hasMeaningfulUiState(state) {
  if (!state) return false;
  if (Array.isArray(state.projects) && state.projects.length) return true;
  if (!Array.isArray(state.sessions)) return false;
  return state.sessions.some(s =>
    (Array.isArray(s.messages) && s.messages.length) ||
    (Array.isArray(s.terminalRuns) && s.terminalRuns.length) ||
    (s.title && s.title !== 'New chat')
  );
}
function saveUiStateToServer() {
  fetch('/api/ui_state', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state: currentUiState() }),
  }).catch(() => {});
}

let uiStatePersistTimer = null;
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

function bindUiStatePagehide() {
  window.addEventListener('pagehide', () => {
    if (uiStatePersistTimer !== null) {
      clearTimeout(uiStatePersistTimer);
      uiStatePersistTimer = null;
    }
    cacheUiState();
    const payload = JSON.stringify({ state: currentUiState() });
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
    if (((deps.bootHadMeaningfulUiState && deps.bootHadMeaningfulUiState()) || isDirtySinceBoot()) && isUiStateNewer(currentUiState(), data.state)) {
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

window.CodeyUiState = {
  init,
  DEFAULT_PROVIDER,
  PROVIDER_LABELS,
  PROVIDERS,
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
  markDirty: markUiStateDirty,
  flush: flushUiState,
  persist: persistActive,
  persistNow: persistActiveNow,
  bindPagehide: bindUiStatePagehide,
  restoreFromServer: restoreUiStateFromServer,
};
})();
