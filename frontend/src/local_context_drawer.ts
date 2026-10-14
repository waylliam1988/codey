import { runtime } from './runtime';
type LocalRecord = Record<string, unknown>;
type LocalAction = [string, string];
export interface LocalContextDeps {
  $(id: string): HTMLElement;
  closeOtherDrawers?(active?: string): void;
  getActiveId(): string;
  currentProjectPath(): string;
  openMenuAt?(menu: HTMLElement, anchor: HTMLElement): void;
  closeAllMenus?(): void;
  copyText(text: string): Promise<boolean>;
}
interface LocalScope {
  session_id: string;
  project: string;
}
function recordOf(value: unknown): LocalRecord {
  return typeof value === 'object' && value !== null ? (value as LocalRecord) : {};
}
function textField(row: LocalRecord, key: string): string {
  const value = row[key];
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '';
}
function rowsOf(value: unknown): LocalRecord[] {
  return Array.isArray(value)
    ? (value.filter((item) => typeof item === 'object' && item !== null) as LocalRecord[])
    : [];
}
/* Codey local context drawer: quiet audit UI for bounded local state. */
(function () {
  'use strict';

  let deps: LocalContextDeps;
  let loadedScope: LocalScope | null = null;
  let activeMenu: { row: LocalRecord; wrap: HTMLElement | null } | null = null;
  let confirmTimer: ReturnType<typeof setTimeout> | null = null;

  function init(nextDeps: LocalContextDeps) {
    deps = nextDeps;
    $('local-context-close').onclick = closeLocalContextDrawer;
    $('local-context-export').onclick = exportLocalContext;
    $('local-context-refresh').onclick = loadLocalContextDrawer;
    $('local-context-menu').addEventListener('click', onMenuClick);
  }

  function $(id: string): HTMLElement {
    return deps.$(id);
  }

  function openLocalContextDrawer() {
    if (deps.closeOtherDrawers) deps.closeOtherDrawers('local_context');
    runtime.UiState.setDrawerOpen('local-context-drawer', true);
    loadLocalContextDrawer();
  }

  function closeLocalContextDrawer() {
    closeRowMenu();
    disarmDangerButtons();
    loadedScope = null;
    runtime.UiState.setDrawerOpen('local-context-drawer', false);
  }

  async function loadLocalContextDrawer() {
    const requestScope = currentScope();
    loadedScope = requestScope;
    $('local-context-subtitle').textContent = 'Loading...';
    $('local-context-body').innerHTML =
      '<div class="changes-empty"><span class="drawer-loading"><span class="spinner"></span><span>Loading local context...</span></span></div>';
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

  function cleanScope(scope: unknown): LocalScope {
    const record = recordOf(scope);
    return {
      session_id: String(record.session_id || ''),
      project: normalizeProjectPath(record.project),
    };
  }

  function normalizeProjectPath(value: unknown): string {
    return String(value || '').replace(/\\/g, '/');
  }

  function sameScope(left: unknown, right: unknown): boolean {
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

  function renderLocalContext(data: LocalRecord) {
    closeRowMenu();
    const body = $('local-context-body');
    body.innerHTML = '';
    if (!data || !data.ok || data.available === false) {
      $('local-context-subtitle').textContent = 'Unavailable';
      body.innerHTML = '<div class="changes-empty">Local context unavailable</div>';
      return;
    }
    const counts = recordOf(data.counts);
    const enabled = data.enabled === true;
    const state = enabled ? 'On' : 'Off';
    $('local-context-subtitle').textContent =
      `${counts.active || 0} active · ${counts.review || 0} pending · ${counts.repair || 0} needs attention · Updates ${state}`;

    const contextRows = rowsOf(data.context),
      reviewRows = rowsOf(data.review),
      repairRows = rowsOf(data.repair),
      activeRows = rowsOf(data.active),
      taskRows = rowsOf(data.tasks),
      observationRows = rowsOf(data.observations);
    const health = recordOf(data.health);
    const warnings = Array.isArray(health.warnings) ? health.warnings : [];
    const hasContent = !!(
      contextRows.length ||
      reviewRows.length ||
      repairRows.length ||
      activeRows.length ||
      taskRows.length ||
      observationRows.length
    );
    const hasWarning = !!warnings.length;

    if (!hasContent && !hasWarning) {
      $('local-context-subtitle').textContent = `No local context yet · Updates ${state}`;
      body.appendChild(emptyStateNode('No local context yet'));
      body.appendChild(settingsNode(enabled));
      return;
    }
    appendGroup(body, 'Recent focus', contextRows, rowNode);
    appendGroup(body, 'Pending review', reviewRows, reviewRowNode);
    appendGroup(body, 'Needs attention', repairRows, repairRowNode);
    appendGroup(body, 'Active preferences', activeRows, rowNode);
    appendGroup(body, 'Recent experiences', observationRows, (row: LocalRecord) => observationRowNode(row, enabled));
    appendGroup(body, 'Follow-ups', taskRows, taskRowNode);
    if (hasWarning || hasContent) body.appendChild(healthNode(health, enabled));
    body.appendChild(settingsNode(enabled));
  }

  function appendGroup(
    container: HTMLElement,
    title: string,
    rows: LocalRecord[],
    renderer: (row: LocalRecord) => HTMLElement,
  ): void {
    if (rows.length) container.appendChild(groupNode(title, rows, renderer));
  }

  function groupNode(title: string, rows: LocalRecord[], renderer: (row: LocalRecord) => HTMLElement): HTMLElement {
    const group = document.createElement('section');
    group.className = 'local-context-group';
    const head = document.createElement('div');
    head.className = 'local-context-group-title';
    head.textContent = title;
    group.appendChild(head);
    for (const row of rows) group.appendChild(renderer(row));
    return group;
  }

  function emptyStateNode(text: string): HTMLElement {
    const empty = document.createElement('div');
    empty.className = 'changes-empty local-context-empty';
    empty.textContent = text;
    return empty;
  }

  function rowNode(row: LocalRecord): HTMLElement {
    return baseRowNode(row);
  }

  function reviewRowNode(row: LocalRecord): HTMLElement {
    return baseRowNode(row, [
      ['accept_candidate', 'Accept'],
      ['reject_candidate', 'Reject'],
    ]);
  }

  function repairRowNode(row: LocalRecord): HTMLElement {
    return baseRowNode(row, [
      ['accept_candidate', 'Retry'],
      ['reject_candidate', 'Discard'],
    ]);
  }

  function observationRowNode(row: LocalRecord, enabled: boolean): HTMLElement {
    return baseRowNode(row, enabled ? [['propose_preference', 'Remember preference']] : []);
  }

  function taskRowNode(row: LocalRecord): HTMLElement {
    const actions: LocalAction[] = [];
    if (['candidate', 'blocked', 'rejected'].includes(textField(row, 'status')))
      actions.push(['queue_work_item', 'Queue']);
    if (['candidate', 'queued', 'blocked'].includes(textField(row, 'status')))
      actions.push(['reject_work_item', 'Reject']);
    return baseRowNode(row, actions);
  }

  function baseRowNode(row: LocalRecord, actions: LocalAction[] = []): HTMLElement {
    const wrap = document.createElement('div');
    wrap.className = 'local-context-row';
    const main = document.createElement('div');
    main.className = 'local-context-main';
    const title = document.createElement('div');
    title.className = 'local-context-title';
    title.textContent = textField(row, 'summary') || 'Untitled';
    const meta = document.createElement('div');
    meta.className = 'local-context-meta';
    meta.textContent = [
      textField(row, 'scope_label'),
      textField(row, 'kind'),
      textField(row, 'status_label') || textField(row, 'reason'),
      textField(row, 'confidence'),
    ]
      .filter(Boolean)
      .join(' · ');
    main.append(title, meta);
    if (textField(row, 'evidence_preview')) {
      const evidence = document.createElement('div');
      evidence.className = 'local-context-evidence';
      evidence.textContent = textField(row, 'evidence_preview');
      main.appendChild(evidence);
    }
    wrap.appendChild(main);
    if (actions && actions.length) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'local-context-more';
      more.setAttribute('aria-label', 'More');
      more.textContent = '⋯';
      more.onclick = (event) => {
        event.stopPropagation();
        openRowMenu(more, row, actions);
      };
      wrap.appendChild(more);
    }
    return wrap;
  }

  function healthNode(health: LocalRecord, enabled: boolean): HTMLElement {
    const group = document.createElement('section');
    group.className = 'local-context-group';
    const title = document.createElement('div');
    title.className = 'local-context-group-title';
    title.textContent = 'Health';
    const status = document.createElement('div');
    status.className = 'local-context-health';
    const association =
      health.associations === 'available' ? 'Local ordering is available' : 'Local ordering unavailable';
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

  function settingsNode(enabled: boolean): HTMLElement {
    const group = document.createElement('section');
    group.className = 'local-context-settings';
    const update = document.createElement('button');
    update.type = 'button';
    update.className = 'drawer-btn';
    update.textContent = enabled ? 'Disable updates' : 'Enable updates';
    update.onclick = () => postAction(enabled ? 'disable_updates' : 'enable_updates');

    const deleteChat = dangerButton('Delete chat data', 'Confirm delete', () =>
      postAction('delete_scope', {
        scope: 'session',
        session_id: actionScope().session_id,
        confirm: true,
      }),
    );
    const deleteProject = dangerButton('Delete project data', 'Confirm delete', () =>
      postAction('delete_scope', {
        scope: 'project',
        project: actionScope().project,
        confirm: true,
      }),
    );
    deleteProject.disabled = !actionScope().project;

    const reset = dangerButton('Reset all', 'Confirm reset', () => postAction('reset_all', { confirm: true }));
    group.append(update, deleteChat, deleteProject, reset);
    return group;
  }

  function dangerButton(label: string, confirmLabel: string, run: () => void): HTMLButtonElement {
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
    document.querySelectorAll<HTMLElement>('.local-context-danger.arming').forEach((button) => {
      button.classList.remove('arming');
      button.textContent = button.dataset.label || button.textContent;
    });
  }

  function openRowMenu(anchor: HTMLElement, row: LocalRecord, actions: LocalAction[]): void {
    if (deps.closeAllMenus) deps.closeAllMenus();
    activeMenu = { row, wrap: anchor.parentElement };
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

  function onMenuClick(event: MouseEvent): void {
    const target = event.target as { closest?: (selector: string) => HTMLButtonElement | null } | null;
    const button = target && typeof target.closest === 'function' ? target.closest('button[data-act]') : null;
    if (!button || !activeMenu) return;
    const action = button.dataset.act;
    const row = activeMenu.row;
    const wrap = activeMenu.wrap;
    closeRowMenu();
    if (action === 'propose_preference') {
      showPreferenceForm(row, wrap);
      return;
    }
    if (typeof action === 'string') postAction(action, { id: row.id });
  }

  function showPreferenceForm(row: LocalRecord, wrap: HTMLElement | null): void {
    if (!wrap) return;
    const existing = wrap.querySelector('.local-context-preference-form');
    if (existing) {
      existing.remove();
      wrap.classList.remove('editing');
      return;
    }
    const form = document.createElement('form');
    form.className = 'local-context-preference-form';

    const preference = document.createElement('select');
    preference.required = true;
    for (const [value, label] of [
      ['', 'Choose a preference'],
      ['reply_length:concise', 'Concise answers'],
      ['reply_length:detailed', 'Detailed answers'],
      ['reply_structure:answer_first', 'Answer first'],
      ['format:bullets', 'Bullet lists'],
      ['format:table', 'Tables'],
      ['format:markdown', 'Markdown'],
      ['tone:direct', 'Direct tone'],
      ['tone:technical', 'Technical tone'],
    ]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      preference.appendChild(option);
    }
    preference.setAttribute('aria-label', 'Preference');

    const scope = document.createElement('select');
    scope.setAttribute('aria-label', 'Where to remember');
    const scopes = [['session', 'This chat']];
    if (actionScope().project) scopes.push(['project', 'Current project']);
    scopes.push(['user', 'This device']);
    for (const [value, label] of scopes) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      scope.appendChild(option);
    }

    const quote = document.createElement('input');
    quote.type = 'text';
    quote.maxLength = 240;
    quote.required = true;
    quote.placeholder = 'Exact words from this request';
    quote.setAttribute('aria-label', 'Evidence quote');
    quote.value =
      row.summary === 'Hidden item'
        ? ''
        : String(row.summary || '')
            .replace(/\.\.\.$/, '')
            .trim();

    const save = document.createElement('button');
    save.type = 'submit';
    save.className = 'drawer-btn';
    save.textContent = 'Add to pending review';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'drawer-btn';
    cancel.textContent = 'Cancel';
    cancel.onclick = () => {
      form.remove();
      wrap.classList.remove('editing');
    };
    form.append(preference, scope, quote, save, cancel);
    form.onsubmit = (event) => {
      event.preventDefault();
      const [conflict_key, value_key] = preference.value.split(':');
      postAction('propose_preference', {
        id: row.id,
        scope: scope.value,
        conflict_key,
        value_key,
        evidence_quote: quote.value.trim(),
      });
    };
    wrap.classList.add('editing');
    wrap.appendChild(form);
    preference.focus();
  }

  async function postAction(action: string, payload: LocalRecord = {}): Promise<void> {
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
        await loadLocalContextDrawer();
        $('local-context-subtitle').textContent = data.error || 'Update failed';
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
    setTimeout(() => {
      $('local-context-export').textContent = 'Export';
    }, 1200);
  }

  runtime.LocalContextDrawer = {
    init,
    open: openLocalContextDrawer,
    load: loadLocalContextDrawer,
    close: closeLocalContextDrawer,
    render: renderLocalContext,
    handleScopeChanged,
  };
})();
