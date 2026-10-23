import { afterEach, expect, it, vi } from 'vitest';
import { loadNavigationIndex } from './conversation_nav_index';

afterEach(() => vi.unstubAllGlobals());
const page = (items: Array<{ id: string; text: string }>, cursor = '') => ({
  schema_version: 1,
  session_id: 'chat',
  epoch: 'db',
  snapshot_revision: 1,
  items,
  has_more: !!cursor,
  before_cursor: cursor || null,
});
it('loads the complete compact index in order without loading transcript bodies', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce({ ok: true, json: async () => page([{ id: 'b', text: 'Second' }], 'older') })
    .mockResolvedValueOnce({ ok: true, json: async () => page([{ id: 'a', text: 'First' }]) });
  vi.stubGlobal('fetch', fetch);
  expect(await loadNavigationIndex('chat', new AbortController().signal)).toEqual([
    { id: 'a', text: 'First' },
    { id: 'b', text: 'Second' },
  ]);
  expect(fetch.mock.calls[1][0]).toContain('before=older');
  expect(fetch.mock.calls.every(([url]) => url.includes('navigation=1'))).toBe(true);
});
it('stops when a server repeats its cursor instead of looping', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => page([{ id: 'a', text: 'First' }], 'same') });
  vi.stubGlobal('fetch', fetch);
  await expect(loadNavigationIndex('chat', new AbortController().signal)).rejects.toThrow('did not advance');
  expect(fetch).toHaveBeenCalledTimes(2);
});
