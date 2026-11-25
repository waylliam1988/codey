import type { FailureInfo } from '../failure_presentation';
import type { ToolPreview } from '../render';

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function isToolPreview(value: unknown): value is ToolPreview {
  if (!isRecord(value)) return false;
  for (const key of ['additions', 'deletions', 'line_start', 'line_end', 'matches']) {
    const field = value[key];
    if (field !== undefined && (typeof field !== 'number' || !Number.isSafeInteger(field) || field < 0)) return false;
  }
  for (const key of ['diff_truncated', 'diff_unavailable', 'has_more']) {
    if (value[key] !== undefined && typeof value[key] !== 'boolean') return false;
  }
  return (
    (value.diff === undefined || typeof value.diff === 'string') &&
    (value.query === undefined || typeof value.query === 'string')
  );
}

export function isFailureInfo(value: unknown): value is FailureInfo {
  if (!isRecord(value)) return false;
  const { scope, effect, resource } = value;
  return (
    value.schema_version === 1 &&
    (scope === 'tool' || scope === 'run' || scope === 'request' || scope === 'render') &&
    (effect === 'not_started' || effect === 'applied' || effect === 'unknown' || effect === 'not_applicable') &&
    ['code', 'category', 'phase', 'incident_id'].every((key) => typeof value[key] === 'string') &&
    ['session_id', 'run_id', 'call_id', 'execution_id', 'source_id', 'diagnostic'].every(
      (key) => value[key] === undefined || typeof value[key] === 'string',
    ) &&
    (value.retry_after === undefined || typeof value.retry_after === 'number') &&
    (resource === undefined ||
      (isRecord(resource) &&
        typeof resource.kind === 'string' &&
        (resource.path === undefined || typeof resource.path === 'string') &&
        (resource.id === undefined || typeof resource.id === 'string')))
  );
}
