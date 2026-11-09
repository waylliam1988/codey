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

const SOURCE_FAILURE_SUMMARIES: Readonly<Record<string, string>> = {
  'source.source_not_selected': 'Choose a specific article or document',
  'source.document_unreadable': 'Document has no readable text; try another source',
  'source.access_restricted': 'Access restricted; try another source',
  'source.login_required': 'Sign-in required; check the page',
  'source.page_not_loaded': 'Page not loaded; check the page',
  'source.page_unavailable': 'Page unavailable; try another source',
  'source.render_script_error': 'Page script failed; try another source',
  'source.render_request_failed': 'Page resources failed to load',
  'source.request_failed': 'Could not load the page',
  'source.execution_failed': 'Could not read the page',
  'source.unsupported_content': 'Unsupported content; try another source',
  'source.policy_denied': 'Page access blocked by policy',
  'source.invalid_response': 'Invalid page response',
  'source.read_range_unavailable': 'Text range unavailable; check the source',
  'source.snapshot_unavailable': 'Captured text unavailable; open the source again',
};

export function toolFailureSummary(failure: FailureInfo | undefined): string {
  return failure?.schema_version === 1 && failure.scope === 'tool' ? SOURCE_FAILURE_SUMMARIES[failure.code] || '' : '';
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
