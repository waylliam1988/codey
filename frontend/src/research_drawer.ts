import type { DocumentRef } from './document_pane';
import { runtime } from './runtime';
import { createIconSvg } from './icon_button';
export interface ResearchSource {
  url?: string;
  final_url?: string;
  requested_url?: string;
  title?: string;
  host?: string;
  quality?: Record<string, unknown>;
  number?: number;
  retrieved_at?: string;
  claim?: string;
  source_url?: string;
  locator?: string;
  page?: number;
  stance?: string;
  note_id?: string;
  excerpt?: string;
  opened?: boolean;
  query?: string;
  rank?: number;
  reason?: string;
  content_kind?: string;
  pages_read?: number;
  page_count?: number;
  truncated?: boolean;
}
export interface ResearchCitation extends ResearchSource {
  url?: string;
}
export interface ResearchRunView {
  notesCreated: string[];
  notesUpdated: string[];
  synthesisId?: string;
  sourceUrls: string[];
  sourcesRead?: number;
  restoreable?: boolean;
  runId?: string;
  evidenceItems: ResearchSource[];
  counterpoints: string[];
  qualityWarnings: string[];
  citationMap: ResearchCitation[];
  openedSources: ResearchSource[];
  coverage: { queries?: string[]; skipped_results?: ResearchSource[] };
  queries: string[];
  searchResults: ResearchSource[];
  notes?: unknown[];
  receipt?: string;
  createdAt?: number;
}
export interface ResearchDrawerDeps {
  openDocument(sessionId: string, ref: DocumentRef): void;
  $(id: string): HTMLElement;
  getCurrentRun(sessionId: string, runId: string): ResearchRunView | null;
  copyText(text: string): Promise<boolean>;
  closeOtherDrawers?(active?: string): void;
  getSessionTitle?(sessionId: string): string;
  getResearchDrawerTab(): string;
  setResearchDrawerTab(value: string): void;
  getActiveId(): string;
  getResearchGraphDepth(): number;
  setResearchGraphDepth(value: number): void;
  getResearchNoteFocusId(): string;
  setResearchNoteFocusId(value: string): void;
}
/* Codey research drawer: evidence / sources / graph / notes tabs.
   Zero-build asset module; index.html injects dependencies via init() at boot. */
(function () {
  'use strict';

  const NOTE_PREVIEW_CHARS = 1500;
  const expandedResearchNoteIds = new Set();
  let boundSessionId = '';
  let boundRunId = '';

  type Source = ResearchSource;
  type Citation = ResearchCitation;
  interface ResearchNote {
    title?: string;
    type?: string;
    updated?: string;
    path?: string;
    body?: string;
    sources?: unknown[];
    __state?: string;
  }
  type ResearchRun = ResearchRunView;

  let deps: ResearchDrawerDeps;

  function init(nextDeps: ResearchDrawerDeps) {
    deps = nextDeps;
  }

  function $(id: string) {
    return deps.$(id);
  }

  function currentResearchRun(sessionId: string, runId: string) {
    return deps.getCurrentRun(sessionId, runId);
  }

  function copyText(text: string) {
    return deps.copyText(text);
  }

  function disposeResearchGraph() {
    if (runtime.ResearchGraph) runtime.ResearchGraph.dispose();
  }

  function openResearchDrawer(sessionId: string, runId: string) {
    if (!runId) return;
    if (deps.closeOtherDrawers) deps.closeOtherDrawers('research');
    boundSessionId = sessionId;
    boundRunId = runId;
    runtime.Workspace?.target('research', { runId });
    runtime.UiState.setDrawerOpen('research-drawer', true);
    const scope = $('research-scope');
    if (scope && deps.getSessionTitle) scope.textContent = deps.getSessionTitle(sessionId);
    renderResearchDrawer(sessionId);
  }

  function closeResearchDrawer() {
    boundSessionId = '';
    boundRunId = '';
    disposeResearchGraph();
    runtime.UiState.setDrawerOpen('research-drawer', false);
    $('research-drawer').classList.remove('graph-open');
  }

  function renderResearchDrawer(sessionId: string) {
    disposeResearchGraph();
    const run = currentResearchRun(sessionId, sessionId === boundSessionId ? boundRunId : '');
    const body = $('research-body');
    $('research-drawer').classList.toggle('graph-open', !!run && deps.getResearchDrawerTab() === 'graph');
    if (!run) {
      $('research-subtitle').textContent = 'No research yet';
      ($('research-restore') as HTMLButtonElement).disabled = true;
      body.innerHTML = '<div class="changes-empty">No research yet</div>';
      return;
    }
    $('research-subtitle').textContent =
      `${run.notesCreated.length} notes · ${run.sourcesRead || run.sourceUrls.length} sources`;
    ($('research-restore') as HTMLButtonElement).disabled = !run.runId || !run.restoreable;
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
      renderResearchNotes(panel, run, sessionId);
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

  function renderResearchEvidence(panel: HTMLElement, run: ResearchRun) {
    let rendered = false;
    for (const item of run.evidenceItems) {
      const url = safeResearchUrl(item.source_url);
      const locator = item.locator || (item.page ? `p.${item.page}` : '');
      const card = researchCard(item.claim || item.source_url || 'Evidence');
      const stance = document.createElement('span');
      stance.className = 'research-stance';
      stance.textContent = evidenceStance(item.stance);
      card.querySelector('.research-card-head')?.prepend(stance);
      const meta = researchMeta(
        card,
        [locator, item.note_id ? `note ${item.note_id}` : ''].filter(Boolean).join(' · '),
      );
      if (url) meta.prepend(researchLink(url, hostForUrl(url)));
      if (item.excerpt) {
        const quote = document.createElement('blockquote');
        quote.className = 'research-quote';
        quote.textContent = item.excerpt;
        card.append(quote);
      }
      panel.append(card);
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

  function evidenceStance(stance: string | undefined): string {
    switch (stance) {
      case 'supports':
        return 'Supports';
      case 'contradicts':
        return 'Contradicts';
      case 'context':
        return 'Context';
      case 'cited_unreviewed':
        return 'Cited, unreviewed';
      default:
        return 'Stance unknown';
    }
  }

  function renderResearchSources(panel: HTMLElement, run: ResearchRun) {
    const citations = run.citationMap;
    const opened = run.openedSources;
    if (!citations.length && !opened.length && !run.sourceUrls.length) {
      panel.appendChild(researchEmpty('No sources recorded'));
      return;
    }
    const openedByUrl = new Map<string, Source>();
    for (const item of opened) {
      for (const url of sourceUrls(item)) openedByUrl.set(url, item);
    }
    const citedUrls = new Set<string>();
    const cited = citations.map((item) => {
      const urls = sourceUrls(item);
      const source = urls.map((url) => openedByUrl.get(url)).find((match) => match !== undefined);
      for (const url of source ? [...urls, ...sourceUrls(source)] : urls) citedUrls.add(url);
      return { ...source, ...item, quality: item.quality || source?.quality, url: urls[0] };
    });
    const openedOnly = opened.filter((item) => {
      const urls = sourceUrls(item);
      return urls.length && !urls.some((url) => citedUrls.has(url));
    });
    appendSourceSection(panel, 'Cited in report', cited);
    appendSourceSection(panel, 'Opened only', openedOnly);
    if (!citations.length && !opened.length) {
      appendSourceSection(
        panel,
        'Recorded links',
        run.sourceUrls.map((url) => ({ url })),
      );
    }
  }

  function sourceUrls(item: Source): string[] {
    return [item.final_url, item.url, item.requested_url].map(safeResearchUrl).filter(Boolean);
  }

  function appendSourceSection(panel: HTMLElement, label: string, items: Source[]) {
    if (!items.length) return;
    const section = document.createElement('section');
    section.className = 'research-source-section';
    const heading = document.createElement('h3');
    heading.textContent = label;
    section.append(heading, ...items.map(researchSourceCard));
    panel.append(section);
  }

  function renderResearchGraph(panel: HTMLElement, run: ResearchRun, sessionId: string) {
    if (!runtime.ResearchGraph) {
      panel.appendChild(researchEmpty('Graph unavailable'));
      return;
    }
    runtime.ResearchGraph.render(panel, {
      run,
      sessionId,
      activeId: deps.getActiveId(),
      depth: deps.getResearchGraphDepth(),
      focusIds: run.synthesisId ? [run.synthesisId] : coreNoteIdsForResearchRun(run),
      counterpoints: run.counterpoints || [],
      onDepthChange(depth: number) {
        deps.setResearchGraphDepth(depth);
        renderResearchDrawer(sessionId);
      },
      onOpenNote(noteId: string) {
        deps.setResearchNoteFocusId(noteId);
        deps.setResearchDrawerTab('notes');
        invalidateResearchNoteCache([noteId]);
        renderResearchDrawer(sessionId);
      },
      onOpenSource(url: string) {
        window.open(url, '_blank', 'noopener,noreferrer');
      },
    });
  }

  function appendResearchCoverage(panel: HTMLElement, run: ResearchRun) {
    const coverage = run.coverage;
    const queries = Array.isArray(coverage.queries) ? coverage.queries : run.queries;
    const results = run.searchResults;
    const skipped = Array.isArray(coverage.skipped_results) ? coverage.skipped_results : [];
    if (!queries.length && !results.length && !skipped.length) {
      return false;
    }
    if (queries.length)
      panel.appendChild(
        researchListCard(
          'Search coverage',
          queries.map((query) => `query: ${query}`),
        ),
      );
    if (results.length) {
      const rows = results.slice(0, 24).map((item) => {
        const opened = item.opened ? 'opened' : 'not opened';
        return `${item.query || ''} · #${item.rank || ''} · ${opened} · ${item.title || item.url || ''}`;
      });
      panel.appendChild(researchListCard('Search results', rows));
    }
    if (skipped.length) {
      const rows = skipped.slice(0, 16).map((item) => `${item.title || item.url || ''} · ${item.reason || 'skipped'}`);
      panel.appendChild(researchListCard('Skipped results', rows));
    }
    return true;
  }

  function researchSourceCard(item: Source) {
    const number = item.number ? `[${item.number}] ` : '';
    const url = sourceUrls(item)[0];
    const title = item.title || url || 'Source';
    const quality = formatSourceQuality(item.quality || {});
    const pdf = formatPdfSourceMeta(item);
    const card = researchCard(number + title);
    if (url) {
      card.querySelector('.research-card-title')?.replaceChildren(researchLink(url, number + title));
      researchMeta(card, hostForUrl(url));
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'drawer-btn research-copy icon-action';
      copy.title = 'Copy citation';
      copy.setAttribute('aria-label', 'Copy citation');
      copy.append(createIconSvg('copy'));
      copy.onclick = () => {
        void copyText(`${number}${item.title || 'Source'} - ${url}`).then((ok) => {
          copy.title = ok ? 'Copied' : 'Could not copy';
          copy.setAttribute('aria-label', ok ? 'Copied' : 'Could not copy');
          setTimeout(
            () => {
              if (copy.isConnected) {
                copy.title = 'Copy citation';
                copy.setAttribute('aria-label', 'Copy citation');
              }
            },
            ok ? 1200 : 4000,
          );
        });
      };
      card.querySelector('.research-card-head')?.append(copy);
    }
    const details = [quality, pdf, item.retrieved_at].filter(Boolean).join(' · ');
    if (details) researchMeta(card, details);
    return card;
  }

  function researchLink(url: string, label: string): HTMLAnchorElement {
    const link = document.createElement('a');
    link.className = 'research-source-link';
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.title = url;
    link.textContent = label;
    return link;
  }

  function researchMeta(card: HTMLElement, text: string): HTMLElement {
    const meta = document.createElement('div');
    meta.className = 'research-card-meta';
    if (text) {
      const detail = document.createElement('span');
      detail.textContent = text;
      meta.append(detail);
    }
    card.append(meta);
    return meta;
  }

  function researchCard(title: unknown, meta = '') {
    const card = document.createElement('div');
    card.className = 'research-card';
    const head = document.createElement('div');
    head.className = 'research-card-head';
    const titleEl = document.createElement('div');
    titleEl.className = 'research-card-title';
    titleEl.textContent = String(title || '');
    head.appendChild(titleEl);
    card.appendChild(head);
    if (meta) researchMeta(card, meta);
    return card;
  }

  function researchListCard(title: string, values: unknown[]) {
    const card = researchCard(title);
    const list = document.createElement('ul');
    list.className = 'research-list';
    for (const value of Array.isArray(values) ? values : []) {
      const li = document.createElement('li');
      li.textContent = String(value || '');
      list.appendChild(li);
    }
    card.appendChild(list);
    return card;
  }

  function researchEmpty(text: string) {
    const div = document.createElement('div');
    div.className = 'changes-empty';
    div.textContent = text;
    return div;
  }

  function formatSourceQuality(quality: Record<string, unknown>) {
    return [quality.level, quality.kind, quality.freshness, quality.independent_group].filter(Boolean).join(' · ');
  }

  function formatPdfSourceMeta(item: Source) {
    if (String(item.content_kind || '').toLowerCase() !== 'pdf') return '';
    const pages = compactPages(Array.isArray(item.pages_read) ? item.pages_read : []);
    const pageCount = Number(item.page_count || 0);
    const parts = ['PDF'];
    if (pages) parts.push(pageCount ? `pages ${pages} / ${pageCount}` : `pages ${pages}`);
    if (item.truncated) parts.push('truncated');
    return parts.join(' · ');
  }

  function compactPages(values: unknown[]) {
    const pages: number[] = Array.from(
      new Set<number>(
        (values || []).map((value) => Number(value)).filter((value) => Number.isInteger(value) && value > 0),
      ),
    ).sort((a, b) => a - b);
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

  function renderResearchNotes(panel: HTMLElement, run: ResearchRun, sessionId: string) {
    const coreIds = coreNoteIdsForResearchRun(run);
    const focusId = deps.getResearchNoteFocusId();
    const selectedIds = focusId && !coreIds.includes(focusId) ? [focusId] : [];
    const synthesisIds = run.synthesisId ? [run.synthesisId] : [];
    const createdIds = uniqueStrings(run.notesCreated).filter((id) => id !== run.synthesisId);
    const updatedIds = uniqueStrings(run.notesUpdated).filter((id) => id !== run.synthesisId);
    const sections: Array<[string, string[]]> = [
      ['Selected note', selectedIds],
      ['Synthesis', synthesisIds],
      ['Created notes', createdIds],
      ['Updated notes', updatedIds],
    ];
    let rendered = false;
    const sourceIndex = buildResearchSourceIndex(run);
    for (const [title, ids] of sections) {
      if (!ids.length) continue;
      panel.appendChild(researchNoteSection(title, ids, run, sessionId, sourceIndex));
      rendered = true;
    }
    if (!rendered) panel.appendChild(researchEmpty('No notes recorded'));
    loadResearchRunNotes(run, sessionId);
  }

  function researchNoteSection(
    title: string,
    ids: string[],
    run: ResearchRun,
    sessionId: string,
    sourceIndex: Map<string, Source>,
  ) {
    const wrap = document.createElement('div');
    wrap.className = 'research-note-section';
    const head = document.createElement('div');
    head.className = 'research-note-section-head';
    const status = document.createElement('span');
    status.className = 'research-note-count';
    status.textContent = String(ids.length);
    const path = document.createElement('span');
    path.className = 'research-note-heading';
    path.textContent = title;
    head.append(status, path);
    wrap.appendChild(head);
    for (const id of ids) wrap.appendChild(researchNoteCard(id, run, sessionId, sourceIndex));
    return wrap;
  }

  function researchNoteCard(noteId: string, run: ResearchRun, sessionId: string, sourceIndex: Map<string, Source>) {
    const id = String(noteId || '');
    const note = researchNoteCache[id];
    if (!note || note.__state) return researchNoteStateCard(id, note);
    const card = document.createElement('div');
    card.className = 'research-card research-note-card';
    const head = document.createElement('div');
    head.className = 'research-card-head';
    const title = document.createElement('div');
    title.className = 'research-card-title';
    title.textContent = note.title || id || 'Note';
    head.appendChild(title);
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'drawer-btn';
    open.textContent = 'Open document';
    open.onclick = () => deps.openDocument(sessionId, { kind: 'research', note_id: id });
    head.appendChild(open);
    card.appendChild(head);
    const meta = [note.type || 'note', note.updated || '', note.path || ''].filter(Boolean).join(' · ');
    if (meta) {
      const metaEl = document.createElement('div');
      metaEl.className = 'research-card-meta';
      metaEl.textContent = meta;
      card.appendChild(metaEl);
    }
    const bodyText = String(note.body || '');
    const expanded = expandedResearchNoteIds.has(id);
    const clipped = !expanded && bodyText.length > NOTE_PREVIEW_CHARS;
    const preview = clipped ? bodyText.slice(0, NOTE_PREVIEW_CHARS).trimEnd() + '\n...' : bodyText;
    const body = document.createElement('div');
    body.className = 'research-note-body md';
    if (runtime.Render && typeof runtime.Render.renderMarkdown === 'function') {
      runtime.Render.renderMarkdown(body, preview || 'No note body');
    } else {
      body.textContent = preview || 'No note body';
    }
    card.appendChild(body);
    const chips = researchSourceChips(note, sourceIndex);
    if (chips) card.appendChild(chips);
    if (bodyText.length > NOTE_PREVIEW_CHARS) {
      const actions = document.createElement('div');
      actions.className = 'research-note-actions';
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'drawer-btn research-note-toggle';
      toggle.textContent = expanded ? 'Show less' : 'Show more';
      toggle.onclick = () => {
        if (expanded) expandedResearchNoteIds.delete(id);
        else expandedResearchNoteIds.add(id);
        renderResearchDrawer(sessionId);
      };
      actions.appendChild(toggle);
      card.appendChild(actions);
    }
    return card;
  }

  function researchNoteStateCard(id: string, note: ResearchNote | undefined) {
    const state = note && note.__state;
    if (state === 'missing') return researchCard(id || 'Note', 'Note no longer exists');
    if (state === 'error') return researchCard(id || 'Note', 'Could not load note');
    const card = researchCard(id || 'Note');
    const status = document.createElement('div');
    status.className = 'research-card-meta drawer-loading';
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    const label = document.createElement('span');
    label.textContent = 'Loading note...';
    status.append(spinner, label);
    card.appendChild(status);
    return card;
  }

  function researchSourceChips(note: ResearchNote, sourceIndex: Map<string, Source>) {
    const refs = sourceRefsForNote(note, sourceIndex);
    if (!refs.length) return null;
    const wrap = document.createElement('div');
    wrap.className = 'research-source-chips';
    const label = document.createElement('span');
    label.className = 'research-source-label';
    label.textContent = 'Sources';
    wrap.appendChild(label);
    for (const ref of refs) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'research-source-chip';
      chip.textContent = ref.number ? `[${ref.number}]` : ref.host || 'source';
      chip.title = [ref.title, ref.host, ref.url].filter(Boolean).join(' · ');
      chip.onclick = (event) => {
        event.stopPropagation();
        window.open(ref.url, '_blank', 'noopener,noreferrer');
      };
      wrap.appendChild(chip);
    }
    return wrap;
  }

  function sourceRefsForNote(note: ResearchNote, sourceIndex: Map<string, Source>): Source[] {
    const refs: Source[] = [];
    const seen = new Set<string>();
    for (const source of (Array.isArray(note.sources) ? note.sources : []) as unknown[]) {
      const url = safeResearchUrl(source);
      if (!url || seen.has(url)) continue;
      seen.add(url);
      const indexed = sourceIndex.get(url) || {};
      refs.push({
        url,
        number: indexed.number || 0,
        title: indexed.title || '',
        host: indexed.host || hostForUrl(url),
      });
    }
    return refs;
  }

  function buildResearchSourceIndex(run: ResearchRun): Map<string, Source> {
    const byUrl = new Map<string, Source>();
    let nextNumber = 1;
    const put = (urlValue: unknown, item: Source = {}, aliasOf: Source | null = null): Source | null => {
      const url = safeResearchUrl(urlValue);
      if (!url) return null;
      const existing = byUrl.get(url) || {};
      const number = Number(item.number || existing.number || (aliasOf && aliasOf.number) || 0);
      if (number >= nextNumber) nextNumber = number + 1;
      const meta = {
        url,
        number: number || existing.number || nextNumber++,
        title: String(item.title || existing.title || (aliasOf && aliasOf.title) || ''),
        host: existing.host || (aliasOf && aliasOf.host) || hostForUrl(url),
      };
      byUrl.set(url, meta);
      return meta;
    };
    for (const item of run.citationMap || []) {
      const meta = put(item.url || item.final_url || item.requested_url, item);
      if (item.final_url) put(item.final_url, item, meta);
      if (item.requested_url) put(item.requested_url, item, meta);
    }
    for (const item of run.openedSources || []) {
      const meta = put(item.final_url || item.url || item.requested_url, item);
      if (item.url) put(item.url, item, meta);
      if (item.final_url) put(item.final_url, item, meta);
      if (item.requested_url) put(item.requested_url, item, meta);
    }
    for (const url of run.sourceUrls || []) put(url, { title: url });
    return byUrl;
  }

  function safeResearchUrl(value: unknown): string {
    const text = String(value || '').trim();
    if (!text) return '';
    try {
      const url = new URL(text);
      return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
    } catch {
      return '';
    }
  }

  function hostForUrl(value: string): string {
    try {
      return new URL(value).host.replace(/^www\./, '');
    } catch {
      return '';
    }
  }

  function uniqueStrings(values: unknown): string[] {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const value of Array.isArray(values) ? values : []) {
      const text = String(value || '');
      if (!text || seen.has(text)) continue;
      seen.add(text);
      out.push(text);
    }
    return out;
  }

  const researchNoteCache: Record<string, ResearchNote> = {};
  function coreNoteIdsForResearchRun(run: ResearchRun | null | undefined): string[] {
    if (!run) return [];
    const ids = [run.synthesisId, ...(run.notesCreated || []), ...(run.notesUpdated || [])].filter(
      (id): id is string => typeof id === 'string' && id.length > 0,
    );
    return Array.from(new Set<string>(ids));
  }
  function noteIdsForResearchRun(run: ResearchRun): string[] {
    const ids = coreNoteIdsForResearchRun(run);
    const focusId = deps.getResearchNoteFocusId();
    if (focusId) ids.push(focusId);
    return Array.from(new Set(ids.filter(Boolean)));
  }
  function invalidateResearchNoteCache(ids: unknown) {
    if (!Array.isArray(ids)) return;
    for (const id of ids) if (typeof id === 'string') delete researchNoteCache[id];
  }
  async function loadResearchRunNotes(run: ResearchRun, sessionId: string) {
    const ids = noteIdsForResearchRun(run).filter((id) => id && !researchNoteCache[id]);
    if (!ids.length) return;
    for (const id of ids) researchNoteCache[id] = { __state: 'loading' };
    try {
      const r = await fetch('/api/research/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      if (!r.ok) {
        for (const id of ids) researchNoteCache[id] = { __state: 'error' };
      } else {
        const data = await r.json();
        const notes = (data && data.ok && data.notes) || {};
        const missing = new Set(Array.isArray(data && data.missing) ? data.missing : []);
        for (const id of ids) {
          if (notes[id]) researchNoteCache[id] = notes[id];
          else if (missing.has(id)) researchNoteCache[id] = { __state: 'missing' };
          else researchNoteCache[id] = { __state: 'error' };
        }
      }
    } catch {
      for (const id of ids) researchNoteCache[id] = { __state: 'error' };
    }
    if (
      boundSessionId === sessionId &&
      currentResearchRun(sessionId, boundRunId) === run &&
      $('research-drawer').classList.contains('open')
    ) {
      renderResearchDrawer(sessionId);
    }
  }

  runtime.ResearchDrawer = {
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
