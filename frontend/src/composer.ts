import { runtime } from './runtime';
import { InputOutbox, type QueuedInput, type QueuedRequest } from './input_outbox';
import type { Draft, Message, Project, Session } from './types/domain';
import type { RunDetailsAction } from './run_details';
import { recoveryForFailure } from './failure_presentation';

export interface ComposerDeps {
  $: (id: string) => HTMLElement;
  getRunningSessionId: () => string | null;
  getRunningRunId?: () => string | null;
  getRunningRunIdForSession?: (sessionId: string) => string | null;
  isSessionRunning?: (id: string) => boolean;
  getActiveId: () => string;
  currentProviderId: () => string;
  activeSession: () => Session | undefined;
  findSession: (id: string | null | undefined) => Session | undefined;
  sessionProject: (session: Session | undefined) => Project | null | undefined;
  sessionProjectPath: (id: string) => string;
  currentIntentForSession: (id: string) => string;
  projectPickerBusy: () => boolean;
  persistActive: () => void;
  persistActiveNow: () => void;
  updateComposerContext: () => void;
  renderChat: () => void;
  syncProviderUI: (provider: string) => void;
  switchSession: (id: string) => void;
  addToSession: (id: string, message: Message) => void;
  addSendError: (id: string) => void;
  acceptRunResponse: (response: Response, sessionId: string) => Promise<unknown>;
  attachCurrentChatToPickedProject: () => void;
  openLocalProviderConfig: () => unknown;
  observeQueuedInput(input: QueuedInput): void;
  PROVIDERS?: string[];
}
export interface ComposerApi {
  init(deps: ComposerDeps): void;
  setReady(): void;
  syncSession(): void;
  captureDraft(): void;
  isSending(sessionId?: string): boolean;
  observeRun(sessionId: string, runId: string): void;
  retryTask(sessionId: string, requestId: string): unknown;
  editMessage(sessionId: string, text: string): Promise<boolean>;
  quoteInReply(sessionId: string, text: string): boolean;
  recoveryActions(message: Message): RunDetailsAction[];
  resizeTask(): void;
  updateSend(): void;
  toggleResearchForActive(): void;
  setActiveProvider(id: string): void;
  continueTask(sessionId: string, sourceRunId: string): Promise<void>;
  recallMessage(sessionId: string, text: string): void;
  applyQueuedInput(input: unknown): void;
  queuedInputForRun(runId: string): QueuedInput | undefined;
  refreshQueuedInputs(): Promise<void>;
}

/* Codey composer runtime: task input, send/stop actions, provider selection,
   and project/research context chips. */
(function () {
  'use strict';
  let deps: ComposerDeps = {} as ComposerDeps;
  let PROVIDERS: string[] = [];
  let handlersBound = false;
  let draftSession: Session | null = null;
  let sendingSessionId = '';
  const admissions = new Map<string, { runId: string; controller: AbortController; cancelled: boolean }>();
  function pendingAdmission(sessionId: string) {
    return deps
      .findSession(sessionId)
      ?.messages.flatMap((message) => message.attempts || [])
      .reverse()
      .find(
        (attempt) =>
          attempt.state === 'sending' ||
          (attempt.state === 'failed' && attempt.sendFailure === 'admission_unconfirmed'),
      );
  }
  async function stopRun(runId: string) {
    if (!runId) return;
    const response = await fetch('/api/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId }),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error('Could not confirm Stop');
  }
  async function stopCurrent() {
    const sessionId = activeId();
    const admission = admissions.get(sessionId);
    const runId = admission?.runId || deps.getRunningRunIdForSession?.(sessionId) || pendingAdmission(sessionId)?.runId;
    if (!runId) return;
    if (admission) {
      admission.cancelled = true;
      admission.controller.abort();
    }
    try {
      await stopRun(runId);
      const attempt = runtime.Requests.forRun(sessionId, runId)?.attempt;
      if (attempt && ['sending', 'failed'].includes(attempt.state))
        runtime.Requests.fail(sessionId, runId, '', { state: 'paused', sendFailure: 'cancelled_admission' });
    } catch {
      const attempt = runtime.Requests.forRun(sessionId, runId)?.attempt;
      if (attempt?.state === 'sending')
        runtime.Requests.fail(sessionId, runId, 'Could not confirm cancellation. Try Stop again when connected.', {
          sendFailure: 'admission_unconfirmed',
        });
    } finally {
      updateSend();
    }
  }
  let ready = false;
  let outbox: InputOutbox;

  function liveDefaultProvider() {
    return runtime.UiState.DEFAULT_PROVIDER;
  }

  function $<T extends HTMLElement = HTMLElement>(id: string): T {
    return deps.$(id) as T;
  }
  function runningSessionId() {
    return deps.getRunningSessionId();
  }
  function isRunningForSession(sessionId: string) {
    return deps.isSessionRunning ? deps.isSessionRunning(sessionId) : runningSessionId() === sessionId;
  }
  function activeId() {
    return deps.getActiveId();
  }
  function currentProviderId() {
    return deps.currentProviderId();
  }

  function init(nextDeps: ComposerDeps) {
    deps = nextDeps;
    PROVIDERS = deps.PROVIDERS || [];
    if (!outbox) {
      const element = document.createElement('div');
      const box = $('task').closest('.composer-box');
      (box || $('task')).before(element);
      outbox = new InputOutbox(element, {
        accepted: (request, revision) => clearDraftIfUnchanged(request.session_id, request.task, revision),
        edit: (session, text) => recallMessage(session, text),
        changed: () => updateSend(),
        observe: (input) => deps.observeQueuedInput(input),
      });
    }
    bindHandlers();
    syncSession();
  }

  function captureDraft() {
    if (!draftSession || deps.findSession(draftSession.id) !== draftSession) return;
    const t = $<HTMLTextAreaElement>('task');
    const previous = draftSession.draft;
    const next: Draft = {
      text: t.value,
      start: t.selectionStart ?? 0,
      end: t.selectionEnd ?? 0,
      direction: t.selectionStart === t.selectionEnd ? 'none' : (t.selectionDirection ?? 'none'),
      revision: previous.revision + (previous.text === t.value ? 0 : 1),
    };
    if (
      next.text === previous.text &&
      next.start === previous.start &&
      next.end === previous.end &&
      next.direction === previous.direction &&
      next.revision === previous.revision
    )
      return;
    draftSession.draft = next;
    deps.persistActive();
  }

  function syncSession() {
    outbox?.setSession(activeId());
    if (!deps) return;
    if (draftSession === deps.activeSession()) {
      $<HTMLTextAreaElement>('task').disabled = !ready || !!runtime.ChatFork?.isBlocked(activeId());
      updateSend();
      return;
    }
    captureDraft();
    draftSession = deps.activeSession() || null;
    if (!draftSession) return;
    // The textarea is scoped to the active session. A run in another session
    // must never leave the newly selected chat's input disabled.
    $<HTMLTextAreaElement>('task').disabled = !ready || !!runtime.ChatFork?.isBlocked(activeId());
    const draft = draftSession.draft;
    if ($<HTMLTextAreaElement>('task').value !== draft.text) $<HTMLTextAreaElement>('task').value = draft.text;
    $<HTMLTextAreaElement>('task').setSelectionRange(draft.start, draft.end, draft.direction);
    resizeTask();
    updateSend();
  }

  function resizeTask() {
    const t = $<HTMLTextAreaElement>('task');
    t.style.height = 'auto';
    t.style.height = Math.min(220, Math.max(40, t.scrollHeight)) + 'px';
  }

  function updateSend() {
    if (!deps) return;
    document.querySelector('main')?.classList.toggle('empty-conversation', !deps.activeSession?.()?.messages.length);
    const has = $<HTMLTextAreaElement>('task').value.trim();
    const selection = runtime.ProviderUI.runSelection(deps.activeSession(), currentProviderId()).model_selection;
    const reason = runtime.Models.reason(currentProviderId(), selection?.model);
    const notice = $('model-notice');
    notice.hidden = !reason;
    notice.replaceChildren();
    if (reason) {
      notice.append(
        runtime.Models.hasAny()
          ? 'This model is ' + reason.toLowerCase() + '. Choose another model or enable it in '
          : 'No models enabled. Choose models in ',
      );
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'link-btn';
      action.textContent = 'Settings';
      action.onclick = () => runtime.Settings.open();
      notice.appendChild(action);
    }
    const ownerSessionId = runningSessionId();
    const running = isRunningForSession(activeId());
    const anotherSessionRunning = !!ownerSessionId && !running;
    const sendingCurrent = sendingSessionId === activeId() || admissions.has(activeId());
    const awaitingAnswer = !!runtime.UserQuestions?.pending(activeId());
    const task = $<HTMLTextAreaElement>('task');
    const send = $<HTMLButtonElement>('send');
    const stop = $<HTMLButtonElement>('stop');
    const pendingSend = !!pendingAdmission(activeId());
    send.disabled =
      task.disabled ||
      !has ||
      !!reason ||
      sendingCurrent ||
      pendingSend ||
      !!outbox?.blocked(activeId()) ||
      awaitingAnswer;
    // A run belongs to its session. Other chats remain usable and can place
    // an input in the outbox while the owner run is still active.
    send.style.display = (running || sendingCurrent || pendingSend) && !has ? 'none' : '';
    // Send and Stop share one fixed slot. A different chat's run is opened
    // from the top status bar; it must not put a second action beside Send in
    // the chat that is currently being composed.
    stop.style.display = running || sendingCurrent || pendingSend ? '' : 'none';
    $('send-hint').textContent = awaitingAnswer
      ? 'Answer above'
      : sendingCurrent
        ? 'Sending…'
        : running
          ? has
            ? 'Send next turn'
            : 'Stop'
          : anotherSessionRunning
            ? 'Enter'
            : 'Enter';
    send.setAttribute('aria-label', sendingCurrent ? 'Sending message' : running ? 'Queue message' : 'Send message');
    const owner = deps.findSession(running || sendingCurrent || pendingSend ? activeId() : ownerSessionId);
    stop.setAttribute('aria-label', owner ? 'Stop ' + owner.title : 'Stop');
    stop.title = owner ? 'Stop ' + owner.title : 'Stop';
    const providerButton = $<HTMLButtonElement>('provider-button');
    providerButton.disabled = task.disabled || running || sendingCurrent || pendingSend;
    const effortButton = document.getElementById('effort-button');
    if (effortButton instanceof HTMLButtonElement) effortButton.disabled = providerButton.disabled;
    if (runtime.ConversationUI) runtime.ConversationUI.updateNotice();
    runtime.Requests.refreshActions();
  }

  function toggleResearchForActive() {
    if (runningSessionId() === activeId() || sendingSessionId === activeId()) return;
    const s = deps.activeSession();
    if (!s) return;
    s.research = !s.research;
    deps.persistActiveNow();
    deps.updateComposerContext();
    deps.renderChat();
  }

  function setActiveProvider(id: string) {
    const provider = PROVIDERS.includes(id) ? id : liveDefaultProvider();
    const s = deps.activeSession();
    if (!s) return;
    if (runtime.UiState && typeof runtime.UiState.setDefaultProvider === 'function') {
      runtime.UiState.setDefaultProvider(provider);
    }
    s.provider = provider;
    deps.persistActiveNow();
    deps.syncProviderUI(provider);
    deps.updateComposerContext();
    updateSend();
  }

  function clearDraftIfUnchanged(sessionId: string, draft: string, revision: number) {
    captureDraft();
    const session = deps.findSession(sessionId),
      stored = session?.draft;
    if (!stored || stored.text.trim() !== draft || stored.revision !== revision) return;
    session.draft = { text: '', start: 0, end: 0, direction: 'none', revision: stored.revision + 1 };
    deps.persistActiveNow();
    if (activeId() !== sessionId) return;
    $<HTMLTextAreaElement>('task').value = '';
    resizeTask();
    updateSend();
    deps.updateComposerContext();
  }

  async function reportSendFailure(sessionId: string, response: Response, runId = '') {
    const data = (await response.json().catch(() => null)) as Record<string, unknown> | null;
    let message = '',
      sendFailure = '',
      runOwner = '';
    if (data?.approval) {
      runtime.ShellApprovals.apply(data.approval);
      message = String(data.error || 'Check the command approval status');
      sendFailure = 'approval';
    } else if (response.status === 400 && data?.reason === 'model_selection_invalid') {
      message = 'Select the model again';
      sendFailure = 'model_selection';
    } else if (response.status === 503 && data?.error === 'browser worker busy') {
      message = 'Codey is temporarily busy';
      sendFailure = 'worker_busy';
    } else if (response.status === 409 && data?.error === 'busy') {
      await runtime.Sse.reconcileRunState();
      const owner = deps.findSession(runningSessionId());
      runOwner = owner?.id || '';
      message = owner
        ? owner.id === sessionId
          ? 'This chat is already running'
          : 'Another chat is running'
        : 'Another task is running';
      sendFailure = 'busy';
    }
    if (runId) {
      runtime.Requests.fail(sessionId, runId, message || 'Could not send the message', { sendFailure, runOwner });
      return;
    }
    if (!message) {
      deps.addSendError(sessionId);
      return;
    }
    deps.addToSession(sessionId, { type: 'err', text: message, sessionId, sendFailure, runOwner });
  }

  function recoveryActions(message: Message): RunDetailsAction[] {
    const runOwner = message.runOwner || '';
    const sessionId = message.sessionId || '';
    if (message.failure) {
      const outputScope = new URLSearchParams({ session_id: sessionId, run_id: message.runId || '' });
      const actions = recoveryForFailure(message.failure, {
        execution_id: message.executionId,
        source_id: message.failure.source_id,
        approval_id: message.approval?.id || message.id,
        request_id: message.requestId || message.id,
        revision: message.approval?.revision,
        execution: message.approval?.execution,
        continuation: message.approval?.continuation,
      });
      return actions.map((action) => ({
        disabled:
          (action.kind === 'check_execution' || (action.kind === 'reload_resource' && !!message.executionId)) &&
          (!sessionId || !message.runId),
        label:
          action.kind === 'check_execution'
            ? 'Check status'
            : action.kind === 'reload_resource'
              ? 'Reload'
              : action.kind === 'retry_continuation'
                ? 'Retry continuation'
                : action.kind === 'open_connection'
                  ? 'Connection settings'
                  : 'Retry request',
        onclick: () => {
          if (action.kind === 'check_execution')
            void fetch(`/api/executions/${encodeURIComponent(action.executionId)}/output?${outputScope}`);
          else if (action.kind === 'retry_continuation')
            void fetch('/api/shell_approval/retry', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ id: action.approvalId }),
            });
          else if (action.kind === 'retry_request') void retryTask(sessionId, message.id || '');
          else if (action.kind === 'open_connection') void deps.openLocalProviderConfig();
          else if (action.kind === 'focus_approval') {
            const target = document.querySelector<HTMLElement>(`[data-approval-id="${CSS.escape(action.approvalId)}"]`);
            target?.scrollIntoView({ block: 'center', behavior: 'smooth' });
            target?.focus({ preventScroll: true });
          } else if (action.kind === 'reload_resource') {
            if (message.executionId)
              void fetch(`/api/executions/${encodeURIComponent(message.executionId)}/output?${outputScope}`);
            else
              window.dispatchEvent(
                new CustomEvent('codey:reload-resource', { detail: { resourceId: action.resourceId } }),
              );
          }
        },
      }));
    }
    if (message.sendFailure === 'busy' && deps.findSession(runOwner)) {
      return [
        {
          label: 'Open',
          onclick: () => {
            if (deps.findSession(runOwner)) deps.switchSession(runOwner);
          },
        },
      ];
    }
    if (message.sendFailure === 'model_selection') {
      return [
        {
          label: 'Choose model',
          disabled: isRunningForSession(sessionId),
          onclick: (e: Event) => {
            if (
              !sessionId ||
              !deps.findSession(sessionId) ||
              isRunningForSession(sessionId) ||
              sendingSessionId === sessionId
            )
              return;
            e?.stopPropagation();
            deps.switchSession(sessionId);
            $<HTMLButtonElement>('provider-button').click();
          },
        },
      ];
    }
    return [];
  }

  async function sendTaskFromSession(
    sessionId: string,
    task: string,
    providerId = '',
    onSendStarted: (() => void) | null = null,
    requestId = '',
    preclaimed = false,
    onSendRejected: (() => void) | null = null,
  ) {
    const text = String(task || '').trim();
    const reject = () => {
      onSendRejected?.();
      return false;
    };
    if (
      !text ||
      pendingAdmission(sessionId) ||
      isRunningForSession(sessionId) ||
      (sendingSessionId === sessionId && !preclaimed)
    )
      return reject();
    const s = deps.findSession(sessionId);
    if (!s) return reject();
    const provider = providerId || s.provider || liveDefaultProvider();
    if (!runtime.Models.allows(provider, runtime.ProviderUI.runSelection(s, provider).model_selection?.model))
      return reject();
    const ownsSending = !preclaimed;
    if (ownsSending) {
      sendingSessionId = sessionId;
      updateSend();
      deps.updateComposerContext();
    }
    if (sessionId === activeId() && !s.messages.length) {
      const main = document.querySelector('main');
      main?.classList.add('composer-animating');
      window.setTimeout(() => main?.classList.remove('composer-animating'), 400);
    }
    const started = runtime.Requests.begin(sessionId, text, requestId);
    if (!started) {
      sendingSessionId = '';
      updateSend();
      return reject();
    }
    const runId = started.attempt.runId;
    const project = deps.sessionProjectPath(sessionId);
    const intent = deps.currentIntentForSession(sessionId);
    const admission = { runId, controller: new AbortController(), cancelled: false };
    admissions.set(sessionId, admission);
    try {
      const r = await fetch('/api/run', {
        signal: AbortSignal.any([admission.controller.signal, AbortSignal.timeout(30000)]),
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          run_id: runId,
          project,
          task: text,
          provider,
          intent,
          ...runtime.ProviderUI.runSelection(s, provider),
        }),
      });
      if (admission.cancelled) return false;
      if (!r.ok) {
        await reportSendFailure(sessionId, r, runId);
        onSendRejected?.();
        return false;
      }
      if (admission.cancelled) return false;
      if (typeof onSendStarted === 'function') onSendStarted();
      await deps.acceptRunResponse(r, sessionId);
    } catch {
      if (!admission.cancelled) {
        // An interrupted HTTP confirmation does not prove rejection. Reconcile
        // without holding the composer, and retain this identity for Stop.
        if (runtime.Requests.forRun(sessionId, runId)?.attempt.state === 'sending')
          runtime.Requests.fail(
            sessionId,
            runId,
            'Message delivery could not be confirmed. Check status or stop this request.',
            { sendFailure: 'admission_unconfirmed' },
          );
        void runtime.Sse.reconcileRunState();
      }
      return false;
    } finally {
      if (admissions.get(sessionId) === admission) admissions.delete(sessionId);
      if (ownsSending) {
        sendingSessionId = '';
        updateSend();
        deps.updateComposerContext();
      }
    }
    return true;
  }

  async function sendActiveDraft() {
    const sessionId = activeId();
    const task = $<HTMLTextAreaElement>('task').value.trim();
    const provider = currentProviderId();
    if (!task) return;
    // Claim this session before the state reconciliation below. Without the
    // early claim, the composer appears idle while the first /api/state call
    // is in flight and a second click can start a duplicate request.
    if (sendingSessionId === sessionId) return;
    sendingSessionId = sessionId;
    updateSend();
    deps.updateComposerContext();
    // The run endpoint is the authoritative admission boundary. A blocking
    // state reconciliation here leaves the submitted draft visible while a
    // slow state request is in flight and prevents the actual admission
    // request from reaching the server. Known local runs still use the durable
    // input queue; a stale snapshot is reconciled by the existing 409 path.
    try {
      captureDraft();
      const revision = deps.findSession(sessionId)?.draft.revision;
      if (revision === undefined) return;
      if (isRunningForSession(sessionId)) {
        if (outbox.blocked(sessionId)) return;
        updateSend();
        if (!(await runtime.UiState.ensureSaved())) return;
        const request: QueuedRequest = {
          session_id: sessionId,
          run_id: 'run_' + crypto.randomUUID().replaceAll('-', ''),
          project: deps.sessionProjectPath(sessionId),
          task,
          provider,
          intent: deps.currentIntentForSession(sessionId),
          input_mode: 'follow_up',
          ...runtime.ProviderUI.runSelection(deps.findSession(sessionId), provider),
        };
        await outbox.enqueue(request, revision);
        return;
      }
      const originalDraft = task;
      const originalRevision = revision;
      // Clear the visible draft at admission start. The user has already
      // submitted this text, and waiting for the HTTP response makes a slow
      // admission look like a stuck composer. A rejected HTTP admission puts
      // the exact draft back unless the user has edited it meanwhile.
      clearDraftIfUnchanged(sessionId, originalDraft, originalRevision);
      const restoreRejectedDraft = () => {
        const session = deps.findSession(sessionId);
        if (!session || session.draft.text || session.draft.revision !== originalRevision + 1) return;
        session.draft = {
          text: originalDraft,
          start: originalDraft.length,
          end: originalDraft.length,
          direction: 'none',
          revision: session.draft.revision + 1,
        };
        deps.persistActiveNow();
        if (activeId() === sessionId) {
          const input = $<HTMLTextAreaElement>('task');
          input.value = originalDraft;
          resizeTask();
          updateSend();
        }
      };
      await sendTaskFromSession(sessionId, task, provider, null, '', true, restoreRejectedDraft);
    } finally {
      sendingSessionId = '';
      updateSend();
      deps.updateComposerContext();
    }
  }

  function retryTask(sessionId: string, requestId: string) {
    const s = deps.findSession(sessionId);
    if (!s || !runtime.Requests.canRetry(sessionId, requestId)) return;
    const original = s.messages.find((m: Message) => m.type === 'user' && m.id === requestId);
    if (!original || typeof original.text !== 'string') return;
    return sendTaskFromSession(sessionId, original.text, s.provider || '', null, requestId);
  }

  async function editMessage(sessionId: string, text: string): Promise<boolean> {
    const s = deps.findSession(sessionId);
    if (!s || !String(text || '').trim()) return false;
    return (await sendTaskFromSession(sessionId, text, s.provider || '')) === true;
  }

  function recallMessage(sessionId: string, text: string): void {
    if (sessionId !== activeId()) deps.switchSession(sessionId);
    syncSession();
    const task = $<HTMLTextAreaElement>('task');
    if (task.value && task.value !== text) {
      task.value = task.value.replace(/\s*$/, '') + '\n\n' + text;
    } else task.value = text;
    task.focus();
    task.setSelectionRange(task.value.length, task.value.length);
    captureDraft();
    resizeTask();
    updateSend();
    deps.persistActiveNow();
  }

  function quoteInReply(sessionId: string, text: string) {
    const session = deps.findSession(sessionId);
    if (!session || sessionId !== activeId() || !text || $<HTMLTextAreaElement>('task').disabled) return false;
    syncSession();
    captureDraft();
    const task = $<HTMLTextAreaElement>('task'),
      original = task.value;
    const separator = !original || original.endsWith('\n\n') ? '' : original.endsWith('\n') ? '\n' : '\n\n';
    const quoted = text
      .replace(/\r\n?/g, '\n')
      .split('\n')
      .map((line: string) => '> ' + line)
      .join('\n');
    task.focus({ preventScroll: true });
    task.setSelectionRange(original.length, original.length);
    // One native editing transaction keeps textarea Undo/Redo intact.
    if (!document.execCommand('insertText', false, separator + quoted + '\n\n')) return false;
    captureDraft();
    resizeTask();
    updateSend();
    deps.persistActiveNow();
    return true;
  }

  async function continueTask(sessionId: string, sourceRunId: string) {
    if (!sourceRunId) return;
    if (runtime.UserQuestions?.pending(sessionId)) {
      runtime.UserQuestions.focus(sessionId);
      return;
    }
    if (pendingAdmission(sessionId) || isRunningForSession(sessionId) || sendingSessionId === sessionId) return;
    const s = deps.findSession(sessionId);
    if (!s) return;
    if (
      !runtime.Models.allows(
        s.provider || '',
        runtime.ProviderUI.runSelection(s, s.provider || '').model_selection?.model,
      )
    )
      return;
    const p = deps.sessionProject(s);
    const instruction = 'Continue the unfinished task in this conversation using its existing context.';
    sendingSessionId = sessionId;
    updateSend();
    const admission = {
      runId: 'run_' + crypto.randomUUID().replaceAll('-', ''),
      controller: new AbortController(),
      cancelled: false,
    };
    admissions.set(sessionId, admission);
    try {
      const r = await fetch('/api/run', {
        signal: AbortSignal.any([admission.controller.signal, AbortSignal.timeout(30000)]),
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          run_id: admission.runId,
          project: p?.path || null,
          input: { kind: 'resume_run', source_run_id: sourceRunId, instruction },
          provider: s.provider || liveDefaultProvider(),
          intent: deps.currentIntentForSession(sessionId),
          ...runtime.ProviderUI.runSelection(s, s.provider || liveDefaultProvider()),
        }),
      });
      if (admission.cancelled) return;
      if (r.status === 409 || !r.ok) {
        await reportSendFailure(sessionId, r);
        return;
      }
      if (!admission.cancelled) await deps.acceptRunResponse(r, sessionId);
    } catch {
      if (!admission.cancelled) deps.addSendError(sessionId);
      return;
    } finally {
      if (admissions.get(sessionId) === admission) admissions.delete(sessionId);
      sendingSessionId = '';
      updateSend();
    }
  }

  function bindHandlers() {
    if (handlersBound) return;
    handlersBound = true;
    $('task').addEventListener('input', () => {
      captureDraft();
      resizeTask();
      updateSend();
      deps.updateComposerContext();
    });
    ['select', 'selectionchange', 'blur', 'keyup', 'pointerup'].forEach((type) =>
      $('task').addEventListener(type, captureDraft),
    );
    $('task').addEventListener('keydown', (e: KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        if (!$<HTMLButtonElement>('send').disabled) $<HTMLButtonElement>('send').click();
      }
    });
    $('composer-context').onclick = (e: MouseEvent) => {
      const target = e.target instanceof Element ? e.target.closest('.ctx-token') : null;
      if (!target || runningSessionId() === activeId()) return;
      if (target.id === 'ctx-folder') {
        const s = deps.activeSession();
        if (!s || deps.sessionProject(s) || deps.projectPickerBusy()) return;
        deps.attachCurrentChatToPickedProject();
      } else if (target.id === 'ctx-research') {
        toggleResearchForActive();
      }
    };
    $('composer-context').addEventListener('keydown', (e: KeyboardEvent) => {
      if ((e.key !== 'Enter' && e.key !== ' ') || runningSessionId() === activeId()) return;
      const target = e.target instanceof Element ? e.target.closest('.ctx-token') : null;
      if (!(target instanceof HTMLElement)) return;
      e.preventDefault();
      target.click();
    });
    $('send').onclick = sendActiveDraft;
    $('stop').onclick = () => {
      void stopCurrent();
    };
  }

  runtime.Composer = {
    init,
    setReady: () => {
      ready = true;
      syncSession();
    },
    syncSession,
    captureDraft,
    observeRun: (sessionId: string, runId: string) => {
      if (runtime.Requests.forRun(sessionId, runId)?.attempt.sendFailure === 'cancelled_admission')
        void stopRun(runId).catch(() => {});
    },
    isSending: (sessionId?: string) =>
      (!!sendingSessionId && (!sessionId || sendingSessionId === sessionId)) ||
      (sessionId ? admissions.has(sessionId) : admissions.size > 0),
    retryTask,
    editMessage,
    quoteInReply,
    recoveryActions,
    resizeTask,
    updateSend,
    toggleResearchForActive,
    setActiveProvider,
    continueTask,
    recallMessage,
    applyQueuedInput: (input: unknown) => {
      outbox.apply(input);
    },
    queuedInputForRun: (runId: string) => outbox.forRun(runId),
    refreshQueuedInputs: () => outbox.refresh(),
  };
})();
