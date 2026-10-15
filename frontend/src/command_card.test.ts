import { expect, it, vi } from 'vitest';
import { commandCard } from './command_card';

it('uses a text disclosure with only the shared trailing chevron', () => {
  const card = commandCard(
    { type: 'shell_result', command: 'python probe.py', approved: true, shellStatus: 'exit', exitCode: 0 },
    vi.fn(),
  );
  const summary = card.querySelector('summary')!;
  expect(summary.querySelector('.command-terminal, svg, img')).toBeNull();
  expect(summary.firstElementChild?.textContent).toBe('Ran');
  expect(summary.lastElementChild?.className).toBe('command-chevron');
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

it('keeps command/output independently copyable and labels retained output honestly', async () => {
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
  expect(buttons[1].getAttribute('aria-label')).toBe('Copy retained output');
  expect(card.querySelector('.command-notice')?.textContent).toBe('Output truncated');
  expect(buttons[0].textContent).toBe('');
  expect(buttons[0].querySelector('svg')).not.toBeNull();
  card.remove();
});
