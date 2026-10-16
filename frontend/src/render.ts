import { markdownHtml, renderMarkdownContent } from './markdown';
import { runtime } from './runtime';
export interface ToolPreview {
  additions?: number;
  deletions?: number;
  diff?: string;
  diff_truncated?: boolean;
  diff_unavailable?: boolean;
  line_start?: number;
  line_end?: number;
  query?: string;
  matches?: number;
  has_more?: boolean;
}
export interface ToolDisplay {
  preview?: ToolPreview;
  kind?: string;
  path?: string;
  command?: string;
  result?: string;
  activity?: string;
  toolKey?: string;
  error?: boolean;
  pending?: boolean;
  turn?: string | number;
  outputTruncated?: boolean;
}
/* Codey render helpers: HTML escaping, minimal markdown, copy buttons,
   tool-line DOM builders. Zero-build asset module; pure helpers only --
   no index.html state, so no init()/deps are needed. */
const renderApi = (function () {
  'use strict';

  function escapeHtml(text: unknown): string {
    return String(text || '').replace(
      /[&<>"']/g,
      (ch: string) =>
        (
          ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
          }) as Record<string, string>
        )[ch],
    );
  }

  async function copyText(text: unknown) {
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
  function addMessageCopyButton(div: HTMLElement, text: unknown): void {
    const value = String(text || '');
    if (!value) return;
    const btn = document.createElement('button');
    btn.className = 'msg-copy';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Copy message');
    const icon =
      '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15V7a2 2 0 0 1 2-2h8"/></svg>';
    btn.innerHTML = icon;
    const feedback = document.createElement('span');
    feedback.className = 'copy-feedback';
    feedback.setAttribute('role', 'status');
    let reset: ReturnType<typeof setTimeout> | null = null,
      copying = false;
    btn.onclick = async (e: MouseEvent) => {
      e.stopPropagation();
      if (copying) return;
      const keyboardFocus = document.activeElement === btn && btn.matches(':focus-visible');
      copying = true;
      if (reset) clearTimeout(reset);
      btn.classList.remove('copied', 'copy-failed');
      btn.classList.add('copy-pending');
      btn.setAttribute('aria-disabled', 'true');
      btn.setAttribute('aria-busy', 'true');
      feedback.textContent = '';
      let ok = false;
      try {
        ok = await copyText(value);
      } catch {}
      if (keyboardFocus && btn.isConnected && document.activeElement === document.body)
        btn.focus({ preventScroll: true });
      copying = false;
      btn.classList.remove('copy-pending');
      btn.removeAttribute('aria-disabled');
      btn.removeAttribute('aria-busy');
      btn.classList.toggle('copied', ok);
      btn.classList.toggle('copy-failed', !ok);
      btn.setAttribute('aria-label', ok ? 'Copied' : 'Could not copy');
      btn.innerHTML = ok ? '<svg class="copy-check" viewBox="0 0 24 24"><path d="m5 12 4 4 10-10"/></svg>' : icon;
      feedback.textContent = ok ? '' : 'Could not copy';
      reset = setTimeout(
        () => {
          btn.classList.remove('copied', 'copy-failed');
          btn.setAttribute('aria-label', 'Copy message');
          btn.innerHTML = icon;
          feedback.textContent = '';
        },
        ok ? 1200 : 4000,
      );
    };
    div.append(btn, feedback);
  }

  function renderInlineMd(text: string): string {
    return markdownHtml(text, true);
  }
  function addCodeCopyButton(pre: HTMLElement, text: unknown): void {
    const value = String(text || '');
    const btn = document.createElement('button');
    btn.className = 'code-copy';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Copy code');
    btn.textContent = 'Copy';
    btn.onclick = async (e: MouseEvent) => {
      e.stopPropagation();
      const ok = await copyText(value);
      btn.classList.toggle('copied', ok);
      btn.textContent = ok ? 'Copied' : 'Could not copy';
      btn.setAttribute('aria-label', ok ? 'Copied' : 'Could not copy');
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.textContent = 'Copy';
        btn.setAttribute('aria-label', 'Copy code');
      }, 1200);
    };
    pre.appendChild(btn);
  }
  function renderMarkdown(container: HTMLElement, text: unknown): void {
    const value = String(text ?? '');
    const content = document.createElement('div');
    try {
      renderMarkdownContent(content, value);
    } catch (error) {
      const boundary = document.createElement('div');
      boundary.className = 'md-render-error';
      const label = document.createElement('div');
      label.textContent = 'This content could not be displayed.';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'link-btn';
      retry.textContent = 'Render again';
      retry.onclick = () => {
        const next = document.createElement('div');
        renderMarkdown(next, value);
        boundary.replaceWith(...next.childNodes);
      };
      const details = document.createElement('details'),
        head = document.createElement('summary'),
        source = document.createElement('pre');
      head.textContent = 'Source and details';
      source.textContent = value + '\n\n' + (error instanceof Error ? error.message : 'Render failed');
      details.append(head, source);
      boundary.append(label, retry, details);
      container.append(boundary);
      return;
    }
    content.querySelectorAll('pre:not(.md-code)').forEach((node) => {
      const pre = node as HTMLElement;
      pre.classList.add('md-code');
      pre.dataset.scrollable = '';
      const code = pre.querySelector('code');
      if (code) addCodeCopyButton(pre, code.textContent);
    });
    container.append(...content.childNodes);
  }

  // ============================ tool line rendering ============================
  const FOLDABLE_TOOL_KINDS = new Set(['read', 'ls', 'search', 'references']);
  function toolRowEl(m: ToolDisplay, compact: boolean) {
    const row = document.createElement('div');
    row.className = 'tool-line' + (compact ? ' compact' : '');
    if (m.error) row.classList.add('error');
    if (m.turn) row.title = 'Step ' + m.turn;
    if (m.pending) row.classList.add('pending');
    if (!compact) {
      const dot = document.createElement('span');
      dot.className = 'tl-dot';
      dot.textContent = '·';
      const kind = document.createElement('span');
      kind.className = 'tl-kind';
      kind.textContent = m.kind || '';
      row.append(dot, kind);
    }
    const path = document.createElement('span');
    path.className = 'tl-path';
    path.textContent = m.path || m.command || '';
    path.title = path.textContent;
    const arrow = document.createElement('span');
    arrow.className = 'tl-arrow';
    arrow.textContent = '→';
    const result = document.createElement('span');
    result.className = 'tl-result' + (m.error ? ' err' : '');
    result.textContent = m.pending
      ? m.activity || m.result || 'Working'
      : m.error
        ? /command_not_allowed/.test(m.result || '')
          ? 'This command requires approval'
          : 'Tool failed'
        : m.result
          ? 'Result available'
          : '';
    if (!m.pending && !m.error && m.preview) {
      const p = m.preview;
      if (p.additions !== undefined && p.deletions !== undefined)
        result.textContent = `+${p.additions} −${p.deletions}`;
      else if (p.line_start !== undefined && p.line_end !== undefined)
        result.textContent = `lines ${p.line_start}–${p.line_end}`;
      else if (p.matches !== undefined) {
        path.textContent = p.query || path.textContent;
        result.textContent = `${p.matches}${p.has_more ? '+' : ''} matches`;
      } else if (p.diff_unavailable) result.textContent = 'Changed · preview unavailable';
    }
    row.append(path, arrow, result);
    return row;
  }

  const renderedBlocks = new WeakMap<HTMLElement, string[]>();
  const pendingUpdates = new WeakMap<HTMLElement, string>();
  const waitingForSelection = new WeakSet<HTMLElement>();
  function updateMarkdown(container: HTMLElement, text: string): void {
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && selection.anchorNode && container.contains(selection.anchorNode)) {
      pendingUpdates.set(container, text);
      if (waitingForSelection.has(container)) return;
      waitingForSelection.add(container);
      const resume = () => {
        if (!window.getSelection() || window.getSelection()?.isCollapsed) {
          document.removeEventListener('selectionchange', resume);
          waitingForSelection.delete(container);
          const pending = pendingUpdates.get(container);
          pendingUpdates.delete(container);
          if (pending !== undefined && container.isConnected) updateMarkdown(container, pending);
        }
      };
      document.addEventListener('selectionchange', resume);
      return;
    }
    const next = document.createElement('div');
    renderMarkdown(next, text);
    const signatures = [...next.children].map((node) => node.outerHTML);
    const previous = renderedBlocks.get(container) || [];
    let common = 0;
    while (common < signatures.length && common < previous.length && signatures[common] === previous[common]) common++;
    while (container.children.length > common) container.lastElementChild!.remove();
    [...next.children].slice(common).forEach((node) => container.append(node));
    renderedBlocks.set(container, signatures);
  }
  function toolDetails(m: ToolDisplay, compact: boolean): HTMLElement {
    const row = toolRowEl(m, compact);
    if (m.pending || !m.result) return row;
    const details = document.createElement('details');
    details.className = 'tool-disclosure';
    if (m.toolKey) details.dataset.toolKey = m.toolKey;
    const summary = document.createElement('summary');
    summary.append(row);
    const panel = document.createElement('div');
    panel.className = 'tool-result-panel';
    const output = document.createElement('pre');
    output.dataset.scrollable = '';
    const displayed = m.preview?.diff || m.result;
    output.textContent = displayed;
    if (m.preview?.diff) output.classList.add('tool-diff');
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'code-copy';
    copy.textContent = m.preview?.diff ? 'Copy diff' : 'Copy retained result';
    copy.onclick = async () => {
      copy.textContent = (await copyText(displayed)) ? 'Copied' : 'Could not copy';
    };
    panel.append(output, copy);
    details.append(summary, panel);
    if (m.outputTruncated) {
      const note = document.createElement('div');
      note.className = 'tool-result-note';
      note.textContent = 'Output was truncated. Copy includes retained result only.';
      panel.append(note);
    }
    if (m.preview?.diff_truncated) {
      const note = document.createElement('div');
      note.className = 'tool-result-note';
      note.textContent = 'Diff preview was truncated. Copy includes the displayed diff only.';
      panel.append(note);
    }
    return details;
  }
  function foldCountLabel(kind: string, n: number): string {
    const nouns: Record<string, [string, string]> = {
      read: ['file', 'files'],
      ls: ['dir', 'dirs'],
      search: ['search', 'searches'],
      references: ['lookup', 'lookups'],
    };
    const pair = nouns[kind] || ['call', 'calls'];
    return n + ' ' + (n === 1 ? pair[0] : pair[1]);
  }
  function createToolGroup(kind: string) {
    const group = document.createElement('div');
    group.className = 'tool-group collapsed';
    group.dataset.foldkind = kind;
    const summary = document.createElement('button');
    summary.type = 'button';
    summary.setAttribute('aria-expanded', 'false');
    summary.className = 'tool-group-summary';
    const dot = document.createElement('span');
    dot.className = 'tl-dot';
    dot.textContent = '·';
    const kindEl = document.createElement('span');
    kindEl.className = 'tg-kind';
    kindEl.textContent = kind;
    const count = document.createElement('span');
    count.className = 'tg-count';
    const chev = document.createElement('span');
    chev.className = 'tg-chevron';
    chev.textContent = '▸';
    summary.append(dot, kindEl, count, chev);
    const body = document.createElement('div');
    body.className = 'tool-group-body';
    summary.onclick = () => {
      group.classList.toggle('collapsed');
      summary.setAttribute('aria-expanded', String(!group.classList.contains('collapsed')));
    };
    group.append(summary, body);
    return group;
  }
  function standaloneToolEl(m: ToolDisplay) {
    const div = document.createElement('div');
    div.className = 'msg tool';
    if (m.toolKey) div.dataset.toolKey = m.toolKey;
    if (FOLDABLE_TOOL_KINDS.has(m.kind || '') && !m.error && !m.pending) {
      div.dataset.foldkind = m.kind || '';
      div.dataset.toolPath = m.path || '';
      div.dataset.toolResult = m.result || '';
    }
    div.appendChild(toolDetails(m, false));
    return div;
  }
  function appendToToolGroup(group: HTMLElement, m: ToolDisplay) {
    const body = group.querySelector<HTMLElement>('.tool-group-body');
    const count = group.querySelector<HTMLElement>('.tg-count');
    if (!body || !count) throw new Error('Invalid tool group');
    const row = toolDetails(m, true);
    if (m.toolKey) row.dataset.toolKey = m.toolKey;
    body.appendChild(row);
    count.textContent = foldCountLabel(m.kind || '', body.children.length);
  }
  function appendOrFoldTool(chat: HTMLElement, m: ToolDisplay) {
    const last = chat.lastElementChild;
    if (last instanceof HTMLElement && last.classList.contains('tool-group') && last.dataset.foldkind === m.kind) {
      appendToToolGroup(last, m);
      return;
    }
    if (
      last instanceof HTMLElement &&
      last.classList.contains('msg') &&
      last.classList.contains('tool') &&
      last.dataset.foldkind === m.kind
    ) {
      const group = createToolGroup(m.kind || '');
      appendToToolGroup(group, {
        kind: m.kind,
        path: last.dataset.toolPath || '',
        result: last.dataset.toolResult || '',
        toolKey: last.dataset.toolKey || '',
        error: false,
      });
      appendToToolGroup(group, m);
      last.replaceWith(group);
      return;
    }
    chat.appendChild(standaloneToolEl(m));
  }

  // Receipt helpers: Codey 0.5 receipts are structured
  // (display / work / verification / integrity), never flat text.
  function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
  }
  function receiptSummary(receipt: unknown): string {
    const display = isRecord(receipt) && isRecord(receipt.display) ? receipt.display : null;
    return display?.summary ? String(display.summary) : '';
  }

  function receiptChangedCount(receipt: unknown, changes?: unknown): number {
    const work = isRecord(receipt) && isRecord(receipt.work) ? receipt.work : null;
    const count = (value: unknown): number => {
      const parsed = typeof value === 'number' || typeof value === 'string' ? Number(value) : 0;
      return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
    };
    const fromReceipt = count(work?.changed_count);
    return fromReceipt > 0 ? fromReceipt : count(isRecord(changes) ? changes.changed_count : undefined);
  }

  function messageCopyText(m: unknown): string {
    return isRecord(m) && typeof m.text === 'string' ? m.text : '';
  }

  return {
    escapeHtml,
    receiptSummary,
    receiptChangedCount,
    messageCopyText,
    copyText,
    addMessageCopyButton,
    renderInlineMd,
    addCodeCopyButton,
    renderMarkdown,
    updateMarkdown,
    FOLDABLE_TOOL_KINDS,
    toolRowEl,
    foldCountLabel,
    createToolGroup,
    standaloneToolEl,
    appendToToolGroup,
    appendOrFoldTool,
  };
})();

export type RenderApi = typeof renderApi;
runtime.Render = renderApi;
