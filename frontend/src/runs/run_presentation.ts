import { fetchRunChanges } from '../api/changes';
import { ConversationUI } from '../conversation/conversation_ui';
import { createIconSvg } from '../render/icon_button';
import type { SessionStateStore } from '../state/session_state_store';
import type { Session } from '../types/domain';
import { ChangesDrawer } from '../workspace/changes_drawer';
import type { UserQuestions } from './user_questions';
import type { RunProjection } from './run_projection';

export const WAITING_FOR_APPROVAL = 'Waiting for approval';
export type ChatStatus = '' | 'Running' | typeof WAITING_FOR_APPROVAL | 'Waiting for your answer' | 'Finished · unread';

function saved(key: string): Set<string> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || '[]');
    return new Set(Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []);
  } catch {
    return new Set();
  }
}

function persist(key: string, values: Set<string>) {
  try {
    localStorage.setItem(key, JSON.stringify([...values].slice(-200)));
  } catch {}
}

const UNREAD = 'codey:unread-runs:v1';
const REVIEWED = 'codey:reviewed-changes:v1';

export class RunPresentation {
  private unread = saved(UNREAD);
  private reviewed = saved(REVIEWED);
  private seen = new Set<string>();
  private loads = new Set<string>();

  constructor(
    private readonly sessions: SessionStateStore,
    private readonly runs: RunProjection,
    private readonly questions: Pick<UserQuestions, 'pending'>,
  ) {}

  mount(): void {
    setInterval(this.paintTop, 1000);
    document.addEventListener('visibilitychange', this.update);
    window.addEventListener('storage', (event) => {
      if (event.key === UNREAD) this.unread = saved(UNREAD);
      else if (event.key === REVIEWED) this.reviewed = saved(REVIEWED);
      else return;
      this.update();
    });
  }

  chatStatus = (session: Session): ChatStatus => {
    if (session.messages.some((message) => message.type === 'shell_request')) return WAITING_FOR_APPROVAL;
    if (this.questions.pending(session.id)) return 'Waiting for your answer';
    const activity = this.runs.forSession(session.id);
    if (activity?.state === 'approval') return WAITING_FOR_APPROVAL;
    if (activity?.state === 'question') return 'Waiting for your answer';
    if (activity?.state === 'running') return 'Running';
    return [...this.unread].some((key) => key.startsWith(`${session.id}\0`)) ? 'Finished · unread' : '';
  };

  settled = (sessionId: string, runId: string): void => {
    const key = `${sessionId}\0${runId}`;
    if (this.seen.has(key)) return;
    this.seen.add(key);
    this.unread.add(key);
    persist(UNREAD, this.unread);
    this.update();
  };

  viewed = (sessionId: string, runId: string): void => {
    const key = `${sessionId}\0${runId}`;
    this.reviewed.delete(key);
    this.reviewed.add(key);
    persist(REVIEWED, this.reviewed);
    this.paintTop();
  };

  collectChanges = async (sessionId: string, eventKey: string, runId: string): Promise<void> => {
    const project = this.sessions.projectPathFor(sessionId);
    const key = `${sessionId}\0${runId}`;
    const session = this.sessions.sessions.find((item) => item.id === sessionId);
    if (!session || !project || !runId || this.loads.has(key)) return;
    if (session.messages.some((message) => message.type === 'changes' && message.runId === runId)) return;
    this.loads.add(key);
    try {
      const data = await fetchRunChanges(sessionId, runId);
      if (!data.ok || !data.changed_count || project !== this.sessions.projectPathFor(sessionId)) return;
      const totals =
        data.files.length === data.changed_count &&
        data.files.every((file) => typeof file.additions === 'number' && typeof file.deletions === 'number');
      this.sessions.addMessage(sessionId, {
        type: 'changes',
        count: data.changed_count,
        files: data.files.slice(0, 3),
        project,
        eventKey,
        runId,
        sessionId,
        undoAvailable: data.undo_available,
        ...(totals
          ? {
              additions: data.files.reduce((sum, file) => sum + (file.additions ?? 0), 0),
              deletions: data.files.reduce((sum, file) => sum + (file.deletions ?? 0), 0),
            }
          : {}),
      });
      this.paintTop();
    } catch {
      // A live terminal event and a restored history window can retry the same read.
    } finally {
      this.loads.delete(key);
    }
  };

  update = (): void => {
    const id = this.sessions.activeId;
    if (document.visibilityState !== 'hidden' && ConversationUI.isFollowing()) {
      let changed = false;
      for (const key of this.unread) {
        if (key.startsWith(`${id}\0`)) changed = this.unread.delete(key) || changed;
      }
      if (changed) persist(UNREAD, this.unread);
    }
    const sessions = new Map(this.sessions.sessions.map((session) => [session.id, session]));
    for (const row of document.querySelectorAll<HTMLElement>('[data-chat-status]')) {
      const session = sessions.get(row.dataset.chatStatus || '');
      const label = session ? this.chatStatus(session) : '';
      row.hidden = !label;
      row.title = label;
      row.dataset.status = label;
      row.textContent = label;
    }
    this.paintTop();
  };

  private paintTop = (): void => {
    const status = document.getElementById('status');
    const button = document.getElementById('top-changes');
    if (!status || !(button instanceof HTMLButtonElement)) return;
    const session = this.sessions.activeSession();
    const activity = session ? this.chatStatus(session) : '';
    const run = session && this.runs.forSession(session.id);
    const elapsed = run?.startedAt ? Math.max(0, Math.floor((Date.now() - run.startedAt) / 1000)) : undefined;
    const prior = status.querySelector<HTMLElement>('.current-task-status');
    if (activity === 'Running' || activity === WAITING_FOR_APPROVAL || activity === 'Waiting for your answer') {
      const line = prior ?? document.createElement('span');
      line.className = 'current-task-status';
      line.dataset.status = activity;
      const text =
        activity === 'Running' && elapsed !== undefined
          ? `Running · ${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, '0')}`
          : activity;
      if (line.textContent !== text) line.textContent = text;
      if (!prior) status.append(line);
    } else prior?.remove();
    const changes = session?.messages
      .slice()
      .reverse()
      .find(
        (message) =>
          message.type === 'changes' &&
          !!message.runId &&
          !this.reviewed.has(`${session.id}\0${message.runId}`) &&
          !!message.project,
      );
    button.hidden = !session || !changes;
    if (session && changes?.type === 'changes' && changes.runId && changes.project) {
      const { runId, project, count, additions, deletions } = changes;
      const stats = additions !== undefined && deletions !== undefined ? ` +${additions} −${deletions}` : '';
      const label = `${count} changed${stats}`;
      if (button.textContent !== label) button.replaceChildren(createIconSvg('file'), label);
      button.onclick = () => void ChangesDrawer.openRun(project, session.id, runId);
    }
  };
}
