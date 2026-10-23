export interface NavigationItem {
  id: string;
  text: string;
}

/** Fetch compact question metadata, never eagerly mount the complete transcript. */
export async function loadNavigationIndex(session: string, signal: AbortSignal): Promise<NavigationItem[]> {
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
    if (!response.ok) throw new Error('Navigation unavailable');
    const page = await response.json();
    if (
      page.schema_version !== 1 ||
      page.session_id !== session ||
      !Array.isArray(page.items) ||
      typeof page.has_more !== 'boolean' ||
      !page.items.every(
        (item: NavigationItem) => item && typeof item.id === 'string' && item.id && typeof item.text === 'string',
      )
    )
      throw new Error('Invalid navigation index');
    const identity = `${page.epoch}:${page.snapshot_revision}`;
    if (snapshot && identity !== snapshot) throw new Error('Navigation changed');
    snapshot = identity;
    items.unshift(...page.items);
    cursor = page.has_more ? page.before_cursor : '';
    if (page.has_more && (typeof cursor !== 'string' || !cursor || cursors.has(cursor) || !page.items.length))
      throw new Error('Navigation did not advance');
    cursors.add(cursor);
  } while (cursor);
  return [...new Map(items.map((item) => [item.id, item])).values()];
}
