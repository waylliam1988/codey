import { afterEach, describe, expect, it, vi } from 'vitest';
import { HistoryRepository } from './history_repository';

afterEach(() => vi.unstubAllGlobals());

describe('HistoryRepository', () => {
  function reply(id: string, revision = 1, more = false, epoch = 'database') {
    return {
      ok: true,
      json: async () => ({
        schema_version: 1,
        session_id: 's1',
        epoch,
        snapshot_revision: revision,
        items: [{ id }],
        before_cursor: more ? 'cursor' : null,
        has_more: more,
      }),
    };
  }

  it('does not let an older refresh overwrite a newer same-session refresh', async () => {
    let release!: (value: ReturnType<typeof reply>) => void;
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              release = resolve;
            }),
        )
        .mockResolvedValueOnce(reply('new', 2)),
    );
    const repo = new HistoryRepository();
    const old = repo.loadLatest('s1');
    expect((await repo.loadLatest('s1'))?.items).toEqual([{ id: 'new' }]);
    release(reply('old'));
    expect(await old).toBeNull();
  });

  it('discards an earlier page racing with a branch refresh', async () => {
    let release!: (value: ReturnType<typeof reply>) => void;
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(reply('old-tail', 1, true))
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              release = resolve;
            }),
        )
        .mockResolvedValueOnce(reply('new-branch', 2)),
    );
    const repo = new HistoryRepository();
    await repo.loadLatest('s1');
    const earlier = repo.loadEarlier('s1');
    await repo.loadLatest('s1');
    release(reply('old-ancestor'));
    expect(await earlier).toBeNull();
  });

  it.each(['revision', 'epoch'])('reloads instead of splicing different %s snapshots', async (change) => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(reply('tail', 1, true))
        .mockResolvedValueOnce(
          reply('different', change === 'revision' ? 2 : 1, false, change === 'epoch' ? 'other' : 'database'),
        )
        .mockResolvedValueOnce(reply('current', 2)),
    );
    const repo = new HistoryRepository();
    await repo.loadLatest('s1');
    expect((await repo.loadEarlier('s1'))?.items).toEqual([{ id: 'current' }]);
  });

  it('rejects invalid entry identities and snapshot revisions', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          schema_version: 1,
          session_id: 's1',
          epoch: 'db',
          snapshot_revision: '1',
          items: [null],
          has_more: false,
        }),
      }),
    );
    await expect(new HistoryRepository().loadLatest('s1')).rejects.toThrow('Invalid conversation history');
  });

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

  it('stops a non-advancing cursor instead of repeatedly loading the same page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply('same', 1, true)));
    const repo = new HistoryRepository();
    await repo.loadLatest('s1');
    await expect(repo.loadEarlier('s1')).rejects.toThrow('did not advance');
  });

  it('cancels an in-flight tail refresh when the reader moves up, retaining loaded ancestors', async () => {
    let release!: (value: ReturnType<typeof reply>) => void;
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(reply('tail', 1, true))
        .mockResolvedValueOnce(reply('ancestor'))
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              release = resolve;
            }),
        ),
    );
    const repo = new HistoryRepository();
    await repo.loadLatest('s1');
    await repo.loadEarlier('s1');
    const refresh = repo.loadLatest('s1');
    repo.cancelLatest();
    release(reply('new-tail', 2));
    expect(await refresh).toBeNull();
    expect(repo.current?.items.map((item) => item.id)).toEqual(['ancestor', 'tail']);
  });
});
