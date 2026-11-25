import { describe, expect, it } from 'vitest';
import { BoundedOutput, mergeOutputSnapshot } from './execution_output';

describe('bounded execution output', () => {
  it('preserves UTF-8 boundaries and head/tail budget', () => {
    const output = new BoundedOutput(8);
    output.append(new TextEncoder().encode('前面\n'));
    output.append(new TextEncoder().encode('中间\n后面'));
    const snapshot = output.snapshot();
    expect(snapshot.total_bytes).toBeGreaterThan(snapshot.stdout.length);
    expect(snapshot.stdout).not.toContain('�');
    expect(snapshot.omitted_bytes).toBeGreaterThanOrEqual(0);
  });

  it('rejects stale snapshots and keeps settled state', () => {
    const current = { revision: 4, execution_state: 'settled' as const, stdout: 'done' };
    expect(mergeOutputSnapshot(current, { revision: 5, execution_state: 'running', stdout: 'late' }).stdout).toBe(
      'done',
    );
    expect(mergeOutputSnapshot(current, { revision: 3, execution_state: 'running', stdout: 'old' })).toBe(current);
  });
});
