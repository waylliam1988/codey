import { afterEach, describe, expect, it, vi } from 'vitest';
import './render';

// Compiled by typecheck, never executed. Unused expect-error fails if the
// global becomes any again, so these test actual type rejection.
function renderTypeContract(): void {
  // @ts-expect-error Rendering requires an HTML container.
  window.Render.renderMarkdown('chat', 'hello');
  // @ts-expect-error Chunk callbacks must be callable.
  window.Render.renderMarkdownChunked(document.body, 'hello', 42);
  // @ts-expect-error Tool kind must be a string.
  window.Render.toolRowEl({ kind: 42 }, false);
}
void renderTypeContract;

describe('render boundaries', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('returns a finite nonnegative count for malformed receipts', () => {
    expect(window.Render.receiptChangedCount({ work: { changed_count: 'bad' } }, {})).toBe(0);
    expect(window.Render.receiptChangedCount({}, { changed_count: -4 })).toBe(0);
    expect(window.Render.receiptChangedCount({}, { changed_count: 'bad' })).toBe(0);
    expect(window.Render.receiptChangedCount(null, { changed_count: 3 })).toBe(3);
    expect(window.Render.receiptChangedCount({ work: { changed_count: 2 } }, { changed_count: 3 })).toBe(2);
  });

  it('preserves nested lists, code text and safe links', () => {
    const body = document.createElement('div');
    window.Render.renderMarkdown(
      body,
      '- parent\n  - child\n\n```ts\n<x>\n```\n\n[unsafe](javascript:alert) [safe](https://example.com)',
    );
    expect(body.querySelector('ul > li > ul > li')?.textContent).toBe('child');
    expect(body.querySelector('pre code')?.textContent).toBe('<x>');
    expect(body.querySelectorAll('a')).toHaveLength(1);
    expect(body.querySelector('a')?.href).toBe('https://example.com/');
  });

  it('does not publish a cancelled chunk render or call its completion', () => {
    const scheduled: Array<() => void> = [];
    vi.stubGlobal('requestIdleCallback', (callback: () => void) => {
      scheduled.push(callback);
    });
    const body = document.createElement('div');
    const done = vi.fn();
    let current = true;
    window.Render.renderMarkdownChunked(body, 'long paragraph\n\n'.repeat(200), done, () => current);
    current = false;
    for (const callback of scheduled) callback();
    expect(body.childNodes).toHaveLength(0);
    expect(done).not.toHaveBeenCalled();
  });
});
