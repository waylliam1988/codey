/* Codey research drawer: evidence / sources / graph / notes tabs.
   Zero-build asset module; index.html injects dependencies via init() at boot. */
(function () {
  'use strict';

  let deps = null;

function init(nextDeps) {
  deps = nextDeps;
}

function $(id) { return deps.$(id); }

function currentResearchRun(sessionId) { return deps.getCurrentRun(sessionId); }

function copyText(text) { return deps.copyText(text); }

function disposeResearchGraph() {
  if (window.CodeyResearchGraph) window.CodeyResearchGraph.dispose();
}

function openResearchDrawer(sessionId) {
  $('research-drawer').classList.add('open');
  $('research-drawer').setAttribute('aria-hidden', 'false');
  renderResearchDrawer(sessionId);
}

function closeResearchDrawer() {
  disposeResearchGraph();
  $('research-drawer').classList.remove('open');
  $('research-drawer').classList.remove('graph-open');
  $('research-drawer').setAttribute('aria-hidden', 'true');
}

function renderResearchDrawer(sessionId) {
  disposeResearchGraph();
  const run = currentResearchRun(sessionId);
  const body = $('research-body');
  $('research-drawer').classList.toggle('graph-open', !!run && deps.getResearchDrawerTab() === 'graph');
  if (!run) {
    $('research-subtitle').textContent = 'No research yet';
    $('research-restore').disabled = true;
    body.innerHTML = '<div class="changes-empty">No research yet</div>';
    return;
  }
  $('research-subtitle').textContent = `${run.notesCreated.length} notes · ${run.sourcesRead || run.sourceUrls.length} sources`;
  $('research-restore').disabled = !run.runId || !run.restoreable;
  body.innerHTML = '';
  body.appendChild(researchTabsNode());
  const panel = document.createElement('div');
  panel.className = 'research-panel';
  body.appendChild(panel);
  if (deps.getResearchDrawerTab() === 'sources') {
    renderResearchSources(panel, run);
  } else if (deps.getResearchDrawerTab() === 'graph') {
    renderResearchGraph(panel, run, sessionId);
  } else if (deps.getResearchDrawerTab() === 'notes') {
    const coreIds = coreNoteIdsForResearchRun(run);
    const focusId = deps.getResearchNoteFocusId();
    if (focusId && !coreIds.includes(focusId)) {
      panel.appendChild(researchSection('Selected note', [focusId]));
    }
    panel.appendChild(researchSection('Synthesis', run.synthesisId ? [run.synthesisId] : []));
    panel.appendChild(researchSection('Notes created', run.notesCreated));
    panel.appendChild(researchSection('Notes updated', run.notesUpdated));
    panel.appendChild(researchSection('Sources', run.sourceUrls));
    loadResearchRunNotes(run, sessionId);
  } else {
    renderResearchEvidence(panel, run);
  }
}

function researchTabsNode() {
  const tabs = document.createElement('div');
  tabs.className = 'research-tabs';
  const items = [
    ['evidence', 'Evidence'],
    ['sources', 'Sources'],
    ['graph', 'Graph'],
    ['notes', 'Notes'],
  ];
  for (const [id, label] of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'research-tab' + (deps.getResearchDrawerTab() === id ? ' active' : '');
    btn.textContent = label;
    btn.onclick = () => {
      deps.setResearchDrawerTab(id);
      renderResearchDrawer(deps.getActiveId());
    };
    tabs.appendChild(btn);
  }
  return tabs;
}

function renderResearchEvidence(panel, run) {
  let rendered = false;
  for (const item of run.evidenceItems) {
    const title = item.claim || item.source_url || 'Evidence';
    const locator = item.locator || (item.page ? `p.${item.page}` : '');
    const meta = [item.stance || 'supports', locator, item.source_url || '', item.note_id ? `note ${item.note_id}` : '']
      .filter(Boolean).join(' · ');
    panel.appendChild(researchCard(title, meta, item.excerpt || ''));
    rendered = true;
  }
  if (run.counterpoints.length) {
    panel.appendChild(researchListCard('Counter-evidence / limitations', run.counterpoints));
    rendered = true;
  }
  if (run.qualityWarnings.length) {
    panel.appendChild(researchListCard('Quality warnings', run.qualityWarnings));
    rendered = true;
  }
  rendered = appendResearchCoverage(panel, run) || rendered;
  if (!rendered) panel.appendChild(researchEmpty('No evidence items recorded'));
}

function renderResearchSources(panel, run) {
  const citations = run.citationMap;
  const opened = run.openedSources;
  if (!citations.length && !opened.length && !run.sourceUrls.length) {
    panel.appendChild(researchEmpty('No sources recorded'));
    return;
  }
  const openedByUrl = new Map(opened.map(item => [String(item.final_url || item.url || ''), item]));
  const citationUrls = new Set(citations.map(item => String(item.url || '')));
  for (const item of citations) {
    const url = String(item.url || '');
    const source = openedByUrl.get(url) || {};
    panel.appendChild(researchSourceCard({
      ...source,
      ...item,
      quality: item.quality || source.quality || {},
      final_url: url || source.final_url || '',
    }));
  }
  for (const source of opened) {
    const url = String(source.final_url || source.url || '');
    if (!url || citationUrls.has(url)) continue;
    panel.appendChild(researchSourceCard({
      ...source,
      title: source.title || url,
      url,
      quality: source.quality || {},
      retrieved_at: source.retrieved_at || '',
    }));
  }
  if (!citations.length && !opened.length) {
    for (const url of run.sourceUrls) panel.appendChild(researchSourceCard({ title: url, url }));
  }
}

function renderResearchGraph(panel, run, sessionId) {
  if (!window.CodeyResearchGraph) {
    panel.appendChild(researchEmpty('Graph unavailable'));
    return;
  }
  window.CodeyResearchGraph.render(panel, {
    run,
    sessionId,
    activeId: deps.getActiveId(),
    depth: deps.getResearchGraphDepth(),
    focusIds: run.synthesisId ? [run.synthesisId] : coreNoteIdsForResearchRun(run),
    counterpoints: run.counterpoints || [],
    onDepthChange(depth) {
      deps.setResearchGraphDepth(depth);
      renderResearchDrawer(sessionId);
    },
    onOpenNote(noteId) {
      deps.setResearchNoteFocusId(noteId);
      deps.setResearchDrawerTab('notes');
      invalidateResearchNoteCache([noteId]);
      renderResearchDrawer(sessionId);
    },
    onOpenSource(url) {
      window.open(url, '_blank', 'noopener,noreferrer');
    },
  });
}

function appendResearchCoverage(panel, run) {
  const coverage = run.coverage;
  const queries = Array.isArray(coverage.queries) ? coverage.queries : run.queries;
  const results = run.searchResults;
  const skipped = Array.isArray(coverage.skipped_results) ? coverage.skipped_results : [];
  if (!queries.length && !results.length && !skipped.length) {
    return false;
  }
  if (queries.length) panel.appendChild(researchListCard('Search coverage', queries.map(query => `query: ${query}`)));
  if (results.length) {
    const rows = results.slice(0, 24).map(item => {
      const opened = item.opened ? 'opened' : 'not opened';
      return `${item.query || ''} · #${item.rank || ''} · ${opened} · ${item.title || item.url || ''}`;
    });
    panel.appendChild(researchListCard('Search results', rows));
  }
  if (skipped.length) {
    const rows = skipped.slice(0, 16).map(item => `${item.title || item.url || ''} · ${item.reason || 'skipped'}`);
    panel.appendChild(researchListCard('Skipped results', rows));
  }
  return true;
}

function researchSourceCard(item) {
  const number = item.number ? `[${item.number}] ` : '';
  const url = String(item.url || item.final_url || '');
  const quality = formatSourceQuality(item.quality || {});
  const pdf = formatPdfSourceMeta(item);
  const meta = [quality, pdf, item.retrieved_at || '', url].filter(Boolean).join(' · ');
  const card = researchCard(number + (item.title || url || 'Source'), meta, '');
  if (url) {
    card.style.cursor = 'pointer';
    card.onclick = () => window.open(url, '_blank', 'noopener,noreferrer');
    const actions = document.createElement('div');
    actions.className = 'research-card-meta';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'drawer-btn research-copy';
    copy.textContent = 'Copy citation';
    copy.onclick = (e) => {
      e.stopPropagation();
      copyText(`${number}${item.title || 'Source'} - ${url}`);
    };
    actions.appendChild(copy);
    card.appendChild(actions);
  }
  return card;
}

function researchCard(title, meta, bodyText) {
  const card = document.createElement('div');
  card.className = 'research-card';
  const head = document.createElement('div');
  head.className = 'research-card-head';
  const titleEl = document.createElement('div');
  titleEl.className = 'research-card-title';
  titleEl.textContent = title || '';
  head.appendChild(titleEl);
  card.appendChild(head);
  if (meta) {
    const metaEl = document.createElement('div');
    metaEl.className = 'research-card-meta';
    metaEl.textContent = meta;
    card.appendChild(metaEl);
  }
  if (bodyText) {
    const body = document.createElement('div');
    body.className = 'research-excerpt';
    body.textContent = bodyText;
    card.appendChild(body);
  }
  return card;
}

function researchListCard(title, values) {
  const card = researchCard(title, '', '');
  const list = document.createElement('ul');
  list.className = 'research-list';
  for (const value of values || []) {
    const li = document.createElement('li');
    li.textContent = String(value || '');
    list.appendChild(li);
  }
  card.appendChild(list);
  return card;
}

function researchEmpty(text) {
  const div = document.createElement('div');
  div.className = 'changes-empty';
  div.textContent = text;
  return div;
}

function formatSourceQuality(quality) {
  return [quality.level, quality.kind, quality.freshness, quality.independent_group]
    .filter(Boolean)
    .join(' · ');
}

function formatPdfSourceMeta(item) {
  if (String(item.content_kind || '').toLowerCase() !== 'pdf') return '';
  const pages = compactPages(Array.isArray(item.pages_read) ? item.pages_read : []);
  const pageCount = Number(item.page_count || 0);
  const parts = ['PDF'];
  if (pages) parts.push(pageCount ? `pages ${pages} / ${pageCount}` : `pages ${pages}`);
  if (item.truncated) parts.push('truncated');
  return parts.join(' · ');
}

function compactPages(values) {
  const pages = Array.from(new Set((values || [])
    .map(value => Number(value))
    .filter(value => Number.isInteger(value) && value > 0)))
    .sort((a, b) => a - b);
  if (!pages.length) return '';
  const ranges = [];
  let start = pages[0];
  let prev = pages[0];
  for (const page of pages.slice(1)) {
    if (page === prev + 1) {
      prev = page;
      continue;
    }
    ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
    start = page;
    prev = page;
  }
  ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
  return ranges.join(',');
}

function researchSection(title, values) {
  const wrap = document.createElement('div');
  wrap.className = 'change-file';
  const btn = document.createElement('button');
  btn.type = 'button';
  const status = document.createElement('span');
  status.className = 'change-status';
  status.textContent = values.length || 0;
  const path = document.createElement('span');
  path.className = 'change-path';
  path.textContent = title;
  const add = document.createElement('span');
  add.className = 'change-stat';
  add.textContent = '';
  const del = document.createElement('span');
  del.className = 'change-stat';
  del.textContent = '';
  btn.append(status, path, add, del);
  const pre = document.createElement('pre');
  pre.className = 'diff-pre';
  pre.hidden = false;
  pre.textContent = values.length ? values.map(researchValueText).join('\n\n') : '(none)';
  wrap.append(btn, pre);
  return wrap;
}

function researchValueText(value) {
  const text = String(value || '');
  const note = researchNoteCache[text];
  if (!note) return text;
  if (note.__state === 'loading') return `${text}\nLoading note...`;
  if (note.__state === 'missing') return `${text}\nNote no longer exists`;
  if (note.__state === 'error') return `${text}\nCould not load note`;
  const excerpt = note.body ? '\n' + note.body.slice(0, 420) + (note.body.length > 420 ? '\n...' : '') : '';
  const path = note.path ? `\n${note.path}` : '';
  return `[${note.type}] ${note.title}\n${note.id}${path}${excerpt}`;
}

const researchNoteCache = {};
function coreNoteIdsForResearchRun(run) {
  if (!run) return [];
  const ids = [run.synthesisId, ...(run.notesCreated || []), ...(run.notesUpdated || [])].filter(Boolean);
  return Array.from(new Set(ids));
}
function noteIdsForResearchRun(run) {
  const ids = coreNoteIdsForResearchRun(run);
  const focusId = deps.getResearchNoteFocusId();
  if (focusId) ids.push(focusId);
  return Array.from(new Set(ids.filter(Boolean)));
}
function invalidateResearchNoteCache(ids) {
  for (const id of ids || []) delete researchNoteCache[id];
}
async function loadResearchRunNotes(run, sessionId) {
  const ids = noteIdsForResearchRun(run)
    .filter(id => id && !researchNoteCache[id]);
  if (!ids.length) return;
  await Promise.all(ids.map(async (id) => {
    researchNoteCache[id] = { __state: 'loading' };
    try {
      const r = await fetch('/api/research/note?id=' + encodeURIComponent(id));
      if (r.status === 404) {
        researchNoteCache[id] = { __state: 'missing' };
        return;
      }
      if (!r.ok) {
        researchNoteCache[id] = { __state: 'error' };
        return;
      }
      const data = await r.json();
      researchNoteCache[id] = data.ok && data.note ? data.note : { __state: 'missing' };
    } catch {
      researchNoteCache[id] = { __state: 'error' };
    }
  }));
  if (currentResearchRun(sessionId) === run && $('research-drawer').classList.contains('open')) {
    renderResearchDrawer(sessionId);
  }
}

window.CodeyResearchDrawer = {
  init,
  open: openResearchDrawer,
  close: closeResearchDrawer,
  render: renderResearchDrawer,
  dispose: disposeResearchGraph,
  coreNoteIdsForRun: coreNoteIdsForResearchRun,
  noteIdsForRun: noteIdsForResearchRun,
  invalidateNoteCache: invalidateResearchNoteCache,
};
})();
