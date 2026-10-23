import {
  APPROVAL_CONTINUATIONS,
  APPROVAL_DECISIONS,
  APPROVAL_EXECUTIONS,
  type ApprovalState,
  type Session,
} from './types/domain';

interface ApprovalDeps {
  sessions(): Session[];
  changed(sessionId: string): void;
  focus?(id: string): void;
  fetch: typeof fetch;
}

function isOneOf<T extends string>(value: unknown, choices: readonly T[]): value is T {
  return typeof value === 'string' && choices.some((choice) => choice === value);
}

function isApprovalState(value: unknown): value is ApprovalState {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;

  const row = value as Record<string, unknown>;
  return (
    row.type === 'shell_approval_status' &&
    typeof row.id === 'string' &&
    typeof row.session_id === 'string' &&
    typeof row.revision === 'number' &&
    Number.isInteger(row.revision) &&
    isOneOf(row.decision, APPROVAL_DECISIONS) &&
    isOneOf(row.execution, APPROVAL_EXECUTIONS) &&
    isOneOf(row.continuation, APPROVAL_CONTINUATIONS) &&
    ['run_id', 'source_run_id', 'continuation_run_id', 'execution_id'].every(
      (key) => row[key] === undefined || typeof row[key] === 'string',
    )
  );
}

export function decodeApprovalState(value: unknown): ApprovalState {
  if (!isApprovalState(value)) {
    throw new Error('Invalid approval status');
  }
  return value;
}

export function approvalText(state: ApprovalState): string {
  if (state.decision === 'cancelled') return 'Approval cancelled';
  if (state.execution === 'uncertain') return 'Command outcome is uncertain; automatic execution is blocked';
  if (state.continuation === 'unavailable') return 'Saved command result is unavailable; task cannot resume';
  if (state.continuation === 'failed') return 'Task continuation failed';
  if (state.continuation === 'started') return 'Task continued';
  if (state.continuation === 'cancelled') return 'Task continuation cancelled';
  if (state.continuation === 'resuming') return 'Resuming task…';
  if (state.execution === 'running') return 'Allowed; command running…';
  if (state.execution === 'queued') return 'Allowed; command queued…';
  if (state.continuation === 'queued') return 'Waiting to continue…';
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
        s.messages.some(
          (m) => ['shell_request', 'shell_result', 'shell_approval_status'].includes(m.type) && m.id === id,
        ),
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
      if (request && request.approvalSubmission !== 'sending') delete request.approvalSubmission;
      deps.changed(session.id);
      return;
    }
    if (request)
      Object.assign(request, {
        type: 'shell_result',
        approved: state.decision === 'allow',
        shellStatus: state.decision === 'deny' ? 'denied' : state.execution,
        executionId: state.execution_id,
      });
    const message = {
      type: 'shell_approval_status',
      id: state.id,
      sessionId: session.id,
      runId: state.run_id,
      approval: state,
    };
    const index = session.messages.findIndex((m) => m.type === message.type && m.id === state.id);
    if (index >= 0) session.messages[index] = message;
    else {
      const answer = session.messages.findIndex(
        (m) =>
          m.type === 'asst' && m.runId === state.run_id && m.contentRole !== 'commentary' && m.finalAnswer !== false,
      );
      if (answer >= 0) session.messages.splice(answer, 0, message);
      else session.messages.push(message);
    }
    if (request && state.execution === 'running') {
      const running = {
        type: 'shell_result',
        id: state.id,
        sessionId: session.id,
        runId: state.run_id,
        command: request.command,
        cwd: request.cwd,
        project: request.project,
        approved: true,
        shellStatus: 'running',
        executionId: state.execution_id,
        eventKey: `shell-running:${state.id}:${state.revision}`,
      };
      const existing = session.messages.findIndex((item) => item.type === 'shell_result' && item.id === state.id);
      if (existing >= 0) session.messages[existing] = running;
      else session.messages.push(running);
    }
    deps.changed(session.id);
  }

  function submission(id: string, value: 'sending' | 'checking' | 'unconfirmed') {
    const session = find(id);
    const request = session?.messages.find((m) => m.type === 'shell_request' && m.id === id);
    if (request && session) {
      if (value === 'sending') delete request.approvalError;
      request.approvalSubmission = value;
      deps.changed(session.id);
    }
  }

  async function refresh(id: string) {
    submission(id, 'checking');
    try {
      const response = await deps.fetch(`/api/shell_approval?id=${encodeURIComponent(id)}`, {
        cache: 'no-store',
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error('Approval state unavailable');
      apply(await response.json());
    } catch {
      submission(id, 'unconfirmed');
    }
  }

  async function submit(id: string, approved: boolean, scope = 'once') {
    if (!id || inFlight.has(id)) return;
    const known = stateFor(id);
    if (known) {
      apply(known);
      return;
    }
    inFlight.add(id);
    submission(id, 'sending');
    try {
      const response = await deps.fetch('/api/shell_approval', {
        signal: AbortSignal.timeout(15000),
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, approved, ...(scope !== 'once' ? { scope } : {}) }),
      });
      const data = await response.json();
      if (response.status === 400 && data.error) {
        const session = find(id);
        const request = session?.messages.find((m) => m.type === 'shell_request' && m.id === id);
        if (request && session) {
          delete request.approvalSubmission;
          request.approvalError = String(data.error);
          deps.changed(session.id);
        }
        return;
      }
      if (data.type === 'shell_approval_status') apply(data);
      else await refresh(id);
      permissionResult(id, data);
    } catch {
      await refresh(id);
    } finally {
      inFlight.delete(id);
    }
  }

  function permissionResult(id: string, data: Record<string, unknown>) {
    const session = find(id);
    const command = session?.messages.find((m) => ['shell_request', 'shell_result'].includes(m.type) && m.id === id);
    if (!command || !session) return;
    if (data.remember_error) {
      command.permissionSaveError = String(data.remember_error);
      command.permissionSaveScope = data.remember_scope === 'project' ? 'project' : 'session';
    } else if (data.permission_scope === 'session' || data.permission_scope === 'project') {
      command.permissionScope = data.permission_scope;
      delete command.permissionSaveError;
      delete command.permissionSaveScope;
    }
    deps.changed(session.id);
  }

  async function remember(id: string, scope: 'session' | 'project') {
    if (inFlight.has(id)) return;
    inFlight.add(id);
    try {
      const response = await deps.fetch('/api/shell_approval/remember', {
        signal: AbortSignal.timeout(15000),
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, scope }),
      });
      const data = await response.json();
      permissionResult(
        id,
        response.ok ? data : { remember_error: data.error || 'Permission could not be saved.', remember_scope: scope },
      );
    } catch {
      permissionResult(id, {
        remember_error: 'Saving could not be confirmed. Check saved permissions before retrying.',
        remember_scope: scope,
      });
    } finally {
      inFlight.delete(id);
    }
  }

  async function retry(id: string) {
    if (inFlight.has(id)) return;
    inFlight.add(id);
    try {
      const response = await deps.fetch('/api/shell_approval/retry', {
        signal: AbortSignal.timeout(15000),
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

  return { apply, submit, retry, refresh, remember, continueSession, decided: (id: string) => !!stateFor(id) };
}
