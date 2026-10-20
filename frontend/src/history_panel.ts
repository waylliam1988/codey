import { requestJson, jsonBody, ApiError } from './api/transport';
import { runtime } from './runtime';
import type { Session } from './types/domain';

interface Tree {
  revision: number;
  head_entry_id: string | null;
  ancestors: Array<{ session_id: string; entry_id: string }>;
  related_sessions?: Array<{ session_id: string; title: string }>;
  branches: Array<{ entry_id: string; text: string; kind: string; current: boolean }>;
}
interface ForkResult {
  session_id: string;
  state: Record<string, unknown>;
}
interface Deps {
  session(): Session | undefined;
  switchSession(id: string): void;
  changed(id: string): Promise<void>;
  adoptFork(data: ForkResult, baseline: Record<string, unknown>): void;
}
function action(label: string, click: () => void): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.onclick = click;
  return button;
}
export class HistoryPanel {
  readonly element = document.createElement('section');
  private generation = 0;
  private requestIds = new Map<string, string>();
  constructor(private deps: Deps) {}

  async show(target: Record<string, string> = {}): Promise<void> {
    const generation = ++this.generation;
    const session = this.deps.session();
    if (!session) return;
    const body = document.createElement('div');
    body.className = 'workspace-content';
    const tabs = document.createElement('div');
    tabs.className = 'history-panel-tabs';
    tabs.append(
      action('Branches', () => {
        void this.show({});
      }),
      action('Workflow', () => {
        void this.show({ mode: 'workflow', runId: target.runId || '' });
      }),
    );
    const content = document.createElement('div');
    content.textContent = 'Loading history…';
    const notice = document.createElement('p');
    notice.setAttribute('role', 'status');
    body.append(tabs, notice, content);
    this.element.replaceChildren(body);
    const valid = () => this.deps.session()?.id === session.id && this.generation === generation;
    const mutate = async (key: string, url: string, payload: object) => {
      if (!valid()) return;
      if (!(await runtime.UiState.ensureSaved())) {
        notice.textContent = 'Save this conversation before creating a branch.';
        return;
      }
      const baseline = runtime.UiState.current();
      let identity = this.requestIds.get(key);
      if (!identity) {
        try {
          const saved = sessionStorage.getItem(`codey:fork:${key}`);
          identity = saved || crypto.randomUUID();
          sessionStorage.setItem(`codey:fork:${key}`, identity);
          this.requestIds.set(key, identity);
        } catch {
          notice.textContent = 'Could not save the branch request identity. No branch was created.';
          return;
        }
      }
      notice.textContent = 'Creating branch…';
      try {
        const result = await requestJson<ForkResult>(url, {
          method: 'POST',
          body: jsonBody({ ...payload, request_id: identity }),
          headers: { 'Content-Type': 'application/json' },
        });
        try {
          sessionStorage.removeItem(`codey:fork:${key}`);
        } catch {
          /* The confirmed response still owns this result. */
        }
        this.requestIds.delete(key);
        // Register the result even when the user changed sessions during admission.
        this.deps.adoptFork(result, baseline);
      } catch (error) {
        if (valid())
          notice.textContent = `${error instanceof Error ? error.message : 'Branch unavailable'} · retry keeps the same request identity.`;
      }
    };
    try {
      if (target.mode === 'workflow') {
        const runs = [
          ...new Set(session.messages.map((message) => message.runId).filter((id): id is string => !!id)),
        ].reverse();
        const select = document.createElement('select');
        select.setAttribute('aria-label', 'Workflow run');
        runs.forEach((id) => {
          const option = document.createElement('option');
          option.value = option.textContent = id;
          select.append(option);
        });
        select.value = target.runId || runs[0] || '';
        select.onchange = () => {
          void this.show({ mode: 'workflow', runId: select.value });
        };
        const run = select.value;
        if (!run) {
          content.textContent = 'No recorded workflow for this conversation.';
          return;
        }
        const data = await requestJson<{
          items: Array<{
            commit_seq: number;
            phase: string;
            status: string;
            head_entry_id: string | null;
            forkable: boolean;
            effect_changes: Array<{ status: string; category: string }>;
          }>;
          has_more: boolean;
          after: number;
        }>(`/api/workflows?${new URLSearchParams({ task_id: run, after: target.after || '0', limit: '100' })}`);
        if (!valid()) return;
        const instruction = document.createElement('textarea');
        instruction.placeholder = 'Instructions for the new branch';
        instruction.setAttribute('aria-label', 'Branch instructions');
        instruction.maxLength = 8000;
        const scope = document.createElement('p');
        scope.textContent =
          'A new conversation inherits the selected checkpoint. Workspace files remain at their current state.';
        content.replaceChildren(select, scope, instruction);
        for (const point of data.items) {
          const row = document.createElement('section');
          row.className = 'workflow-point';
          const label = document.createElement('div');
          label.textContent = `${point.phase} · ${point.status} · commit ${point.commit_seq}`;
          const fork = action('Continue from here', () => {
            if (!instruction.value.trim()) {
              notice.textContent = 'Enter instructions for the new branch.';
              instruction.focus();
              return;
            }
            void mutate(`${run}:${point.commit_seq}:${instruction.value}`, '/api/workflows/fork', {
              task_id: run,
              commit_seq: point.commit_seq,
              instruction: instruction.value,
            });
          });
          fork.disabled = !point.head_entry_id || point.forkable === false;
          if (fork.disabled) fork.title = 'This checkpoint has no conversation entry or has unsettled external work.';
          row.append(label, fork);
          content.append(row);
        }
        if (data.has_more)
          content.append(
            action('Load next checkpoints', () => {
              void this.show({ mode: 'workflow', runId: run, after: String(data.after) });
            }),
          );
        return;
      }
      const tree = await requestJson<Tree>(`/api/conversation_tree?session_id=${encodeURIComponent(session.id)}`);
      if (!valid()) return;
      content.replaceChildren();
      const heading = document.createElement('h3');
      heading.textContent = 'Conversation branches';
      content.append(heading);
      for (const ancestor of tree.ancestors)
        content.append(
          action(`Parent conversation · ${ancestor.session_id}`, () => this.deps.switchSession(ancestor.session_id)),
        );
      for (const related of tree.related_sessions || []) {
        if (related.session_id !== session.id)
          content.append(action(related.title, () => this.deps.switchSession(related.session_id)));
      }
      const warning = document.createElement('p');
      warning.textContent =
        'Browse a branch before choosing it for future messages. Switching conversation history does not restore workspace files.';
      content.append(warning);
      for (const branch of tree.branches) {
        const row = document.createElement('section');
        row.className = 'conversation-branch';
        const label = document.createElement('div');
        label.textContent = `${branch.current ? 'Current · ' : ''}${branch.text}`;
        const preview = document.createElement('div');
        preview.className = 'branch-preview';
        preview.hidden = true;
        const browse = action('Browse', async () => {
          preview.hidden = false;
          preview.textContent = 'Loading branch…';
          try {
            const page = await requestJson<{
              items: Array<{ type: string; text?: string; content?: string; result?: string }>;
            }>(
              `/api/conversation_tree?${new URLSearchParams({ session_id: session.id, entry_id: branch.entry_id, leaf_id: branch.entry_id })}`,
            );
            if (!valid()) return;
            preview.replaceChildren();
            for (const message of page.items) {
              const text = document.createElement('p');
              text.textContent = `${message.type} · ${message.text || message.content || message.result || ''}`;
              preview.append(text);
            }
          } catch (error) {
            if (valid()) preview.textContent = error instanceof Error ? error.message : 'Branch unavailable';
          }
        });
        const choose = action('Use this branch', async () => {
          choose.disabled = true;
          try {
            await requestJson('/api/conversation_tree/action', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: jsonBody({
                action: 'select',
                session_id: session.id,
                entry_id: branch.entry_id,
                revision: tree.revision,
              }),
            });
            await this.deps.changed(session.id);
            if (valid()) void this.show();
          } catch (error) {
            if (valid()) {
              notice.textContent = error instanceof Error ? error.message : 'Could not switch branch';
              choose.disabled = false;
            }
          }
        });
        choose.disabled = branch.current;
        row.append(
          label,
          browse,
          choose,
          action('Fork conversation', () => {
            void mutate(`${session.id}:${branch.entry_id}`, '/api/conversation_tree/action', {
              action: 'fork',
              session_id: session.id,
              entry_id: branch.entry_id,
            });
          }),
          preview,
        );
        content.append(row);
      }
      if (target.entryId) {
        const key = `${session.id}:${target.entryId}`;
        content.prepend(
          action('Fork from selected message', () => {
            void mutate(key, '/api/conversation_tree/action', {
              action: 'fork',
              session_id: session.id,
              entry_id: target.entryId,
            });
          }),
        );
      }
    } catch (error) {
      if (!valid()) return;
      if (error instanceof ApiError && error.status === 404 && !session.messages.length) {
        content.textContent = 'No conversation history yet.';
        return;
      }
      content.textContent = error instanceof Error ? error.message : 'History unavailable';
      content.append(
        action('Retry', () => {
          void this.show(target);
        }),
      );
    }
  }
}
