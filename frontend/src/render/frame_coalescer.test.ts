import { describe, expect, it, vi } from 'vitest';
import { FrameCoalescer } from './frame_coalescer';

describe('FrameCoalescer', () => {
  it('consumes only the newest update for each key per frame', () => {
    let run!: () => void;
    const consume = vi.fn();
    const scheduler = new FrameCoalescer(
      consume,
      (callback) => {
        run = callback;
        return 1;
      },
      vi.fn(),
    );

    scheduler.enqueue('run-1:stream-1', 'a');
    scheduler.enqueue('run-1:stream-1', 'b');
    scheduler.enqueue('run-2:stream-2', 'x');
    expect(consume).not.toHaveBeenCalled();

    run();
    expect(consume.mock.calls).toEqual([
      ['run-1:stream-1', 'b'],
      ['run-2:stream-2', 'x'],
    ]);
  });

  it('flushes pending updates immediately and cancels the scheduled frame', () => {
    const cancel = vi.fn();
    const consume = vi.fn();
    const scheduler = new FrameCoalescer(
      consume,
      vi.fn(() => 7),
      cancel,
    );

    scheduler.enqueue('stream', 'latest');
    scheduler.flush();

    expect(cancel).toHaveBeenCalledWith(7);
    expect(consume).toHaveBeenCalledWith('stream', 'latest');
  });
});
