import { expect, it, vi } from 'vitest';
import { artifactRow, decorateDeliverables } from './artifacts';

it('uses one primary document action and a named secondary Research menu', () => {
  const open = vi.fn();
  const row = artifactRow('Research report.md', 'Research · Markdown', () => open('report'), [
    { label: 'View research evidence', action: () => open('evidence') },
  ]);
  row.querySelector<HTMLButtonElement>('.artifact-open')!.click();
  expect(open).toHaveBeenCalledWith('report');
  expect(row.textContent).not.toContain('Open report');
  expect(row.querySelector('summary')?.getAttribute('aria-label')).toBe('Research actions');
});

it('turns explicit deliverables into compact rows, leaving ordinary references alone', () => {
  const host = document.createElement('div');
  host.innerHTML =
    '<p>See <button data-document-path="cache.py">cache.py</button>.</p><p><strong>产出文件</strong></p><ul><li><button data-document-path="docs/report.md"><code>docs/report.md</code></button> —— 完整报告</li></ul>';
  decorateDeliverables(host);
  expect(host.querySelectorAll('.artifact-row')).toHaveLength(1);
  expect(host.querySelector('p [data-document-path]')?.textContent).toBe('cache.py');
  expect(host.querySelector('.artifact-row [data-document-path]')?.textContent).toContain('report.md');
});
