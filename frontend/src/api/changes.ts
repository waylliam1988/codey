import { decodeChangeFilePreview, decodeChangesData, isRecord } from '../domain/protocol';
import type { RestoreChangesResult } from '../types/domain';
import { requestResponse } from './transport';

export async function fetchChanges(project: string) {
  const r = await requestResponse('/api/changes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project }),
  });
  const data = decodeChangesData(await r.json());
  if (!r.ok) throw new Error(data.error || 'Could not load changes');
  return data;
}

export async function fetchChangeFile(project: string, path: string) {
  const r = await requestResponse('/api/changes/file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project, path }),
  });
  const data = decodeChangeFilePreview(await r.json());
  if (!r.ok || !data.ok) throw new Error(data.error || 'Could not load file diff');
  return data;
}

export async function fetchRunChanges(sessionId: string, runId: string) {
  const query = new URLSearchParams({ session_id: sessionId, run_id: runId });
  const response = await requestResponse(`/api/run_changes?${query}`);
  if (!response.ok) throw new Error('Run changes unavailable');
  return decodeChangesData(await response.json());
}

export async function undoRunChanges(sessionId: string, runId: string, preview: boolean) {
  const response = await requestResponse('/api/run_changes/undo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, run_id: runId, preview }),
  });
  const data: unknown = await response.json();
  if (!isRecord(data)) throw new Error('Could not confirm undo');
  const strings = (value: unknown) =>
    Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
  return {
    ok: response.ok && data.ok === true,
    error: typeof data.error === 'string' ? data.error : '',
    files: strings(data.files),
    restored: strings(data.restored),
    conflicts: strings(data.conflicts),
  };
}

export async function restoreChanges(project: string) {
  const r = await requestResponse('/api/changes/restore', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project }),
  });
  const data: unknown = await r.json().catch(() => null);
  if (!isRecord(data)) return { ok: false };
  const result: RestoreChangesResult = {
    ok: r.ok && data.ok === true,
    error: typeof data.error === 'string' ? data.error : undefined,
    message: typeof data.message === 'string' ? data.message : undefined,
    conflicts: Array.isArray(data.conflicts)
      ? data.conflicts.filter((item: unknown): item is string => typeof item === 'string')
      : undefined,
  };
  return result;
}
