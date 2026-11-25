import { expect, it } from 'vitest';
import { nestedOwnsScroll } from './scroll_owner';

it('lets inner output consume scrolling until its directional boundary', () => {
  const root = document.createElement('div'),
    output = document.createElement('pre');
  output.dataset.scrollable = '';
  root.append(output);
  Object.defineProperties(output, { scrollHeight: { value: 300 }, clientHeight: { value: 100 } });
  output.scrollTop = 50;
  expect(nestedOwnsScroll(output, root, -20)).toBe(true);
  expect(nestedOwnsScroll(output, root, 20)).toBe(true);
  output.scrollTop = 0;
  expect(nestedOwnsScroll(output, root, -20)).toBe(false);
  expect(nestedOwnsScroll(output, root, 20)).toBe(true);
  output.scrollTop = 200;
  expect(nestedOwnsScroll(output, root, 20)).toBe(false);
  expect(nestedOwnsScroll(root, root, -20)).toBe(false);
});
