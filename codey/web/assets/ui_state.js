const LS_SESSIONS = 'codey:sessions';
const LS_ACTIVE = 'codey:active';
const LS_PROJECTS = 'codey:projects';
const LS_UI_UPDATED = 'codey:ui-updated';
const LS_UI_REVISION = 'codey:ui-revision';
const DEFAULT_PROVIDER = 'deepseek';
const PROVIDER_LABELS = { deepseek: 'DeepSeek', mimo: 'MiMo', stepfun: 'StepFun', qwen: 'Qwen', glm: 'GLM', local: 'Local' };
const PROVIDERS = Object.keys(PROVIDER_LABELS);

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
  sessions = normalizeSessions(state && state.sessions);
  projects = normalizeProjects(state && state.projects);
  activeId = state && state.active_id ? String(state.active_id) : '';
  uiStateUpdatedAt = Number(state && state.updated_at || 0) || 0;
  uiStateRevision = Number(state && state.revision || 0) || 0;
  if (!sessions.length) {
    const s = defaultSession();
    sessions = [s];
    activeId = s.id;
  }
  if (!sessions.find(s => s.id === activeId)) activeId = sessions[0].id;
}
function currentUiState() {
  return { active_id: activeId, sessions, projects, updated_at: uiStateUpdatedAt, revision: uiStateRevision };
}
function cacheUiState() {
  saveSessions(sessions);
  saveProjects(projects);
  safeLocalSet(LS_ACTIVE, activeId);
  safeLocalSet(LS_UI_UPDATED, String(uiStateUpdatedAt));
  safeLocalSet(LS_UI_REVISION, String(uiStateRevision));
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
async function restoreUiStateFromServer() {
  try {
    const r = await fetch('/api/ui_state', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    if (!hasStoredUiState(data.state)) {
      saveUiStateToServer();
      return;
    }
    if ((bootHadMeaningfulUiState || uiStateDirtySinceBoot) && isUiStateNewer(currentUiState(), data.state)) {
      saveUiStateToServer();
      return;
    }
    applyUiState(data.state);
    cacheUiState();
    renderSidebar();
    renderChat();
    updateComposerContext();
    syncProviderUI(currentProviderId());
  } catch {}
}
