import { beforeEach, describe, expect, it } from 'vitest';
import './ui_state';
import type { Session } from './types/domain';

describe('browser capability no longer depends on persisted chat settings', () => {
  let sessions: Session[] = [];

  beforeEach(() => {
    sessions = [];
    window.UiState.init({
      getSessions: () => sessions,
      setSessions: (value) => {
        sessions = value;
      },
    });
  });

  it.each([false, true])('drops the old browser=%s toggle when restoring a chat', (browser) => {
    window.UiState.apply({ sessions: [{ id: 'old-chat', browser }], active_id: 'old-chat' });
    expect(sessions[0]).not.toHaveProperty('browser');
    expect(window.UiState.current().sessions).toEqual(
      expect.arrayContaining([expect.not.objectContaining({ browser })]),
    );
  });

  it.each([null, 'project'])('creates a chat without a browser toggle for project %s', (projectId) => {
    expect(window.UiState.defaultSession(projectId)).not.toHaveProperty('browser');
  });
});
