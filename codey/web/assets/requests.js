/* A user request owns its attempts and one stable, derived status position. */
(function () {
'use strict';
let deps = null;
const pending = attempt => ['sending', 'running'].includes(attempt.state);
const latest = request => request.attempts[request.attempts.length - 1];
function init(nextDeps) { deps = nextDeps; }
function session(id) { return deps.getSessions().find(s => s.id === id); }
function requestIn(s, id) { return s?.messages.find(m => m.type === 'user' && m.id === id && m.attempts); }
function forRun(sessionId, runId) {
  if (!runId) return null;
  const s = session(sessionId);
  const request = s?.messages.find(m => m.type === 'user' && m.attempts?.some(a => a.runId === runId));
  return request ? {s, request, attempt:request.attempts.find(a => a.runId === runId)} : null;
}
function busy() { return !!deps.getRunningSessionId() || window.CodeyComposer.isSending(); }
function canRetry(sessionId, requestId) {
  const s = session(sessionId), request = requestIn(s, requestId);
  return !!request && !busy() && latest(request).state === 'failed'
    && s.messages.filter(m => m.type === 'user').at(-1) === request;
}
function statusNode(requestId) {
  return Array.from(deps.$('chat').querySelectorAll('.request-status')).find(n => n.dataset.requestId === requestId);
}
function changed(s, request, attempts) {
  const next = {...request, attempts};
  s.messages[s.messages.indexOf(request)] = next;
  deps.persistActiveNow();
  if (s.id === deps.getActiveId()) {
    const node = statusNode(request.id);
    if (node) paint(node, s.id, next);
  }
  return next;
}
function update(sessionId, runId, fields) {
  const match = forRun(sessionId, runId);
  if (!match || latest(match.request) !== match.attempt) return false;
  const attempts = match.request.attempts.map(a => a === match.attempt ? {...a, ...fields} : a);
  changed(match.s, match.request, attempts);
  return true;
}
function begin(sessionId, text, requestId = '') {
  const s = session(sessionId);
  if (!s) return null;
  let request = requestId ? requestIn(s, requestId) : null;
  if (requestId && (!request || latest(request).state !== 'failed'
      || s.messages.filter(m => m.type === 'user').at(-1) !== request)) return null;
  const attempt = {runId:'run_' + crypto.randomUUID().replaceAll('-', ''), state:'sending'};
  if (request) {
    request = changed(s, request, [...request.attempts, attempt]);
  } else {
    request = {type:'user', id:'message_' + crypto.randomUUID(), text, attempts:[attempt]};
    deps.pushMsgToSession(sessionId, request);
    deps.addToSession(sessionId, {type:'request_status', sessionId, requestId:request.id});
  }
  refreshActions();
  return {request, attempt};
}
function running(sessionId, runId) {
  const match = forRun(sessionId, runId);
  if (!match || !pending(match.attempt)) return false;
  return update(sessionId, runId, {state:'running'});
}
function fail(sessionId, runId, error, fields = {}) {
  return update(sessionId, runId, {state:'failed', error, ...fields});
}
function finish(data) {
  const match = forRun(data.session_id, data.run_id);
  if (!match) return false;
  const summary = String(data.summary || '');
  const failed = data.stop_reason === 'error' || summary.startsWith('ERROR:');
  update(data.session_id, data.run_id, failed
    ? {state:'failed', error:summary.replace(/^ERROR:\s*/i, '') || 'Task failed'}
    : {state:data.stop_reason === 'done' || !data.stop_reason ? 'done' : 'paused'});
  return failed;
}
function warning(sessionId, runId) { return update(sessionId, runId, {warning:'Local update paused'}); }
function acceptsEvent(data) {
  const match = forRun(data.session_id, data.run_id);
  if (!match) return true;
  // Approvals and their settlements remain real records after a run ends.
  if (['shell_request', 'shell_result', 'teach_request'].includes(data.type)) return true;
  if (latest(match.request) !== match.attempt) return false;
  // Terminal attempts cannot be revived by delayed start/progress messages.
  return pending(match.attempt) || ['task_done', 'reply', 'review', 'tool', 'ghost_post_turn_warning'].includes(data.type);
}
function reconcile(data) {
  for (const s of deps.getSessions()) {
    for (const request of s.messages.filter(m => m.type === 'user' && m.attempts?.length)) {
      const attempt = latest(request);
      if (!pending(attempt)) continue;
      if (data.busy && data.run_id === attempt.runId && data.session_id === s.id) running(s.id, attempt.runId);
      // An in-flight POST has not been admitted yet; an earlier state snapshot cannot settle it.
      else if (attempt.state !== 'sending' || !window.CodeyComposer.isSending()) fail(s.id, attempt.runId, 'Response was not confirmed');
    }
  }
  refreshActions();
}
function readable(error) {
  if (/overloaded/i.test(error)) return 'Model temporarily overloaded';
  if (/free tier.*(?:within|in) OpenCode/i.test(error)) return 'Free tier only available in OpenCode';
  return error;
}
function details(panel, request) {
  panel.replaceChildren();
  const title = document.createElement('div'); title.className = 'run-details-title'; title.textContent = 'Run details';
  panel.append(title);
  request.attempts.forEach((attempt, i) => {
    const row = document.createElement('div'); row.className = 'run-details-row';
    const label = document.createElement('div'); label.className = 'run-details-label'; label.textContent = 'Attempt ' + (i + 1);
    const value = document.createElement('div'); value.className = 'run-details-value';
    value.textContent = [attempt.error || ({sending:'Sending', running:'Running', done:'Completed', paused:'Paused'}[attempt.state]), attempt.warning].filter(Boolean).join(' · ');
    row.append(label, value); panel.append(row);
  });
}
function paint(node, sessionId, request) {
  const attempt = latest(request);
  const focused = node.contains(document.activeElement);
  const focusWasStatus = document.activeElement === node;
  node.hidden = !pending(attempt) && attempt.state !== 'failed';
  node.replaceChildren();
  if (node.hidden) {
    if (focused && sessionId === deps.getActiveId()) deps.$('task').focus();
    return;
  }
  const failed = attempt.state === 'failed';
  const actions = [];
  if (failed) {
    actions.push({label:'Details', onclick:event => {
      node._detailsOpen = !node._detailsOpen;
      const panel = node.querySelector('.retry-details'); panel.hidden = !node._detailsOpen;
      event.currentTarget.setAttribute('aria-expanded', String(node._detailsOpen));
    }});
    const recovery = window.CodeyComposer.recoveryActions({...attempt, sessionId});
    actions.push(...(recovery || []));
    actions.push({label:'Retry', disabled:!canRetry(sessionId, request.id),
      onclick:() => window.CodeyComposer.retryTask(sessionId, request.id)});
  }
  const text = failed ? readable(attempt.error) : request.attempts.length > 1
    ? 'Retrying · Waiting for reply' : attempt.state === 'sending' ? 'Sending…' : 'Working · Waiting for reply';
  const row = deps.statusRow(failed ? 'ERROR' : '', text, {err:failed, actions}).firstElementChild;
  if (!failed) { const spinner = document.createElement('span'); spinner.className = 'spinner'; spinner.setAttribute('aria-hidden', 'true'); row.firstElementChild.replaceWith(spinner); }
  node.append(row);
  if (failed) {
    const panel = document.createElement('div'); panel.className = 'run-details retry-details'; panel.hidden = !node._detailsOpen;
    details(panel, request); node.append(panel);
    row.querySelector('button').setAttribute('aria-expanded', String(!!node._detailsOpen));
  }
  if (focused) {
    if (failed && focusWasStatus) row.querySelector('button:last-child').focus();
    else node.focus();
  }
}
function appendStatus(chat, message) {
  const request = requestIn(session(message.sessionId), message.requestId);
  if (!request) return;
  const node = document.createElement('div'); node.className = 'msg request-status';
  node.dataset.requestId = request.id; node.tabIndex = -1;
  node.setAttribute('role', 'status'); node.setAttribute('aria-live', 'polite');
  paint(node, message.sessionId, request); chat.append(node);
}
function refreshActions() {
  if (!deps) return;
  const s = session(deps.getActiveId());
  for (const node of deps.$('chat').querySelectorAll('.request-status')) {
    const request = requestIn(s, node.dataset.requestId);
    if (!request) continue;
    for (const button of node.querySelectorAll('.status-row button')) {
      if (button.textContent === 'Retry') button.disabled = !canRetry(s.id, request.id);
      if (button.textContent === 'Choose model') button.disabled = busy();
    }
  }
}
function addToSession(sid, m) {
  const s = session(sid);
  if (!s) return false;
  window.CodeyUiState.ensureSessionIndexes(s);
  if (m.eventKey && s._eventKeys.has(m.eventKey)) return false;
  if (['limit', 'pause', 'teach', 'err'].includes(m.type) && !m.sessionId) m.sessionId = sid;
  const match = forRun(sid, m.runId);
  const marker = match && s.messages.findIndex(item => item.type === 'request_status' && item.requestId === match.request.id);
  if (match && marker >= 0) s.messages.splice(marker, 0, m); else s.messages.push(m);
  window.CodeyUiState.trackSessionMessage(s, m);
  deps.persistActive();
  if (sid === deps.getActiveId()) {
    const follow = window.CodeyConversationUI.isFollowing();
    if (s.messages.length === 1) deps.renderChat();
    else {
      const chat = deps.$('chat'), before = new Set(chat.children);
      deps.appendMessageNode(chat, m);
      const anchor = match && statusNode(match.request.id);
      if (anchor) for (const node of Array.from(chat.children)) if (!before.has(node) && node !== anchor) chat.insertBefore(node, anchor);
      deps.scrollChat(follow);
    }
  }
  refreshActions();
  window.CodeyConversationUI.updateNotice();
  return true;
}
window.CodeyRequests = {init, begin, canRetry, forRun, running, fail, finish, warning,
  acceptsEvent, reconcile, appendStatus, refreshActions, addToSession};
})();
