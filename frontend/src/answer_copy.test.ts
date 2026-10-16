import { describe, expect, it } from 'vitest';
import { finalAnswerMarkdown } from './answer_copy';
import type { Message } from './types/domain';

describe('final answer selection', () => {
  it('copies only canonical final parts for one turn', () => {
    const messages = [
      { type: 'asst', text: 'thinking', turnId: 't1', contentRole: 'commentary', runId: 'r1' },
      { type: 'tool', result: 'tool output', turnId: 't1', runId: 'r1' },
      {
        type: 'asst',
        text: 'first',
        turnId: 't1',
        contentRole: 'final',
        answerId: 'a1',
        answerPartIndex: 0,
        runId: 'r1',
      },
      {
        type: 'asst',
        text: 'second',
        turnId: 't1',
        contentRole: 'final',
        answerId: 'a1',
        answerPartIndex: 1,
        runId: 'r2',
      },
      {
        type: 'asst',
        text: 'new request',
        turnId: 't2',
        contentRole: 'final',
        answerId: 'a2',
        answerPartIndex: 0,
        runId: 'r3',
      },
    ] as Message[];
    expect(finalAnswerMarkdown(messages, 't1')).toBe('first\n\nsecond');
  });

  it('does not infer final status from the last assistant message', () => {
    expect(finalAnswerMarkdown([{ type: 'asst', text: 'last', runId: 'r1' } as Message], 't1')).toBeNull();
  });

  it('ignores duplicate and cross-turn parts', () => {
    const rows = [
      { type: 'asst', text: 'one', turnId: 't1', contentRole: 'final', answerId: 'a', answerPartIndex: 0 },
      { type: 'asst', text: 'dup', turnId: 't1', contentRole: 'final', answerId: 'a', answerPartIndex: 0 },
      { type: 'asst', text: 'other', turnId: 't2', contentRole: 'final', answerId: 'a', answerPartIndex: 1 },
    ] as Message[];
    expect(finalAnswerMarkdown(rows, 't1')).toBe('one');
  });

  it('does not combine different answer identities in one turn', () => {
    const rows = [
      { type: 'asst', text: 'one', turnId: 't1', contentRole: 'final', answerId: 'a1', answerPartIndex: 0 },
      { type: 'asst', text: 'other answer', turnId: 't1', contentRole: 'final', answerId: 'a2', answerPartIndex: 1 },
    ] as Message[];
    expect(finalAnswerMarkdown(rows, 't1')).toBe('one');
  });
});
