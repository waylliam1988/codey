import MarkdownIt from 'markdown-it';
import taskLists from 'markdown-it-task-lists';
import katex from 'katex';
import 'katex/dist/katex.min.css';
import { parseDocumentLink } from './document_links';

const parser = new MarkdownIt({ html: false, linkify: false, breaks: false }).use(taskLists);
const escape = parser.utils.escapeHtml;

function mathMarkup(source: string, display: boolean): string {
  const value = source.trim();
  try {
    return katex.renderToString(value, {
      displayMode: display,
      throwOnError: true,
      output: 'htmlAndMathml',
      strict: 'warn',
    });
  } catch {
    return `<code class="math-source" data-math-error="true">${escape(value)}</code>`;
  }
}

function escapedAt(source: string, position: number): boolean {
  let slashes = 0;
  while (position > 0 && source[--position] === '\\') slashes++;
  return slashes % 2 === 1;
}
function closingMath(source: string, close: string, start: number): number {
  for (let end = source.indexOf(close, start); end >= 0; end = source.indexOf(close, end + close.length)) {
    if (!escapedAt(source, end)) return end;
  }
  return -1;
}
parser.inline.ruler.before('escape', 'math_inline', (state, silent) => {
  const start = state.pos;
  const dollar = state.src[start] === '$';
  const display = state.src.startsWith('$$', start) || state.src.startsWith('\\[', start);
  const open = dollar ? (display ? '$$' : '$') : display ? '\\[' : '\\(';
  if (!state.src.startsWith(open, start)) return false;
  const close = dollar ? open : display ? '\\]' : '\\)';
  const from = start + open.length;
  if (dollar && !display && (/[\s$]/.test(state.src[from] || ' ') || state.src[start - 1] === '$')) return false;
  const end = closingMath(state.src, close, from);
  if (end < 0 || end === from) return false;
  if (dollar && !display && /^\d[\d.,]*\s+[A-Za-z]/.test(state.src.slice(from, end))) return false;
  // Currency and unmatched streaming delimiters stay prose. Code spans/fences
  // remain owned by Markdown's code rules rather than a global replacement.
  if (
    dollar &&
    !display &&
    (/\s/.test(state.src[end - 1]) ||
      /[\d$]/.test(state.src[end + 1] || '') ||
      state.src.slice(from, end).includes('\n'))
  )
    return false;
  if (!silent) {
    const token = state.push(display ? 'math_display' : 'math_inline', 'math', 0);
    token.content = state.src.slice(from, end);
  }
  state.pos = end + close.length;
  return true;
});
parser.renderer.rules.math_inline = (tokens, index) => mathMarkup(tokens[index].content, false);
parser.renderer.rules.math_display = (tokens, index) => mathMarkup(tokens[index].content, true);

parser.block.ruler.before(
  'fence',
  'math_block',
  (state, startLine, endLine, silent) => {
    if (state.sCount[startLine] - state.blkIndent >= 4) return false;
    const first = state.getLines(startLine, startLine + 1, state.blkIndent, false).trim();
    const open = first.startsWith('$$') ? '$$' : first.startsWith('\\[') ? '\\[' : '';
    if (!open) return false;
    const close = open === '$$' ? '$$' : '\\]';
    const body: string[] = [];
    let next = startLine;
    for (; next < endLine; next++) {
      const line =
        next === startLine
          ? first.slice(open.length)
          : state.getLines(next, next + 1, state.blkIndent, false).trimEnd();
      const end = closingMath(line, close, 0);
      if (end >= 0) {
        if (line.slice(end + close.length).trim()) return false;
        body.push(line.slice(0, end));
        break;
      }
      body.push(line);
    }
    if (next >= endLine) return false;
    if (silent) return true;
    const token = state.push('math_block', 'math', 0);
    token.content = body.join('\n');
    token.map = [startLine, next + 1];
    state.line = next + 1;
    return true;
  },
  { alt: ['paragraph', 'reference', 'blockquote', 'list'] },
);
parser.renderer.rules.math_block = (tokens, index) => mathMarkup(tokens[index].content, true) + '\n';
// Model prose often puts padding before closing **. Treat that padding as
// outside emphasis; code spans and fenced code are owned by earlier rules.
parser.inline.ruler.before('emphasis', 'spaced_strong', (state, silent) => {
  const start = state.pos;
  if (state.src.slice(start, start + 2) !== '**') return false;
  let end = -1;
  for (let cursor = start + 2; cursor < state.src.length - 1; cursor++) {
    if (state.src[cursor] === '`') {
      const run = state.src.slice(cursor).match(/^`+/)?.[0] || '`';
      const close = state.src.indexOf(run, cursor + run.length);
      if (close < 0) break;
      cursor = close + run.length - 1;
      continue;
    }
    if (state.src.slice(cursor, cursor + 2) === '**') {
      end = cursor;
      break;
    }
  }
  if (end < 0) return false;
  const content = state.src.slice(start + 2, end),
    body = content.trimEnd();
  if (!body || /^\s/.test(body) || body === content || body.includes('\n')) return false;
  if (silent) return true;
  state.push('strong_open', 'strong', 1);
  const children: typeof state.tokens = [];
  parser.inline.parse(body, parser, state.env, children);
  children.forEach((token) => {
    token.level += state.level;
    state.tokens.push(token);
  });
  state.push('strong_close', 'strong', -1);
  state.push('text', '', 0).content = content.slice(body.length);
  state.pos = end + 2;
  return true;
});
parser.renderer.rules.fence = (tokens, index) => {
  const token = tokens[index];
  const language = token.info.trim().split(/\s+/)[0];
  const mermaid = language.toLowerCase() === 'mermaid';
  return `<pre${mermaid ? ' data-mermaid-source="true"' : ''}>${language ? `<div class="code-language">${escape(language)}</div>` : ''}<code>${escape(token.content.replace(/\n$/, ''))}</code></pre>`;
};

function destination(path: string): string {
  try {
    return decodeURI(path);
  } catch {
    return path;
  }
}
function fileAction(path: string, label: string): string | null {
  const file = parseDocumentLink(destination(path));
  if (!file) return null;
  return `<button type="button" class="md-file-link" data-document-path="${escape(file.path)}"${file.line ? ` data-document-line="${file.line}"` : ''}>${label}</button>`;
}

parser.renderer.rules.code_inline = (tokens, index) => {
  const value = tokens[index].content;
  const file = parseDocumentLink(value);
  return `<code class="md-ic"${file ? ` data-document-candidate="${escape(file.path)}"${file.line ? ` data-document-line="${file.line}"` : ''}` : ''}>${escape(value)}</code>`;
};
parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  const token = tokens[index];
  const action = fileAction(String(token.attrGet('href') || ''), '');
  if (action) {
    token.tag = 'button';
    const node = document.createElement('template');
    node.innerHTML = action;
    token.attrs = [...node.content.firstElementChild!.attributes].map((attr) => [attr.name, attr.value]);
    // The matching closing token belongs to this inline token list.
    let depth = 1;
    for (let end = index + 1; end < tokens.length; end++) {
      if (tokens[end].type === 'link_open') depth++;
      if (tokens[end].type === 'link_close' && --depth === 0) {
        tokens[end].tag = 'button';
        break;
      }
    }
  } else {
    token.attrSet('class', 'md-link');
    token.attrSet('target', '_blank');
    token.attrSet('rel', 'noopener noreferrer');
  }
  return renderer.renderToken(tokens, index, options);
};
parser.renderer.rules.image = (tokens, index, options, env, renderer) => {
  const token = tokens[index];
  const source = String(token.attrGet('src') || '');
  const file = parseDocumentLink(destination(source));
  if (file) {
    token.attrSet('data-document-image', file.path);
    token.attrs = token.attrs?.filter(([name]) => name !== 'src') || [];
  }
  token.attrSet('class', 'md-image');
  token.attrSet('loading', 'lazy');
  token.attrSet('referrerpolicy', 'no-referrer');
  token.attrSet('alt', renderer.renderInlineAsText(token.children || [], options, env));
  return renderer.renderToken(tokens, index, options);
};

// Keep a code span's pipe inside its cell even when the model omits GFM's
// escape. Fenced code is untouched; the Markdown parser owns all other syntax.
function tableCodePipes(text: string): string {
  let fence = '';
  let inTable = false;
  const lines = text.split('\n');
  const tableDelimiter = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$/;
  return lines
    .map((line, index) => {
      const marker = /^\s{0,3}(`{3,}|~{3,})/.exec(line)?.[1];
      if (marker) {
        if (!fence) fence = marker[0];
        else if (marker[0] === fence) fence = '';
        return line;
      }
      if (fence) return line;
      if (!inTable) {
        const next = lines[index + 1] || '';
        inTable = line.includes('|') && tableDelimiter.test(next);
      } else if (!line.includes('|')) {
        inTable = false;
      }
      if (!inTable || !line.includes('|')) return line;
      return line.replace(/(`+)(.*?)\1/g, (span) => span.replace(/(?<!\\)\|/g, '\\|'));
    })
    .join('\n');
}

export function markdownHtml(text: string, inline = false): string {
  const normalized = tableCodePipes(text.replace(/\r\n?/g, '\n'));
  return inline ? parser.renderInline(normalized) : parser.render(normalized);
}

export function renderMarkdownContent(container: HTMLElement, text: string): void {
  container.insertAdjacentHTML('beforeend', markdownHtml(text));
  const classes: Record<string, string> = {
    p: 'md-p',
    ul: 'md-list',
    ol: 'md-list',
    blockquote: 'md-quote',
    table: 'md-table',
  };
  for (const [tag, className] of Object.entries(classes))
    container.querySelectorAll(tag).forEach((el) => el.classList.add(className));
  container
    .querySelectorAll('h1,h2,h3,h4,h5,h6')
    .forEach((el) => el.classList.add('md-h', 'md-' + el.tagName.toLowerCase()));
  container.querySelectorAll('table:not(.md-table-wrap table)').forEach((table) => {
    const wrapper = document.createElement('div');
    wrapper.className = 'md-table-wrap';
    wrapper.dataset.scrollable = '';
    table.replaceWith(wrapper);
    wrapper.append(table);
  });
  container.querySelectorAll<HTMLElement>('pre[data-mermaid-source]').forEach((source) => {
    const code = source.querySelector('code')?.textContent || '';
    const svg = mermaidSvg(code);
    if (!svg) return;
    source.hidden = true;
    const wrapper = document.createElement('div');
    wrapper.className = 'md-mermaid';
    wrapper.dataset.sourceSha = simpleDigest(code);
    source.parentElement?.insertBefore(wrapper, source);
    wrapper.append(source, svg);
    void renderMermaidWithLibrary(wrapper, code, simpleDigest(code));
  });
}

async function renderMermaidWithLibrary(wrapper: HTMLElement, source: string, digest: string): Promise<void> {
  if (!wrapper.isConnected || wrapper.dataset.sourceSha !== digest || source.length > 8_000) return;
  try {
    const { default: mermaid } = await import('mermaid');
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      themeVariables: { primaryColor: 'white', lineColor: 'black' },
    });
    const rendered = await mermaid.render(`codey-mermaid-${digest}`, source);
    if (!wrapper.isConnected || wrapper.dataset.sourceSha !== digest) return;
    const holder = document.createElement('div');
    holder.innerHTML = rendered.svg;
    const svg = holder.firstElementChild;
    if (!(svg instanceof SVGElement)) return;
    svg.classList.add('md-mermaid-svg');
    wrapper.querySelector('.md-mermaid-svg')?.replaceWith(svg);
  } catch {
    // The source and deterministic local preview remain available. A bad
    // diagram cannot invalidate the rest of the message.
  }
}

function simpleDigest(value: string): string {
  let hash = 2166136261;
  for (const char of value) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  return (hash >>> 0).toString(16);
}

function mermaidSvg(source: string): SVGSVGElement | null {
  const lines = source
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length || source.length > 8_000) return null;
  const isSequence = /^sequenceDiagram$/i.test(lines[0]);
  const isFlowchart = /^(flowchart|graph)\s+(TD|TB|LR|RL|BT)$/i.test(lines[0]);
  if (!isSequence && !isFlowchart) return null;
  const edges: Array<[string, string, string]> = [];
  const labels = new Map<string, string>();
  if (isSequence) {
    for (const line of lines.slice(1)) {
      const match = /^([^:]+?)(?:-->>|->>)([^:]+?):\s*(.*)$/.exec(line);
      if (match) edges.push([match[1].trim(), match[2].trim(), match[3].trim()]);
    }
  } else {
    for (const line of lines.slice(1)) {
      const match = /^(.+?)\s*(?:-->|->>)\s*(.+?)(?:\s*:\s*(.*))?$/.exec(line);
      if (match) {
        const node = (raw: string): [string, string] => {
          const labelled = /^([^[(]+)[[(]([^\])]+)[\])]$/.exec(raw.trim());
          return labelled ? [labelled[1].trim(), labelled[2].trim()] : [raw.trim(), raw.trim()];
        };
        const from = node(match[1]);
        const to = node(match[2]);
        labels.set(from[0], from[1]);
        labels.set(to[0], to[1]);
        edges.push([from[0], to[0], (match[3] || '').trim()]);
      }
    }
  }
  if (!edges.length) return null;
  const nodes = [...new Set(edges.flatMap(([from, to]) => [from, to]))].slice(0, 32);
  if (nodes.length < 2) return null;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${Math.max(360, nodes.length * 150)} 180`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'Mermaid diagram');
  svg.classList.add('md-mermaid-svg');
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
  marker.setAttribute('id', `arrow-${simpleDigest(source)}`);
  marker.setAttribute('markerWidth', '8');
  marker.setAttribute('markerHeight', '8');
  marker.setAttribute('refX', '7');
  marker.setAttribute('refY', '4');
  marker.setAttribute('orient', 'auto');
  const tip = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  tip.setAttribute('d', 'M0,0 L8,4 L0,8 z');
  tip.setAttribute('fill', 'currentColor');
  marker.append(tip);
  defs.append(marker);
  svg.append(defs);
  const x = (node: string) => 80 + nodes.indexOf(node) * 140;
  for (const [from, to, label] of edges.slice(0, 32)) {
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', String(x(from) + 45));
    line.setAttribute('x2', String(x(to) + 45));
    line.setAttribute('y1', '80');
    line.setAttribute('y2', '80');
    line.setAttribute('stroke', 'currentColor');
    line.setAttribute('marker-end', `url(#${marker.id})`);
    svg.append(line);
    if (label) {
      const textNode = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      textNode.setAttribute('x', String((x(from) + x(to)) / 2 + 45));
      textNode.setAttribute('y', '62');
      textNode.setAttribute('text-anchor', 'middle');
      textNode.textContent = label.slice(0, 120);
      svg.append(textNode);
    }
  }
  for (const node of nodes) {
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', String(x(node)));
    rect.setAttribute('y', '60');
    rect.setAttribute('width', '90');
    rect.setAttribute('height', '40');
    rect.setAttribute('rx', '8');
    rect.setAttribute('fill', 'none');
    rect.setAttribute('stroke', 'currentColor');
    const textNode = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    textNode.setAttribute('x', String(x(node) + 45));
    textNode.setAttribute('y', '85');
    textNode.setAttribute('text-anchor', 'middle');
    textNode.textContent = (labels.get(node) || node).slice(0, 40);
    svg.append(rect, textNode);
  }
  return svg;
}
