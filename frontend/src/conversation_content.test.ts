import { describe, expect, it } from 'vitest';
import './render';

function render(text: string): HTMLElement {
  const host = document.createElement('div');
  window.Render.renderMarkdown(host, text);
  return host;
}

describe('conversation content contract', () => {
  it('repairs model emphasis with a space before the closing stars, preserving code literals', () => {
    const host = render('**最需要动手：`selfie/ui/main_window.py` 有 2084 行。 ** 继续阅读。\n\n`**literal **`');
    expect(host.querySelector('strong')?.textContent).toBe('最需要动手：selfie/ui/main_window.py 有 2084 行。');
    expect(host.querySelector('p:last-child code')?.textContent).toBe('**literal **');
  });
  it('keeps code pipes and the following table value', () => {
    const host = render('| key | value |\n|---|---|\n| `a|b` | x |');
    expect([...host.querySelectorAll('td')].map((cell) => cell.textContent)).toEqual(['a|b', 'x']);
  });
  it('preserves a pipe inside inline code in ordinary prose', () => {
    const host = render('命令片段：`a|b`');
    expect(host.querySelector('code')?.textContent).toBe('a|b');
  });
  it('preserves strong formatting around code containing double stars', () => {
    const host = render('**说明：`a ** b` 结束 **');
    expect(host.querySelector('strong')?.textContent).toBe('说明：a ** b 结束');
    expect(host.querySelector('code')?.textContent).toBe('a ** b');
  });

  it('renders common formatting, task items, separators and images', () => {
    const host = render(
      '*强调* ~~取消~~\n\n- [x] 完成\n- [ ] 待办\n\n---\n\n![检查图](https://example.test/image.png)',
    );
    expect(host.querySelector('em')?.textContent).toBe('强调');
    expect(host.querySelector('s')?.textContent).toBe('取消');
    expect(host.querySelectorAll('input[type=checkbox]:disabled')).toHaveLength(2);
    expect(host.querySelector('input')?.checked).toBe(true);
    expect(host.querySelector('hr')).not.toBeNull();
    expect(host.querySelector('img')?.getAttribute('src')).toBe('https://example.test/image.png');
  });

  it('preserves balanced link parentheses and never promotes dotted symbols', () => {
    const host = render('[说明](https://example.test/docs(foo))\n\n`json.dumps` `os.path`');
    expect(host.querySelector('a')?.href).toBe('https://example.test/docs(foo)');
    expect(host.querySelectorAll('[data-document-path]')).toHaveLength(0);
    expect(host.querySelectorAll('code')).toHaveLength(2);
  });

  it('never activates HTML or executable link and image schemes', () => {
    const host = render('<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1))\n\n![x](data:text/html,evil)');
    expect(host.querySelector('[onerror],script,a,img')).toBeNull();
    expect(host.textContent).toContain('<img');
  });
  it('keeps a malformed percent escape from breaking the rest of the answer', () => {
    const host = render('[report](report%AB.md)\n\n结论仍可阅读');
    expect(host.querySelector('.md-render-error')).toBeNull();
    expect(host.querySelector('p:last-child')?.textContent).toBe('结论仍可阅读');
  });

  it('keeps a full tool result inspectable without exposing it in the summary', () => {
    const result = 'retained output '.repeat(100);
    const host = window.Render.standaloneToolEl({ kind: 'read', path: 'cache.py', result });
    expect(host.querySelector('details')).not.toBeNull();
    expect(host.querySelector('pre')?.textContent).toBe(result);
    expect(host.querySelector('summary')?.textContent).not.toContain(result);
  });

  it('shows an edit diff and measured counts rather than another generic result row', () => {
    const host = window.Render.standaloneToolEl({
      kind: 'edit',
      path: 'cache.py',
      result: 'edited cache.py',
      preview: { additions: 2, deletions: 1, diff: '-old\n+new\n+added', diff_truncated: false },
    });
    expect(host.querySelector('summary')?.textContent).toContain('+2 −1');
    expect(host.querySelector('pre')?.textContent).toBe('-old\n+new\n+added');
  });

  it('separates an actionable command denial from its original error', () => {
    const result = 'ERROR: project guard denied run: command_not_allowed; use shell with user approval';
    const host = window.Render.standaloneToolEl({ kind: 'run', command: 'python test.py', result, error: true });
    expect(host.querySelector('summary')?.textContent).not.toContain('requires approval');
    expect(host.querySelector('summary')?.textContent).toContain('blocked');
    expect(host.querySelector('pre')?.textContent).toBe(result);
  });
});
