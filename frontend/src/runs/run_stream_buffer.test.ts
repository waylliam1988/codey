import { describe, expect, it, vi } from 'vitest';
import { RunStreamBuffer } from './run_stream_buffer';

describe('RunStreamBuffer', () => {
  it('coalesces by run and stream and checks acceptance when flushing', () => {
    const handle = vi.fn();
    let accepted = true;
    const buffer = new RunStreamBuffer({
      acceptsRun: () => accepted,
      version: () => '0',
      handle,
    });

    buffer.enqueue({ session_id: 's1', run_id: 'r1', stream_id: 'a' });
    buffer.enqueue({ session_id: 's1', run_id: 'r1', stream_id: 'a' });
    accepted = false;
    buffer.flush();

    expect(handle).not.toHaveBeenCalled();
  });

  it('drops a coalesced frame from an older path even if its run is visible again', () => {
    const handle = vi.fn();
    let version = 'old';
    const buffer = new RunStreamBuffer({ acceptsRun: () => true, version: () => version, handle });
    buffer.enqueue({ session_id: 'chat', run_id: 'run', stream_id: 'stream' });
    version = 'new';
    buffer.flush();
    expect(handle).not.toHaveBeenCalled();
  });
});
