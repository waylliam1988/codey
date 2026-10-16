import type { Message } from './types/domain';

export type FailureEffect = 'not_started' | 'applied' | 'unknown' | 'not_applicable';

export interface FailureInfo {
  schema_version: 1;
  code: string;
  category: string;
  scope: 'tool' | 'run' | 'request' | 'render';
  phase: string;
  effect: FailureEffect;
  session_id?: string;
  run_id?: string;
  call_id?: string;
  execution_id?: string;
  source_id?: string;
  incident_id: string;
  resource?: { kind: string; path?: string; id?: string };
  diagnostic?: string;
  retry_after?: number;
}

export type RecoveryAction =
  | { kind: 'open_connection'; sourceId: string }
  | { kind: 'focus_approval'; approvalId: string }
  | { kind: 'check_execution'; executionId: string }
  | { kind: 'retry_continuation'; approvalId: string; revision: number }
  | { kind: 'reload_resource'; resourceId: string }
  | { kind: 'retry_request'; requestId: string };

export interface AuthoritativeFailureState {
  execution_id?: string;
  approval_id?: string;
  request_id?: string;
  source_id?: string;
  revision?: number;
  execution?: string;
  continuation?: string;
}

const KNOWN_CODES = new Set([
  'provider.connection_failed',
  'provider.authentication_failed',
  'provider.rate_limited',
  'filesystem.permission_denied',
  'filesystem.not_found',
  'policy.approval_required',
  'execution.uncertain',
  'continuation.failed',
  'output.unavailable',
  'render.failed',
]);

export function recoveryForFailure(
  failure: FailureInfo,
  authoritativeState: AuthoritativeFailureState,
): RecoveryAction[] {
  if (!failure || failure.schema_version !== 1 || !KNOWN_CODES.has(failure.code)) return [];
  if (failure.effect === 'unknown' || failure.code === 'execution.uncertain') {
    const executionId = authoritativeState.execution_id || failure.execution_id;
    return executionId ? [{ kind: 'check_execution', executionId }] : [];
  }
  switch (failure.code) {
    case 'provider.connection_failed':
      return authoritativeState.source_id ? [{ kind: 'open_connection', sourceId: authoritativeState.source_id }] : [];
    case 'provider.authentication_failed':
      return authoritativeState.source_id ? [{ kind: 'open_connection', sourceId: authoritativeState.source_id }] : [];
    case 'filesystem.not_found':
    case 'filesystem.permission_denied': {
      const resourceId = authoritativeState.request_id || failure.resource?.path || failure.resource?.id;
      return resourceId ? [{ kind: 'reload_resource', resourceId }] : [];
    }
    case 'policy.approval_required':
      return authoritativeState.approval_id
        ? [{ kind: 'focus_approval', approvalId: authoritativeState.approval_id }]
        : [];
    case 'continuation.failed':
      if (
        authoritativeState.approval_id &&
        Number.isSafeInteger(authoritativeState.revision) &&
        authoritativeState.execution === 'settled' &&
        authoritativeState.continuation === 'failed'
      ) {
        return [
          {
            kind: 'retry_continuation',
            approvalId: authoritativeState.approval_id,
            revision: authoritativeState.revision!,
          },
        ];
      }
      return [];
    case 'output.unavailable': {
      const resourceId = authoritativeState.execution_id || failure.execution_id;
      return resourceId ? [{ kind: 'reload_resource', resourceId }] : [];
    }
    case 'render.failed': {
      const resourceId = authoritativeState.request_id || failure.resource?.id;
      return resourceId ? [{ kind: 'reload_resource', resourceId }] : [];
    }
    case 'provider.rate_limited':
      return failure.retry_after && authoritativeState.request_id
        ? [{ kind: 'retry_request', requestId: authoritativeState.request_id }]
        : [];
    default:
      return [];
  }
}

export function failureFromMessage(message: Message): FailureInfo | null {
  const value = message.failure;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Partial<FailureInfo>;
  if (row.schema_version !== 1 || typeof row.code !== 'string' || typeof row.effect !== 'string') return null;
  return row as FailureInfo;
}
