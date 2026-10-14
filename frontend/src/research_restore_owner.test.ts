import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from './types/domain';

describe('research restore owner', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('removes the restored run from its owner session after switching sessions', async () => {
    const a: Session = {
      id: 'a',
      title: 'A',
      draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
      messages: [],
      researchRuns: [
        {
          runId: 'run-a',
          restoreable: true,
          notesCreated: [],
          notesUpdated: [],
          sourceUrls: [],
          queries: [],
          searchResults: [],
          openedSources: [],
          coverage: {},
          citationMap: [],
          evidenceItems: [],
          counterpoints: [],
          qualityWarnings: [],
        },
      ],
    };
    const b: Session = {
      id: 'b',
      title: 'B',
      draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
      messages: [],
      researchRuns: [],
    };
    let active = 'a';
    window.ResearchRuns = {} as typeof window.ResearchRuns;
    await import('./research_runs');
    window.ResearchRuns.init({
      getSessions: () => [a, b],
      getActiveId: () => active,
      activeSession: () => [a, b].find((s) => s.id === active),
      persistActive: vi.fn(),
      persistActiveNow: vi.fn(),
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ ok: true, restored: [{ path: 'x' }] }) })),
    );
    const promise = window.ResearchRuns.restore('a');
    active = 'b';
    await promise;
    expect(a.researchRuns).toHaveLength(0);
    expect(b.researchRuns).toHaveLength(0);
    expect((await promise)?.ownerSessionId).toBe('a');
  });
});
