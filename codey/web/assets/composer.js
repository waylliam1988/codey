/* Codey composer runtime: task input, send/stop actions, provider selection,
   and project/research context chips. */
(function () {
'use strict';

let deps = null;
let PROVIDERS = [];
let handlersBound = false;
let draftSessionId = '';
let sendingSessionId = '';
const drafts = new Map();

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
  if (!draftSessionId) return;
  const t = $('task');
  const previous = drafts.get(draftSessionId);
  drafts.set(draftSessionId, { text: t.value, start: t.selectionStart, end: t.selectionEnd,
    revision: (previous ? previous.revision : 0) + (previous && previous.text === t.value ? 0 : 1) });
}

function syncSession() {
  if (!deps || draftSessionId === activeId()) return;
  captureDraft();
  draftSessionId = activeId();
  const draft = drafts.get(draftSessionId) || { text: '', start: 0, end: 0 };
  $('task').value = draft.text;
  $('task').setSelectionRange(draft.start, draft.end);
  resizeTask();
  updateSend();
}

function forgetDraft(id) {
  drafts.delete(id);
  if (draftSessionId === id) { draftSessionId = ''; $('task').value = ''; }
}

function resizeTask() {
  const t = $('task');
  t.style.height = 'auto';
  t.style.height = Math.min(220, Math.max(40, t.scrollHeight)) + 'px';
}

function updateSend() {
  const has = $('task').value.trim();
  const running = !!runningSessionId();
  $('send').disabled = !has || running || !!sendingSessionId;
  $('send').style.display = running ? 'none' : '';
  $('stop').style.display = running ? '' : 'none';
  $('send-hint').textContent = sendingSessionId ? 'Sending…' : running ? 'Stop' : 'Enter';
  $('send').setAttribute('aria-label', sendingSessionId ? 'Sending message' : 'Send message');
  const owner = deps.findSession(runningSessionId());
  $('stop').setAttribute('aria-label', owner ? 'Stop ' + owner.title : 'Stop');
  $('stop').title = owner ? 'Stop ' + owner.title : 'Stop';
  $('provider-button').disabled = running || !!sendingSessionId;
  if ($('effort-button')) $('effort-button').disabled = $('provider-button').disabled;
  if (window.CodeyConversationUI) window.CodeyConversationUI.updateNotice();
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
  if (provider === 'local' && !window.CodeyProviderUI.localReady()) deps.openLocalProviderConfig();
}

function clearDraftIfUnchanged(sessionId, draft, revision = null) {
  captureDraft();
  const stored = drafts.get(sessionId);
  if (!stored || stored.text.trim() !== draft || (revision !== null && stored.revision !== revision)) return;
  drafts.delete(sessionId);
  if (activeId() !== sessionId) return;
  $('task').value = '';
  resizeTask();
  updateSend();
  deps.updateComposerContext();
}

async function reportSendFailure(sessionId, text, response) {
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
  if (!message) { deps.addSendError(sessionId, '', '', text); return; }
  deps.addToSession(sessionId, {type:'err', text:message, sessionId, retryTask:text, sendFailure, runOwner});
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

async function sendTaskFromSession(sessionId, task, providerId = '', onSendStarted = null) {
  const text = String(task || '').trim();
  if (!text || runningSessionId() || sendingSessionId) return false;
  const s = deps.findSession(sessionId);
  if (!s) return false;
  const provider = providerId || s.provider || liveDefaultProvider();
  sendingSessionId = sessionId;
  updateSend();
  deps.updateComposerContext();
  deps.pushMsgToSession(sessionId, { type: 'user', text });
  const project = deps.sessionProjectPath(sessionId);
  const intent = deps.currentIntentForSession(sessionId);
  try {
    const r = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, project, task: text, provider, intent,
        ...window.CodeyProviderUI.runSelection(s, provider) }),
    });
    if (!r.ok) {
      await reportSendFailure(sessionId, text, r);
      return false;
    }
    if (typeof onSendStarted === 'function') onSendStarted();
    await deps.acceptRunResponse(r, sessionId);
  } catch {
    deps.addSendError(sessionId, '', '', text);
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
  const revision = drafts.get(sessionId).revision;
  await sendTaskFromSession(sessionId, task, provider, () => clearDraftIfUnchanged(sessionId, task, revision));
}

function retryTask(sessionId, submittedText = '') {
  const s = deps.findSession(sessionId);
  if (!s) return;
  const original = [...s.messages].reverse().find(m => m.type === 'user' && m.text);
  const text = submittedText || (original && original.text);
  if (text) return sendTaskFromSession(sessionId, text, s.provider);
}

async function continueTask(sessionId) {
  if (runningSessionId() || sendingSessionId) return;
  const s = deps.findSession(sessionId);
  if (!s) return;
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
      await reportSendFailure(sessionId, '', r);
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
      deps.attachCurrentChatToPickedProject({ sendDraft: false });
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
  forgetDraft,
  isSending: () => !!sendingSessionId,
  retryTask,
  recoveryActions,
  resizeTask,
  updateSend,
  toggleResearchForActive,
  setActiveProvider,
  clearDraftIfUnchanged,
  sendTaskFromSession,
  continueTask,
};
})();
