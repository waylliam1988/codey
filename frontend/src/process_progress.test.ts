import { expect, it } from 'vitest';
import './render';
import { Process } from './process';

it('does not mark the task complete when an incremental answer first appears', () => {
  const chat = document.createElement('div');
  Process.append(chat, { type: 'request_activity', runId: 'r', state: 'running' }, () => {});
  Process.append(chat, { type: 'asst', runId: 'r', streamId: 's', streamState: 'partial', text: 'partial' }, () => {});
  expect(chat.querySelector('.process-title')?.textContent).toContain('Working');
  expect(chat.querySelector('.spinner')).not.toBeNull();
});

it('does not let an answer override an authoritative running checkpoint', () => {
  const chat = document.createElement('div');
  Process.append(chat, { type: 'run_state', runId: 'r', state: 'running' }, () => {});
  Process.append(
    chat,
    { type: 'asst', runId: 'r', finalAnswer: true, streamState: 'complete', text: 'Answer' },
    () => {},
  );
  expect(chat.querySelector('.process-title')?.textContent).toContain('Working');
  Process.append(chat, { type: 'run_state', runId: 'r', state: 'done' }, () => {});
  expect(chat.querySelector('.process-title')?.textContent).toContain('Worked');
});

it('uses settled run time and restores an individually opened tool result', () => {
  const chat = document.createElement('div');
  const tool = {
    type: 'tool' as const,
    kind: 'read',
    path: 'a.py',
    result: 'saved output',
    runId: 'r',
    toolKey: 'r:1:0',
  };
  Process.append(chat, tool, () => {});
  chat.querySelector<HTMLDetailsElement>('.tool-disclosure')!.open = true;
  const saved = Process.capture(chat);
  chat.replaceChildren();
  Process.append(chat, tool, () => {});
  Process.restore(chat, saved);
  expect(chat.querySelector<HTMLDetailsElement>('.tool-disclosure')!.open).toBe(true);
  Process.append(chat, { type: 'run_state', runId: 'r', state: 'done', elapsedMs: 42750 }, () => {});
  expect(chat.querySelector('.process-title')?.textContent).toContain('Worked for 43s');
});

it('folds out-of-order read completions into the same read group', () => {
  const chat = document.createElement('div');
  const pending = (toolKey: string, path: string) =>
    ({ type: 'tool_pending', kind: 'read', path, pending: true, runId: 'r', toolKey }) as const;
  Process.append(chat, pending('r:1:0', 'README.md'), () => {});
  Process.append(chat, pending('r:1:1', 'selfie'), () => {});
  Process.append(
    chat,
    { type: 'tool' as const, kind: 'read', path: 'README.md', result: 'one', runId: 'r', toolKey: 'r:1:0' },
    () => {},
  );
  Process.append(
    chat,
    { type: 'tool' as const, kind: 'read', path: 'selfie', result: 'two', runId: 'r', toolKey: 'r:1:1' },
    () => {},
  );
  expect(chat.querySelectorAll('.tool-group')).toHaveLength(1);
  expect(chat.querySelector('.tg-count')?.textContent).toBe('2 files');
  expect(chat.querySelector('.tool-group')?.textContent).toContain('selfie');
});

it('attaches a child arriving before its parent, retains it through settlement and ignores a late start', () => {
  const chat = document.createElement('div');
  const child = {
    type: 'tool' as const,
    kind: 'read',
    path: 'child.py',
    result: 'saved',
    runId: 'r',
    toolKey: 'r:child',
    callId: 'child',
    parentCallId: 'parent',
  };
  Process.append(chat, child, () => {});
  Process.append(
    chat,
    {
      type: 'tool_pending',
      kind: 'codemode',
      toolName: 'codemode',
      pending: true,
      runId: 'r',
      toolKey: 'r:parent',
      callId: 'parent',
    },
    () => {},
  );
  expect(chat.querySelector('[data-call-id="parent"] .tool-children [data-call-id="child"]')).not.toBeNull();
  Process.append(
    chat,
    {
      type: 'tool' as const,
      kind: 'codemode',
      toolName: 'codemode',
      result: 'finished',
      runId: 'r',
      toolKey: 'r:parent',
      callId: 'parent',
    },
    () => {},
  );
  expect(chat.querySelector('[data-call-id="parent"] .tool-children [data-call-id="child"]')).not.toBeNull();
  Process.append(chat, { ...child, type: 'tool_pending', pending: true, result: '' }, () => {});
  expect(chat.querySelector('[data-call-id="child"]')?.textContent).toContain('saved');
  expect(chat.querySelectorAll('[data-call-id="child"]')).toHaveLength(1);
});

it('does not nest cycles and keeps failures in their original stream with a visible failure count', () => {
  const chat = document.createElement('div');
  const make = (id: string, parent: string, error = false) => ({
    type: 'tool' as const,
    kind: 'read',
    result: id,
    runId: 'r',
    toolKey: `r:${id}`,
    callId: id,
    parentCallId: parent,
    error,
  });
  Process.append(chat, make('a', 'b'), () => {});
  Process.append(chat, make('b', 'a'), () => {});
  expect(chat.querySelector('.tool-children')).toBeNull();
  Process.append(chat, make('error', 'a', true), () => {});
  expect(chat.querySelector('.process-body [data-call-id="error"]')).not.toBeNull();
  expect(chat.querySelector('.process-alerts [data-call-id="error"]')).toBeNull();
  expect(chat.querySelector('.process-title')?.textContent).toContain('1 failed tool');
});
