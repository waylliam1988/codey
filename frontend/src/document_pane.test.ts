import { beforeEach, expect, it, vi } from 'vitest';
import { DocumentPane } from './document_pane';
beforeEach(() => localStorage.clear());

it('restores tabs and reading preferences without storing file contents', async () => {
  const read = vi.fn().mockResolvedValue({
    title: 'private',
    path: 'a.txt',
    content: 'private contents\nsecond\nthird',
    format: 'text',
    version: 'v1',
    workspace: 'root',
  });
  const deps = { read, markdown: vi.fn(), copy: vi.fn() };
  const first = new DocumentPane(deps);
  await first.open('one', { kind: 'workspace', path: 'a.txt' });
  first.element.querySelector<HTMLElement>('[role="tabpanel"]')!.scrollTop = 120;
  first.element.querySelector<HTMLButtonElement>('[aria-label="Wrap lines"]')!.click();
  first.close();
  expect(localStorage.getItem('codey:document-tabs:v1')).not.toContain('private contents');
  const restored = new DocumentPane(deps);
  restored.show('one');
  await vi.waitFor(() => expect(restored.element.querySelector('pre')?.textContent).toContain('third'));
  expect(restored.element.querySelector('.document-body')?.classList.contains('wrap')).toBe(true);
  expect(restored.element.querySelector<HTMLElement>('.document-body')!.scrollTop).toBe(120);
  expect(read).toHaveBeenLastCalledWith('one', { kind: 'workspace', path: 'a.txt' }, 'root');
});

it('finds text beyond the first line and never replaces source with only a preview line', async () => {
  const pane = new DocumentPane({
    read: vi.fn().mockResolvedValue({
      title: 'code',
      path: 'a.py',
      content: 'one\ntwo\nfind <literal>\nfour',
      format: 'text',
      version: 'v1',
      workspace: 'root',
    }),
    markdown: vi.fn(),
    copy: vi.fn(),
  });
  await pane.open('chat', { kind: 'workspace', path: 'a.py', line: 3 });
  expect(pane.element.querySelectorAll('[data-line]')).toHaveLength(4);
  expect(pane.element.querySelector('pre')?.textContent).toBe('one\ntwo\nfind <literal>\nfour');
  const find = pane.element.querySelector<HTMLInputElement>('[aria-label="Find in document"]')!;
  find.value = '<literal>';
  find.dispatchEvent(new Event('input'));
  expect(pane.element.querySelector('mark')?.textContent).toBe('<literal>');
  expect(pane.element.querySelector('literal')).toBeNull();
  find.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  expect(pane.element.querySelector('mark')).toBeNull();
  expect(pane.element.querySelector('pre')?.textContent).toContain('four');
});

it('does not replace the saved reading position with the empty body while a restored tab loads', async () => {
  const data = {
    title: 'Long',
    path: 'long.txt',
    content: 'line\n'.repeat(500),
    format: 'text',
    version: 'v1',
    workspace: 'root',
  };
  const first = new DocumentPane({ read: vi.fn().mockResolvedValue(data), markdown: vi.fn(), copy: vi.fn() });
  await first.open('chat', { kind: 'workspace', path: 'long.txt' });
  first.element.querySelector<HTMLElement>('.document-body')!.scrollTop = 240;
  first.close();
  let resolve: (value: typeof data) => void = () => {};
  const restored = new DocumentPane({
    read: () =>
      new Promise((done) => {
        resolve = done;
      }),
    markdown: vi.fn(),
    copy: vi.fn(),
  });
  restored.show('chat');
  restored.element.querySelector<HTMLElement>('.document-body')!.scrollTop = 0;
  restored.close();
  resolve(data);
  await vi.waitFor(() => expect(restored.element.querySelector('.document-status')).toBeNull());
  restored.show('chat');
  expect(restored.element.querySelector<HTMLElement>('.document-body')!.scrollTop).toBe(240);
});

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
