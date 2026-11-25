import type { Message, ResearchPayload, Session } from '../types/domain';
import type { DocumentRef } from '../workspace/document_pane';
import type { Workspace } from '../workspace/workspace';
import { ResearchDrawer } from './research_drawer';
import { ResearchRuns } from './research_runs';

interface ResearchPanelDeps {
  workspace: Pick<Workspace, 'target' | 'getTarget'>;
  element(id: string): HTMLElement;
  activeId(): string;
  sessions(): Session[];
  receiptSummary(receipt: unknown): string;
  persistActive(): void;
  persistActiveNow(): void;
  openDocument(sessionId: string, ref: DocumentRef): void;
  copyText(text: string): Promise<boolean>;
  addToSession(sessionId: string, message: Message): boolean;
}

export function createResearchPanel(deps: ResearchPanelDeps) {
  function init(): void {
    ResearchRuns.init({
      getActiveId: deps.activeId,
      getSessions: deps.sessions,
      receiptSummary: deps.receiptSummary,
      persistActive: deps.persistActive,
      persistActiveNow: deps.persistActiveNow,
    });
    ResearchDrawer.init({
      targetResearch: (runId) => deps.workspace.target('research', { runId }),
      openDocument: deps.openDocument,
      $: deps.element,
      getActiveId: deps.activeId,
      getSessionTitle: (id) => deps.sessions().find((session) => session.id === id)?.title || 'Chat',
      getCurrentRun: ResearchRuns.forRun,
      getResearchGraphDepth: () => Number(deps.workspace.getTarget('research').depth) || 1,
      setResearchGraphDepth: (value) => deps.workspace.target('research', { depth: String(value) }),
      getResearchNoteFocusId: () => deps.workspace.getTarget('research').note || '',
      setResearchNoteFocusId: (value) => deps.workspace.target('research', { note: value }),
      getResearchDrawerTab: () => deps.workspace.getTarget('research').tab || 'evidence',
      setResearchDrawerTab: (value) => deps.workspace.target('research', { tab: value }),
      copyText: deps.copyText,
    });
  }

  function recordResearchRun(sessionId: string, runId: string, research: ResearchPayload, receipt: unknown) {
    const item = ResearchRuns.record(sessionId, runId, research, receipt);
    if (!item) return null;
    ResearchDrawer.invalidateNoteCache(ResearchDrawer.noteIdsForRun(item));
    return item;
  }

  function currentResearchRun(sessionId = deps.activeId()) {
    return ResearchRuns.current(sessionId);
  }

  async function restoreResearchRun() {
    const restore = deps.element('research-restore');
    if (restore instanceof HTMLButtonElement) restore.disabled = true;
    const ownerSessionId = deps.activeId();
    try {
      const result = await ResearchRuns.restore(ownerSessionId);
      if (!result) return;
      const subtitle = deps.activeId() === ownerSessionId ? deps.element('research-subtitle') : null;
      if (subtitle) subtitle.textContent = String(result.message || '');
      if (result.run) {
        ResearchDrawer.invalidateNoteCache(ResearchDrawer.noteIdsForRun(result.run));
      }
      if (result.ok) {
        deps.addToSession(result.ownerSessionId, {
          type: 'done',
          text: `Research restored: ${result.restored.length} files`,
          eventKey: `research-restore:${String(result.run.runId || '')}`,
        });
      }
      if (deps.activeId() === result.ownerSessionId) ResearchDrawer.render(deps.activeId());
    } finally {
      const current = currentResearchRun();
      if (restore instanceof HTMLButtonElement) restore.disabled = !current || !current.restoreable;
    }
  }

  function markResearchRestoreAvailability(runIds: unknown) {
    const changed = ResearchRuns.markRestoreAvailability(runIds);
    const drawer = deps.element('research-drawer');
    if (changed && drawer && drawer.classList.contains('open')) {
      ResearchDrawer.render(deps.activeId());
    }
  }

  return { init, recordResearchRun, currentResearchRun, restoreResearchRun, markResearchRestoreAvailability };
}
