/* Codey changes drawer: per-file diff list with fold/unfold rendering.
   Zero-build asset module; index.html injects dependencies via init() at boot. */
(function () {
  'use strict';

  let deps = null;
  let generation = 0;
  let activeProject = '';
  let selectedPath = '';
  let lastData = null;
  let loading = false, restoring = false, fresh = false, failed = false;
  let pathFocus = null;

function init(nextDeps) {
  deps = nextDeps;
  $('changes-refresh').onclick = () => { if (activeProject && !loading && !restoring) loadChangesDrawer(activeProject); };
  $('changes-restore').onclick = restore;
  $('changes-copy').onclick = copyDiff;
  updateControls();
}

function $(id) { return deps.$(id); }

function fetchChanges(project) { return deps.fetchChanges(project); }

function escapeHtml(text) { return deps.escapeHtml(text); }

function updateControls() {
  $('changes-refresh').disabled = !activeProject || loading || restoring;
  $('changes-refresh').textContent = failed ? 'Retry' : 'Refresh';
  $('changes-restore').hidden = lastData?.mode === 'git';
  $('changes-restore').disabled = !fresh || loading || restoring || !lastData?.files?.length || lastData.mode === 'git';
  $('changes-restore').textContent = restoring ? 'Restoring' : 'Restore';
  $('changes-copy').disabled = !lastData?.diff;
}
async function copyDiff() {
  if (!lastData?.diff) return;
  const project = activeProject, data = lastData;
  const ok = await window.CodeyRender.copyText(data.diff);
  if (activeProject !== project || lastData !== data) return;
  $('changes-copy').textContent = ok ? 'Copied' : 'Could not copy';
  setTimeout(() => { if (activeProject === project && lastData === data) $('changes-copy').textContent = 'Copy diff'; }, 1200);
}
async function openChangesDrawer(project, path = '') {
  if (!project) return;
  if (deps.closeOtherDrawers) deps.closeOtherDrawers('changes');
  if (project !== activeProject) {
    lastData = null; fresh = false; restoring = false;
    $('changes-body').replaceChildren();
    $('changes-copy').textContent = 'Copy diff';
  }
  activeProject = project; selectedPath = path;
  const scope = $('changes-scope');
  scope.textContent = window.CodeyUiState.pathName(project); scope.title = project;
  window.CodeyUiState.setDrawerOpen('changes-drawer', true);
  pathFocus = document.activeElement;
  await loadChangesDrawer(project);
}
async function loadChangesDrawer(project) {
  const request = ++generation;
  loading = true; fresh = false; failed = false;
  $('changes-subtitle').textContent = lastData ? 'Updating…' : 'Loading…';
  updateControls();
  if (!lastData) $('changes-body').innerHTML = '<div class="changes-empty"><span class="drawer-loading"><span class="spinner"></span><span>Reading changes...</span></span></div>';
  try {
    const data = await fetchChanges(project);
    if (request !== generation || project !== activeProject) return false;
    if (!data?.ok || !Array.isArray(data.files) || typeof data.diff !== 'string') throw new Error(data?.error || 'Invalid changes result');
    renderChangesDrawer(data); fresh = true;
    focusPath();
    return true;
  } catch (err) {
    if (request !== generation || project !== activeProject) return false;
    failed = true;
    $('changes-subtitle').textContent = 'Could not update' + (lastData ? ' · Showing previous changes' : '') + ' · Retry';
    if (!lastData) { const error = document.createElement('div'); error.className = 'changes-error'; error.textContent = String(err); $('changes-body').replaceChildren(error); }
    return false;
  } finally {
    if (request === generation && project === activeProject) { loading = false; updateControls(); }
  }
}
function closeChangesDrawer() {
  generation++; activeProject = ''; selectedPath = ''; pathFocus = null;
  lastData = null; loading = restoring = fresh = failed = false;
  updateControls();
  window.CodeyUiState.setDrawerOpen('changes-drawer', false);
}
function renderChangesDrawer(data) {
  lastData = data;
  const body = $('changes-body'), scroll = body.scrollTop;
  const top = body.getBoundingClientRect().top;
  const anchor = Array.from(body.querySelectorAll('.diff-line, .change-file > button')).find(node => node.getBoundingClientRect().bottom > top);
  const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
  const files = data.files, chunks = parseDiffChunks(data.diff);
  const mode = data.mode === 'git' ? 'Working tree' : 'Snapshot';
  $('changes-subtitle').textContent = `${files.length} file${files.length === 1 ? '' : 's'} changed · ${mode}`;
  const previous = new Map(Array.from(body.querySelectorAll('.change-file')).map(node => [node.dataset.path, node]));
  const nodes = files.map(file => {
    const node = previous.get(file.path) || changeFileNode(file);
    updateFileNode(node, file, chunks);
    return node;
  });
  const kept = new Set(nodes);
  for (const node of Array.from(body.children)) if (!kept.has(node)) {
    if (node.contains(document.activeElement)) $('changes-close').focus({preventScroll:true});
    node.remove();
  }
  nodes.forEach((node, index) => { if (body.children[index] !== node) body.insertBefore(node, body.children[index] || null); });
  if (!files.length) body.innerHTML = '<div class="changes-empty">No changes</div>';
  body.scrollTop = anchor?.isConnected ? body.scrollTop + anchor.getBoundingClientRect().top - top - offset : scroll;
}
async function restore() {
  if (!activeProject || !fresh || loading || restoring || !lastData?.files.length || lastData.mode === 'git') return;
  const project = activeProject;
  let request = generation;
  const current = () => request === generation && project === activeProject;
  restoring = true; updateControls();
  try {
    const data = await deps.restoreChanges(project);
    if (!current()) return;
    if (!data.ok) {
      fresh = false; failed = true;
      $('changes-subtitle').textContent = data.conflicts?.length ? `Conflict: ${data.conflicts.join(', ')}` : (data.error || 'Restore failed');
      return;
    }
    const refresh = loadChangesDrawer(project); request = generation;
    const updated = await refresh;
    if (!current()) return;
    if (!updated) $('changes-subtitle').textContent = 'Restored · ' + $('changes-subtitle').textContent;
    else $('changes-subtitle').textContent += ' · Restored';
  } catch (err) {
    if (current()) { fresh = false; failed = true; $('changes-subtitle').textContent = 'Could not confirm restore · ' + String(err); }
  } finally { if (current()) { restoring = false; updateControls(); } }
}

function parseDiffChunks(diff) {
  const lines = (diff || '').split('\n');
  const chunks = [];
  let current = null;
  function push() { if (current) chunks.push(current); }
  function start(path) {
    push();
    current = { path: (path || '').replace(/^b\//, ''), lines: [] };
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const next = lines[i + 1] || '';
    if (line.startsWith('diff --git ')) {
      const m = line.match(/\sb\/(.+)$/);
      start(m ? m[1] : '');
    } else if (line.startsWith('--- /dev/null') && next.startsWith('+++ ')
        && (!current || !current.lines.some(l => l.startsWith('diff --git ')))) {
      start(next.replace(/^\+\+\+\s+/, '').replace(/^b\//, ''));
    }
    if (!current && line.trim()) current = { path: '', lines: [] };
    if (current) current.lines.push(line);
  }
  push();
  return chunks;
}

function chunkForPath(chunks, path) {
  const target = (path || '').replace(/\\/g, '/');
  return chunks.find(c => c.path === target || c.path.endsWith('/' + target)) || null;
}

function changeFileNode(file) {
  const wrap = document.createElement('div');
  wrap.className = 'change-file';
  wrap.dataset.path = file.path || '';
  const btn = document.createElement('button');
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
  btn.onclick = () => { pre.hidden = !pre.hidden; btn.setAttribute('aria-expanded', String(!pre.hidden)); };
  wrap.append(btn, pre);
  return wrap;
}

function updateFileNode(node, file, chunks) {
  node.querySelector('.change-status').textContent = file.status || 'M';
  const stats = node.querySelectorAll('.change-stat');
  stats[0].textContent = `+${file.additions || 0}`; stats[1].textContent = `-${file.deletions || 0}`;
  const chunk = chunkForPath(chunks, file.path || '');
  const content = chunk ? renderDiffLines(chunk.lines) : '<span class="diff-line meta">(no text diff available)</span>';
  const pre = node.querySelector('pre');
  if (node._diffContent !== content) { pre.innerHTML = content; node._diffContent = content; }
}

function focusPath() {
  if (!selectedPath) return;
  const normalize = path => String(path).replace(/\\/g, '/');
  const target = normalize(selectedPath), requested = selectedPath; selectedPath = '';
  const row = Array.from($('changes-body').querySelectorAll('.change-file')).find(el => {
    const path = normalize(el.dataset.path);
    return path === target || normalize(activeProject + '/' + path) === target;
  });
  if (!row) { $('changes-subtitle').textContent += ' · No change for ' + requested; return; }
  row.querySelector('pre').hidden = false;
  row.querySelector('button').setAttribute('aria-expanded', 'true');
  if (document.activeElement === pathFocus) { row.scrollIntoView({ block: 'nearest' }); row.querySelector('button').focus({preventScroll:true}); }
  pathFocus = null;
}

function renderDiffLines(lines) {
  let oldLine = 0;
  let newLine = 0;
  const out = [];
  for (const line of (lines || [])) {
    if (line.startsWith('@@')) {
      const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) {
        oldLine = Number(m[1]);
        newLine = Number(m[2]);
      }
      continue;
    }
    if (line.startsWith('diff --git') || line.startsWith('index ') || line.startsWith('---') || line.startsWith('+++')) {
      continue;
    }
    if (line.startsWith('+')) {
      out.push(`<span class="diff-line add"><span class="ln">${newLine || ''}</span><span class="mark">+</span>${escapeHtml(line.slice(1) || ' ')}</span>`);
      newLine += 1;
      continue;
    }
    if (line.startsWith('-')) {
      out.push(`<span class="diff-line del"><span class="ln">${oldLine || ''}</span><span class="mark">-</span>${escapeHtml(line.slice(1) || ' ')}</span>`);
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

window.CodeyChangesDrawer = {
  init,
  open: openChangesDrawer,
  close: closeChangesDrawer,
};
})();
