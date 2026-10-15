import { expect, it, vi } from 'vitest';
import { DocumentPane } from './document_pane';

it('preserves the reading position when reloading the current document', async () => {
  const pane = new DocumentPane({
    read: vi.fn().mockResolvedValue({
      title: 'Long document',
      path: 'long.txt',
      content: 'line\n'.repeat(500),
      format: 'text',
      version: 'v1',
      workspace: 'root',
    }),
    markdown: vi.fn(),
    copy: vi.fn(),
  });
  await pane.open('chat', { kind: 'workspace', path: 'long.txt' });
  pane.element.querySelector<HTMLElement>('[role="tabpanel"]')!.scrollTop = 240;
  await pane.reload();
  expect(pane.element.querySelector<HTMLElement>('[role="tabpanel"]')!.scrollTop).toBe(240);
});

it('resolves links inside a workspace document relative to its own directory', async () => {
  const pane = new DocumentPane({
    read: vi.fn().mockResolvedValue({
      title: 'report',
      path: 'docs/report.md',
      content: 'report',
      format: 'text',
      version: 'v1',
      workspace: 'root',
    }),
    markdown: vi.fn(),
    copy: vi.fn(),
  });
  await pane.open('chat', { kind: 'workspace', path: 'docs/report.md' });
  expect(pane.fileLink('probe.py', 9)).toEqual({ kind: 'workspace', path: 'docs/probe.py', line: 9 });
  expect(pane.fileLink('E:/other/file.py')).toEqual({ kind: 'workspace', path: 'E:/other/file.py' });
});

it('retains the focused document when the same chat renders again', async () => {
  const pane = new DocumentPane({
    read: vi.fn().mockResolvedValue({ title: 'a', path: 'a.txt', content: 'a', format: 'text', version: 'v1' }),
    markdown: vi.fn(),
    copy: vi.fn(),
  });
  await pane.open('chat', { kind: 'workspace', path: 'a.txt' });
  const body = pane.element.querySelector('[role="tabpanel"]');
  pane.setSession('chat');
  expect(pane.element.querySelector('[role="tabpanel"]')).toBe(body);
});

it('formats JSON and reveals a referenced source line', async () => {
  const read = vi
    .fn()
    .mockResolvedValueOnce({ title: 'data', path: 'data.json', content: '{"a":1}', format: 'json', version: 'v1' })
    .mockResolvedValueOnce({
      title: 'code',
      path: 'code.py',
      content: 'one\ntwo\nthree',
      format: 'text',
      version: 'v1',
    });
  const pane = new DocumentPane({ read, markdown: vi.fn(), copy: vi.fn() });
  await pane.open('chat', { kind: 'workspace', path: 'data.json' });
  expect(pane.element.querySelector('pre')?.textContent).toBe('{\n  "a": 1\n}');
  await pane.open('chat', { kind: 'workspace', path: 'code.py', line: 2 });
  expect(pane.element.querySelector('[data-line="2"].selected-line')?.textContent).toBe('two\n');
});

it('shares workspace and research tabs, preserves source and isolates chats', async () => {
  const read = vi.fn().mockResolvedValue({
    ok: true,
    title: 'Report',
    path: 'docs/report.md',
    format: 'markdown',
    content: '# report',
    version: 'v1',
  });
  const pane = new DocumentPane({
    read,
    markdown: (el, text) => {
      el.textContent = `preview:${text}`;
    },
    copy: vi.fn(),
  });
  document.body.append(pane.element);
  await pane.open('chat', { kind: 'workspace', path: 'docs/report.md' });
  expect(pane.element.textContent).toContain('preview:# report');
  (pane.element.querySelector('[aria-label="View source"]') as HTMLButtonElement).click();
  expect(pane.element.querySelector('pre')?.textContent).toBe('# report');
  await pane.open('chat', { kind: 'research', note_id: 'research-report' });
  expect(pane.element.querySelectorAll('[role="tab"]')).toHaveLength(2);
  pane.setSession('other');
  expect(pane.element.hidden).toBe(true);
  pane.setSession('chat');
  expect(pane.element.hidden).toBe(false);
  expect(pane.element.querySelectorAll('[role="tab"]')).toHaveLength(2);
  pane.element.remove();
});

it('never replaces a newer document with a late response and shows reload failure', async () => {
  let resolveFirst: (value: unknown) => void = () => {};
  const read = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    )
    .mockResolvedValueOnce({ ok: true, title: 'New', path: 'new.md', content: 'NEW', format: 'text', version: 'v2' })
    .mockRejectedValueOnce(new Error('document_missing'));
  const pane = new DocumentPane({ read, markdown: vi.fn(), copy: vi.fn() });
  const first = pane.open('chat', { kind: 'workspace', path: 'old.md' });
  await pane.open('chat', { kind: 'workspace', path: 'new.md' });
  const currentBody = pane.element.querySelector('[role="tabpanel"]');
  resolveFirst({ ok: true, title: 'Old', path: 'old.md', content: 'OLD', format: 'text', version: 'v1' });
  await first;
  expect(pane.element.querySelector('[role="tabpanel"]')).toBe(currentBody);
  expect(pane.element.querySelector('pre')?.textContent).toBe('NEW');
  await pane.reload();
  expect(pane.element.textContent).toContain('document_missing');
});

it('keeps secondary document actions compact and accessible', async () => {
  const copy = vi.fn().mockResolvedValue(true);
  const pane = new DocumentPane({
    read: vi.fn().mockResolvedValue({
      title: 'Report',
      path: 'docs/report.md',
      content: '# report',
      format: 'markdown',
      version: 'v1',
      workspace: 'root',
    }),
    markdown: vi.fn(),
    copy,
  });
  await pane.open('chat', { kind: 'workspace', path: 'docs/report.md' });
  const action = pane.element.querySelector<HTMLButtonElement>('[aria-label="Copy document"]')!;
  expect(action.textContent).toBe('');
  expect(action.querySelector('svg')).not.toBeNull();
  action.click();
  expect(copy).toHaveBeenCalledWith('# report');
  expect(pane.element.querySelector('[aria-label="Reload"]')?.textContent).toBe('');
  expect(pane.element.querySelector('.document-breadcrumb')?.textContent).toBe('docs / report.md');
});
