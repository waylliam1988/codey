import { describe, expect, it } from 'vitest';
import { renderMarkdownContent } from './markdown';

describe('shared rich markdown blocks', () => {
  it('renders supported math but keeps code and dollar amounts literal', () => {
    const host = document.createElement('div');
    renderMarkdownContent(host, '金额 $5 与 `\\(x\\)`\n\n\\(x^2\\)');
    expect(host.textContent).toContain('金额 $5');
    expect(host.querySelector('math')).not.toBeNull();
    expect(host.querySelector('code')?.textContent).toBe('\\(x\\)');
  });

  it('keeps incomplete mermaid fences as source and recognizes a stable block', () => {
    const host = document.createElement('div');
    renderMarkdownContent(host, '```mermaid\ngraph TD\n A-->B');
    expect(host.querySelector('pre code')?.textContent).toContain('graph TD');
    const complete = document.createElement('div');
    renderMarkdownContent(complete, '```mermaid\ngraph TD\n A-->B\n```');
    expect(complete.querySelector('[data-mermaid-source]')?.textContent).toContain('graph TD');
    expect(complete.querySelector('svg.md-mermaid-svg')).not.toBeNull();
  });
});

it.each([
  '$C = (F - 32) \\times \\frac{5}{9}$',
  '$$C = (F - 32) \\times \\frac{5}{9}$$',
  '$$\nC = (F - 32) \\times \\frac{5}{9}\n$$',
  '\\[C = (F - 32) \\times \\frac{5}{9}\\]',
  '\\[C = (F - 32)\n \\times \\frac{5}{9}\\]',
  '\\(C = (F - 32) \\times \\frac{5}{9}\\)',
  '- Temperature: $C = \\frac{5}{9}(F-32)$',
  '| Temperature |\n| --- |\n| $C = \\frac{5}{9}(F-32)$ |',
])('renders formula delimiters without losing content: %s', (source) => {
  const host = document.createElement('div');
  renderMarkdownContent(host, source);
  expect(host.querySelectorAll('math')).toHaveLength(1);
  expect(host.querySelector('annotation')?.textContent).toContain('C =');
  expect(host.querySelector('.mfrac')).not.toBeNull();
  expect(host.querySelector('[data-math-error]')).toBeNull();
});

it('preserves currency, escaped delimiters, code and incomplete streamed math', () => {
  const host = document.createElement('div');
  const source = 'Prices $5 and $10. Escaped \\$x$ and `$x$`.\n\n```tex\n$$x$$\n```\n\n$C = \\frac{5}{9}';
  renderMarkdownContent(host, source);
  expect(host.querySelector('math')).toBeNull();
  expect(host.textContent).toContain('Prices $5 and $10');
  expect(host.querySelector('pre code')?.textContent).toBe('$$x$$');
  renderMarkdownContent(host, '$C = \\frac{5}{9}$');
  expect(host.querySelector('math')).not.toBeNull();
});

it('keeps malformed math readable and does not enable trusted HTML commands', () => {
  const host = document.createElement('div');
  renderMarkdownContent(host, '$\\invalidcommand{x}$ and $\\href{javascript:alert(1)}{click}$');
  expect(host.querySelector('[data-math-error]')?.textContent).toContain('invalidcommand');
  expect(host.querySelector('a[href^="javascript:"]')).toBeNull();
});
