import { afterEach, describe, expect, it, vi } from 'vitest';
import './render';
import * as markdown from './markdown';

// Compiled by typecheck, never executed. Unused expect-error fails if the
// global becomes any again, so these test actual type rejection.
function renderTypeContract(): void {
  // @ts-expect-error Rendering requires an HTML container.
  window.Render.renderMarkdown('chat', 'hello');
  // @ts-expect-error Stable Markdown rendering accepts no completion callback.
  window.Render.updateMarkdown(document.body, 'hello', 42);
  // @ts-expect-error Tool kind must be a string.
  window.Render.toolRowEl({ kind: 42 }, false);
}
void renderTypeContract;

describe('render boundaries', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders receipt basis from proof without changing the final answer', () => {
    const answer = '{"ok":true}';
    expect(window.Render.receiptSummary({ display: { summary: answer } })).toBe(answer);
    expect(
      window.Render.receiptSummary({
        display: { summary: 'Completed' },
        completion_proof: {
          evidence_refs: ['source:verified'],
          limitation_refs: ['limit:observed'],
        },
      }),
    ).toBe('Completed\n依据：source:verified\n限制：limit:observed');
    expect(
      window.Render.receiptSummary({
        display: { summary: 'Completed', evidence_refs: ['model claim'] },
        completion_proof: { evidence_refs: [null, 7], limitation_refs: [] },
      }),
    ).toBe('Completed');
  });

  it('renders Windows CRLF headings and paragraphs without stalling', () => {
    const body = document.createElement('div');
    window.Render.renderMarkdown(body, '# Report\r\n\r\n参数正常\r\n');
    expect(body.querySelector('.md-h1')?.textContent).toBe('Report');
    expect(body.querySelector('p')?.textContent).toBe('参数正常');
  });

  it('keeps bold intact across inline code, file actions and safe links', () => {
    const body = document.createElement('div');
    window.Render.renderMarkdown(body, '**最需要动手的地方：`selfie/ui/main_window.py` 有 2084 行。**');
    expect(body.querySelector('strong [data-document-candidate]')?.textContent).toBe('selfie/ui/main_window.py');
    expect(body.textContent).not.toContain('**');
    body.replaceChildren();
    window.Render.renderMarkdown(body, '**Read [source](https://example.com) and `**literal**`**');
    expect(body.querySelector('strong a')?.textContent).toBe('source');
    expect(body.querySelector('strong code')?.textContent).toBe('**literal**');
  });

  it('returns a finite nonnegative count for malformed receipts', () => {
    expect(window.Render.receiptChangedCount({ work: { changed_count: 'bad' } }, {})).toBe(0);
    expect(window.Render.receiptChangedCount({}, { changed_count: -4 })).toBe(0);
    expect(window.Render.receiptChangedCount({}, { changed_count: 'bad' })).toBe(0);
    expect(window.Render.receiptChangedCount(null, { changed_count: 3 })).toBe(3);
    expect(window.Render.receiptChangedCount({ work: { changed_count: 2 } }, { changed_count: 3 })).toBe(2);
  });

  it('isolates parser failure and retries rendering without invoking any task', () => {
    const body = document.createElement('div');
    const parser = vi.spyOn(markdown, 'renderMarkdownContent').mockImplementationOnce(() => {
      throw new Error('display failed');
    });
    expect(() => window.Render.renderMarkdown(body, 'saved answer')).not.toThrow();
    expect(body.querySelector('.md-render-error')).not.toBeNull();
    body.querySelector<HTMLButtonElement>('.md-render-error button')!.click();
    expect(body.textContent).toContain('saved answer');
    expect(body.querySelector('.md-render-error')).toBeNull();
    parser.mockRestore();
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

  it('keeps a long fenced block intact when later prose arrives', () => {
    const body = document.createElement('div');
    const code = Array.from({ length: 200 }, (_, index) => `print(${index})`).join('\n');
    const initial = '```python\n' + code + '\n```';
    window.Render.updateMarkdown(body, initial);
    const block = body.querySelector('pre')!;
    window.Render.updateMarkdown(body, initial + '\n\nFinal paragraph');
    expect(body.querySelectorAll('pre')).toHaveLength(1);
    expect(body.querySelector('pre')).toBe(block);
    expect(block.querySelector('code')?.textContent).toBe(code);
    expect(body.querySelector('p')?.textContent).toBe('Final paragraph');
  });

  it('keeps an already displayed streaming paragraph mounted as text grows', () => {
    const body = document.createElement('div');
    window.Render.updateMarkdown(body, '第一段已经到达。');
    const paragraph = body.querySelector('p')!;
    window.Render.updateMarkdown(body, '第一段已经到达。\n\n**完整回答**，保留已显示段落。');
    expect(body.querySelector('p')).toBe(paragraph);
    expect(paragraph.textContent).toContain('第一段已经到达。');
  });

  it('uses icon-only copy actions and omits redundant result availability text', () => {
    const body = document.createElement('div');
    window.Render.renderMarkdown(body, '```text\nhello\n```');
    const codeCopy = body.querySelector<HTMLButtonElement>('.code-copy')!;
    expect(codeCopy.textContent).toBe('');
    expect(codeCopy.querySelector('svg')).not.toBeNull();
    expect(codeCopy.getAttribute('aria-label')).toBe('Copy code');

    const row = window.Render.toolRowEl({ kind: 'read', path: 'requirements.txt', result: 'full text' }, false);
    expect(row.querySelector('.tl-kind')?.textContent).toBe('');
    expect(row.querySelector('.tl-path')?.textContent).toBe('requirements.txt');
    expect(row.querySelector('.tl-result')?.textContent).toBe('');
    expect(row.querySelector('.tl-arrow')).toHaveProperty('hidden', true);
  });

  it('groups canonical read tool aliases into a collapsed file group', () => {
    const host = document.createElement('div');
    window.Render.appendOrFoldTool(host, { kind: 'read_file', path: 'README.md', result: 'one' });
    window.Render.appendOrFoldTool(host, { kind: 'read_file', path: 'pyproject.toml', result: 'two' });
    const group = host.querySelector<HTMLElement>('.tool-group')!;
    expect(group.dataset.foldkind).toBe('read');
    expect(group.classList.contains('collapsed')).toBe(true);
    expect(group.querySelector('.tg-kind')?.textContent).toBe('read');
    expect(group.querySelector('.tg-count')?.textContent).toBe('2 files');
    expect(group.querySelectorAll('.tool-line')).toHaveLength(2);
  });

  it('groups events that provide the real tool name separately from kind', () => {
    const host = document.createElement('div');
    window.Render.appendOrFoldTool(host, { toolName: 'read_file', path: 'README.md', result: 'one' });
    window.Render.appendOrFoldTool(host, { toolName: 'read_file', path: 'pyproject.toml', result: 'two' });
    const group = host.querySelector<HTMLElement>('.tool-group')!;
    expect(group.dataset.foldkind).toBe('read');
    expect(group.querySelector('.tg-count')?.textContent).toBe('2 files');
  });

  it('keeps reads in one group when reasoning appears between calls', () => {
    const host = document.createElement('div');
    window.Render.appendOrFoldTool(host, { kind: 'read', path: 'README.md', result: 'one' });
    const reasoning = document.createElement('details');
    reasoning.className = 'thinking';
    host.append(reasoning);
    window.Render.appendOrFoldTool(host, {
      toolName: 'read_file',
      path: 'docs/影像零售技术栈发展方案.md',
      result: 'two',
    });
    const group = host.querySelector<HTMLElement>('.tool-group')!;
    expect(host.querySelectorAll('.tool-group')).toHaveLength(1);
    expect(group.querySelector('.tg-count')?.textContent).toBe('2 files');
    expect(group.textContent).toContain('docs/影像零售技术栈发展方案.md');
  });

  it('does not ellipsize a file path when there is no result column', () => {
    const path = 'selfie/beauty/render_orchestrator/runtime_contract.py';
    const row = window.Render.toolRowEl({ kind: 'read', path }, false);
    expect(row.classList.contains('no-result')).toBe(true);
    expect(row.classList.contains('file-read')).toBe(true);
    expect(row.querySelector('.tl-path')?.textContent).toBe(path);
  });
});
