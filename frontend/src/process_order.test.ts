import { expect, it, vi } from 'vitest';
import { commandCard } from './command_card';
import type { Message } from './types/domain';
import './render';
import './process';

function call(id: string, kind = 'codemode', extra: Partial<Message> = {}): Message {
  return {
    type: 'tool_pending',
    pending: true,
    runId: 'run',
    callId: id,
    toolKey: `run:${id}`,
    toolName: kind,
    kind,
    turn: 1,
    path: `${id}.ts`,
    ...extra,
  };
}
const render = (host: HTMLElement, message: Message) => host.append(commandCard(message, vi.fn()));
const append = (host: HTMLElement, message: Message) => window.Process.append(host, message, render);
const complete = (host: HTMLElement, message: Message, extra: Partial<Message> = {}) =>
  append(host, { ...message, type: 'tool', pending: false, result: 'Retained result', ...extra });
const order = (host: Element) =>
  [...host.querySelectorAll<HTMLElement>('[data-call-id]')].map((node) => node.dataset.callId);

it.each(['codemode', 'read', 'search', 'ls', 'references', 'edit', 'write', 'apply_patch', 'run', 'custom_tool'])(
  'keeps a %s completion before a later, still-running tool',
  (kind) => {
    const host = document.createElement('div');
    const first = call('first', kind),
      later = call('later', 'codemode');
    append(host, first);
    append(host, later);
    const laterNode = host.querySelector('[data-call-id="later"]');
    complete(host, first);
    expect(order(host)).toEqual(['first', 'later']);
    expect(host.querySelector('[data-call-id="later"]')).toBe(laterNode);
    complete(host, later);
    complete(host, first);
    expect(order(host)).toEqual(['first', 'later']);
  },
);

it('groups consecutive read starts once and keeps order and expansion through reverse completion and failure', () => {
  const host = document.createElement('div');
  const first = call('a', 'read'),
    second = call('b', 'read');
  append(host, first);
  append(host, second);
  const group = host.querySelector<HTMLElement>('.tool-group')!;
  expect(group.querySelector('.tg-count')?.textContent).toBe('2 files · 2 running');
  group.querySelector<HTMLButtonElement>('button')!.click();
  complete(host, second);
  expect(order(group)).toEqual(['a', 'b']);
  complete(host, first, { error: true, result: 'Read failed' });
  expect(host.querySelector('.tool-group')).toBe(group);
  expect(group.classList.contains('collapsed')).toBe(false);
  expect(group.querySelector('button')?.getAttribute('aria-expanded')).toBe('true');
  expect(group.querySelector('.tg-count')?.textContent).toBe('2 files · 1 failed');
  expect(order(group)).toEqual(['a', 'b']);
  expect(host.querySelector('.process-title')?.textContent).toContain('1 failed tool');
  expect(host.querySelector('.process-alerts')?.textContent).toBe('');
});

it.each(['edit', 'write', 'search', 'run', 'codemode'])('does not merge reads across an intervening %s', (kind) => {
  const host = document.createElement('div');
  const calls = [call('a', 'read'), call('middle', kind), call('b', 'read'), call('c', 'read')];
  calls.forEach((message) => append(host, message));
  [...calls].reverse().forEach((message) => complete(host, message));
  expect(order(host)).toEqual(['a', 'middle', 'b', 'c']);
  const groups = host.querySelectorAll('.tool-group');
  expect(groups).toHaveLength(1);
  expect(order(groups[0])).toEqual(['b', 'c']);
});

it('separates adjacent read groups by turn and run scope', () => {
  const host = document.createElement('div');
  [
    call('a', 'read'),
    call('b', 'read'),
    call('c', 'read', { turn: 2 }),
    call('d', 'read', { runId: 'other', toolKey: 'other:d', turn: 2 }),
  ].forEach((message) => append(host, message));
  expect(host.querySelectorAll('.tool-group')).toHaveLength(1);
  expect(order(host)).toEqual(['a', 'b', 'c', 'd']);
});

it('does not fold a later read backwards when reasoning updates an already-mounted Thinking disclosure', () => {
  const host = document.createElement('div');
  append(host, { type: 'thinking', runId: 'run', text: 'Inspect the input' });
  const a = call('a', 'read');
  append(host, a);
  append(host, { type: 'thinking', runId: 'run', text: 'Inspect the output' });
  complete(host, a);
  append(host, call('b', 'read'));
  expect(host.querySelectorAll('.thinking')).toHaveLength(1);
  expect(host.querySelectorAll('.tool-group')).toHaveLength(0);
  expect(order(host)).toEqual(['a', 'b']);
});

it('keeps CodeMode and its grouped children in place when parent and children settle', () => {
  const host = document.createElement('div');
  const parent = call('parent'),
    a = call('a', 'read', { parentCallId: 'parent' });
  const b = call('b', 'read', { parentCallId: 'parent' });
  const edit = call('edit', 'edit', { parentCallId: 'parent' });
  const c = call('c', 'read', { parentCallId: 'parent' });
  [parent, a, b, edit, c, call('later')].forEach((message) => append(host, message));
  const children = host.querySelector('.tool-children');
  const readGroup = children!.querySelector<HTMLElement>('.tool-group')!;
  readGroup.querySelector<HTMLButtonElement>('button')!.click();
  const initial = order(host);
  [b, a, edit, c].forEach((message) => complete(host, message));
  complete(host, parent);
  expect(order(host)).toEqual(initial);
  expect(host.querySelector('.tool-children')).toBe(children);
  expect(children!.querySelector('.tool-group')).toBe(readGroup);
  expect(readGroup.classList.contains('collapsed')).toBe(false);
  expect(children!.querySelectorAll('.tool-group')).toHaveLength(1);
  complete(host, a, { error: true });
  expect(order(host)).toEqual(initial);
  expect(children!.querySelector('[data-call-id="a"]')).not.toBeNull();
});

it('attaches orphaned sibling starts to a later parent without reversing them', () => {
  const host = document.createElement('div');
  const a = call('a', 'read', { parentCallId: 'parent' }),
    b = call('b', 'read', { parentCallId: 'parent' });
  [a, b, call('parent')].forEach((message) => append(host, message));
  complete(host, b);
  complete(host, a);
  const children = host.querySelector('.tool-children')!;
  expect(order(children)).toEqual(['a', 'b']);
  expect(children.querySelectorAll('.tool-group')).toHaveLength(1);
});

it('keeps command-card promotion in the original slot and preserves the child parent identity', () => {
  const host = document.createElement('div');
  const parent = call('parent'),
    shell = call('shell', 'shell', { parentCallId: 'parent' });
  [parent, shell, call('next-child', 'edit', { parentCallId: 'parent' }), call('later')].forEach((message) =>
    append(host, message),
  );
  append(host, {
    type: 'shell_result',
    id: 'approval',
    callId: 'shell',
    runId: 'run',
    approved: true,
    shellStatus: 'running',
    command: 'npm test',
    output: '',
  });
  expect(order(host)).toEqual(['parent', 'shell', 'next-child', 'later']);
  expect(host.querySelector('.tool-children .command-card')).not.toBeNull();
  append(host, {
    type: 'shell_result',
    id: 'approval',
    callId: 'shell',
    runId: 'run',
    approved: true,
    shellStatus: 'exit',
    exitCode: 1,
    command: 'npm test',
    output: '1 test failed',
  });
  complete(host, parent);
  expect(order(host)).toEqual(['parent', 'shell', 'next-child', 'later']);
  expect(host.querySelectorAll('.command-card')).toHaveLength(1);
  expect(host.querySelector('.process-title')?.textContent).toContain('1 failed tool');
});

it('keeps separate command slots when results have call IDs without approval IDs', () => {
  const host = document.createElement('div');
  const result = (callId: string, shellStatus: string) => ({
    type: 'shell_result',
    runId: 'run',
    callId,
    approved: true,
    shellStatus,
    command: 'npm test',
  });
  append(host, result('a', 'running'));
  append(host, result('b', 'running'));
  append(host, { ...result('a', 'exit'), exitCode: 0 });
  expect(order(host)).toEqual(['a', 'b']);
  expect(host.querySelectorAll('.command-card')).toHaveLength(2);
  expect(host.querySelector<HTMLElement>('[data-call-id="b"]')?.dataset.status).toBe('running');
});

it('preserves an opened result and scroll position when its record is refreshed', () => {
  const host = document.createElement('div');
  const message = call('edit', 'edit');
  append(host, message);
  complete(host, message);
  host.querySelector<HTMLDetailsElement>('.tool-disclosure')!.open = true;
  const output = host.querySelector<HTMLElement>('pre')!;
  output.scrollTop = 35;
  output.scrollLeft = 20;
  complete(host, message, { result: 'Updated result' });
  expect(host.querySelector<HTMLDetailsElement>('.tool-disclosure')!.open).toBe(true);
  expect(host.querySelector('pre')!.scrollTop).toBe(35);
  expect(host.querySelector('pre')!.scrollLeft).toBe(20);
});

it('counts explicit command failures in the summary without relocating them', () => {
  for (const shellStatus of ['stopped', 'timeout', 'spawn_error', 'wait_error', 'output_read_error', 'drain_timeout']) {
    const host = document.createElement('div');
    append(host, call('command', 'shell'));
    append(host, call('later'));
    append(host, { type: 'shell_result', runId: 'run', callId: 'command', approved: true, shellStatus });
    expect(order(host)).toEqual(['command', 'later']);
    expect(host.querySelector('.process-title')?.textContent).toContain('1 failed tool');
    expect(host.querySelector('.process-title')?.textContent).not.toContain('ran 1 command');
  }
});

it('restores the same order and explicit disclosures from retained starts and results', () => {
  const messages = [call('a', 'read'), call('b', 'read'), call('edit', 'edit'), call('c', 'read')];
  const final = [...messages]
    .reverse()
    .map((message) => ({ ...message, type: 'tool', pending: false, result: 'Done' }));
  const first = document.createElement('div');
  [...messages, ...final].forEach((message) => append(first, message));
  first.querySelector<HTMLButtonElement>('.tool-group-summary')!.click();
  first.querySelector<HTMLDetailsElement>('.tool-disclosure')!.open = true;
  first.querySelector('pre')!.scrollTop = 35;
  const saved = window.Process.capture(first);
  const restored = document.createElement('div');
  [...messages, ...final].forEach((message) => append(restored, message));
  window.Process.restore(restored, saved);
  expect(order(restored)).toEqual(order(first));
  expect(restored.querySelector('.tool-group')!.classList.contains('collapsed')).toBe(false);
  expect(restored.querySelector<HTMLDetailsElement>('.tool-disclosure')!.open).toBe(true);
  expect(restored.querySelector('pre')!.scrollTop).toBe(35);
});
