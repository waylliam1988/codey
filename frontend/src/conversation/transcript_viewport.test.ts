import { afterEach, expect, it, vi } from 'vitest';
import { createTranscriptViewport, type TranscriptView } from './transcript_viewport';

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

it('keeps reading intent on growth and returns to following only at the latest edge', () => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
    },
  );
  const area = document.createElement('div');
  const chat = document.createElement('div');
  const row = document.createElement('div');
  chat.append(row);
  area.append(chat);
  document.body.append(area);

  let height = 500;
  let position = 400;
  Object.defineProperties(area, {
    scrollHeight: { get: () => height },
    clientHeight: { value: 100 },
    scrollTop: {
      get: () => position,
      set: (value: number) => {
        position = Math.max(0, Math.min(value, height - 100));
      },
    },
  });
  vi.spyOn(row, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 200 - area.scrollTop, 100, 250));

  let saved: TranscriptView | undefined;
  const onReading = vi.fn();
  const loadEarlier = vi.fn();
  const viewport = createTranscriptViewport(area, chat, {
    sessionId: () => 'chat',
    currentView: () => saved,
    capture: () => {
      saved = viewport.capture();
    },
    updateLatest: vi.fn(),
    onResize: vi.fn(),
    loadEarlier,
    onReading,
  });
  const wheel = (deltaY: number) => area.dispatchEvent(new WheelEvent('wheel', { deltaY, bubbles: true }));
  const move = (top: number) => {
    area.scrollTop = top;
    area.dispatchEvent(new Event('scroll'));
  };
  wheel(-40);
  move(75);
  expect(viewport.following()).toBe(false);
  expect(onReading).toHaveBeenCalledOnce();
  expect(loadEarlier).toHaveBeenCalledOnce();
  height = 600;
  viewport.syncLayout();
  expect(area.scrollTop).toBe(75);

  wheel(40);
  move(499);
  expect(viewport.following()).toBe(true);
  height = 650;
  viewport.syncLayout();
  expect(area.scrollTop).toBe(550);
});
