import { beforeEach, describe, expect, it, vi } from 'vitest';
import './ui_state';
import type { Project, Session } from './types/domain';

declare global {
  interface Window {
    __testSessions: Array<{ id: string }>;
    __testProjects: Array<{ path: string }>;
    __testActiveId: string;
  }
}

describe('ui state persistence boundary', () => {
  beforeEach(() => {
    window.UiState.init({
      getSessions: () => [],
      setSessions: (value: Session[]) => {
        window.__testSessions = value;
      },
      getProjects: () => [],
      setProjects: (value: Project[]) => {
        window.__testProjects = value;
      },
      getActiveId: () => '',
      setActiveId: (value: string) => {
        window.__testActiveId = value;
      },
      getUpdatedAt: () => 0,
      setUpdatedAt: () => undefined,
      getRevision: () => 0,
      setRevision: () => undefined,
    });
    window.UiState.accept({ sessions: [], projects: [], active_id: '', revision: 0 });
  });

  it('ignores malformed persisted entries without throwing', () => {
    expect(() =>
      window.UiState.apply({
        sessions: [null, 42, { id: 's1', messages: [{ type: 'user', text: 'hello' }] }],
        projects: [null, { path: 'C:/project' }],
        active_id: 's1',
      }),
    ).not.toThrow();

    expect(window.__testSessions).toHaveLength(3);
    expect(window.__testSessions.map(({ id }) => id)).toContain('s1');
    expect(window.__testProjects).toHaveLength(1);
  });

  it('waits for the server save before an input can depend on a new session', async () => {
    let release: (response: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            release = resolve;
          }),
      ),
    );
    let confirmed = false;
    const saved = window.UiState.ensureSaved().then((ok) => {
      confirmed = ok;
      return ok;
    });
    await Promise.resolve();
    expect(confirmed).toBe(false);
    release({ ok: true, json: async () => ({ revision: 1 }) } as Response);
    expect(await saved).toBe(true);
    vi.unstubAllGlobals();
  });

  it('persists only UI metadata and loads messages from durable history', () => {
    const session = {
      id: 's1',
      title: 'Chat',
      messages: [{ type: 'user', text: 'secret transcript' }],
      draft: { text: 'draft', start: 0, end: 0, direction: 'none', revision: 1 },
    } as Session;
    window.UiState.init({
      getSessions: () => [session],
      setSessions: (value) => {
        window.__testSessions = value;
      },
    });
    const current = window.UiState.current();
    expect((current.sessions as object[])[0]).not.toHaveProperty('messages');
    window.UiState.cache();
    expect(localStorage.getItem('codey:ui-cache')).not.toContain('secret transcript');
    window.UiState.apply({ sessions: [{ id: 's1', title: 'Chat', draft: session.draft }] });
    expect(window.__testSessions[0]).toMatchObject({
      messages: [{ type: 'user', text: 'secret transcript' }],
      draft: { text: 'draft' },
    });
    expect(session.messages).toHaveLength(1);
  });
});
