import type { Message } from './types/domain';

export interface TerminalAnswerEvent {
  summary?: string;
  stop_reason?: string;
  final_delivery?: string;
  mode?: string;
}

/** Build final-answer identity only from an explicit successful delivery. */
export function terminalAnswerMessage(event: TerminalAnswerEvent, runId: string, turnId: string): Message | null {
  const text = typeof event.summary === 'string' ? event.summary : '';
  if (!text || event.stop_reason !== 'done' || event.final_delivery !== 'success' || !runId || !turnId) return null;
  const answerId = `terminal:${runId}:answer`;
  return {
    type: 'asst',
    text,
    runId,
    eventKey: answerId,
    contentRole: 'final',
    finalAnswer: true,
    answerId,
    answerPartIndex: 0,
    turnId,
  };
}
