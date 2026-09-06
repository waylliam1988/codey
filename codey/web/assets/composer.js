/* Codey composer runtime: task input, send/stop actions, provider selection,
   and project/research context chips. */
(function () {
'use strict';

let deps = null;
let PROVIDERS = [];
let DEFAULT_PROVIDER = '';
let handlersBound = false;

function $(id) { return deps.$(id); }
function runningSessionId() { return deps.getRunningSessionId(); }
function activeId() { return deps.getActiveId(); }
function currentProviderId() { return deps.currentProviderId(); }

function init(nextDeps) {
  deps = nextDeps;
  PROVIDERS = deps.PROVIDERS;
  DEFAULT_PROVIDER = deps.DEFAULT_PROVIDER;
  bindHandlers();
}

function resizeTask() {
  const t = $('task');
  t.style.height = 'auto';
  t.style.height = Math.min(220, Math.max(40, t.scrollHeight)) + 'px';
}

function updateSend() {
  const has = $('task').value.trim();
  const running = !!runningSessionId();
  $('send').disabled = !has || running;
  $('send').style.display = running ? 'none' : '';
  $('stop').style.display = running ? '' : 'none';
  $('send-hint').textContent = running ? 'Stop' : 'Enter';
}

function toggleResearchForActive() {
  if (runningSessionId()) return;
  const s = deps.activeSession();
  if (!s) return;
  s.research = !s.research;
  deps.persistActiveNow();
  deps.updateComposerContext();
  deps.renderChat();
}

function setActiveProvider(id) {
  const provider = PROVIDERS.includes(id) ? id : DEFAULT_PROVIDER;
  const s = deps.activeSession();
  if (!s) return;
  s.provider = provider;
  deps.persistActiveNow();
  deps.syncProviderUI(provider);
  deps.updateComposerContext();
  if (provider === 'local') deps.openLocalProviderConfig();
}

function clearDraftIfUnchanged(sessionId, draft) {
  if (activeId() !== sessionId || $('task').value.trim() !== draft) return;
  $('task').value = '';
  resizeTask();
  updateSend();
  deps.updateComposerContext();
}

async function sendTaskFromSession(sessionId, task, providerId = '', onSendStarted = null) {
  const text = String(task || '').trim();
  if (!text || runningSessionId()) return false;
  const s = deps.findSession(sessionId);
  if (!s) return false;
  const provider = PROVIDERS.includes(providerId)
    ? providerId
    : (PROVIDERS.includes(s.provider) ? s.provider : DEFAULT_PROVIDER);
  if (typeof onSendStarted === 'function') onSendStarted();
  deps.pushMsgToSession(sessionId, { type: 'user', text });
  const project = deps.sessionProjectPath(sessionId);
  const intent = deps.currentIntentForSession(sessionId);
  const r = await fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, project, task: text, provider, intent }),
  });
  if (r.status === 409) { deps.addSendError(sessionId); return true; }
  if (!r.ok) {
    deps.addSendError(sessionId);
    return true;
  }
  await deps.acceptRunResponse(r, sessionId);
  return true;
}

async function sendActiveDraft() {
  const sessionId = activeId();
  const task = $('task').value.trim();
  const provider = currentProviderId();
  if (!task) return;
  await sendTaskFromSession(sessionId, task, provider, () => clearDraftIfUnchanged(sessionId, task));
}

async function continueTask(sessionId) {
  if (runningSessionId()) return;
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
  const r = await fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      project: p.path,
      task,
      continue_task: true,
      provider: s.provider || DEFAULT_PROVIDER,
      intent: 'project',
    }),
  });
  if (r.status === 409 || !r.ok) {
    deps.addSendError(sessionId);
    return;
  }
  await deps.acceptRunResponse(r, sessionId);
}

function bindHandlers() {
  if (handlersBound) return;
  handlersBound = true;
  $('task').addEventListener('input', () => { resizeTask(); updateSend(); deps.updateComposerContext(); });
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
      deps.attachCurrentChatToPickedProject({ sendDraft: !!$('task').value.trim() });
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
  resizeTask,
  updateSend,
  toggleResearchForActive,
  setActiveProvider,
  clearDraftIfUnchanged,
  sendTaskFromSession,
  continueTask,
};
})();
