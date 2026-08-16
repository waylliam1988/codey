/* Codey run details: quiet inline explanation loaded only on request. */
(function () {
'use strict';

let deps = null;
const cache = {};

function init(nextDeps) {
  deps = nextDeps;
}

function actionForMessage(message) {
  const runId = String((message && message.runId) || '').trim();
  if (!runId) return null;
  return {
    label: 'Details',
    onclick: event => toggle(event.currentTarget, message),
  };
}
function actionsForMessage(message, extras) {
  const actions = [];
  const details = actionForMessage(message);
  if (details) actions.push(details);
  for (const action of Array.isArray(extras) ? extras : []) {
    if (action) actions.push(action);
  }
  return actions;
}

async function toggle(button, message) {
  if (!button || !message) return;
  const container = button.closest('.msg');
  if (!container) return;
  let panel = container.querySelector('.run-details');
  if (panel) {
    const hidden = panel.hasAttribute('hidden');
    if (hidden) panel.removeAttribute('hidden');
    else panel.setAttribute('hidden', '');
    button.setAttribute('aria-expanded', hidden ? 'true' : 'false');
    if (hidden) reveal(panel);
    return;
  }
  panel = document.createElement('div');
  panel.className = 'run-details';
  panel.textContent = 'Loading details...';
  container.appendChild(panel);
  button.setAttribute('aria-expanded', 'true');

  const sessionId = String(message.sessionId || deps.getActiveId() || '').trim();
  const runId = String(message.runId || '').trim();
  const key = `${sessionId}:${runId}`;
  try {
    const data = cache[key] || await fetchDetails(sessionId, runId);
    cache[key] = data;
    render(panel, data);
    reveal(panel);
  } catch {
    renderUnavailable(panel);
    reveal(panel);
  }
}

async function fetchDetails(sessionId, runId) {
  if (!sessionId || !runId) throw new Error('missing ids');
  const params = new URLSearchParams({ session_id: sessionId, run_id: runId });
  const response = await fetch('/api/run_details?' + params.toString());
  if (!response.ok) throw new Error('details unavailable');
  const data = await response.json();
  if (!data || data.ok !== true) throw new Error('details unavailable');
  return data;
}

function render(panel, data) {
  panel.innerHTML = '';
  const details = data.details || {};
  const title = document.createElement('div');
  title.className = 'run-details-title';
  title.textContent = details.title || 'Run details';
  panel.appendChild(title);

  const rows = Array.isArray(details.rows) ? details.rows : [];
  if (!data.available || !rows.length) {
    appendRow(panel, 'Status', 'Details unavailable', 'warning');
  } else {
    for (const row of rows) {
      appendRow(panel, row.label, row.value, row.tone);
    }
  }

  const warnings = Array.isArray(details.warnings) ? details.warnings : [];
  for (const warning of warnings) {
    appendRow(panel, 'Note', warning, 'warning');
  }
}

function renderUnavailable(panel) {
  panel.innerHTML = '';
  const title = document.createElement('div');
  title.className = 'run-details-title';
  title.textContent = 'Run details';
  panel.appendChild(title);
  appendRow(panel, 'Status', 'Details unavailable', 'warning');
}

function appendRow(panel, label, value, tone) {
  const row = document.createElement('div');
  row.className = 'run-details-row' + (tone === 'warning' ? ' warning' : '');
  const labelEl = document.createElement('div');
  labelEl.className = 'run-details-label';
  labelEl.textContent = String(label || '');
  const valueEl = document.createElement('div');
  valueEl.className = 'run-details-value';
  valueEl.textContent = String(value || '');
  row.append(labelEl, valueEl);
  panel.appendChild(row);
}

function reveal(panel) {
  requestAnimationFrame(() => panel.scrollIntoView({ block: 'nearest' }));
}

window.CodeyRunDetails = {
  init,
  actionForMessage,
  actionsForMessage,
};
})();
