import { expect, it, vi } from 'vitest';
import { createApprovalController, decodeApprovalState } from './shell_approvals';
import type { ApprovalState } from './types/domain';
import type { Session } from './types/domain';

const validStatus = {
  type: 'shell_approval_status',
  id: 'approval',
  session_id: 'session',
  decision: 'allow',
  execution: 'running',
  continuation: 'waiting_for_result',
  revision: 3,
} satisfies ApprovalState;

it('rejects an unknown execution state', () => {
  expect(() =>
    decodeApprovalState({
      ...validStatus,
      execution: 'unexpected_execution',
    }),
  ).toThrow('Invalid approval status');
});

it('rejects an unknown continuation state', () => {
  expect(() =>
    decodeApprovalState({
      ...validStatus,
      continuation: 'unexpected_continuation',
    }),
  ).toThrow('Invalid approval status');
});

it('accepts a valid status without optional run identifiers', () => {
  expect(decodeApprovalState(validStatus)).toEqual(validStatus);
});

it('preserves additional response metadata', () => {
  const response = {
    ...validStatus,
    approval_id: 'approval',
    code: 'example_metadata',
  };

  expect(decodeApprovalState(response)).toBe(response);
});

it.each([false, true])('reconciles the approval response without SSE: approved=%s', async (approved) => {
  const session: Session = {
    id: 'session',
    title: 'Session',
    draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
    messages: [{ type: 'shell_request', id: 'approval-1', command: 'echo approved' }],
  };
  const state: ApprovalState = {
    type: 'shell_approval_status',
    id: 'approval-1',
    session_id: 'session',
    decision: approved ? 'allow' : 'deny',
    execution: approved ? 'settled' : 'not_required',
    continuation: 'started',
    revision: 5,
  };
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
    new Response(JSON.stringify(state), {
      status: 202,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
  const changed = vi.fn();
  const controller = createApprovalController({ sessions: () => [session], changed, fetch: fetcher });

  await controller.submit('approval-1', approved);

  expect(fetcher).toHaveBeenCalledTimes(1);
  const [url, options] = fetcher.mock.calls[0]!;
  expect(url).toBe('/api/shell_approval');
  expect(options?.method).toBe('POST');
  expect(JSON.parse(String(options?.body))).toEqual({ id: 'approval-1', approved });
  expect(session.messages).toHaveLength(1);
  expect(session.messages[0]?.type).toBe('shell_approval_status');
  expect(session.messages[0]?.approval).toEqual(state);
  expect(controller.decided('approval-1')).toBe(true);
  expect(changed).toHaveBeenCalledWith('session');
});

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

it('keeps late approval events isolated by approval id', async () => {
  const { createApprovalController } = await import('./shell_approvals');
  const session: Session = {
    id: 'session',
    title: 'Session',
    draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
    messages: [
      { type: 'shell_request', id: 'old-approval', command: 'echo same' },
      { type: 'shell_request', id: 'new-approval', command: 'echo same' },
    ],
  };
  const fetcher = vi.fn();
  const controller = createApprovalController({ sessions: () => [session], changed: vi.fn(), fetch: fetcher });

  const newState = {
    ...validStatus,
    id: 'new-approval',
    session_id: 'session',
    execution: 'running',
    revision: 3,
  } satisfies ApprovalState;
  const oldState = {
    ...validStatus,
    id: 'old-approval',
    session_id: 'session',
    decision: 'cancelled',
    execution: 'not_started',
    continuation: 'cancelled',
    revision: 4,
  } satisfies ApprovalState;

  controller.apply(newState);
  controller.apply(oldState);
  controller.apply(newState);

  const newMessages = session.messages.filter(
    (message) => message.type === 'shell_approval_status' && message.id === 'new-approval',
  );
  const oldMessage = session.messages.find(
    (message) => message.type === 'shell_approval_status' && message.id === 'old-approval',
  );
  expect(newMessages[0]?.approval?.execution).toBe('running');
  expect(oldMessage?.approval?.continuation).toBe('cancelled');
  expect(newMessages).toHaveLength(1);
  expect(fetcher).not.toHaveBeenCalled();
});
