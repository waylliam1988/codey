import { afterEach, describe, expect, it, vi } from 'vitest';
import { QuestionDraftStore } from './question_draft_store';

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe('QuestionDraftStore', () => {
  it('round trips a validated custom answer and isolates instance/session/question', () => {
    const first = new QuestionDraftStore({ instanceId: 'one' });
    first.save('s1', 'q1', {
      question_revision: 2,
      selected: 'Write an answer',
      custom: true,
      text: '中文回答',
      edit_revision: 3,
      writer_id: 'w1',
      saved_at: 1,
    });
    expect(
      new QuestionDraftStore({ instanceId: 'one' }).load('s1', 'q1', {
        revision: 2,
        options: ['A', 'Write an answer'],
        allow_free_text: true,
      }),
    ).toMatchObject({ text: '中文回答', custom: true });
    expect(
      new QuestionDraftStore({ instanceId: 'two' }).load('s1', 'q1', {
        revision: 2,
        options: ['A', 'Write an answer'],
        allow_free_text: true,
      }),
    ).toBeNull();
  });

  it('rejects stale option revisions and invalid choices', () => {
    const store = new QuestionDraftStore({ instanceId: 'one' });
    store.save('s1', 'q1', {
      question_revision: 1,
      selected: 'A',
      custom: false,
      text: '',
      edit_revision: 1,
      writer_id: 'w1',
      saved_at: 1,
    });
    expect(store.load('s1', 'q1', { revision: 2, options: ['A'], allow_free_text: false })).toBeNull();
    store.save('s1', 'q2', {
      question_revision: 2,
      selected: 'bad',
      custom: false,
      text: '',
      edit_revision: 1,
      writer_id: 'w1',
      saved_at: 1,
    });
    expect(store.load('s1', 'q2', { revision: 2, options: ['A'], allow_free_text: false })).toBeNull();
  });

  it('keeps an in-memory draft when storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });
    const store = new QuestionDraftStore({ instanceId: 'one' });
    expect(() =>
      store.save('s1', 'q1', {
        question_revision: 1,
        selected: 'A',
        custom: false,
        text: '',
        edit_revision: 1,
        writer_id: 'w1',
        saved_at: 1,
      }),
    ).not.toThrow();
    expect(store.lastError()).toBe('Could not save draft. Refresh may lose it.');
    expect(store.load('s1', 'q1', { revision: 1, options: ['A'], allow_free_text: false })).toMatchObject({
      selected: 'A',
    });
  });
});
