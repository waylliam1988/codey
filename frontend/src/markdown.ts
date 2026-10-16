import MarkdownIt from 'markdown-it';
import taskLists from 'markdown-it-task-lists';
import { parseDocumentLink } from './document_links';

const parser = new MarkdownIt({ html: false, linkify: false, breaks: false }).use(taskLists);
const escape = parser.utils.escapeHtml;
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
  return `<pre>${language ? `<div class="code-language">${escape(language)}</div>` : ''}<code>${escape(token.content.replace(/\n$/, ''))}</code></pre>`;
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
}
