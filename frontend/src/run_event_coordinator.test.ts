import { afterEach, describe, expect, it, vi } from 'vitest';
import { RunEventCoordinator, type RunEventDeps } from './run_event_coordinator';
import { SessionStateStore } from './session_state_store';
import type { RequestMatch } from './requests';
import type { Session } from './types/domain';
import type { UiStateApi } from './ui_state';
import { runtime } from './runtime';
import './ui_state';

function setup() {
  const ui = runtime.UiState as UiStateApi;
  const requests = {
    forRun: vi.fn((_sessionId: string, _runId: string): RequestMatch | null => null),
    acceptsEvent: vi.fn(() => true),
    projectLifecycle: vi.fn(() => true),
    reconcile: vi.fn(),
    warning: vi.fn(() => false),
    fail: vi.fn(() => true),
  };
  const session: Session = {
    id: 'session',
    title: 'Chat',
    messages: [],
    draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
  };
  const state = new SessionStateStore({
    ui: { ...ui, persist: vi.fn(), persistNow: vi.fn() },
    defaultProvider: () => '',
    isRenaming: () => false,
    invalidate: vi.fn(),
    forgetView: vi.fn(),
    forgetOnServer: async () => true,
    requestIdForRun: (sid, runId) => requests.forRun(sid, runId)?.request.id,
    mountedStreamId: () => undefined,
    messageAdded: vi.fn(),
  });
  state.sessions = [session];
  state.activeId = session.id;
  const approvals = { apply: vi.fn(), decided: vi.fn(() => false) };
  const deps = {
    state,
    requests,
    ui: {
      persist: vi.fn(),
      flush: vi.fn(),
      refreshInventory: vi.fn(async () => {}),
      ensureSessionIndexes: ui.ensureSessionIndexes,
    },
    history: {
      acceptsRun: vi.fn(() => true),
      observeCommit: vi.fn(async () => {}),
      refreshHistory: vi.fn(async () => {}),
      syncAnswerIdentity: vi.fn(async () => {}),
    },
    composer: {
      observeRun: vi.fn(),
      refreshQueuedInputs: vi.fn(async () => {}),
      applyQueuedInput: vi.fn(),
      queuedInputForRun: vi.fn(() => undefined),
    },
    approvals: () => approvals,
    questions: { apply: vi.fn(() => true) },
    transport: {
      eventKey: (_event: unknown, fallback = '') => fallback,
      displayRunStatus: vi.fn(),
      reconcileRunState: vi.fn(async () => null),
      acceptRunResponse: vi.fn(async () => {}),
      connect: vi.fn(),
    },
    revealApproval: vi.fn(),
    providers: [],
    setStatus: vi.fn(),
    updateSend: vi.fn(),
    updateComposerContext: vi.fn(),
    syncResearchUseProjectButton: vi.fn(),
    refreshProviderStatus: vi.fn(),
    applyProviderStatus: vi.fn(),
    recordResearchRun: vi.fn(() => null),
    markResearchRestoreAvailability: vi.fn(),
    receiptSummary: () => '',
    receiptChangedCount: () => 0,
    maybeAddChangesSummary: vi.fn(async () => {}),
  } satisfies RunEventDeps;
  return { coordinator: new RunEventCoordinator(deps), deps, approvals, state, session };
}

const start = {
  type: 'task_start',
  session_id: 'session',
  run_id: 'run',
  task_status: 'running',
  task_revision: 4,
  event_id: 10,
};

describe('RunEventCoordinator ordering', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('does not write request or terminal projections for an older snapshot', () => {
    const { coordinator, deps, session } = setup();
    coordinator.handleServerEvent(start);
    const count = session.messages.length;
    deps.requests.projectLifecycle.mockClear();
    coordinator.applyRunState({
      task_states: [{ session_id: 'session', run_id: 'run', task_status: 'completed', task_revision: 3 }],
    });
    expect(coordinator.projection.isActive('run')).toBe(true);
    expect(session.messages).toHaveLength(count);
    expect(session.terminalRuns || []).toEqual([]);
    expect(deps.requests.projectLifecycle).not.toHaveBeenCalled();
  });

  it('rejects events belonging to an older request attempt before changing lifecycle state', () => {
    const { coordinator, deps, session } = setup();
    const old = { runId: 'run', state: 'running' as const },
      latest = { runId: 'retry', state: 'sending' as const };
    deps.requests.forRun.mockReturnValue({
      s: session,
      request: { type: 'user', id: 'request', text: 'task', attempts: [old, latest] },
      attempt: old,
    });
    coordinator.handleServerEvent(start);
    expect(coordinator.projection.size).toBe(0);
    expect(session.messages).toEqual([]);
    expect(deps.requests.projectLifecycle).not.toHaveBeenCalled();
  });

  it('flushes the last stream frame before terminal projection and preserves one answer', () => {
    vi.useFakeTimers();
    const { coordinator, session } = setup();
    coordinator.handleServerEvent(start);
    coordinator.handleServerEvent({
      type: 'reply_stream',
      session_id: 'session',
      run_id: 'run',
      stream_id: 'stream',
      sequence: 1,
      state: 'partial',
      text: 'partial',
    });
    expect(session.messages.some((message) => message.streamId)).toBe(false);
    coordinator.handleServerEvent({
      type: 'task_done',
      session_id: 'session',
      run_id: 'run',
      task_status: 'completed',
      task_revision: 5,
      event_id: 11,
      mode: 'chat',
      summary: 'final',
      stop_reason: 'done',
    });
    expect(session.messages.filter((message) => message.type === 'asst')).toEqual([
      expect.objectContaining({ text: 'final', streamId: 'stream', streamState: 'complete', finalAnswer: true }),
    ]);
    expect(coordinator.projection.size).toBe(0);
    vi.runAllTimers();
    expect(session.messages.filter((message) => message.type === 'asst')).toHaveLength(1);
  });

  it('withdraws only provisional HTTP admissions after a follow-up authoritative task snapshot', () => {
    const { coordinator } = setup();
    coordinator.applyLifecycle({ type: 'task_start', session_id: 'session', run_id: 'run' }, 'admission');
    coordinator.applyRunState({ response_ack_run_id: 'run' });
    expect(coordinator.projection.isActive('run')).toBe(true);
    coordinator.applyRunState({ response_ack_run_id: 'run', task_states: [] });
    expect(coordinator.projection.isActive('run')).toBe(false);
    coordinator.handleServerEvent(start);
    coordinator.applyRunState({ response_ack_run_id: 'run', task_states: [] });
    expect(coordinator.projection.isActive('run')).toBe(true);
  });

  it('keeps a waiting owner from the snapshot active across same-revision event replay', () => {
    const { coordinator } = setup();
    coordinator.applyRunState({
      active_runs: [{ run_id: 'run', session_id: 'session' }],
      task_states: [
        { run_id: 'run', session_id: 'session', task_revision: 6, task_status: 'waiting', stop_reason: 'approval' },
      ],
    });
    coordinator.handleServerEvent({
      type: 'task_done',
      run_id: 'run',
      session_id: 'session',
      task_revision: 6,
      task_status: 'waiting',
      stop_reason: 'approval',
      event_id: 12,
    });
    expect(coordinator.projection.isActive('run')).toBe(true);
  });

  it('routes question, queue and approval updates through their existing owners', () => {
    const { coordinator, deps, approvals } = setup();
    const question = { id: 'question' },
      input = { id: 'input' };
    coordinator.handleServerEvent({ type: 'user_question', question });
    coordinator.handleServerEvent({ type: 'conversation_input', input });
    const approval = { type: 'shell_approval_status', id: 'approval' };
    coordinator.handleServerEvent(approval);
    expect(deps.questions.apply).toHaveBeenCalledWith(question);
    expect(deps.composer.applyQueuedInput).toHaveBeenCalledWith(input);
    expect(approvals.apply).toHaveBeenCalledWith(approval);
    expect(deps.transport.reconcileRunState).toHaveBeenCalledOnce();
  });

  it('observes every durable commit while coalescing the queued-input refresh', () => {
    vi.useFakeTimers();
    const { coordinator, deps } = setup();
    for (const commit_seq of [1, 2])
      coordinator.handleServerEvent({ type: 'durable_commit', session_id: 'session', commit_seq });
    expect(deps.history.observeCommit).toHaveBeenCalledTimes(2);
    expect(deps.composer.refreshQueuedInputs).not.toHaveBeenCalled();
    vi.advanceTimersByTime(200);
    expect(deps.composer.refreshQueuedInputs).toHaveBeenCalledOnce();
  });

  it('rejects fork-excluded runs before changing messages or request state', () => {
    const { coordinator, deps, session } = setup();
    deps.history.acceptsRun.mockReturnValue(false);
    coordinator.handleServerEvent(start);
    expect(coordinator.projection.size).toBe(0);
    expect(session.messages).toEqual([]);
    expect(deps.requests.projectLifecycle).not.toHaveBeenCalled();
  });
});
