import { expect, it, vi } from 'vitest';
import { Render } from './render';

it('renders Chinese emphasis when the closing stars and following text arrive', () => {
  const host = document.createElement('div');
  const prefix = '作者将网络作为**层（Layers）';
  Render.updateMarkdown(host, prefix);
  expect(host.querySelector('strong')).toBeNull();
  Render.updateMarkdown(host, prefix + '**');
  expect(host.querySelector('strong')?.textContent).toBe('层（Layers）');
  Render.updateMarkdown(host, prefix + '**集成。');
  expect(host.querySelector('strong')?.textContent).toBe('层（Layers）');
  expect(host.textContent).toBe('作者将网络作为层（Layers）集成。');
});

it('keeps completed paragraphs and code blocks mounted while the tail changes', () => {
  const host = document.createElement('div');
  Render.updateMarkdown(host, '第一段。\n\n```py\nprint(1)\n```\n\n尾');
  const first = host.querySelector('p'),
    code = host.querySelector('pre');
  Render.updateMarkdown(host, '第一段。\n\n```py\nprint(1)\n```\n\n尾部继续');
  expect(host.querySelector('p')).toBe(first);
  expect(host.querySelector('pre')).toBe(code);
  expect(host.textContent).toContain('尾部继续');
  Render.updateMarkdown(host, '第一段。\n\n```py\nprint(1)\n```\n\n尾部继续');
  expect(host.querySelectorAll('pre')).toHaveLength(1);
});

it('defers selected text with one listener, then applies the latest tail', () => {
  const host = document.createElement('div');
  document.body.append(host);
  Render.updateMarkdown(host, '原始段落');
  const anchor = host.querySelector('p')!.firstChild;
  const selection = vi
    .spyOn(window, 'getSelection')
    .mockReturnValue({ isCollapsed: false, anchorNode: anchor } as Selection);
  const listen = vi.spyOn(document, 'addEventListener');
  Render.updateMarkdown(host, '新段落');
  Render.updateMarkdown(host, '最新段落');
  expect(listen.mock.calls.filter(([event]) => event === 'selectionchange')).toHaveLength(1);
  selection.mockReturnValue({ isCollapsed: true } as Selection);
  document.dispatchEvent(new Event('selectionchange'));
  expect(host.textContent).toBe('最新段落');
  listen.mockRestore();
  selection.mockRestore();
  host.remove();
});
