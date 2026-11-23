import { runtime } from './runtime';

export interface LocalContextDeps {
  $(id: string): HTMLElement;
  getActiveId(): string;
  currentProjectPath(): string;
  copyText(text: string): Promise<boolean>;
}
export interface MessageSelection {
  sessionId: string;
  entryId: string;
  quote: string;
  start: number;
  end: number;
}

export function messageSelectionOffsets(
  raw: string,
  body: Element,
  range: Range,
  quote: string,
): Pick<MessageSelection, 'start' | 'end'> | null {
  if (!quote.trim() || !body.contains(range.startContainer) || !body.contains(range.endContainer)) return null;
  let position = raw.indexOf(quote);
  if (position < 0) return null;
  if (raw.indexOf(quote, position + 1) >= 0) {
    if (body.textContent !== raw) return null;
    const prefix = range.cloneRange();
    prefix.selectNodeContents(body);
    prefix.setEnd(range.startContainer, range.startOffset);
    position = prefix.toString().length;
    if (raw.slice(position, position + quote.length) !== quote) return null;
  }
  const start = Array.from(raw.slice(0, position)).length;
  const end = start + Array.from(quote).length;
  return Array.from(raw).slice(start, end).join('') === quote ? { start, end } : null;
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
  value: string;
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
  relations: { target_id: string; relation: string; source_refs: string[] }[];
}
interface Summary {
  enabled: boolean;
  scope: Scope;
  items: Item[];
  pending: Proposal[];
  controls: Controls;
  records: { item: Item; state: string; superseded_by: string }[];
  exposures: { id: string; item_id: string; run_id: string; at: string; source_ref: string }[];
  behaviors: { id: string; run_id: string; request_ref: string; kind: string; value: string }[];
  predictions: { statement: string; status: string; due_at: string }[];
  families: { family: string; consumer: string; state: string; scope: string }[];
  familyOptions: string[];
  familyStates: string[];
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
    value: string(row.value ?? ''),
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
      return {
        id: string(row.id ?? ''),
        item_id: string(row.item_id),
        run_id: string(row.run_id),
        at: string(row.at),
        source_ref: string(row.source_ref ?? ''),
      };
    }),
    behaviors: rows(data.behaviors ?? []).map((value) => {
      const row = record(value);
      return {
        id: string(row.id),
        run_id: string(row.run_id),
        request_ref: string(row.request_ref),
        kind: string(row.kind),
        value: string(row.value),
      };
    }),
    predictions: rows(data.predictions ?? []).map((value) => {
      const row = record(value);
      return { statement: string(row.statement), status: string(row.status), due_at: string(row.due_at) };
    }),
    families: rows(data.families ?? []).map((value) => {
      const row = record(value);
      return {
        family: string(row.family),
        consumer: string(row.consumer),
        state: string(row.state),
        scope: string(row.scope),
      };
    }),
    familyOptions: rows(data.family_options ?? []).map(string),
    familyStates: rows(data.family_states ?? []).map(string),
    pending: rows(data.pending).map((value) => {
      const row = record(value);
      return {
        proposal_id: string(row.proposal_id),
        version: string(row.version),
        item: item(row.item),
        relations: rows(row.relations ?? []).map((value) => {
          const edge = record(value);
          return {
            target_id: string(edge.target_id),
            relation: string(edge.relation),
            source_refs: rows(edge.source_refs).map(string),
          };
        }),
      };
    }),
  };
}

let deps: LocalContextDeps;
let loadedScope: Scope | null = null;
let generation = 0;
let selectedScopeKind = '';
let selectedMessage: MessageSelection | null = null;
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
  selectedMessage = null;
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
    for (const edge of proposal.relations) {
      const relation = document.createElement('p');
      const target = data.records.find((row) => row.item.item_id === edge.target_id);
      relation.textContent = `${edge.relation}: ${target?.item.text ?? edge.target_id}. Sources: ${edge.source_refs.join(', ')}`;
      node.append(relation);
      if (edge.relation === 'conflicting' || edge.relation === 'suspected_conflict') {
        node.append(
          button(
            'Confirm and replace this item',
            () =>
              void postAction('accept', {
                proposal_id: proposal.proposal_id,
                version: proposal.version,
                supersedes: [edge.target_id],
                ...(proposal.item.scope === 'user' ? { scope_kind: 'user' } : {}),
              }),
          ),
        );
      }
    }
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
    if (proposal.item.kind === 'preference' && proposal.item.scope !== 'user') {
      actions.append(
        button('Save as cross-project preference', () => {
          void postAction('confirm_user_preference', {
            proposal_id: proposal.proposal_id,
            version: proposal.version,
            text: proposal.item.text,
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
  const selection = selectedMessage;
  heading.textContent = selection ? 'Remember selected text' : 'Propose an item';
  const kind = document.createElement('select');
  kind.setAttribute('aria-label', 'Item type');
  kind.add(new Option('Preference', 'preference'));
  kind.add(new Option('Goal', 'goal'));
  kind.add(new Option('Working assumption', 'hypothesis'));
  const text = document.createElement('textarea');
  text.setAttribute('aria-label', 'Proposed item');
  text.placeholder = 'What should Codey keep in mind?';
  if (selection) text.value = selection.quote;
  const submit = button('Submit for review', () => {});
  submit.disabled = !text.value.trim();
  text.oninput = () => {
    submit.disabled = !text.value.trim();
  };
  form.onsubmit = (event) => event.preventDefault();
  form.append(heading);
  if (selection) {
    const quote = document.createElement('blockquote');
    quote.textContent = selection.quote;
    form.append(quote);
  }
  form.append(kind, text, submit);
  const slot = document.createElement('input');
  slot.placeholder = 'Preference slot, such as language (optional)';
  slot.setAttribute('aria-label', 'Preference slot');
  const normalized = document.createElement('input');
  normalized.placeholder = 'Normalized value, such as es, brief or python';
  normalized.setAttribute('aria-label', 'Normalized preference value');
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
    if (normalized.value.trim()) fields.value = normalized.value.trim();
    if (context.value) fields.context = context.value;
    if (expiry.value) fields.expires_at = new Date(expiry.value).toISOString();
    if (replaces.value) fields.supersedes = [replaces.value];
    if (selection) {
      void postAction('propose_from_message', {
        ...fields,
        entry_id: selection.entryId,
        quote: selection.quote,
        start: selection.start,
        end: selection.end,
      });
    } else void postAction('propose', fields);
  };
  form.insertBefore(slot, submit);
  form.insertBefore(normalized, submit);
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
  renderGovernance(body, data);
}

function renderGovernance(body: HTMLElement, data: Summary): void {
  if (!data.familyOptions.length) return;
  const panel = document.createElement('details');
  const title = document.createElement('summary');
  title.textContent = 'Memory categories and storage';
  panel.append(title);
  for (const value of data.families) {
    const label = document.createElement('p');
    label.textContent = `${value.family} · ${value.consumer}: ${value.state} (${value.scope})`;
    panel.append(label);
  }
  for (const value of data.predictions) {
    const label = document.createElement('p');
    label.textContent = `Prediction: ${value.statement} · ${value.status} · due ${value.due_at}`;
    panel.append(label);
  }
  const family = document.createElement('select');
  family.setAttribute('aria-label', 'Memory category');
  for (const value of data.familyOptions) family.add(new Option(value, value));
  const consumer = document.createElement('select');
  consumer.setAttribute('aria-label', 'Memory consumer');
  for (const value of ['chat', 'project', 'research']) consumer.add(new Option(value, value));
  const state = document.createElement('select');
  state.setAttribute('aria-label', 'Category state');
  for (const value of data.familyStates) state.add(new Option(value, value));
  const references = document.createElement('input');
  references.placeholder = 'Registered evaluation IDs, separated by commas';
  references.setAttribute('aria-label', 'Evaluation references');
  panel.append(
    family,
    consumer,
    state,
    references,
    button(
      'Apply category state',
      () =>
        void postAction('family', {
          family: family.value,
          consumer: consumer.value,
          state: state.value,
          evaluation_refs: references.value
            .split(',')
            .map((value) => value.trim())
            .filter(Boolean),
        }),
    ),
  );
  const report = document.createElement('textarea');
  report.setAttribute('aria-label', 'Frozen evaluation report');
  report.placeholder = 'Paste a frozen evaluation report JSON';
  panel.append(
    report,
    button('Register evaluation', () => {
      try {
        const value: unknown = JSON.parse(report.value);
        void postAction('evaluate', { report: record(value) });
      } catch {
        report.setCustomValidity('A JSON object is required');
        report.reportValidity();
      }
    }),
  );
  const note = document.createElement('p');
  note.textContent =
    'Apply retention limits and create a recovery snapshot. Core conversations remain under their own deletion controls.';
  panel.append(
    note,
    button('Apply storage retention', () => void postAction('compact')),
  );
  body.append(panel);
}
function renderSettings(body: HTMLElement, data: Summary): void {
  const selector = document.createElement('select');
  selector.setAttribute('aria-label', 'Memory scope');
  selector.add(new Option('Current workspace', ''));
  if (!selectedMessage) selector.add(new Option('Across chats', 'user'));
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
  const shadow = document.createElement('input');
  shadow.type = 'checkbox';
  shadow.setAttribute('aria-label', 'Include categories under evaluation');
  const shadowLabel = document.createElement('label');
  shadowLabel.append(shadow, 'Include categories under evaluation in this preview');
  body.append(
    query,
    shadowLabel,
    button('Explain recall', () => void postAction('explain', { query: query.value, shadow: shadow.checked })),
    preview,
  );
}
function renderFeedback(node: HTMLElement, value: Item, data: Summary): void {
  const exposures = data.exposures.filter((row) => row.item_id === value.item_id);
  if (!exposures.length) return;
  const answer = document.createElement('select');
  answer.setAttribute('aria-label', 'Answer to review');
  for (const row of exposures) answer.add(new Option(`Answer from ${row.at}`, row.id || row.run_id));
  const behavior = document.createElement('select');
  behavior.setAttribute('aria-label', 'Observed behavior');
  const refreshBehavior = () => {
    behavior.replaceChildren(new Option('Whole answer: exposure only', ''));
    const selected = exposures.find((row) => (row.id || row.run_id) === answer.value);
    for (const row of data.behaviors.filter(
      (row) => row.run_id === selected?.run_id && row.request_ref === selected.source_ref,
    ))
      behavior.add(new Option(`${row.kind}: ${row.value}`, row.id));
  };
  answer.onchange = refreshBehavior;
  refreshBehavior();
  const correction = document.createElement('textarea');
  correction.setAttribute('aria-label', 'Correction in your own words');
  correction.placeholder = 'Optional correction, e.g. In this project, use Go for code examples.';
  correction.maxLength = 1200;
  node.append(answer, behavior, correction);
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
            run_id: exposures.find((row) => (row.id || row.run_id) === answer.value)?.run_id ?? '',
            exposure_id: exposures.find((row) => (row.id || row.run_id) === answer.value)?.id ?? '',
            behavior_id: behavior.value,
            rating,
            ...(rating === 'correction' && correction.value.trim() ? { correction_text: correction.value } : {}),
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
async function postAction(action: string, payload: Record<string, unknown> = {}): Promise<void> {
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
    if (action === 'propose_from_message') selectedMessage = null;
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
  selectedMessage = null;
  $('refresh').onclick = () => void load();
  $('export').onclick = () => void exportMemory();
}
function open(): void {
  setOpen(true);
  void load();
}
function openSelection(selection: MessageSelection): void {
  if (deps.getActiveId() !== selection.sessionId) return;
  selectedMessage = selection;
  selectedScopeKind = scope().project ? 'project' : 'session';
  open();
}
runtime.LocalContextDrawer = { init, open, openSelection, close, load, render, handleScopeChanged };
