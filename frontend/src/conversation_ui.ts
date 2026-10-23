import { runtime } from './runtime';
import { nestedOwnsScroll } from './scroll_owner';
import type { Message, Project, Session } from './types/domain';
import type { ProcessSnapshot } from './process';
import { ConversationSearch } from './conversation_search';
import { ConversationHistory } from './conversation_history';
import { iconButton } from './icon_button';
import type { ServerEvent } from './domain/protocol';
export interface ConversationUiApi {
  init(deps: ConversationUiDeps): void;
  renderChat(): void;
  refreshHistory(sessionId: string): Promise<void>;
  searchHistory(): void;
  observeCommit(event: ServerEvent): Promise<void>;
  scrollChat(capture?: boolean): void;
  captureView(): void;
  isFollowing(): boolean;
  updateLatest(): void;
  updateNotice(): void;
  stopFollowing(): void;
  refreshApprovals(): boolean;
  revealApproval(id: string): void;
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
  const historyRendered = new Set<string>();
  const forcedHistoryRefresh = new Set<string>();
  const views = new Map<string, ViewState>();
  const historyStarts = new Map<string, number>();
  const loadingHistory = new Map<string, symbol>();
  const deferredHistory = new Set<string>();
  let historyObserver: IntersectionObserver | undefined;
  let userScrollPending = false;
  let searchPanel: ConversationSearch | null = null;
  const focusRendering = new Map<string, symbol>();
  let searchOrigin: { session: string; entry: string; offset: number; scroll: number; following: boolean } | null =
    null;

  async function focusHistory(entryId: string): Promise<void> {
    const session = deps.activeSession();
    if (!session) return;
    forcedHistoryRefresh.add(session.id);
    following = false;
    const request = Symbol();
    focusRendering.set(session.id, request);
    try {
      if (!(await history.focus(session, entryId)) || deps.activeSession() !== session) return;
    } finally {
      if (focusRendering.get(session.id) === request) focusRendering.delete(session.id);
    }
    const target = $('chat').querySelector<HTMLElement>(`[data-entry-id="${CSS.escape(entryId)}"]`);
    if (!target) throw new Error('Message could not be displayed');
    setScroll(
      $('chat-area').scrollTop +
        target.getBoundingClientRect().top -
        $('chat-area').getBoundingClientRect().top -
        (searchPanel && !searchPanel.element.hidden ? searchPanel.element.offsetHeight + 16 : 24),
    );
    captureView();
  }
  function searchHistory(): void {
    if (!searchPanel) {
      searchPanel = new ConversationSearch({
        sessionId: () => deps.getActiveId(),
        jump: focusHistory,
        restore: async () => {
          const origin = searchOrigin;
          if (!origin || origin.session !== deps.getActiveId()) return;
          if (origin.following) {
            await refreshHistory(origin.session);
            backToLatest();
          } else if (origin.entry) {
            await focusHistory(origin.entry);
            const target = $('chat').querySelector<HTMLElement>(`[data-entry-id="${CSS.escape(origin.entry)}"]`);
            if (target)
              setScroll(
                $('chat-area').scrollTop +
                  target.getBoundingClientRect().top -
                  $('chat-area').getBoundingClientRect().top -
                  origin.offset,
              );
          } else setScroll(origin.scroll);
          captureView();
        },
      });
      document.querySelector('main')?.append(searchPanel.element);
    }
    if (searchPanel.element.hidden) {
      const top = $('chat-area').getBoundingClientRect().top;
      const anchor = Array.from($('chat').querySelectorAll<HTMLElement>('[data-entry-id]')).find(
        (node) => node.getBoundingClientRect().bottom >= top,
      );
      searchOrigin = {
        session: deps.getActiveId(),
        entry: anchor?.dataset.entryId || '',
        offset: (anchor?.getBoundingClientRect().top || top) - top,
        scroll: $('chat-area').scrollTop,
        following,
      };
    }
    searchPanel.open();
  }
  const history = new ConversationHistory((session, revealEarlier) => {
    runtime.UiState?.hydrateSessionIndexes(session);
    if (revealEarlier) {
      // A mounted history window prepends its older entries in-place after
      // the repository resolves. Re-rendering here would replace the visible
      // tail and invalidate selections, anchors, and browser node handles.
      historyStarts.set(session.id, 0);
      return;
    } else {
      if (focusRendering.has(session.id)) historyStarts.set(session.id, 0);
      else historyStarts.delete(session.id);
    }
    // A mounted conversation owns its visible nodes while live events are
    // arriving. Durable commits are still reconciled into the session model,
    // but rebuilding the tree here would detach nodes the user is reading.
    // An empty mount still needs the first authoritative history render.
    const force = forcedHistoryRefresh.delete(session.id);
    if (deps.activeSession() === session && historyRendered.has(session.id) && !force) return;
    historyRendered.add(session.id);
    views.delete(session.id);
    if (deps.activeSession() === session) renderChat(focusRendering.has(session.id));
  });
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
    userScrollPending = true;
    history.pauseRefresh(shownId);
    if (shownId) deferredHistory.add(shownId);
    captureView();
    updateLatest();
  }
  function revealApproval(id: string): void {
    const card = $('chat').querySelector<HTMLElement>(`[data-approval-id="${CSS.escape(id)}"]`);
    if (!card) return;
    const area = $('chat-area'),
      bounds = area.getBoundingClientRect(),
      rect = card.getBoundingClientRect();
    // Scroll only the transcript, never the main grid or the document.
    setScroll(area.scrollTop + rect.top - bounds.top - Math.max(16, (area.clientHeight - rect.height) / 2));
    card.focus({ preventScroll: true });
    captureView();
  }
  function refreshApprovals(): boolean {
    const session = deps.activeSession();
    if (!session || session.id !== shownId) return false;
    const chat = $('chat'),
      area = $('chat-area'),
      top = area.getBoundingClientRect().top;
    const anchor = Array.from(chat.children).find((node) => node.getBoundingClientRect().bottom > top);
    const offset = anchor?.getBoundingClientRect().top;
    const scroll = area.scrollTop;
    if (!runtime.Messages.updateApprovals(chat, session.messages)) return false;
    if (following) setScroll(area.scrollHeight);
    else if (anchor?.isConnected && offset !== undefined)
      setScroll(area.scrollTop + anchor.getBoundingClientRect().top - offset);
    else setScroll(scroll);
    captureView();
    updateLatest();
    return true;
  }
  function backToLatest() {
    userScrollPending = false;
    if (history.hasAfter(deps.getActiveId()) || deferredHistory.has(deps.getActiveId())) {
      void refreshHistory(deps.getActiveId()).then(() => {
        following = true;
        scrollChat();
      });
      return;
    }
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
      view.messages.every((m: Message, i: number) => sameMessage(m, session.messages[i]))
    );
  }
  function sameMessage(previous: Message, current: Message | undefined): boolean {
    if (!current || previous === current || previous.type !== current.type) return previous === current;
    // Server event keys are immutable identities. This avoids deep serialization when a
    // reconciled state snapshot recreated otherwise unchanged message objects.
    if (previous.eventKey && previous.eventKey === current.eventKey) return true;
    if (
      previous.type === 'asst' &&
      previous.text === current.text &&
      previous.runId === current.runId &&
      previous.streamId === current.streamId &&
      previous.streamSequence === current.streamSequence &&
      previous.streamState === current.streamState &&
      previous.finalAnswer === current.finalAnswer &&
      previous.contentRole === current.contentRole &&
      previous.turnId === current.turnId &&
      previous.answerId === current.answerId
    )
      return true;
    if (
      previous.type === 'user' &&
      previous.id === current.id &&
      previous.text === current.text &&
      previous.attempts?.length === current.attempts?.length &&
      previous.attempts?.every(
        (attempt, index) => JSON.stringify(attempt) === JSON.stringify(current.attempts?.[index]),
      )
    )
      return true;
    return JSON.stringify(previous) === JSON.stringify(current);
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
  function renderChat(force = false) {
    if (!deps) return;
    const s = deps.activeSession();
    if (!s) return;
    if (shownId && shownId !== s.id) {
      searchPanel?.hide();
      document.querySelector('main')?.classList.remove('composer-animating');
    }
    const chat = $('chat');
    document.querySelector('main')?.classList.toggle('empty-conversation', !s.messages.length);
    // Streaming handlers update the existing assistant node in place. Other
    // state projections may request a render while a response is still live;
    // keep that node mounted until the stream has settled or the user changes
    // sessions.
    const hasLiveStream = s.messages.some(
      (message) => message.type === 'asst' && Boolean(message.streamId) && message.finalAnswer === false,
    );
    if (!force && shownId === s.id && hasLiveStream && chat.querySelector('.msg.asst[data-stream-id]')) return;
    history.ensure(s);
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
    if (unchanged(saved, s)) {
      if (changing && saved?.nodes) chat.replaceChildren(...saved.nodes);
    } else {
      const disclosures = changing ? saved?.disclosures : runtime.Process.capture(chat);
      const streams = new Map(
        Array.from(chat.querySelectorAll<HTMLElement>('.msg.asst[data-stream-id]')).map((node) => [
          `${node.dataset.runId}:${node.dataset.streamId}`,
          node,
        ]),
      );
      chat.replaceChildren();
      if (s.messages.length) {
        let start = historyStarts.get(s.id);
        if (start === undefined || start >= s.messages.length)
          start = historyBoundary(s.messages, Math.max(0, s.messages.length - HISTORY_BATCH));
        historyStarts.set(s.id, start);
        addHistoryLoader(chat, s, start);
        for (const m of s.messages.slice(start)) {
          const before = chat.lastElementChild;
          const stream = m.streamId && m.finalAnswer === false ? streams.get(`${m.runId}:${m.streamId}`) : undefined;
          if (stream) chat.append(stream);
          else deps.appendMessageNode(chat, m);
          if (m.entryId && before !== chat.lastElementChild && chat.lastElementChild instanceof HTMLElement)
            chat.lastElementChild.dataset.entryId = m.entryId;
        }
      }
      runtime.Process.restore(chat, disclosures);
    }
    if (history.failed(s.id)) {
      const notice = document.createElement('div');
      notice.className = 'history-loader';
      notice.append(document.createTextNode('Could not load conversation history'));
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'link-btn';
      retry.textContent = 'Retry';
      retry.dataset.historyRetry = '';
      retry.onclick = () => {
        void refreshHistory(s.id);
      };
      notice.append(retry);
      chat.prepend(notice);
    }
    shownId = s.id;
    void runtime.ChatFork?.showOrigin(s.id);
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

  async function refreshHistory(sessionId: string, force = true, background = false): Promise<void> {
    const session = deps?.getSessions().find((item) => item.id === sessionId);
    if (!session) return;
    if (background && sessionId === shownId && (!following || loadingHistory.has(sessionId))) {
      deferredHistory.add(sessionId);
      return;
    }
    if (
      background &&
      searchPanel &&
      !searchPanel.element.hidden &&
      sessionId === shownId &&
      history.hasAfter(sessionId)
    )
      return;
    if (force) forcedHistoryRefresh.add(sessionId);
    deferredHistory.delete(sessionId);
    await history.refresh(session);
  }
  async function observeCommit(event: ServerEvent): Promise<void> {
    if (
      event.type === 'durable_commit' &&
      event.session_id &&
      (event.entry_ids?.length || event.kind === 'branch' || event.kind === 'fork')
    ) {
      // Every durable entry commit can change the visible conversation. The
      // render path preserves an active stream node, so forcing the history
      // reconciliation here also makes queued turns appear immediately after
      // their terminal entry is committed.
      const force = true;
      if (event.kind === 'branch' || event.kind === 'fork') historyRendered.delete(event.session_id);
      await refreshHistory(event.session_id, force, event.kind !== 'branch' && event.kind !== 'fork');
      if (event.session_id === deps.getActiveId())
        runtime.ConversationNav.refresh(event.kind === 'branch' || event.kind === 'fork');
    }
  }
  function historyBoundary(messages: Message[], index: number): number {
    while (index > 0 && messages[index]?.type !== 'user') index--;
    return index;
  }
  function addHistoryLoader(chat: HTMLElement, session: Session, start: number): void {
    historyObserver?.disconnect();
    chat.querySelector(':scope > [data-history-sentinel]')?.remove();
    if (!start && !history.hasMore(session.id)) return;
    const loader = document.createElement('div');
    loader.className = 'history-loader';
    loader.dataset.historySentinel = '';
    loader.setAttribute('role', 'status');
    loader.setAttribute('aria-live', 'polite');
    if (loadingHistory.has(session.id)) {
      const spinner = document.createElement('span');
      spinner.className = 'spinner';
      spinner.setAttribute('aria-label', 'Loading earlier messages');
      loader.append(spinner);
    } else if (history.failed(session.id)) {
      loader.append(
        iconButton('Retry loading earlier messages', 'reload', () => {
          void loadEarlier(true);
        }),
      );
    }
    chat.prepend(loader);
    historyObserver?.observe(loader);
  }

  async function loadEarlier(retry = false): Promise<void> {
    const session = deps.activeSession();
    if (!session || session.id !== shownId || loadingHistory.has(session.id) || (history.failed(session.id) && !retry))
      return;
    const start = historyStarts.get(session.id) || 0;
    if (!start && !history.hasMore(session.id)) return;
    const request = Symbol();
    loadingHistory.set(session.id, request);
    const chat = $('chat');
    following = false;
    scrollIntent = 'history';
    addHistoryLoader(chat, session, start);
    try {
      let prefix: Message[];
      let next = 0;
      if (start) {
        next = historyBoundary(session.messages, Math.max(0, start - HISTORY_BATCH));
        prefix = session.messages.slice(next, start);
      } else {
        const first = session.messages[0];
        await history.earlier(session);
        if (deps.activeSession() !== session || loadingHistory.get(session.id) !== request) return;
        const index = session.messages.findIndex((message) => sameMessage(first, message));
        if (index < 0) {
          views.delete(session.id);
          renderChat(true);
          return;
        }
        prefix = session.messages.slice(0, index);
      }
      if (deps.activeSession() !== session || loadingHistory.get(session.id) !== request) return;
      const areaTop = $('chat-area').getBoundingClientRect().top;
      const anchor = Array.from(chat.children).find(
        (node) =>
          !(node instanceof HTMLElement && node.hasAttribute('data-history-sentinel')) &&
          node.getBoundingClientRect().bottom > areaTop,
      );
      const top = anchor?.getBoundingClientRect().top;
      const fragment = document.createElement('div');
      for (const message of prefix) {
        const previous = fragment.lastElementChild;
        deps.appendMessageNode(fragment, message);
        if (
          message.entryId &&
          previous !== fragment.lastElementChild &&
          fragment.lastElementChild instanceof HTMLElement
        )
          fragment.lastElementChild.dataset.entryId = message.entryId;
      }
      chat.querySelector(':scope > [data-history-sentinel]')?.remove();
      chat.prepend(...fragment.childNodes);
      historyStarts.set(session.id, next);
      if (anchor?.isConnected && top !== undefined)
        setScroll($('chat-area').scrollTop + anchor.getBoundingClientRect().top - top);
      captureView();
    } finally {
      if (loadingHistory.get(session.id) === request) loadingHistory.delete(session.id);
      if (deps.activeSession() === session) {
        addHistoryLoader(chat, session, historyStarts.get(session.id) || 0);
        captureView();
        updateLatest();
      }
    }
  }

  function scrollChat(capture = true) {
    document.querySelector('main')?.classList.toggle('empty-conversation', !deps.activeSession()?.messages.length);
    if (following) setScroll($('chat-area').scrollHeight);
    else if (!userScrollPending && !scrollbarHeld) restoreReading(views.get(shownId));
    if (capture) captureView();
    updateLatest();
    updateNotice();
  }
  function updateLatest() {
    $('back-to-latest').hidden = !history.hasAfter(deps.getActiveId()) && (following || isAtLatest());
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
          open.type = 'button';
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
    historyRendered.delete(id);
    forcedHistoryRefresh.delete(id);
    historyStarts.delete(id);
    loadingHistory.delete(id);
    deferredHistory.delete(id);
    history.forget(id);
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
    historyObserver?.disconnect();
    if (typeof IntersectionObserver !== 'undefined') {
      historyObserver = new IntersectionObserver(
        (entries) => {
          if (!following && entries.some((entry) => entry.isIntersecting)) void loadEarlier();
        },
        { root: area, rootMargin: '100px 0px 0px 0px' },
      );
    }
    area.addEventListener(
      'wheel',
      (e) => {
        if (e.shiftKey || Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
        if (nestedOwnsScroll(e.target, area, e.deltaY)) return;
        if (e.deltaY < 0) {
          stopFollowing();
          if (area.scrollTop < 100) void loadEarlier();
        } else {
          scrollIntent = 'latest';
          userScrollPending = true;
        }
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
      else if (['ArrowDown', 'PageDown', 'End', ' '].includes(e.key)) {
        scrollIntent = 'latest';
        userScrollPending = true;
      }
      if (scrollIntent === 'history' && area.scrollTop < 100) void loadEarlier();
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
        else if (touchY !== undefined && y !== undefined && y < touchY) {
          scrollIntent = 'latest';
          userScrollPending = true;
        }
        touchY = y;
      },
      { passive: true },
    );
    area.addEventListener('scroll', () => {
      const view = views.get(shownId);
      const userMoved = (userScrollPending || scrollbarHeld) && Math.abs(area.scrollTop - scrollOffset) > 0.5;
      if (userMoved) userScrollPending = false;
      if (!userMoved && !userScrollPending && view && view.height !== area.scrollHeight) {
        if (following) setScroll(area.scrollHeight);
        else restoreReading(view);
      } else if (area.scrollTop > scrollOffset && isAtLatest() && (scrollIntent === 'latest' || scrollbarHeld)) {
        following = true;
        scrollIntent = null;
      }
      scrollOffset = area.scrollTop;
      captureView();
      updateLatest();
      if (!following && scrollIntent === 'history' && area.scrollTop < 100) void loadEarlier();
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
        if (
          (e.ctrlKey || e.metaKey) &&
          e.key.toLowerCase() === 'f' &&
          !target?.closest('.document-pane,[role="dialog"]')
        ) {
          e.preventDefault();
          e.stopPropagation();
          searchHistory();
          return;
        }
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
        const drawer = document.querySelector('.changes-drawer.open:not(.workspace .changes-drawer)');
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
    runtime.ConversationNav.init({
      getActiveId: nextDeps.getActiveId,
      getMessages: () => deps.activeSession()?.messages || [],
      reveal: async (entryId: string, message?: Message) => {
        if (message?.entryId || !message) {
          await focusHistory(entryId);
          return;
        }
        const session = deps.activeSession();
        if (!session || !session.messages.includes(message)) throw new Error('Message unavailable');
        historyStarts.set(session.id, historyBoundary(session.messages, session.messages.indexOf(message)));
        views.delete(session.id);
        renderChat(true);
      },
    });
  }

  runtime.ConversationUI = {
    init,
    renderChat,
    refreshHistory,
    searchHistory,
    observeCommit,
    scrollChat,
    captureView,
    isFollowing,
    updateLatest,
    updateNotice,
    stopFollowing,
    refreshApprovals,
    revealApproval,
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
