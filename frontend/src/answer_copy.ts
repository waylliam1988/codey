import type { Message } from './types/domain';

export function finalAnswerMarkdown(messages: Message[], turnId: string): string | null {
  const parts = new Map<string, { index: number; text: string }>();
  let answerId = '';
  for (const message of messages) {
    if (message.type !== 'asst' || message.turnId !== turnId || message.contentRole !== 'final' || !message.answerId)
      continue;
    if (!Number.isSafeInteger(message.answerPartIndex) || typeof message.text !== 'string') continue;
    if (!answerId) answerId = message.answerId;
    if (message.answerId !== answerId) continue;
    const key = `${message.answerId}:${message.answerPartIndex}`;
    if (parts.has(key)) continue;
    parts.set(key, { index: message.answerPartIndex!, text: message.text });
  }
  const ordered = [...parts.values()].sort((a, b) => a.index - b.index);
  return ordered.length ? ordered.map((part) => part.text).join('\n\n') : null;
}
