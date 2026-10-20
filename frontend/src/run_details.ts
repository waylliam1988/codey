import { runtime } from './runtime';
import type { Message, RunData } from './types/domain';
export interface RunDetailsDeps {
  getActiveId?: () => string;
}
export interface RunDetailsAction {
  label: string;
  disabled?: boolean;
  onclick: (event: Event) => unknown;
}
/* Codey run details: quiet inline explanation loaded only on request. */
(function () {
  'use strict';

  let deps: RunDetailsDeps = {};
  const cache: Record<string, RunData> = {};
  const CACHE_LIMIT = 32;

  function cacheSet(key: string, data: RunData): void {
    if (!Object.prototype.hasOwnProperty.call(cache, key) && Object.keys(cache).length >= CACHE_LIMIT) {
      delete cache[Object.keys(cache)[0]];
    }
    cache[key] = data;
  }

  function init(nextDeps: RunDetailsDeps): void {
    deps = nextDeps;
  }

  function actionForMessage(message: Message) {
    const runId = String((message && message.runId) || '').trim();
    if (!runId) return null;
    return {
      label: 'Details',
      onclick: (event: Event) => toggle(event.currentTarget as Element | null, message),
    };
  }
  function actionsForMessage(message: Message, extras: Array<RunDetailsAction | null> | undefined): RunDetailsAction[] {
    const actions = [];
    const details = actionForMessage(message);
    if (details) actions.push(details);
    for (const action of Array.isArray(extras) ? extras : []) {
      if (action) actions.push(action);
    }
    return actions;
  }

  async function toggle(button: Element | null, message: Message | null): Promise<void> {
    if (!button || !message) return;
    const container = button.closest<HTMLElement>('.msg');
    if (!container) return;
    let panel = container.querySelector<HTMLElement>('.run-details');
    if (panel) {
      const hidden = panel.hasAttribute('hidden');
      if (hidden) panel.removeAttribute('hidden');
      else panel.setAttribute('hidden', '');
      button.setAttribute('aria-expanded', hidden ? 'true' : 'false');
      if (hidden) reveal(panel as HTMLElement);
      return;
    }
    panel = document.createElement('div');
    panel.className = 'run-details';
    renderLoading(panel);
    container.appendChild(panel);
    button.setAttribute('aria-expanded', 'true');

    const sessionId = String(message.sessionId || deps.getActiveId?.() || '').trim();
    const runId = String(message.runId || '').trim();
    const key = `${sessionId}:${runId}`;
    try {
      const data = cache[key] || (await fetchDetails(sessionId, runId));
      cacheSet(key, data);
      render(panel, data);
      if (runtime.Workspace) {
        const actions = document.createElement('div');
        actions.className = 'run-details-actions';
        for (const [label, action] of [
          ['Recorded review', () => runtime.Workspace.select('review', { runId })],
          ['Workflow history', () => runtime.Workspace.select('history', { mode: 'workflow', runId })],
        ] as const) {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'link-btn';
          button.textContent = label;
          button.onclick = action;
          actions.append(button);
        }
        panel.append(actions);
      }
      reveal(panel);
    } catch {
      renderUnavailable(panel);
      reveal(panel);
    }
  }

  async function fetchDetails(sessionId: string, runId: string): Promise<RunData> {
    if (!sessionId || !runId) throw new Error('missing ids');
    const params = new URLSearchParams({ session_id: sessionId, run_id: runId });
    const response = await fetch('/api/run_details?' + params.toString());
    if (!response.ok) throw new Error('details unavailable');
    const data = (await response.json()) as RunData;
    if (!data || data.ok !== true) throw new Error('details unavailable');
    return data;
  }

  function render(panel: HTMLElement, data: RunData): void {
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

  function renderUnavailable(panel: HTMLElement): void {
    panel.innerHTML = '';
    const title = document.createElement('div');
    title.className = 'run-details-title';
    title.textContent = 'Run details';
    panel.appendChild(title);
    appendRow(panel, 'Status', 'Details unavailable', 'warning');
  }

  function renderLoading(panel: HTMLElement): void {
    panel.innerHTML = '';
    const row = document.createElement('div');
    row.className = 'run-details-row';
    const labelEl = document.createElement('div');
    labelEl.className = 'run-details-label';
    labelEl.textContent = 'Status';
    const valueEl = document.createElement('div');
    valueEl.className = 'run-details-value drawer-loading';
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    const label = document.createElement('span');
    label.textContent = 'Loading details...';
    valueEl.append(spinner, label);
    row.append(labelEl, valueEl);
    panel.appendChild(row);
  }

  function appendRow(panel: HTMLElement, label: unknown, value: unknown, tone: string | undefined): void {
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

  function reveal(panel: HTMLElement): void {
    requestAnimationFrame(() => panel.scrollIntoView({ block: 'nearest' }));
  }

  runtime.RunDetails = {
    init,
    actionForMessage,
    actionsForMessage,
  };
})();
