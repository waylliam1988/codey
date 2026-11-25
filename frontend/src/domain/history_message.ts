import { validRef } from '../attachments';
import type { Message } from '../types/domain';
import { isFailureInfo, isRecord, isToolPreview } from './display_values';

const string = (value: unknown): string | undefined => (typeof value === 'string' ? value : undefined);
const number = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined;
const boolean = (value: unknown): boolean | undefined => (typeof value === 'boolean' ? value : undefined);
const counter = (value: unknown): string | number | undefined => string(value) ?? number(value);

export function decodeHistoryMessage(row: Record<string, unknown>): Message | null {
  const id = string(row.id);
  if (!id) throw new Error('Missing durable entry identity');
  const identity = {
    id,
    entryId: id,
    runId: string(row.runId),
    sessionId: string(row.sessionId),
    turnId: string(row.turnId),
    text: string(row.text),
  };
  const { type } = row;
  switch (type) {
    case 'user': {
      const { source, attachments } = row;
      return {
        ...identity,
        type,
        queuedInputId: string(row.queuedInputId),
        ...(Array.isArray(attachments) && attachments.every(validRef) ? { attachments } : {}),
        ...(isRecord(source) &&
        typeof source.actor_id === 'string' &&
        typeof source.name === 'string' &&
        typeof source.kind === 'string'
          ? { source: { actor_id: source.actor_id, name: source.name, kind: source.kind } }
          : {}),
      };
    }
    case 'asst':
      return {
        ...identity,
        type,
        answerId: string(row.answerId),
        answerPartIndex: number(row.answerPartIndex),
        finalAnswer: boolean(row.finalAnswer),
        contentRole: row.contentRole === 'final' || row.contentRole === 'commentary' ? row.contentRole : undefined,
      };
    case 'tool':
    case 'tool_pending': {
      const fields = {
        ...identity,
        kind: string(row.kind),
        toolName: string(row.toolName),
        toolKey: string(row.toolKey),
        callId: string(row.callId),
        parentCallId: string(row.parentCallId),
        turn: counter(row.turn),
        path: string(row.path),
        command: string(row.command),
        activity: string(row.activity),
        result: string(row.result),
        status: string(row.status),
        pending: boolean(row.pending),
        error: boolean(row.error),
        changed: boolean(row.changed),
        outputTruncated: boolean(row.outputTruncated),
        outputBytes: number(row.outputBytes),
        outputStoredBytes: number(row.outputStoredBytes),
        elapsedMs: number(row.elapsedMs),
        phase: string(row.phase),
        preview: isToolPreview(row.preview) ? row.preview : undefined,
        failure: isFailureInfo(row.failure) ? row.failure : undefined,
      };
      return type === 'tool' ? { ...fields, type } : { ...fields, type };
    }
    case 'turn':
    case 'thinking': {
      const fields = {
        ...identity,
        n: counter(row.n),
        turn: counter(row.turn),
        note: string(row.note),
        reasoning: string(row.reasoning),
      };
      return type === 'turn' ? { ...fields, type } : { ...fields, type };
    }
    case 'shell_request':
    case 'shell_result': {
      const fields = {
        ...identity,
        callId: string(row.callId),
        executionId: string(row.executionId),
        command: string(row.command),
        cwd: string(row.cwd),
        project: string(row.project),
        output: string(row.output),
        shellStatus: string(row.shellStatus),
        approved: boolean(row.approved),
        exitCode: row.exitCode === null ? null : number(row.exitCode),
        outputTruncated: boolean(row.outputTruncated),
        failure: isFailureInfo(row.failure) ? row.failure : undefined,
      };
      return type === 'shell_request' ? { ...fields, type } : { ...fields, type };
    }
    case 'info':
    case 'review':
    case 'done':
    case 'research_done':
    case 'err':
    case 'pause':
    case 'limit':
      return { ...identity, type, payload: row.payload, failure: isFailureInfo(row.failure) ? row.failure : undefined };
    default:
      return null;
  }
}
