export type FrameRequest = (callback: () => void) => number;
export type FrameCancel = (handle: number) => void;

/** Coalesces high-frequency updates until the next paint opportunity. */
export class FrameCoalescer<T> {
  private readonly pending = new Map<string, T>();
  private frame: number | null = null;

  constructor(
    private readonly consume: (key: string, value: T) => void,
    private readonly request: FrameRequest = (callback) =>
      typeof requestAnimationFrame === 'function' ? requestAnimationFrame(callback) : window.setTimeout(callback, 0),
    private readonly cancel: FrameCancel = (handle) => {
      if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(handle);
      else window.clearTimeout(handle);
    },
  ) {}

  enqueue(key: string, value: T): void {
    this.pending.set(key, value);
    if (this.frame !== null) return;
    this.frame = this.request(() => {
      this.frame = null;
      this.drain();
    });
  }

  flush(): void {
    if (this.frame !== null) {
      this.cancel(this.frame);
      this.frame = null;
    }
    this.drain();
  }

  private drain(): void {
    if (!this.pending.size) return;
    const updates = [...this.pending.entries()];
    this.pending.clear();
    for (const [key, value] of updates) this.consume(key, value);
  }
}
