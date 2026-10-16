import type { Message, Session } from './types/domain';
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
  if (session.terminalRuns?.includes(event.run_id)) return null;
  return {
    ...old,
    type: 'asst',
    runId: event.run_id,
    streamId: event.stream_id,
    streamSequence: event.sequence,
    streamState: event.state as Message['streamState'],
    text: event.text,
    finalAnswer: false,
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
  return old ? { ...old, text, eventKey, finalAnswer: true, streamState: 'complete' } : null;
}
