/* Codey local context drawer: quiet audit UI for bounded local state. */
(function () {
  'use strict';

  let deps = null;
  let loadedScope = null;
  let activeMenu = null;
  let confirmTimer = null;

function init(nextDeps) {
  deps = nextDeps;
  $('local-context-close').onclick = closeLocalContextDrawer;
  $('local-context-export').onclick = exportLocalContext;
  $('local-context-refresh').onclick = loadLocalContextDrawer;
  $('local-context-menu').addEventListener('click', onMenuClick);
}

function $(id) { return deps.$(id); }

function openLocalContextDrawer() {
  if (deps.closeOtherDrawers) deps.closeOtherDrawers('local_context');
  $('local-context-drawer').classList.add('open');
  $('local-context-drawer').setAttribute('aria-hidden', 'false');
  loadLocalContextDrawer();
}

function closeLocalContextDrawer() {
  closeRowMenu();
  disarmDangerButtons();
  loadedScope = null;
  $('local-context-drawer').classList.remove('open');
  $('local-context-drawer').setAttribute('aria-hidden', 'true');
}

async function loadLocalContextDrawer() {
  const requestScope = currentScope();
  loadedScope = requestScope;
  $('local-context-subtitle').textContent = 'Loading...';
  $('local-context-body').innerHTML = '<div class="changes-empty"><span class="drawer-loading"><span class="spinner"></span><span>Loading local context...</span></span></div>';
  try {
    const params = new URLSearchParams();
    if (requestScope.session_id) params.set('session_id', requestScope.session_id);
    if (requestScope.project) params.set('project', requestScope.project);
    const suffix = params.toString() ? '?' + params.toString() : '';
    const response = await fetch('/api/ghost/summary' + suffix, { cache: 'no-store' });
    const data = await response.json();
    if (!isOpen() || !sameScope(requestScope, currentScope())) return;
    loadedScope = data && data.scope ? cleanScope(data.scope) : requestScope;
    renderLocalContext(data);
  } catch {
    if (!isOpen() || !sameScope(requestScope, currentScope())) return;
    loadedScope = null;
    $('local-context-subtitle').textContent = 'Unavailable';
    $('local-context-body').innerHTML = '<div class="changes-error">Error loading local context</div>';
  }
}

function currentScope() {
  return cleanScope({
    session_id: deps.getActiveId(),
    project: deps.currentProjectPath(),
  });
}

function cleanScope(scope) {
  return {
    session_id: String(scope && scope.session_id || ''),
    project: normalizeProjectPath(scope && scope.project),
  };
}

function normalizeProjectPath(value) {
  return String(value || '').replace(/\\/g, '/');
}

function sameScope(left, right) {
  const a = cleanScope(left);
  const b = cleanScope(right);
  return a.session_id === b.session_id && a.project === b.project;
}

function isOpen() {
  return $('local-context-drawer').classList.contains('open');
}

function handleScopeChanged() {
  if (isOpen() && loadedScope && !sameScope(loadedScope, currentScope())) closeLocalContextDrawer();
}

function renderLocalContext(data) {
  closeRowMenu();
  const body = $('local-context-body');
  body.innerHTML = '';
  if (!data || !data.ok || data.available === false) {
    $('local-context-subtitle').textContent = 'Unavailable';
    body.innerHTML = '<div class="changes-empty">Local context unavailable</div>';
    return;
  }
  const counts = data.counts || {};
  const state = data.enabled ? 'On' : 'Off';
  $('local-context-subtitle').textContent = `${counts.active || 0} active · ${counts.review || 0} pending · Updates ${state}`;

  const contextRows = Array.isArray(data.context) ? data.context : [];
  const reviewRows = Array.isArray(data.review) ? data.review : [];
  const activeRows = Array.isArray(data.active) ? data.active : [];
  const taskRows = Array.isArray(data.tasks) ? data.tasks : [];
  const warnings = Array.isArray(data.health && data.health.warnings) ? data.health.warnings : [];
  const hasContent = !!(contextRows.length || reviewRows.length || activeRows.length || taskRows.length);
  const hasWarning = !!warnings.length;

  if (!hasContent && !hasWarning) {
    $('local-context-subtitle').textContent = `No local context yet · Updates ${state}`;
    body.appendChild(emptyStateNode('No local context yet'));
    body.appendChild(settingsNode(data.enabled));
    return;
  }
  appendGroup(body, 'Recent focus', contextRows, rowNode);
  appendGroup(body, 'Pending review', reviewRows, reviewRowNode);
  appendGroup(body, 'Active preferences', activeRows, rowNode);
  appendGroup(body, 'Follow-ups', taskRows, taskRowNode);
  if (hasWarning || hasContent) body.appendChild(healthNode(data.health || {}, data.enabled));
  body.appendChild(settingsNode(data.enabled));
}

function appendGroup(container, title, rows, renderer) {
  if (rows.length) container.appendChild(groupNode(title, rows, renderer));
}

function groupNode(title, rows, renderer) {
  const group = document.createElement('section');
  group.className = 'local-context-group';
  const head = document.createElement('div');
  head.className = 'local-context-group-title';
  head.textContent = title;
  group.appendChild(head);
  for (const row of rows) group.appendChild(renderer(row));
  return group;
}

function emptyStateNode(text) {
  const empty = document.createElement('div');
  empty.className = 'changes-empty local-context-empty';
  empty.textContent = text;
  return empty;
}

function rowNode(row) {
  return baseRowNode(row);
}

function reviewRowNode(row) {
  return baseRowNode(row, [
    ['accept_candidate', 'Accept'],
    ['reject_candidate', 'Reject'],
  ]);
}

function taskRowNode(row) {
  const actions = [];
  if (['candidate', 'blocked', 'rejected'].includes(row.status)) actions.push(['queue_work_item', 'Queue']);
  if (['candidate', 'queued', 'blocked'].includes(row.status)) actions.push(['reject_work_item', 'Reject']);
  return baseRowNode(row, actions);
}

function baseRowNode(row, actions) {
  const wrap = document.createElement('div');
  wrap.className = 'local-context-row';
  const main = document.createElement('div');
  main.className = 'local-context-main';
  const title = document.createElement('div');
  title.className = 'local-context-title';
  title.textContent = row.summary || 'Untitled';
  const meta = document.createElement('div');
  meta.className = 'local-context-meta';
  meta.textContent = [row.scope_label, row.kind, row.status_label || row.reason || '', row.confidence || '']
    .filter(Boolean)
    .join(' · ');
  main.append(title, meta);
  if (row.evidence_preview) {
    const evidence = document.createElement('div');
    evidence.className = 'local-context-evidence';
    evidence.textContent = row.evidence_preview;
    main.appendChild(evidence);
  }
  wrap.appendChild(main);
  if (actions && actions.length) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'local-context-more';
    more.title = 'More';
    more.textContent = '⋯';
    more.onclick = (event) => {
      event.stopPropagation();
      openRowMenu(more, row, actions);
    };
    wrap.appendChild(more);
  }
  return wrap;
}

function healthNode(health, enabled) {
  const group = document.createElement('section');
  group.className = 'local-context-group';
  const title = document.createElement('div');
  title.className = 'local-context-group-title';
  title.textContent = 'Health';
  const status = document.createElement('div');
  status.className = 'local-context-health';
  const association = health.associations === 'available' ? 'Local ordering is available' : 'Local ordering unavailable';
  status.textContent = `${enabled ? 'Updates are on' : 'Updates are off'} · ${association}`;
  group.append(title, status);
  const warnings = Array.isArray(health.warnings) ? health.warnings : [];
  for (const warning of warnings.slice(0, 4)) {
    const row = document.createElement('div');
    row.className = 'local-context-warning';
    row.textContent = warning;
    group.appendChild(row);
  }
  return group;
}

function settingsNode(enabled) {
  const group = document.createElement('section');
  group.className = 'local-context-settings';
  const update = document.createElement('button');
  update.type = 'button';
  update.className = 'drawer-btn';
  update.textContent = enabled ? 'Disable updates' : 'Enable updates';
  update.onclick = () => postAction(enabled ? 'disable_updates' : 'enable_updates');

  const deleteChat = dangerButton('Delete chat data', 'Confirm delete', () => postAction('delete_scope', {
    scope: 'session',
    session_id: actionScope().session_id,
    confirm: true,
  }));
  const deleteProject = dangerButton('Delete project data', 'Confirm delete', () => postAction('delete_scope', {
    scope: 'project',
    project: actionScope().project,
    confirm: true,
  }));
  deleteProject.disabled = !actionScope().project;

  const reset = dangerButton('Reset all', 'Confirm reset', () => postAction('reset_all', { confirm: true }));
  group.append(update, deleteChat, deleteProject, reset);
  return group;
}

function dangerButton(label, confirmLabel, run) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'drawer-btn local-context-danger';
  button.textContent = label;
  button.dataset.label = label;
  button.onclick = () => {
    if (button.classList.contains('arming')) {
      disarmDangerButtons();
      run();
      return;
    }
    disarmDangerButtons();
    button.textContent = confirmLabel;
    button.classList.add('arming');
    confirmTimer = setTimeout(disarmDangerButtons, 3000);
  };
  return button;
}

function disarmDangerButtons() {
  if (confirmTimer !== null) {
    clearTimeout(confirmTimer);
    confirmTimer = null;
  }
  document.querySelectorAll('.local-context-danger.arming').forEach((button) => {
    button.classList.remove('arming');
    button.textContent = button.dataset.label || button.textContent;
  });
}

function openRowMenu(anchor, row, actions) {
  if (deps.closeAllMenus) deps.closeAllMenus();
  activeMenu = { row };
  const menu = $('local-context-menu');
  menu.innerHTML = '';
  for (const [action, label] of actions) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.act = action;
    button.textContent = label;
    menu.appendChild(button);
  }
  if (deps.openMenuAt) deps.openMenuAt(menu, anchor);
}

function closeRowMenu() {
  activeMenu = null;
  const menu = $('local-context-menu');
  if (menu) {
    menu.classList.remove('open');
    menu.innerHTML = '';
  }
}

function onMenuClick(event) {
  const button = event.target.closest('button[data-act]');
  if (!button || !activeMenu) return;
  const action = button.dataset.act;
  const row = activeMenu.row;
  closeRowMenu();
  postAction(action, { id: row.id });
}

async function postAction(action, payload) {
  if (!loadedScope || !sameScope(loadedScope, currentScope())) {
    closeLocalContextDrawer();
    return;
  }
  const scope = actionScope();
  const body = {
    action,
    session_id: scope.session_id,
    project: scope.project,
    ...(payload || {}),
  };
  $('local-context-subtitle').textContent = 'Updating...';
  try {
    const response = await fetch('/api/ghost/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      $('local-context-subtitle').textContent = 'Update failed';
      return;
    }
    await loadLocalContextDrawer();
  } catch {
    $('local-context-subtitle').textContent = 'Update failed';
  }
}

function actionScope() {
  return loadedScope || currentScope();
}

async function exportLocalContext() {
  if (!loadedScope || !sameScope(loadedScope, currentScope())) {
    closeLocalContextDrawer();
    return;
  }
  $('local-context-export').textContent = 'Exporting';
  try {
    const response = await fetch('/api/ghost/export', { cache: 'no-store' });
    const data = await response.json();
    const ok = await deps.copyText(JSON.stringify(data, null, 2));
    $('local-context-export').textContent = ok ? 'Copied' : 'Failed';
  } catch {
    $('local-context-export').textContent = 'Failed';
  }
  setTimeout(() => { $('local-context-export').textContent = 'Export'; }, 1200);
}

window.CodeyLocalContextDrawer = {
  init,
  open: openLocalContextDrawer,
  load: loadLocalContextDrawer,
  close: closeLocalContextDrawer,
  render: renderLocalContext,
  handleScopeChanged,
};
})();
