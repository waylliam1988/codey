import type { Draft, Message, Project, Session } from './types/domain';
import type { RunDetailsAction } from './run_details';

export interface ComposerDeps {
  $: (id: string) => HTMLElement;
  getRunningSessionId: () => string | null;
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
  PROVIDERS?: string[];
}
export interface ComposerApi {
  init(deps: ComposerDeps): void;
  syncSession(): void;
  captureDraft(): void;
  isSending(): boolean;
  retryTask(sessionId: string, requestId: string): unknown;
  quoteInReply(sessionId: string, text: string): boolean;
  recoveryActions(message: Message): RunDetailsAction[];
  resizeTask(): void;
  updateSend(): void;
  toggleResearchForActive(): void;
  setActiveProvider(id: string): void;
  continueTask(sessionId: string): Promise<void>;
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

  function liveDefaultProvider() {
    return window.UiState.DEFAULT_PROVIDER;
  }

  function $<T extends HTMLElement = HTMLElement>(id: string): T {
    return deps.$(id) as T;
  }
  function runningSessionId() {
    return deps.getRunningSessionId();
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
    if (!deps || draftSession === deps.activeSession()) return;
    captureDraft();
    draftSession = deps.activeSession() || null;
    if (!draftSession) return;
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
    const has = $<HTMLTextAreaElement>('task').value.trim();
    const selection = window.ProviderUI.runSelection(deps.activeSession(), currentProviderId()).model_selection;
    const reason = window.Models.reason(currentProviderId(), selection?.model);
    const notice = $('model-notice');
    notice.hidden = !reason;
    notice.replaceChildren();
    if (reason) {
      notice.append(
        window.Models.hasAny()
          ? 'This model is ' + reason.toLowerCase() + '. Choose another model or enable it in '
          : 'No models enabled. Choose models in ',
      );
      const action = document.createElement('button');
      action.className = 'link-btn';
      action.textContent = 'Settings';
      action.onclick = () => window.Settings.open();
      notice.appendChild(action);
    }
    const running = !!runningSessionId();
    const task = $<HTMLTextAreaElement>('task');
    const send = $<HTMLButtonElement>('send');
    const stop = $<HTMLButtonElement>('stop');
    send.disabled = task.disabled || !has || !!reason || running || !!sendingSessionId;
    send.style.display = running ? 'none' : '';
    stop.style.display = running ? '' : 'none';
    $('send-hint').textContent = sendingSessionId ? 'Sending…' : running ? 'Stop' : 'Enter';
    send.setAttribute('aria-label', sendingSessionId ? 'Sending message' : 'Send message');
    const owner = deps.findSession(runningSessionId());
    stop.setAttribute('aria-label', owner ? 'Stop ' + owner.title : 'Stop');
    stop.title = owner ? 'Stop ' + owner.title : 'Stop';
    const providerButton = $<HTMLButtonElement>('provider-button');
    providerButton.disabled = task.disabled || running || !!sendingSessionId;
    const effortButton = document.getElementById('effort-button');
    if (effortButton instanceof HTMLButtonElement) effortButton.disabled = providerButton.disabled;
    if (window.ConversationUI) window.ConversationUI.updateNotice();
    window.Requests.refreshActions();
  }

  function toggleResearchForActive() {
    if (runningSessionId() || sendingSessionId) return;
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
    if (window.UiState && typeof window.UiState.setDefaultProvider === 'function') {
      window.UiState.setDefaultProvider(provider);
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
    if (response.status === 400 && data?.reason === 'model_selection_invalid') {
      message = 'Select the model again';
      sendFailure = 'model_selection';
    } else if (response.status === 503 && data?.error === 'browser worker busy') {
      message = 'Codey is temporarily busy';
      sendFailure = 'worker_busy';
    } else if (response.status === 409 && data?.error === 'busy') {
      await window.Sse.reconcileRunState();
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
      window.Requests.fail(sessionId, runId, message || 'Could not send the message', { sendFailure, runOwner });
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
          disabled: !!runningSessionId(),
          onclick: (e: Event) => {
            if (!sessionId || !deps.findSession(sessionId) || runningSessionId() || sendingSessionId) return;
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
  ) {
    const text = String(task || '').trim();
    if (!text || runningSessionId() || sendingSessionId) return false;
    const s = deps.findSession(sessionId);
    if (!s) return false;
    const provider = providerId || s.provider || liveDefaultProvider();
    if (!window.Models.allows(provider, window.ProviderUI.runSelection(s, provider).model_selection?.model))
      return false;
    sendingSessionId = sessionId;
    updateSend();
    deps.updateComposerContext();
    const started = window.Requests.begin(sessionId, text, requestId);
    if (!started) {
      sendingSessionId = '';
      updateSend();
      return false;
    }
    const runId = started.attempt.runId;
    const project = deps.sessionProjectPath(sessionId);
    const intent = deps.currentIntentForSession(sessionId);
    try {
      const r = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          run_id: runId,
          project,
          task: text,
          provider,
          intent,
          ...window.ProviderUI.runSelection(s, provider),
        }),
      });
      if (!r.ok) {
        await reportSendFailure(sessionId, r, runId);
        return false;
      }
      if (typeof onSendStarted === 'function') onSendStarted();
      await deps.acceptRunResponse(r, sessionId);
    } catch {
      await window.Sse.reconcileRunState();
      const attempt = window.Requests.forRun(sessionId, runId)?.attempt;
      if (attempt?.state === 'sending') window.Requests.fail(sessionId, runId, 'Response was not confirmed');
      return false;
    } finally {
      sendingSessionId = '';
      updateSend();
      deps.updateComposerContext();
    }
    return true;
  }

  async function sendActiveDraft() {
    const sessionId = activeId();
    const task = $<HTMLTextAreaElement>('task').value.trim();
    const provider = currentProviderId();
    if (!task) return;
    captureDraft();
    const revision = deps.findSession(sessionId)?.draft.revision;
    if (revision === undefined) return;
    await sendTaskFromSession(sessionId, task, provider, () => clearDraftIfUnchanged(sessionId, task, revision));
  }

  function retryTask(sessionId: string, requestId: string) {
    const s = deps.findSession(sessionId);
    if (!s || !window.Requests.canRetry(sessionId, requestId)) return;
    const original = s.messages.find((m: Message) => m.type === 'user' && m.id === requestId);
    if (!original || typeof original.text !== 'string') return;
    return sendTaskFromSession(sessionId, original.text, s.provider || '', null, requestId);
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

  async function continueTask(sessionId: string) {
    if (runningSessionId() || sendingSessionId) return;
    const s = deps.findSession(sessionId);
    if (!s) return;
    if (
      !window.Models.allows(
        s.provider || '',
        window.ProviderUI.runSelection(s, s.provider || '').model_selection?.model,
      )
    )
      return;
    const p = deps.sessionProject(s);
    if (!p) {
      deps.addToSession(sessionId, {
        type: 'err',
        text: 'Only project tasks can be continued; plain chats have no tool loop.',
        sessionId,
      });
      return;
    }
    deps.addToSession(sessionId, { type: 'info', text: 'continue task' });
    const sourceMessage = [...(Array.isArray(s.messages) ? s.messages : [])]
      .reverse()
      .find((m) => m && m.type === 'user' && Array.isArray(m.attempts) && m.attempts.length);
    const sourceRunId = sourceMessage?.attempts?.at(-1)?.runId || '';
    if (!sourceRunId) {
      deps.addToSession(sessionId, { type: 'err', text: 'No source run is available to resume.', sessionId });
      return;
    }
    const task = [
      'Continue the unfinished task in this same conversation.',
      'Use the existing project context and finish the original user request.',
      'If the work is complete, reply with a JSON done tool call.',
    ].join(' ');
    sendingSessionId = sessionId;
    updateSend();
    try {
      const r = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          project: p.path,
          task,
          input: { kind: 'resume_run', source_run_id: sourceRunId },
          provider: s.provider || liveDefaultProvider(),
          intent: 'project',
          ...window.ProviderUI.runSelection(s, s.provider || liveDefaultProvider()),
        }),
      });
      if (r.status === 409 || !r.ok) {
        await reportSendFailure(sessionId, r);
        return;
      }
      await deps.acceptRunResponse(r, sessionId);
    } catch {
      deps.addSendError(sessionId);
      return;
    } finally {
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
      if (!target || runningSessionId()) return;
      if (target.id === 'ctx-folder') {
        const s = deps.activeSession();
        if (!s || deps.sessionProject(s) || deps.projectPickerBusy()) return;
        deps.attachCurrentChatToPickedProject();
      } else if (target.id === 'ctx-research') {
        toggleResearchForActive();
      }
    };
    $('composer-context').addEventListener('keydown', (e: KeyboardEvent) => {
      if ((e.key !== 'Enter' && e.key !== ' ') || runningSessionId()) return;
      const target = e.target instanceof Element ? e.target.closest('.ctx-token') : null;
      if (!(target instanceof HTMLElement)) return;
      e.preventDefault();
      target.click();
    });
    $('send').onclick = sendActiveDraft;
    $('stop').onclick = () => fetch('/api/stop', { method: 'POST' });
  }

  window.Composer = {
    init,
    syncSession,
    captureDraft,
    isSending: () => !!sendingSessionId,
    retryTask,
    quoteInReply,
    recoveryActions,
    resizeTask,
    updateSend,
    toggleResearchForActive,
    setActiveProvider,
    continueTask,
  };
})();
