import { describe, expect, it } from 'vitest';
import { RunProjection } from './run_projection';

function projection(requests: Record<string, string> = {}): RunProjection {
  return new RunProjection({
    requestIdForRun: (_sessionId, runId) => requests[runId],
  });
}

describe('RunProjection', () => {
  it('derives the primary run while preserving Map insertion order', () => {
    const value = projection();

    value.transition({ runId: 'r1', sessionId: 's1', state: 'running', active: true, source: 'event' });
    value.transition({ runId: 'r2', sessionId: 's2', state: 'running', active: true, source: 'event' });
    value.transition({ runId: 'r1', sessionId: 's1', state: 'running', active: true, source: 'event' });

    expect(value.primaryRun).toEqual({ runId: 'r2', sessionId: 's2' });
    expect(value.isSessionRunning('s1')).toBe(true);
  });

  it('withdraws only an admitted run and keeps turn IDs stable', () => {
    const value = projection({ r1: 'request-1' });

    value.transition({ runId: 'r1', sessionId: 's1', state: 'running', active: true, source: 'admission' });
    expect(value.turnIdForRun('s1', 'r1')).toBe('turn:request-1');
    expect(value.turnIdForRun('s1', 'r1')).toBe('turn:request-1');

    expect(value.forgetAdmission('r1')).toBe(true);
    expect(value.primaryRun).toBeNull();
    expect(value.forgetAdmission('r1')).toBe(false);
  });
});
