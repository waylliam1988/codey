import { iconButton } from './icon_button';
import type { Message } from './types/domain';

type Copy = (text: string) => Promise<boolean>;

function copyButton(label: string, text: string, copy: Copy): HTMLButtonElement {
  const button = iconButton(label, 'copy', async () => {
    const status = (await copy(text)) ? 'Copied' : 'Could not copy';
    button.title = status;
    button.setAttribute('aria-label', status);
  });
  return button;
}

export function commandCard(message: Message, copy: Copy): HTMLElement {
  const card = document.createElement('div');
  card.className = 'msg shell command-card';
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  const exit = message.exitCode;
  const failures: Record<string, string> = {
    stopped: 'Stopped',
    timeout: 'Timed out',
    spawn_error: 'Failed to start',
    wait_error: 'Failed while waiting',
    output_read_error: 'Failed reading output',
    drain_timeout: 'Output did not finish',
  };
  const status = message.shellStatus || '';
  const failure = Object.hasOwn(failures, status) ? failures[status] : undefined;
  const completed = message.approved === true && message.shellStatus === 'exit' && Number.isInteger(exit);
  const success = completed && exit === 0;
  card.dataset.status = success ? 'success' : failure || completed ? 'failed' : 'unknown';
  const result =
    failure ||
    (!message.approved
      ? 'Denied'
      : completed
        ? `Command ${success ? 'completed' : 'failed'} · exit ${exit}`
        : 'Result status unknown');
  const label = document.createElement('span');
  label.className = 'command-label';
  label.textContent = completed ? 'Ran' : result;
  summary.append(label);
  const excerpt = document.createElement('span');
  excerpt.className = 'command-excerpt';
  excerpt.textContent = message.command || '';
  summary.append(excerpt);
  const chevron = document.createElement('span');
  chevron.className = 'command-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  summary.append(chevron);
  details.append(summary);
  const panel = document.createElement('div');
  panel.className = 'command-panel';
  const panelLabel = document.createElement('div');
  panelLabel.className = 'command-panel-label';
  panelLabel.textContent = 'Shell';
  panel.append(panelLabel);
  const command = document.createElement('section');
  command.className = 'command-source';
  const pre = document.createElement('pre');
  pre.textContent = message.command || '';
  command.append(
    copyButton(message.commandTruncated ? 'Copy retained command' : 'Copy command', message.command || '', copy),
    pre,
  );
  const output = document.createElement('section');
  output.className = 'command-result';
  const text = document.createElement('pre');
  text.className = 'shell-output';
  text.tabIndex = 0;
  text.setAttribute('role', 'region');
  text.setAttribute('aria-label', 'Command output');
  text.textContent = message.output || '(no output)';
  output.append(
    copyButton(message.outputTruncated ? 'Copy retained output' : 'Copy output', message.output || '', copy),
    text,
  );
  if (message.outputTruncated || message.commandTruncated) {
    const notice = document.createElement('div');
    notice.className = 'command-notice';
    notice.textContent = message.outputTruncated ? 'Output truncated' : 'Command truncated';
    notice.title = 'Copy includes retained content only.';
    output.append(notice);
  }
  const settlement = document.createElement('div');
  settlement.className = 'command-status';
  settlement.textContent = success ? '✓ Succeeded' : result;
  panel.append(command, output, settlement);
  details.append(panel);
  card.append(details);
  return card;
}
