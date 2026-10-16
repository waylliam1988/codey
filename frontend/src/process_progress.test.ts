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
