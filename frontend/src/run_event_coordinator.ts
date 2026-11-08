import type { ComposerApi } from './composer';
import type { ConversationUiApi } from './conversation_ui';
import { decodeServerEvent, type ServerEvent } from './domain/protocol';
import type { HistoryTask } from './history_repository';
import type { QueuedInput } from './input_outbox';
import type { RequestsApi } from './requests';
import { streamMessage } from './reply_stream';
import { eventSequence, type LifecycleInput, lastRunState, lifecycleState } from './run_lifecycle';
import { RunProjection } from './run_projection';
import { RunStreamBuffer } from './run_stream_buffer';
import type { SessionStateStore } from './session_state_store';
import type { createApprovalController } from './shell_approvals';
import { terminalAnswerMessage } from './terminal_answer';
import type { ChangesData, ResearchPayload, ResearchRun, RunState } from './types/domain';
import type { UiStateApi } from './ui_state';
import type { UserQuestion, UserQuestions } from './user_questions';

export interface RunEventTransport {
  eventKey(data: ServerEvent, fallback?: string): string;
  displayRunStatus(status: string): void;
  reconcileRunState(): Promise<RunState | null>;
  acceptRunResponse(response: Response, sessionId: string): Promise<void>;
  connect(): void;
}

export interface RunEventDeps {
  state: Pick<
    SessionStateStore,
    'sessions' | 'addMessage' | 'replaceMessage' | 'dropMessages' | 'projectTerminalRun' | 'projectPathFor'
  >;
  ui: Pick<UiStateApi, 'persist' | 'flush' | 'refreshInventory' | 'ensureSessionIndexes'>;
  history: Pick<
    ConversationUiApi,
    'acceptsRun' | 'acceptRunEvent' | 'runEventVersion' | 'observeCommit' | 'refreshHistory' | 'syncAnswerIdentity'
  >;
  requests: Pick<RequestsApi, 'forRun' | 'acceptsEvent' | 'projectLifecycle' | 'reconcile' | 'warning' | 'fail'>;
  composer: Pick<ComposerApi, 'observeRun' | 'refreshQueuedInputs' | 'applyQueuedInput' | 'queuedInputForRun'>;
  approvals(): Pick<ReturnType<typeof createApprovalController>, 'apply' | 'decided'>;
  questions: Pick<UserQuestions, 'apply'>;
  transport: RunEventTransport;
  providers: string[];
  revealApproval(sessionId: string, approvalId: string): void;
  setStatus(text: string, kind: string): void;
  updateSend(): void;
  updateComposerContext(): void;
  syncResearchUseProjectButton(): void;
  refreshProviderStatus(immediate?: boolean): void;
  applyProviderStatus(providers: unknown): void;
  recordResearchRun(sessionId: string, runId: string, research: ResearchPayload, receipt: unknown): ResearchRun | null;
  markResearchRestoreAvailability(runIds: unknown): void;
  receiptSummary(receipt: unknown): string;
  receiptChangedCount(receipt: unknown, changes: Partial<ChangesData>): number;
  maybeAddChangesSummary(sessionId: string, eventKey: string, runId: string): Promise<void>;
}

type ServerEventData = ServerEvent & {
  preview?: import('./render').ToolPreview;
  elapsed_ms?: number;
  stream_id?: string;
  sequence?: number;
  state?: string;
  output_bytes?: number;
  output_stored_bytes?: number;
  detail?: string;
  detail_truncated?: boolean;
  final_delivery?: string;
  failure?: import('./failure_presentation').FailureInfo;
};

/** Apply durable facts to disposable browser projections in their original order. */
export class RunEventCoordinator {
  readonly projection: RunProjection;
  private readonly replyStreamBuffer: RunStreamBuffer<ServerEventData>;
  private inputRefreshTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(private readonly deps: RunEventDeps) {
    this.projection = new RunProjection({
      requestIdForRun: (sessionId, runId) => deps.requests.forRun(sessionId, runId)?.request.id,
    });
    this.replyStreamBuffer = new RunStreamBuffer({
      acceptsRun: (sessionId, runId) => deps.history.acceptsRun(sessionId, runId),
      version: (sessionId) => deps.history.runEventVersion(sessionId),
      handle: (data) => this.handleReplyStream(data),
    });
  }

  private removeTeachMessage(sessionId: string, id: string): void {
    this.deps.state.dropMessages(sessionId, (message) => message.type === 'teach' && message.id === id);
  }

  applyLifecycle(
    data: ServerEvent,
    source: LifecycleInput['source'],
    active?: boolean,
    historySessionId?: string,
  ): boolean {
    if (data.run_id && historySessionId && this.deps.history.acceptsRun(historySessionId, data.run_id) === false)
      return false;
    const runId = data.run_id || '',
      sessionId = data.session_id || '';
    const owner = this.deps.state.sessions.find((item) => item.id === sessionId);
    const display = historySessionId ? this.deps.state.sessions.find((item) => item.id === historySessionId) : owner;
    const displays = this.deps.state.sessions.filter(
      (item) =>
        (item === owner || item === display || item.messages.some((message) => message.runId === runId)) &&
        this.deps.history.acceptsRun(item.id, runId),
    );
    const session = owner || display || displays[0];
    if (!session || !runId) return false;
    if (session.terminalRuns?.includes(runId)) this.projection.restoreTerminal(sessionId, runId);
    const request = this.deps.requests.forRun(sessionId, runId);
    if (source !== 'snapshot' && request && request.request.attempts.at(-1) !== request.attempt) return false;
    const state = lifecycleState(
      data.task_status,
      data.stop_reason,
      data.type === 'task_start' ? 'running' : data.stop_reason || 'done',
    );
    const view = this.projection.transition({
      runId,
      sessionId,
      state,
      active: active ?? (state === 'running' ? true : undefined),
      revision: data.task_revision,
      sequence: eventSequence(data.event_id),
      source,
    });
    if (!view) return false;
    // Every lifecycle write follows the one transition decision above. These are
    // derived UI indexes; the durable task and its revision remain authoritative.
    for (const target of displays) {
      this.deps.state.projectTerminalRun(target.id, runId, view.terminal, source === 'snapshot');
      if (view.state !== 'idle')
        this.recordRunState(target.id, runId, target === owner || !view.active ? view.state : 'unknown', data);
    }
    if (view.active) this.deps.composer.observeRun?.(sessionId, runId);
    this.deps.requests.projectLifecycle({ ...data, session_id: sessionId, run_id: runId }, view.state);
    if (data.type === 'task_done') data.stop_reason = view.state;
    this.deps.ui.persist();
    if (this.projection.size) this.deps.transport.displayRunStatus('running');
    else this.deps.setStatus('', 'ok');
    this.deps.updateSend();
    this.deps.updateComposerContext();
    this.deps.syncResearchUseProjectButton();
    return true;
  }

  private handleReplyStream(data: ServerEventData) {
    const sid = data.session_id || '';
    const runId = (data.run_id || '').toString();
    const runIsActive = (id: string) => this.projection.isActive(id) || id === this.projection.primaryRun?.runId;
    if (!runId || !runIsActive(runId)) return;
    if (!this.deps.requests.acceptsEvent({ ...data, session_id: data.session_id || '', run_id: data.run_id || '' }))
      return;
    const session = this.deps.state.sessions.find((session) => session.id === sid);
    const message = session && streamMessage(session, data);
    if (
      message &&
      !this.deps.state.replaceMessage(
        sid,
        (item) => item.streamId === message.streamId && item.runId === runId,
        message,
      )
    )
      this.deps.state.addMessage(sid, message);
  }

  handleServerEvent(raw: unknown, lifecycleSource: 'event' | 'snapshot' = 'event') {
    const data = decodeServerEvent(raw) as ServerEventData;
    if (data.type === 'conversation_inventory_changed') {
      void this.deps.ui.refreshInventory?.();
      return;
    }
    if (data.type === 'durable_commit') {
      void this.deps.history.observeCommit(decodeServerEvent(raw));
      if (this.inputRefreshTimer === undefined)
        this.inputRefreshTimer = setTimeout(() => {
          this.inputRefreshTimer = undefined;
          void this.deps.composer.refreshQueuedInputs();
        }, 200);
      return;
    }
    if (data.type === 'user_question' || data.type === 'user_question_state') {
      this.deps.questions.apply((raw as { question: unknown }).question);
      return;
    }
    if (data.type === 'conversation_input') {
      this.deps.composer.applyQueuedInput((raw as { input: unknown }).input);
      return;
    }
    if (data.type === 'shell_approval_status') {
      this.deps.approvals().apply(raw);
      void this.reconcileRunState();
      return;
    }
    if (data.type === 'resync_required') {
      void this.reconcileRunState();
      this.deps.refreshProviderStatus(true);
      return;
    }
    if (data.type === 'status') {
      this.deps.transport.displayRunStatus(data.status || '');
      return;
    }
    if (data.type === 'providers') {
      this.deps.applyProviderStatus(data.providers);
      return;
    }
    // Terminal control releases the run even when its display was removed.
    if (data.type === 'task_done') {
      this.replyStreamBuffer.flush();
      if (!this.applyLifecycle(data, lifecycleSource)) return;
    }
    this.deps.history.acceptRunEvent(data.session_id || '', data.run_id || '', () =>
      this.handleDisplayEvent(raw, data, lifecycleSource),
    );
  }

  private handleDisplayEvent(raw: unknown, data: ServerEventData, lifecycleSource: 'event' | 'snapshot'): void {
    if (data.type === 'reply_stream') {
      this.replyStreamBuffer.enqueue(data);
      return;
    }
    this.replyStreamBuffer.flush();
    if (data.type === 'task_start') {
      if (!this.applyLifecycle(data, lifecycleSource)) return;
      const input = this.deps.composer.queuedInputForRun(data.run_id || '');
      if (input) this.observeQueuedInput({ ...input, state: 'dispatching' });
    }
    const runId = (data.run_id || '').toString();
    const sid = data.session_id || '';
    if (
      data.type !== 'task_start' &&
      data.type !== 'task_done' &&
      !this.deps.requests.acceptsEvent({ ...data, session_id: data.session_id || '', run_id: data.run_id || '' })
    )
      return;
    const mayOutliveRun = [
      'task_start',
      'task_done',
      'shell_request',
      'shell_result',
      'teach_request',
      'reply',
      'ghost_post_turn_warning',
      'resync_required',
    ];
    const runIsActive = (id: string) => this.projection.isActive(id) || id === this.projection.primaryRun?.runId;
    if (runId && !mayOutliveRun.includes(data.type) && !runIsActive(runId)) return;
    if (data.type === 'turn') {
      this.deps.state.addMessage(sid, {
        type: 'turn',
        n: data.turn || 0,
        note: (data.note || '').toString(),
        reasoning: typeof data.reasoning === 'string' ? data.reasoning : '',
        runId,
        eventKey: this.deps.transport.eventKey(data, runId ? `turn:${runId}:${data.turn || 0}` : ''),
      });
      return;
    }
    if (data.type === 'reasoning') {
      if (typeof data.text === 'string' && data.text.trim())
        this.deps.state.addMessage(sid, {
          type: 'thinking',
          text: data.text,
          turn: data.turn || 0,
          runId,
          eventKey: this.deps.transport.eventKey(data, `reasoning:${runId}:${data.turn || 0}`),
        });
      return;
    }
    if (data.type === 'tool_started') {
      const rawToolId = (data.call_id || data.tool_id || '').toString();
      const toolKey = rawToolId ? `${runId}:${rawToolId}` : '';
      const s = this.deps.state.sessions.find((x) => x.id === sid);
      if (s && toolKey) {
        this.deps.ui.ensureSessionIndexes(s);
        if (s._toolKeys!.has(toolKey)) return;
      }
      this.deps.state.addMessage(sid, {
        type: 'tool_pending',
        callId: data.call_id || '',
        parentCallId: data.parent_call_id || '',
        toolKey,
        eventKey: toolKey ? `tool:${toolKey}:start` : '',
        kind: data.kind || '',
        toolName: data.tool_name || '',
        turn: data.turn || 0,
        path: data.path || '',
        activity: (data.activity || '').toString().slice(0, 200),
        pending: true,
        runId,
      });
      return;
    }
    if (data.type === 'tool') {
      const rawToolId = (data.call_id || data.tool_id || '').toString();
      const toolKey = rawToolId ? `${runId}:${rawToolId}` : '';
      const toolStatus = (data.status || (data.ok === false ? 'error' : 'ok')).toString();
      const message = {
        type: 'tool',
        callId: data.call_id || '',
        parentCallId: data.parent_call_id || '',
        toolKey,
        eventKey: toolKey ? `tool:${toolKey}:final` : '',
        kind: data.kind || '',
        toolName: data.tool_name || '',
        turn: data.turn || 0,
        changed: data.changed === true,
        command: data.command || '',
        path: data.path || '',
        result: (data.detail ?? data.result ?? '').toString(),
        preview: data.preview,
        outputTruncated: data.detail_truncated || data.truncated,
        outputBytes: data.output_bytes,
        outputStoredBytes: data.output_stored_bytes,
        failure: data.failure,
        status: toolStatus,
        error: toolStatus === 'error',
        runId,
      };
      if (
        toolKey &&
        this.deps.state.replaceMessage(sid, (item) => item.type === 'tool_pending' && item.toolKey === toolKey, message)
      )
        return;
      this.deps.state.addMessage(sid, message);
      return;
    }
    if (data.type === 'info') {
      if (data.text)
        this.deps.state.addMessage(sid, {
          type: 'info',
          text: data.text,
          runId,
          eventKey: this.deps.transport.eventKey(data),
        });
      return;
    }
    if (data.type === 'ghost_post_turn_warning') {
      if (this.deps.requests.warning(sid, runId)) return;
      const detail = [data.stage, data.error_type, data.error_ref].filter(Boolean).join(' · ');
      this.deps.state.addMessage(sid, {
        type: 'local_update_warning',
        text: 'Local memory update unavailable' + (detail ? ` · ${detail}` : ''),
        sessionId: sid,
        runId,
        eventKey: this.deps.transport.eventKey(data, runId ? `local-health:${runId}:${data.error_ref || ''}` : ''),
      });
      return;
    }
    if (data.type === 'teach_request') {
      this.removeTeachMessage(sid, data.id || '');
      this.deps.state.addMessage(sid, {
        type: 'teach',
        id: data.id,
        text: data.text,
        sessionId: sid,
        runId,
        eventKey: `teach:${data.id}`,
      });
      return;
    }
    if (data.type === 'task_start') {
      return;
    }
    if (data.type === 'task_done') {
      const research = data.research;
      const summary = (data.summary || '').toString();
      const reason = (data.stop_reason || 'done').toString();
      if (reason === 'running' || reason === 'waiting' || reason === 'idle') return;
      const requestFailed = this.deps.requests.forRun(sid, runId)?.attempt.state === 'failed';
      const terminalKey = runId ? `terminal:${runId}` : '';
      const answerKey = terminalKey ? `${terminalKey}:answer` : '';
      const receiptKey = terminalKey ? `${terminalKey}:receipt` : '';
      const project = this.deps.state.projectPathFor(sid);
      let shownReceipt = false;
      const researchRun = research ? this.deps.recordResearchRun(sid, runId, research, data.receipt) : null;
      if (reason === 'error' && data.provider_failure && data.provider_failure.action === 'connect') {
        if (data.provider && this.deps.providers.includes(data.provider)) {
          this.deps.applyProviderStatus([{ id: data.provider, available: false }]);
        }
        if (!requestFailed) {
          // Preserve the established send-error hook for status bookkeeping.
          // addSendError(sid, terminalKey, runId)
          this.deps.state.addMessage(sid, {
            type: 'err',
            text: summary || 'Could not connect to the provider',
            sessionId: sid,
            runId,
            eventKey: terminalKey,
            failure: data.failure,
          });
        }
        this.deps.ui.flush();
        return;
      }
      if (requestFailed) {
        // The request's status position already contains this attempt's failure.
      } else if (summary.startsWith('ERROR:')) {
        this.deps.state.addMessage(sid, {
          type: 'err',
          text: summary.replace(/^ERROR:\s*/i, ''),
          sessionId: sid,
          runId,
          eventKey: terminalKey,
          failure: data.failure,
        });
      } else if (reason === 'max_turns') {
        const turns = data.turns || data.max_turns || 0;
        const label = turns ? `Reached turn limit (${turns})` : 'Reached turn limit';
        this.deps.state.addMessage(sid, { type: 'limit', text: label, sessionId: sid, runId, eventKey: terminalKey });
      } else if (reason === 'no_progress') {
        this.deps.state.addMessage(sid, {
          type: 'pause',
          text: 'Paused after several turns without new file writes',
          sessionId: sid,
          runId,
          eventKey: terminalKey,
        });
      } else if (reason === 'approval' || reason === 'question') {
        // shell approval card is emitted separately
      } else if (reason === 'stopped') {
        const turns = data.turns || 0;
        const label = turns ? `Stopped after ${turns} turn${turns === 1 ? '' : 's'}` : 'Stopped';
        this.deps.state.addMessage(sid, { type: 'pause', text: label, sessionId: sid, runId, eventKey: terminalKey });
      } else if (reason === 'error' || reason === 'provider_failure') {
        const text = summary || 'Task failed';
        this.deps.state.addMessage(sid, {
          type: 'err',
          text,
          sessionId: sid,
          runId,
          eventKey: terminalKey,
          failure: data.failure,
        });
      } else if (reason === 'done') {
        const terminalAnswer = terminalAnswerMessage(data, runId, this.projection.turnIdForRun(sid, runId));
        if (data.mode === 'research') {
          if (summary)
            this.deps.state.addMessage(
              sid,
              terminalAnswer || { type: 'asst', text: summary, runId, eventKey: answerKey },
            );
          if (this.deps.receiptSummary(data.receipt)) {
            this.deps.state.addMessage(sid, {
              type: 'research_done',
              text: this.deps.receiptSummary(data.receipt),
              runId,
              sessionId: sid,
              eventKey: receiptKey,
            });
            shownReceipt = true;
          }
        } else if (data.mode === 'chat') {
          if (summary)
            this.deps.state.addMessage(
              sid,
              terminalAnswer || { type: 'asst', text: summary, runId, eventKey: answerKey },
            );
        } else if (project && summary) {
          this.deps.state.addMessage(
            sid,
            terminalAnswer || { type: 'asst', text: summary, runId, eventKey: answerKey },
          );
        }
        const changes: Partial<ChangesData> = data.changes || {};
        const changed = !!data.changed;
        const changedCount = this.deps.receiptChangedCount(data.receipt, changes);
        if (changed && changedCount > 0 && this.deps.receiptSummary(data.receipt)) {
          this.deps.state.addMessage(sid, {
            type: 'done',
            text: this.deps.receiptSummary(data.receipt),
            project: changes.project || project,
            changed: true,
            runId,
            sessionId: sid,
            eventKey: receiptKey,
          });
          shownReceipt = true;
        } else if (!project && summary && data.mode !== 'research' && data.mode !== 'chat') {
          this.deps.state.addMessage(sid, {
            type: 'done',
            text: summary,
            sessionId: sid,
            runId,
            eventKey: terminalKey,
          });
        }
      } else if (summary) {
        this.deps.state.addMessage(sid, { type: 'done', text: summary, sessionId: sid, runId, eventKey: terminalKey });
      }
      if (reason === 'done' && sid && runId) void this.deps.history.syncAnswerIdentity(sid, runId);
      if (reason !== 'error' && reason !== 'approval' && data.changed && !shownReceipt)
        this.deps.maybeAddChangesSummary(sid, terminalKey, runId);
      this.deps.ui.flush();
      return;
    }
    if (data.type === 'shell_request') {
      if (this.deps.approvals().decided(data.id || '')) return;
      const isNew = !this.deps.state.sessions
        .find((session) => session.id === sid)
        ?.messages.some((message) => message.id === data.id && message.type === 'shell_request');
      this.deps.state.addMessage(sid, {
        type: 'shell_request',
        id: data.id,
        command: data.command,
        commandSha256: data.command_sha256,
        commandChars: data.command_chars,
        commandTruncated: !!data.command_truncated,
        cwd: data.cwd,
        project: data.project,
        riskLabel: data.risk_label,
        riskTitle: data.risk_title,
        riskDetail: data.risk_detail,
        sessionRuleAvailable: data.session_rule_available,
        projectRuleAvailable: data.project_rule_available,
        sessionId: sid,
        runId,
        eventKey: `shell:${data.id}`,
      });
      if (isNew) this.deps.revealApproval(sid, data.id || '');
      return;
    }
    if (data.type === 'shell_result') {
      this.deps.state.addMessage(sid, {
        type: 'shell_result',
        id: data.id,
        callId: data.call_id || '',
        approved: !!data.approved,
        shellStatus: data.status || '',
        command: data.command,
        commandSha256: data.command_sha256,
        commandChars: data.command_chars,
        commandTruncated: !!data.command_truncated,
        cwd: data.cwd,
        project: data.project,
        output: data.output,
        outputTruncated: !!data.truncated,
        exitCode: data.exit_code,
        executionId: data.execution_id,
        failure: data.failure,
        sessionId: sid,
        runId,
        eventKey: `shell-result:${data.id}`,
      });
      return;
    }
    if (data.type === 'reply') {
      const answerKey = runId ? `terminal:${runId}:answer` : '';
      if (data.text) {
        const final = terminalAnswerMessage(
          { summary: data.text, stop_reason: 'done', final_delivery: 'success' },
          runId,
          this.projection.turnIdForRun(sid, runId),
        );
        this.deps.state.addMessage(sid, final || { type: 'asst', text: data.text, runId, eventKey: answerKey });
      }
      return;
    }
    if (data.type === 'review') {
      if (data.text)
        this.deps.state.addMessage(sid, {
          type: 'review',
          text: data.text,
          sessionId: sid,
          runId,
          eventKey: this.deps.transport.eventKey(data),
        });
      return;
    }
  }

  projectTaskStates(tasks: HistoryTask[], owners: Map<string, string>, historySessionId?: string): void {
    for (const row of tasks) {
      const state = lifecycleState(row.task_status, row.stop_reason, 'idle');
      this.applyLifecycle(
        { ...row, type: state === 'running' ? 'task_start' : 'task_done' },
        'snapshot',
        state === 'running' || (owners.has(row.run_id) && ['approval', 'question', 'waiting'].includes(state)),
        historySessionId,
      );
    }
  }

  applyRunState(data: RunState) {
    const owners = new Map((data.active_runs || []).map((run) => [run.run_id, run.session_id]));
    if (data.busy && data.run_id && data.session_id) owners.set(data.run_id, data.session_id);
    this.projectTaskStates(data.task_states || [], owners);
    (data.shell_approvals || []).forEach(this.deps.approvals().apply);
    if (data.last_shell_result) this.handleServerEvent(data.last_shell_result);
    if (data.last_terminal_event) this.handleServerEvent(data.last_terminal_event, 'snapshot');
    if (data.pending_event) this.handleServerEvent(data.pending_event);
    // Only withdraw the provisional HTTP admission after its follow-up snapshot.
    // Confirmed task events/revisions keep their lifecycle authority.
    const acknowledged = data.response_ack_run_id;
    if (
      acknowledged &&
      data.task_states &&
      !owners.has(acknowledged) &&
      !data.task_states.some((task) => task.run_id === acknowledged) &&
      this.projection.forgetAdmission(acknowledged)
    ) {
      // The projection withdraws the provisional admission and updates its index.
    }
    this.deps.markResearchRestoreAvailability(data.research_restore_runs || []);
    this.deps.requests.reconcile({
      session_id: data.session_id ?? '',
      run_id: data.run_id ?? '',
      busy: this.projection.size > 0,
      active_runs: this.projection.entries().map(([run_id, session_id]) => ({ run_id, session_id })),
      response_ack_run_id: data.response_ack_run_id,
    });
    this.deps.updateSend();
    this.deps.updateComposerContext();
    this.deps.syncResearchUseProjectButton();
  }

  reconcileRunState() {
    return this.deps.transport.reconcileRunState();
  }

  async acceptRunResponse(response: Response, sessionId: string) {
    return this.deps.transport.acceptRunResponse(response, sessionId);
  }

  connectEvents() {
    return this.deps.transport.connect();
  }

  observeQueuedInput(input: QueuedInput): void {
    const sid = input.request.session_id,
      runId = input.request.run_id;
    const session = this.deps.state.sessions.find((session) => session.id === sid);
    if (!session) return;
    if (input.state === 'consumed') {
      void this.deps.history.refreshHistory(sid, true, true);
      return;
    }
    const represented = session.messages.some(
      (message) =>
        message.type === 'user' &&
        (message.queuedInputId === input.id ||
          message.id === input.id ||
          message.runId === runId ||
          message.attempts?.some((attempt) => attempt.runId === runId)),
    );
    if (['dispatching', 'submitted', 'completed', 'unconfirmed'].includes(input.state) && !represented) {
      this.deps.state.addMessage(sid, {
        type: 'user',
        id: input.id,
        queuedInputId: input.id,
        text: input.request.task,
        ...(input.request.attachments?.length ? { attachments: input.request.attachments } : {}),
        attempts: [
          { runId, state: input.state === 'completed' ? 'done' : input.state === 'submitted' ? 'running' : 'sending' },
        ],
      });
      this.deps.state.addMessage(sid, { type: 'request_status', sessionId: sid, requestId: input.id });
    }
    if (['failed', 'unconfirmed'].includes(input.state))
      this.deps.requests.fail(sid, runId, input.error || 'Delivery could not be confirmed', {
        sendFailure: 'queued_input',
      });
    if (input.state === 'completed' && input.terminal) this.handleServerEvent(input.terminal);
  }

  observeUserQuestion(question: UserQuestion): void {
    const session = this.deps.state.sessions.find((session) => session.id === question.session_id);
    if (!session) return;
    if (!session.messages.some((message) => message.type === 'user_question' && message.id === question.id))
      this.deps.state.addMessage(session.id, {
        type: 'user_question',
        id: question.id,
        runId: question.run_id,
        eventKey: 'question:' + question.id,
      });
    if (question.state !== 'pending')
      this.deps.state.addMessage(session.id, {
        type: 'run_state',
        runId: question.run_id,
        state: question.state === 'cancelled' ? 'stopped' : question.state === 'answered' ? 'done' : 'blocked',
        eventKey: `question-state:${question.id}:${question.revision}`,
      });
  }

  private recordRunState(sid: string, runId: string, state: string, data: ServerEvent & { elapsed_ms?: unknown }) {
    const session = this.deps.state.sessions.find((item) => item.id === sid);
    if (!session || lastRunState(session, runId) === state) return;
    const revision = session.messages.filter((item) => item.type === 'run_state' && item.runId === runId).length;
    this.deps.state.addMessage(sid, {
      type: 'run_state',
      state,
      runId,
      elapsedMs: typeof data.elapsed_ms === 'number' ? data.elapsed_ms : undefined,
      eventKey: this.deps.transport.eventKey(data, `run-state:${runId}:${revision}:${state}`),
    });
  }
}
