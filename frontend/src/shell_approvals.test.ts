import { expect, it, vi } from 'vitest';
import type { Session } from './types/domain';

it('confirms a lost response before reopening approval and rejects stale state', async () => {
  const { createApprovalController } = await import('./shell_approvals');
  const session = { id: 's', messages: [{ type: 'shell_request', id: 'a' }] } as Session;
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new Error('response lost'))
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        type: 'shell_approval_status',
        id: 'a',
        session_id: 's',
        decision: 'allow',
        execution: 'running',
        continuation: 'waiting_for_result',
        revision: 3,
      }),
    });
  const controller = createApprovalController({ sessions: () => [session], changed: vi.fn(), fetch: fetcher });
  await controller.submit('a', true);
  expect(fetcher.mock.calls[1][0]).toContain('/api/shell_approval?id=a');
  expect(session.messages.some((m) => m.type === 'shell_request')).toBe(false);
  expect(controller.decided('a')).toBe(true);
  controller.apply({
    type: 'shell_approval_status',
    id: 'a',
    session_id: 's',
    decision: 'pending',
    execution: 'not_started',
    continuation: 'waiting_for_result',
    revision: 1,
  });
  expect(controller.decided('a')).toBe(true);
});

it('keeps decisions disabled when neither submission nor status query returns', async () => {
  const { createApprovalController } = await import('./shell_approvals');
  const session = { id: 's', messages: [{ type: 'shell_request', id: 'a' }] } as Session;
  const controller = createApprovalController({
    sessions: () => [session],
    changed: vi.fn(),
    fetch: vi.fn().mockRejectedValue(new Error('offline')),
  });
  await controller.submit('a', true);
  expect(session.messages[0].approvalSubmission).toBe('unconfirmed');
});

it('retry continues through a separate endpoint and never resubmits Allow', async () => {
  const { createApprovalController } = await import('./shell_approvals');
  const session = { id: 's', messages: [] } as unknown as Session;
  const state = {
    type: 'shell_approval_status',
    id: 'a',
    session_id: 's',
    decision: 'allow',
    execution: 'settled',
    continuation: 'failed',
    revision: 5,
  };
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => state });
  const controller = createApprovalController({ sessions: () => [session], changed: vi.fn(), fetch: fetcher });
  controller.apply(state);
  await controller.retry('a');
  expect(fetcher.mock.calls[0][0]).toBe('/api/shell_approval/retry');
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ id: 'a' });
});

it('does not let a buffered live event overwrite an uncertain restart snapshot at the same revision', async () => {
  const { createApprovalController } = await import('./shell_approvals');
  const session = { id: 's', messages: [] } as unknown as Session;
  const controller = createApprovalController({ sessions: () => [session], changed: vi.fn(), fetch: vi.fn() });
  const state = {
    type: 'shell_approval_status',
    id: 'a',
    session_id: 's',
    decision: 'allow',
    execution: 'uncertain',
    continuation: 'waiting_for_result',
    revision: 3,
  };
  controller.apply(state);
  controller.apply({ ...state, execution: 'running' });
  expect(session.messages[0].approval?.execution).toBe('uncertain');
});
