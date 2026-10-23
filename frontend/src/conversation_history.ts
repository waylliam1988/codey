import { HistoryRepository, type HistoryResult } from './history_repository';
import type { Message, Session } from './types/domain';

/** Durable entry identity replaces a live projection; message text is never an identity. */
export function reconcileHistory(current: Message[], items: HistoryResult['items']): Message[] {
  const committed = items
    .filter((item) => typeof item.type === 'string')
    .map(
      (item) =>
        ({
          ...item,
          entryId: item.id,
        }) as unknown as Message,
    );
  const ids = new Set(committed.map((message) => message.id));
  const turns = new Set(
    committed.filter((message) => message.runId).map((message) => `${message.type}:${message.runId}`),
  );
  const streamingRuns = new Set(
    current
      .filter(
        (message) =>
          message.type === 'asst' &&
          message.streamId &&
          message.runId &&
          message.finalAnswer === false &&
          !!message.text?.trim(),
      )
      .map((message) => message.runId as string),
  );
  const visibleCommitted = committed.filter(
    (message) => !(message.type === 'asst' && message.runId && streamingRuns.has(message.runId)),
  );
  const live = current.filter((message) => {
    if (message.entryId || (message.id && ids.has(message.id))) return false;
    if (message.type === 'user')
      return ![message.runId, ...(message.attempts || []).map((attempt) => attempt.runId)].some(
        (runId) => runId && turns.has(`user:${runId}`),
      );
    if (message.type === 'asst' && message.contentRole !== 'commentary' && message.finalAnswer !== false)
      return !message.runId || !turns.has(`asst:${message.runId}`);
    return true;
  });
  // Transient process/approval records belong to their turn, even when the
  // durable final answer arrives before its corresponding live events.
  const before = new Map<Message, Message[]>();
  const after = new Map<Message, Message[]>();
  const tail: Message[] = [];
  for (const message of live) {
    const sameRun = message.runId ? visibleCommitted.filter((row) => row.runId === message.runId) : [];
    const final = sameRun.find(
      (row) => row.type === 'asst' && row.contentRole !== 'commentary' && row.finalAnswer !== false,
    );
    const anchor = final || sameRun.at(-1);
    if (!anchor || message.type === 'user') {
      tail.push(message);
      continue;
    }
    const slots = final ? before : after;
    const bucket = slots.get(anchor) || [];
    bucket.push(message);
    slots.set(anchor, bucket);
  }
  return visibleCommitted.flatMap((row) => [...(before.get(row) || []), row, ...(after.get(row) || [])]).concat(tail);
}

/** In-memory projection cache. It never saves conversation content. */
export class ConversationHistory {
  private repositories = new Map<string, HistoryRepository>();
  private sessions = new Map<string, Session>();
  private requests = new Map<string, symbol>();
  private errors = new Set<string>();
  private inactiveRuns = new Map<string, Set<string>>();

  revision(id: string): number | undefined {
    return this.repositories.get(id)?.current?.revision;
  }

  acceptsRun(id: string, runId: string): boolean {
    return !runId || !this.inactiveRuns.get(id)?.has(runId);
  }

  retireRuns(session: Session, runIds: string[], invalidate = true): void {
    const retired = invalidate ? this.inactiveRuns.get(session.id) || new Set<string>() : new Set<string>();
    runIds.forEach((id) => retired.add(id));
    this.inactiveRuns.set(session.id, retired);
    const removedRequests = new Set(
      session.messages.filter((message) => message.runId && retired.has(message.runId)).map((message) => message.id),
    );
    session.messages = session.messages.filter(
      (message) =>
        (!message.runId || !retired.has(message.runId)) &&
        !(message.type === 'request_status' && removedRequests.has(message.requestId)),
    );
    if (invalidate) {
      this.repositories.get(session.id)?.invalidate();
      this.requests.delete(session.id);
    }
  }

  constructor(private changed: (session: Session, revealEarlier?: boolean) => void) {}

  ensure(session: Session): void {
    if (this.sessions.get(session.id) !== session) void this.refresh(session);
  }

  hasMore(id: string): boolean {
    return this.repositories.get(id)?.current?.hasMore === true;
  }
  failed(id: string): boolean {
    return this.errors.has(id);
  }

  hasAfter(id: string): boolean {
    return this.repositories.get(id)?.current?.hasAfter === true;
  }

  pauseRefresh(id: string): void {
    this.repositories.get(id)?.cancelLatest();
  }

  async focus(session: Session, entryId: string): Promise<boolean> {
    const repository = this.repositories.get(session.id) || new HistoryRepository();
    this.repositories.set(session.id, repository);
    this.sessions.set(session.id, session);
    const request = Symbol();
    this.requests.set(session.id, request);
    const page = await repository.loadAround(session.id, entryId);
    if (!page || this.requests.get(session.id) !== request) return false;
    session.messages = reconcileHistory(session.messages, page.items);
    this.errors.delete(session.id);
    this.changed(session);
    return true;
  }

  async refresh(session: Session): Promise<void> {
    this.sessions.set(session.id, session);
    const repository = this.repositories.get(session.id) || new HistoryRepository();
    this.repositories.set(session.id, repository);
    const request = Symbol();
    this.requests.set(session.id, request);
    this.errors.delete(session.id);
    try {
      const page = await repository.loadLatest(session.id);
      if (!page || this.requests.get(session.id) !== request) return;
      if (page.inactiveRuns) this.retireRuns(session, page.inactiveRuns, false);
      // An empty page can race the commit that admitted the current turn. Do
      // not erase a mounted/live projection on that transient response; an
      // explicit clear starts with an empty session and still converges to an
      // empty durable page.
      if (page.items.length > 0 || session.messages.length === 0)
        session.messages = reconcileHistory(session.messages, page.items);
    } catch {
      if (this.requests.get(session.id) !== request) return;
      this.errors.add(session.id);
    }
    this.changed(session);
  }

  async earlier(session: Session): Promise<void> {
    const repository = this.repositories.get(session.id);
    if (!repository) return;
    try {
      const page = await repository.loadEarlier(session.id);
      if (!page || this.sessions.get(session.id) !== session) return;
      session.messages = reconcileHistory(session.messages, page.items);
      this.errors.delete(session.id);
    } catch {
      this.errors.add(session.id);
    }
    this.changed(session, true);
  }

  forget(id: string): void {
    this.repositories.get(id)?.invalidate();
    this.repositories.delete(id);
    this.sessions.delete(id);
    this.requests.delete(id);
    this.errors.delete(id);
    this.inactiveRuns.delete(id);
  }
}
