import { requestResponse } from './api/transport';
import { decodeTaskStates, nonempty, sequence } from './domain/protocol';
import { isRecord } from './domain/display_values';
import type { RunState } from './types/domain';
export type HistoryTask = NonNullable<RunState['task_states']>[number];
export interface HistoryPage {
  schema_version: 1;
  session_id: string;
  epoch: string;
  snapshot_revision: number;
  path_generation: number;
  display_version: string | null;
  items: Array<Record<string, unknown>>;
  before_cursor?: string | null;
  has_more: boolean;
  has_after?: boolean;
  task_states?: HistoryTask[];
}

export interface HistoryResult {
  items: Array<Record<string, unknown>>;
  hasMore: boolean;
  cursor: string | null;
  epoch: string;
  revision: number;
  pathGeneration: number;
  displayVersion: string | null;
  hasAfter?: boolean;
  taskStates?: HistoryTask[];
}

export class HistorySnapshotChanged extends Error {}

export interface HistoryIdentity {
  epoch: string;
  pathGeneration: number;
  revision: number;
}

export interface RunValidation extends HistoryIdentity {
  runs: Array<{ run_id: string; visible: boolean }>;
}

export async function validateHistoryRuns(
  sessionId: string,
  runIds: string[],
  identity: HistoryIdentity,
): Promise<RunValidation> {
  const response = await requestResponse('/api/history/validate_runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      epoch: identity.epoch,
      path_generation: identity.pathGeneration,
      run_ids: runIds,
    }),
    signal: AbortSignal.timeout(5000),
  });
  if (response.status === 409) throw new HistorySnapshotChanged('Conversation history changed');
  if (!response.ok) throw new Error('Could not validate conversation tasks');
  const data: unknown = await response.json();
  if (
    !isRecord(data) ||
    data.schema_version !== 1 ||
    data.session_id !== sessionId ||
    data.epoch !== identity.epoch ||
    data.path_generation !== identity.pathGeneration ||
    !sequence(data.snapshot_revision, identity.revision) ||
    !Array.isArray(data.runs) ||
    data.runs.length !== runIds.length
  )
    throw new HistorySnapshotChanged('Conversation task validation changed');
  const runs = data.runs.map((run: unknown) => {
    if (
      !isRecord(run) ||
      typeof run.run_id !== 'string' ||
      !runIds.includes(run.run_id) ||
      typeof run.visible !== 'boolean'
    )
      throw new HistorySnapshotChanged('Conversation task validation changed');
    return { run_id: run.run_id, visible: run.visible };
  });
  if (new Set(runs.map((run) => run.run_id)).size !== runIds.length)
    throw new HistorySnapshotChanged('Conversation task validation changed');
  return {
    epoch: data.epoch,
    pathGeneration: data.path_generation,
    revision: data.snapshot_revision,
    runs,
  };
}

export class HistoryRepository {
  private session = '';
  private generation = 0;
  private page: HistoryResult | null = null;
  private loading = false;
  private loadingLatest = false;

  get current(): HistoryResult | null {
    return this.page;
  }

  cancelLatest(): void {
    if (!this.loadingLatest || !this.page) return;
    this.generation++;
    this.loadingLatest = false;
    this.loading = false;
  }

  invalidate(): void {
    this.generation++;
    this.page = null;
    this.loading = false;
  }

  switchSession(sessionId: string): void {
    if (this.session !== sessionId) {
      this.session = sessionId;
      this.invalidate();
    }
  }

  async loadLatest(sessionId: string): Promise<HistoryResult | null> {
    this.switchSession(sessionId);
    const generation = ++this.generation;
    this.loading = false;
    this.loadingLatest = true;
    let page: HistoryPage;
    try {
      page = await this.fetchPage(sessionId, '');
    } finally {
      if (generation === this.generation) this.loadingLatest = false;
    }
    if (generation !== this.generation || this.session !== sessionId) return null;
    this.page = {
      items: dedupe(page.items),
      hasMore: page.has_more,
      cursor: page.before_cursor || null,
      epoch: page.epoch,
      revision: page.snapshot_revision,
      pathGeneration: page.path_generation,
      displayVersion: page.display_version,
      taskStates: page.task_states,
    };
    return this.page;
  }

  async loadEarlier(sessionId: string): Promise<HistoryResult | null> {
    if (sessionId !== this.session) return null;
    if (!this.page?.hasMore || !this.page.cursor || this.loading) return null;
    const generation = this.generation;
    const previous = this.page;
    this.loading = true;
    try {
      const page = await this.fetchPage(sessionId, this.page.cursor);
      if (generation !== this.generation || this.session !== sessionId) return null;
      if (
        page.epoch !== previous.epoch ||
        page.path_generation !== previous.pathGeneration ||
        page.snapshot_revision !== previous.revision
      )
        return await this.loadLatest(sessionId);
      if (page.has_more && page.before_cursor === previous.cursor)
        throw new Error('Conversation history did not advance');
      const items = dedupe([...page.items, ...previous.items]);
      if (page.has_more && items.length <= previous.items.length)
        throw new Error('Conversation history did not advance');
      this.page = {
        items,
        hasMore: page.has_more,
        cursor: page.before_cursor || null,
        epoch: page.epoch,
        revision: page.snapshot_revision,
        pathGeneration: page.path_generation,
        displayVersion: page.display_version,
        taskStates: page.task_states,
        hasAfter: previous.hasAfter,
      };
      return this.page;
    } catch (error) {
      if (error instanceof HistorySnapshotChanged && generation === this.generation)
        return await this.loadLatest(sessionId);
      throw error;
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  async search(sessionId: string, query: string): Promise<Array<Record<string, unknown>>> {
    if (!query.trim()) return [];
    const response = await requestResponse(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages/search?q=${encodeURIComponent(query.trim().slice(0, 200))}`,
      { signal: AbortSignal.timeout(15000) },
    );
    if (response.status === 404) return [];
    if (!response.ok) throw new Error('Could not search conversation history');
    const data: unknown = await response.json();
    if (!isRecord(data) || !Array.isArray(data.hits)) throw new Error('Invalid history search response');
    return data.hits.filter(isRecord);
  }

  async loadAround(sessionId: string, entryId: string): Promise<HistoryResult | null> {
    this.switchSession(sessionId);
    const generation = ++this.generation;
    const page = await this.fetchPage(sessionId, '', entryId);
    if (generation !== this.generation || this.session !== sessionId) return null;
    this.page = {
      items: dedupe(page.items),
      hasMore: page.has_more,
      cursor: page.before_cursor || null,
      epoch: page.epoch,
      revision: page.snapshot_revision,
      pathGeneration: page.path_generation,
      displayVersion: page.display_version,
      taskStates: page.task_states,
      hasAfter: page.has_after === true,
    };
    return this.page;
  }

  private async fetchPage(sessionId: string, cursor: string, target?: string): Promise<HistoryPage> {
    const url = target
      ? `/api/conversation_tree?${new URLSearchParams({ session_id: sessionId, entry_id: target })}`
      : `/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=100${cursor ? `&before=${encodeURIComponent(cursor)}` : ''}`;
    const response = await requestResponse(url, { signal: AbortSignal.timeout(15000) });
    if (!response.ok) {
      const failure: unknown = await response.json();
      if (
        isRecord(failure) &&
        (failure.error === 'history snapshot changed' ||
          failure.error === 'cursor does not belong to current conversation path')
      )
        throw new HistorySnapshotChanged('Conversation history changed');
      throw new Error('Could not load conversation history');
    }
    const data: unknown = await response.json();
    if (
      !isRecord(data) ||
      data.schema_version !== 1 ||
      data.session_id !== sessionId ||
      !nonempty(data.epoch) ||
      !sequence(data.snapshot_revision) ||
      !sequence(data.path_generation) ||
      !(data.display_version === null || typeof data.display_version === 'string') ||
      !Array.isArray(data.items) ||
      typeof data.has_more !== 'boolean' ||
      (data.has_more && !nonempty(data.before_cursor))
    )
      throw new Error('Invalid conversation history response');
    const items = data.items.map((item: unknown) => {
      if (!isRecord(item) || !nonempty(item.id)) throw new Error('Invalid conversation history entry');
      return item;
    });
    return {
      schema_version: data.schema_version,
      session_id: data.session_id,
      epoch: data.epoch,
      snapshot_revision: data.snapshot_revision,
      path_generation: data.path_generation,
      display_version: data.display_version,
      has_more: data.has_more,
      has_after: data.has_after === true,
      before_cursor: typeof data.before_cursor === 'string' ? data.before_cursor : null,
      task_states: data.task_states === undefined ? undefined : decodeTaskStates(data.task_states),
      items,
    };
  }
}

function dedupe(items: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  const seen = new Set<string>();
  return items.filter((item) => {
    const id = typeof item.id === 'string' ? item.id : JSON.stringify(item);
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}
