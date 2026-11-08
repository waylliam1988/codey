import { type HistoryIdentity, type RunValidation } from './history_repository';

interface PendingEvent {
  runId: string;
  accept(): void;
}

/** Membership belongs to one path generation; unknown events never render optimistically. */
export class RunVisibility {
  private identity: HistoryIdentity | undefined;
  private token = 0;
  private confirmed = new Map<string, boolean>();
  private candidates = new Set<string>();
  private pending: PendingEvent[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  private scheduled = false;
  private loading = false;

  constructor(
    private validate: (runs: string[], identity: HistoryIdentity) => Promise<RunValidation>,
    private resync: () => void,
    private retired: (runs: string[]) => void,
  ) {}

  version(): string {
    return String(this.token);
  }

  accepts(runId: string): boolean {
    return !runId || this.confirmed.get(runId) === true;
  }

  update(identity: HistoryIdentity, visibleRuns: string[]): boolean {
    const changed =
      !!this.identity &&
      (this.identity.epoch !== identity.epoch || this.identity.pathGeneration !== identity.pathGeneration);
    if (changed) this.reset();
    if (!this.identity) this.confirmed.clear();
    if (this.identity && identity.revision < this.identity.revision) return changed;
    if (!this.identity || identity.revision > this.identity.revision)
      for (const [run, visible] of this.confirmed) if (!visible) this.confirmed.delete(run);
    this.identity = identity;
    for (const run of visibleRuns) this.confirmed.set(run, true);
    this.schedule();
    return changed;
  }

  observe(generation: number, revision: number): boolean {
    if (!this.identity) return false;
    if (generation < this.identity.pathGeneration) return false;
    if (generation !== this.identity.pathGeneration) {
      const epoch = this.identity.epoch;
      this.reset();
      this.identity = { epoch, pathGeneration: generation, revision };
      return true;
    }
    if (revision > this.identity.revision) this.update({ ...this.identity, revision }, []);
    return false;
  }

  check(runs: string[]): void {
    for (const run of runs) if (run && !this.confirmed.has(run)) this.candidates.add(run);
    this.schedule();
  }

  admit(runId: string, accept: () => void): void {
    if (!runId || this.confirmed.get(runId) === true) {
      accept();
      return;
    }
    if (this.confirmed.get(runId) === false) return;
    if (this.pending.length >= 512) {
      this.reset();
      this.resync();
      return;
    }
    this.pending.push({ runId, accept });
    this.candidates.add(runId);
    if (this.timer === undefined)
      this.timer = setTimeout(() => {
        this.reset();
        this.resync();
      }, 5000);
    if (!this.identity) this.resync();
    this.schedule();
  }

  reject(runs: string[]): void {
    for (const run of runs) this.confirmed.set(run, false);
    this.pending = this.pending.filter((event) => !runs.includes(event.runId));
  }

  reset(): void {
    this.token++;
    this.identity = undefined;
    this.confirmed.clear();
    this.candidates.clear();
    this.pending = [];
    this.loading = false;
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
  }

  private schedule(): void {
    if (this.scheduled || this.loading || !this.identity || this.candidates.size === 0) return;
    this.scheduled = true;
    queueMicrotask(() => {
      this.scheduled = false;
      void this.flush();
    });
  }

  private async flush(): Promise<void> {
    if (!this.identity || this.loading || !this.candidates.size) return;
    const token = this.token;
    const identity = this.identity;
    const runs = [...this.candidates].slice(0, 200);
    runs.forEach((run) => this.candidates.delete(run));
    this.loading = true;
    try {
      const result = await this.validate(runs, identity);
      if (token !== this.token) return;
      if (
        !this.identity ||
        result.epoch !== this.identity.epoch ||
        result.pathGeneration !== this.identity.pathGeneration ||
        result.revision < this.identity.revision
      ) {
        runs.forEach((run) => this.candidates.add(run));
        return;
      }
      for (const run of result.runs) this.confirmed.set(run.run_id, run.visible);
      this.retired(result.runs.filter((run) => !run.visible).map((run) => run.run_id));
      const pending = this.pending;
      this.pending = [];
      for (const event of pending) {
        if (token !== this.token) break;
        if (this.confirmed.get(event.runId) === true) event.accept();
        else if (!this.confirmed.has(event.runId)) this.pending.push(event);
      }
      if (!this.pending.length && this.timer !== undefined) {
        clearTimeout(this.timer);
        this.timer = undefined;
      }
    } catch {
      if (token === this.token) {
        this.reset();
        this.resync();
      }
    } finally {
      if (token === this.token) {
        this.loading = false;
        this.schedule();
      }
    }
  }
}
