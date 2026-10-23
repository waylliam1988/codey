interface CommandResult {
  type: string;
  id?: string;
  shellStatus?: string;
  exitCode?: number | null;
  executionId?: string;
}

// Presentation of one approval/execution must move forward even when the
// reconnect snapshot and live start notification arrive in reverse order.
function progress(message: CommandResult): number {
  const status = message.shellStatus;
  if (!status || status === 'queued') return 0;
  if (status === 'running') return 1;
  if (['settled', 'uncertain', 'not_started'].includes(status)) return 2;
  return 3;
}
export function mergeCommandResult<T extends CommandResult>(previous: T | undefined, incoming: T): T {
  if (!previous || previous.type !== 'shell_result' || incoming.type !== 'shell_result' || previous.id !== incoming.id)
    return { ...previous, ...incoming };
  if (previous.executionId && incoming.executionId && previous.executionId !== incoming.executionId) return previous;
  if (incoming.shellStatus && progress(incoming) < progress(previous)) return previous;
  if (
    progress(previous) === 3 &&
    incoming.shellStatus &&
    (previous.shellStatus !== incoming.shellStatus ||
      (Number.isInteger(previous.exitCode) &&
        Number.isInteger(incoming.exitCode) &&
        previous.exitCode !== incoming.exitCode))
  )
    return previous;
  return { ...previous, ...incoming };
}
