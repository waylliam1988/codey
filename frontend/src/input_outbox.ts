export interface QueuedRequest {
  session_id: string;
  run_id: string;
  task: string;
  provider: string;
  project?: string;
  intent?: string;
  model_selection?: unknown;
  input_mode?: 'steer' | 'follow_up';
}
export interface QueuedInput {
  id: string;
  revision: number;
  state: string;
  request: QueuedRequest;
  error?: string;
  terminal?: Record<string, unknown>;
  local?: boolean;
  draftRevision?: number;
  rejected?: boolean;
  consumed_by_run_id?: string;
  entry_id?: string;
}
interface OutboxDeps {
  accepted(request: QueuedRequest, revision: number): void;
  edit(session: string, text: string): void;
  changed(): void;
  observe(input: QueuedInput): void;
}
const STORAGE = 'codey:pending-input-delivery';
function inputRecord(value: unknown): value is QueuedInput {
  if (!value || typeof value !== 'object') return false;
  const item = value as QueuedInput;
  return (
    typeof item.id === 'string' &&
    Number.isSafeInteger(item.revision) &&
    typeof item.state === 'string' &&
    !!item.request &&
    typeof item.request.session_id === 'string' &&
    typeof item.request.run_id === 'string' &&
    typeof item.request.task === 'string'
  );
}

export class InputOutbox {
  private items = new Map<string, QueuedInput>();
  private session = '';
  private storageError = '';
  private checking = new Set<string>();
  private editing = new Map<string, { text: string; revision: number }>();
  constructor(
    readonly element: HTMLElement,
    private deps: OutboxDeps,
  ) {
    element.className = 'pending-inputs';
    element.setAttribute('aria-live', 'polite');
    try {
      const stored = JSON.parse(localStorage.getItem(STORAGE) || '[]');
      if (!Array.isArray(stored) || !stored.every(inputRecord)) throw new Error('Invalid pending delivery state');
      stored.forEach((item) =>
        this.items.set(item.id, {
          ...item,
          state: item.rejected ? 'failed' : 'unconfirmed',
          local: true,
        }),
      );
    } catch {
      this.storageError = 'Pending delivery state could not be read. Drafts are retained.';
    }
  }
  setSession(session: string): void {
    this.session = session;
    this.render();
  }
  forRun(runId: string): QueuedInput | undefined {
    return [...this.items.values()].find((item) => item.request.run_id === runId);
  }
  blocked(session: string): boolean {
    return (
      !!this.storageError ||
      [...this.items.values()].some((item) => item.request.session_id === session && item.local && !item.rejected)
    );
  }
  private persist(): void {
    localStorage.setItem(STORAGE, JSON.stringify([...this.items.values()].filter((item) => item.local)));
  }

  apply(value: unknown): boolean {
    if (!inputRecord(value)) return false;
    const old = this.items.get(value.id);
    if (old && old.revision >= value.revision && !old.local) return false;
    const item = { ...value, local: false };
    this.items.set(item.id, item);
    if (old?.local) {
      this.persist();
      this.deps.accepted(item.request, old.draftRevision || 0);
    }
    this.deps.observe(item);
    this.render();
    this.deps.changed();
    return true;
  }

  async enqueue(request: QueuedRequest, draftRevision: number): Promise<boolean> {
    if (this.blocked(request.session_id)) return false;
    const item: QueuedInput = {
      id: 'input_' + crypto.randomUUID().replaceAll('-', ''),
      revision: 0,
      state: 'sending',
      request,
      local: true,
      draftRevision,
    };
    this.items.set(item.id, item);
    try {
      this.persist();
    } catch {
      this.items.delete(item.id);
      this.storageError = 'Could not save pending delivery identity. The message was not sent.';
      this.render();
      this.deps.changed();
      return false;
    }
    this.render();
    this.deps.changed();
    try {
      const response = await fetch('/api/inputs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'create', id: item.id, request }),
      });
      const data = await response.json();
      if (!response.ok || !inputRecord(data.item)) {
        if (response.status === 409 && data.error === 'input_queue_full') {
          item.state = 'failed';
          item.rejected = true;
          item.error = data.error;
          this.persist();
          this.render();
          this.deps.changed();
          return false;
        }
        throw new Error(data.error || 'Delivery could not be confirmed');
      }
      this.apply(data.item);
      return true;
    } catch (error) {
      // A push may have confirmed this identity while its POST reply was lost.
      if (!this.items.get(item.id)?.local) return true;
      item.state = 'unconfirmed';
      item.error = error instanceof Error ? error.message : 'Delivery could not be confirmed';
      this.persist();
      this.render();
      this.deps.changed();
      return false;
    }
  }

  async refresh(): Promise<void> {
    try {
      const response = await fetch('/api/inputs');
      const data = await response.json();
      if (!response.ok || !Array.isArray(data.items)) return;
      data.items.forEach((item: unknown) => this.apply(item));
    } catch {
      /* Pending identities are retained until an explicit status check. */
    }
  }
  async check(identity: string): Promise<void> {
    if (this.checking.has(identity)) return;
    this.checking.add(identity);
    this.render();
    try {
      const response = await fetch('/api/inputs?id=' + encodeURIComponent(identity)),
        data = await response.json();
      if (!response.ok || !inputRecord(data.item)) {
        const item = this.items.get(identity);
        if (response.status === 404 && data.error === 'input_not_found' && item?.local) {
          item.state = 'failed';
          item.rejected = true;
          item.error = data.error;
          this.persist();
          return;
        }
        throw new Error(data.error || 'Status could not be confirmed');
      }
      this.apply(data.item);
    } catch (error) {
      const item = this.items.get(identity);
      if (item) item.error = error instanceof Error ? error.message : 'Status could not be confirmed';
    } finally {
      this.checking.delete(identity);
      this.render();
    }
  }
  private async action(
    item: QueuedInput,
    action: 'edit' | 'remove' | 'resume' | 'steer',
    text?: string,
  ): Promise<void> {
    if (this.checking.has(item.id)) return;
    if (item.rejected) {
      this.items.delete(item.id);
      this.persist();
      if (action === 'edit') this.deps.edit(item.request.session_id, item.request.task);
      this.render();
      this.deps.changed();
      return;
    }
    this.checking.add(item.id);
    this.render();
    try {
      const response = await fetch('/api/inputs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action,
          id: item.id,
          revision: action === 'edit' ? this.editing.get(item.id)?.revision : item.revision,
          ...(action === 'edit' ? { text } : {}),
        }),
      });
      const data = await response.json();
      if (!response.ok || !inputRecord(data.item)) throw new Error(data.error || 'Input could not be changed');
      if (action === 'edit') this.editing.delete(item.id);
      this.apply(data.item);
    } catch (error) {
      // Check the same identity after an uncertain response. Never resubmit
      // the message as a new task; keep any unsaved edit available.
      this.checking.delete(item.id);
      await this.check(item.id);
      const current = this.items.get(item.id);
      if (current) {
        if (action === 'edit' && current.request.task === text && current.revision > item.revision)
          this.editing.delete(item.id);
        else if (
          (action === 'steer' && current.request.input_mode === 'steer') ||
          (action === 'remove' && current.state === 'cancelled')
        )
          delete current.error;
        else {
          current.error = error instanceof Error ? error.message : 'Input could not be changed';
          const edit = this.editing.get(item.id);
          if (edit && current.state === 'queued') edit.revision = current.revision;
        }
      }
    } finally {
      this.checking.delete(item.id);
      this.render();
    }
  }
  private render(): void {
    const focused =
      document.activeElement instanceof HTMLTextAreaElement && this.element.contains(document.activeElement)
        ? {
            id: document.activeElement.closest<HTMLElement>('[data-input-id]')?.dataset.inputId,
            start: document.activeElement.selectionStart,
            end: document.activeElement.selectionEnd,
          }
        : undefined;
    this.element.replaceChildren();
    if (this.storageError) {
      this.element.textContent = this.storageError;
      this.element.hidden = false;
      return;
    }
    for (const item of this.items.values()) {
      const edit = this.editing.get(item.id);
      if (
        item.request.session_id !== this.session ||
        (!edit && ['submitted', 'completed', 'cancelled', 'consumed'].includes(item.state))
      )
        continue;
      const row = document.createElement('div');
      row.className = 'pending-input';
      const label = document.createElement('div');
      label.className = 'pending-input-state';
      label.textContent = this.checking.has(item.id)
        ? 'Checking…'
        : edit && item.state !== 'queued' && !item.rejected
          ? 'Message already received. Your unsaved edit is retained.'
          : (
              {
                queued: item.request.input_mode === 'steer' ? 'Steer at next boundary' : 'Next turn',
                consumed: 'Applied · current task',
                sending: 'Queuing…',
                dispatching: 'Sending…',
                unconfirmed: 'Delivery unconfirmed',
                paused: 'Paused · not sent',
                failed: 'Not sent',
              } as Record<string, string>
            )[item.state] || item.state;
      const text = document.createElement('div');
      text.className = 'pending-input-text';
      text.textContent = item.request.task;
      const actions = document.createElement('div');
      actions.className = 'pending-input-actions';
      const choices: Array<[string, string]> =
        item.state === 'unconfirmed'
          ? [['check', 'Check status']]
          : ['queued', 'paused', 'failed'].includes(item.state)
            ? [
                ...(item.state === 'paused' ? [['resume', 'Resume'] as [string, string]] : []),
                ['edit', item.rejected ? 'Return to draft' : 'Edit queued message'],
                ['remove', 'Delete queued message'],
                ...(item.state === 'queued' && item.request.input_mode !== 'steer'
                  ? [['steer', 'Steer'] as [string, string]]
                  : []),
              ]
            : [];
      for (const [action, title] of edit
        ? [
            ['save', 'Save'],
            ['cancel-edit', 'Cancel'],
            ...(item.state !== 'queued' ? [['restore', 'Return to draft']] : []),
          ]
        : choices) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'pending-input-action';
        button.dataset.action = action;
        button.setAttribute('aria-label', title);
        button.title = title;
        if (!edit && !item.rejected && (action === 'edit' || action === 'remove'))
          button.append(createIconSvg(action === 'edit' ? 'edit' : 'trash'));
        else button.textContent = title;
        button.disabled = this.checking.has(item.id) || (action === 'save' && item.state !== 'queued');
        button.onclick = () => {
          if (action === 'edit' && !item.rejected) {
            this.editing.set(item.id, { text: item.request.task, revision: item.revision });
            this.render();
            Array.from(this.element.querySelectorAll<HTMLElement>('[data-input-id]'))
              .find((row) => row.dataset.inputId === item.id)
              ?.querySelector('textarea')
              ?.focus();
          } else if (action === 'cancel-edit' || action === 'restore') {
            if (action === 'restore' && edit) this.deps.edit(item.request.session_id, edit.text);
            this.editing.delete(item.id);
            this.render();
          } else if (action === 'save' && edit) {
            if (edit.text.trim()) void this.action(item, 'edit', edit.text.trim());
          } else if (action === 'check') void this.check(item.id);
          else void this.action(item, action as 'edit' | 'remove' | 'resume' | 'steer');
        };
        actions.append(button);
      }
      row.dataset.inputId = item.id;
      if (item.state !== 'queued' || item.request.input_mode === 'steer' || edit) row.append(label);
      else row.setAttribute('aria-label', 'Queued for the next turn');
      if (edit) {
        const input = document.createElement('textarea');
        input.className = 'pending-input-editor';
        input.setAttribute('aria-label', 'Edit queued message');
        input.value = edit.text;
        input.disabled = this.checking.has(item.id);
        input.oninput = () => {
          edit.text = input.value;
        };
        row.append(input);
      } else if (item.state !== 'unconfirmed') row.append(text);
      row.append(actions);
      if (item.error) {
        const details = document.createElement('details'),
          head = document.createElement('summary'),
          error = document.createElement('pre');
        head.textContent = 'Details';
        error.textContent = item.error;
        details.append(head, error);
        row.append(details);
      }
      this.element.append(row);
    }
    this.element.hidden = !this.element.childNodes.length;
    if (focused?.id) {
      const editor = Array.from(this.element.querySelectorAll<HTMLElement>('[data-input-id]'))
        .find((row) => row.dataset.inputId === focused.id)
        ?.querySelector('textarea');
      editor?.focus();
      editor?.setSelectionRange(focused.start, focused.end);
    }
  }
}
import { createIconSvg } from './icon_button';
