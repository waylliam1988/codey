/* Codey SSE runtime: reconnect, state reconciliation, and accepted-run handoff. */
(function () {
  'use strict';

  let deps = null;
  let evtSrc = null;
  let reconnectTimer = null;
  let reconcilePromise = null;
  let bufferedServerEvents = [];

function init(nextDeps) {
  deps = nextDeps;
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
      for (const event of events) deps.handleServerEvent(event);
    }
  })();
  return reconcilePromise;
}

function ingestServerEvent(data) {
  if (reconcilePromise) {
    bufferedServerEvents.push(data);
    return;
  }
  deps.handleServerEvent(data);
}

async function acceptRunResponse(response, sessionId) {
  const data = await response.json();
  const runId = data.run_id || null;
  deps.acceptRun(runId, runId ? sessionId : null);
  if (runId) {
    deps.setStatus('Running', 'run');
    deps.setProviderBusy(true);
    deps.updateSend();
    deps.updateComposerContext();
  }
  await reconcileRunState();
}

function connect() {
  if (evtSrc) return;
  evtSrc = new EventSource('/api/events');
  evtSrc.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    const eventId = Number.parseInt(e.lastEventId || '', 10);
    if (Number.isFinite(eventId) && eventId > 0 && data.event_id == null) data.event_id = eventId;
    if (data.type === 'hello') {
      clearReconnectTimer();
      reconcileRunState();
      deps.refreshProviderStatus();
      return;
    }
    ingestServerEvent(data);
  };
  evtSrc.onerror = scheduleReconnectStatus;
}

window.CodeySse = {
  init,
  connect,
  reconcileRunState,
  acceptRunResponse,
};
})();
