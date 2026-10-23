import type { Session } from './types/domain';

export function isRunSuspension(reason: unknown): boolean {
  return reason === 'approval' || reason === 'question' || reason === 'waiting';
}

export function isTerminalRun(session: Session | undefined, runId: string): boolean {
  return !!session?.terminalRuns?.includes(runId);
}

/** Presentation deduplication only; never used to decide terminal ownership. */
export function lastRunState(session: Session | undefined, runId: string): string | undefined {
  return session?.messages.filter((message) => message.type === 'run_state' && message.runId === runId).at(-1)?.state;
}

export interface LifecycleInput {
  runId: string;
  sessionId: string;
  state: string;
  active: boolean;
  revision?: number;
  sequence?: number;
  source: 'event' | 'snapshot' | 'admission';
}

export interface LifecycleView extends LifecycleInput {
  terminal: boolean;
  restored: boolean;
  snapshotBarrier: boolean;
}

/** Disposable presentation watermarks, never a task runtime or durable authority. */
export class RunLifecycleProjection {
  private rows = new Map<string, LifecycleView>();

  current(runId: string): LifecycleView | undefined {
    return this.rows.get(runId);
  }

  restoreTerminal(sessionId: string, runId: string): void {
    if (!this.rows.has(runId))
      this.rows.set(runId, {
        runId,
        sessionId,
        state: 'done',
        active: false,
        terminal: true,
        restored: true,
        snapshotBarrier: false,
        source: 'snapshot',
      });
  }

  transition(input: LifecycleInput): LifecycleView | null {
    if (!input.runId || !input.sessionId) return null;
    const old = this.rows.get(input.runId);
    if (old && old.sessionId !== input.sessionId) return null;
    const terminal = !input.active && !isRunSuspension(input.state) && input.state !== 'idle';
    const migration = old?.restored && input.source === 'snapshot';
    // Actual terminal states are irreversible. Only the snapshot restore
    // boundary may repair a legacy cache that incorrectly settled a park.
    if (old?.terminal && !migration && (!terminal || old.state !== input.state)) return null;
    if (old && !migration) {
      if (old.revision !== undefined) {
        if (input.revision === undefined || input.revision < old.revision) return null;
        if (input.revision === old.revision && input.state !== old.state) return null;
      }
      const sameRevision = input.revision === old.revision;
      if (
        sameRevision &&
        input.source === 'event' &&
        input.sequence !== undefined &&
        old.sequence !== undefined &&
        input.sequence <= old.sequence
      )
        return null;
      // Older servers lack task versions: active snapshots still fence old
      // parks buffered during reconciliation, including cursor-less parks.
      if (
        old.snapshotBarrier &&
        input.source !== 'snapshot' &&
        input.revision === undefined &&
        !terminal &&
        (input.sequence === undefined || input.sequence <= (old.sequence ?? 0)) &&
        input.state !== old.state
      )
        return null;
      if (input.source === 'admission' && old.source !== 'admission') return null;
    }
    const view: LifecycleView = {
      ...input,
      terminal,
      restored: false,
      revision: input.revision ?? old?.revision,
      sequence: input.sequence ?? old?.sequence,
      snapshotBarrier: (input.source === 'snapshot' && input.active) || !!old?.snapshotBarrier,
    };
    this.rows.set(input.runId, view);
    return view;
  }
}

export function lifecycleState(status: string | undefined, reason: string | undefined, fallback: string): string {
  if (status === 'cancelled') return 'stopped';
  if (status === 'failed') return reason && !isRunSuspension(reason) && reason !== 'done' ? reason : 'error';
  if (status === 'completed') return reason === 'max_turns' ? reason : 'done';
  if (status === 'waiting') return isRunSuspension(reason) ? reason! : 'waiting';
  if (status === 'running' || status === 'pending' || status === 'completing') return 'running';
  return fallback;
}

export function eventSequence(value: unknown): number | undefined {
  const sequence = Number(String(value ?? '').split(':')[0]);
  return Number.isSafeInteger(sequence) && sequence > 0 ? sequence : undefined;
}
