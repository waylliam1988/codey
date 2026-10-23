import { expect, it } from 'vitest';
import './render';
import './process';

it('does not mark the task complete when an incremental answer first appears', () => {
  const chat = document.createElement('div');
  window.Process.append(chat, { type: 'request_activity', runId: 'r', state: 'running' }, () => {});
  window.Process.append(
    chat,
    { type: 'asst', runId: 'r', streamId: 's', streamState: 'partial', text: 'partial' },
    () => {},
  );
  expect(chat.querySelector('.process-title')?.textContent).toContain('Working');
  expect(chat.querySelector('.spinner')).not.toBeNull();
});

it('does not let an answer override an authoritative running checkpoint', () => {
  const chat = document.createElement('div');
  window.Process.append(chat, { type: 'run_state', runId: 'r', state: 'running' }, () => {});
  window.Process.append(
    chat,
    { type: 'asst', runId: 'r', finalAnswer: true, streamState: 'complete', text: 'Answer' },
    () => {},
  );
  expect(chat.querySelector('.process-title')?.textContent).toContain('Working');
  window.Process.append(chat, { type: 'run_state', runId: 'r', state: 'done' }, () => {});
  expect(chat.querySelector('.process-title')?.textContent).toContain('Worked');
});

it('uses settled run time and restores an individually opened tool result', () => {
  const chat = document.createElement('div');
  const tool = { type: 'tool', kind: 'read', path: 'a.py', result: 'saved output', runId: 'r', toolKey: 'r:1:0' };
  window.Process.append(chat, tool, () => {});
  chat.querySelector<HTMLDetailsElement>('.tool-disclosure')!.open = true;
  const saved = window.Process.capture(chat);
  chat.replaceChildren();
  window.Process.append(chat, tool, () => {});
  window.Process.restore(chat, saved);
  expect(chat.querySelector<HTMLDetailsElement>('.tool-disclosure')!.open).toBe(true);
  window.Process.append(chat, { type: 'run_state', runId: 'r', state: 'done', elapsedMs: 42750 }, () => {});
  expect(chat.querySelector('.process-title')?.textContent).toContain('Worked for 43s');
});

it('folds out-of-order read completions into the same read group', () => {
  const chat = document.createElement('div');
  const pending = (toolKey: string, path: string) =>
    ({ type: 'tool_pending', kind: 'read', path, pending: true, runId: 'r', toolKey }) as const;
  window.Process.append(chat, pending('r:1:0', 'README.md'), () => {});
  window.Process.append(chat, pending('r:1:1', 'selfie'), () => {});
  window.Process.append(
    chat,
    { type: 'tool', kind: 'read', path: 'README.md', result: 'one', runId: 'r', toolKey: 'r:1:0' },
    () => {},
  );
  window.Process.append(
    chat,
    { type: 'tool', kind: 'read', path: 'selfie', result: 'two', runId: 'r', toolKey: 'r:1:1' },
    () => {},
  );
  expect(chat.querySelectorAll('.tool-group')).toHaveLength(1);
  expect(chat.querySelector('.tg-count')?.textContent).toBe('2 files');
  expect(chat.querySelector('.tool-group')?.textContent).toContain('selfie');
});

it('attaches a child arriving before its parent, retains it through settlement and ignores a late start', () => {
  const chat = document.createElement('div');
  const child = {
    type: 'tool',
    kind: 'read',
    path: 'child.py',
    result: 'saved',
    runId: 'r',
    toolKey: 'r:child',
    callId: 'child',
    parentCallId: 'parent',
  };
  window.Process.append(chat, child, () => {});
  window.Process.append(
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
  window.Process.append(
    chat,
    {
      type: 'tool',
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
  window.Process.append(chat, { ...child, type: 'tool_pending', pending: true, result: '' }, () => {});
  expect(chat.querySelector('[data-call-id="child"]')?.textContent).toContain('saved');
  expect(chat.querySelectorAll('[data-call-id="child"]')).toHaveLength(1);
});

it('does not nest cycles or hide child failures inside the collapsed parent', () => {
  const chat = document.createElement('div');
  const make = (id: string, parent: string, error = false) => ({
    type: 'tool',
    kind: 'read',
    result: id,
    runId: 'r',
    toolKey: `r:${id}`,
    callId: id,
    parentCallId: parent,
    error,
  });
  window.Process.append(chat, make('a', 'b'), () => {});
  window.Process.append(chat, make('b', 'a'), () => {});
  expect(chat.querySelector('.tool-children')).toBeNull();
  window.Process.append(chat, make('error', 'a', true), () => {});
  expect(chat.querySelector('.process-alerts [data-call-id="error"]')).not.toBeNull();
});
