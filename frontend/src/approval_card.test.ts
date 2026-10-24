import { beforeEach, expect, it, vi } from 'vitest';
import './render';
import './process';
import './messages';
import type { Message } from './types/domain';
import { runtime } from './runtime';

const submit = vi.fn();
const refresh = vi.fn();
beforeEach(() => {
  vi.stubGlobal('CSS', { escape: (value: string) => value });
  submit.mockClear();
  refresh.mockClear();
  runtime.ShellApprovals = { refresh };
  window.Messages.init({ approveCommand: submit } as unknown as Parameters<typeof window.Messages.init>[0]);
});

function render(fields: Partial<Message> = {}) {
  const chat = document.createElement('div');
  window.Messages.append(chat, {
    type: 'shell_request',
    id: 'approval',
    sessionId: 'chat',
    command: 'python -m pytest --collect-only',
    cwd: 'E:/Selfie - 副本',
    riskTitle: 'Shell command',
    riskDetail: 'May read or change files',
    ...fields,
  });
  return chat;
}

it('shows the approved compact approval layout without a reason section', () => {
  const chat = render();
  expect(chat.querySelector('.sc-title')?.textContent).toBe('Run this command?');
  expect(chat.querySelector('.shell-command')?.textContent).toBe('python -m pytest --collect-only');
  expect(chat.querySelector('.sc-meta')?.textContent).toBe('Working directory · E:/Selfie - 副本');
  expect(chat.textContent).not.toMatch(/Reason|May read or change files/);
  const buttons = chat.querySelectorAll('button');
  expect(Array.from(buttons, (button) => button.textContent).filter((text) => text === 'Allow once')).toHaveLength(1);
  expect(chat.querySelector('.approval-options')?.textContent).not.toContain('Allow once');
  expect(chat.querySelector('.approval-options')?.textContent).toContain('Allow exact command in this chat');
  expect(chat.querySelector('.approval-options')?.textContent).toContain('Manage saved permissions…');
  buttons[1].click();
  expect(submit).toHaveBeenCalledExactlyOnceWith('approval', true);
});

it('does not render a placeholder working directory when it is unavailable', () => {
  const chat = render({ cwd: undefined });
  expect(chat.querySelector('.sc-meta')).toBeNull();
  expect(chat.querySelector('.sc-meta')).toBeNull();
});

it('locks both choices and places submission progress inside the primary button', () => {
  const chat = render({ approvalSubmission: 'sending' });
  expect(Array.from(chat.querySelectorAll('button')).every((button) => button.disabled)).toBe(true);
  const allow = chat.querySelector('.approval-allow')!;
  expect(allow.textContent).toBe('Submitting…');
  expect(allow.querySelector('.spinner')).not.toBeNull();
  expect(chat.querySelector('.sc-error')?.textContent).toBe('');
});

it('preserves uncertain-decision recovery and truncated-command identity', () => {
  const chat = render({ approvalSubmission: 'unconfirmed', commandTruncated: true, commandSha256: 'abc123' });
  expect(chat.textContent).toContain('Decision could not be confirmed');
  expect(chat.textContent).toContain('abc123');
  const check = Array.from(chat.querySelectorAll('button')).find((button) => button.textContent === 'Check status')!;
  check.click();
  expect(refresh).toHaveBeenCalledExactlyOnceWith('approval');
});

it('offers an explicit project rule only when the server permits it', () => {
  const chat = render({ projectRuleAvailable: true });
  const project = Array.from(chat.querySelectorAll('button')).find((button) =>
    button.textContent?.startsWith('Allow exact command in this project'),
  )!;
  project.click();
  expect(submit).toHaveBeenCalledExactlyOnceWith('approval', true, 'project');
  expect(chat.textContent).toContain('Same command and working directory');
});

it('shows a rejected scope error in the original card', () => {
  const chat = render({ approvalError: 'Invalid approval scope' });
  expect(chat.querySelector<HTMLElement>('.sc-error')?.hidden).toBe(false);
  expect(chat.querySelector('.sc-error')?.textContent).toBe('Invalid approval scope');
});

it.each(['before', 'after', 'replace'])('groups approved commands above the answer: %s', (order) => {
  const chat = document.createElement('div');
  const request: Message = { type: 'shell_request', id: 'approved', runId: 'run', command: 'curl.exe weather' };
  const result: Message = {
    ...request,
    type: 'shell_result',
    shellStatus: 'exit',
    approved: true,
    exitCode: 0,
    output: '27 C',
  };
  const answer = document.createElement('div');
  answer.className = 'msg asst';
  answer.dataset.runId = 'run';
  answer.textContent = 'The temperature is 27 C';
  if (order === 'replace') window.Messages.append(chat, request);
  if (order !== 'before') chat.append(answer);
  if (order === 'replace') window.Messages.updateApprovals(chat, [result]);
  else window.Messages.append(chat, result);
  if (order === 'before') chat.append(answer);
  expect(chat.querySelectorAll('.command-card')).toHaveLength(1);
  expect(chat.querySelectorAll(':scope > .command-card, :scope > .shell-card')).toHaveLength(0);
  expect(chat.querySelector('.process-body .command-excerpt')?.textContent).toBe('curl.exe weather');
  expect(chat.firstElementChild?.classList.contains('process-block')).toBe(true);
  expect(chat.lastElementChild).toBe(answer);
  const details = chat.querySelector<HTMLDetailsElement>('.command-card details')!;
  details.open = true;
  window.Messages.updateApprovals(chat, [{ ...result, output: 'Updated output' }]);
  expect(chat.querySelectorAll('.command-card')).toHaveLength(1);
  expect(chat.querySelector<HTMLDetailsElement>('.command-card details')?.open).toBe(true);
  expect(chat.lastElementChild).toBe(answer);
});

it('settles one approval from mixed live/history result records rather than selecting the first', () => {
  const chat = render({ runId: 'run' });
  const running: Message = {
    type: 'shell_result',
    id: 'approval',
    runId: 'run',
    approved: true,
    shellStatus: 'running',
    command: 'curl.exe weather',
  };
  const final: Message = { ...running, shellStatus: 'exit', exitCode: 0, output: '27 C' };
  window.Messages.updateApprovals(chat, [running, final, running]);
  expect(chat.querySelectorAll('.command-card')).toHaveLength(1);
  expect(chat.querySelector('.command-card')?.getAttribute('data-status')).toBe('success');
  expect(chat.querySelector('.shell-stdout')?.textContent).toBe('27 C');
});
