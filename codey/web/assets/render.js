/* Codey render helpers: HTML escaping, minimal markdown, copy buttons,
   tool-line DOM builders. Zero-build asset module; pure helpers only --
   no index.html state, so no init()/deps are needed. */
(function () {
'use strict';

function escapeHtml(text) {
  return (text || '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

async function copyText(text) {
  const value = String(text || '');
  if (!value) return false;
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {}
  const area = document.createElement('textarea');
  area.value = value;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    area.remove();
  }
}
function addMessageCopyButton(div, text) {
  const value = String(text || '');
  if (!value) return;
  const btn = document.createElement('button');
  btn.className = 'msg-copy';
  btn.type = 'button';
  btn.title = 'Copy';
  btn.setAttribute('aria-label', 'Copy message');
  btn.innerHTML = '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15V7a2 2 0 0 1 2-2h8"/></svg>';
  btn.onclick = async (e) => {
    e.stopPropagation();
    const ok = await copyText(value);
    btn.classList.toggle('copied', ok);
    btn.title = ok ? 'Copied' : 'Could not copy';
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.title = 'Copy';
    }, 1200);
  };
  div.appendChild(btn);
}

function applyBold(segment) {
  return segment.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}
function renderInlineMd(text) {
  const escaped = escapeHtml(text);
  const re = /`([^`]+)`/g;
  let out = '';
  let last = 0;
  let m;
  while ((m = re.exec(escaped)) !== null) {
    out += applyBold(escaped.slice(last, m.index));
    out += `<code class="md-ic">${m[1]}</code>`;
    last = re.lastIndex;
  }
  out += applyBold(escaped.slice(last));
  return out;
}
function addCodeCopyButton(pre, text) {
  const value = String(text || '');
  const btn = document.createElement('button');
  btn.className = 'code-copy';
  btn.type = 'button';
  btn.title = 'Copy';
  btn.setAttribute('aria-label', 'Copy code');
  btn.textContent = 'Copy';
  btn.onclick = async (e) => {
    e.stopPropagation();
    const ok = await copyText(value);
    btn.classList.toggle('copied', ok);
    btn.textContent = ok ? 'Copied' : 'Copy';
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = 'Copy'; }, 1200);
  };
  pre.appendChild(btn);
}
function appendCodeBlock(container, code) {
  const pre = document.createElement('pre');
  pre.className = 'md-code';
  const el = document.createElement('code');
  el.textContent = code;
  pre.appendChild(el);
  addCodeCopyButton(pre, code);
  container.appendChild(pre);
}
function renderMarkdown(container, text) {
  const lines = String(text == null ? '' : text).split('\n');
  let listStack = [];
  const flushList = () => {
    if (listStack.length) container.appendChild(listStack[0].el);
    listStack = [];
  };
  const appendListItem = (line) => {
    const match = line.match(/^(\s*)([-*]|\d+[.)])\s+(.*)$/);
    if (!match) return false;
    const spaces = match[1].replace(/\t/g, '    ').length;
    let depth = Math.floor(spaces / 2);
    const tag = /^\d/.test(match[2]) ? 'ol' : 'ul';
    if (depth > listStack.length) depth = listStack.length;
    if (depth === 0 && listStack[0] && listStack[0].tag !== tag) flushList();
    while (listStack.length > depth + 1) listStack.pop();
    if (!listStack[depth] || listStack[depth].tag !== tag) {
      if (depth > 0 && (!listStack[depth - 1] || !listStack[depth - 1].lastLi)) depth = 0;
      if (depth === 0 && listStack[0] && listStack[0].tag !== tag) flushList();
      const next = document.createElement(tag);
      next.className = 'md-list';
      if (depth > 0) {
        const parent = listStack[depth - 1];
        parent.lastLi.appendChild(next);
      }
      listStack = listStack.slice(0, depth);
      listStack[depth] = { el: next, tag, lastLi: null };
    }
    const li = document.createElement('li');
    li.innerHTML = renderInlineMd(match[3]);
    listStack[depth].el.appendChild(li);
    listStack[depth].lastLi = li;
    return true;
  };
  const isBreak = (line) => (
    /^\s*```/.test(line) || /^#{1,6}\s+/.test(line)
    || /^\s*>\s?/.test(line)
    || /^\s*[-*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)
  );
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flushList();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      appendCodeBlock(container, buf.join('\n'));
      continue;
    }
    if (!line.trim()) { flushList(); i++; continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushList();
      const el = document.createElement('div');
      el.className = 'md-h md-h' + Math.min(6, heading[1].length);
      el.innerHTML = renderInlineMd(heading[2]);
      container.appendChild(el);
      i++;
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      flushList();
      const quote = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      const el = document.createElement('blockquote');
      el.className = 'md-quote';
      el.innerHTML = renderInlineMd(quote.join('\n'));
      container.appendChild(el);
      continue;
    }
    if (appendListItem(line)) {
      i++;
      continue;
    }
    flushList();
    const para = [];
    while (i < lines.length && lines[i].trim() && !isBreak(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    const p = document.createElement('p');
    p.className = 'md-p';
    p.innerHTML = renderInlineMd(para.join('\n'));
    container.appendChild(p);
  }
  flushList();
}

// ============================ tool line rendering ============================
const FOLDABLE_TOOL_KINDS = new Set(['read', 'ls', 'search', 'references']);
function toolRowEl(m, compact) {
  const row = document.createElement('div');
  row.className = 'tool-line' + (compact ? ' compact' : '');
  if (m.pending) row.classList.add('pending');
  if (!compact) {
    const dot = document.createElement('span'); dot.className = 'tl-dot'; dot.textContent = '·';
    const kind = document.createElement('span'); kind.className = 'tl-kind'; kind.textContent = m.kind || '';
    row.append(dot, kind);
  }
  const path = document.createElement('span'); path.className = 'tl-path'; path.textContent = m.path || '';
  const arrow = document.createElement('span'); arrow.className = 'tl-arrow'; arrow.textContent = '→';
  const result = document.createElement('span'); result.className = 'tl-result' + (m.error ? ' err' : ''); result.textContent = m.pending ? (m.activity || m.result || 'Working') : (m.result || '');
  row.append(path, arrow, result);
  return row;
}
function foldCountLabel(kind, n) {
  const nouns = {
    read: ['file', 'files'],
    ls: ['dir', 'dirs'],
    search: ['search', 'searches'],
    references: ['lookup', 'lookups'],
  };
  const pair = nouns[kind] || ['call', 'calls'];
  return n + ' ' + (n === 1 ? pair[0] : pair[1]);
}
function createToolGroup(kind) {
  const group = document.createElement('div');
  group.className = 'tool-group collapsed';
  group.dataset.foldkind = kind;
  const summary = document.createElement('div');
  summary.className = 'tool-group-summary';
  const dot = document.createElement('span'); dot.className = 'tl-dot'; dot.textContent = '·';
  const kindEl = document.createElement('span'); kindEl.className = 'tg-kind'; kindEl.textContent = kind;
  const count = document.createElement('span'); count.className = 'tg-count';
  const chev = document.createElement('span'); chev.className = 'tg-chevron'; chev.textContent = '▸';
  summary.append(dot, kindEl, count, chev);
  const body = document.createElement('div');
  body.className = 'tool-group-body';
  summary.onclick = () => group.classList.toggle('collapsed');
  group.append(summary, body);
  return group;
}
function standaloneToolEl(m) {
  const div = document.createElement('div');
  div.className = 'msg tool';
  if (FOLDABLE_TOOL_KINDS.has(m.kind) && !m.error && !m.pending) {
    div.dataset.foldkind = m.kind;
    div.dataset.toolPath = m.path || '';
    div.dataset.toolResult = m.result || '';
  }
  div.appendChild(toolRowEl(m, false));
  return div;
}
function appendToToolGroup(group, m) {
  const body = group.querySelector('.tool-group-body');
  body.appendChild(toolRowEl(m, true));
  group.querySelector('.tg-count').textContent = foldCountLabel(m.kind, body.children.length);
}
function appendOrFoldTool(chat, m) {
  const last = chat.lastElementChild;
  if (last && last.classList && last.classList.contains('tool-group') && last.dataset.foldkind === m.kind) {
    appendToToolGroup(last, m);
    return;
  }
  if (last && last.classList && last.classList.contains('msg') && last.classList.contains('tool') && last.dataset.foldkind === m.kind) {
    const group = createToolGroup(m.kind);
    appendToToolGroup(group, {
      kind: m.kind,
      path: last.dataset.toolPath || '',
      result: last.dataset.toolResult || '',
      error: false,
    });
    appendToToolGroup(group, m);
    last.replaceWith(group);
    return;
  }
  chat.appendChild(standaloneToolEl(m));
}

window.CodeyRender = {
  escapeHtml,
  copyText,
  addMessageCopyButton,
  applyBold,
  renderInlineMd,
  addCodeCopyButton,
  appendCodeBlock,
  renderMarkdown,
  FOLDABLE_TOOL_KINDS,
  toolRowEl,
  foldCountLabel,
  createToolGroup,
  standaloneToolEl,
  appendToToolGroup,
  appendOrFoldTool,
};
})();
