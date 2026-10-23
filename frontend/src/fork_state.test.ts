import { expect, it } from 'vitest';
import { mergeUiState } from './ui_state_merge';

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
  const result = mergeUiState(baseline, current, server);
  expect(result.active_id).toBe('b');
  expect(result.revision).toBe(9);
  expect(result.sessions).toEqual([
    { id: 'a', title: 'Old', draft: { text: 'typing' } },
    { id: 'b', title: 'Local new chat' },
    { id: 'fork', title: 'Fork' },
  ]);
  expect(result.projects).toEqual([{ id: 'p', name: 'New project' }]);
});
