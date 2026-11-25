import { requestResponse } from './api/transport';
import type { SseDeps } from './types/domain';
import type { RunState } from './types/domain';
import { decodeRunState, decodeServerEvent, isRecord, UnknownServerEvent, type ServerEvent } from './domain/protocol';
/* Codey SSE runtime: reconnect, state reconciliation, and accepted-run handoff. */
let deps: SseDeps;
let evtSrc: EventSource | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconcilePromise: Promise<RunState | null> | null = null;
interface SseFrame {
  source: EventSource;
  event: ServerEvent;
  eventId: number;
  commit: number;
  cursor: string;
}
let bufferedServerEvents: SseFrame[] = [];
let dispatches = Promise.resolve();
let bufferedGap = false;
const BUFFER_LIMIT = 100;
let lastKnownEventId = 0;
const COMMIT_CURSOR_KEY = 'codey:durable-cursor';
const storedCursor = (() => {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(COMMIT_CURSOR_KEY) || 'null');
    if (
      isRecord(value) &&
      typeof value.database_id === 'string' &&
      value.database_id &&
      typeof value.commit_seq === 'number' &&
      Number.isSafeInteger(value.commit_seq) &&
      value.commit_seq >= 0
    )
      return { database_id: value.database_id, commit_seq: value.commit_seq };
  } catch {
    // Browser storage is optional; the in-memory cursor remains usable.
  }
  return { database_id: '', commit_seq: 0 };
})();
let lastKnownDatabaseId: string = storedCursor.database_id;
let lastKnownCommitSeq: number = storedCursor.commit_seq;

function saveCommitCursor(): void {
  try {
    localStorage.setItem(
      COMMIT_CURSOR_KEY,
      JSON.stringify({ database_id: lastKnownDatabaseId, commit_seq: lastKnownCommitSeq }),
    );
  } catch {
    // A page reload can replay entries again; durable entry identity deduplicates them.
  }
}

function rememberCommitCursor(value: number): void {
  if (value <= lastKnownCommitSeq) return;
  lastKnownCommitSeq = value;
  saveCommitCursor();
}

function handleAndAcknowledge(frame: SseFrame): void {
  dispatches = dispatches.then(async () => {
    const { source, event, eventId, commit, cursor } = frame;
    if (source !== evtSrc) return;
    if (event.type === 'durable_commit' && commit <= lastKnownCommitSeq) return;
    if (
      event.type !== 'durable_commit' &&
      event.type !== 'resync_required' &&
      eventId > 0 &&
      eventId <= lastKnownEventId
    )
      return;
    try {
      await deps.handleServerEvent(event, cursor);
    } catch (error) {
      console.error('SSE event handling failed', error);
      retryStream(source);
      return;
    }
    if (source !== evtSrc) return;
    const reset = event.type === 'resync_required' ? Number(event.cursor) : NaN;
    if (Number.isSafeInteger(reset) && reset >= 0) lastKnownEventId = reset;
    else if (event.type !== 'durable_commit' && eventId > 0) lastKnownEventId = eventId;
    if (event.type === 'durable_commit') rememberCommitCursor(commit);
  });
}

function init(nextDeps: SseDeps): void {
  deps = nextDeps;
}

function displayRunStatus(status: string): void {
  const running = status === 'running';
  const text = status === 'connecting' ? 'Connecting' : running ? 'Running' : status;
  deps.setStatus(text, running ? 'run' : 'warn');
}

function clearReconnectTimer() {
  if (reconnectTimer === null) return;
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
}

function reconcileRunState(): Promise<RunState | null> {
  if (reconcilePromise) return reconcilePromise;
  bufferedServerEvents = [];
  bufferedGap = false;
  reconcilePromise = (async () => {
    let snapshot: RunState | null = null;
    try {
      const response = await requestResponse('/api/state', { cache: 'no-store', signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error('state unavailable');
      snapshot = decodeRunState(await response.json());
      deps.applyRunState(snapshot);
    } catch {
      // Keep the current projection until the next successful snapshot.
    } finally {
      const events = bufferedServerEvents;
      bufferedServerEvents = [];
      reconcilePromise = null;
      if (bufferedGap) {
        bufferedGap = false;
        if (evtSrc) retryStream(evtSrc);
      } else {
        events.forEach(handleAndAcknowledge);
      }
    }
    return snapshot;
  })();
  return reconcilePromise;
}

function ingestServerEvent(frame: SseFrame): void {
  if (reconcilePromise) {
    if (bufferedServerEvents.length >= BUFFER_LIMIT) {
      bufferedGap = true;
      bufferedServerEvents.shift();
    }
    bufferedServerEvents.push(frame);
    return;
  }
  handleAndAcknowledge(frame);
}

function eventKey(cursor: string | number | undefined, fallback = ''): string {
  const raw = String(cursor ?? '0');
  const parts = raw.split(':');
  const eventId = Number(parts[0] || 0);
  const commitSeq = Number(parts[1] || 0);
  if (Number.isFinite(eventId) && eventId > 0 && Number.isFinite(commitSeq) && commitSeq > 0) {
    return `sse:${eventId}:${commitSeq}`;
  }
  if (Number.isFinite(eventId) && eventId > 0) return `sse:${eventId}`;
  if (Number.isFinite(commitSeq) && commitSeq > 0) return `durable:${commitSeq}`;
  return fallback;
}

async function acceptRunResponse(response: Response, sessionId: string): Promise<void> {
  const data: unknown = await response.json();
  if (!isRecord(data) || typeof data.run_id !== 'string' || !data.run_id)
    throw new Error('Invalid run admission response');
  const runId = data.run_id;
  const request = deps.forRun(sessionId, runId);
  if (request && !['sending', 'running'].includes(request.attempt.state)) return;
  deps.acceptRun(runId, runId ? sessionId : null);
  const initialSnapshot = await reconcileRunState();
  if (runId) {
    // Give the worker one event-loop turn to publish admission before
    // treating an absent run as unconfirmed. The second snapshot keeps a
    // normal POST/state publication race from becoming a false failure.
    setTimeout(() => {
      void reconcileRunState().then((snapshot) => {
        const state = snapshot || initialSnapshot;
        deps.applyRunState({
          ...state,
          session_id: state?.session_id || sessionId,
          run_id: state?.run_id || runId,
          busy: state?.busy,
          active_runs: state?.active_runs || [],
          response_ack_run_id: runId,
        });
      });
    }, 0);
  }
}

function retryStream(source: EventSource): void {
  if (evtSrc !== source) return;
  source.close();
  evtSrc = null;
  void reconcileRunState();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void dispatches.then(connect);
  }, 1000);
}

function connect() {
  if (evtSrc || reconnectTimer !== null) return;
  const params = new URLSearchParams();
  if (lastKnownEventId > 0) params.set('last_event_id', String(lastKnownEventId));
  if (lastKnownDatabaseId) params.set('database_id', lastKnownDatabaseId);
  if (lastKnownCommitSeq > 0) params.set('after_commit_seq', String(lastKnownCommitSeq));
  const query = params.toString();
  const url = query ? `/api/events?${query}` : '/api/events';
  const source = new EventSource(url);
  evtSrc = source;
  source.onmessage = (e) => {
    if (source !== evtSrc) return;
    let raw: unknown;
    try {
      raw = JSON.parse(e.data);
    } catch {
      void reconcileRunState();
      return;
    }
    let data: ServerEvent;
    try {
      data = decodeServerEvent(raw);
    } catch (error) {
      if (!(error instanceof UnknownServerEvent)) void reconcileRunState();
      return;
    }
    if (data.type === 'hello') {
      if (lastKnownDatabaseId !== data.database_id) lastKnownEventId = 0;
      lastKnownDatabaseId = data.database_id;
      lastKnownCommitSeq = data.after_commit_seq;
      saveCommitCursor();
      clearReconnectTimer();
      void reconcileRunState();
      deps.refreshProviderStatus(true);
      return;
    }
    const rawEventId = e.lastEventId || '0';
    const eventId = Number(rawEventId.split(':', 1)[0] || 0);
    ingestServerEvent({
      source,
      event: data,
      eventId: Number.isSafeInteger(eventId) ? eventId : 0,
      commit: data.type === 'durable_commit' ? data.commit_seq : 0,
      cursor: rawEventId,
    });
  };
  source.onerror = () => {
    if (source !== evtSrc) return;
    deps.setStatus('Reconnecting...', 'warn');
    retryStream(source);
  };
}

export const Sse = {
  init,
  displayRunStatus,
  connect,
  reconcileRunState,
  acceptRunResponse,
  eventKey,
};
