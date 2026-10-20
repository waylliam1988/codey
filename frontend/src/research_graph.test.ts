import { afterEach, expect, it, vi } from 'vitest';
import { runtime } from './runtime';
import './research_graph';

afterEach(() => {
  runtime.ResearchGraph.dispose();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('wakes a settled graph during drag, keeps its neighbors moving and cleans up cancelled capture', async () => {
  const frames = new Map<number, FrameRequestCallback>();
  let next = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.set(++next, callback);
    return next;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  const arc = vi.fn(),
    translate = vi.fn(),
    scale = vi.fn();
  const context = {
    clearRect() {},
    fillRect() {},
    save() {},
    restore() {},
    setTransform() {},
    translate,
    scale,
    arc,
    beginPath() {},
    fill() {},
    stroke() {},
    moveTo() {},
    lineTo() {},
    setLineDash() {},
    measureText: () => ({ width: 20 }),
    fillText() {},
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 600,
    bottom: 500,
    width: 600,
    height: 500,
    toJSON() {},
  });
  const captured = new Set<number>();
  HTMLCanvasElement.prototype.setPointerCapture = (id) => {
    captured.add(id);
  };
  HTMLCanvasElement.prototype.hasPointerCapture = (id) => captured.has(id);
  HTMLCanvasElement.prototype.releasePointerCapture = (id) => {
    captured.delete(id);
  };
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        ok: true,
        graph: {
          nodes: [
            { id: 'one', label: 'One' },
            { id: 'two', label: 'Two' },
          ],
          edges: [{ src: 'one', dst: 'two' }],
        },
      }),
    }),
  );
  const panel = document.createElement('div');
  document.body.append(panel);
  runtime.ResearchGraph.render(panel, { sessionId: 'chat' });
  await vi.waitFor(() => expect(frames.size).toBe(1));
  const step = () => {
    const [id, callback] = [...frames][0];
    frames.delete(id);
    arc.mockClear();
    callback(0);
  };
  for (let count = 0; frames.size && count < 2000; count++) step();
  expect(frames.size).toBe(0);
  const positions = arc.mock.calls.slice(0, 2).map(([x, y]) => ({ x, y }));
  const [tx, ty] = translate.mock.calls.at(-1)!;
  const [zoom] = scale.mock.calls.at(-1)!;
  const canvas = panel.querySelector('canvas')!;
  const pointer = (type: string, x: number, y: number, id = 1, button = 0) => {
    const event = new MouseEvent(type, { clientX: x, clientY: y, button, bubbles: true, cancelable: true });
    Object.defineProperty(event, 'pointerId', { value: id });
    canvas.dispatchEvent(event);
  };
  const x = positions[0].x * zoom + tx,
    y = positions[0].y * zoom + ty;
  pointer('pointerdown', x, y, 2, 2);
  expect(frames.size).toBe(0);
  pointer('pointerdown', x, y);
  expect(frames.size).toBe(1);
  pointer('pointermove', x + 100, y + 60, 3); // unrelated pointer cannot move the selected node
  pointer('pointermove', x + 100, y + 60);
  for (let count = 0; count < 100; count++) step();
  expect(frames.size).toBe(1); // holding a node never lets the simulation sleep
  const movedNeighbor = arc.mock.calls[1];
  expect(Math.hypot(movedNeighbor[0] - positions[1].x, movedNeighbor[1] - positions[1].y)).toBeGreaterThan(5);
  expect(captured.has(1)).toBe(true);
  pointer('pointercancel', x + 100, y + 60);
  expect(captured.size).toBe(0);
  expect(panel.querySelector('.research-graph-detail')?.textContent).toBe('Select a node');
  expect(canvas.classList.contains('dragging')).toBe(false);
  expect((context as unknown as { fillStyle: string }).fillStyle).toBe('Highlight');
});
