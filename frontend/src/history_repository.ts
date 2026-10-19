export interface HistoryPage {
  schema_version: 1;
  session_id: string;
  epoch: string;
  snapshot_revision: number;
  items: Array<Record<string, unknown>>;
  before_cursor?: string | null;
  has_more: boolean;
}

export interface HistoryResult {
  items: Array<Record<string, unknown>>;
  hasMore: boolean;
  cursor: string | null;
  epoch: string;
  revision: number;
}

export class HistoryRepository {
  private session = '';
  private generation = 0;
  private page: HistoryResult | null = null;
  private loading = false;

  get current(): HistoryResult | null {
    return this.page;
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
    const page = await this.fetchPage(sessionId, '');
    if (generation !== this.generation || this.session !== sessionId) return null;
    this.page = {
      items: dedupe(page.items),
      hasMore: page.has_more,
      cursor: page.before_cursor || null,
      epoch: page.epoch,
      revision: page.snapshot_revision,
    };
    return this.page;
  }

  async loadEarlier(sessionId: string): Promise<HistoryResult | null> {
    if (sessionId !== this.session) return null;
    if (!this.page?.hasMore || !this.page.cursor || this.loading) return this.page;
    const generation = this.generation;
    const previous = this.page;
    this.loading = true;
    try {
      const page = await this.fetchPage(sessionId, this.page.cursor);
      if (generation !== this.generation || this.session !== sessionId) return null;
      if (page.epoch !== previous.epoch || page.snapshot_revision !== previous.revision)
        return await this.loadLatest(sessionId);
      this.page = {
        items: dedupe([...page.items, ...previous.items]),
        hasMore: page.has_more,
        cursor: page.before_cursor || null,
        epoch: page.epoch,
        revision: page.snapshot_revision,
      };
      return this.page;
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  async search(sessionId: string, query: string): Promise<Array<Record<string, unknown>>> {
    if (!query.trim()) return [];
    const response = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages/search?q=${encodeURIComponent(query.trim().slice(0, 200))}`,
    );
    if (!response.ok) throw new Error('Could not search conversation history');
    const data = (await response.json()) as { hits?: unknown };
    return Array.isArray(data.hits)
      ? data.hits.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
      : [];
  }

  private async fetchPage(sessionId: string, cursor: string): Promise<HistoryPage> {
    const url = `/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=100${cursor ? `&before=${encodeURIComponent(cursor)}` : ''}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error('Could not load conversation history');
    const data = (await response.json()) as Partial<HistoryPage>;
    if (
      data.schema_version !== 1 ||
      data.session_id !== sessionId ||
      typeof data.epoch !== 'string' ||
      !data.epoch ||
      !Number.isSafeInteger(data.snapshot_revision) ||
      (data.snapshot_revision as number) < 0 ||
      !Array.isArray(data.items) ||
      !data.items.every(
        (item) => item && typeof item === 'object' && typeof item.id === 'string' && item.id.length > 0,
      ) ||
      typeof data.has_more !== 'boolean' ||
      (data.has_more && (typeof data.before_cursor !== 'string' || !data.before_cursor))
    )
      throw new Error('Invalid conversation history response');
    return data as HistoryPage;
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
