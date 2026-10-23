import { runtime } from './runtime';
import type { SseDeps } from './types/domain';
import type { RunState } from './types/domain';
import { decodeRunState, decodeServerEvent, type ServerEvent } from './domain/protocol';
/* Codey SSE runtime: reconnect, state reconciliation, and accepted-run handoff. */
(function () {
  'use strict';

  let deps: SseDeps;
  let evtSrc: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconcilePromise: Promise<RunState | null> | null = null;
  let bufferedServerEvents: ServerEvent[] = [];
  let bufferedGap = false;
  const BUFFER_LIMIT = 100;
  let lastKnownEventId = 0;
  const COMMIT_CURSOR_KEY = 'codey:durable-cursor';
  const storedCursor = (() => {
    try {
      const value = JSON.parse(localStorage.getItem(COMMIT_CURSOR_KEY) || 'null');
      if (
        value &&
        typeof value.database_id === 'string' &&
        value.database_id &&
        Number.isSafeInteger(value.commit_seq) &&
        value.commit_seq >= 0
      )
        return value;
    } catch {
      // Browser storage is optional; the in-memory cursor remains usable.
    }
    return { database_id: '', commit_seq: 0 };
  })();
  let lastKnownDatabaseId: string = storedCursor.database_id;
  let lastKnownCommitSeq: number = storedCursor.commit_seq;
  let commitGap = false;

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
    if (commitGap || !Number.isSafeInteger(value) || value <= lastKnownCommitSeq) return;
    lastKnownCommitSeq = value;
    saveCommitCursor();
  }

  function handleAndAcknowledge(event: ServerEvent): void {
    deps.handleServerEvent(event);
    rememberCommitCursor(Number(event.commit_seq || String(event.event_id || '').split(':')[1] || 0));
  }

  function init(nextDeps: SseDeps): void {
    deps = nextDeps;
  }

  function displayRunStatus(status: string): void {
    const running = status === 'running';
    const text = status === 'connecting' ? 'Connecting…' : running ? 'Running' : status;
    deps.setStatus(text, running ? 'run' : 'warn');
  }

  function clearReconnectTimer() {
    if (reconnectTimer === null) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function scheduleReconnectStatus() {
    if (reconnectTimer !== null) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (evtSrc && evtSrc.readyState !== EventSource.OPEN) deps.setStatus('Reconnecting...', 'warn');
    }, 5000);
  }

  function reconcileRunState(): Promise<RunState | null> {
    if (reconcilePromise) return reconcilePromise;
    bufferedServerEvents = [];
    bufferedGap = false;
    reconcilePromise = (async () => {
      let snapshot: RunState | null = null;
      try {
        const response = await fetch('/api/state', { cache: 'no-store' });
        if (!response.ok) throw new Error('state unavailable');
        snapshot = decodeRunState(await response.json());
        deps.applyRunState(snapshot);
        clearReconnectTimer();
      } catch {
        scheduleReconnectStatus();
      } finally {
        const events = bufferedServerEvents;
        bufferedServerEvents = [];
        reconcilePromise = null;
        let replayFailed = false;
        if (bufferedGap) {
          commitGap = true;
          bufferedGap = false;
          void reconcileRunState();
        } else {
          for (const event of events) {
            try {
              handleAndAcknowledge(event);
            } catch (error) {
              commitGap = true;
              console.error('SSE event handling failed', error);
              replayFailed = true;
            }
          }
          if (replayFailed) void reconcileRunState();
        }
      }
      return snapshot;
    })();
    return reconcilePromise;
  }

  function ingestServerEvent(data: ServerEvent): void {
    if (reconcilePromise) {
      if (bufferedServerEvents.length >= BUFFER_LIMIT) {
        bufferedGap = true;
        bufferedServerEvents.shift();
      }
      bufferedServerEvents.push(data);
      return;
    }
    try {
      handleAndAcknowledge(data);
    } catch (error) {
      commitGap = true;
      console.error('SSE event handling failed', error);
      void reconcileRunState();
    }
  }

  function eventKey(data: ServerEvent, fallback = ''): string {
    const raw = String((data && data.event_id) || '0');
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
    const data = await response.json();
    const runId = data.run_id || null;
    const request = runtime.Requests.forRun(sessionId, runId);
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

  function connect() {
    if (evtSrc) return;
    const params = new URLSearchParams();
    if (lastKnownEventId > 0) params.set('last_event_id', String(lastKnownEventId));
    if (lastKnownDatabaseId) params.set('database_id', lastKnownDatabaseId);
    if (lastKnownCommitSeq > 0) params.set('after_commit_seq', String(lastKnownCommitSeq));
    const query = params.toString();
    const url = query ? `/api/events?${query}` : '/api/events';
    evtSrc = new EventSource(url);
    evtSrc.onmessage = (e) => {
      let raw: unknown;
      try {
        raw = JSON.parse(e.data) as unknown;
      } catch {
        return;
      }
      let data: ServerEvent;
      try {
        data = decodeServerEvent(raw);
      } catch {
        void reconcileRunState();
        return;
      }
      if (data.type === 'hello') {
        if (lastKnownDatabaseId !== data.database_id) lastKnownEventId = 0;
        lastKnownDatabaseId = data.database_id!;
        lastKnownCommitSeq = data.after_commit_seq!;
        commitGap = false;
        saveCommitCursor();
        clearReconnectTimer();
        void reconcileRunState();
        deps.refreshProviderStatus(true);
        return;
      }
      const rawEventId = String(e.lastEventId || data.event_id || '0');
      const eventId = Number(rawEventId.split(':', 1)[0] || 0);
      const embeddedCommit = Number(rawEventId.split(':', 2)[1] || 0);
      const duplicate =
        Number.isSafeInteger(eventId) &&
        eventId > 0 &&
        eventId <= lastKnownEventId &&
        (!Number.isSafeInteger(embeddedCommit) || embeddedCommit <= lastKnownCommitSeq);
      const cursor = typeof data.cursor === 'number' ? data.cursor : Number(data.cursor);
      if (data.type === 'resync_required' && Number.isSafeInteger(cursor) && cursor >= 0) {
        lastKnownEventId = cursor;
      } else if (duplicate) {
        return;
      } else if (Number.isSafeInteger(eventId) && eventId > 0) {
        lastKnownEventId = Math.max(lastKnownEventId, eventId);
        if (data.event_id == null) data.event_id = rawEventId;
      }
      try {
        ingestServerEvent(data);
      } catch {
        console.error('SSE event handling failed');
        void reconcileRunState();
      }
    };
    evtSrc.onerror = () => {
      scheduleReconnectStatus();
    };
  }

  runtime.Sse = {
    init,
    displayRunStatus,
    connect,
    reconcileRunState,
    acceptRunResponse,
    eventKey,
  };
})();
