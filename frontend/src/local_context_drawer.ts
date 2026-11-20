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
  };
}
function summary(value: unknown): Summary {
  const data = record(value);
  if (data.ok !== true || typeof data.enabled !== 'boolean') throw new Error('Memory unavailable');
  const scope = record(data.scope);
  return {
    enabled: data.enabled,
    scope: { session_id: string(scope.session_id), project: string(scope.project) },
    items: rows(data.items).map(item),
    pending: rows(data.pending).map((value) => {
      const row = record(value);
      return { proposal_id: string(row.proposal_id), version: string(row.version), item: item(row.item) };
    }),
  };
}

let deps: LocalContextDeps;
let loadedScope: Scope | null = null;
let generation = 0;
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
    value.kind === 'preference' ? 'Preference' : value.kind === 'goal' ? 'Goal' : 'Working assumption';
  const text = document.createElement('p');
  text.textContent = value.text;
  const refs = document.createElement('small');
  refs.textContent = value.source_refs.join(', ');
  const main = document.createElement('div');
  main.className = 'local-context-main';
  refs.className = 'local-context-evidence';
  refs.title = refs.textContent;
  main.append(text, title, refs);
  node.append(main);
  return node;
}
function render(data: Summary): void {
  const body = $('body');
  body.replaceChildren();
  $('subtitle').textContent = data.enabled
    ? `${data.items.length} items · ${data.pending.length} pending`
    : 'Local context disabled';
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
          void postAction(action, { proposal_id: proposal.proposal_id, version: proposal.version });
        }),
      );
    }
    node.append(actions);
    pending.appendChild(node);
  }
  for (const value of data.items) active.appendChild(rowNode(value));
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
  body.appendChild(form);
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
async function load(): Promise<void> {
  const requested = scope();
  const revision = ++generation;
  loadedScope = requested;
  $('subtitle').textContent = 'Loading...';
  try {
    const response = await fetch('/api/ghost/summary?' + new URLSearchParams({ ...requested }), { cache: 'no-store' });
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
async function postAction(action: string, payload: Record<string, string> = {}): Promise<void> {
  if (!loadedScope || !sameScope(loadedScope, scope())) {
    close();
    return;
  }
  const requested = loadedScope;
  const revision = generation;
  $('subtitle').textContent = 'Updating...';
  try {
    const response = await fetch('/api/ghost/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...requested, action, ...payload }),
    });
    const decoded: unknown = await response.json();
    const data = record(decoded);
    if (!isOpen() || revision !== generation || !sameScope(requested, scope())) return;
    if (!response.ok || data.ok !== true)
      throw new Error(typeof data.error === 'string' ? data.error : 'Update failed');
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
  $('refresh').onclick = () => void load();
  $('export').onclick = () => void exportMemory();
}
function open(): void {
  setOpen(true);
  void load();
}
runtime.LocalContextDrawer = { init, open, close, load, render, handleScopeChanged };
