import { afterEach, describe, expect, it, vi } from 'vitest';
import { HistoryRepository } from './history_repository';

afterEach(() => vi.unstubAllGlobals());

describe('HistoryRepository', () => {
  it('loads a tail page, prepends earlier rows, and rejects a late old session', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          schema_version: 1,
          session_id: 's1',
          epoch: 'e1',
          snapshot_revision: 3,
          items: [{ id: 'm2', ordinal: 2 }],
          before_cursor: 'c1',
          has_more: true,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          schema_version: 1,
          session_id: 's1',
          epoch: 'e1',
          snapshot_revision: 3,
          items: [{ id: 'm1', ordinal: 1 }],
          before_cursor: null,
          has_more: false,
        }),
      });
    vi.stubGlobal('fetch', fetcher);
    const repo = new HistoryRepository();
    await expect(repo.loadLatest('s1')).resolves.toMatchObject({ items: [{ id: 'm2' }], hasMore: true });
    await expect(repo.loadEarlier('s1')).resolves.toMatchObject({ items: [{ id: 'm1' }, { id: 'm2' }] });
    expect(fetcher.mock.calls[1][0]).toContain('before=c1');
    repo.switchSession('s2');
    await expect(repo.loadEarlier('s1')).resolves.toBeNull();
  });

  it('searches literal text and returns bounded hits', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ hits: [{ message_id: 'm1', excerpt: 'cache' }] }) }),
    );
    await expect(new HistoryRepository().search('s1', 'cache')).resolves.toEqual([
      { message_id: 'm1', excerpt: 'cache' },
    ]);
  });
});
