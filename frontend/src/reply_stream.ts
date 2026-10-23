import type { Message, Session } from './types/domain';
import { isTerminalRun } from './run_lifecycle';
export interface ReplySnapshot {
  stream_id: string;
  run_id: string;
  sequence: number;
  state: string;
  text: string;
}

export function streamMessage(session: Session, event: ReplySnapshot): Message | null {
  if (
    !event.stream_id ||
    !event.run_id ||
    !Number.isSafeInteger(event.sequence) ||
    event.sequence < 1 ||
    !['partial', 'complete', 'interrupted'].includes(event.state) ||
    typeof event.text !== 'string'
  )
    return null;
  const old = session.messages.find(
    (message) => message.runId === event.run_id && message.streamId === event.stream_id,
  );
  if (old && ((old.streamSequence || 0) >= event.sequence || old.finalAnswer || old.streamState !== 'partial'))
    return null;
  if (isTerminalRun(session, event.run_id)) return null;
  const request = session.messages.find(
    (message) =>
      message.type === 'user' && message.id && message.attempts?.some((attempt) => attempt.runId === event.run_id),
  );
  const turnId = request?.id ? `turn:${request.id}` : undefined;
  return {
    ...old,
    type: 'asst',
    runId: event.run_id,
    streamId: event.stream_id,
    streamSequence: event.sequence,
    streamState: event.state as Message['streamState'],
    text: event.text,
    finalAnswer: false,
    ...(turnId ? { turnId } : {}),
    eventKey: `reply-stream:${event.run_id}:${event.stream_id}:${event.sequence}`,
  };
}

export function finalStreamMessage(session: Session, runId: string, text: string, eventKey: string): Message | null {
  const old = [...session.messages]
    .reverse()
    .find(
      (message) =>
        message.type === 'asst' &&
        message.runId === runId &&
        message.streamId &&
        (message.text?.trim() === text.trim() || (message.text?.length === 64000 && text.startsWith(message.text))),
    );
  if (!old) return null;
  const request = session.messages.find(
    (message) => message.type === 'user' && message.id && message.attempts?.some((attempt) => attempt.runId === runId),
  );
  const turnId = old.turnId || (request?.id ? `turn:${request.id}` : '');
  return {
    ...old,
    text,
    eventKey,
    finalAnswer: true,
    streamState: 'complete',
    ...(turnId ? { contentRole: 'final', turnId, answerId: eventKey, answerPartIndex: 0 } : {}),
  };
}
