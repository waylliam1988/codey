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

it('waits for an explicit local choice in a non-Git directory and sends the whole-turn boundary', async () => {
  const { fork, deps } = setup();
  request
    .mockResolvedValueOnce({ worktree: false })
    .mockResolvedValueOnce({ session_id: 'child', state: { revision: 2 } })
    .mockResolvedValue({ origin: null });
  await fork.open('source', 'question');
  expect(fork.dialog.open).toBe(true);
  expect(request).toHaveBeenCalledTimes(1);
  expect(deps.saved).not.toHaveBeenCalled();
  expect(deps.adopt).not.toHaveBeenCalled();
  click(fork.dialog, 'Fork in this workspace');
  await vi.waitFor(() => expect(deps.adopt).toHaveBeenCalled());
  expect(deps.adopt).toHaveBeenCalledWith(
    { session_id: 'child', state: { revision: 2 } },
    { active_id: 'source' },
    true,
  );
  const payload = JSON.parse((request.mock.calls[1][1] as RequestInit).body as string);
  expect(payload).toMatchObject({ action: 'fork', boundary: 'turn', destination: 'local', entry_id: 'question' });
  expect(fork.dialog.open).toBe(false);
});

it.each([
  [
    { worktree: false, worktree_reason: 'no_project', worktree_message: 'This chat is not linked to a Git project.' },
    'current environment',
    false,
  ],
  [
    {
      worktree: false,
      worktree_reason: 'not_git',
      worktree_message: 'This project directory is not a Git working directory.',
    },
    'this workspace',
    false,
  ],
  [
    {
      worktree: false,
      worktree_reason: 'no_commits',
      worktree_message: 'Create a commit before using a new worktree.',
    },
    'this workspace',
    false,
  ],
  [{ worktree: true, is_worktree: false, worktree_status: 'available' }, 'this workspace', true],
  [{ worktree: true, is_worktree: true, worktree_status: 'available' }, 'this worktree', true],
] as const)('keeps both choices open without creating for capability %j', async (metadata, label, available) => {
  const { fork, deps } = setup();
  request.mockResolvedValue(metadata);
  await fork.open('source', 'question');
  const buttons = fork.dialog.querySelectorAll<HTMLButtonElement>('.chat-fork-destination');
  expect(buttons).toHaveLength(2);
  expect(buttons[0].textContent).toContain(label);
  expect(buttons[0].disabled).toBe(false);
  expect(buttons[1].disabled).toBe(!available);
  if ('worktree_message' in metadata) expect(buttons[1].textContent).toContain(metadata.worktree_message);
  if (!available) buttons[1].click();
  expect(fork.dialog.open).toBe(true);
  expect(request).toHaveBeenCalledTimes(1);
  expect(deps.saved).not.toHaveBeenCalled();
  expect(deps.adopt).not.toHaveBeenCalled();
  fork.dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
  expect(fork.dialog.open).toBe(false);
  expect(request).toHaveBeenCalledTimes(1);
});

it.each(['network', 'server', 'malformed'])(
  'keeps local choice available after a %s check failure and retries only the check',
  async (kind) => {
    const { fork, deps } = setup();
    if (kind === 'network') request.mockRejectedValueOnce(new Error('Offline'));
    else if (kind === 'server')
      request.mockResolvedValueOnce({
        worktree: false,
        worktree_status: 'error',
        worktree_message: 'Git check timed out.',
      });
    else request.mockResolvedValueOnce({ ok: true });
    request.mockResolvedValue({ worktree: true });
    await fork.open('source', 'question');
    const local = fork.dialog.querySelector<HTMLButtonElement>('.chat-fork-destination')!;
    const worktree = fork.dialog.querySelectorAll<HTMLButtonElement>('.chat-fork-destination')[1];
    expect(local.disabled).toBe(false);
    expect(worktree.disabled).toBe(true);
    expect(fork.dialog.open).toBe(true);
    expect(deps.saved).not.toHaveBeenCalled();
    expect(deps.adopt).not.toHaveBeenCalled();
    click(fork.dialog, 'Retry');
    await vi.waitFor(() => expect(worktree.disabled).toBe(false));
    expect(local).toBe(fork.dialog.querySelector('.chat-fork-destination'));
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls.every(([url]) => String(url).includes('/api/conversation_fork?'))).toBe(true);
    expect(deps.adopt).not.toHaveBeenCalled();
  },
);

it('keeps an unavailable worktree disabled when local creation fails and explicitly retries the same child', async () => {
  const { fork, deps } = setup();
  request
    .mockResolvedValueOnce({ worktree: false, worktree_reason: 'no_project' })
    .mockRejectedValueOnce(new Error('Creation failed'))
    .mockResolvedValueOnce({ session_id: 'child', state: { revision: 2 } })
    .mockResolvedValue({ origin: null });
  await fork.open('source', 'question');
  click(fork.dialog, 'Fork in the current environment');
  await vi.waitFor(() => expect(fork.dialog.textContent).toContain('Creation failed'));
  expect(fork.dialog.open).toBe(true);
  const [local, worktree] = fork.dialog.querySelectorAll<HTMLButtonElement>('.chat-fork-destination');
  expect(local.disabled).toBe(false);
  expect(worktree.disabled).toBe(true);
  click(fork.dialog, 'Retry');
  await vi.waitFor(() => expect(deps.adopt).toHaveBeenCalled());
  const bodies = request.mock.calls
    .filter(([url]) => url === '/api/conversation_tree/action')
    .map(([, init]) => JSON.parse(init!.body as string));
  expect(bodies).toHaveLength(2);
  expect(bodies[0].request_id).toBe(bodies[1].request_id);
  expect(fork.dialog.open).toBe(false);
});

it('allows local creation after a failed check without accidentally enabling worktree', async () => {
  const { fork, deps } = setup();
  request
    .mockRejectedValueOnce(new Error('Offline'))
    .mockResolvedValueOnce({ session_id: 'child', state: { revision: 2 } })
    .mockResolvedValue({ origin: null });
  await fork.open('source', 'question');
  click(fork.dialog, 'Fork in the current environment');
  await vi.waitFor(() => expect(deps.adopt).toHaveBeenCalled());
  const payload = JSON.parse(request.mock.calls[1][1]!.body as string);
  expect(payload.destination).toBe('local');
});

it('distinguishes retrying a failed capability check from retrying a failed local creation', async () => {
  const { fork, deps } = setup();
  request
    .mockRejectedValueOnce(new Error('Git check failed'))
    .mockRejectedValueOnce(new Error('Creation failed'))
    .mockResolvedValueOnce({ session_id: 'child', state: { revision: 2 } })
    .mockResolvedValue({ origin: null });
  await fork.open('source', 'question');
  click(fork.dialog, 'Fork in the current environment');
  await vi.waitFor(() => expect(fork.dialog.textContent).toContain('Creation failed'));
  expect(fork.dialog.textContent).toContain('Retry workspace check');
  click(fork.dialog, 'Retry creation');
  await vi.waitFor(() => expect(deps.adopt).toHaveBeenCalledTimes(1));
  expect(request.mock.calls.filter(([url]) => url === '/api/conversation_tree/action')).toHaveLength(2);
});

it('does not reopen or create when a workspace check fails after dismissal', async () => {
  const { fork, deps } = setup();
  let reject: (reason: Error) => void = () => {};
  request.mockImplementationOnce(
    () =>
      new Promise((_resolve, failure) => {
        reject = failure;
      }),
  );
  const opening = fork.open('source', 'question');
  fork.dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
  reject(new Error('Late check failure'));
  await opening;
  expect(fork.dialog.open).toBe(false);
  expect(fork.dialog.textContent).not.toContain('Late check failure');
  expect(request).toHaveBeenCalledTimes(1);
  expect(deps.saved).not.toHaveBeenCalled();
  expect(deps.adopt).not.toHaveBeenCalled();
});

it('shows both choices while checking and does not enable them during an in-flight local submission', async () => {
  const { fork, deps } = setup();
  let checked: (value: unknown) => void = () => {};
  let created: (value: unknown) => void = () => {};
  request
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          checked = resolve;
        }),
    )
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          created = resolve;
        }),
    )
    .mockResolvedValue({ origin: null });
  const opening = fork.open('source', 'question');
  const [local, worktree] = fork.dialog.querySelectorAll<HTMLButtonElement>('.chat-fork-destination');
  expect(fork.dialog.open).toBe(true);
  expect(local.disabled).toBe(false);
  expect(worktree.disabled).toBe(true);
  expect(worktree.textContent).toContain('Checking Git workspace');
  local.click();
  local.click();
  await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  checked({ worktree: true });
  await opening;
  expect(local.disabled).toBe(true);
  expect(worktree.disabled).toBe(true);
  expect(fork.dialog.textContent).toContain('Creating chat');
  expect(deps.adopt).not.toHaveBeenCalled();
  created({ session_id: 'child', state: { revision: 2 } });
  await vi.waitFor(() => expect(deps.adopt).toHaveBeenCalledTimes(1));
});

it('ignores a check that finishes after the dialog is dismissed or replaced for another chat', async () => {
  const { fork, deps } = setup();
  let checked: (value: unknown) => void = () => {};
  request
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          checked = resolve;
        }),
    )
    .mockResolvedValue({ worktree: false, worktree_reason: 'no_project' });
  const first = fork.open('source', 'question');
  fork.dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
  expect(fork.dialog.open).toBe(false);
  await fork.open('other', 'answer');
  checked({ worktree: true });
  await first;
  expect(fork.dialog.open).toBe(true);
  expect(fork.dialog.querySelectorAll<HTMLButtonElement>('.chat-fork-destination')[1].disabled).toBe(true);
  expect(fork.dialog.textContent).toContain('current environment');
  expect(deps.saved).not.toHaveBeenCalled();
  expect(deps.adopt).not.toHaveBeenCalled();
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
