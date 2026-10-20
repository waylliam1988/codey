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
}
interface Tab {
  ref: DocumentRef;
  data?: DocumentData;
  error?: string;
  source: boolean;
  loading: boolean;
  generation: number;
  scroll: number;
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

  constructor(private deps: PaneDeps) {
    this.element.className = 'document-pane';
    this.element.hidden = true;
    this.element.setAttribute('aria-label', 'Document preview');
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
    if (tab && this.body) tab.scroll = this.body.scrollTop;
  }

  async open(session: string, ref: DocumentRef): Promise<void> {
    this.saveScroll();
    this.session = session;
    let state = this.sessions.get(session);
    if (!state) {
      state = { tabs: new Map(), selected: '', visible: true };
      this.sessions.set(session, state);
    }
    const key = JSON.stringify(ref.kind === 'workspace' ? { kind: ref.kind, path: ref.path } : ref);
    if (!state.tabs.has(key)) state.tabs.set(key, { ref, source: false, loading: false, generation: 0, scroll: 0 });
    else state.tabs.get(key)!.ref = ref;
    state.selected = key;
    state.visible = true;
    if (ref.kind === 'workspace' && ref.line) state.tabs.get(key)!.source = true;
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
      const data = await this.deps.read(session, tab.ref, tab.data?.workspace);
      if (tab.generation === generation) tab.data = data;
    } catch (error) {
      if (tab.generation === generation) tab.error = error instanceof Error ? error.message : String(error);
    } finally {
      if (tab.generation === generation) tab.loading = false;
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
    this.addResizeHandle();
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
      });
      select.setAttribute('role', 'tab');
      select.setAttribute('aria-selected', String(key === state.selected));
      const group = document.createElement('div');
      group.className = 'document-tab';
      const close = iconButton(`Close ${title}`, 'close', () => {
        tab.generation++;
        state.tabs.delete(key);
        if (state.selected === key) state.selected = state.tabs.keys().next().value || '';
        this.render();
      });
      group.append(select, close);
      strip.append(group);
    }
    const closePreview = iconButton('Close preview', 'close', () => this.close());
    strip.append(closePreview);
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
    if (tab.loading || tab.error) {
      const status = document.createElement('div');
      status.className = 'document-status';
      status.setAttribute('role', 'status');
      status.textContent = tab.error || 'Loading document…';
      this.element.append(status);
    }
    const body = document.createElement('div');
    body.className = 'document-body';
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
        const line = tab.ref.kind === 'workspace' ? tab.ref.line : undefined;
        if (line) {
          const lines = content.split('\n');
          const selected = document.createElement('span');
          selected.dataset.line = String(line);
          selected.className = 'selected-line';
          selected.textContent = (lines[line - 1] || '') + (line < lines.length ? '\n' : '');
          pre.append(
            document.createTextNode(lines.slice(0, line - 1).join('\n') + (line > 1 ? '\n' : '')),
            selected,
            document.createTextNode(lines.slice(line).join('\n')),
          );
          requestAnimationFrame(() => {
            if (selected.isConnected) body.scrollTop = selected.offsetTop - body.offsetTop;
          });
        } else pre.textContent = content;
        body.append(pre);
      }
    }
    this.element.append(body);
    body.scrollTop = tab.scroll;
    if (tab.ref.kind === 'workspace' && tab.ref.line && tab.source) {
      const marker = document.createElement('div');
      marker.className = 'document-line';
      marker.textContent = `Line ${tab.ref.line}`;
      toolbar.prepend(marker);
    }
  }
}
