import type { Message, Project, Session } from './types/domain';
import type { UiStateApi } from './ui_state';
import { mergeCommandResult } from './command_result';
import { finalStreamMessage } from './reply_stream';

export type SessionStateChange =
  | { kind: 'session'; action: 'switch' | 'create' | 'delete' | 'clear' | 'rename' | 'attach'; sessionId: string }
  | { kind: 'project'; action: 'rename' | 'delete'; projectId: string }
  | {
      kind: 'message';
      action: 'append' | 'first' | 'title' | 'replace' | 'drop';
      sessionId: string;
      message?: Message;
      beforeRequestId?: string;
    };

export interface SessionStateDeps {
  ui: Pick<
    UiStateApi,
    | 'defaultSession'
    | 'uid'
    | 'pathKey'
    | 'pathName'
    | 'persist'
    | 'persistNow'
    | 'ensureSessionIndexes'
    | 'trackSessionMessage'
    | 'hydrateSessionIndexes'
  >;
  defaultProvider(): string;
  isRenaming(sessionId: string): boolean;
  invalidate(change: SessionStateChange): void;
  forgetView(sessionId: string): void;
  forgetOnServer(sessionId: string, preserveUi: boolean): Promise<boolean>;
  requestIdForRun(sessionId: string, runId: string): string | undefined;
  mountedStreamId(sessionId: string, runId: string): string | undefined;
  messageAdded(): void;
}

/** Presentation state and indexes. Durable history and task authority stay on the server. */
export class SessionStateStore {
  sessions: Session[] = [];
  projects: Project[] = [];
  activeId = '';
  updatedAt = 0;
  revision = 0;

  constructor(private readonly deps: SessionStateDeps) {}

  activeSession(): Session | undefined {
    return this.sessions.find((session) => session.id === this.activeId);
  }

  projectById(id: string): Project | null {
    return this.projects.find((project) => project.id === id) || null;
  }

  sessionProject(session: Session | undefined): Project | null {
    return session?.projectId ? this.projectById(session.projectId) : null;
  }

  activeProject(): Project | null {
    return this.sessionProject(this.activeSession());
  }

  currentProjectPath(): string {
    return this.activeProject()?.path || '';
  }

  currentProviderId(): string {
    const session = this.activeSession();
    return session ? (session.provider ?? '') : this.deps.defaultProvider();
  }

  renameSession(id: string, value: string | undefined): void {
    const session = this.sessions.find((item) => item.id === id);
    const title = (value || '').trim();
    if (session && title && title !== session.title) {
      session.title = title.slice(0, 80);
      session.titleRenamed = true;
      this.deps.ui.persistNow();
    }
    this.deps.invalidate({ kind: 'session', action: 'rename', sessionId: id });
  }

  renameProject(id: string, value: string | undefined): void {
    const project = this.projectById(id);
    const name = (value || '').trim();
    if (project && name && name !== project.name) {
      project.name = name.slice(0, 80);
      this.deps.ui.persistNow();
    }
    this.deps.invalidate({ kind: 'project', action: 'rename', projectId: id });
  }

  switchSession(id: string): void {
    this.activeId = id;
    this.deps.ui.persistNow();
    this.deps.invalidate({ kind: 'session', action: 'switch', sessionId: id });
  }

  newSession(projectId: string | null = null): void {
    const session = this.deps.ui.defaultSession(projectId, this.currentProviderId());
    this.sessions.unshift(session);
    this.activeId = session.id;
    this.deps.ui.persistNow();
    this.deps.invalidate({ kind: 'session', action: 'create', sessionId: session.id });
  }

  openProject(id: string): void {
    const project = this.projectById(id);
    if (!project) return;
    project.expanded = true;
    const first = this.sessions.find((session) => session.projectId === id);
    if (first) this.switchSession(first.id);
    else this.newSession(id);
  }

  async deleteSession(id: string): Promise<void> {
    if (!(await this.deps.forgetOnServer(id, false))) return;
    const provider = this.currentProviderId();
    this.sessions = this.sessions.filter((session) => session.id !== id);
    this.deps.forgetView(id);
    if (!this.sessions.length) this.sessions = [this.deps.ui.defaultSession(null, provider)];
    if (!this.sessions.some((session) => session.id === this.activeId)) this.activeId = this.sessions[0].id;
    this.deps.ui.persistNow();
    this.deps.invalidate({ kind: 'session', action: 'delete', sessionId: id });
  }

  async clearMessages(id: string): Promise<void> {
    if (!(await this.deps.forgetOnServer(id, true))) return;
    const session = this.sessions.find((item) => item.id === id);
    if (!session) return;
    this.deps.forgetView(id);
    session.messages = [];
    session.terminalRuns = [];
    session.researchRuns = [];
    this.deps.ui.persistNow();
    this.deps.invalidate({ kind: 'session', action: 'clear', sessionId: id });
  }

  async deleteProject(id: string): Promise<void> {
    if (!this.projectById(id)) return;
    const cleared = await Promise.all(
      this.sessions
        .filter((session) => session.projectId === id)
        .map((session) => this.deps.forgetOnServer(session.id, false)),
    );
    if (!cleared.every(Boolean)) return;
    this.projects = this.projects.filter((project) => project.id !== id);
    for (const session of this.sessions.filter((item) => item.projectId === id)) this.deps.forgetView(session.id);
    this.sessions = this.sessions.filter((session) => session.projectId !== id);
    if (!this.sessions.length) this.sessions = [this.deps.ui.defaultSession(null, this.deps.defaultProvider())];
    if (!this.sessions.some((session) => session.id === this.activeId)) this.activeId = this.sessions[0].id;
    this.deps.ui.persistNow();
    this.deps.invalidate({ kind: 'project', action: 'delete', projectId: id });
  }

  ensureProject(path: string, name: string): Project {
    const existing = this.projects.find((project) => this.deps.ui.pathKey(project.path) === this.deps.ui.pathKey(path));
    if (existing) {
      existing.expanded = true;
      return existing;
    }
    const project = {
      id: this.deps.ui.uid(),
      name: name || this.deps.ui.pathName(path),
      path,
      expanded: true,
      createdAt: Date.now(),
    };
    this.projects.unshift(project);
    this.deps.ui.persist();
    return project;
  }

  attachSessionToProject(projectId: string | null, sessionId = this.activeId): boolean {
    const session = this.sessions.find((item) => item.id === sessionId);
    const project = projectId ? this.projectById(projectId) : null;
    if (!session || !project || this.sessionProject(session)) return false;
    session.projectId = project.id;
    session.research = false;
    project.expanded = true;
    this.deps.ui.persistNow();
    this.deps.invalidate({ kind: 'session', action: 'attach', sessionId });
    return true;
  }

  pushMessage(sessionId: string, message: Message): boolean {
    const session = this.sessions.find((item) => item.id === sessionId);
    if (!session) return false;
    const wasEmpty = session.messages.length === 0;
    session.messages.push(message);
    const title =
      session.title === 'New chat' &&
      !session.titleRenamed &&
      !this.deps.isRenaming(sessionId) &&
      message.type === 'user'
        ? message.text
        : undefined;
    if (title) session.title = title.slice(0, 28) + (title.length > 28 ? '…' : '');
    this.deps.ui.persistNow();
    this.deps.invalidate({
      kind: 'message',
      action: title ? 'title' : wasEmpty ? 'first' : 'append',
      sessionId,
      message,
    });
    return true;
  }

  addMessage(sessionId: string, message: Message): boolean {
    const session = this.sessions.find((item) => item.id === sessionId);
    if (!session) return false;
    if (
      message.type === 'user' &&
      message.queuedInputId &&
      session.messages.some(
        (item) =>
          item.type === 'user' && (item.queuedInputId === message.queuedInputId || item.id === message.queuedInputId),
      )
    )
      return true;
    if (message.type === 'shell_result' && message.id) {
      const matches = (item: Message) =>
        ['shell_request', 'shell_result'].includes(item.type) && item.id === message.id;
      if (this.replaceMessage(sessionId, matches, mergeCommandResult(session.messages.find(matches), message)))
        return true;
    }
    if (message.type === 'asst' && !message.streamId && message.runId && message.text) {
      const existing = session.messages
        .slice()
        .reverse()
        .find((item) => item.type === 'asst' && item.runId === message.runId);
      if (existing) {
        const settled = existing.streamId
          ? {
              ...existing,
              ...message,
              streamId: existing.streamId,
              streamState: 'complete' as const,
              finalAnswer: true,
            }
          : message;
        return this.replaceMessage(sessionId, (item) => item === existing, settled);
      }
      // A history refresh may replace the transient record before terminal delivery.
      // The view supplies only its mounted identity; this store owns the new record.
      const streamId = this.deps.mountedStreamId(sessionId, message.runId);
      if (streamId) {
        const settled: Message = { ...message, streamId, streamState: 'complete', finalAnswer: true };
        session.messages.push(settled);
        this.deps.ui.trackSessionMessage(session, settled);
        this.deps.ui.persist();
        this.deps.invalidate({ kind: 'message', action: 'replace', sessionId, message: settled });
        return true;
      }
      const final = finalStreamMessage(session, message.runId, message.text, message.eventKey || '');
      if (final)
        return this.replaceMessage(
          sessionId,
          (item) => item.streamId === final.streamId && item.runId === message.runId,
          final,
        );
    }
    this.deps.ui.ensureSessionIndexes(session);
    if (message.eventKey && session._eventKeys?.has(message.eventKey)) return false;
    if (['limit', 'pause', 'teach', 'err'].includes(message.type) && !message.sessionId) message.sessionId = sessionId;
    const requestId = this.deps.requestIdForRun(sessionId, message.runId || '');
    const marker = requestId
      ? session.messages.findIndex((item) => item.type === 'request_status' && item.requestId === requestId)
      : -1;
    if (marker >= 0) session.messages.splice(marker, 0, message);
    else session.messages.push(message);
    this.deps.ui.trackSessionMessage(session, message);
    this.deps.ui.persist();
    this.deps.invalidate({
      kind: 'message',
      action: session.messages.length === 1 ? 'first' : 'append',
      sessionId,
      message,
      beforeRequestId: requestId,
    });
    this.deps.messageAdded();
    return true;
  }

  replaceMessage(sessionId: string, predicate: (message: Message) => boolean, message: Message): boolean {
    const session = this.sessions.find((item) => item.id === sessionId);
    if (!session) return false;
    this.deps.ui.ensureSessionIndexes(session);
    const index = session.messages.findIndex(predicate);
    if (index < 0) return false;
    session.messages[index] = message;
    this.deps.ui.trackSessionMessage(session, message);
    this.deps.ui.persist();
    this.deps.invalidate({ kind: 'message', action: 'replace', sessionId, message });
    return true;
  }

  dropMessages(sessionId: string, predicate: (message: Message) => boolean): void {
    const session = this.sessions.find((item) => item.id === sessionId);
    if (!session) return;
    const before = session.messages.length;
    session.messages = session.messages.filter((message) => !predicate(message));
    if (session.messages.length === before) return;
    this.deps.ui.hydrateSessionIndexes(session);
    this.deps.ui.persist();
    this.deps.invalidate({ kind: 'message', action: 'drop', sessionId });
  }
}
