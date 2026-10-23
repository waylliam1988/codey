import { expect, it } from 'vitest';
import { mergeUiState } from './ui_state_merge';

const base = {
  revision: 1,
  active_id: 'a',
  sessions: [{ id: 'a', title: 'Alpha', draft: { text: '' } }],
  projects: [],
};
it('merges concurrent fields without resurrecting remote deletions', () => {
  const local = { ...base, sessions: [{ ...base.sessions[0], draft: { text: 'local' } }] };
  const remote = {
    ...base,
    revision: 2,
    sessions: [
      { ...base.sessions[0], title: 'Remote' },
      { id: 'b', title: 'Beta' },
    ],
  };
  expect(mergeUiState(base, local, remote).sessions).toEqual([
    { id: 'a', title: 'Remote', draft: { text: 'local' } },
    { id: 'b', title: 'Beta' },
  ]);
  expect(mergeUiState(base, local, { ...remote, sessions: [] }).sessions).toEqual([]);
});
it('keeps explicit local deletion and replacement while retaining another clients new chat', () => {
  const local = { ...base, active_id: 'c', sessions: [{ id: 'c', title: 'New chat' }] };
  const remote = { ...base, revision: 3, sessions: [...base.sessions, { id: 'b', title: 'Beta' }] };
  expect(mergeUiState(base, local, remote).sessions.map((row) => row.id)).toEqual(['c', 'b']);
});

it('does not interpret server JSON key order as a local draft edit', () => {
  const original = { ...base, sessions: [{ id: 'a', draft: { text: 'same', start: 0 } }] };
  const reordered = { ...original, sessions: [{ id: 'a', draft: { start: 0, text: 'same' } }] };
  const remote = { ...original, sessions: [{ id: 'a', draft: { text: 'remote edit', start: 0 } }] };
  expect(mergeUiState(original, reordered, remote).sessions[0].draft).toEqual({ text: 'remote edit', start: 0 });
});

it('honors deletion identities even when the original creation acknowledgement was lost', () => {
  const empty = { sessions: [], projects: [], revision: 0 };
  const local = { sessions: [{ id: 'lost' }], projects: [{ id: 'lost-project' }], active_id: 'lost' };
  const remote = { ...empty, revision: 3, deleted_ids: { sessions: ['lost'], projects: ['lost-project'] } };
  const result = mergeUiState(empty, local, remote);
  expect(result.sessions).toEqual([]);
  expect(result.projects).toEqual([]);
  expect(mergeUiState(empty, { ...local, sessions: [{ id: 'never-saved' }] }, remote).sessions).toEqual([
    { id: 'never-saved' },
  ]);
});
