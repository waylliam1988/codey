import { runtime } from './runtime';
import { createIconSvg } from './icon_button';
import type { ChangeFilePreview, ChangesData, RestoreChangesResult } from './types/domain';
export interface ChangesDrawerDeps {
  $(id: string): HTMLElement;
  fetchChanges(project: string): Promise<ChangesData>;
  fetchChangeFile(project: string, path: string): Promise<ChangeFilePreview>;
  restoreChanges(project: string): Promise<RestoreChangesResult>;
  escapeHtml(text: string): string;
  closeOtherDrawers?: (name: string) => void;
}
/* Codey changes drawer: per-file diff list with fold/unfold rendering.
   Zero-build asset module; index.html injects dependencies via init() at boot. */
(function () {
  'use strict';

  interface ChangeData extends ChangesData {
    mode?: string;
    error?: string;
    conflicts?: string[];
  }
  interface DiffChunk {
    path: string;
    lines: string[];
  }
  interface ChangeNode extends HTMLElement {
    _diffContent?: string;
  }
  interface ChangesDeps extends ChangesDrawerDeps {
    $<T extends HTMLElement = HTMLElement>(id: string): T;
    fetchChanges(project: string): Promise<ChangeData>;
    fetchChangeFile(project: string, path: string): Promise<ChangeFilePreview>;
    restoreChanges(project: string): Promise<RestoreChangesResult>;
    escapeHtml(text: string): string;
    closeOtherDrawers?: (name: string) => void;
  }
  let deps: ChangesDeps;
  let generation = 0;
  let activeProject = '';
  let selectedPath = '';
  let lastData: ChangeData | null = null;
  let loading = false,
    restoring = false,
    fresh = false,
    failed = false;
  let pathFocus: Element | null = null;
  let previewSequence = 0;

  function init(nextDeps: ChangesDeps): void {
    deps = nextDeps;
    const copy = $<HTMLButtonElement>('changes-copy');
    copy.replaceChildren(createIconSvg('copy'));
    copy.title = 'Copy diff';
    copy.setAttribute('aria-label', 'Copy diff');
    $('changes-refresh').onclick = () => {
      if (activeProject && !loading && !restoring) loadChangesDrawer(activeProject);
    };
    $('changes-restore').onclick = restore;
    $('changes-copy').onclick = copyDiff;
    updateControls();
  }

  function $<T extends HTMLElement = HTMLElement>(id: string): T {
    return deps.$<T>(id);
  }

  function fetchChanges(project: string): Promise<ChangeData> {
    return deps.fetchChanges(project);
  }

  function escapeHtml(text: string): string {
    return deps.escapeHtml(text);
  }

  function updateControls() {
    const refresh = $<HTMLButtonElement>('changes-refresh'),
      restoreButton = $<HTMLButtonElement>('changes-restore'),
      copy = $<HTMLButtonElement>('changes-copy');
    refresh.disabled = !activeProject || loading || restoring;
    $('changes-refresh').textContent = failed ? 'Retry' : 'Refresh';
    $('changes-restore').hidden = lastData?.mode === 'git';
    restoreButton.disabled = !fresh || loading || restoring || !lastData?.files?.length || lastData.mode === 'git';
    $('changes-restore').textContent = restoring ? 'Restoring' : 'Restore';
    copy.disabled = !lastData?.diff;
  }
  async function copyDiff() {
    if (!lastData?.diff) return;
    const project = activeProject,
      data = lastData;
    const ok = await runtime.Render.copyText(data.diff);
    if (activeProject !== project || lastData !== data) return;
    const copy = $<HTMLButtonElement>('changes-copy');
    copy.replaceChildren(createIconSvg('copy'));
    copy.title = ok ? 'Copied' : 'Could not copy';
    copy.setAttribute('aria-label', ok ? 'Copied' : 'Could not copy');
    setTimeout(() => {
      if (activeProject === project && lastData === data) {
        copy.title = 'Copy diff';
        copy.setAttribute('aria-label', 'Copy diff');
      }
    }, 1200);
  }
  async function openChangesDrawer(project: string, path = ''): Promise<void> {
    if (!project) return;
    if (deps.closeOtherDrawers) deps.closeOtherDrawers('changes');
    if (project !== activeProject) {
      lastData = null;
      fresh = false;
      restoring = false;
      $('changes-body').replaceChildren();
      const copy = $<HTMLButtonElement>('changes-copy');
      copy.replaceChildren(createIconSvg('copy'));
      copy.title = 'Copy diff';
      copy.setAttribute('aria-label', 'Copy diff');
    }
    activeProject = project;
    selectedPath = path;
    runtime.Workspace?.target('changes', { path });
    const scope = $('changes-scope');
    scope.textContent = runtime.UiState.pathName(project);
    scope.title = project;
    runtime.UiState.setDrawerOpen('changes-drawer', true);
    pathFocus = document.activeElement;
    await loadChangesDrawer(project);
  }
  async function loadChangesDrawer(project: string): Promise<boolean> {
    const request = ++generation;
    loading = true;
    fresh = false;
    failed = false;
    $('changes-subtitle').textContent = lastData ? 'Updating…' : 'Loading…';
    updateControls();
    if (!lastData)
      $('changes-body').innerHTML =
        '<div class="changes-empty"><span class="drawer-loading"><span class="spinner"></span><span>Reading changes...</span></span></div>';
    try {
      const data = await fetchChanges(project);
      if (request !== generation || project !== activeProject) return false;
      if (!data?.ok || !Array.isArray(data.files) || typeof data.diff !== 'string')
        throw new Error(data?.error || 'Invalid changes result');
      renderChangesDrawer(data);
      fresh = true;
      focusPath();
      return true;
    } catch (err) {
      if (request !== generation || project !== activeProject) return false;
      failed = true;
      $('changes-subtitle').textContent =
        'Could not update' + (lastData ? ' · Showing previous changes' : '') + ' · Retry';
      if (!lastData) {
        const error = document.createElement('div');
        error.className = 'changes-error';
        error.textContent = String(err);
        $('changes-body').replaceChildren(error);
      }
      return false;
    } finally {
      if (request === generation && project === activeProject) {
        loading = false;
        updateControls();
      }
    }
  }
  function closeChangesDrawer() {
    generation++;
    activeProject = '';
    selectedPath = '';
    pathFocus = null;
    lastData = null;
    loading = restoring = fresh = failed = false;
    updateControls();
    runtime.UiState.setDrawerOpen('changes-drawer', false);
  }
  function renderChangesDrawer(data: ChangeData): void {
    lastData = data;
    const body = $('changes-body'),
      scroll = body.scrollTop;
    const top = body.getBoundingClientRect().top;
    const anchor = Array.from(body.querySelectorAll<HTMLElement>('.diff-line, .change-file > button')).find(
      (node) => node.getBoundingClientRect().bottom > top,
    );
    const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
    const files = data.files,
      chunks = parseDiffChunks(data.diff);
    const mode = data.mode === 'git' ? 'Working tree' : 'Snapshot';
    $('changes-subtitle').textContent =
      `${files.length} file${files.length === 1 ? '' : 's'} changed · ${mode}${data.truncated ? ' · Diff list truncated; open a file to load its file diff' : ''}`;
    const previous = new Map(
      Array.from(body.querySelectorAll<ChangeNode>('.change-file')).map((node) => [node.dataset.path, node]),
    );
    const nodes = files.map((file) => {
      const node = previous.get(file.path) || changeFileNode(file);
      updateFileNode(node, file, chunks);
      return node;
    });
    const kept = new Set<ChangeNode>(nodes);
    for (const node of Array.from(body.children).filter((child): child is ChangeNode => child instanceof HTMLElement))
      if (!kept.has(node)) {
        if (node.contains(document.activeElement)) $('changes-close').focus({ preventScroll: true });
        node.remove();
      }
    nodes.forEach((node, index) => {
      if (body.children[index] !== node) body.insertBefore(node, body.children[index] || null);
    });
    if (data.mode === 'git') {
      for (const node of nodes) {
        const pre = node.querySelector<HTMLPreElement>('pre');
        if (pre && !pre.hidden) void loadFilePreview(node, node.dataset.path || '');
      }
    }
    if (!files.length) body.innerHTML = '<div class="changes-empty">No changes</div>';
    body.scrollTop = anchor?.isConnected ? body.scrollTop + anchor.getBoundingClientRect().top - top - offset : scroll;
  }
  async function restore() {
    if (!activeProject || !fresh || loading || restoring || !lastData?.files.length || lastData.mode === 'git') return;
    const project = activeProject;
    let request = generation;
    const current = () => request === generation && project === activeProject;
    restoring = true;
    updateControls();
    try {
      const data = await deps.restoreChanges(project);
      if (!current()) return;
      if (!data.ok) {
        fresh = false;
        failed = true;
        $('changes-subtitle').textContent = data.conflicts?.length
          ? `Conflict: ${data.conflicts.join(', ')}`
          : data.error || 'Restore failed';
        return;
      }
      const refresh = loadChangesDrawer(project);
      request = generation;
      const updated = await refresh;
      if (!current()) return;
      if (!updated) $('changes-subtitle').textContent = 'Restored · ' + $('changes-subtitle').textContent;
      else $('changes-subtitle').textContent += ' · Restored';
    } catch (err) {
      if (current()) {
        fresh = false;
        failed = true;
        $('changes-subtitle').textContent = 'Could not confirm restore · ' + String(err);
      }
    } finally {
      if (current()) {
        restoring = false;
        updateControls();
      }
    }
  }

  function parseDiffChunks(diff: string): DiffChunk[] {
    const lines = (diff || '').split('\n');
    const chunks: DiffChunk[] = [];
    let current: DiffChunk | null = null;
    function push(): void {
      if (current) chunks.push(current);
    }
    function start(path: string): void {
      push();
      current = { path: (path || '').replace(/^b\//, ''), lines: [] };
    }
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const next = lines[i + 1] || '';
      if (line.startsWith('diff --git ')) {
        const m = line.match(/\sb\/(.+)$/);
        start(m ? m[1] : '');
      } else if (
        line.startsWith('--- /dev/null') &&
        next.startsWith('+++ ') &&
        (!current || !current.lines.some((l) => l.startsWith('diff --git ')))
      ) {
        start(next.replace(/^\+\+\+\s+/, '').replace(/^b\//, ''));
      }
      if (!current && line.trim()) current = { path: '', lines: [] };
      if (current) current.lines.push(line);
    }
    push();
    return chunks;
  }

  function chunksForPath(chunks: DiffChunk[], path: string): DiffChunk[] {
    const target = (path || '').replace(/\\/g, '/');
    return chunks.filter((c) => c.path === target || c.path.endsWith('/' + target));
  }

  function changeFileNode(file: ChangesData['files'][number]): ChangeNode {
    const wrap = document.createElement('div');
    wrap.className = 'change-file';
    wrap.dataset.path = file.path || '';
    const btn = document.createElement('button');
    btn.type = 'button';
    const status = document.createElement('span');
    status.className = 'change-status';
    status.textContent = file.status || 'M';
    const path = document.createElement('span');
    path.className = 'change-path';
    path.title = file.path || '';
    path.textContent = file.path || '';
    const add = document.createElement('span');
    add.className = 'change-stat';
    add.textContent = `+${file.additions || 0}`;
    const del = document.createElement('span');
    del.className = 'change-stat';
    del.textContent = `-${file.deletions || 0}`;
    btn.append(status, path, add, del);
    const pre = document.createElement('pre');
    pre.className = 'diff-pre';
    pre.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    btn.onclick = () => toggleFileNode(wrap, file.path || '', pre, btn);
    wrap.append(btn, pre);
    return wrap as ChangeNode;
  }

  function updateFileNode(node: ChangeNode, file: ChangesData['files'][number], chunks: DiffChunk[]): void {
    const status = node.querySelector<HTMLElement>('.change-status');
    if (status) status.textContent = file.status || 'M';
    const stats = node.querySelectorAll<HTMLElement>('.change-stat');
    stats[0].textContent = `+${file.additions || 0}`;
    stats[1].textContent = `-${file.deletions || 0}`;
    const matching = chunksForPath(chunks, file.path || '');
    const statusMessage: Record<string, string> = {
      binary: '(binary file; text diff unavailable)',
      too_large: '(file is too large for inline preview)',
      read_error: '(file could not be read)',
      symlink: '(symbolic link preview unavailable)',
    };
    const content =
      file.preview_status && file.preview_status !== 'text'
        ? `<span class="diff-line meta">${escapeHtml(statusMessage[file.preview_status] || 'File preview unavailable')}</span>`
        : matching.length
          ? matching.map((chunk) => renderDiffLines(chunk.lines)).join('')
          : '<span class="diff-line meta">Open to load file diff</span>';
    const pre = node.querySelector<HTMLPreElement>('pre');
    if (!pre) return;
    const keepDisplayedContent = lastData?.mode === 'git' && !pre.hidden && pre.childNodes.length > 0;
    delete node.dataset.previewLoading;
    delete node.dataset.previewToken;
    delete node.dataset.previewLoaded;
    if (!keepDisplayedContent && node._diffContent !== content) {
      pre.innerHTML = content;
      node._diffContent = content;
    }
  }

  async function loadFilePreview(node: ChangeNode, path: string): Promise<void> {
    if (lastData?.mode !== 'git' || !activeProject) return;
    if (node.dataset.previewLoading === 'true') return;
    const project = activeProject;
    const requestGeneration = generation;
    if (node.dataset.previewLoaded === String(requestGeneration)) return;
    const token = `${requestGeneration}:${++previewSequence}`;
    node.dataset.previewLoading = 'true';
    node.dataset.previewToken = token;
    try {
      const preview = await deps.fetchChangeFile(project, path);
      if (
        !node.isConnected ||
        activeProject !== project ||
        generation !== requestGeneration ||
        node.dataset.previewToken !== token
      )
        return;
      const pre = node.querySelector<HTMLPreElement>('pre');
      if (!pre) return;
      const message: Record<string, string> = {
        binary: '(binary file; text diff unavailable)',
        too_large: '(file is too large for inline preview)',
        read_error: '(file could not be read)',
        symlink: '(symbolic link preview unavailable)',
      };
      if (preview.status === 'text' && preview.diff) {
        const chunks = parseDiffChunks(preview.diff);
        pre.innerHTML = chunks.map((chunk) => renderDiffLines(chunk.lines)).join('');
        if (preview.truncated) {
          pre.innerHTML += '<span class="diff-line meta">(file diff truncated)</span>';
        }
      } else {
        pre.innerHTML = `<span class="diff-line meta">${escapeHtml(message[preview.status] || preview.error || '(no text diff available)')}</span>`;
      }
      node._diffContent = pre.innerHTML;
      node.dataset.previewLoaded = String(requestGeneration);
    } catch (error) {
      if (activeProject !== project || generation !== requestGeneration || node.dataset.previewToken !== token) return;
      const pre = node.querySelector<HTMLPreElement>('pre');
      if (pre) pre.innerHTML = `<span class="diff-line meta">${escapeHtml(String(error))}</span>`;
    } finally {
      if (node.dataset.previewToken === token) {
        delete node.dataset.previewLoading;
        delete node.dataset.previewToken;
      }
    }
  }

  function toggleFileNode(node: ChangeNode, path: string, pre: HTMLPreElement, button: HTMLButtonElement): void {
    pre.hidden = !pre.hidden;
    button.setAttribute('aria-expanded', String(!pre.hidden));
    if (!pre.hidden) ensureFileNodeOpen(node, path, pre, button);
  }

  function ensureFileNodeOpen(node: ChangeNode, path: string, pre: HTMLPreElement, button: HTMLButtonElement): void {
    pre.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    if (lastData?.mode === 'git') void loadFilePreview(node, path);
  }

  function focusPath() {
    if (!selectedPath) return;
    const normalize = (path: string): string => String(path).replace(/\\/g, '/');
    const target = normalize(selectedPath),
      requested = selectedPath;
    selectedPath = '';
    const row = Array.from($('changes-body').querySelectorAll<ChangeNode>('.change-file')).find((el) => {
      const path = normalize(el.dataset.path || '');
      return path === target || normalize(activeProject + '/' + path) === target;
    });
    if (!row) {
      $('changes-subtitle').textContent += ' · No change for ' + requested;
      return;
    }
    const pre = row.querySelector<HTMLPreElement>('pre'),
      button = row.querySelector<HTMLButtonElement>('button');
    if (!pre || !button) return;
    ensureFileNodeOpen(row, row.dataset.path || requested, pre, button);
    if (document.activeElement === pathFocus) {
      row.scrollIntoView({ block: 'nearest' });
      button.focus({ preventScroll: true });
    }
    pathFocus = null;
  }

  function renderDiffLines(lines: string[]): string {
    let oldLine = 0;
    let newLine = 0;
    const out: string[] = [];
    for (const line of lines || []) {
      if (line.startsWith('@@')) {
        const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        if (m) {
          oldLine = Number(m[1]);
          newLine = Number(m[2]);
        }
        continue;
      }
      if (
        line.startsWith('diff --git') ||
        line.startsWith('index ') ||
        line.startsWith('---') ||
        line.startsWith('+++')
      ) {
        continue;
      }
      if (line.startsWith('+')) {
        out.push(
          `<span class="diff-line add"><span class="ln">${newLine || ''}</span><span class="mark">+</span>${escapeHtml(line.slice(1) || ' ')}</span>`,
        );
        newLine += 1;
        continue;
      }
      if (line.startsWith('-')) {
        out.push(
          `<span class="diff-line del"><span class="ln">${oldLine || ''}</span><span class="mark">-</span>${escapeHtml(line.slice(1) || ' ')}</span>`,
        );
        oldLine += 1;
        continue;
      }
      if (line) {
        oldLine += 1;
        newLine += 1;
      }
    }
    return out.join('');
  }

  runtime.ChangesDrawer = {
    init,
    open: openChangesDrawer,
    close: closeChangesDrawer,
  };
})();
