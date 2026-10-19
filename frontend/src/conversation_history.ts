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
        (message) => message.type === 'asst' && message.streamId && message.runId && message.finalAnswer === false,
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
  return [...visibleCommitted, ...live];
}

/** In-memory projection cache. It never saves conversation content. */
export class ConversationHistory {
  private repositories = new Map<string, HistoryRepository>();
  private sessions = new Map<string, Session>();
  private requests = new Map<string, symbol>();
  private errors = new Set<string>();

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

  async earlier(session: Session, all = false): Promise<void> {
    const repository = this.repositories.get(session.id);
    if (!repository) return;
    try {
      let page: HistoryResult | null;
      do {
        page = await repository.loadEarlier(session.id);
        if (!page || this.sessions.get(session.id) !== session) return;
        session.messages = reconcileHistory(session.messages, page.items);
      } while (all && page.hasMore);
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
  }
}
