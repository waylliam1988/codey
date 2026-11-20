import { runtime } from './runtime';

export interface LocalContextDeps {
  $(id: string): HTMLElement;
  getActiveId(): string;
  currentProjectPath(): string;
  copyText(text: string): Promise<boolean>;
}
interface Scope {
  session_id: string;
  project: string;
}
interface Item {
  item_id: string;
  kind: string;
  text: string;
  scope: string;
  source_refs: string[];
  slot: string;
  context: string;
  expires_at: string;
}
type ControlName = 'accept_events' | 'retrieve_projections' | 'run_maintenance' | 'admit_hints';
interface Controls {
  accept_events: boolean;
  retrieve_projections: boolean;
  run_maintenance: boolean;
  admit_hints: boolean;
}
interface Proposal {
  proposal_id: string;
  version: string;
  item: Item;
}
interface Summary {
  enabled: boolean;
  scope: Scope;
  items: Item[];
  pending: Proposal[];
  controls: Controls;
  records: { item: Item; state: string; superseded_by: string }[];
  exposures: { item_id: string; run_id: string; at: string }[];
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function record(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) throw new Error('Invalid memory response');
  return value;
}
function string(value: unknown): string {
  if (typeof value !== 'string') throw new Error('Invalid memory field');
  return value;
}
function boolean(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new Error('Invalid memory control');
  return value;
}
function rows(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error('Invalid memory list');
  return value;
}
function item(value: unknown): Item {
  const row = record(value);
  return {
    item_id: string(row.item_id),
    kind: string(row.kind),
    text: string(row.text),
    scope: string(row.scope),
    source_refs: rows(row.source_refs).map(string),
    slot: string(row.slot),
    context: string(row.context),
    expires_at: string(row.expires_at),
  };
}
function summary(value: unknown): Summary {
  const data = record(value);
  if (data.ok !== true || typeof data.enabled !== 'boolean') throw new Error('Memory unavailable');
  const scope = record(data.scope);
  const controls = record(data.controls);
  return {
    enabled: data.enabled,
    scope: { session_id: string(scope.session_id), project: string(scope.project) },
    items: rows(data.items).map(item),
    controls: {
      accept_events: boolean(controls.accept_events),
      retrieve_projections: boolean(controls.retrieve_projections),
      run_maintenance: boolean(controls.run_maintenance),
      admit_hints: boolean(controls.admit_hints),
    },
    records: rows(data.records).map((value) => {
      const row = record(value);
      return { item: item(row.item), state: string(row.state), superseded_by: string(row.superseded_by) };
    }),
    exposures: rows(data.exposures).map((value) => {
      const row = record(value);
      return { item_id: string(row.item_id), run_id: string(row.run_id), at: string(row.at) };
    }),
    pending: rows(data.pending).map((value) => {
      const row = record(value);
      return { proposal_id: string(row.proposal_id), version: string(row.version), item: item(row.item) };
    }),
  };
}

let deps: LocalContextDeps;
let loadedScope: Scope | null = null;
let generation = 0;
let selectedScopeKind = '';
let pendingRequest: { key: string; id: string } | null = null;
function setOpen(open: boolean): void {
  const state: unknown = runtime.UiState;
  if (!isUiState(state)) throw new Error('UI state unavailable');
  state.setDrawerOpen('local-context-drawer', open);
}
function isUiState(value: unknown): value is { setDrawerOpen(id: string, open: boolean): void } {
  return isRecord(value) && typeof value.setDrawerOpen === 'function';
}
function $(id: string): HTMLElement {
  return deps.$('local-context-' + id);
}
function scope(): Scope {
  return { session_id: deps.getActiveId(), project: deps.currentProjectPath() };
}
function sameScope(left: Scope, right: Scope): boolean {
  return left.session_id === right.session_id && left.project === right.project;
}
function isOpen(): boolean {
  const drawer = $('drawer');
  return drawer.classList.contains('open') && !drawer.closest('[hidden]');
}
function close(): void {
  generation++;
  loadedScope = null;
  pendingRequest = null;
  $('body').replaceChildren();
  $('subtitle').textContent = 'Not loaded';
  setOpen(false);
}
function handleScopeChanged(): void {
  if (loadedScope && !sameScope(loadedScope, scope())) close();
}
function button(label: string, action: () => void): HTMLButtonElement {
  const node = document.createElement('button');
  node.type = 'button';
  node.className = 'drawer-btn';
  node.textContent = label;
  node.onclick = action;
  return node;
}
function rowNode(value: Item): HTMLElement {
  const node = document.createElement('div');
  node.className = 'local-context-row';
  const title = document.createElement('small');
  title.textContent =
    value.kind === 'preference'
      ? 'Preference'
      : value.kind === 'goal'
        ? 'Goal'
        : value.kind === 'experience'
          ? 'Past exchange'
          : 'Working assumption';
  const text = document.createElement('p');
  text.textContent = value.text;
  const refs = document.createElement('small');
  refs.textContent = value.source_refs.join(', ');
  const main = document.createElement('div');
  main.className = 'local-context-main';
  refs.className = 'local-context-evidence';
  refs.title = refs.textContent;
  main.append(text, title, refs);
  if (value.expires_at || value.context) {
    const lifetime = document.createElement('small');
    lifetime.textContent = [value.context && `For ${value.context}`, value.expires_at && `Until ${value.expires_at}`]
      .filter(Boolean)
      .join(' · ');
    main.append(lifetime);
  }
  node.append(main);
  return node;
}
function render(data: Summary): void {
  const body = $('body');
  body.replaceChildren();
  $('subtitle').textContent = data.enabled
    ? `${data.items.length} items · ${data.pending.length} pending`
    : 'Local context disabled';
  if (data.enabled) renderSettings(body, data);
  const pending = document.createElement('section');
  const active = document.createElement('section');
  pending.className = active.className = 'local-context-group';
  const pendingTitle = document.createElement('h3');
  pendingTitle.textContent = 'Pending review';
  const activeTitle = document.createElement('h3');
  activeTitle.textContent = data.items.every((value) => value.kind === 'preference')
    ? 'Active preferences'
    : 'Active context';
  pending.append(pendingTitle);
  active.append(activeTitle);
  for (const proposal of data.pending) {
    const node = rowNode(proposal.item);
    const actions = document.createElement('div');
    actions.className = 'local-context-actions';
    for (const action of ['accept', 'reject']) {
      actions.appendChild(
        button(action === 'accept' ? 'Confirm' : 'Reject', () => {
          void postAction(action, {
            proposal_id: proposal.proposal_id,
            version: proposal.version,
            ...(proposal.item.scope === 'user' ? { scope_kind: 'user' } : {}),
          });
        }),
      );
    }
    node.append(actions);
    pending.appendChild(node);
  }
  for (const value of data.items) {
    const node = rowNode(value);
    const owner: Record<string, string> = value.scope === 'user' ? { scope_kind: 'user' } : {};
    node.append(button('Forget item', () => void postAction('revoke', { item_id: value.item_id, ...owner })));
    node.append(button('Pause item', () => void postAction('quarantine', { item_id: value.item_id, ...owner })));
    renderFeedback(node, value, data);
    active.appendChild(node);
  }
  body.append(pending, active);
  if (!data.enabled) return;
  const form = document.createElement('form');
  form.className = 'local-context-proposal';
  const heading = document.createElement('h3');
  heading.textContent = 'Propose an item';
  const kind = document.createElement('select');
  kind.setAttribute('aria-label', 'Item type');
  kind.add(new Option('Preference', 'preference'));
  kind.add(new Option('Goal', 'goal'));
  kind.add(new Option('Working assumption', 'hypothesis'));
  const text = document.createElement('textarea');
  text.setAttribute('aria-label', 'Proposed item');
  text.placeholder = 'What should Codey keep in mind?';
  const submit = button('Submit for review', () => {
    if (text.value.trim()) void postAction('propose', { kind: kind.value, text: text.value.trim() });
  });
  submit.disabled = true;
  text.oninput = () => {
    submit.disabled = !text.value.trim();
  };
  form.onsubmit = (event) => event.preventDefault();
  form.append(heading, kind, text, submit);
  const slot = document.createElement('input');
  slot.placeholder = 'Preference slot, such as language (optional)';
  slot.setAttribute('aria-label', 'Preference slot');
  const context = document.createElement('select');
  context.setAttribute('aria-label', 'Applies during');
  for (const [label, value] of [
    ['Any task', ''],
    ['Chat', 'chat'],
    ['Coding', 'project'],
    ['Planning', 'planning'],
    ['Research', 'research'],
  ])
    context.add(new Option(label, value));
  const expiry = document.createElement('input');
  expiry.type = 'datetime-local';
  expiry.setAttribute('aria-label', 'Expires at');
  const replaces = document.createElement('select');
  replaces.setAttribute('aria-label', 'Replaces item');
  replaces.add(new Option('Keep existing items', ''));
  for (const value of data.items.filter((value) => value.kind !== 'experience'))
    replaces.add(new Option(value.text, value.item_id));
  submit.onclick = () => {
    if (!text.value.trim()) return;
    const fields: Record<string, string | string[]> = { kind: kind.value, text: text.value.trim() };
    if (slot.value.trim()) fields.slot = slot.value.trim();
    if (context.value) fields.context = context.value;
    if (expiry.value) fields.expires_at = new Date(expiry.value).toISOString();
    if (replaces.value) fields.supersedes = [replaces.value];
    void postAction('propose', fields);
  };
  form.insertBefore(slot, submit);
  form.insertBefore(context, submit);
  form.insertBefore(expiry, submit);
  form.insertBefore(replaces, submit);
  body.appendChild(form);
  const history = document.createElement('details');
  const historyLabel = document.createElement('summary');
  historyLabel.textContent = 'Replaced, paused and forgotten items';
  history.append(historyLabel);
  for (const value of data.records.filter((value) => value.state !== 'active')) {
    const node = rowNode(value.item);
    const state = document.createElement('small');
    state.textContent = value.state;
    node.append(state);
    if (value.state === 'revoked' || value.state === 'quarantined') {
      node.append(
        button(
          'Restore item',
          () =>
            void postAction('restore', {
              item_id: value.item.item_id,
              ...(value.item.scope === 'user' ? { scope_kind: 'user' } : {}),
            }),
        ),
      );
    }
    history.append(node);
  }
  body.append(history);
  const confirmation = document.createElement('div');
  confirmation.className = 'local-context-confirmation';
  confirmation.hidden = true;
  const forget = button('Clear local context…', () => {
    confirmation.hidden = false;
    forget.setAttribute('aria-expanded', 'true');
    cancel.focus();
  });
  forget.setAttribute('aria-expanded', 'false');
  const cancel = button('Cancel', () => {
    confirmation.hidden = true;
    forget.setAttribute('aria-expanded', 'false');
    forget.focus();
  });
  const prompt = document.createElement('p');
  prompt.textContent = 'Clear saved items and proposals in this scope?';
  confirmation.append(
    prompt,
    cancel,
    button('Clear local context', () => {
      void postAction('delete_scope');
    }),
  );
  confirmation.onkeydown = (event) => {
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    cancel.click();
  };
  const footer = document.createElement('div');
  footer.className = 'local-context-settings';
  footer.append(forget, confirmation);
  body.append(footer);
}
function renderSettings(body: HTMLElement, data: Summary): void {
  const selector = document.createElement('select');
  selector.setAttribute('aria-label', 'Memory scope');
  selector.add(new Option('Current workspace', ''));
  selector.add(new Option('Across chats', 'user'));
  selector.add(new Option('This chat', 'session'));
  if (scope().project) selector.add(new Option('This project', 'project'));
  selector.value = selectedScopeKind;
  selector.onchange = () => {
    selectedScopeKind = selector.value;
    void load();
  };
  body.append(selector);
  const labels: [ControlName, string][] = [
    ['accept_events', 'Save new experiences'],
    ['retrieve_projections', 'Recall saved context'],
    ['run_maintenance', 'Maintain saved context'],
    ['admit_hints', 'Use context in answers'],
  ];
  for (const [name, text] of labels) {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = data.controls[name];
    input.onchange = () => void postAction('control', { control: name, enabled: input.checked });
    label.append(input, text);
    body.append(label);
  }
  const query = document.createElement('input');
  query.placeholder = 'Preview what Codey would recall';
  query.setAttribute('aria-label', 'Recall query');
  const preview = document.createElement('pre');
  preview.dataset.recallPreview = 'yes';
  body.append(
    query,
    button('Explain recall', () => void postAction('explain', { query: query.value })),
    preview,
  );
}
function renderFeedback(node: HTMLElement, value: Item, data: Summary): void {
  const exposures = data.exposures.filter((row) => row.item_id === value.item_id);
  if (!exposures.length) return;
  const answer = document.createElement('select');
  answer.setAttribute('aria-label', 'Answer to review');
  for (const row of exposures) answer.add(new Option(`Answer from ${row.at}`, row.run_id));
  node.append(answer);
  for (const [label, rating] of [
    ['Helpful', 'helpful'],
    ['Not helpful', 'unhelpful'],
    ['Needs correction', 'correction'],
  ]) {
    node.append(
      button(
        label,
        () =>
          void postAction('feedback', {
            item_id: value.item_id,
            run_id: answer.value,
            rating,
            ...(value.scope === 'user' ? { scope_kind: 'user' } : {}),
          }),
      ),
    );
  }
}
async function load(): Promise<void> {
  const requested = scope();
  const revision = ++generation;
  loadedScope = requested;
  $('subtitle').textContent = 'Loading...';
  try {
    const response = await fetch(
      '/api/ghost/summary?' +
        new URLSearchParams({ ...requested, ...(selectedScopeKind ? { scope_kind: selectedScopeKind } : {}) }),
      { cache: 'no-store' },
    );
    const value: unknown = await response.json();
    const data = summary(value);
    if (!isOpen() || revision !== generation || !sameScope(requested, scope())) return;
    if (!response.ok || !sameScope(requested, data.scope)) throw new Error('Memory scope changed');
    render(data);
  } catch {
    if (!isOpen() || revision !== generation || !sameScope(requested, scope())) return;
    $('subtitle').textContent = 'Memory unavailable';
    $('body').replaceChildren();
  }
}
async function postAction(action: string, payload: Record<string, string | boolean | string[]> = {}): Promise<void> {
  if (!loadedScope || !sameScope(loadedScope, scope())) {
    close();
    return;
  }
  const requested = loadedScope;
  const revision = generation;
  $('subtitle').textContent = 'Updating...';
  const command = { ...requested, ...(selectedScopeKind ? { scope_kind: selectedScopeKind } : {}), action, ...payload };
  const key = JSON.stringify(command);
  if (!pendingRequest || pendingRequest.key !== key) pendingRequest = { key, id: crypto.randomUUID() };
  try {
    const response = await fetch('/api/ghost/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...command, request_id: pendingRequest.id }),
    });
    const decoded: unknown = await response.json();
    const data = record(decoded);
    if (!isOpen() || revision !== generation || !sameScope(requested, scope())) return;
    if (!response.ok || data.ok !== true)
      throw new Error(typeof data.error === 'string' ? data.error : 'Update failed');
    pendingRequest = null;
    if (action === 'explain') {
      const preview = $('body').querySelector('[data-recall-preview]');
      if (preview)
        preview.textContent = rows(data.trace)
          .map((value) => {
            const row = record(value);
            return `${string(row.item_id)}: ${string(row.reason)}`;
          })
          .join('\n');
      $('subtitle').textContent = `Recall preview · ${String(data.used_chars)} characters`;
      return;
    }
    await load();
  } catch (error: unknown) {
    if (isOpen() && revision === generation && sameScope(requested, scope())) {
      $('subtitle').textContent = error instanceof Error ? error.message : 'Update failed';
    }
  }
}
async function exportMemory(): Promise<void> {
  try {
    const response = await fetch('/api/ghost/export', { cache: 'no-store' });
    if (!response.ok) throw new Error('Export failed');
    const value: unknown = await response.json();
    $('export').textContent = (await deps.copyText(JSON.stringify(value, null, 2))) ? 'Copied' : 'Export failed';
  } catch {
    $('export').textContent = 'Export failed';
  }
}
function init(next: LocalContextDeps): void {
  deps = next;
  selectedScopeKind = '';
  pendingRequest = null;
  $('refresh').onclick = () => void load();
  $('export').onclick = () => void exportMemory();
}
function open(): void {
  setOpen(true);
  void load();
}
runtime.LocalContextDrawer = { init, open, close, load, render, handleScopeChanged };
