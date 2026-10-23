import { expect, it } from 'vitest';
import { mergeCommandResult } from './command_result';
import type { Message } from './types/domain';

const final: Message = {
  type: 'shell_result',
  id: 'approval',
  executionId: 'execution',
  shellStatus: 'exit',
  exitCode: 0,
  output: 'result',
};
it.each(['queued', 'running', 'settled', 'uncertain', 'timeout'])(
  'does not replace a settled execution with %s',
  (shellStatus) => {
    expect(mergeCommandResult(final, { ...final, shellStatus, output: '' })).toBe(final);
  },
);
it('rejects a conflicting execution or exit code for the same approval', () => {
  expect(mergeCommandResult(final, { ...final, executionId: 'other' })).toBe(final);
  expect(mergeCommandResult(final, { ...final, exitCode: 1 })).toBe(final);
});
it('keeps distinct approvals independent even if commands match', () => {
  const next = { ...final, id: 'other', shellStatus: 'running' };
  expect(mergeCommandResult(final, next).id).toBe('other');
});
