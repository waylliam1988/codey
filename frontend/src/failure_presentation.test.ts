import { describe, expect, it } from 'vitest';
import { recoveryForFailure, type FailureInfo } from './failure_presentation';

const base: FailureInfo = {
  schema_version: 1,
  code: 'execution.uncertain',
  category: 'execution',
  scope: 'tool',
  phase: 'run',
  effect: 'unknown',
  session_id: 's1',
  run_id: 'r1',
  call_id: 'c1',
  incident_id: 'i1',
  diagnostic: 'response lost',
};

describe('failure recovery policy', () => {
  it('never offers a rerun when execution effect is unknown', () => {
    expect(recoveryForFailure(base, { execution_id: 'e1' })).toEqual([{ kind: 'check_execution', executionId: 'e1' }]);
  });

  it('uses a read-only reload for a missing file', () => {
    expect(
      recoveryForFailure(
        {
          ...base,
          code: 'filesystem.not_found',
          category: 'missing_resource',
          effect: 'not_started',
          resource: { kind: 'workspace_file', path: 'a.txt' },
        },
        {},
      ),
    ).toEqual([{ kind: 'reload_resource', resourceId: 'a.txt' }]);
  });

  it('only retries continuation after a settled command', () => {
    expect(
      recoveryForFailure(
        { ...base, code: 'continuation.failed', category: 'continuation', effect: 'applied' },
        { approval_id: 'a1', revision: 4, execution: 'settled', continuation: 'failed' },
      ),
    ).toEqual([{ kind: 'retry_continuation', approvalId: 'a1', revision: 4 }]);
  });

  it('does not trust an unknown server code or server-provided actions', () => {
    expect(
      recoveryForFailure(
        { ...base, code: 'unknown.server.code', effect: 'not_started', action: 'run-script' } as FailureInfo & {
          action: string;
        },
        {},
      ),
    ).toEqual([]);
  });
});
