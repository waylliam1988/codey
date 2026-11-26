import { expect, it, vi } from 'vitest';
import { bootApplication } from './application_boot';
import { reportDesktopReady } from './desktop_ready';
import { OperatorAuth } from './operator_auth';

vi.mock('./character_pond', () => ({ mountCharacterPond: vi.fn() }));
vi.mock('./desktop_ready', () => ({ reportDesktopReady: vi.fn() }));

it('restores authoritative state before enabling input, connecting events or reporting ready', async () => {
  const activity: string[] = [];
  const record = (name: string) => () => {
    activity.push(name);
  };
  const asyncRecord = (name: string) => async () => {
    activity.push(name);
  };
  const authorize = vi.spyOn(OperatorAuth, 'authorize').mockImplementation(asyncRecord('authorize'));
  vi.mocked(reportDesktopReady).mockImplementation(record('ready'));
  try {
    await bootApplication({
      sessions: { restore: asyncRecord('session state') },
      view: { mount: record('view'), render: record('render'), renderSidebar: record('sidebar'), scroll: vi.fn() },
      provider: { adoptCatalog: asyncRecord('catalog'), mount: record('provider'), refresh: record('refresh') },
      runs: { mount: record('runs'), connect: record('events'), setStatus: vi.fn() },
      composer: { mount: record('composer'), prime: record('prime'), restoreAndEnable: asyncRecord('composer ready') },
    });
    expect(activity).toEqual([
      'authorize',
      'catalog',
      'provider',
      'runs',
      'composer',
      'view',
      'sidebar',
      'render',
      'prime',
      'refresh',
      'session state',
      'composer ready',
      'events',
      'ready',
    ]);
  } finally {
    authorize.mockRestore();
  }
});
