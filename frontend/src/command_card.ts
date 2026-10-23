import { iconButton } from './icon_button';
import type { Message } from './types/domain';
import { mergeOutputSnapshot, type ExecutionSnapshot } from './execution_output';
import { permissionsButton } from './command_permissions';
import { runtime } from './runtime';

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
  if (message.id) card.dataset.approvalId = message.id;
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.className = 'activity-row command-summary';
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
  const running = status === 'running';
  const queued = status === 'queued';
  const failure = Object.hasOwn(failures, status) ? failures[status] : undefined;
  const completed = message.approved === true && message.shellStatus === 'exit' && Number.isInteger(exit);
  const success = completed && exit === 0;
  card.dataset.status = running
    ? 'running'
    : queued
      ? 'queued'
      : success
        ? 'success'
        : failure || completed
          ? 'failed'
          : 'unknown';
  const result = queued
    ? 'Allowed; command queued'
    : running
      ? 'Command running'
      : failure ||
        (!message.approved
          ? 'Denied'
          : completed
            ? `Command ${success ? 'completed' : 'failed'} · exit ${exit}`
            : 'Result status unknown');
  const dot = document.createElement('span');
  dot.className = 'activity-dot';
  dot.textContent = '·';
  dot.setAttribute('aria-hidden', 'true');
  summary.append(dot);
  const label = document.createElement('span');
  label.className = 'command-label';
  label.textContent = running ? 'Running' : completed ? 'Ran' : result;
  summary.append(label);
  const excerpt = document.createElement('span');
  excerpt.className = 'command-excerpt';
  excerpt.textContent = message.command || '';
  summary.append(excerpt);
  const arrow = document.createElement('span');
  arrow.className = 'command-arrow';
  arrow.textContent = '→';
  arrow.setAttribute('aria-hidden', 'true');
  const resultSlot = document.createElement('span');
  resultSlot.className = 'command-result-slot';
  resultSlot.setAttribute('aria-hidden', 'true');
  summary.append(arrow, resultSlot);
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
  command.append(copyButton('Copy command', message.command || '', copy), pre);
  const output = document.createElement('section');
  output.className = 'command-result';
  const stdoutText = document.createElement('pre');
  stdoutText.className = 'shell-output shell-stdout';
  stdoutText.tabIndex = 0;
  stdoutText.setAttribute('role', 'region');
  stdoutText.setAttribute('aria-label', 'Standard output');
  stdoutText.textContent = message.output || (running ? 'Waiting for output…' : '(no output)');
  const stderrText = document.createElement('pre');
  stderrText.className = 'shell-output shell-stderr';
  stderrText.tabIndex = 0;
  stderrText.setAttribute('role', 'region');
  stderrText.setAttribute('aria-label', 'Standard error');
  stderrText.hidden = true;
  output.append(
    copyButton(running ? 'Copy received content' : 'Copy output', message.output || '', copy),
    stdoutText,
    stderrText,
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
  const tail = document.createElement('button');
  tail.className = 'text-btn';
  tail.textContent = 'Show last 40 lines';
  let showingTail = false;
  tail.onclick = () => {
    showingTail = !showingTail;
    const text = message.output || '';
    stdoutText.textContent = showingTail
      ? text
          .replace(/\r?\n$/, '')
          .split(/\r?\n/)
          .slice(-40)
          .join('\n')
      : text;
    tail.textContent = showingTail ? 'Show retained output' : 'Show last 40 lines';
  };
  if (!running && message.output) panel.append(tail);
  if (message.id && !message.permissionScope && !message.permissionSaveError)
    panel.append(permissionsButton(message.sessionId || '', message.project || ''));
  details.append(panel);
  card.append(details);
  if (message.permissionSaveError || message.permissionScope) {
    const notice = document.createElement('div');
    notice.className = 'command-permission-note';
    notice.setAttribute('role', 'status');
    notice.textContent = message.permissionSaveError
      ? `Allowed once. ${message.permissionSaveError}`
      : `Exact command allowed in this ${message.permissionScope === 'project' ? 'project' : 'chat'}.`;
    if (message.permissionSaveError && message.permissionSaveScope) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'link-btn';
      retry.textContent = 'Retry saving';
      retry.onclick = () => {
        retry.disabled = true;
        void runtime.ShellApprovals.remember(message.id || '', message.permissionSaveScope!);
      };
      notice.append(' ', retry);
    }
    notice.append(' ', permissionsButton(message.sessionId || '', message.project || ''));
    card.append(notice);
  }
  if (running && message.executionId) {
    const outputCopy = output.querySelector('button') as HTMLButtonElement | null;
    let latest: ExecutionSnapshot = { revision: -1, execution_state: 'running', stdout: '', stderr: '' };
    const updateOutput = (stdout: string, stderr: string) => {
      const following = textAtBottom(stdoutText);
      const stdoutTop = stdoutText.scrollTop;
      const stderrTop = stderrText.scrollTop;
      stdoutText.textContent =
        [stdout, stderr ? `[stderr]\n${stderr}` : ''].filter(Boolean).join('\n\n') || 'Waiting for output…';
      stderrText.textContent = stderr;
      stderrText.hidden = !stderr;
      stdoutText.scrollTop = following ? stdoutText.scrollHeight : stdoutTop;
      stderrText.scrollTop = stderrTop;
      if (outputCopy) {
        outputCopy.title = 'Copy received content';
        outputCopy.setAttribute('aria-label', 'Copy received content');
        outputCopy.onclick = async () => {
          const value = [latest.stdout, latest.stderr ? `[stderr]\n${latest.stderr}` : ''].filter(Boolean).join('\n\n');
          const ok = await copy(value);
          outputCopy.title = ok ? 'Copied' : 'Could not copy';
        };
      }
    };
    const poll = async (): Promise<void> => {
      while (card.isConnected || latest.revision < 0) {
        if (!card.isConnected && latest.revision >= 0) break;
        try {
          const scope = new URLSearchParams({ after_revision: String(latest.revision) });
          if (message.sessionId) scope.set('session_id', message.sessionId);
          if (message.runId) scope.set('run_id', message.runId);
          const response = await fetch(
            `/api/executions/${encodeURIComponent(message.executionId!)}/output?${scope.toString()}`,
          );
          if (response.status !== 304 && response.ok) {
            const data = (await response.json()) as {
              revision?: number;
              execution_state?: 'running' | 'settled' | 'unknown';
              stdout?: unknown;
              stderr?: unknown;
            };
            const textValue = (value: unknown) =>
              typeof value === 'string'
                ? value
                : value && typeof value === 'object' && 'text' in value
                  ? String((value as { text?: unknown }).text || '')
                  : '';
            const stdout = textValue(data.stdout);
            const stderr = textValue(data.stderr);
            latest = mergeOutputSnapshot(latest, {
              revision: Number(data.revision || 0),
              execution_state: data.execution_state || 'running',
              stdout,
              stderr,
            });
            updateOutput(latest.stdout, latest.stderr || '');
            if (latest.execution_state === 'settled' || latest.execution_state === 'unknown') break;
          }
        } catch {}
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    };
    void poll();
  }
  return card;
}

function textAtBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= 8;
}
