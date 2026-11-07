import { FrameCoalescer } from './frame_coalescer';

export interface RunStreamEvent {
  session_id?: string;
  run_id?: string;
  stream_id?: string;
}

export interface RunStreamBufferDeps<T extends RunStreamEvent> {
  acceptsRun(sessionId: string, runId: string): boolean;
  handle(data: T): void;
}

export class RunStreamBuffer<T extends RunStreamEvent> {
  private readonly frames: FrameCoalescer<T>;

  constructor(private readonly deps: RunStreamBufferDeps<T>) {
    this.frames = new FrameCoalescer((_key, data) => {
      const sessionId = data.session_id || '';
      const runId = data.run_id || '';
      if (this.deps.acceptsRun(sessionId, runId)) this.deps.handle(data);
    });
  }

  enqueue(data: T): void {
    const key = `${data.run_id || ''}:${data.stream_id || ''}`;
    this.frames.enqueue(key, data);
  }

  flush(): void {
    this.frames.flush();
  }
}
