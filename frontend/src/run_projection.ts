import { type LifecycleInput, type LifecycleView, RunLifecycleProjection } from './run_lifecycle';

export interface PrimaryRun {
  runId: string;
  sessionId: string;
}

export interface RunProjectionDeps {
  requestIdForRun(sessionId: string, runId: string): string | undefined;
}

export class RunProjection {
  private readonly lifecycle = new RunLifecycleProjection();
  private readonly activeRuns = new Map<string, string>();
  private readonly turnIds = new Map<string, string>();

  constructor(private readonly deps: RunProjectionDeps) {}

  get primaryRun(): PrimaryRun | null {
    const item = [...this.activeRuns.entries()].at(-1);
    return item ? { runId: item[0], sessionId: item[1] } : null;
  }

  get size(): number {
    return this.activeRuns.size;
  }

  isActive(runId: string): boolean {
    return this.activeRuns.has(runId);
  }

  isSessionRunning(sessionId: string): boolean {
    return [...this.activeRuns.values()].includes(sessionId);
  }

  runIdForSession(sessionId: string): string | null {
    for (const [runId, owner] of this.activeRuns) {
      if (owner === sessionId) return runId;
    }
    return null;
  }

  entries(): Array<[string, string]> {
    return [...this.activeRuns.entries()];
  }

  restoreTerminal(sessionId: string, runId: string): void {
    this.lifecycle.restoreTerminal(sessionId, runId);
  }

  transition(input: LifecycleInput): LifecycleView | null {
    const view = this.lifecycle.transition(input);
    if (!view) return null;

    if (view.active) this.activeRuns.set(input.runId, input.sessionId);
    else this.activeRuns.delete(input.runId);

    if (view.active) this.ensureTurnId(input.sessionId, input.runId);
    return view;
  }

  forgetAdmission(runId: string): boolean {
    if (!this.lifecycle.forgetAdmission(runId)) return false;
    this.activeRuns.delete(runId);
    return true;
  }

  turnIdForRun(sessionId: string, runId: string): string {
    const existing = this.turnIds.get(runId);
    if (existing) return existing;

    const requestId = this.deps.requestIdForRun(sessionId, runId);
    if (!requestId) return '';

    const turnId = `turn:${requestId}`;
    this.turnIds.set(runId, turnId);
    return turnId;
  }

  private ensureTurnId(sessionId: string, runId: string): void {
    this.turnIdForRun(sessionId, runId);
  }
}
