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
}

export class HistoryRepository {
  private session = '';
  private generation = 0;
  private page: HistoryResult | null = null;
  private loading = false;

  switchSession(sessionId: string): void {
    if (this.session !== sessionId) {
      this.session = sessionId;
      this.generation++;
      this.page = null;
      this.loading = false;
    }
  }

  async loadLatest(sessionId: string): Promise<HistoryResult> {
    this.switchSession(sessionId);
    const generation = this.generation;
    const page = await this.fetchPage(sessionId, '');
    if (generation !== this.generation || this.session !== sessionId)
      return this.page || { items: [], hasMore: false, cursor: null, epoch: '' };
    this.page = {
      items: dedupe(page.items),
      hasMore: page.has_more,
      cursor: page.before_cursor || null,
      epoch: page.epoch,
    };
    return this.page;
  }

  async loadEarlier(sessionId: string): Promise<HistoryResult | null> {
    if (sessionId !== this.session) return null;
    if (!this.page?.hasMore || !this.page.cursor || this.loading) return this.page;
    const generation = this.generation;
    this.loading = true;
    try {
      const page = await this.fetchPage(sessionId, this.page.cursor);
      if (generation !== this.generation || this.session !== sessionId) return null;
      this.page = {
        items: dedupe([...page.items, ...this.page.items]),
        hasMore: page.has_more,
        cursor: page.before_cursor || null,
        epoch: page.epoch,
      };
      return this.page;
    } finally {
      this.loading = false;
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
      !Array.isArray(data.items) ||
      typeof data.has_more !== 'boolean'
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
