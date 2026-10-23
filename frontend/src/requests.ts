import { runtime } from './runtime';
import type { Message, Session } from './types/domain';
import { isRunSuspension, isTerminalRun } from './run_lifecycle';
export interface RequestAttempt {
  edit?: boolean;
  runId: string;
  state: import('./types/domain').AttemptState;
  error?: string;
  warning?: string;
  sendFailure?: string;
  runOwner?: string;
}
export interface RequestMatch {
  s: Session;
  request: Message & { id: string; text: string; attempts: RequestAttempt[] };
  attempt: RequestAttempt;
}
export interface RequestEventData {
  session_id: string;
  run_id: string;
  type?: string;
  summary?: string;
  stop_reason?: string;
  busy?: boolean;
  status?: string;
  text?: string;
  error?: string;
  active_runs?: Array<{ run_id: string; session_id: string }>;
  response_ack_run_id?: string;
}
export interface RequestsApi {
  projectLifecycle(data: RequestEventData, state: string): boolean;
  init(deps: RequestsDeps): void;
  begin(
    sessionId: string,
    text: string,
    requestId?: string,
    editEntryId?: string,
  ): { request: Message; attempt: RequestAttempt } | null;
  canRetry(sessionId: string, requestId: string): boolean;
  forRun(sessionId: string, runId: string): RequestMatch | null;
  running(sessionId: string, runId: string): boolean;
  fail(sessionId: string, runId: string, error: string, fields?: Partial<RequestAttempt>): boolean;
  finish(data: RequestEventData): boolean;
  warning(sessionId: string, runId: string): boolean;
  acceptsEvent(data: RequestEventData): boolean;
  reconcile(data: RequestEventData): void;
  appendStatus(chat: HTMLElement, message: Message): void;
  refreshActions(): void;
  addToSession(sessionId: string, message: Message): boolean;
}
interface StatusAction {
  label: string;
  disabled?: boolean;
  onclick: (event?: Event) => unknown;
}
export interface RequestsDeps {
  $(id: string): HTMLElement;
  getSessions(): Session[];
  getActiveId(): string;
  getRunningSessionId(): string | null;
  persistActive(): void;
  persistActiveNow(): void;
  pushMsgToSession(sessionId: string, message: Message): void;
  addToSession(sessionId: string, message: Message): boolean;
  appendMessageNode(chat: HTMLElement, message: Message): void;
  renderChat(): void;
  scrollChat(capture?: boolean): void;
  statusRow(prefix: string, text: string, options: { err?: boolean; actions?: StatusAction[] }): HTMLElement;
}
/* A user request owns its attempts and one stable, derived status position. */
(function () {
  'use strict';
  interface Attempt extends RequestAttempt {}
  interface RequestMessage extends Message {
    id: string;
    text: string;
    attempts: Attempt[];
  }
  type EventData = RequestEventData;
  type StatusNode = HTMLDivElement & { _detailsOpen?: boolean };
  let deps: RequestsDeps = {} as RequestsDeps;
  const pending = (attempt: Attempt): boolean => ['sending', 'running', 'waiting'].includes(attempt.state);
  const latest = (request: RequestMessage): Attempt => request.attempts[request.attempts.length - 1];
  function init(nextDeps: RequestsDeps): void {
    deps = nextDeps;
  }
  function session(id: string): Session | undefined {
    return deps.getSessions().find((s: Session) => s.id === id);
  }
  function requestIn(s: Session | undefined, id: string): RequestMessage | undefined {
    return s?.messages.find(
      (m: Message): m is RequestMessage =>
        m.type === 'user' && typeof m.id === 'string' && m.id === id && Array.isArray(m.attempts),
    ) as RequestMessage | undefined;
  }
  function forRun(sessionId: string, runId: string) {
    if (!runId) return null;
    const s = session(sessionId);
    const request = s?.messages.find(
      (m: Message): m is RequestMessage =>
        m.type === 'user' && Array.isArray(m.attempts) && m.attempts.some((a: Attempt) => a.runId === runId),
    ) as RequestMessage | undefined;
    const attempt = request?.attempts.find((a: Attempt) => a.runId === runId);
    return s && request && attempt ? { s, request, attempt } : null;
  }
  function busy() {
    return !!deps.getRunningSessionId() || runtime.Composer.isSending();
  }
  function canRetry(sessionId: string, requestId: string): boolean {
    const s = session(sessionId),
      request = requestIn(s, requestId);
    return (
      !!s &&
      !!request &&
      !request.queuedInputId &&
      !latest(request).edit &&
      runtime.Models.allows(
        s.provider || '',
        runtime.ProviderUI.runSelection(s, s.provider || '').model_selection?.model,
      ) &&
      !busy() &&
      latest(request).state === 'failed' &&
      latest(request).sendFailure !== 'admission_unconfirmed' &&
      s.messages.filter((m: Message) => m.type === 'user').at(-1) === request
    );
  }
  function statusNode(requestId: string): StatusNode | undefined {
    return Array.from((deps.$('chat') as HTMLElement).querySelectorAll<HTMLElement>('.request-status')).find(
      (n) => n.dataset.requestId === requestId,
    ) as StatusNode | undefined;
  }
  function changed(s: Session, request: RequestMessage, attempts: Attempt[]): RequestMessage {
    const next = { ...request, attempts };
    s.messages[s.messages.indexOf(request)] = next;
    deps.persistActiveNow();
    if (s.id === deps.getActiveId()) {
      const node = statusNode(request.id);
      if (node) paint(node, s.id, next);
    }
    return next;
  }
  function update(sessionId: string, runId: string, fields: Partial<Attempt>): boolean {
    const match = forRun(sessionId, runId);
    if (!match || latest(match.request) !== match.attempt) return false;
    const attempts = match.request.attempts.map((a) => (a === match.attempt ? { ...a, ...fields } : a));
    changed(match.s, match.request, attempts);
    return true;
  }
  function begin(sessionId: string, text: string, requestId = '', editEntryId = '') {
    const s = session(sessionId);
    if (!s) return null;
    let request = requestId ? requestIn(s, requestId) : null;
    if (editEntryId) {
      const source = s.messages.find((message) => message.type === 'user' && message.entryId === editEntryId);
      if (!source?.id) return null;
      request = { ...source, id: source.id, text: String(source.text || ''), attempts: source.attempts || [] };
      s.messages[s.messages.indexOf(source)] = request;
    }
    if (
      requestId &&
      (!request || latest(request).state !== 'failed' || s.messages.filter((m) => m.type === 'user').at(-1) !== request)
    )
      return null;
    const attempt: Attempt = {
      runId: 'run_' + crypto.randomUUID().replaceAll('-', ''),
      state: 'sending',
      ...(editEntryId ? { edit: true } : {}),
    };
    if (request) {
      request = changed(s, request, [...request.attempts, attempt]);
    } else {
      request = { type: 'user', id: 'message_' + crypto.randomUUID(), text, attempts: [attempt] };
      deps.pushMsgToSession(sessionId, request);
      deps.addToSession(sessionId, { type: 'request_status', sessionId, requestId: request.id });
    }
    refreshActions();
    return { request, attempt };
  }
  function running(sessionId: string, runId: string): boolean {
    const match = forRun(sessionId, runId);
    if (!match || !pending(match.attempt)) return false;
    return update(sessionId, runId, { state: 'running' });
  }
  function fail(sessionId: string, runId: string, error: string, fields: Partial<Attempt> = {}): boolean {
    return update(sessionId, runId, { state: 'failed', error, ...fields });
  }
  function finish(data: EventData, fields: Partial<Attempt> = {}): boolean {
    const match = forRun(data.session_id, data.run_id);
    if (!match) return false;
    const summary = String(data.summary || '');
    const failed = data.stop_reason === 'error' || summary.startsWith('ERROR:');
    update(data.session_id, data.run_id, {
      ...fields,
      ...(failed
        ? { state: 'failed', error: summary.replace(/^ERROR:\s*/i, '') || 'Task failed' }
        : {
            state: isRunSuspension(data.stop_reason)
              ? 'waiting'
              : data.stop_reason === 'done' || !data.stop_reason
                ? 'done'
                : 'paused',
          }),
    });
    return failed;
  }
  function projectLifecycle(data: EventData, state: string): boolean {
    if (state === 'idle') return false;
    const attempt = forRun(data.session_id, data.run_id)?.attempt;
    const fields =
      attempt?.sendFailure === 'admission_unconfirmed'
        ? { sendFailure: undefined, error: undefined, runOwner: undefined }
        : {};
    if (state === 'running') return update(data.session_id, data.run_id, { ...fields, state: 'running' });
    return finish({ ...data, stop_reason: state }, fields);
  }
  function warning(sessionId: string, runId: string): boolean {
    return update(sessionId, runId, { warning: 'Local memory update unavailable' });
  }
  function acceptsEvent(data: EventData): boolean {
    // History restores durable user entries without a browser attempt. The
    // session's settled run identities still forbid replay from reviving work.
    if (data.type === 'task_start' && isTerminalRun(session(data.session_id), data.run_id)) return false;
    const match = forRun(data.session_id, data.run_id);
    if (!match) return true;
    // Approvals and their settlements remain real records after a run ends.
    if (['shell_request', 'shell_result', 'teach_request'].includes(data.type ?? '')) return true;
    if (latest(match.request) !== match.attempt) return false;
    // Terminal attempts cannot be revived by delayed start/progress messages.
    return (
      pending(match.attempt) ||
      ['task_done', 'reply', 'review', 'tool', 'ghost_post_turn_warning'].includes(data.type ?? '')
    );
  }
  function reconcile(data: EventData): void {
    const active = new Set((data.active_runs || []).map((run) => `${run.session_id}\u0000${run.run_id}`));
    if (data.run_id && data.session_id && data.busy) active.add(`${data.session_id}\u0000${data.run_id}`);
    for (const s of deps.getSessions()) {
      for (const request of s.messages.filter(
        (m: Message): m is RequestMessage => m.type === 'user' && Array.isArray(m.attempts) && m.attempts.length > 0,
      )) {
        const attempt = latest(request);
        if (active.has(`${s.id}\u0000${attempt.runId}`)) continue;
        if (!pending(attempt)) continue;
        // An in-flight POST has not been admitted yet. A snapshot for another
        // session is not evidence of failure, and a delayed response may still
        // be followed by task_start on SSE. Only explicit HTTP failure or the
        // matching terminal event may settle a sending attempt.
        if (
          attempt.state !== 'sending' &&
          attempt.state !== 'waiting' &&
          (!runtime.Composer.isSending(s.id) || attempt.runId === data.response_ack_run_id)
        )
          fail(s.id, attempt.runId, 'Response was not confirmed');
      }
    }
    refreshActions();
  }
  function readable(error: unknown): string {
    const text = String(error ?? '');
    if (/overloaded/i.test(text)) return 'Model temporarily overloaded';
    return text;
  }
  function details(panel: HTMLElement, request: RequestMessage): void {
    panel.replaceChildren();
    const title = document.createElement('div');
    title.className = 'run-details-title';
    title.textContent = 'Run details';
    panel.append(title);
    request.attempts.forEach((attempt, i) => {
      const row = document.createElement('div');
      row.className = 'run-details-row';
      const label = document.createElement('div');
      label.className = 'run-details-label';
      label.textContent = 'Attempt ' + (i + 1);
      const value = document.createElement('div');
      value.className = 'run-details-value';
      value.textContent = [
        attempt.error ||
          {
            sending: 'Sending',
            running: 'Running',
            waiting: 'Waiting',
            done: 'Completed',
            paused: 'Paused',
            failed: 'Failed',
          }[attempt.state],
        attempt.warning,
      ]
        .filter(Boolean)
        .join(' · ');
      row.append(label, value);
      panel.append(row);
    });
  }
  function paint(node: StatusNode, sessionId: string, request: RequestMessage): void {
    const attempt = latest(request);
    runtime.Process.append(
      deps.$('chat'),
      { type: 'request_activity', runId: attempt.runId, state: attempt.state },
      () => {},
    );
    const focused = node.contains(document.activeElement);
    const focusWasStatus = document.activeElement === node;
    node.hidden = attempt.state !== 'failed' || !!request.queuedInputId;
    node.replaceChildren();
    if (node.hidden) {
      if (focused && sessionId === deps.getActiveId()) deps.$('task').focus();
      return;
    }
    const failed = attempt.state === 'failed';
    const actions: StatusAction[] = [];
    if (failed && attempt.sendFailure === 'admission_unconfirmed')
      actions.push({ label: 'Check status', onclick: () => runtime.Sse.reconcileRunState() });
    if (failed) {
      actions.push({
        label: 'Details',
        onclick: (event?: Event) => {
          node._detailsOpen = !node._detailsOpen;
          const panel = node.querySelector<HTMLElement>('.retry-details');
          if (panel) panel.hidden = !node._detailsOpen;
          if (event?.currentTarget instanceof Element)
            event.currentTarget.setAttribute('aria-expanded', String(node._detailsOpen));
        },
      });
      const recovery = runtime.Composer.recoveryActions({
        type: 'err',
        sessionId,
        runId: attempt.runId,
        text: attempt.error ?? '',
        sendFailure: attempt.sendFailure,
        runOwner: attempt.runOwner,
      });
      actions.push(
        ...recovery.filter(
          (item: unknown): item is StatusAction =>
            typeof (item as Partial<StatusAction>).label === 'string' &&
            typeof (item as Partial<StatusAction>).onclick === 'function',
        ),
      );
      actions.push({
        label: 'Retry',
        disabled: !canRetry(sessionId, request.id),
        onclick: () => runtime.Composer.retryTask(sessionId, request.id),
      });
    }
    const text = readable(attempt.error);
    const row = deps.statusRow(failed ? 'ERROR' : '', text, { err: failed, actions })
      .firstElementChild as HTMLElement | null;
    if (!row) return;
    node.append(row);
    if (failed) {
      const panel = document.createElement('div');
      panel.className = 'run-details retry-details';
      panel.hidden = !node._detailsOpen;
      details(panel, request);
      node.append(panel);
      row.querySelector('button')?.setAttribute('aria-expanded', String(!!node._detailsOpen));
    }
    if (focused) {
      if (failed && focusWasStatus) row.querySelector<HTMLButtonElement>('button:last-child')?.focus();
      else node.focus();
    }
  }
  function appendStatus(chat: HTMLElement, message: Message): void {
    if (typeof message.sessionId !== 'string' || typeof message.requestId !== 'string') return;
    const request = requestIn(session(message.sessionId), message.requestId);
    if (!request) return;
    const node = document.createElement('div');
    node.className = 'msg request-status';
    node.dataset.requestId = request.id;
    node.tabIndex = -1;
    node.setAttribute('role', 'status');
    node.setAttribute('aria-live', 'polite');
    paint(node, message.sessionId, request);
    chat.append(node);
  }
  function refreshActions() {
    if (!deps) return;
    const s = session(deps.getActiveId());
    if (!s) return;
    for (const node of (deps.$('chat') as HTMLElement).querySelectorAll<HTMLElement>('.request-status')) {
      const request = node.dataset.requestId ? requestIn(s, node.dataset.requestId) : undefined;
      if (!request) continue;
      for (const button of node.querySelectorAll<HTMLButtonElement>('.status-row button')) {
        if (button.textContent === 'Retry') button.disabled = !canRetry(s.id, request.id);
        if (button.textContent === 'Choose model') button.disabled = busy();
      }
    }
  }
  function addToSession(sid: string, m: Message): boolean {
    const s = session(sid);
    if (!s) return false;
    runtime.UiState.ensureSessionIndexes(s);
    if (m.eventKey && s._eventKeys?.has(String(m.eventKey))) return false;
    if (['limit', 'pause', 'teach', 'err'].includes(m.type) && !m.sessionId) m.sessionId = sid;
    const match = forRun(sid, m.runId || '');
    const marker = match
      ? s.messages.findIndex((item) => item.type === 'request_status' && item.requestId === match.request.id)
      : -1;
    if (match && marker >= 0) s.messages.splice(marker, 0, m);
    else s.messages.push(m);
    runtime.UiState.trackSessionMessage(s, m);
    deps.persistActive();
    if (sid === deps.getActiveId()) {
      if (s.messages.length === 1) deps.renderChat();
      else {
        const chat = deps.$('chat'),
          before = new Set(chat.children);
        deps.appendMessageNode(chat, m);
        const anchor = match && statusNode(match.request.id);
        if (anchor)
          for (const node of Array.from(chat.children))
            if (!before.has(node) && node !== anchor) chat.insertBefore(node, anchor);
        deps.scrollChat(false);
      }
    }
    refreshActions();
    runtime.ConversationUI.updateNotice();
    return true;
  }
  runtime.Requests = {
    projectLifecycle,
    init,
    begin,
    canRetry,
    forRun,
    running,
    fail,
    finish,
    warning,
    acceptsEvent,
    reconcile,
    appendStatus,
    refreshActions,
    addToSession,
  };
})();
