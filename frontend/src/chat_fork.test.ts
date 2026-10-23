import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { requestJson } from './api/transport';
import { ChatFork } from './chat_fork';

vi.mock('./api/transport', () => ({ requestJson: vi.fn(), jsonBody: JSON.stringify }));
const request = vi.mocked(requestJson);

it('keeps an accepted child without navigating after its creation dialog was dismissed', async () => {
  const { fork, deps } = setup();
  let finish: (value: unknown) => void = () => {};
  request
    .mockResolvedValueOnce({ worktree: true })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValue({ origin: null });
  await fork.open('source', 'question');
  click(fork.dialog, 'Fork in this workspace');
  await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  fork.dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
  finish({ session_id: 'child', state: { revision: 2 } });
  await vi.waitFor(() =>
    expect(deps.adopt).toHaveBeenCalledWith(
      { session_id: 'child', state: { revision: 2 } },
      { active_id: 'source' },
      false,
    ),
  );
});

it('keeps worktree input blocked and offers an explicit retry when its status cannot be checked', async () => {
  const { fork } = setup(true);
  request.mockRejectedValue(new Error('Offline'));
  await fork.showOrigin('source');
  expect(fork.isBlocked('source')).toBe(true);
  expect(document.querySelector('.chat-fork-setup')?.textContent).toContain('Retry to reconnect.');
  expect([...document.querySelectorAll('.chat-fork-setup button')].map((button) => button.textContent)).toEqual([
    'Retry',
  ]);
});
const controllers: ChatFork[] = [];
beforeEach(() => {
  document.body.innerHTML =
    '<div id="chat"><article data-entry-id="answer">Answer</article></div><textarea id="task"></textarea>';
  sessionStorage.clear();
  request.mockReset();
});
afterEach(() => {
  controllers.splice(0).forEach((controller) => controller.dispose());
  vi.useRealTimers();
  vi.restoreAllMocks();
});
function setup(worktree = false) {
  let active = 'source';
  const deps = {
    activeId: () => active,
    isWorktree: () => worktree,
    workspaceChanged: vi.fn(),
    saved: vi.fn().mockResolvedValue(true),
    state: () => ({ active_id: 'source' }),
    adopt: vi.fn((result) => {
      active = result.session_id;
    }),
    switchSession: vi.fn(),
  };
  const fork = new ChatFork(deps);
  controllers.push(fork);
  fork.dialog.showModal = () => {
    fork.dialog.setAttribute('open', '');
  };
  fork.dialog.close = () => {
    fork.dialog.removeAttribute('open');
    fork.dialog.dispatchEvent(new Event('close'));
  };
  return { fork, deps };
}
const click = (root: Element, label: string) => {
  const button = [...root.querySelectorAll('button')].find((button) => button.textContent?.includes(label));
  expect(button).toBeDefined();
  button!.click();
};

it('creates directly from a non-Git directory and sends the whole-turn boundary', async () => {
  const { fork, deps } = setup();
  request
    .mockResolvedValueOnce({ worktree: false })
    .mockResolvedValueOnce({ session_id: 'child', state: { revision: 2 } })
    .mockResolvedValue({ origin: null });
  await fork.open('source', 'question');
  expect(deps.adopt).toHaveBeenCalledWith(
    { session_id: 'child', state: { revision: 2 } },
    { active_id: 'source' },
    true,
  );
  const payload = JSON.parse((request.mock.calls[1][1] as RequestInit).body as string);
  expect(payload).toMatchObject({ action: 'fork', boundary: 'turn', destination: 'local', entry_id: 'question' });
  expect(fork.dialog.open).toBe(false);
});

it('offers both Git destinations and preserves request identity after an uncertain submission', async () => {
  const { fork, deps } = setup();
  request
    .mockResolvedValueOnce({ worktree: true })
    .mockRejectedValueOnce(new Error('Connection interrupted'))
    .mockResolvedValueOnce({ session_id: 'child', state: { revision: 2 } })
    .mockResolvedValue({ origin: null });
  await fork.open('source', 'question');
  expect(fork.dialog.textContent).toContain('Fork in this workspace');
  expect(fork.dialog.textContent).toContain('Fork in a new worktree');
  click(fork.dialog, 'Fork in this workspace');
  await vi.waitFor(() => expect(fork.dialog.textContent).toContain('Connection interrupted'));
  click(fork.dialog, 'Retry');
  await vi.waitFor(() => expect(deps.adopt).toHaveBeenCalled());
  const bodies = request.mock.calls
    .filter((call) => call[0] === '/api/conversation_tree/action')
    .map((call) => JSON.parse((call[1] as RequestInit).body as string));
  expect(bodies).toHaveLength(2);
  expect(bodies[0].request_id).toBe(bodies[1].request_id);
});

it('does not submit after cancellation while waiting for the source save', async () => {
  const { fork, deps } = setup();
  let done: (value: boolean) => void = () => {};
  deps.saved.mockImplementation(
    () =>
      new Promise((resolve) => {
        done = resolve;
      }),
  );
  request.mockResolvedValue({ worktree: true });
  await fork.open('source', 'question');
  click(fork.dialog, 'Fork in this workspace');
  fork.dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
  done(true);
  await Promise.resolve();
  await Promise.resolve();
  expect(request).toHaveBeenCalledTimes(1);
  expect(deps.adopt).not.toHaveBeenCalled();
});

it('adopts a pending worktree immediately and blocks input until its authoritative ready state', async () => {
  const { fork, deps } = setup(true);
  request
    .mockResolvedValueOnce({ worktree: true })
    .mockResolvedValueOnce({ session_id: 'child', state: { revision: 2 }, job: { status: 'pending' } })
    .mockResolvedValue({ job: { status: 'pending' }, origin: { session_id: 'source', entry_id: 'answer' } });
  await fork.open('source', 'question');
  click(fork.dialog, 'Fork in a new worktree');
  await vi.waitFor(() => expect(deps.adopt).toHaveBeenCalled());
  await vi.waitFor(() => expect(document.body.textContent).toContain('Creating worktree…'));
  expect(fork.isBlocked('child')).toBe(true);
  request.mockResolvedValue({ job: { status: 'ready' }, origin: { session_id: 'source', entry_id: 'answer' } });
  await fork.showOrigin('child');
  expect(fork.isBlocked('child')).toBe(false);
  expect(document.querySelector('.chat-fork-setup')).toBeNull();
  click(document.querySelector('#chat')!, 'Continued from chat');
  expect(deps.switchSession).toHaveBeenCalledWith('source');
});

it('restores a failed setup with retry against the same accepted child', async () => {
  const { fork } = setup(true);
  request
    .mockResolvedValueOnce({ worktree: true })
    .mockResolvedValueOnce({ session_id: 'child', state: {}, job: { status: 'error' } })
    .mockResolvedValue({
      job: { status: 'error', error: 'Setup interrupted' },
      origin: { session_id: 'source', entry_id: 'answer' },
    });
  await fork.open('source', 'question');
  click(fork.dialog, 'Fork in a new worktree');
  await vi.waitFor(() =>
    expect(document.querySelector('.chat-fork-setup')?.textContent).toContain('Setup interrupted'),
  );
  request
    .mockResolvedValueOnce({ ok: true })
    .mockResolvedValue({ job: { status: 'ready' }, origin: { session_id: 'source', entry_id: 'answer' } });
  click(document.querySelector('.chat-fork-setup')!, 'Retry');
  await vi.waitFor(() => expect(fork.isBlocked('child')).toBe(false));
  const retry = request.mock.calls.find((call) => call[0] === '/api/conversation_fork/retry')!;
  expect(JSON.parse((retry[1] as RequestInit).body as string)).toEqual({ session_id: 'child' });
});
