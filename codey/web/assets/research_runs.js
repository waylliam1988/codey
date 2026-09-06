/* Codey research run runtime: session artifacts, restore state, and drawer state. */
(function () {
'use strict';

let deps = {};
let researchDrawerTab = 'evidence';
let researchGraphDepth = 1;
let researchNoteFocusId = '';

function init(nextDeps) {
  deps = nextDeps || {};
}

function activeId() {
  return deps.getActiveId ? deps.getActiveId() : '';
}

function sessions() {
  return deps.getSessions ? deps.getSessions() : [];
}

function normalizeResearchRunArtifact(runId, research, receipt) {
  research = research && typeof research === 'object' ? research : {};
  return {
    runId,
    synthesisId: (research.synthesis_id || '').toString(),
    notesCreated: Array.isArray(research.notes_created) ? research.notes_created.map(String) : [],
    notesUpdated: Array.isArray(research.notes_updated) ? research.notes_updated.map(String) : [],
    sourceUrls: Array.isArray(research.source_urls) ? research.source_urls.map(String) : [],
    sourcesRead: Number(research.sources_read || 0),
    queries: Array.isArray(research.queries) ? research.queries.map(String) : [],
    searchResults: Array.isArray(research.search_results) ? research.search_results : [],
    openedSources: Array.isArray(research.opened_sources) ? research.opened_sources : [],
    coverage: research.coverage && typeof research.coverage === 'object' ? research.coverage : {},
    citationMap: Array.isArray(research.citation_map) ? research.citation_map : [],
    evidenceItems: Array.isArray(research.evidence_items) ? research.evidence_items : [],
    counterpoints: Array.isArray(research.counterpoints) ? research.counterpoints.map(String) : [],
    qualityWarnings: Array.isArray(research.quality_warnings) ? research.quality_warnings.map(String) : [],
    receipt: deps.receiptSummary ? deps.receiptSummary(receipt || {}) : '',
    restoreable: true,
    createdAt: Date.now(),
  };
}

function recordResearchRun(sessionId, runId, research, receipt) {
  const s = sessions().find(x => x.id === sessionId);
  if (!s) return null;
  const item = normalizeResearchRunArtifact(runId, research, receipt);
  researchDrawerTab = 'evidence';
  researchNoteFocusId = '';
  s.researchRuns = Array.isArray(s.researchRuns)
    ? s.researchRuns.filter(run => run.runId !== runId)
    : [];
  s.researchRuns.push(item);
  s.researchRuns = s.researchRuns.slice(-32);
  invalidateResearchNoteCache(noteIdsForResearchRun(item));
  if (deps.persistActive) deps.persistActive();
  return item;
}

function currentResearchRun(sessionId = activeId()) {
  const s = sessions().find(x => x.id === sessionId);
  const runs = s && Array.isArray(s.researchRuns) ? s.researchRuns : [];
  return runs.length ? runs[runs.length - 1] : null;
}

function disposeResearchGraph() {
  window.CodeyResearchDrawer.dispose();
}

function openResearchDrawer(sessionId = activeId()) {
  window.CodeyResearchDrawer.open(sessionId);
}

function closeResearchDrawer() {
  window.CodeyResearchDrawer.close();
}

function renderResearchDrawer(sessionId = activeId()) {
  window.CodeyResearchDrawer.render(sessionId);
}

function coreNoteIdsForResearchRun(run) {
  return window.CodeyResearchDrawer.coreNoteIdsForRun(run);
}

function noteIdsForResearchRun(run) {
  return window.CodeyResearchDrawer.noteIdsForRun(run);
}

function invalidateResearchNoteCache(ids) {
  return window.CodeyResearchDrawer.invalidateNoteCache(ids);
}

async function restoreResearchRun() {
  const run = currentResearchRun();
  if (!run || !run.runId) return;
  deps.$('research-restore').disabled = true;
  try {
    const r = await fetch('/api/research/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: run.runId }),
    });
    let data = {};
    try { data = await r.json(); } catch {}
    if (!r.ok || !data.ok) {
      deps.$('research-subtitle').textContent = data.error || 'Restore failed';
      if (run && /not found/i.test(data.error || '')) {
        run.restoreable = false;
        if (deps.persistActiveNow) deps.persistActiveNow();
        renderResearchDrawer(activeId());
      }
      return;
    }
    deps.$('research-subtitle').textContent = `${(data.restored || []).length} restored`;
    invalidateResearchNoteCache(noteIdsForResearchRun(run));
    const s = deps.activeSession ? deps.activeSession() : null;
    if (s && Array.isArray(s.researchRuns)) {
      s.researchRuns = s.researchRuns.filter(item => item.runId !== run.runId);
      if (deps.persistActiveNow) deps.persistActiveNow();
    }
    if (deps.addToSession) {
      deps.addToSession(activeId(), {
        type: 'done',
        text: `Research restored: ${(data.restored || []).length} files`,
        eventKey: `research-restore:${run.runId}`,
      });
    }
    renderResearchDrawer(activeId());
  } finally {
    const current = currentResearchRun();
    deps.$('research-restore').disabled = !current || !current.restoreable;
  }
}

function markResearchRestoreAvailability(runIds) {
  const available = new Set(Array.isArray(runIds) ? runIds.map(String) : []);
  let changed = false;
  for (const s of sessions()) {
    const runs = Array.isArray(s.researchRuns) ? s.researchRuns : [];
    for (const run of runs) {
      const next = available.has(String(run.runId || ''));
      if (!!run.restoreable !== next) {
        run.restoreable = next;
        changed = true;
      }
    }
  }
  if (changed && deps.persistActive) deps.persistActive();
  if (deps.$('research-drawer').classList.contains('open')) renderResearchDrawer(activeId());
}

window.CodeyResearchRuns = {
  init,
  normalize: normalizeResearchRunArtifact,
  record: recordResearchRun,
  current: currentResearchRun,
  disposeGraph: disposeResearchGraph,
  open: openResearchDrawer,
  close: closeResearchDrawer,
  render: renderResearchDrawer,
  coreNoteIdsForRun: coreNoteIdsForResearchRun,
  noteIdsForRun: noteIdsForResearchRun,
  invalidateNoteCache: invalidateResearchNoteCache,
  restore: restoreResearchRun,
  markRestoreAvailability: markResearchRestoreAvailability,
  getGraphDepth: () => researchGraphDepth,
  setGraphDepth: value => { researchGraphDepth = value; },
  getNoteFocusId: () => researchNoteFocusId,
  setNoteFocusId: value => { researchNoteFocusId = value; },
  getDrawerTab: () => researchDrawerTab,
  setDrawerTab: value => { researchDrawerTab = value; },
};
})();
