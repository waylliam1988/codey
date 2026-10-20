import { iconButton } from './icon_button';

export type DocumentRef = { kind: 'workspace'; path: string; line?: number } | { kind: 'research'; note_id: string };
interface DocumentData {
  title: string;
  path: string;
  content: string;
  format: string;
  version: string;
  workspace: string;
}
interface PaneDeps {
  read(session: string, ref: DocumentRef, workspace?: string): Promise<DocumentData>;
  markdown(container: HTMLElement, text: string, scope?: { path: string; workspace: string; sessionId: string }): void;
  copy(text: string): Promise<boolean>;
  visibility?(visible: boolean): void;
  docked?: boolean;
}
interface Tab {
  ref: DocumentRef;
  data?: DocumentData;
  error?: string;
  source: boolean;
  loading: boolean;
  generation: number;
  scroll: number;
  wrap?: boolean;
  workspace?: string;
  reveal?: number;
}
interface SessionTabs {
  tabs: Map<string, Tab>;
  selected: string;
  visible: boolean;
}

function button(label: string, action: () => void): HTMLButtonElement {
  const el = document.createElement('button');
  el.type = 'button';
  el.textContent = label;
  el.setAttribute('aria-label', label);
  el.onclick = action;
  return el;
}

export class DocumentPane {
  readonly element = document.createElement('aside');
  private sessions = new Map<string, SessionTabs>();
  private session = '';
  private body: HTMLElement | null = null;
  private storageError = '';
  private readonly storageKey = 'codey:document-tabs:v1';

  constructor(private deps: PaneDeps) {
    this.element.className = 'document-pane';
    this.element.hidden = true;
    this.element.setAttribute('aria-label', 'Document preview');
    try {
      const stored: unknown = JSON.parse(localStorage.getItem(this.storageKey) || '[]');
      if (!Array.isArray(stored)) throw new Error('Invalid document tabs');
      for (const [session, state] of stored.slice(-40)) {
        if (typeof session !== 'string' || !state || !Array.isArray(state.tabs)) continue;
        const tabs = new Map<string, Tab>();
        for (const raw of state.tabs.slice(0, 24)) {
          const ref = raw?.ref;
          if (
            !ref ||
            !(
              (ref.kind === 'workspace' && typeof ref.path === 'string') ||
              (ref.kind === 'research' && typeof ref.note_id === 'string')
            )
          )
            continue;
          const key = this.key(ref);
          tabs.set(key, {
            ref,
            source: raw.source === true,
            wrap: raw.wrap === true,
            scroll: Number.isFinite(raw.scroll) ? Math.max(0, raw.scroll) : 0,
            workspace: typeof raw.workspace === 'string' ? raw.workspace : undefined,
            generation: 0,
            loading: false,
          });
        }
        this.sessions.set(session, {
          tabs,
          selected: tabs.has(state.selected) ? state.selected : tabs.keys().next().value || '',
          visible: state.visible === true,
        });
      }
    } catch {
      this.storageError = 'Document tabs could not be restored.';
    }
    window.addEventListener('pagehide', () => this.saveScroll());
  }

  private key(ref: DocumentRef): string {
    return JSON.stringify(ref.kind === 'workspace' ? { kind: ref.kind, path: ref.path } : ref);
  }

  private persist(): void {
    try {
      localStorage.setItem(
        this.storageKey,
        JSON.stringify(
          [...this.sessions].slice(-40).map(([session, state]) => [
            session,
            {
              ...state,
              tabs: [...state.tabs.values()].slice(0, 24).map((tab) => ({
                ref: tab.ref,
                source: tab.source,
                wrap: tab.wrap,
                scroll: tab.scroll,
                workspace: tab.data?.workspace || tab.workspace,
              })),
            },
          ]),
        ),
      );
    } catch {
      this.storageError = 'Document tabs could not be saved. They remain available until reload.';
    }
  }

  show(session: string): void {
    this.setSession(session);
    const state = this.sessions.get(session);
    if (!state?.tabs.size) {
      this.element.hidden = false;
      this.element.textContent = 'Open a file from the conversation to preview it here.';
      return;
    }
    state.visible = true;
    this.render();
    const tab = state.tabs.get(state.selected);
    if (tab && !tab.data && !tab.loading) void this.load(session, state.selected);
  }

  setSession(session: string): void {
    if (this.session === session) return;
    this.saveScroll();
    this.session = session;
    this.render();
  }

  private saveScroll(): void {
    const state = this.sessions.get(this.session);
    const tab = state?.tabs.get(state.selected);
    if (tab?.data && this.body && this.visible()) tab.scroll = this.body.scrollTop;
    this.persist();
  }

  private visible(): boolean {
    return (
      !this.element.hidden &&
      !this.element.closest<HTMLElement>('.workspace')?.hidden &&
      !this.element.closest<HTMLElement>('[data-workspace-panel]')?.hidden
    );
  }

  async open(session: string, ref: DocumentRef): Promise<void> {
    this.saveScroll();
    this.session = session;
    let state = this.sessions.get(session);
    if (!state) {
      state = { tabs: new Map(), selected: '', visible: true };
      this.sessions.set(session, state);
    }
    const key = this.key(ref);
    if (!state.tabs.has(key)) state.tabs.set(key, { ref, source: false, loading: false, generation: 0, scroll: 0 });
    else state.tabs.get(key)!.ref = ref;
    state.selected = key;
    state.visible = true;
    this.deps.visibility?.(true);
    if (ref.kind === 'workspace' && ref.line) {
      state.tabs.get(key)!.source = true;
      state.tabs.get(key)!.reveal = ref.line;
    }
    await this.load(session, key);
  }

  fileLink(path: string, line?: number): DocumentRef {
    const state = this.sessions.get(this.session);
    const tab = state?.tabs.get(state.selected);
    if (tab?.ref.kind === 'workspace' && tab.data && !/^(?:[a-z]:[\\/]|\/)/i.test(path)) {
      const parent = tab.data.path.split('/').slice(0, -1).join('/');
      if (parent) path = `${parent}/${path}`;
    }
    return { kind: 'workspace', path, ...(line ? { line } : {}) };
  }

  close(): void {
    this.saveScroll();
    const state = this.sessions.get(this.session);
    if (state) state.visible = false;
    this.deps.visibility?.(false);
    this.persist();
    this.render();
  }

  async reload(): Promise<void> {
    this.saveScroll();
    const state = this.sessions.get(this.session);
    if (state) await this.load(this.session, state.selected);
  }

  private async load(session: string, key: string): Promise<void> {
    const tab = this.sessions.get(session)?.tabs.get(key);
    if (!tab) return;
    const generation = ++tab.generation;
    tab.loading = true;
    tab.error = undefined;
    this.render();
    try {
      const data = await this.deps.read(session, tab.ref, tab.data?.workspace || tab.workspace);
      if (tab.generation === generation) tab.data = data;
    } catch (error) {
      if (tab.generation === generation) tab.error = error instanceof Error ? error.message : String(error);
    } finally {
      if (tab.generation === generation) tab.loading = false;
      this.persist();
      if (this.session === session && this.sessions.get(session)?.selected === key && tab.generation === generation)
        this.render();
    }
  }

  private addResizeHandle(): void {
    const handle = document.createElement('div');
    handle.className = 'document-resize';
    handle.tabIndex = 0;
    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-label', 'Resize document preview');
    handle.setAttribute('aria-orientation', 'vertical');
    const resize = (width: number) => {
      const value = Math.round(Math.max(320, Math.min(window.innerWidth - 368, width)));
      document.body.style.setProperty('--document-width', `${value}px`);
      handle.setAttribute('aria-valuenow', String(value));
    };
    handle.onkeydown = (event) => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        resize(this.element.getBoundingClientRect().width + (event.key === 'ArrowLeft' ? 32 : -32));
      }
    };
    handle.onpointerdown = (event) => {
      handle.setPointerCapture(event.pointerId);
    };
    handle.onpointermove = (event) => {
      if (handle.hasPointerCapture(event.pointerId)) resize(window.innerWidth - event.clientX);
    };
    handle.onpointerup = (event) => {
      if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    };
    this.element.append(handle);
  }

  private render(): void {
    const state = this.sessions.get(this.session);
    this.element.hidden = !state?.visible || !state.tabs.size;
    this.element.replaceChildren();
    if (!this.deps.docked) this.addResizeHandle();
    this.body = null;
    if (this.element.hidden || !state) return;
    const strip = document.createElement('div');
    strip.className = 'document-tabs';
    strip.setAttribute('role', 'tablist');
    for (const [key, tab] of state.tabs) {
      const title =
        tab.data?.title ||
        (tab.ref.kind === 'workspace' ? tab.ref.path.split('/').pop() : tab.ref.note_id) ||
        'Document';
      const select = button(title, () => {
        this.saveScroll();
        state.selected = key;
        this.render();
        if (!tab.data && !tab.loading) void this.load(this.session, key);
      });
      select.setAttribute('role', 'tab');
      select.setAttribute('aria-selected', String(key === state.selected));
      select.tabIndex = key === state.selected ? 0 : -1;
      select.onkeydown = (event) => {
        const keys = [...state.tabs.keys()];
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = keys.indexOf(key);
        const next =
          event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? keys.length - 1
              : (index + (event.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length;
        this.saveScroll();
        state.selected = keys[next];
        this.render();
        this.element.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus();
        const selected = state.tabs.get(state.selected)!;
        if (!selected.data && !selected.loading) void this.load(this.session, state.selected);
      };
      const group = document.createElement('div');
      group.className = 'document-tab';
      const close = iconButton(`Close ${title}`, 'close', () => {
        const focused = group.contains(document.activeElement);
        tab.generation++;
        state.tabs.delete(key);
        if (state.selected === key) state.selected = state.tabs.keys().next().value || '';
        this.persist();
        this.render();
        if (focused) this.element.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus();
        if (!state.tabs.size) this.deps.visibility?.(false);
      });
      group.append(select, close);
      strip.append(group);
    }
    const closePreview = iconButton('Close preview', 'close', () => this.close());
    if (!this.deps.docked) strip.append(closePreview);
    this.element.append(strip);
    const tab = state.tabs.get(state.selected);
    if (!tab) return;
    const toolbar = document.createElement('div');
    toolbar.className = 'document-toolbar';
    const path = document.createElement('span');
    path.className = 'document-breadcrumb';
    path.textContent = (tab.data?.path || (tab.ref.kind === 'workspace' ? tab.ref.path : '')).split('/').join(' / ');
    path.title = tab.data?.path || '';
    toolbar.append(
      path,
      iconButton('Reload', 'reload', () => {
        void this.reload();
      }),
    );
    if (tab.data) {
      if (tab.data.format === 'markdown' || tab.data.format === 'json')
        toolbar.append(
          button(tab.source ? 'View preview' : 'View source', () => {
            this.saveScroll();
            tab.source = !tab.source;
            this.render();
          }),
        );
      toolbar.append(
        iconButton('Copy document', 'copy', () => {
          void this.deps.copy(tab.data!.content);
        }),
      );
    }
    this.element.append(toolbar);
    if (tab.loading || tab.error || this.storageError) {
      const status = document.createElement('div');
      status.className = 'document-status';
      status.setAttribute('role', 'status');
      status.textContent = tab.error || this.storageError || 'Loading document…';
      this.element.append(status);
    }
    const body = document.createElement('div');
    body.className = 'document-body';
    body.classList.toggle('wrap', tab.wrap === true);
    body.setAttribute('role', 'tabpanel');
    body.tabIndex = 0;
    this.body = body;
    if (tab.data && !tab.error) {
      if (tab.data.format === 'markdown' && !tab.source) {
        body.classList.add('md');
        this.deps.markdown(body, tab.data.content, {
          sessionId: this.session,
          path: tab.ref.kind === 'workspace' ? tab.data.path : '',
          workspace: tab.data.workspace,
        });
      } else {
        const pre = document.createElement('pre');
        let content = tab.data.content;
        if (tab.data.format === 'json' && !tab.source) {
          try {
            content = JSON.stringify(JSON.parse(content), null, 2);
          } catch {
            const notice = document.createElement('div');
            notice.textContent = 'Invalid JSON · showing source';
            body.append(notice);
          }
        }
        const lines = content.split('\n');
        const requested = tab.ref.kind === 'workspace' ? tab.ref.line : undefined;
        const line = requested ? Math.max(1, Math.min(lines.length, requested)) : 0;
        pre.className = 'source-lines';
        const fragment = document.createDocumentFragment();
        for (let index = 0; index < lines.length; index++) {
          const row = document.createElement('span');
          row.dataset.line = String(index + 1);
          row.classList.toggle('selected-line', index + 1 === line);
          row.textContent = lines[index] + (index + 1 < lines.length ? '\n' : '');
          fragment.append(row);
        }
        pre.append(fragment);
        body.append(pre);
      }
    }
    const tools = document.createElement('div');
    tools.className = 'document-tools';
    const find = document.createElement('input');
    find.type = 'search';
    find.placeholder = 'Find in document';
    find.setAttribute('aria-label', 'Find in document');
    const count = document.createElement('span');
    count.setAttribute('role', 'status');
    let matches: HTMLElement[] = [],
      current = -1;
    const move = (direction: number) => {
      if (!matches.length) return;
      current = (current + direction + matches.length) % matches.length;
      matches.forEach((match, index) => match.classList.toggle('current-match', index === current));
      matches[current].scrollIntoView({ block: 'center' });
      tab.scroll = body.scrollTop;
      count.textContent = `${current + 1} / ${matches.length}`;
    };
    find.oninput = () => {
      body
        .querySelectorAll('mark.document-match')
        .forEach((mark) => mark.replaceWith(document.createTextNode(mark.textContent || '')));
      body.normalize();
      matches = [];
      current = -1;
      const query = find.value.toLocaleLowerCase();
      if (!query) {
        count.textContent = '';
        return;
      }
      const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
      const nodes: Text[] = [];
      while (walker.nextNode()) nodes.push(walker.currentNode as Text);
      for (const text of nodes) {
        if (text.parentElement?.closest('button,svg,script,style')) continue;
        const content = text.data;
        const lower = content.toLocaleLowerCase();
        let from = 0,
          at = lower.indexOf(query);
        if (at < 0) continue;
        const parts = document.createDocumentFragment();
        while (at >= 0 && matches.length < 1000) {
          parts.append(document.createTextNode(content.slice(from, at)));
          const mark = document.createElement('mark');
          mark.className = 'document-match';
          mark.textContent = content.slice(at, at + query.length);
          parts.append(mark);
          matches.push(mark);
          from = at + query.length;
          at = lower.indexOf(query, from);
        }
        parts.append(document.createTextNode(content.slice(from)));
        text.replaceWith(parts);
      }
      count.textContent = matches.length
        ? `${matches.length === 1000 ? '1000+' : matches.length} matches`
        : 'No matches';
    };
    find.onkeydown = (event) => {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        move(event.shiftKey ? -1 : 1);
      }
      if (event.key === 'Escape' && find.value) {
        event.preventDefault();
        event.stopPropagation();
        find.value = '';
        find.dispatchEvent(new Event('input'));
      }
    };
    this.element.onkeydown = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault();
        event.stopPropagation();
        find.focus();
      }
    };
    const wrap = button('Wrap lines', () => {
      tab.wrap = !tab.wrap;
      body.classList.toggle('wrap', tab.wrap);
      wrap.setAttribute('aria-pressed', String(tab.wrap));
      this.persist();
    });
    wrap.setAttribute('aria-pressed', String(tab.wrap === true));
    tools.append(
      find,
      count,
      button('Previous match', () => move(-1)),
      button('Next match', () => move(1)),
      wrap,
    );
    this.element.append(tools, body);
    body.scrollTop = tab.scroll;
    body.onscroll = () => {
      if (tab.data && this.body === body && this.visible()) tab.scroll = body.scrollTop;
    };
    if (tab.reveal && tab.data && !tab.loading) {
      const reveal = tab.reveal;
      tab.reveal = undefined;
      requestAnimationFrame(() => {
        if (!body.isConnected) return;
        const lines = body.querySelectorAll<HTMLElement>('[data-line]');
        const selected = lines[Math.max(0, Math.min(lines.length - 1, reveal - 1))];
        if (selected) body.scrollTop = selected.offsetTop - body.offsetTop;
        tab.scroll = body.scrollTop;
      });
    }
    if (tab.ref.kind === 'workspace' && tab.ref.line && tab.source) {
      const marker = document.createElement('div');
      marker.className = 'document-line';
      marker.textContent = `Line ${tab.ref.line}`;
      toolbar.prepend(marker);
    }
  }
}
