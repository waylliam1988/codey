import type { ModelChoice, ModelSource, ModelState, ModelUse } from './types/domain';
export interface ModelSettingsDeps {
  close(): void;
}
/* One source editor for every connector, including removable integrations. */
(function () {
  'use strict';
  type EditorModel = { id: string; name?: string };
  type EditorSource = ModelSource & {
    label: string;
    discoverable?: boolean;
    connection_editor?: boolean;
    models: EditorModel[];
  };
  type ModelRow = { label: HTMLLabelElement; input: HTMLInputElement; model: EditorModel; text: HTMLSpanElement };
  interface IdMap {
    'model-settings-save': HTMLButtonElement;
    'model-settings-cancel': HTMLButtonElement;
    'model-settings-retry': HTMLButtonElement;
    'model-settings-error': HTMLElement;
    'model-source-list': HTMLElement;
    'model-settings-body': HTMLElement;
    'local-connection-editor': HTMLElement;
    'model-settings-loading': HTMLElement;
  }
  let deps: ModelSettingsDeps;
  let draft: ModelState | null = null,
    baseline = '',
    generation = 0,
    saving = false;
  function $<K extends keyof IdMap>(id: K): IdMap[K];
  function $(id: string): HTMLElement;
  function $(id: string): HTMLElement {
    const element = document.getElementById(id);
    if (!element) throw new Error(`Missing element: ${id}`);
    return element;
  }
  function currentDraft(): ModelState {
    if (!draft) throw new Error('model settings are not open');
    return draft;
  }
  function init(next: ModelSettingsDeps): void {
    deps = next;
    $('model-settings-save').onclick = save;
    $('model-settings-cancel').onclick = deps.close as () => void;
    $('model-settings-retry').onclick = open;
  }
  function error(text: unknown): void {
    $('model-settings-error').textContent = String(text ?? '');
  }
  function preferenceKey(sources: Record<string, ModelChoice>): string {
    return JSON.stringify(
      Object.keys(sources)
        .sort()
        .map((id) => [id, !!sources[id].enabled, [...new Set(sources[id].models)].sort()]),
    );
  }
  function updateSaveState(): void {
    $('model-settings-save').disabled =
      saving || !draft || preferenceKey(currentDraft().preferences.sources) === baseline;
  }
  function used(source: EditorSource, model?: string): boolean {
    return currentDraft().in_use.some((item: ModelUse) =>
      source.id === 'websites'
        ? source.models.some((entry) => entry.id === item.provider) && (!model || item.provider === model)
        : item.provider === source.id && (!model || !item.model || item.model === model),
    );
  }
  function button(label: string, action: () => unknown | Promise<unknown>) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'link-btn';
    node.textContent = label;
    node.onclick = action;
    return node;
  }
  function checkbox(
    label: string,
    checked: boolean,
    action: (checked: boolean) => void,
    className = '',
  ): HTMLInputElement {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = checked;
    input.className = className;
    input.setAttribute('aria-label', label);
    input.onchange = () => action(input.checked);
    return input;
  }
  function renderSource(source: EditorSource): HTMLElement {
    const choice = currentDraft().preferences.sources[source.id];
    const section = document.createElement('section');
    section.className = 'model-source';
    section.dataset.sourceId = source.id;
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    const name = document.createElement('span');
    name.textContent = source.label;
    const count = document.createElement('span');
    count.className = 'model-source-count';
    const toggle = checkbox(
      'Enable ' + source.label,
      choice.enabled,
      (checked: boolean) => {
        choice.enabled = checked;
        sync();
      },
      'model-source-toggle',
    );
    if (used(source)) toggle.title = 'In use';
    summary.append(name, count);
    details.appendChild(summary);
    section.append(details, toggle);
    const body = document.createElement('div');
    body.className = 'model-source-body';
    details.appendChild(body);
    const actions = document.createElement('div');
    actions.className = 'model-source-actions';
    body.appendChild(actions);
    actions.append(
      button('Select all', () => {
        choice.models = [...new Set([...choice.models, ...(source.models || []).map((model) => model.id)])];
        choice.enabled = !!choice.models.length;
        sync();
      }),
      button('Clear', () => {
        choice.models = choice.models.filter((model) => used(source, model));
        sync();
      }),
    );
    let query = '';
    const search = document.createElement('input');
    search.type = 'search';
    search.placeholder = 'Find models';
    search.setAttribute('aria-label', 'Find ' + source.label + ' models');
    body.appendChild(search);
    search.oninput = () => {
      query = search.value.trim().toLowerCase();
      sync();
    };
    const list = document.createElement('div');
    list.className = 'model-source-models';
    body.appendChild(list);
    let rows: ModelRow[] = [];
    function modelRow(model: EditorModel): ModelRow {
      const label = document.createElement('label');
      label.className = 'model-choice';
      const input = checkbox(
        'Use ' + (model.name || model.id),
        choice.models.includes(model.id),
        (checked: boolean) => {
          choice.models = checked
            ? [...new Set([...choice.models, model.id])]
            : choice.models.filter((id) => id !== model.id);
          if (checked) choice.enabled = true;
          sync();
        },
      );
      input.disabled = used(source, model.id);
      const text = document.createElement('span');
      text.textContent = model.name || model.id;
      label.append(input, text);
      return { label, input, model, text };
    }
    function syncModelRows() {
      const existing = new Map(rows.map((row: ModelRow) => [row.model.id, row]));
      rows = (source.models || []).map((model: EditorModel) => {
        const row = existing.get(model.id) || modelRow(model);
        row.model = model;
        row.text.textContent = model.name || model.id;
        row.input.setAttribute('aria-label', 'Use ' + (model.name || model.id));
        return row;
      });
      const kept = new Set(rows.map((row: ModelRow) => row.label));
      Array.from(list.children).forEach((node) => {
        if (!(node instanceof HTMLLabelElement)) return;
        if (!kept.has(node)) {
          if (node.contains(document.activeElement)) summary.focus({ preventScroll: true });
          node.remove();
        }
      });
      rows.forEach((row: ModelRow, index: number) => {
        if (list.children[index] !== row.label) list.insertBefore(row.label, list.children[index] || null);
      });
      if (!rows.length) {
        const empty = document.createElement('p');
        empty.className = 'settings-hint';
        empty.textContent = 'No models loaded';
        list.appendChild(empty);
      }
      search.hidden = rows.length <= 8 && !query;
    }
    syncModelRows();
    if (source.discoverable) {
      const refresh = button('Refresh models', async () => {
        const request = generation;
        refresh.disabled = true;
        refresh.textContent = 'Refreshing…';
        error('');
        try {
          const response = await fetch('/api/model_catalog', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: source.id }),
          });
          const data = await response.json();
          if (request !== generation) return;
          if (!response.ok) throw new Error(data.error || 'Could not refresh models');
          window.Models.observeCatalog(data.source);
          Object.assign(source, data.source);
          syncModelRows();
          sync();
        } catch (exception) {
          if (request === generation) error(exception instanceof Error ? exception.message : String(exception));
        } finally {
          if (request === generation) {
            refresh.disabled = false;
            refresh.textContent = 'Refresh models';
          }
        }
      });
      refresh.classList.add('model-source-refresh');
      actions.appendChild(refresh);
    }
    if (source.connection_editor) body.appendChild($('local-connection-editor'));
    function sync() {
      if (!choice.models.length) choice.enabled = false;
      toggle.checked = choice.enabled;
      toggle.disabled = used(source) || !choice.models.length;
      count.textContent = choice.models.length + ' selected';
      section.classList.toggle('source-disabled', !choice.enabled);
      rows.forEach(({ label, input, model }) => {
        input.checked = choice.models.includes(model.id);
        input.disabled = used(source, model.id);
        label.hidden =
          !!query && !(model.name || model.id).toLowerCase().includes(query) && !model.id.toLowerCase().includes(query);
      });
      updateSaveState();
    }
    sync();
    return section;
  }
  function render() {
    const list = $('model-source-list');
    const opened = Array.from(list.querySelectorAll<HTMLElement>('.model-source > details[open]'))
      .map((node) => node.parentElement?.dataset.sourceId)
      .filter((id): id is string => !!id);
    // Initial construction keeps the existing connection form and values.
    $('model-settings-body').appendChild($('local-connection-editor'));
    const sources = currentDraft().sources as EditorSource[];
    $('local-connection-editor').hidden = !sources.some((source) => source.connection_editor);
    list.replaceChildren(...sources.map(renderSource));
    opened.forEach((id) => {
      const node = Array.from(list.children).find(
        (child): child is HTMLElement => child instanceof HTMLElement && child.dataset.sourceId === id,
      );
      node?.querySelector('details')?.setAttribute('open', '');
    });
  }
  async function open() {
    const request = ++generation;
    saving = false;
    draft = null;
    $('model-source-list').inert = false;
    error('');
    $('model-settings-save').disabled = true;
    $('model-settings-retry').hidden = true;
    $('model-settings-loading').hidden = false;
    $('model-source-list').hidden = true;
    try {
      const loaded = await window.Models.load();
      if (request !== generation) return;
      draft = loaded;
      baseline = preferenceKey(draft.preferences.sources);
      render();
      $('model-source-list').hidden = false;
      updateSaveState();
    } catch (exception) {
      if (request === generation) {
        error(exception instanceof Error ? exception.message : String(exception));
        $('model-settings-retry').hidden = false;
      }
    } finally {
      if (request === generation) $('model-settings-loading').hidden = true;
    }
  }
  function close() {
    generation++;
    draft = null;
  }
  async function save() {
    if (saving || !draft || preferenceKey(draft.preferences.sources) === baseline) return;
    saving = true;
    const request = generation;
    error('');
    $('model-settings-save').disabled = true;
    $('model-source-list').inert = true;
    try {
      const response = await fetch('/api/model_settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_revision: draft.preferences.revision, sources: draft.preferences.sources }),
      });
      const data = await response.json();
      if (request !== generation) return;
      if (!response.ok) throw new Error(data.error || 'Could not save models');
      window.Models.applySettings(data);
      deps.close();
    } catch (exception) {
      if (request === generation) error(exception instanceof Error ? exception.message : String(exception));
    } finally {
      if (request === generation) {
        saving = false;
        updateSaveState();
        $('model-source-list').inert = false;
      }
    }
  }
  window.ModelSettings = { init, open, close };
})();
