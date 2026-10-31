import { expect, it, vi } from 'vitest';
import { commandCard } from './command_card';
import type { Message } from './types/domain';
import './render';
import './process';

const render = (chat: HTMLElement, message: Message) => chat.append(commandCard(message, vi.fn()));
const result = (callId = 'call-1', runId = 'run-1'): Message => ({
  type: 'shell_result',
  id: `approval-${runId}-${callId}`,
  callId,
  runId,
  command: 'echo marker',
  output: 'marker',
  shellStatus: 'exit',
  approved: true,
  exitCode: 0,
});
const tool = (callId = 'call-1', runId = 'run-1'): Message => ({
  type: 'tool',
  runId,
  callId,
  toolKey: `${runId}:${callId}`,
  kind: 'shell',
  toolName: 'shell',
  command: 'echo marker',
  result: 'marker',
});

it.each([true, false])('shows an approved execution once when the command card arrives first: %s', (cardFirst) => {
  const chat = document.createElement('div');
  const messages = cardFirst ? [result(), tool()] : [tool(), result()];
  messages.forEach((message) => window.Process.append(chat, message, render));
  // Reconnect/history replay and a late start must not recreate the generic row.
  window.Process.append(chat, { ...tool(), type: 'tool_pending', pending: true }, render);
  window.Process.append(chat, tool(), render);
  window.Process.append(chat, result(), render);
  expect(chat.querySelectorAll('.command-card')).toHaveLength(1);
  expect(chat.querySelectorAll('.tool-line')).toHaveLength(0);
  expect(chat.querySelector('.shell-stdout')?.textContent).toBe('marker');
  expect(chat.querySelector('.process-title')?.textContent).toContain('ran 1 command');
});

it('keeps identical commands with different call or run identities visible', () => {
  const chat = document.createElement('div');
  window.Process.append(chat, result(), render);
  window.Process.append(chat, tool(), render);
  window.Process.append(chat, tool('call-2'), render);
  window.Process.append(chat, tool('call-1', 'run-2'), render);
  expect(chat.querySelectorAll('.command-card')).toHaveLength(1);
  expect(chat.querySelectorAll('.tool-line')).toHaveLength(2);
});

it('retains the open command output and ignores a late running snapshot', () => {
  const chat = document.createElement('div');
  window.Process.append(chat, tool(), render);
  window.Process.append(chat, result(), render);
  chat.querySelector<HTMLDetailsElement>('.command-card details')!.open = true;
  window.Process.append(chat, { ...result(), shellStatus: 'running', exitCode: null, output: '' }, render);
  expect(chat.querySelector<HTMLDetailsElement>('.command-card details')!.open).toBe(true);
  expect(chat.querySelector('.shell-stdout')?.textContent).toBe('marker');
  expect(chat.querySelectorAll('.tool-line')).toHaveLength(0);
});
