/* Codey changes drawer: per-file diff list with fold/unfold rendering.
   Zero-build asset module; index.html injects dependencies via init() at boot. */
(function () {
  'use strict';

  let deps = null;

function init(nextDeps) {
  deps = nextDeps;
}

function $(id) { return deps.$(id); }

function fetchChanges(project) { return deps.fetchChanges(project); }

function escapeHtml(text) { return deps.escapeHtml(text); }

async function openChangesDrawer(project) {
  if (!project) return;
  if (deps.closeOtherDrawers) deps.closeOtherDrawers('changes');
  deps.setActiveProject(project);
  $('changes-drawer').classList.add('open');
  $('changes-drawer').setAttribute('aria-hidden', 'false');
  await loadChangesDrawer(project);
}

async function loadChangesDrawer(project) {
  $('changes-subtitle').textContent = 'Loading…';
  $('changes-restore').disabled = true;
  $('changes-body').innerHTML = '<div class="changes-empty">Reading changes…</div>';
  try {
    const data = await fetchChanges(project);
    deps.setLastData(data);
    renderChangesDrawer(data);
  } catch (err) {
    deps.setLastData(null);
    $('changes-subtitle').textContent = 'Failed';
    $('changes-body').innerHTML = `<div class="changes-error">${escapeHtml(String(err))}</div>`;
  }
}

function closeChangesDrawer() {
  $('changes-drawer').classList.remove('open');
  $('changes-drawer').setAttribute('aria-hidden', 'true');
}

function renderChangesDrawer(data) {
  const body = $('changes-body');
  if (!data || !data.ok) {
    $('changes-subtitle').textContent = data && data.error ? data.error : 'No changes';
    body.innerHTML = `<div class="changes-empty">${escapeHtml(data && data.error ? data.error : 'No changes')}</div>`;
    return;
  }
  const files = Array.isArray(data.files) ? data.files : [];
  const mode = data.mode === 'git' ? 'Git' : 'Snapshot';
  $('changes-subtitle').textContent = `${files.length} file${files.length === 1 ? '' : 's'} changed · ${mode}`;
  $('changes-restore').disabled = data.mode === 'git' || !files.length;
  if (!files.length) {
    body.innerHTML = '<div class="changes-empty">No changes</div>';
    return;
  }
  const chunks = parseDiffChunks(data.diff || '');
  body.innerHTML = '';
  for (const file of files) body.appendChild(changeFileNode(file, chunks));
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

function changeFileNode(file, chunks) {
  const wrap = document.createElement('div');
  wrap.className = 'change-file';
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
  const chunk = chunkForPath(chunks, file.path || '');
  pre.innerHTML = chunk ? renderDiffLines(chunk.lines) : '<span class="diff-line meta">(no text diff available)</span>';
  btn.onclick = () => { pre.hidden = !pre.hidden; };
  wrap.append(btn, pre);
  return wrap;
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
  load: loadChangesDrawer,
  close: closeChangesDrawer,
  render: renderChangesDrawer,
};
})();
