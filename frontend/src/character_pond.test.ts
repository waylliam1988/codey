import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mountCharacterPond } from './character_pond';

let dispose = () => {};
let callbacks: Map<number, FrameRequestCallback>;
let serial = 0;
let time = 0;
let main: HTMLElement;
let ctx: {
  clearRect: ReturnType<typeof vi.fn>;
  fillText: ReturnType<typeof vi.fn>;
  setTransform: ReturnType<typeof vi.fn>;
  font: string;
  textBaseline: string;
  globalAlpha: number;
  fillStyle: string;
};

beforeEach(() => {
  vi.useFakeTimers();
  callbacks = new Map();
  serial = 0;
  time = 0;
  ctx = {
    clearRect: vi.fn(),
    fillText: vi.fn(),
    setTransform: vi.fn(),
    font: '',
    textBaseline: '',
    globalAlpha: 1,
    fillStyle: '',
  };
  document.body.innerHTML = '<main class="empty-conversation"><textarea></textarea></main>';
  main = document.querySelector('main')!;
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0,
    y: 44,
    left: 0,
    top: 44,
    width: 480,
    height: 480,
    right: 480,
    bottom: 524,
    toJSON() {},
  });
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    callbacks.set(++serial, callback);
    return serial;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => callbacks.delete(id));
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  dispose();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function advance(frames: number) {
  for (let i = 0; i < frames; i++) {
    time += 34;
    const batch = [...callbacks.values()];
    callbacks.clear();
    batch.forEach((callback) => callback(time));
  }
}

it('animates without pointer input and resumes from blur on hover without clicking', () => {
  dispose = mountCharacterPond(main);
  advance(25);
  expect(ctx.fillText).toHaveBeenCalled();
  expect(callbacks.size).toBe(1);
  window.dispatchEvent(new Event('blur'));
  expect(callbacks.size).toBe(0);
  // No focus, pointerdown, or click is dispatched.
  main.dispatchEvent(new MouseEvent('pointermove', { clientX: 100, clientY: 140, bubbles: true }));
  expect(callbacks.size).toBe(1);
  advance(10);
  main.dispatchEvent(new Event('pointerleave'));
  expect(callbacks.size).toBe(1);
});

it('fades after the first message, stops, and restarts only for an empty chat', async () => {
  dispose = mountCharacterPond(main);
  main.classList.remove('empty-conversation');
  await Promise.resolve();
  expect(callbacks.size).toBe(1);
  vi.advanceTimersByTime(530);
  expect(callbacks.size).toBe(0);
  main.dispatchEvent(new MouseEvent('pointermove', { clientX: 100, clientY: 140 }));
  expect(callbacks.size).toBe(0);
  main.classList.add('empty-conversation');
  await Promise.resolve();
  expect(callbacks.size).toBe(1);
  dispose();
  expect(main.querySelector('canvas')).toBeNull();
  expect(callbacks.size).toBe(0);
});

it('pauses on hidden pages and restores without relying on reduced-motion preferences', () => {
  const media = vi.fn();
  vi.stubGlobal('matchMedia', media);
  dispose = mountCharacterPond(main);
  const hidden = vi.spyOn(document, 'hidden', 'get');
  hidden.mockReturnValue(true);
  document.dispatchEvent(new Event('visibilitychange'));
  expect(callbacks.size).toBe(0);
  hidden.mockReturnValue(false);
  document.dispatchEvent(new Event('visibilitychange'));
  expect(callbacks.size).toBe(1);
  expect(media).not.toHaveBeenCalled();
});
