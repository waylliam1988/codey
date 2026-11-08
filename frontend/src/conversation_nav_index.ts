import { HistorySnapshotChanged } from './history_repository';

export interface NavigationItem {
  id: string;
  text: string;
}

interface NavigationPage {
  schema_version: number;
  session_id: string;
  epoch: string;
  snapshot_revision: number;
  path_generation: number;
  display_version: string | null;
  items: NavigationItem[];
  has_more: boolean;
  before_cursor: string | null;
}

/** Fetch compact question metadata, never eagerly mount the complete transcript. */
export async function loadNavigationIndex(session: string, signal: AbortSignal): Promise<NavigationItem[]> {
  try {
    return await loadNavigationSnapshot(session, signal);
  } catch (error) {
    if (!(error instanceof HistorySnapshotChanged)) throw error;
    return await loadNavigationSnapshot(session, signal);
  }
}

async function loadNavigationSnapshot(session: string, signal: AbortSignal): Promise<NavigationItem[]> {
  const items: NavigationItem[] = [];
  const cursors = new Set<string>();
  let cursor = '';
  let snapshot = '';
  do {
    const query = new URLSearchParams({ session_id: session, navigation: '1' });
    if (cursor) query.set('before', cursor);
    const response = await fetch(`/api/conversation_tree?${query}`, {
      signal: AbortSignal.any([signal, AbortSignal.timeout(15000)]),
    });
    if (response.status === 409) throw new HistorySnapshotChanged('Navigation changed');
    if (!response.ok) throw new Error('Navigation unavailable');
    const page = (await response.json()) as NavigationPage;
    if (
      page.schema_version !== 1 ||
      page.session_id !== session ||
      typeof page.epoch !== 'string' ||
      !Number.isSafeInteger(page.snapshot_revision) ||
      !Number.isSafeInteger(page.path_generation) ||
      !Array.isArray(page.items) ||
      typeof page.has_more !== 'boolean' ||
      !page.items.every(
        (item: NavigationItem) => item && typeof item.id === 'string' && item.id && typeof item.text === 'string',
      )
    )
      throw new Error('Invalid navigation index');
    const identity = `${page.epoch}:${page.path_generation}:${page.display_version}:${page.snapshot_revision}`;
    if (snapshot && identity !== snapshot) throw new HistorySnapshotChanged('Navigation changed');
    snapshot = identity;
    items.unshift(...page.items);
    cursor = page.has_more ? page.before_cursor || '' : '';
    if (page.has_more && (typeof cursor !== 'string' || !cursor || cursors.has(cursor) || !page.items.length))
      throw new Error('Navigation did not advance');
    cursors.add(cursor);
  } while (cursor);
  return [...new Map(items.map((item) => [item.id, item])).values()];
}
