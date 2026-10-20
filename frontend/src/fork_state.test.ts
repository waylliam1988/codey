import { expect, it } from 'vitest';
import { mergeForkState } from './fork_state';

it('preserves drafts and renamed projects changed during admission without dropping a saved new branch', () => {
  const baseline = {
    active_id: 'a',
    sessions: [{ id: 'a', title: 'Old', draft: { text: '' } }],
    projects: [{ id: 'p', name: 'Old project' }],
  };
  const current = {
    active_id: 'b',
    sessions: [
      { id: 'a', title: 'Old', draft: { text: 'typing' } },
      { id: 'b', title: 'Local new chat' },
    ],
    projects: [{ id: 'p', name: 'New project' }],
  };
  const server = {
    revision: 9,
    sessions: [{ id: 'fork', title: 'Fork' }, ...baseline.sessions],
    projects: baseline.projects,
  };
  const result = mergeForkState(server, baseline, current);
  expect(result.active_id).toBe('b');
  expect(result.revision).toBe(9);
  expect(result.sessions).toEqual([
    { id: 'fork', title: 'Fork' },
    { id: 'a', title: 'Old', draft: { text: 'typing' } },
    { id: 'b', title: 'Local new chat' },
  ]);
  expect(result.projects).toEqual([{ id: 'p', name: 'New project' }]);
});
