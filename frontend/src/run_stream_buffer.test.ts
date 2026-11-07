import { describe, expect, it, vi } from 'vitest';
import { RunStreamBuffer } from './run_stream_buffer';

describe('RunStreamBuffer', () => {
  it('coalesces by run and stream and checks acceptance when flushing', () => {
    const handle = vi.fn();
    let accepted = true;
    const buffer = new RunStreamBuffer({
      acceptsRun: () => accepted,
      handle,
    });

    buffer.enqueue({ session_id: 's1', run_id: 'r1', stream_id: 'a' });
    buffer.enqueue({ session_id: 's1', run_id: 'r1', stream_id: 'a' });
    accepted = false;
    buffer.flush();

    expect(handle).not.toHaveBeenCalled();
  });
});
