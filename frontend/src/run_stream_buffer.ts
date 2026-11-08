import { FrameCoalescer } from './frame_coalescer';

export interface RunStreamEvent {
  session_id?: string;
  run_id?: string;
  stream_id?: string;
}

export interface RunStreamBufferDeps<T extends RunStreamEvent> {
  acceptsRun(sessionId: string, runId: string): boolean;
  version(sessionId: string): string;
  handle(data: T): void;
}

export class RunStreamBuffer<T extends RunStreamEvent> {
  private readonly frames: FrameCoalescer<{ event: T; version: string }>;

  constructor(private readonly deps: RunStreamBufferDeps<T>) {
    this.frames = new FrameCoalescer((_key, item) => {
      const data = item.event;
      const sessionId = data.session_id || '';
      const runId = data.run_id || '';
      if (item.version === this.deps.version(sessionId) && this.deps.acceptsRun(sessionId, runId))
        this.deps.handle(data);
    });
  }

  enqueue(data: T): void {
    const key = `${data.session_id || ''}:${data.run_id || ''}:${data.stream_id || ''}`;
    this.frames.enqueue(key, { event: data, version: this.deps.version(data.session_id || '') });
  }

  flush(): void {
    this.frames.flush();
  }
}
