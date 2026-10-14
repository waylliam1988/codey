import { beforeEach, describe, expect, it } from 'vitest';
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
});
