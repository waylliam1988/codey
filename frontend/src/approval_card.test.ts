import { beforeEach, expect, it, vi } from 'vitest';
import './render';
import './process';
import './messages';
import type { Message } from './types/domain';
import { runtime } from './runtime';

const submit = vi.fn();
const refresh = vi.fn();
beforeEach(() => {
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
  expect(chat.querySelector('.sc-meta')?.textContent).toBe('E:/Selfie - 副本');
  expect(chat.textContent).not.toMatch(/Reason|May read or change files/);
  const buttons = chat.querySelectorAll('button');
  expect(Array.from(buttons, (button) => button.textContent)).toEqual(['Deny', 'Allow once']);
  buttons[1].click();
  expect(submit).toHaveBeenCalledExactlyOnceWith('approval', true);
});

it('locks both choices and places submission progress inside the primary button', () => {
  const chat = render({ approvalSubmission: 'sending' });
  expect(Array.from(chat.querySelectorAll('button')).every((button) => button.disabled)).toBe(true);
  const allow = chat.querySelector('.shell-actions button:last-child')!;
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
