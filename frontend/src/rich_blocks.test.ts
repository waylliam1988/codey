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
