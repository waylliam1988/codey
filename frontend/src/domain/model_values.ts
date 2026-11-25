import type { ModelChoice, ModelSource, ModelState, ProviderModel } from '../types/domain';
import { isRecord } from './display_values';

function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item: unknown) => typeof item === 'string');
}

function optionalScalars(row: Record<string, unknown>, kind: 'string' | 'boolean' | 'number', keys: string[]): boolean {
  return keys.every((key) => row[key] === undefined || typeof row[key] === kind);
}

function isProviderModel(value: unknown): value is ProviderModel {
  return (
    isRecord(value) &&
    typeof value.id === 'string' &&
    optionalScalars(value, 'string', ['name', 'protocol', 'unavailable_reason']) &&
    optionalScalars(value, 'boolean', ['thinking_toggle', 'thinking_default_allowed', 'trial']) &&
    (value.efforts === undefined || strings(value.efforts)) &&
    (value.availability === undefined ||
      value.availability === 'available' ||
      value.availability === 'unavailable' ||
      value.availability === 'capability_unknown')
  );
}

export function isModelSource(value: unknown): value is ModelSource {
  return (
    isRecord(value) &&
    typeof value.id === 'string' &&
    Array.isArray(value.models) &&
    value.models.every(isProviderModel) &&
    optionalScalars(value, 'string', ['label', 'state', 'error', 'base_url', 'default_model']) &&
    optionalScalars(value, 'boolean', ['discoverable', 'connection_editor', 'stale']) &&
    optionalScalars(value, 'number', ['updated_at', 'checked_at']) &&
    (value.unavailable_models === undefined ||
      (Array.isArray(value.unavailable_models) &&
        value.unavailable_models.every(
          (item: unknown) =>
            isRecord(item) && typeof item.id === 'string' && optionalScalars(item, 'string', ['name', 'status']),
        )))
  );
}

function isModelChoice(value: unknown): value is ModelChoice {
  return isRecord(value) && typeof value.enabled === 'boolean' && strings(value.models);
}

export function isModelState(value: unknown): value is ModelState {
  return (
    isRecord(value) &&
    isRecord(value.preferences) &&
    typeof value.preferences.revision === 'number' &&
    Number.isSafeInteger(value.preferences.revision) &&
    isRecord(value.preferences.sources) &&
    Object.values(value.preferences.sources).every(isModelChoice) &&
    Array.isArray(value.sources) &&
    value.sources.every(isModelSource) &&
    Array.isArray(value.in_use) &&
    value.in_use.every(
      (item: unknown) =>
        isRecord(item) &&
        typeof item.provider === 'string' &&
        (item.model === undefined || typeof item.model === 'string'),
    )
  );
}
