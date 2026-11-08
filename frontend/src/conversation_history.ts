import {
  HistoryRepository,
  type HistoryResult,
  type HistoryTask,
  type HistoryIdentity,
  validateHistoryRuns,
} from './history_repository';
import { RunVisibility } from './run_visibility';
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
    if ((message.type === 'turn' || message.type === 'thinking') && message.runId)
      return !committed.some(
        (row) =>
          row.type === 'turn' &&
          row.runId === message.runId &&
          row.n === (message.n ?? message.turn) &&
          row.reasoning === (message.reasoning || message.text),
      );
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
  private visibility = new Map<string, RunVisibility>();
  private identities = new Map<string, HistoryIdentity>();
  private resyncing = new Set<string>();
  private rewritten = new Set<string>();

  revision(id: string): number | undefined {
    return this.repositories.get(id)?.current?.revision;
  }

  acceptsRun(id: string, runId: string): boolean {
    return !runId || this.runs(id).accepts(runId);
  }

  runEventVersion(id: string): string {
    return this.runs(id).version();
  }

  acceptRunEvent(id: string, runId: string, accept: () => void): void {
    this.runs(id).admit(runId, accept);
  }

  observePath(id: string, generation: number, revision: number): boolean {
    const previous = this.identities.get(id);
    const changed = !!previous && generation > previous.pathGeneration;
    if (previous && generation >= previous.pathGeneration)
      this.identities.set(id, {
        epoch: previous.epoch,
        pathGeneration: generation,
        revision: Math.max(revision, previous.revision),
      });
    this.runs(id).observe(generation, revision);
    if (changed) {
      this.rewritten.add(id);
      this.repositories.get(id)?.invalidate();
      this.requests.delete(id);
    }
    return changed;
  }

  private runs(id: string): RunVisibility {
    let visibility = this.visibility.get(id);
    if (!visibility) {
      visibility = new RunVisibility(
        (runs, identity) => validateHistoryRuns(id, runs, identity),
        () => {
          const session = this.sessions.get(id);
          if (!session || this.resyncing.has(id)) return;
          this.resyncing.add(id);
          void this.refresh(session).finally(() => this.resyncing.delete(id));
        },
        (runs) => {
          const session = this.sessions.get(id);
          if (!session || !runs.length) return;
          if (this.removeRuns(session, runs)) this.changed(session, false, undefined, true);
        },
      );
      this.visibility.set(id, visibility);
    }
    return visibility;
  }

  private applyPage(session: Session, page: HistoryResult): boolean {
    const previous = this.identities.get(session.id);
    if (previous?.epoch === page.epoch && page.pathGeneration < previous.pathGeneration) return false;
    const changed =
      this.rewritten.has(session.id) ||
      (!!previous && (previous.epoch !== page.epoch || previous.pathGeneration !== page.pathGeneration));
    const visibleRuns = page.items.flatMap((item) => (typeof item.runId === 'string' ? [item.runId] : []));
    this.runs(session.id).update(page, visibleRuns);
    this.identities.set(
      session.id,
      previous?.epoch === page.epoch && previous.pathGeneration === page.pathGeneration
        ? { ...page, revision: Math.max(previous.revision, page.revision) }
        : page,
    );
    if (page.items.length || !session.messages.length || changed)
      session.messages = reconcileHistory(session.messages, page.items);
    this.rewritten.delete(session.id);
    this.runs(session.id).check([
      ...new Set(
        session.messages.flatMap((message) => [
          ...(message.runId ? [message.runId] : []),
          ...(message.attempts || []).map((attempt) => attempt.runId),
        ]),
      ),
    ]);
    return true;
  }

  retireRuns(session: Session, runIds: string[], invalidate = true): void {
    if (invalidate) this.runs(session.id).reset();
    this.runs(session.id).reject(runIds);
    this.removeRuns(session, runIds);
    if (invalidate) {
      this.rewritten.add(session.id);
      this.repositories.get(session.id)?.invalidate();
      this.requests.delete(session.id);
    }
  }

  private removeRuns(session: Session, runIds: string[]): boolean {
    const previousCount = session.messages.length;
    const retired = new Set(runIds);
    const removedRequests = new Set(
      session.messages.filter((message) => message.runId && retired.has(message.runId)).map((message) => message.id),
    );
    session.messages = session.messages.filter(
      (message) =>
        (!message.runId || !retired.has(message.runId)) &&
        !(message.type === 'request_status' && removedRequests.has(message.requestId)),
    );
    return session.messages.length !== previousCount;
  }

  constructor(
    private changed: (session: Session, revealEarlier?: boolean, tasks?: HistoryTask[], removedRuns?: boolean) => void,
  ) {}

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
    if (!this.applyPage(session, page)) return false;
    this.errors.delete(session.id);
    this.changed(session, false, page.taskStates);
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
      // An empty page can race the commit that admitted the current turn. Do
      // not erase a mounted/live projection on that transient response; an
      // explicit clear starts with an empty session and still converges to an
      // empty durable page.
      if (!this.applyPage(session, page)) return;
    } catch {
      if (this.requests.get(session.id) !== request) return;
      this.errors.add(session.id);
    }
    this.changed(session, false, repository.current?.taskStates);
  }

  async earlier(session: Session): Promise<void> {
    const repository = this.repositories.get(session.id);
    if (!repository) return;
    try {
      const page = await repository.loadEarlier(session.id);
      if (!page || this.sessions.get(session.id) !== session) return;
      if (!this.applyPage(session, page)) return;
      this.errors.delete(session.id);
    } catch {
      this.errors.add(session.id);
    }
    this.changed(session, true, repository?.current?.taskStates);
  }

  forget(id: string): void {
    this.repositories.get(id)?.invalidate();
    this.repositories.delete(id);
    this.sessions.delete(id);
    this.requests.delete(id);
    this.errors.delete(id);
    this.visibility.get(id)?.reset();
    this.visibility.delete(id);
    this.identities.delete(id);
    this.rewritten.delete(id);
  }
}
