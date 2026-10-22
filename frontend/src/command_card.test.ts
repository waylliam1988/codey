import { expect, it, vi } from 'vitest';
import { commandCard } from './command_card';
import './render';

it('tails retained output without changing status or copied output', () => {
  const text = Array.from({ length: 80 }, (_, i) => String(i)).join('\n') + '\n';
  const copy = vi.fn().mockResolvedValue(true);
  const card = commandCard(
    { type: 'shell_result', approved: true, shellStatus: 'exit', exitCode: 7, output: text },
    copy,
  );
  const tail = Array.from(card.querySelectorAll('button')).find(
    (button) => button.textContent === 'Show last 40 lines',
  )!;
  tail.click();
  expect(card.querySelector('.shell-stdout')?.textContent?.split('\n')).toHaveLength(40);
  expect(card.querySelector('.shell-stdout')?.textContent).toMatch(/^40\n/);
  expect(card.querySelector('.command-status')?.textContent).toContain('exit 7');
  (card.querySelector('[aria-label="Copy output"]') as HTMLButtonElement).click();
  expect(copy).toHaveBeenCalledWith(text);
  tail.click();
  expect(card.querySelector('.shell-stdout')?.textContent).toBe(text);
});

it('uses the shared activity-row disclosure with a trailing chevron', () => {
  const card = commandCard(
    { type: 'shell_result', command: 'python probe.py', approved: true, shellStatus: 'exit', exitCode: 0 },
    vi.fn(),
  );
  const summary = card.querySelector('summary')!;
  expect(summary.querySelector('.command-terminal, svg, img')).toBeNull();
  expect(summary.querySelector('.activity-dot')?.textContent).toBe('·');
  expect(summary.querySelector('.command-label')?.textContent).toBe('Ran');
  expect(summary.querySelector('.command-arrow')?.textContent).toBe('→');
  expect(summary.lastElementChild?.className).toBe('command-chevron');
});

it('uses the shared masked chevron for grouped Read and Search rows', () => {
  const read = window.Render.createToolGroup('read');
  const search = window.Render.createToolGroup('search');
  expect(read?.querySelector('.tg-chevron')?.textContent).toBe('');
  expect(search?.querySelector('.tg-chevron')?.textContent).toBe('');
  expect(read?.querySelector('.tg-chevron')?.getAttribute('aria-hidden')).toBe('true');
});

it('never labels a denial, timeout, unknown status or nonzero exit as successful', () => {
  const cases = [
    { approved: false, shellStatus: 'exit', exitCode: 0, label: 'Denied' },
    { approved: false, shellStatus: '', exitCode: null, label: 'Denied' },
    { approved: true, shellStatus: 'timeout', exitCode: null, label: 'Timed out' },
    { approved: true, shellStatus: 'future', exitCode: 0, label: 'Result status unknown' },
    { approved: true, shellStatus: 'constructor', exitCode: 0, label: 'Result status unknown' },
  ];
  for (const value of cases) {
    const card = commandCard({ type: 'shell_result', ...value }, vi.fn());
    expect(card.querySelector('summary')?.textContent).toContain(value.label);
    expect(card.dataset.status).not.toBe('success');
  }
});

it('keeps command/output independently copyable while exposing truncation separately', async () => {
  const copy = vi.fn().mockResolvedValue(true);
  const card = commandCard(
    {
      type: 'shell_result',
      command: 'x'.repeat(4000),
      output: '参数\n'.repeat(2000),
      exitCode: 1,
      approved: true,
      shellStatus: 'exit',
      outputTruncated: true,
    },
    copy,
  );
  document.body.append(card);
  expect(card.querySelector('.command-status')?.textContent).toContain('exit 1');
  expect(card.querySelector('details')?.open).toBe(false);
  expect(card.querySelector('.shell-output')?.textContent).toContain('参数');
  const buttons = card.querySelectorAll('button');
  buttons[0].click();
  expect(copy).toHaveBeenLastCalledWith('x'.repeat(4000));
  buttons[1].click();
  expect(copy).toHaveBeenLastCalledWith('参数\n'.repeat(2000));
  expect(buttons[1].getAttribute('aria-label')).toBe('Copy output');
  expect(card.querySelector('.command-notice')?.textContent).toBe('Output truncated');
  expect(buttons[0].textContent).toBe('');
  expect(buttons[0].querySelector('svg')).not.toBeNull();
  card.remove();
});

it('scopes live output polling to the owning session and run', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValue(
      new Response(
        JSON.stringify({ revision: 1, execution_state: 'settled', stdout: { text: 'ok' }, stderr: { text: '' } }),
        { status: 200 },
      ),
    );
  vi.stubGlobal('fetch', fetcher);
  const card = commandCard(
    {
      type: 'shell_result',
      command: 'x',
      approved: true,
      shellStatus: 'running',
      executionId: 'e1',
      sessionId: 's1',
      runId: 'run_1',
    },
    vi.fn(),
  );
  document.body.append(card);
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(String(fetcher.mock.calls[0]?.[0])).toContain('session_id=s1');
  expect(String(fetcher.mock.calls[0]?.[0])).toContain('run_id=run_1');
  card.remove();
  vi.unstubAllGlobals();
});

it('shows a running command card and observes bounded output before settlement', async () => {
  const responses = [
    new Response(
      JSON.stringify({ revision: 1, execution_state: 'running', stdout: { text: 'first line' }, stderr: { text: '' } }),
      { status: 200 },
    ),
    new Response(
      JSON.stringify({
        revision: 2,
        execution_state: 'settled',
        stdout: { text: 'first line\\nlast line' },
        stderr: { text: 'warn' },
      }),
      { status: 200 },
    ),
  ];
  const fetcher = vi.fn().mockImplementation(async () => responses.shift()!);
  vi.stubGlobal('fetch', fetcher);
  const card = commandCard(
    {
      type: 'shell_result',
      command: 'python live.py',
      approved: true,
      shellStatus: 'running',
      executionId: 'execution-1',
    },
    vi.fn(),
  );
  document.body.append(card);
  expect(card.dataset.status).toBe('running');
  expect(card.querySelector('summary')?.textContent).toContain('Running');
  await new Promise((resolve) => setTimeout(resolve, 650));
  expect(fetcher).toHaveBeenCalled();
  expect(card.querySelector('.shell-output')?.textContent).toContain('last line');
  expect(card.querySelector('.shell-output')?.textContent).toContain('[stderr]');
  vi.unstubAllGlobals();
  card.remove();
});
