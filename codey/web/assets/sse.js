/* Codey SSE runtime: reconnect, state reconciliation, and accepted-run handoff. */
(function () {
  'use strict';

  let deps = null;
  let evtSrc = null;
  let reconnectTimer = null;
  let reconcilePromise = null;
  let bufferedServerEvents = [];
  let bufferedGap = false;
  const BUFFER_LIMIT = 100;
  let lastKnownEventId = 0;

function init(nextDeps) {
  deps = nextDeps;
}

function displayRunStatus(status) {
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

function reconcileRunState() {
  if (reconcilePromise) return reconcilePromise;
  bufferedServerEvents = [];
  bufferedGap = false;
  reconcilePromise = (async () => {
    try {
      const response = await fetch('/api/state', { cache: 'no-store' });
      if (!response.ok) throw new Error('state unavailable');
      deps.applyRunState(await response.json());
      clearReconnectTimer();
    } catch {
      scheduleReconnectStatus();
    } finally {
      const events = bufferedServerEvents;
      bufferedServerEvents = [];
      reconcilePromise = null;
      if (bufferedGap) {
        bufferedGap = false;
        reconcileRunState();
      } else {
        for (const event of events) deps.handleServerEvent(event);
      }
    }
  })();
  return reconcilePromise;
}

function ingestServerEvent(data) {
  if (reconcilePromise) {
    if (bufferedServerEvents.length >= BUFFER_LIMIT) {
      bufferedGap = true;
      bufferedServerEvents.shift();
    }
    bufferedServerEvents.push(data);
    return;
  }
  deps.handleServerEvent(data);
}

function eventKey(data, fallback = '') {
  const eventId = Number((data && data.event_id) || 0);
  return Number.isFinite(eventId) && eventId > 0 ? `sse:${eventId}` : fallback;
}

async function acceptRunResponse(response, sessionId) {
  const data = await response.json();
  const runId = data.run_id || null;
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
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    const eventId = Number(e.lastEventId || data.event_id || 0);
    if (data.type === 'resync_required' && Number.isSafeInteger(data.cursor) && data.cursor >= 0) {
      lastKnownEventId = data.cursor;
    } else if (Number.isSafeInteger(eventId) && eventId > 0) {
      if (eventId <= lastKnownEventId) return;
      lastKnownEventId = eventId;
      if (data.event_id == null) data.event_id = eventId;
    }
    if (data.type === 'hello') {
      clearReconnectTimer();
      reconcileRunState();
      deps.refreshProviderStatus();
      return;
    }
    ingestServerEvent(data);
  };
  evtSrc.onerror = () => {
    scheduleReconnectStatus();
  };
}

window.CodeySse = {
  init,
  displayRunStatus,
  connect,
  reconcileRunState,
  acceptRunResponse,
  eventKey,
};
})();
