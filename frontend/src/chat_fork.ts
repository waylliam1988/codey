import { requestJson, jsonBody } from './api/transport';
import { iconButton, createIconSvg } from './icon_button';

interface ForkResult {
  session_id: string;
  state?: Record<string, unknown>;
  job?: { status: string; error?: string };
}
interface Metadata extends Partial<ForkResult> {
  worktree?: boolean;
  is_worktree?: boolean;
  origin?: { session_id: string; entry_id: string } | null;
}
interface Deps {
  activeId(): string;
  isWorktree(id: string): boolean;
  workspaceChanged(): void;
  originRendered?(): void;
  saved(): Promise<boolean>;
  state(): Record<string, unknown>;
  adopt(
    result: { session_id: string; state: Record<string, unknown> },
    baseline: Record<string, unknown>,
    follow?: boolean,
  ): void;
  switchSession(id: string): void;
}

/** Message-local branching; history management remains a separate workspace view. */
export class ChatFork {
  readonly dialog = document.createElement('dialog');
  private notice = document.createElement('p');
  private options = document.createElement('div');
  private origin = new Map<string, Metadata>();
  private generation = 0;
  private submitting = false;
  private trigger: HTMLElement | null = null;
  private target: { session: string; entry: string } | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private statusTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private disposed = false;

  dispose(): void {
    this.disposed = true;
    this.close();
    for (const timer of this.statusTimers.values()) clearTimeout(timer);
    this.statusTimers.clear();
    this.dialog.remove();
  }

  isBlocked(session: string): boolean {
    const metadata = this.origin.get(session);
    return metadata ? !!metadata.job && metadata.job.status !== 'ready' : this.deps.isWorktree(session);
  }

  constructor(private deps: Deps) {
    this.dialog.className = 'chat-fork-dialog';
    this.dialog.setAttribute('aria-labelledby', 'chat-fork-title');
    const heading = document.createElement('h2');
    heading.id = 'chat-fork-title';
    heading.textContent = 'Fork chat from here';
    this.notice.className = 'chat-fork-notice';
    this.notice.setAttribute('role', 'status');
    this.dialog.append(heading, this.options, this.notice);
    this.dialog.addEventListener('click', (event) => {
      if (event.target !== this.dialog) return;
      const rect = this.dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        this.close();
    });
    this.dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      this.close();
    });
    this.dialog.addEventListener('close', () => {
      this.trigger?.focus({ preventScroll: true });
    });
    document.body.append(this.dialog);
  }

  private close(): void {
    this.generation++;
    clearTimeout(this.timer);
    if (this.dialog.open) this.dialog.close();
    this.submitting = false;
  }

  async open(session: string, entry: string): Promise<void> {
    if (!session || !entry || this.submitting) return;
    this.trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const generation = ++this.generation;
    this.target = { session, entry };
    this.notice.textContent = 'Checking workspace…';
    this.options.replaceChildren();
    this.dialog.showModal();
    try {
      const metadata = await requestJson<Metadata>(
        `/api/conversation_fork?${new URLSearchParams({ session_id: session, capabilities: '1' })}`,
      );
      if (generation !== this.generation) return;
      this.notice.textContent = '';
      if (!metadata.worktree) {
        await this.create('local');
        return;
      }
      this.options.append(
        this.destination(
          metadata.is_worktree ? 'Fork in this worktree' : 'Fork in this workspace',
          metadata.is_worktree
            ? 'Fork from this message in the same worktree'
            : 'Fork from this message in the current workspace',
          'local',
        ),
      );
      this.options.append(
        this.destination('Fork in a new worktree', 'Fork from this message in a new worktree', 'worktree'),
      );
    } catch (error) {
      if (generation === this.generation) this.failure(error, () => void this.open(session, entry));
    }
  }

  private destination(label: string, description: string, destination: 'local' | 'worktree'): HTMLButtonElement {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chat-fork-destination';
    const text = document.createElement('span'),
      title = document.createElement('strong'),
      detail = document.createElement('small');
    title.textContent = label;
    detail.textContent = description;
    text.append(title, detail);
    button.append(createIconSvg('branch'), text);
    button.onclick = () => void this.create(destination);
    return button;
  }

  private failure(error: unknown, retry: () => void): void {
    this.submitting = false;
    this.options.querySelectorAll<HTMLButtonElement>('button').forEach((button) => {
      button.disabled = false;
    });
    this.notice.replaceChildren(
      document.createTextNode(`${error instanceof Error ? error.message : 'Could not create chat.'} `),
    );
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Retry';
    button.onclick = retry;
    this.notice.append(button);
  }

  private async create(destination: 'local' | 'worktree'): Promise<void> {
    const target = this.target;
    if (!target || this.submitting) return;
    this.submitting = true;
    const generation = this.generation;
    this.options.querySelectorAll<HTMLButtonElement>('button').forEach((button) => {
      button.disabled = true;
    });
    let baseline: Record<string, unknown>;
    const key = `codey:message-fork:${target.session}:${target.entry}:${destination}`;
    try {
      if (!(await this.deps.saved())) throw new Error('Save this chat before creating a fork.');
      if (generation !== this.generation) return;
      baseline = this.deps.state();
      const identity = sessionStorage.getItem(key) || crypto.randomUUID();
      sessionStorage.setItem(key, identity);
      this.notice.textContent = destination === 'worktree' ? 'Creating worktree…' : 'Creating chat…';
      const result = await requestJson<ForkResult>('/api/conversation_tree/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: jsonBody({
          action: 'fork',
          session_id: target.session,
          entry_id: target.entry,
          boundary: 'turn',
          destination,
          request_id: identity,
        }),
      });
      const finish = (created: ForkResult) => {
        if (!created.state) throw new Error('Fork setup did not return the new chat.');
        sessionStorage.removeItem(key);
        // A late accepted result still belongs in the real sidebar; adopt preserves
        // local edits and does not steal a different chat the user opened meanwhile.
        this.origin.set(created.session_id, { job: created.job });
        this.deps.adopt(
          { session_id: created.session_id, state: created.state },
          baseline,
          generation === this.generation,
        );
        this.origin.delete(created.session_id);
        void this.showOrigin(created.session_id);
        if (generation === this.generation) {
          this.close();
          if (this.deps.activeId() === created.session_id && created.job?.status !== 'pending')
            document.getElementById('task')?.focus({ preventScroll: true });
        }
      };
      if (result.state) {
        finish(result);
        return;
      }
      const poll = async () => {
        try {
          const status = await requestJson<Metadata>(
            `/api/conversation_fork?${new URLSearchParams({ session_id: result.session_id })}`,
          );
          if (status.job?.status === 'ready' && status.state) {
            finish({ session_id: result.session_id, state: status.state });
            return;
          }
          if (status.job?.status === 'error') throw new Error(status.job.error || 'Worktree setup did not finish.');
          if (generation === this.generation) this.timer = setTimeout(() => void poll(), 700);
        } catch (error) {
          if (generation === this.generation) this.failure(error, () => void this.create(destination));
        }
      };
      await poll();
    } catch (error) {
      if (generation === this.generation) this.failure(error, () => void this.create(destination));
    }
  }

  async showOrigin(session: string): Promise<void> {
    if (this.disposed) return;
    const wasBlocked = this.isBlocked(session);
    let metadata = this.origin.get(session);
    if (!metadata || (metadata.job && metadata.job.status === 'pending')) {
      try {
        metadata = await requestJson<Metadata>(
          `/api/conversation_fork?${new URLSearchParams({ session_id: session })}`,
        );
        this.origin.set(session, metadata);
      } catch {
        if (this.deps.activeId() === session && this.deps.isWorktree(session)) {
          const unavailable: Metadata = {
            job: { status: 'error', error: 'Could not check worktree setup. Retry to reconnect.' },
          };
          this.origin.set(session, unavailable);
          const chat = document.getElementById('chat');
          if (chat) this.renderSetup(session, unavailable, chat);
        }
        return;
      }
    }
    if (this.disposed || this.deps.activeId() !== session) return;
    const chat = document.getElementById('chat');
    if (!chat) return;
    this.renderSetup(session, metadata, chat);
    if (wasBlocked && !this.isBlocked(session) && document.activeElement === document.body)
      document.getElementById('task')?.focus({ preventScroll: true });
    if (!metadata.origin || chat.querySelector('.chat-fork-origin')) return;
    let boundary =
      [...chat.querySelectorAll<HTMLElement>('[data-entry-id]')].find(
        (node) => node.dataset.entryId === metadata!.origin!.entry_id,
      ) || null;
    if (!boundary) return;
    while (boundary.parentElement !== chat && boundary.parentElement) boundary = boundary.parentElement;
    const divider = document.createElement('div');
    divider.className = 'chat-fork-origin';
    const link = iconButton('Continued from chat', 'branch', () =>
      this.deps.switchSession(metadata!.origin!.session_id),
    );
    link.className = 'chat-fork-source';
    link.append(document.createTextNode('Continued from chat'));
    divider.append(link);
    boundary.after(divider);
    this.deps.originRendered?.();
  }

  private renderSetup(session: string, metadata: Metadata, chat: HTMLElement): void {
    chat.querySelector('.chat-fork-setup')?.remove();
    clearTimeout(this.statusTimers.get(session));
    this.statusTimers.delete(session);
    const job = metadata.job;
    if (!job || job.status === 'ready') {
      this.deps.workspaceChanged();
      return;
    }
    const note = document.createElement('div');
    note.className = 'chat-fork-setup';
    note.setAttribute('role', 'status');
    if (job.status === 'error') {
      note.textContent = job.error || 'Worktree setup did not finish.';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = 'Retry';
      retry.onclick = async () => {
        retry.disabled = true;
        try {
          await requestJson('/api/conversation_fork/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: jsonBody({ session_id: session }),
          });
          this.origin.delete(session);
          await this.showOrigin(session);
        } catch (error) {
          note.textContent = error instanceof Error ? error.message : 'Could not retry setup.';
          note.append(retry);
          retry.disabled = false;
        }
      };
      note.append(retry);
    } else {
      const spinner = document.createElement('span');
      spinner.className = 'chat-fork-spinner';
      spinner.setAttribute('aria-hidden', 'true');
      note.append(spinner, document.createTextNode('Creating worktree…'));
      this.statusTimers.set(
        session,
        setTimeout(() => void this.showOrigin(session), 700),
      );
    }
    chat.append(note);
    this.deps.workspaceChanged();
  }
}
