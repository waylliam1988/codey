import { describe, expect, it } from 'vitest';
import { terminalAnswerMessage } from './terminal_answer';

describe('terminal answer identity', () => {
  it('marks an explicitly successful terminal delivery as final', () => {
    expect(
      terminalAnswerMessage(
        {
          summary: 'answer',
          stop_reason: 'done',
          final_delivery: 'success',
          mode: 'chat',
        },
        'run-1',
        'turn-1',
      ),
    ).toMatchObject({
      text: 'answer',
      runId: 'run-1',
      turnId: 'turn-1',
      contentRole: 'final',
      finalAnswer: true,
      answerId: 'terminal:run-1:answer',
      answerPartIndex: 0,
    });
  });

  it('does not infer final identity from a terminal-looking summary', () => {
    expect(
      terminalAnswerMessage({ summary: 'answer', stop_reason: 'done', mode: 'chat' }, 'run-1', 'turn-1'),
    ).toBeNull();
    expect(
      terminalAnswerMessage(
        { summary: 'answer', stop_reason: 'done', final_delivery: 'unknown', mode: 'chat' },
        'run-1',
        'turn-1',
      ),
    ).toBeNull();
  });
});
