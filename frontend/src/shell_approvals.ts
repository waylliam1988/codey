import type { ApprovalState, Session } from './types/domain';

interface ApprovalDeps {
  sessions(): Session[];
  changed(sessionId: string): void;
  focus?(id: string): void;
  fetch: typeof fetch;
}

export function decodeApprovalState(value: unknown): ApprovalState {
  const row = value as ApprovalState;
  if (
    !row ||
    row.type !== 'shell_approval_status' ||
    typeof row.id !== 'string' ||
    typeof row.session_id !== 'string' ||
    !Number.isInteger(row.revision) ||
    !['pending', 'allow', 'deny', 'cancelled'].includes(row.decision) ||
    typeof row.execution !== 'string' ||
    typeof row.continuation !== 'string'
  ) {
    throw new Error('Invalid approval status');
  }
  return row;
}

export function approvalText(state: ApprovalState): string {
  if (state.decision === 'cancelled') return 'Approval cancelled';
  if (state.execution === 'uncertain') return 'Command outcome is uncertain; automatic execution is blocked';
  if (state.continuation === 'unavailable') return 'Saved command result is unavailable; task cannot resume';
  if (state.continuation === 'failed') return 'Command result saved; task continuation failed';
  if (state.continuation === 'started') return 'Command result saved; task continued';
  if (state.continuation === 'cancelled') return 'Task continuation cancelled';
  if (state.continuation === 'resuming') return 'Command result saved; resuming task…';
  if (state.execution === 'running') return 'Allowed; command running…';
  if (state.execution === 'queued') return 'Allowed; command queued…';
  if (state.continuation === 'queued') return 'Command result saved; waiting to continue…';
  if (state.decision === 'deny') return 'Denied; command will not run';
  if (state.execution === 'settled') return 'Command completed';
  return 'Waiting for approval';
}

export function createApprovalController(deps: ApprovalDeps) {
  const inFlight = new Set<string>();
  const find = (id: string) =>
    deps
      .sessions()
      .find((s) =>
        s.messages.some((m) => (m.type === 'shell_request' || m.type === 'shell_approval_status') && m.id === id),
      );
  const stateFor = (id: string) =>
    find(id)?.messages.find((m) => m.type === 'shell_approval_status' && m.id === id)?.approval;

  function apply(value: unknown) {
    const state = decodeApprovalState(value);
    const session = deps.sessions().find((s) => s.id === state.session_id);
    if (!session) return;
    const old = stateFor(state.id);
    if (old && old.revision > state.revision) return;
    if (old?.revision === state.revision && old.execution === 'uncertain' && state.execution !== 'uncertain') return;
    const request = session.messages.find((m) => m.type === 'shell_request' && m.id === state.id);
    if (state.decision === 'pending') {
      if (request) delete request.approvalSubmission;
      deps.changed(session.id);
      return;
    }
    session.messages = session.messages.filter((m) => m.type !== 'shell_request' || m.id !== state.id);
    const message = {
      type: 'shell_approval_status',
      id: state.id,
      sessionId: session.id,
      runId: state.run_id,
      approval: state,
    };
    const index = session.messages.findIndex((m) => m.type === message.type && m.id === state.id);
    if (index >= 0) session.messages[index] = message;
    else session.messages.push(message);
    deps.changed(session.id);
  }

  function submission(id: string, value: 'sending' | 'checking' | 'unconfirmed') {
    const session = find(id);
    const request = session?.messages.find((m) => m.type === 'shell_request' && m.id === id);
    if (request && session) {
      request.approvalSubmission = value;
      deps.changed(session.id);
    }
  }

  async function refresh(id: string) {
    submission(id, 'checking');
    try {
      const response = await deps.fetch(`/api/shell_approval?id=${encodeURIComponent(id)}`, { cache: 'no-store' });
      if (!response.ok) throw new Error('Approval state unavailable');
      apply(await response.json());
    } catch {
      submission(id, 'unconfirmed');
    }
  }

  async function submit(id: string, approved: boolean) {
    if (!id || inFlight.has(id) || stateFor(id)) return;
    inFlight.add(id);
    submission(id, 'sending');
    try {
      const response = await deps.fetch('/api/shell_approval', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, approved }),
      });
      const data = await response.json();
      if (data.type === 'shell_approval_status') apply(data);
      else await refresh(id);
    } catch {
      await refresh(id);
    } finally {
      inFlight.delete(id);
    }
  }

  async function retry(id: string) {
    if (inFlight.has(id)) return;
    inFlight.add(id);
    try {
      const response = await deps.fetch('/api/shell_approval/retry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      });
      const data = await response.json();
      if (data.type === 'shell_approval_status') apply(data);
      else await refresh(id);
    } catch {
      await refresh(id);
    } finally {
      inFlight.delete(id);
    }
  }

  async function continueSession(sessionId: string) {
    // Route from authoritative service state, independent of the user's wording.
    const response = await deps.fetch('/api/state', { cache: 'no-store' });
    if (!response.ok) throw new Error('Task state unavailable');
    const data = await response.json();
    const states = (data.shell_approvals || []).map(decodeApprovalState) as ApprovalState[];
    states.forEach(apply);
    const state = states.filter((s) => s.session_id === sessionId).at(-1);
    if (
      !state ||
      state.continuation === 'started' ||
      state.continuation === 'not_required' ||
      state.decision === 'cancelled'
    )
      return false;
    if (state.continuation === 'failed' || state.continuation === 'queued') await retry(state.id);
    else deps.focus?.(state.id);
    return true;
  }

  return { apply, submit, retry, refresh, continueSession, decided: (id: string) => !!stateFor(id) };
}
