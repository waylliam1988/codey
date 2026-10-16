import { expect, it } from 'vitest';
import { parseDocumentLink } from './document_links';
import './render';

it('parses Windows paths, spaces and line references without treating a drive as a URL scheme', () => {
  expect(parseDocumentLink('E:\\Selfie - 副本\\docs\\报告.md:12:3')).toEqual({
    path: 'E:/Selfie - 副本/docs/报告.md',
    line: 12,
  });
  expect(parseDocumentLink('docs/report.md#L8')).toEqual({ path: 'docs/report.md', line: 8 });
  expect(parseDocumentLink('javascript:alert(1)')).toBeNull();
  expect(parseDocumentLink('https://example.com/report.md')).toBeNull();
});

it('renders local links and output filenames as file actions without executing Markdown HTML', () => {
  const body = document.createElement('div');
  window.Render.renderMarkdown(
    body,
    '[报告](<E:/Selfie - 副本/docs/报告.md:12>) `docs/probe.py` <img src=x onerror=alert(1)>',
  );
  expect(body.querySelectorAll('[data-document-path]')).toHaveLength(1);
  expect(body.querySelector('[data-document-candidate]')?.textContent).toBe('docs/probe.py');
  expect(body.querySelector('[data-document-line]')?.getAttribute('data-document-line')).toBe('12');
  expect(body.querySelector('img')).toBeNull();
});

it('does not turn glob patterns or dotted member names into file tabs', () => {
  expect(parseDocumentLink('docs/*.txt')).toBeNull();
  expect(parseDocumentLink('self._owner_thread_id')).toBeNull();
  expect(parseDocumentLink('beauty/cache.py')).toEqual({ path: 'beauty/cache.py' });
});
