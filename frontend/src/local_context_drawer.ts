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
  runtime.UiState.setDrawerOpen('local-context-drawer', false);
}
function handleScopeChanged(): void {
  if (loadedScope && !sameScope(loadedScope, scope())) close();
}
function button(label: string, action: () => void): HTMLButtonElement {
  const node = document.createElement('button');
  node.type = 'button';
  node.textContent = label;
  node.onclick = action;
  return node;
}
function rowNode(value: Item): HTMLElement {
  const node = document.createElement('div');
  node.className = 'local-context-row';
  const title = document.createElement('strong');
  title.textContent = value.kind;
  const text = document.createElement('p');
  text.textContent = value.text;
  const refs = document.createElement('small');
  refs.textContent = value.source_refs.join(', ');
  node.append(title, text, refs);
  return node;
}
function render(data: Summary): void {
  const body = $('body');
  body.replaceChildren();
  $('subtitle').textContent = data.enabled
    ? `${data.items.length} memories · ${data.pending.length} pending`
    : 'Memory disabled';
  for (const proposal of data.pending) {
    const node = rowNode(proposal.item);
    for (const action of ['accept', 'reject']) {
      node.appendChild(
        button(action === 'accept' ? 'Confirm' : 'Reject', () => {
          void postAction(action, { proposal_id: proposal.proposal_id, version: proposal.version });
        }),
      );
    }
    body.appendChild(node);
  }
  for (const value of data.items) body.appendChild(rowNode(value));
  if (!data.enabled) return;
  const form = document.createElement('form');
  const kind = document.createElement('select');
  kind.setAttribute('aria-label', 'Memory kind');
  for (const value of ['preference', 'goal', 'hypothesis']) kind.add(new Option(value, value));
  const text = document.createElement('textarea');
  text.setAttribute('aria-label', 'Proposed memory');
  const submit = button('Propose memory', () => {
    if (text.value.trim()) void postAction('propose', { kind: kind.value, text: text.value.trim() });
  });
  form.onsubmit = (event) => event.preventDefault();
  form.append(kind, text, submit);
  body.appendChild(form);
  const forget = button('Delete memories in this scope', () => {
    if (forget.dataset.confirm === 'yes') {
      void postAction('delete_scope');
    } else {
      forget.dataset.confirm = 'yes';
      forget.textContent = 'Confirm deletion';
    }
  });
  body.appendChild(forget);
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
  runtime.UiState.setDrawerOpen('local-context-drawer', true);
  void load();
}
runtime.LocalContextDrawer = { init, open, close, load, render, handleScopeChanged };
