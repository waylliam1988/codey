import { runtime } from './runtime';
import { nestedOwnsScroll } from './scroll_owner';
import type { Message, Project, Session } from './types/domain';
import type { ProcessSnapshot } from './process';
import { HistoryRepository } from './history_repository';
export interface ConversationUiApi {
  init(deps: ConversationUiDeps): void;
  renderChat(): void;
  scrollChat(): void;
  captureView(): void;
  isFollowing(): boolean;
  updateLatest(): void;
  updateNotice(): void;
  stopFollowing(): void;
  matchesSession(session: Session): boolean;
  matchesProject(project: Project): boolean;
  appendGroupLabel(list: HTMLElement, text: string, hasMatches: boolean | number): void;
  updateSearch(): void;
  isSearching(): boolean;
  isRenamingSession(id: string): boolean;
  forget(id: string): void;
  openFile(path: string | null, project?: string): unknown;
  menuOpened(menu: HTMLElement, anchor: HTMLElement): void;
  menusClosed(restore?: boolean): void;
}
export interface ConversationUiDeps {
  activeSession(): Session | undefined;
  activeProject(): Project | null | undefined;
  getSessions(): Session[];
  getProjects(): Project[];
  getActiveId(): string;
  getRunningSessionId(): string | null;
  sessionProject(session: Session): Project | null | undefined;
  currentProviderId(): string;
  syncProviderUI(provider: string): void;
  updateComposerContext(): void;
  syncResearchUseProjectButton(active?: string): void;
  appendMessageNode(chat: HTMLElement, message: Message): void;
  renderSidebar(): void;
  switchSession(id: string): void;
  closeAllMenus(active?: string): void;
  closeOtherDrawers(active?: string): void;
  currentProjectPath(): string;
  commitSessionRename(id: string, value: string): void;
  renameInputEl(
    value: string,
    onCommit: (value: string | null | undefined) => void,
    onCancel: () => void,
  ): HTMLInputElement;
}
/* Conversation continuity, quiet status, search, and keyboard navigation. */
(function () {
  'use strict';
  interface ViewState {
    messages: Message[] | null;
    nodes: Node[] | null;
    disclosures: ProcessSnapshot[];
    scroll: number;
    following: boolean;
    anchor?: { index: number; offset: number } | null;
    height: number;
  }
  interface TitleEdit {
    id: string;
    input: HTMLInputElement;
    composing: boolean;
    blurPending: boolean;
    pointerAway: boolean;
    refocus: boolean;
  }
  interface ScrollPosition {
    node: HTMLElement;
    top: number;
    left: number;
  }
  interface SelectionSnapshot {
    input?: HTMLInputElement | HTMLTextAreaElement;
    start?: number;
    end?: number;
    range?: Range;
    text: string;
    focus: Element | null;
    quoteBody?: Element | null;
    sessionId?: string;
    scrollPositions?: ScrollPosition[];
  }
  let deps: ConversationUiDeps;
  let shownId = '';
  const views = new Map<string, ViewState>();
  const historyStarts = new Map<string, number>();
  const historyRepositories = new Map<string, HistoryRepository>();
  const archiveHydrated = new Set<string>();
  const HISTORY_BATCH = 100;
  let menuTrigger: HTMLElement | null = null;
  let selectedCopy: SelectionSnapshot | null = null,
    copyGeneration = 0;
  let titleEdit: TitleEdit | null = null;
  let following = true,
    scrollOffset = 0;
  let scrollIntent: 'history' | 'latest' | null = null,
    scrollbarHeld = false;

  function $<T extends HTMLElement = HTMLElement>(id: string): T {
    const element = document.getElementById(id);
    if (!element) throw new Error(`Missing conversation UI element: ${id}`);
    return element as T;
  }
  function query(): string {
    return $<HTMLInputElement>('chat-search').value.trim().toLocaleLowerCase();
  }
  function matchesSession(s: Session): boolean {
    const p = deps.sessionProject(s);
    return [s.title, p && p.name].filter(Boolean).join(' ').toLocaleLowerCase().includes(query());
  }
  function matchesProject(p: Project): boolean {
    return (
      !query() ||
      String(p.name || '')
        .toLocaleLowerCase()
        .includes(query()) ||
      deps.getSessions().some((s: Session) => s.projectId === p.id && matchesSession(s))
    );
  }
  function appendGroupLabel(list: HTMLElement, text: string, hasMatches: boolean): void {
    const label = document.createElement('div');
    label.className = 'group-label';
    label.textContent = text;
    label.hidden = !!query() && !hasMatches;
    list.appendChild(label);
  }
  function updateSearch() {
    if (!deps) return;
    $('chat-search-clear').hidden =
      $<HTMLInputElement>('chat-search').hidden || !$<HTMLInputElement>('chat-search').value;
    const empty = !!query() && !deps.getProjects().some(matchesProject) && !deps.getSessions().some(matchesSession);
    $('search-empty').hidden = !empty;
    $('session-list').hidden = empty;
  }
  function isFollowing() {
    return following;
  }
  function isAtLatest() {
    const a = $('chat-area');
    return a.scrollHeight - a.scrollTop - a.clientHeight <= 2;
  }
  function setScroll(top: number) {
    const area = $('chat-area');
    area.scrollTop = top;
    scrollOffset = area.scrollTop;
  }
  function readingAnchor() {
    const top = $('chat-area').getBoundingClientRect().top;
    const index = Array.from($('chat').children).findIndex((node) => node.getBoundingClientRect().bottom > top);
    return index < 0 ? null : { index, offset: $('chat').children[index].getBoundingClientRect().top - top };
  }
  function restoreReading(view: Pick<ViewState, 'scroll' | 'anchor'> | undefined) {
    const node = view?.anchor && $('chat').children[view.anchor.index];
    if (node && view?.anchor)
      setScroll(
        $('chat-area').scrollTop +
          node.getBoundingClientRect().top -
          $('chat-area').getBoundingClientRect().top -
          view.anchor.offset,
      );
    else if (view) setScroll(view.scroll);
  }
  function stopFollowing() {
    following = false;
    scrollIntent = 'history';
    captureView();
    updateLatest();
  }
  function backToLatest() {
    following = true;
    scrollIntent = null;
    scrollChat();
  }

  function captureView() {
    if (!shownId) return;
    const session = deps.getSessions().find((s: Session) => s.id === shownId);
    if (!session) {
      views.delete(shownId);
      return;
    }
    views.delete(shownId);
    views.set(shownId, {
      messages: session.messages.slice(),
      nodes: Array.from($('chat').childNodes),
      disclosures: runtime.Process.capture($('chat')),
      scroll: $('chat-area').scrollTop,
      following,
      anchor: readingAnchor(),
      height: $('chat-area').scrollHeight,
    });
    const cached = Array.from(views.values()).filter((view) => view.nodes);
    for (const old of cached.slice(0, Math.max(0, cached.length - 6))) {
      old.nodes = null;
      old.messages = null;
    }
  }
  function unchanged(view: ViewState | undefined, session: Session): boolean {
    return !!(
      view &&
      view.nodes &&
      view.messages &&
      view.messages.length === session.messages.length &&
      view.messages.every(
        (m: Message, i: number) =>
          m === session.messages[i] || JSON.stringify(m) === JSON.stringify(session.messages[i]),
      )
    );
  }
  function finishTitleEdit(edit: TitleEdit, value: string | null, restoreFocus = false): void {
    if (titleEdit !== edit) return;
    titleEdit = null;
    if (value !== null) deps.commitSessionRename(edit.id, value);
    else {
      const session = deps.activeSession();
      if (session) renderTitle(session, deps.activeProject());
    }
    if (restoreFocus && deps.getActiveId() === edit.id)
      $('sess-title').querySelector<HTMLElement>('.crumb-chat')?.focus({ preventScroll: true });
  }
  function startTitleRename(id: string): void {
    const s = deps.activeSession();
    if (!s || s.id !== id || titleEdit) return;
    deps.closeAllMenus();
    const edit: TitleEdit = {
      id,
      input: document.createElement('input'),
      composing: false,
      blurPending: false,
      pointerAway: false,
      refocus: false,
    };
    const input = deps.renameInputEl(
      s.title || '',
      (value: string | null | undefined) => finishTitleEdit(edit, value ?? null, edit.refocus),
      () => finishTitleEdit(edit, null, true),
    ) as HTMLInputElement;
    input.classList.add('title-rename');
    input.setAttribute('aria-label', 'Chat title');
    edit.input = input;
    titleEdit = edit;
    const keydown = input.onkeydown;
    input.onkeydown = (e: KeyboardEvent) => {
      if (titleEdit !== edit || edit.composing || e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter' || e.key === 'Escape') {
        edit.refocus = true;
        e.stopPropagation();
      }
      keydown?.call(input, e);
    };
    input.addEventListener('compositionstart', () => {
      if (titleEdit === edit) edit.composing = true;
    });
    input.addEventListener('compositionend', () => {
      if (titleEdit !== edit) return;
      edit.composing = false;
      if (!edit.pointerAway) finishTitleBlur();
    });
    input.onblur = () => {
      if (titleEdit !== edit) return;
      edit.blurPending = true;
      if (!edit.pointerAway) finishTitleBlur();
    };
    $('sess-title').querySelector<HTMLElement>('.crumb-chat')?.replaceWith(input);
    input.focus({ preventScroll: true });
    input.select();
  }
  function finishTitleBlur() {
    const edit = titleEdit;
    if (edit?.blurPending && !edit.composing) finishTitleEdit(edit, edit.input.value);
    if (titleEdit) titleEdit.pointerAway = false;
  }
  function initTitleRename() {
    document.addEventListener(
      'pointerdown',
      (e) => {
        if (titleEdit && e.target !== titleEdit.input) titleEdit.pointerAway = true;
      },
      true,
    );
    // Commit during the click event, after its target has been determined, so
    // sidebar rerendering cannot swallow a click on the next chat.
    document.addEventListener('click', finishTitleBlur, true);
    document.addEventListener(
      'pointerup',
      () => {
        const edit = titleEdit;
        setTimeout(() => {
          if (titleEdit === edit) finishTitleBlur();
        }, 0);
      },
      true,
    );
    document.addEventListener('pointercancel', finishTitleBlur, true);
    window.addEventListener('blur', finishTitleBlur);
  }
  function renderTitle(s: Session, p: Project | null | undefined) {
    if (titleEdit && (titleEdit.id !== s.id || !titleEdit.input.isConnected))
      finishTitleEdit(titleEdit, titleEdit.input.value);
    const title = $('sess-title');
    if (titleEdit) {
      const project = title.querySelector('.crumb-proj');
      if (project && p) project.textContent = p.name;
      return;
    }
    const existing = title.querySelector('button.crumb-chat');
    if (existing && title.dataset.sessionId === s.id && title.dataset.projectId === (p?.id || '')) {
      existing.textContent = s.title || 'New chat';
      const project = title.querySelector('.crumb-proj');
      if (project) project.textContent = p?.name || '';
      return;
    }
    title.replaceChildren();
    title.dataset.sessionId = s.id;
    title.dataset.projectId = p?.id || '';
    if (p) {
      const project = document.createElement('span');
      project.className = 'crumb-proj';
      project.textContent = p.name;
      const separator = document.createElement('span');
      separator.className = 'crumb-sep';
      separator.textContent = '/';
      title.append(project, separator);
    }
    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'crumb-chat';
    name.textContent = s.title || 'New chat';
    name.title = 'Rename chat';
    name.setAttribute('aria-label', 'Rename chat');
    name.onclick = () => startTitleRename(s.id);
    title.append(name);
  }
  function renderChat() {
    if (!deps) return;
    const s = deps.activeSession();
    if (!s) return;
    if (!archiveHydrated.has(s.id) && s.messages.length >= 500) void hydrateArchivedHistory(s);
    runtime.Composer.syncSession();
    const p = deps.activeProject();
    renderTitle(s, p);
    deps.syncProviderUI(deps.currentProviderId());
    deps.updateComposerContext();
    deps.syncResearchUseProjectButton();
    const changing = shownId !== s.id;
    if (changing) {
      closeSelectionMenu();
      captureView();
    }
    const saved = views.get(s.id);
    const scroll = changing && saved ? saved.scroll : $('chat-area').scrollTop;
    const follow = changing ? !saved || saved.following : following;
    const reader = changing ? saved : { scroll, anchor: readingAnchor() };
    const chat = $('chat');
    if (unchanged(saved, s)) {
      if (changing && saved?.nodes) chat.replaceChildren(...saved.nodes);
    } else {
      const disclosures = changing ? saved?.disclosures : runtime.Process.capture(chat);
      chat.replaceChildren();
      if (!s.messages.length) {
        const welcome = document.createElement('div');
        welcome.className = 'welcome';
        welcome.innerHTML = '<h1>Codey</h1><p>Send a message to start.</p>';
        chat.append(welcome);
      } else {
        let start = historyStarts.get(s.id);
        if (start === undefined || start >= s.messages.length)
          start = historyBoundary(s.messages, Math.max(0, s.messages.length - HISTORY_BATCH));
        historyStarts.set(s.id, start);
        addHistoryLoader(chat, s, start);
        for (const m of s.messages.slice(start)) deps.appendMessageNode(chat, m);
      }
      runtime.Process.restore(chat, disclosures);
    }
    shownId = s.id;
    runtime.UserQuestions?.syncViews(chat);
    following = follow;
    if (changing) scrollIntent = null;
    if (following) setScroll($('chat-area').scrollHeight);
    else {
      setScroll(scroll);
      restoreReading(reader);
    }
    captureView();
    updateLatest();
    updateNotice();
  }

  async function hydrateArchivedHistory(session: Session): Promise<void> {
    const repository = historyRepositories.get(session.id) || new HistoryRepository();
    historyRepositories.set(session.id, repository);
    try {
      const page = await repository.loadLatest(session.id);
      if (deps.activeSession() !== session) return;
      const archived = page.items
        .map((value) => value as unknown as Message)
        .filter((message) => typeof message.type === 'string');
      const messageKey = (message: Message): string => {
        const id = message.id || message.eventKey;
        if (id) return `id:${id}`;
        return `body:${JSON.stringify({ type: message.type, text: message.text, result: message.result, output: message.output, command: message.command })}`;
      };
      const mergedById = new Map<string, Message>();
      const unkeyed: Message[] = [];
      for (const message of archived) {
        const id = messageKey(message);
        if (id) mergedById.set(id, message);
        else unkeyed.push(message);
      }
      for (const message of session.messages) {
        const id = messageKey(message);
        if (id) mergedById.set(id, message);
        else unkeyed.push(message);
      }
      const merged = [...mergedById.values(), ...unkeyed];
      merged.sort((left, right) => {
        const a =
          typeof (left as Message & { ordinal?: unknown }).ordinal === 'number'
            ? Number((left as Message & { ordinal?: unknown }).ordinal)
            : Number.MAX_SAFE_INTEGER;
        const b =
          typeof (right as Message & { ordinal?: unknown }).ordinal === 'number'
            ? Number((right as Message & { ordinal?: unknown }).ordinal)
            : Number.MAX_SAFE_INTEGER;
        return a - b;
      });
      archiveHydrated.add(session.id);
      if (merged.length !== session.messages.length) {
        session.messages = merged;
        historyStarts.delete(session.id);
        renderChat();
      }
    } catch {
      // The archive is display-only; the current UI state remains usable.
    }
  }
  function historyBoundary(messages: Message[], index: number): number {
    while (index > 0 && messages[index]?.type !== 'user') index--;
    return index;
  }
  function addHistoryLoader(chat: HTMLElement, session: Session, start: number): void {
    if (!start) return;
    const loader = document.createElement('div');
    loader.className = 'history-loader';
    const earlier = document.createElement('button'),
      all = document.createElement('button'),
      searchButton = document.createElement('button');
    earlier.type = all.type = 'button';
    earlier.className = all.className = 'link-btn';
    earlier.textContent = 'Load earlier messages';
    earlier.dataset.loadHistory = '';
    all.textContent = 'Show all for find';
    all.dataset.showAllHistory = '';
    searchButton.type = 'button';
    searchButton.className = 'link-btn';
    searchButton.textContent = 'Search this conversation';
    searchButton.dataset.searchHistory = '';
    const load = async (everything: boolean) => {
      if (deps.activeSession() !== session) return;
      if (!everything && start === 0) {
        const repository = historyRepositories.get(session.id);
        if (repository) {
          const page = await repository.loadEarlier(session.id);
          if (deps.activeSession() !== session || !page) return;
          const known = new Set(session.messages.map((message) => message.id || message.eventKey).filter(Boolean));
          const older = page.items
            .map((item) => item as unknown as Message)
            .filter((message) => !known.has(message.id || message.eventKey));
          if (older.length) session.messages = [...older, ...session.messages];
          historyStarts.set(session.id, older.length ? 0 : start);
          renderChat();
          return;
        }
      }
      const anchor = loader.nextElementSibling;
      const top = anchor?.getBoundingClientRect().top;
      const next = everything ? 0 : historyBoundary(session.messages, Math.max(0, start - HISTORY_BATCH));
      const prefix = document.createElement('div');
      for (const message of session.messages.slice(next, start)) deps.appendMessageNode(prefix, message);
      loader.remove();
      chat.prepend(...prefix.childNodes);
      historyStarts.set(session.id, next);
      addHistoryLoader(chat, session, next);
      if (next) chat.prepend(chat.lastElementChild!);
      following = false;
      scrollIntent = 'history';
      if (anchor && top !== undefined) setScroll($('chat-area').scrollTop + anchor.getBoundingClientRect().top - top);
      captureView();
      updateLatest();
    };
    earlier.onclick = () => load(false);
    all.onclick = () => {
      if (deps.activeSession() !== session) return;
      const anchor = loader.nextElementSibling;
      const top = anchor?.getBoundingClientRect().top;
      const prefix = document.createElement('div');
      for (const message of session.messages.slice(0, start)) deps.appendMessageNode(prefix, message);
      loader.remove();
      chat.prepend(...prefix.childNodes);
      historyStarts.set(session.id, 0);
      following = false;
      if (anchor && top !== undefined) setScroll($('chat-area').scrollTop + anchor.getBoundingClientRect().top - top);
      captureView();
      updateLatest();
    };
    searchButton.onclick = async () => {
      if (deps.activeSession() !== session) return;
      let search = loader.querySelector<HTMLInputElement>('[data-history-search]');
      if (!search) {
        search = document.createElement('input');
        search.type = 'search';
        search.placeholder = 'Search this conversation';
        search.setAttribute('aria-label', 'Search this conversation');
        search.dataset.historySearch = '';
        loader.insertBefore(search, earlier);
        search.focus();
        search.onkeydown = (event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            searchButton.click();
          }
        };
        return;
      }
      const query = search.value;
      if (!query.trim()) return;
      const repository = historyRepositories.get(session.id) || new HistoryRepository();
      historyRepositories.set(session.id, repository);
      try {
        const hits = await repository.search(session.id, query);
        if (deps.activeSession() !== session) return;
        const status = document.createElement('span');
        status.className = 'history-search-status';
        status.textContent = hits.length ? `${hits.length} matches` : 'No matches';
        loader.replaceChildren(status, earlier, all, searchButton);
        window.setTimeout(() => status.remove(), 1800);
      } catch {
        const status = document.createElement('span');
        status.className = 'history-search-status';
        status.textContent = 'Search unavailable';
        loader.prepend(status);
        window.setTimeout(() => status.remove(), 1800);
      }
    };
    loader.append(earlier, all, searchButton);
    chat.append(loader);
  }

  function scrollChat() {
    if (following) setScroll($('chat-area').scrollHeight);
    else restoreReading(views.get(shownId));
    captureView();
    updateLatest();
    updateNotice();
  }
  function updateLatest() {
    $('back-to-latest').hidden = following || isAtLatest();
  }

  function updateNotice() {
    if (!deps) return;
    const sessions = deps.getSessions();
    const runningId = deps.getRunningSessionId();
    const owner = sessions.find((s: Session) => s.id === runningId);
    const status = $('status');
    if (owner && status.classList.contains('run')) {
      const text = status.querySelector('.status-text');
      if (text) text.textContent = owner.id === deps.getActiveId() ? 'Running' : 'Running in ' + owner.title;
      let open = status.querySelector('button');
      if (owner.id !== deps.getActiveId()) {
        if (!open) {
          open = document.createElement('button');
          open.className = 'link-btn';
          open.textContent = 'Open';
          status.append(open);
        }
        open.onclick = () => deps.switchSession(owner.id);
      } else if (open) open.remove();
    }
  }

  function openFile(path: string | null, project = '') {
    const target = project || deps.currentProjectPath();
    if (target && path) return runtime.ChangesDrawer.open(target, path);
  }
  function forget(id: string) {
    views.delete(id);
    historyStarts.delete(id);
  }

  function menuOpened(menu: HTMLElement, anchor: HTMLElement) {
    menuTrigger = anchor;
    menu.setAttribute('role', 'menu');
    (menu as HTMLElement)
      .querySelectorAll('button')
      .forEach((button: Element) => button.setAttribute('role', 'menuitem'));
    anchor.setAttribute('aria-expanded', 'true');
    menu.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
  }
  function menusClosed(restore = false) {
    closeSelectionMenu(restore);
    if (menuTrigger) {
      menuTrigger.setAttribute('aria-expanded', 'false');
      if (restore && menuTrigger.isConnected) {
        const row = menuTrigger.closest('.session-item, .project-row');
        row?.querySelector<HTMLElement>('.session-title, .project-main')?.focus();
        menuTrigger.focus();
      }
      menuTrigger = null;
    }
  }

  function initSidebarResize() {
    const handle = $('sidebar-resize'),
      root = document.documentElement;
    const tokens = getComputedStyle(root),
      token = (name: string): number => parseFloat(tokens.getPropertyValue(name));
    const initial = token('--sidebar-width'),
      min = token('--sidebar-min-width'),
      max = token('--sidebar-max-width');
    const contentMin = token('--sidebar-content-min-width'),
      narrow = () => innerWidth <= 720;
    let preferred = initial;
    let drag: { id: number; x: number; width: number; preferred: number } | null = null;
    try {
      const rawSaved = localStorage.getItem('codey:sidebar-width');
      const saved = rawSaved ? JSON.parse(rawSaved) : null;
      if (typeof saved === 'number' && Number.isFinite(saved)) preferred = Math.max(min, Math.min(max, saved));
    } catch {}
    const limit = () => Math.max(min, Math.min(max, innerWidth - contentMin));
    const clamp = (value: number): number => Math.max(min, Math.min(limit(), value));
    function apply(snap = false) {
      if (snap) $('aside').style.transition = 'none';
      const size = narrow() ? Math.min(initial, innerWidth - 32) : clamp(preferred);
      root.style.setProperty('--sidebar-width', size + 'px');
      handle.setAttribute('aria-valuemin', String(min));
      handle.setAttribute('aria-valuemax', String(limit()));
      handle.setAttribute('aria-valuenow', String(size));
      if (snap) {
        $('aside').getBoundingClientRect();
        $('aside').style.removeProperty('transition');
      }
    }
    function save() {
      try {
        localStorage.setItem('codey:sidebar-width', JSON.stringify(preferred));
      } catch {}
    }
    function finish(cancel = false) {
      if (!drag) return;
      const previous = drag;
      drag = null;
      if (cancel) preferred = previous.preferred;
      document.body.classList.remove('sidebar-resizing');
      if (handle.hasPointerCapture(previous.id)) handle.releasePointerCapture(previous.id);
      apply();
      if (!cancel) save();
    }
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || narrow()) return;
      e.preventDefault();
      deps.closeAllMenus();
      drag = { id: e.pointerId, x: e.clientX, width: $('aside').getBoundingClientRect().width, preferred };
      handle.setPointerCapture(e.pointerId);
      document.body.classList.add('sidebar-resizing');
    });
    handle.addEventListener('pointermove', (e) => {
      if (!drag || drag.id !== e.pointerId) return;
      preferred = clamp(drag.width + e.clientX - drag.x);
      apply();
    });
    handle.addEventListener('pointerup', (e) => {
      if (drag?.id === e.pointerId) finish();
    });
    handle.addEventListener('pointercancel', () => finish(true));
    handle.addEventListener('lostpointercapture', () => finish(true));
    handle.addEventListener('dblclick', () => {
      if (!narrow()) {
        preferred = initial;
        apply();
        save();
      }
    });
    handle.addEventListener('keydown', (e) => {
      if (narrow() || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
      e.preventDefault();
      preferred =
        e.key === 'Home'
          ? min
          : e.key === 'End'
            ? limit()
            : clamp($('aside').getBoundingClientRect().width + (e.key === 'ArrowLeft' ? -10 : 10));
      apply();
      save();
    });
    document.addEventListener(
      'keydown',
      (e) => {
        if (drag && e.key === 'Escape') {
          e.preventDefault();
          e.stopImmediatePropagation();
          finish(true);
        }
      },
      true,
    );
    window.addEventListener('resize', () => {
      if (narrow()) finish(true);
      apply(true);
      closeSelectionMenu();
    });
    window.addEventListener('blur', () => finish(true));
    new MutationObserver(() => {
      if (document.body.classList.contains('sidebar-collapsed')) finish(true);
    }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    apply(true);
  }

  function selectionFor(target: Element | null): SelectionSnapshot | null {
    if (!target) return null;
    const input = target.closest<HTMLInputElement | HTMLTextAreaElement>('input,textarea');
    if (input) {
      if (input.type === 'password' || input.selectionStart === null || input.selectionStart === input.selectionEnd)
        return null;
      const start = input.selectionStart ?? 0,
        end = input.selectionEnd ?? start;
      return { input, start, end, text: input.value.slice(start, end), focus: input };
    }
    const area = target.closest<HTMLElement>('#chat-area,.changes-drawer'),
      selection = getSelection();
    if (
      !area ||
      !selection ||
      !selection.rangeCount ||
      selection.isCollapsed ||
      !selection.toString() ||
      !area.contains(selection.anchorNode) ||
      !area.contains(selection.focusNode)
    )
      return null;
    const range = selection.getRangeAt(0).cloneRange();
    if (!range.intersectsNode(target)) return null;
    const parent = (node: Node): Element | null =>
      node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
    const answer = parent(range.startContainer)?.closest('#chat .msg.asst .body');
    const quoteBody = answer?.contains(range.endContainer) ? answer : null;
    return {
      range,
      text: selection.toString(),
      focus: document.activeElement,
      quoteBody,
      sessionId: deps.getActiveId(),
    };
  }
  function restoreSelection(snapshot: SelectionSnapshot | null, focus = false): void {
    if (!snapshot) return;
    if (
      focus &&
      snapshot.focus?.isConnected &&
      !snapshot.focus.closest('[inert]') &&
      snapshot.focus instanceof HTMLElement
    )
      snapshot.focus.focus({ preventScroll: true });
    if (snapshot.input?.isConnected && snapshot.start !== undefined && snapshot.end !== undefined)
      snapshot.input.setSelectionRange(snapshot.start, snapshot.end);
    else if (snapshot.range?.startContainer.isConnected && snapshot.range?.endContainer.isConnected) {
      const selection = getSelection();
      if (selection) {
        selection.removeAllRanges();
        selection.addRange(snapshot.range);
      }
    }
  }
  function closeSelectionMenu(restore = false) {
    const snapshot = selectedCopy;
    selectedCopy = null;
    copyGeneration++;
    $('selection-menu')?.classList.remove('open');
    if (restore) restoreSelection(snapshot, true);
  }
  function placeSelectionMenu(x: number, y: number) {
    const menu = $('selection-menu'),
      rect = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(x, innerWidth - rect.width - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(y, innerHeight - rect.height - 8)) + 'px';
  }
  function showSelectionMenu(target: Element | null, x: number, y: number, keyboard = false): boolean {
    const snapshot = selectionFor(target);
    deps.closeAllMenus();
    runtime.ProviderUI.closeMenu();
    if (!snapshot) return false;
    if (snapshot.range && $('chat').contains(snapshot.range.startContainer)) stopFollowing();
    snapshot.scrollPositions = [];
    for (let node: Element | null = target; node instanceof HTMLElement; node = node.parentElement)
      snapshot.scrollPositions.push({ node, top: node.scrollTop, left: node.scrollLeft });
    const menu = $('selection-menu'),
      copy = $<HTMLButtonElement>('selection-copy');
    (target?.closest('dialog[open]') || document.body).appendChild(menu);
    selectedCopy = snapshot;
    copyGeneration++;
    $('selection-quote').hidden = !snapshot.quoteBody;
    copy.textContent = 'Copy';
    copy.disabled = false;
    $('selection-copy-status').hidden = true;
    $('selection-copy-status').textContent = '';
    menu.classList.toggle('keyboard-open', keyboard);
    menu.classList.add('open');
    placeSelectionMenu(x, y);
    copy.focus({ preventScroll: true });
    return true;
  }
  function initSelectionMenu() {
    const menu = $('selection-menu'),
      copy = $<HTMLButtonElement>('selection-copy');
    document.addEventListener('contextmenu', (e) => {
      const target = e.target instanceof Element ? e.target : null;
      if (target && menu.contains(target)) {
        e.preventDefault();
        return;
      }
      if (showSelectionMenu(target, e.clientX, e.clientY)) {
        e.preventDefault();
        e.stopPropagation();
      }
    });
    menu.addEventListener('pointerdown', (e) => e.preventDefault());
    $('selection-quote').onclick = () => {
      const snapshot = selectedCopy;
      if (
        !snapshot?.quoteBody?.isConnected ||
        typeof snapshot.sessionId !== 'string' ||
        snapshot.sessionId !== deps.getActiveId() ||
        !$('chat').contains(snapshot.quoteBody)
      ) {
        closeSelectionMenu();
        return;
      }
      if (runtime.Composer.quoteInReply(snapshot.sessionId, snapshot.text)) closeSelectionMenu();
      else {
        $('selection-copy-status').textContent = 'Could not insert quote';
        $('selection-copy-status').hidden = false;
      }
    };
    copy.onclick = async () => {
      const snapshot = selectedCopy,
        generation = copyGeneration;
      if (!snapshot || copy.disabled) return;
      copy.disabled = true;
      let ok = false;
      try {
        ok = await runtime.Render.copyText(snapshot.text);
      } catch {}
      if (generation !== copyGeneration) return;
      restoreSelection(snapshot);
      if (ok) {
        copy.textContent = 'Copied';
        setTimeout(() => {
          if (generation === copyGeneration) closeSelectionMenu(true);
        }, 800);
      } else {
        copy.disabled = false;
        $('selection-copy-status').textContent = 'Could not copy';
        $('selection-copy-status').hidden = false;
        const rect = menu.getBoundingClientRect();
        placeSelectionMenu(rect.left, rect.top);
        copy.focus({ preventScroll: true });
      }
    };
    window.addEventListener(
      'keydown',
      (e) => {
        if (menu.classList.contains('open') && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key))
          menu.classList.add('keyboard-open');
        if (menu.classList.contains('open') && ['Escape', 'Tab'].includes(e.key)) {
          closeSelectionMenu(true);
          if (e.key === 'Escape') {
            e.preventDefault();
            e.stopImmediatePropagation();
          }
        } else if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
          const target = document.activeElement,
            snapshot = selectionFor(target);
          if (!snapshot) return;
          const rect = snapshot.range
            ? snapshot.range.getBoundingClientRect()
            : (target?.getBoundingClientRect() ?? new DOMRect());
          e.preventDefault();
          e.stopImmediatePropagation();
          showSelectionMenu(target, rect.left, rect.bottom, true);
        }
      },
      true,
    );
    document.addEventListener(
      'scroll',
      () => {
        if (
          selectedCopy?.scrollPositions?.some(
            ({ node, top, left }) => node.scrollTop !== top || node.scrollLeft !== left,
          )
        )
          closeSelectionMenu();
      },
      true,
    );
    window.addEventListener('blur', () => closeSelectionMenu());
  }

  function init(nextDeps: ConversationUiDeps) {
    deps = nextDeps;
    const narrow = matchMedia('(max-width: 720px)');
    const collapseOnNarrow = () => {
      if (narrow.matches) {
        document.body.classList.add('sidebar-collapsed');
        $('aside').inert = true;
        $('show-sidebar').style.display = '';
      }
    };
    collapseOnNarrow();
    narrow.addEventListener('change', collapseOnNarrow);
    const search = $<HTMLInputElement>('chat-search'),
      searchToggle = $<HTMLButtonElement>('chat-search-toggle'),
      searchClear = $<HTMLButtonElement>('chat-search-clear');
    const setSearchOpen = (open: boolean) => {
      search.hidden = !open;
      searchToggle.hidden = open;
      searchToggle.setAttribute('aria-expanded', String(open));
      searchClear.hidden = !open || !search.value;
    };
    searchToggle.onclick = () => {
      setSearchOpen(true);
      search.focus();
    };
    searchClear.onclick = () => {
      search.value = '';
      search.dispatchEvent(new Event('input'));
      search.focus();
    };
    search.addEventListener('blur', () => {
      if (!query()) setSearchOpen(false);
    });
    $('chat-search').addEventListener('input', () => {
      deps.renderSidebar();
    });
    $('chat-search').addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        search.value = '';
        search.dispatchEvent(new Event('input'));
        setSearchOpen(false);
        searchToggle.focus();
      }
    });
    const area = $('chat-area');
    area.addEventListener(
      'wheel',
      (e) => {
        if (e.shiftKey || Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
        if (nestedOwnsScroll(e.target, area, e.deltaY)) return;
        if (e.deltaY < 0) stopFollowing();
        else scrollIntent = 'latest';
      },
      { passive: true },
    );
    area.addEventListener('keydown', (e) => {
      const target = e.target instanceof Element ? e.target : null;
      if (e.isComposing || target?.closest('input,textarea,[contenteditable="true"]')) return;
      if (
        nestedOwnsScroll(
          e.target,
          area,
          ['ArrowUp', 'PageUp', 'Home'].includes(e.key) || (e.key === ' ' && e.shiftKey) ? -1 : 1,
        )
      )
        return;
      if (['ArrowUp', 'PageUp', 'Home'].includes(e.key) || (e.key === ' ' && e.shiftKey)) stopFollowing();
      else if (['ArrowDown', 'PageDown', 'End', ' '].includes(e.key)) scrollIntent = 'latest';
    });
    area.addEventListener('pointerdown', (e) => {
      const rect = area.getBoundingClientRect();
      if (e.target === area && e.clientX >= rect.right - (area.offsetWidth - area.clientWidth || 12)) {
        scrollbarHeld = true;
        stopFollowing();
      }
    });
    document.addEventListener('pointerup', () => {
      if (scrollbarHeld && isAtLatest()) {
        following = true;
        scrollIntent = null;
        captureView();
        updateLatest();
      }
      scrollbarHeld = false;
    });
    document.addEventListener('pointercancel', () => {
      scrollbarHeld = false;
    });
    window.addEventListener('blur', () => {
      scrollbarHeld = false;
    });
    area.addEventListener('pointerup', () => {
      const selection = getSelection();
      if (selection && !selection.isCollapsed && area.contains(selection.anchorNode)) stopFollowing();
    });
    let touchY: number | undefined;
    area.addEventListener(
      'touchstart',
      (e) => {
        touchY = e.touches[0]?.clientY;
      },
      { passive: true },
    );
    area.addEventListener(
      'touchmove',
      (e) => {
        const y = e.touches[0]?.clientY;
        if (touchY !== undefined && y !== undefined && y > touchY) stopFollowing();
        else if (touchY !== undefined && y !== undefined && y < touchY) scrollIntent = 'latest';
        touchY = y;
      },
      { passive: true },
    );
    area.addEventListener('scroll', () => {
      const view = views.get(shownId);
      if (view && view.height !== area.scrollHeight) {
        if (following) setScroll(area.scrollHeight);
        else restoreReading(view);
      } else if (area.scrollTop > scrollOffset && isAtLatest() && (scrollIntent === 'latest' || scrollbarHeld)) {
        following = true;
        scrollIntent = null;
      }
      scrollOffset = area.scrollTop;
      captureView();
      updateLatest();
    });
    new ResizeObserver(() => {
      if (shownId) scrollChat();
    }).observe($('chat'));
    $('back-to-latest').onclick = backToLatest;
    document.addEventListener(
      'keydown',
      (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        const target = e.target instanceof Element ? e.target : null;
        const menu = target?.closest('.ctx-menu.open');
        if (menu && ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape'].includes(e.key)) {
          e.preventDefault();
          e.stopPropagation();
          if (e.key === 'Escape') {
            menusClosed(true);
            deps.closeAllMenus();
            return;
          }
          const rows = Array.from(menu.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')).filter(
            (node) => !node.hidden,
          );
          const index = document.activeElement instanceof HTMLButtonElement ? rows.indexOf(document.activeElement) : -1;
          const next =
            e.key === 'Home'
              ? 0
              : e.key === 'End'
                ? rows.length - 1
                : (index + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
          rows[next]?.focus();
          return;
        }
        const drawer = document.querySelector('.changes-drawer.open');
        if (drawer && e.key === 'Escape') {
          e.preventDefault();
          e.stopPropagation();
          deps.closeOtherDrawers();
        } else if (drawer && e.key === 'Tab') {
          const rows = Array.from(
            drawer.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input, [tabindex="0"]'),
          ).filter((el) => el.getClientRects().length);
          const first = rows[0],
            last = rows[rows.length - 1];
          if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last?.focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first?.focus();
          }
        }
      },
      true,
    );
    $('chat').addEventListener('click', (e) => {
      const target = e.target instanceof Element ? e.target : null;
      const path = target?.closest('.cf-path');
      if (path) openFile(path.textContent, path.closest<HTMLElement>('[data-project]')?.dataset.project);
    });
    document.querySelectorAll<HTMLElement>('.changes-drawer').forEach((el) => {
      el.inert = !el.classList.contains('open');
    });
    initSidebarResize();
    initSelectionMenu();
    initTitleRename();
    runtime.ConversationNav.init({ getActiveId: nextDeps.getActiveId });
  }

  runtime.ConversationUI = {
    init,
    renderChat,
    scrollChat,
    captureView,
    isFollowing,
    updateLatest,
    updateNotice,
    stopFollowing,
    matchesSession,
    matchesProject,
    appendGroupLabel,
    updateSearch,
    isSearching: () => !!query(),
    isRenamingSession: (id: string) => titleEdit?.id === id,
    forget,
    openFile,
    menuOpened,
    menusClosed,
  };
})();
