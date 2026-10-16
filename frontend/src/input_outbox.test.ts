import { afterEach, expect, it, vi } from 'vitest';
import { InputOutbox } from './input_outbox';

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});
it('retains a stable delivery identity across a lost response and restart', async () => {
  const request = { session_id: 'chat', run_id: 'run_one', task: '补充', provider: 'local' };
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('lost response')));
  const host = document.createElement('div');
  const box = new InputOutbox(host, { accepted: vi.fn(), edit: vi.fn(), changed: vi.fn(), observe: vi.fn() });
  box.setSession('chat');
  expect(await box.enqueue(request, 3)).toBe(false);
  expect(box.blocked('chat')).toBe(true);
  const saved = JSON.parse(localStorage.getItem('codey:pending-input-delivery')!);
  expect(saved[0].request.run_id).toBe('run_one');
  const accepted = vi.fn();
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      json: async () => ({ ok: true, item: { ...saved[0], state: 'queued', revision: 1 } }),
    })),
  );
  const restored = new InputOutbox(host, { accepted, edit: vi.fn(), changed: vi.fn(), observe: vi.fn() });
  restored.setSession('chat');
  await restored.check(saved[0].id);
  expect(accepted).toHaveBeenCalledTimes(1);
  expect(restored.blocked('chat')).toBe(false);
});

it('ignores older snapshots and edits only a still-queued item', async () => {
  const edit = vi.fn(),
    host = document.createElement('div');
  const box = new InputOutbox(host, { accepted: vi.fn(), edit, changed: vi.fn(), observe: vi.fn() });
  box.setSession('chat');
  const item = {
    id: 'one',
    revision: 3,
    state: 'queued',
    request: { session_id: 'chat', run_id: 'run_one', task: '补充', provider: 'local' },
  };
  box.apply(item);
  box.apply({ ...item, revision: 2, state: 'submitted' });
  expect(host.textContent).toContain('Queued');
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      json: async () => ({ ok: true, item: { ...item, revision: 4, state: 'cancelled' } }),
    })),
  );
  host.querySelector<HTMLButtonElement>('[data-action=edit]')!.click();
  await vi.waitFor(() => expect(edit).toHaveBeenCalledWith('chat', '补充'));
});

it('releases a delivery that the server explicitly reports as not accepted', async () => {
  const edit = vi.fn();
  const host = document.createElement('div');
  const request = { session_id: 'chat', run_id: 'run_rejected', task: '恢复草稿', provider: 'local' };
  vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('lost response')));
  const box = new InputOutbox(host, { accepted: vi.fn(), edit, changed: vi.fn(), observe: vi.fn() });
  box.setSession('chat');
  expect(await box.enqueue(request, 4)).toBe(false);
  const saved = JSON.parse(localStorage.getItem('codey:pending-input-delivery')!);
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ ok: false, error: 'input_not_found' }),
    }),
  );

  await box.check(saved[0].id);

  expect(box.blocked('chat')).toBe(false);
  expect(host.textContent).toContain('Not sent');
  expect(host.querySelector('[data-action="check"]')).toBeNull();
  expect(host.querySelector('[data-action="edit"]')).not.toBeNull();
  host.querySelector<HTMLButtonElement>('[data-action="edit"]')!.click();
  await vi.waitFor(() => expect(edit).toHaveBeenCalledWith('chat', '恢复草稿'));
});
