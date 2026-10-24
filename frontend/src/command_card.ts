import { iconButton } from './icon_button';
import type { Message } from './types/domain';
import { mergeOutputSnapshot, type ExecutionSnapshot } from './execution_output';
import { commandOutputView } from './command_output_view';
import { runtime } from './runtime';

type Copy = (text: string) => Promise<boolean>;

function copyButton(label: string, text: string | (() => string), copy: Copy): HTMLButtonElement {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const button = iconButton(label, 'copy', async () => {
    if (timer) clearTimeout(timer);
    button.disabled = true;
    let ok = false;
    try {
      ok = await copy(typeof text === 'function' ? text() : text);
    } catch {
      /* Keep copying retryable. */
    }
    const status = ok ? 'Copied' : 'Could not copy';
    button.disabled = false;
    button.dataset.copyFeedback = 'true';
    button.setAttribute('aria-label', status);
    timer = setTimeout(
      () => {
        delete button.dataset.copyFeedback;
        button.setAttribute('aria-label', button.dataset.copyLabel || label);
      },
      ok ? 1200 : 4000,
    );
  });
  button.removeAttribute('title');
  button.dataset.copyLabel = label;
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
    ? 'Queued'
    : running
      ? 'Command running'
      : failure ||
        (!message.approved
          ? 'Denied'
          : completed
            ? success
              ? '✓ Success'
              : `Exit code ${exit}`
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
  const chevron = document.createElement('span');
  chevron.className = 'command-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  summary.append(chevron);
  details.append(summary);
  const panel = document.createElement('div');
  panel.className = 'command-panel';
  const command = document.createElement('section');
  command.className = 'command-source';
  const pre = document.createElement('pre');
  pre.textContent = (message.commandTruncated ? '[command truncated]\n' : '') + (message.command || '');
  command.append(
    copyButton(message.commandTruncated ? 'Copy retained command' : 'Copy command', message.command || '', copy),
    pre,
  );
  if (message.commandTruncated && message.commandSha256) {
    const integrity = document.createElement('div');
    integrity.className = 'command-integrity';
    integrity.textContent = `Full command sha256 · ${message.commandSha256}`;
    command.append(integrity);
  }
  const output = document.createElement('section');
  output.className = 'command-result';
  const stdoutText = document.createElement('pre');
  stdoutText.className = 'shell-output shell-stdout';
  stdoutText.tabIndex = 0;
  stdoutText.setAttribute('role', 'region');
  stdoutText.setAttribute('aria-label', 'Standard output');
  let retainedOutput = message.output || '';
  const outputCopy = copyButton(
    message.outputTruncated ? 'Copy retained output' : running ? 'Copy received content' : 'Copy output',
    () => retainedOutput,
    copy,
  );
  output.append(outputCopy, stdoutText);
  const reader = commandOutputView(stdoutText, output, details);
  reader.write(
    (message.outputTruncated ? '[output truncated]\n' : '') +
      (retainedOutput || (running ? 'Waiting for output…' : 'No output')),
  );
  const settlement = document.createElement('div');
  settlement.className = 'command-status';
  settlement.textContent = running ? '' : result;
  panel.append(command, output, settlement);
  details.append(panel);
  card.append(details);
  if (message.permissionSaveError) {
    const notice = document.createElement('div');
    notice.className = 'command-permission-note';
    notice.setAttribute('role', 'status');
    notice.textContent = 'Permission not saved.';
    notice.title = message.permissionSaveError;
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
    card.append(notice);
  }
  if (running && message.executionId) {
    let latest: ExecutionSnapshot = { revision: -1, execution_state: 'running', stdout: '', stderr: '' };
    const updateOutput = (stdout: string, stderr: string, truncated: boolean) => {
      retainedOutput = [stdout, stderr ? `[stderr]\n${stderr}` : ''].filter(Boolean).join('\n\n');
      reader.write(
        (truncated ? '[output truncated]\n' : '') +
          (retainedOutput || (latest.execution_state === 'running' ? 'Waiting for output…' : 'No output')),
      );
      outputCopy.dataset.copyLabel = truncated
        ? 'Copy retained output'
        : latest.execution_state === 'running'
          ? 'Copy received content'
          : 'Copy output';
      if (!outputCopy.dataset.copyFeedback) outputCopy.setAttribute('aria-label', outputCopy.dataset.copyLabel);
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
              exit_code?: number | null;
            };
            const textValue = (value: unknown) =>
              typeof value === 'string'
                ? value
                : value && typeof value === 'object' && 'text' in value
                  ? String((value as { text?: unknown }).text || '')
                  : '';
            const stdout = textValue(data.stdout);
            const stderr = textValue(data.stderr);
            const next = mergeOutputSnapshot(latest, {
              revision: Number(data.revision || 0),
              execution_state: data.execution_state || 'running',
              stdout,
              stderr,
            });
            if (next !== latest) {
              latest = next;
              const truncated = [data.stdout, data.stderr].some(
                (value) => value && typeof value === 'object' && 'truncated' in value && value.truncated === true,
              );
              updateOutput(latest.stdout, latest.stderr || '', truncated);
              if (latest.execution_state !== 'running') {
                const exit = data.exit_code;
                const known = latest.execution_state === 'settled' && Number.isInteger(exit);
                card.dataset.status = known ? (exit === 0 ? 'success' : 'failed') : 'unknown';
                label.textContent = 'Ran';
                settlement.textContent = known
                  ? exit === 0
                    ? '✓ Success'
                    : `Exit code ${exit}`
                  : 'Result status unknown';
              }
            }
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
