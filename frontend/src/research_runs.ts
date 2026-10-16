import { runtime } from './runtime';
import type { ResearchPayload, ResearchRestoreResult, ResearchRun, Session, UiDeps } from './types/domain';
/* Codey research run runtime: session artifacts and restore state. */
(function () {
  'use strict';

  let deps: UiDeps = {};

  function init(nextDeps: UiDeps): void {
    deps = nextDeps || {};
  }

  function activeId(): string {
    return deps.getActiveId ? deps.getActiveId() : '';
  }

  function sessions(): Session[] {
    return deps.getSessions ? deps.getSessions() : [];
  }

  function normalizeResearchRunArtifact(runId: string, research: ResearchPayload, receipt: unknown): ResearchRun {
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
      restoreable: research.restoreable === true,
      createdAt: Date.now(),
    };
  }

  function recordResearchRun(
    sessionId: string,
    runId: string,
    research: ResearchPayload,
    receipt: unknown,
  ): ResearchRun | null {
    const s = sessions().find((x) => x.id === sessionId);
    if (!s) return null;
    const item = normalizeResearchRunArtifact(runId, research, receipt);
    s.researchRuns = Array.isArray(s.researchRuns) ? s.researchRuns.filter((run) => run.runId !== runId) : [];
    s.researchRuns.push(item);
    s.researchRuns = s.researchRuns.slice(-32);
    if (deps.persistActive) deps.persistActive();
    return item;
  }

  function currentResearchRun(sessionId: string = activeId()): ResearchRun | null {
    const s = sessions().find((x) => x.id === sessionId);
    const runs = s && Array.isArray(s.researchRuns) ? s.researchRuns : [];
    return runs.length ? runs[runs.length - 1] : null;
  }

  function researchRunFor(sessionId: string, runId: string): ResearchRun | null {
    const s = sessions().find((x) => x.id === sessionId);
    const runs = s && Array.isArray(s.researchRuns) ? s.researchRuns : [];
    return runs.find((run) => run.runId === runId) || null;
  }

  function researchRunForMessage(sessionId: string, runId: string): ResearchRun | null {
    if (runId) return researchRunFor(sessionId, runId);
    const s = sessions().find((x) => x.id === sessionId);
    const runs = s && Array.isArray(s.researchRuns) ? s.researchRuns : [];
    return runs.length === 1 ? runs[0] : null;
  }

  async function restoreResearchRun(ownerSessionId = activeId()): Promise<ResearchRestoreResult | null> {
    const run = currentResearchRun(ownerSessionId);
    if (!run || !run.runId) return null;
    const r = await fetch('/api/research/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: run.runId }),
    });
    let data: { ok?: boolean; error?: string; restored?: unknown[] } = {};
    try {
      data = await r.json();
    } catch {}
    if (!r.ok || data.ok !== true) {
      const error = typeof data.error === 'string' ? data.error : 'Restore failed';
      if (/not found/i.test(error)) {
        run.restoreable = false;
        if (deps.persistActiveNow) deps.persistActiveNow();
      }
      return { ok: false, ownerSessionId, run, message: error, restored: [] };
    }
    const restored = Array.isArray(data.restored) ? data.restored : [];
    const s = sessions().find((item) => item.id === ownerSessionId) ?? null;
    if (s && Array.isArray(s.researchRuns)) {
      s.researchRuns = s.researchRuns.filter((item: ResearchRun) => item.runId !== run.runId);
      if (deps.persistActiveNow) deps.persistActiveNow();
    }
    return { ok: true, ownerSessionId, run, message: `${restored.length} restored`, restored };
  }

  function markResearchRestoreAvailability(runIds: unknown): boolean {
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
    return changed;
  }

  runtime.ResearchRuns = {
    init,
    normalize: normalizeResearchRunArtifact,
    record: recordResearchRun,
    current: currentResearchRun,
    forRun: researchRunFor,
    forMessage: researchRunForMessage,
    restore: restoreResearchRun,
    markRestoreAvailability: markResearchRestoreAvailability,
  };
})();
