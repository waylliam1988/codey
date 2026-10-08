/* Codey composer runtime: task input, send/stop actions, provider selection,
   and project/research context chips. */
(function () {
'use strict';

let deps = null;
let PROVIDERS = [];
let handlersBound = false;
let draftSession = null;
let sendingSessionId = '';

function liveDefaultProvider() {
  return window.CodeyUiState.DEFAULT_PROVIDER;
}

function $(id) { return deps.$(id); }
function runningSessionId() { return deps.getRunningSessionId(); }
function activeId() { return deps.getActiveId(); }
function currentProviderId() { return deps.currentProviderId(); }

function init(nextDeps) {
  deps = nextDeps;
  PROVIDERS = deps.PROVIDERS;
  bindHandlers();
  syncSession();
}

function captureDraft() {
  if (!draftSession || deps.findSession(draftSession.id) !== draftSession) return;
  const t = $('task');
  const previous = draftSession.draft;
  const next = {text:t.value, start:t.selectionStart, end:t.selectionEnd, direction:t.selectionStart === t.selectionEnd ? 'none' : t.selectionDirection,
    revision:previous.revision + (previous.text === t.value ? 0 : 1)};
  if (Object.keys(next).every(key => next[key] === previous[key])) return;
  draftSession.draft = next;
  deps.persistActive();
}

function syncSession() {
  if (!deps || draftSession === deps.activeSession()) return;
  captureDraft();
  draftSession = deps.activeSession();
  if (!draftSession) return;
  const draft = draftSession.draft;
  if ($('task').value !== draft.text) $('task').value = draft.text;
  $('task').setSelectionRange(draft.start, draft.end, draft.direction);
  resizeTask();
  updateSend();
}

function resizeTask() {
  const t = $('task');
  t.style.height = 'auto';
  t.style.height = Math.min(220, Math.max(40, t.scrollHeight)) + 'px';
}

function updateSend() {
  if (!deps) return;
  const has = $('task').value.trim();
  const selection = window.CodeyProviderUI.runSelection(deps.activeSession(), currentProviderId()).model_selection;
  const reason = window.CodeyModels.reason(currentProviderId(), selection?.model);
  const notice = $('model-notice'); notice.hidden = !reason; notice.replaceChildren();
  if (reason) {
    notice.append(window.CodeyModels.hasAny() ? 'This model is ' + reason.toLowerCase() + '. Choose another model or enable it in ' : 'No models enabled. Choose models in ');
    const action = document.createElement('button'); action.className = 'link-btn'; action.textContent = 'Settings';
    action.onclick = () => window.CodeySettings.open(); notice.appendChild(action);
  }
  const running = !!runningSessionId();
  $('send').disabled = $('task').disabled || !has || !!reason || running || !!sendingSessionId;
  $('send').style.display = running ? 'none' : '';
  $('stop').style.display = running ? '' : 'none';
  $('send-hint').textContent = sendingSessionId ? 'Sending…' : running ? 'Stop' : 'Enter';
  $('send').setAttribute('aria-label', sendingSessionId ? 'Sending message' : 'Send message');
  const owner = deps.findSession(runningSessionId());
  $('stop').setAttribute('aria-label', owner ? 'Stop ' + owner.title : 'Stop');
  $('stop').title = owner ? 'Stop ' + owner.title : 'Stop';
  $('provider-button').disabled = $('task').disabled || running || !!sendingSessionId;
  if ($('effort-button')) $('effort-button').disabled = $('provider-button').disabled;
  if (window.CodeyConversationUI) window.CodeyConversationUI.updateNotice();
  window.CodeyRequests.refreshActions();
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

function setActiveProvider(id) {
  const provider = PROVIDERS.includes(id) ? id : liveDefaultProvider();
  const s = deps.activeSession();
  if (!s) return;
  s.provider = provider;
  deps.persistActiveNow();
  deps.syncProviderUI(provider);
  deps.updateComposerContext();
  updateSend();
}

function clearDraftIfUnchanged(sessionId, draft, revision) {
  captureDraft();
  const session = deps.findSession(sessionId), stored = session?.draft;
  if (!stored || stored.text.trim() !== draft || stored.revision !== revision) return;
  session.draft = {text:'', start:0, end:0, direction:'none', revision:stored.revision + 1};
  deps.persistActiveNow();
  if (activeId() !== sessionId) return;
  $('task').value = '';
  resizeTask();
  updateSend();
  deps.updateComposerContext();
}

async function reportSendFailure(sessionId, response, runId = '') {
  const data = await response.json().catch(() => null);
  let message = '', sendFailure = '', runOwner = '';
  if (response.status === 400 && data?.reason === 'model_selection_invalid') {
    message = 'Select the model again'; sendFailure = 'model_selection';
  } else if (response.status === 503 && data?.error === 'browser worker busy') {
    message = 'Codey is temporarily busy'; sendFailure = 'worker_busy';
  } else if (response.status === 409 && data?.error === 'busy') {
    await window.CodeySse.reconcileRunState();
    const owner = deps.findSession(runningSessionId());
    runOwner = owner?.id || '';
    message = owner ? (owner.id === sessionId ? 'This chat is already running' : 'Another chat is running') : 'Another task is running';
    sendFailure = 'busy';
  }
  if (runId) {
    window.CodeyRequests.fail(sessionId, runId, message || 'Could not send the message', {sendFailure, runOwner});
    return;
  }
  if (!message) { deps.addSendError(sessionId); return; }
  deps.addToSession(sessionId, {type:'err', text:message, sessionId, sendFailure, runOwner});
}

function recoveryActions(message) {
  const sessionId = message.sessionId;
  if (message.sendFailure === 'busy' && deps.findSession(message.runOwner)) {
    return [{label:'Open', onclick:() => {
      if (deps.findSession(message.runOwner)) deps.switchSession(message.runOwner);
    }}];
  }
  if (message.sendFailure === 'model_selection') {
    return [{label:'Choose model', disabled:!!runningSessionId(), onclick:e => {
      if (!deps.findSession(sessionId) || runningSessionId() || sendingSessionId) return;
      e?.stopPropagation();
      deps.switchSession(sessionId);
      $('provider-button').click();
    }}];
  }
  return null;
}

async function sendTaskFromSession(sessionId, task, providerId = '', onSendStarted = null, requestId = '') {
  const text = String(task || '').trim();
  if (!text || runningSessionId() || sendingSessionId) return false;
  const s = deps.findSession(sessionId);
  if (!s) return false;
  const provider = providerId || s.provider || liveDefaultProvider();
  if (!window.CodeyModels.allows(provider, window.CodeyProviderUI.runSelection(s, provider).model_selection?.model)) return false;
  sendingSessionId = sessionId;
  updateSend();
  deps.updateComposerContext();
  const started = window.CodeyRequests.begin(sessionId, text, requestId);
  if (!started) { sendingSessionId = ''; updateSend(); return false; }
  const runId = started.attempt.runId;
  const project = deps.sessionProjectPath(sessionId);
  const intent = deps.currentIntentForSession(sessionId);
  try {
    const r = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, run_id: runId, project, task: text, provider, intent,
        ...window.CodeyProviderUI.runSelection(s, provider) }),
    });
    if (!r.ok) {
      await reportSendFailure(sessionId, r, runId);
      return false;
    }
    if (typeof onSendStarted === 'function') onSendStarted();
    await deps.acceptRunResponse(r, sessionId);
  } catch {
    await window.CodeySse.reconcileRunState();
    const attempt = window.CodeyRequests.forRun(sessionId, runId)?.attempt;
    if (attempt?.state === 'sending') window.CodeyRequests.fail(sessionId, runId, 'Response was not confirmed');
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
  const task = $('task').value.trim();
  const provider = currentProviderId();
  if (!task) return;
  captureDraft();
  const revision = deps.findSession(sessionId).draft.revision;
  await sendTaskFromSession(sessionId, task, provider, () => clearDraftIfUnchanged(sessionId, task, revision));
}

function retryTask(sessionId, requestId) {
  const s = deps.findSession(sessionId);
  if (!s || !window.CodeyRequests.canRetry(sessionId, requestId)) return;
  const original = s.messages.find(m => m.type === 'user' && m.id === requestId);
  return sendTaskFromSession(sessionId, original.text, s.provider, null, requestId);
}

function quoteInReply(sessionId, text) {
  const session = deps.findSession(sessionId);
  if (!session || sessionId !== activeId() || !text || $('task').disabled) return false;
  syncSession(); captureDraft();
  const task = $('task'), original = task.value;
  const separator = !original || original.endsWith('\n\n') ? '' : original.endsWith('\n') ? '\n' : '\n\n';
  const quoted = text.replace(/\r\n?/g, '\n').split('\n').map(line => '> ' + line).join('\n');
  task.focus({preventScroll:true}); task.setSelectionRange(original.length, original.length);
  // One native editing transaction keeps textarea Undo/Redo intact.
  if (!document.execCommand('insertText', false, separator + quoted + '\n\n')) return false;
  captureDraft(); resizeTask(); updateSend(); deps.persistActiveNow();
  return true;
}

async function continueTask(sessionId) {
  if (runningSessionId() || sendingSessionId) return;
  const s = deps.findSession(sessionId);
  if (!s) return;
  if (!window.CodeyModels.allows(s.provider, window.CodeyProviderUI.runSelection(s, s.provider).model_selection?.model)) return;
  const p = deps.sessionProject(s);
  if (!p) {
    deps.addToSession(sessionId, { type: 'err', text: 'Only project tasks can be continued; plain chats have no tool loop.', sessionId });
    return;
  }
  deps.addToSession(sessionId, { type: 'info', text: 'continue task' });
  const task = [
    'Continue the unfinished task in this same conversation.',
    'Use the existing project context and finish the original user request.',
    'If the work is complete, reply with a JSON done tool call.'
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
        continue_task: true,
        provider: s.provider || liveDefaultProvider(),
        intent: 'project',
        ...window.CodeyProviderUI.runSelection(s, s.provider || liveDefaultProvider()),
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
  $('task').addEventListener('input', () => { captureDraft(); resizeTask(); updateSend(); deps.updateComposerContext(); });
  ['select','selectionchange','blur','keyup','pointerup'].forEach(type => $('task').addEventListener(type, captureDraft));
  $('task').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      if (!$('send').disabled) $('send').click();
    }
  });
  $('composer-context').onclick = (e) => {
    const target = e.target.closest('.ctx-token');
    if (!target || runningSessionId()) return;
    if (target.id === 'ctx-folder') {
      const s = deps.activeSession();
      if (!s || deps.sessionProject(s) || deps.projectPickerBusy()) return;
      deps.attachCurrentChatToPickedProject();
    } else if (target.id === 'ctx-research') {
      toggleResearchForActive();
    }
  };
  $('composer-context').addEventListener('keydown', (e) => {
    if ((e.key !== 'Enter' && e.key !== ' ') || runningSessionId()) return;
    const target = e.target.closest('.ctx-token');
    if (!target) return;
    e.preventDefault();
    target.click();
  });
  $('send').onclick = sendActiveDraft;
  $('stop').onclick = () => fetch('/api/stop', { method: 'POST' });
}

window.CodeyComposer = {
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
