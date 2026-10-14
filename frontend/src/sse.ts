import { runtime } from './runtime';
import type { SseDeps } from './types/domain';
import { decodeRunState, decodeServerEvent, type ServerEvent } from './domain/protocol';
/* Codey SSE runtime: reconnect, state reconciliation, and accepted-run handoff. */
(function () {
  'use strict';

  let deps: SseDeps;
  let evtSrc: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconcilePromise: Promise<void> | null = null;
  let bufferedServerEvents: ServerEvent[] = [];
  let bufferedGap = false;
  const BUFFER_LIMIT = 100;
  let lastKnownEventId = 0;

  function init(nextDeps: SseDeps): void {
    deps = nextDeps;
  }

  function displayRunStatus(status: string): void {
    const running = status === 'running';
    const text = status === 'connecting' ? 'Connecting to browser…' : running ? 'Running' : status;
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

  function reconcileRunState(): Promise<void> {
    if (reconcilePromise) return reconcilePromise;
    bufferedServerEvents = [];
    bufferedGap = false;
    reconcilePromise = (async () => {
      try {
        const response = await fetch('/api/state', { cache: 'no-store' });
        if (!response.ok) throw new Error('state unavailable');
        deps.applyRunState(decodeRunState(await response.json()));
        clearReconnectTimer();
      } catch {
        scheduleReconnectStatus();
      } finally {
        const events = bufferedServerEvents;
        bufferedServerEvents = [];
        reconcilePromise = null;
        let replayFailed = false;
        if (bufferedGap) {
          bufferedGap = false;
          reconcileRunState();
        } else {
          for (const event of events) {
            try {
              deps.handleServerEvent(event);
            } catch (error) {
              console.error('SSE event handling failed', error);
              replayFailed = true;
            }
          }
          if (replayFailed) void reconcileRunState();
        }
      }
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
      deps.handleServerEvent(data);
    } catch (error) {
      console.error('SSE event handling failed', error);
      void reconcileRunState();
    }
  }

  function eventKey(data: ServerEvent, fallback = ''): string {
    const eventId = Number((data && data.event_id) || 0);
    return Number.isFinite(eventId) && eventId > 0 ? `sse:${eventId}` : fallback;
  }

  async function acceptRunResponse(response: Response, sessionId: string): Promise<void> {
    const data = await response.json();
    const runId = data.run_id || null;
    const request = runtime.Requests.forRun(sessionId, runId);
    if (request && !['sending', 'running'].includes(request.attempt.state)) return;
    runtime.Requests.running(sessionId, runId);
    deps.acceptRun(runId, runId ? sessionId : null);
    if (runId) {
      displayRunStatus('running');
      deps.setProviderBusy(true);
      deps.updateSend();
      deps.updateComposerContext();
    }
    await reconcileRunState();
  }

  function connect() {
    if (evtSrc) return;
    const url = lastKnownEventId > 0 ? `/api/events?last_event_id=${lastKnownEventId}` : '/api/events';
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
      const eventId = Number(e.lastEventId || data.event_id || 0);
      const cursor = typeof data.cursor === 'number' ? data.cursor : Number(data.cursor);
      if (data.type === 'resync_required' && Number.isSafeInteger(cursor) && cursor >= 0) {
        lastKnownEventId = cursor;
      } else if (Number.isSafeInteger(eventId) && eventId > 0) {
        if (eventId <= lastKnownEventId) return;
        lastKnownEventId = eventId;
        if (data.event_id == null) data.event_id = eventId;
      }
      if (data.type === 'hello') {
        clearReconnectTimer();
        reconcileRunState();
        deps.refreshProviderStatus(true);
        return;
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
