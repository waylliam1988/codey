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
  const acceptsRun = vi.fn(() => true);
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
      acceptsRun,
      acceptRunEvent: vi.fn((_sid: string, _run: string, accept: () => void) => {
        if (acceptsRun()) accept();
      }),
      runEventVersion: vi.fn(() => '0'),
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
    previewHtml: vi.fn(),
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

const htmlDone = {
  ...start,
  type: 'task_done',
  task_status: 'completed',
  stop_reason: 'done',
  task_revision: 5,
  event_id: 11,
  mode: 'chat',
  changed: true,
  changes: { changed_count: 1, files: [{ path: 'snake.html', status: 'A' }] },
};

describe('RunEventCoordinator ordering', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('previews the HTML changed by a live run once', () => {
    const { coordinator, deps } = setup();
    coordinator.handleServerEvent(start);
    coordinator.handleServerEvent(htmlDone);
    coordinator.handleServerEvent(htmlDone);
    expect(deps.previewHtml).toHaveBeenCalledExactlyOnceWith('session', 'snake.html', '');
  });

  it('does not auto-open a restored result or an ambiguous or incomplete file list', () => {
    const { coordinator, deps } = setup();
    coordinator.handleServerEvent(start);
    coordinator.handleServerEvent(htmlDone, 'snapshot');
    for (const [index, files, changedCount] of [
      [1, [{ path: 'one.html' }, { path: 'two.html' }], 2],
      [2, [{ path: 'only-visible.html' }], 4],
      [3, [{ path: 'deleted.html', status: 'D' }], 1],
    ] as const) {
      const runId = `run-${index}`;
      coordinator.handleServerEvent({ ...start, run_id: runId });
      coordinator.handleServerEvent({
        ...htmlDone,
        run_id: runId,
        changes: { files, changed_count: changedCount },
      });
    }
    expect(deps.previewHtml).not.toHaveBeenCalled();
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

  it.each(['answered', 'cancelled'])('keeps an %s assistance question separate from task completion', (state) => {
    const { coordinator, session } = setup();
    coordinator.observeUserQuestion({
      id: 'q',
      session_id: 'session',
      run_id: 'run',
      revision: 2,
      state,
      question: 'Continue?',
      options: ['Continue', 'Cancel'],
    });
    expect(session.messages.some((message) => message.type === 'user_question')).toBe(true);
    expect(session.messages.some((message) => message.type === 'run_state' && message.state === 'done')).toBe(false);
    expect(session.messages.some((message) => message.type === 'run_state' && message.state === 'stopped')).toBe(
      state === 'cancelled',
    );
  });

  it('projects each settled browser call duration independently', () => {
    const { coordinator, session } = setup();
    coordinator.handleServerEvent(start);
    coordinator.handleServerEvent({
      ...start,
      type: 'tool',
      tool_name: 'browser_click',
      tool_id: '1:0',
      call_id: 'click',
      ok: true,
      status: 'ok',
      elapsed_ms: 27,
    });
    coordinator.handleServerEvent({
      ...start,
      type: 'tool',
      tool_name: 'browser_fill',
      tool_id: '2:0',
      call_id: 'fill',
      ok: true,
      status: 'ok',
      elapsed_ms: 5,
    });
    expect(session.messages.filter((message) => message.type === 'tool').map((message) => message.elapsedMs)).toEqual([
      27, 5,
    ]);
  });

  it('keeps control information flowing while unknown display events await validation', () => {
    const { coordinator, deps, approvals, session } = setup();
    deps.history.acceptRunEvent.mockImplementation(() => {});
    coordinator.handleServerEvent(start);
    expect(session.messages).toEqual([]);
    coordinator.handleServerEvent({ type: 'conversation_input', session_id: 'session', run_id: 'unknown', input: {} });
    coordinator.handleServerEvent({ type: 'shell_approval_status', session_id: 'session', run_id: 'unknown' });
    expect(deps.composer.applyQueuedInput).toHaveBeenCalledOnce();
    expect(approvals.apply).toHaveBeenCalledOnce();
    expect(deps.history.acceptRunEvent).toHaveBeenCalledOnce();
  });
});
