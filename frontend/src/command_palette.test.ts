import { afterEach, expect, it, vi } from 'vitest';
import { CommandPalette } from './command_palette';

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});
it('filters the shared action list and uses keyboard navigation to invoke exactly one action', () => {
  Element.prototype.scrollIntoView = vi.fn();
  const files = vi.fn(),
    search = vi.fn();
  const palette = new CommandPalette(() => [
    { id: 'files', label: 'Files', run: files },
    { id: 'search', label: 'Search this conversation', run: search },
  ]);
  palette.element.showModal = () => {
    palette.element.open = true;
  };
  palette.element.close = () => {
    palette.element.open = false;
  };
  document.body.append(palette.element);
  palette.open();
  const input = palette.element.querySelector('input')!;
  input.value = 'search';
  input.dispatchEvent(new Event('input'));
  expect(palette.element.querySelectorAll('[role="option"]')).toHaveLength(1);
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  expect(search).toHaveBeenCalledTimes(1);
  expect(files).not.toHaveBeenCalled();
  expect(palette.element.open).toBe(false);
});
