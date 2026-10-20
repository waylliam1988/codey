import { iconButton } from './icon_button';

export type PanelId = 'files' | 'changes' | 'research' | 'review' | 'outputs' | 'history';
type Target = Record<string, string>;
interface Layout {
  open: boolean;
  selected: PanelId;
  tabs: PanelId[];
  width: number;
  full: boolean;
  targets: Partial<Record<PanelId, Target>>;
  scroll: Partial<Record<PanelId, number>>;
}
interface Panel {
  label: string;
  element: HTMLElement;
  wrapper: HTMLElement;
  show(target: Target): void;
}
const KEY = 'codey:workspace:v1';
const ids: PanelId[] = ['files', 'changes', 'research', 'review', 'outputs', 'history'];
const initial = (): Layout => ({
  open: false,
  selected: 'files',
  tabs: [],
  width: 480,
  full: false,
  targets: {},
  scroll: {},
});

/** Only view metadata is stored here. Documents and run facts are read from their scoped APIs. */
export class Workspace {
  readonly element = document.createElement('aside');
  private panels = new Map<PanelId, Panel>();
  private layouts = new Map<string, Layout>();
  private session = '';
  private suspended = false;
  private restoring = false;
  private returnFocus: HTMLElement | null = null;
  private header = document.createElement('div');
  private body = document.createElement('div');
  private notice = document.createElement('div');
  private drag: { id: number; width: number } | null = null;
  private handle = document.createElement('div');

  constructor() {
    this.element.className = 'workspace';
    this.element.hidden = true;
    this.element.setAttribute('aria-label', 'Workspace');
    this.header.className = 'workspace-header';
    this.body.className = 'workspace-panels';
    this.notice.className = 'workspace-notice';
    this.notice.setAttribute('role', 'status');
    this.handle.className = 'workspace-resize';
    this.handle.tabIndex = 0;
    this.handle.setAttribute('role', 'separator');
    this.handle.setAttribute('aria-label', 'Resize workspace');
    this.handle.setAttribute('aria-orientation', 'vertical');
    this.element.append(this.handle, this.header, this.notice, this.body);
    try {
      const records: unknown = JSON.parse(localStorage.getItem(KEY) || '[]');
      if (!Array.isArray(records)) throw new Error('Invalid layouts');
      for (const [session, layout] of records.slice(-40)) {
        if (typeof session !== 'string' || !layout || !ids.includes(layout.selected) || !Array.isArray(layout.tabs))
          continue;
        const clean = initial();
        clean.open = layout.open === true;
        clean.full = layout.full === true;
        clean.selected = layout.selected;
        clean.tabs = [...new Set<PanelId>(layout.tabs.filter((id: PanelId) => ids.includes(id)))];
        if (Number.isFinite(layout.width)) clean.width = Math.max(320, Math.min(900, layout.width));
        for (const id of ids) {
          const target = layout.targets?.[id];
          if (target && typeof target === 'object') {
            clean.targets[id] = Object.fromEntries(
              Object.entries(target).filter(([, value]) => typeof value === 'string' && value.length <= 4096),
            ) as Target;
          }
          if (Number.isFinite(layout.scroll?.[id])) clean.scroll[id] = Math.max(0, layout.scroll[id]);
        }
        this.layouts.set(session, clean);
      }
    } catch {
      this.notice.textContent = 'Workspace layout could not be restored. Using the default layout.';
    }
    this.handle.onpointerdown = (event) => {
      if (event.button !== 0 || this.drag) return;
      event.preventDefault();
      this.drag = { id: event.pointerId, width: this.layout.width };
      this.handle.setPointerCapture(event.pointerId);
    };
    this.handle.onpointermove = (event) => {
      if (this.drag?.id === event.pointerId) this.resize(innerWidth - event.clientX);
    };
    this.handle.onpointerup = (event) => {
      if (this.drag?.id === event.pointerId) this.finishResize();
    };
    this.handle.onpointercancel = this.handle.onlostpointercapture = () => this.finishResize(true);
    this.handle.onkeydown = (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      this.resize(
        event.key === 'Home'
          ? 320
          : event.key === 'End'
            ? 900
            : this.layout.width + (event.key === 'ArrowLeft' ? 32 : -32),
      );
      this.save();
    };
    this.element.addEventListener('keydown', (event) => {
      if (event.key === 'Tab' && (innerWidth <= 1100 || this.layout.full)) {
        const targets = Array.from(
          this.element.querySelectorAll<HTMLElement>(
            'button:not(:disabled),input,textarea,select,[tabindex="0"],a[href]',
          ),
        ).filter((element) => element.getClientRects().length && !element.closest('[inert]'));
        const first = targets[0],
          last = targets.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
      if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      if (this.drag) this.finishResize(true);
      else this.close();
    });
    window.addEventListener('blur', () => this.finishResize(true));
    window.addEventListener('resize', () => this.paintSize());
    window.addEventListener('pagehide', () => {
      this.captureScroll();
      this.save();
    });
  }

  private get layout(): Layout {
    if (!this.layouts.has(this.session)) this.layouts.set(this.session, initial());
    return this.layouts.get(this.session)!;
  }
  register(id: PanelId, label: string, element: HTMLElement, show: Panel['show']): void {
    const wrapper = document.createElement('section');
    wrapper.dataset.workspacePanel = id;
    wrapper.hidden = true;
    wrapper.append(element);
    this.panels.set(id, { label, element, wrapper, show });
    this.body.append(wrapper);
  }
  suspend(): void {
    this.captureScroll();
    this.suspended = true;
  }
  setSession(session: string): void {
    if (session === this.session) {
      this.suspended = false;
      return;
    }
    this.captureScroll();
    this.session = session;
    this.suspended = false;
    this.render();
    if (this.layout.open) this.select(this.layout.selected);
  }
  target(id: PanelId, target: Target): void {
    this.layout.targets[id] = { ...this.layout.targets[id], ...target };
    this.save();
  }
  getTarget(id: PanelId): Target {
    return { ...this.layout.targets[id] };
  }
  reveal(id: PanelId): void {
    if (this.suspended || !this.session) return;
    if (!this.element.hidden && this.layout.selected === id && this.layout.tabs.includes(id)) {
      this.save();
      return;
    }
    this.captureScroll();
    const opening = this.element.hidden;
    if (opening && document.activeElement instanceof HTMLElement) this.returnFocus = document.activeElement;
    this.layout.open = true;
    this.layout.selected = id;
    if (!this.layout.tabs.includes(id)) this.layout.tabs.push(id);
    this.render();
    if (opening)
      this.header.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus({ preventScroll: true });
    this.save();
  }
  select(id: PanelId, target?: Target): void {
    if (target) this.target(id, target);
    this.reveal(id);
    this.panels.get(id)?.show(this.layout.targets[id] || {});
  }
  panelClosed(id: PanelId): void {
    if (!this.suspended && this.layout.selected === id) this.close();
  }
  close(): void {
    if (this.suspended) return;
    this.captureScroll();
    this.layout.open = false;
    const restore = this.element.contains(document.activeElement);
    this.render();
    this.save();
    if (restore)
      (this.returnFocus?.isConnected ? this.returnFocus : document.getElementById('task'))?.focus({
        preventScroll: true,
      });
  }
  private captureScroll(): void {
    // DocumentPane owns scroll per file tab, including asynchronous reloads.
    if (this.restoring || !this.session || this.layout.selected === 'files') return;
    const element = this.panels.get(this.layout.selected)?.element;
    const scroll = element?.querySelector<HTMLElement>('.document-body,.changes-body,.workspace-content');
    if (scroll) this.layout.scroll[this.layout.selected] = scroll.scrollTop;
  }
  private save(): void {
    try {
      localStorage.setItem(KEY, JSON.stringify([...this.layouts].slice(-40)));
    } catch {
      this.notice.textContent = 'Workspace layout could not be saved. This view will remain available until reload.';
    }
  }
  private finishResize(cancel = false): void {
    if (!this.drag) return;
    const drag = this.drag;
    this.drag = null;
    if (cancel) this.resize(drag.width);
    if (this.handle.hasPointerCapture(drag.id)) this.handle.releasePointerCapture(drag.id);
    this.save();
  }
  private resize(width: number): void {
    this.layout.width = Math.max(320, Math.min(900, width));
    this.paintSize();
  }
  private paintSize(): void {
    const sidebar = document.getElementById('aside')?.getBoundingClientRect().width || 0;
    const width = Math.max(320, Math.min(this.layout.width, innerWidth - sidebar - 400));
    document.body.style.setProperty('--workspace-width', `${width}px`);
    this.handle.setAttribute('aria-valuemin', '320');
    this.handle.setAttribute('aria-valuemax', '900');
    this.handle.setAttribute('aria-valuenow', String(Math.round(width)));
  }
  private render(): void {
    const state = this.layout;
    this.element.hidden = !state.open;
    document.body.classList.toggle('workspace-open', state.open);
    this.element.classList.toggle('workspace-full', state.full);
    this.paintSize();
    this.header.replaceChildren();
    const tabs = document.createElement('div');
    tabs.className = 'workspace-tabs';
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Workspace views');
    const available = state.tabs.filter((id) => this.panels.has(id));
    for (const id of available) {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.textContent = this.panels.get(id)!.label;
      tab.id = `workspace-tab-${id}`;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(id === state.selected));
      tab.setAttribute('aria-controls', `workspace-panel-${id}`);
      tab.tabIndex = id === state.selected ? 0 : -1;
      tab.onclick = () => this.select(id);
      tab.onkeydown = (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = available.indexOf(id);
        const next =
          event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? available.length - 1
              : (index + (event.key === 'ArrowRight' ? 1 : -1) + available.length) % available.length;
        this.select(available[next]);
        this.header.querySelector<HTMLElement>(`#workspace-tab-${available[next]}`)?.focus();
      };
      tabs.append(tab);
    }
    const modes = iconButton(state.full ? 'Dock workspace' : 'Expand workspace', state.full ? 'dock' : 'expand', () => {
      state.full = !state.full;
      this.render();
      this.save();
      this.header.querySelector<HTMLElement>('[title$="workspace"]')?.focus();
    });
    this.header.append(
      tabs,
      modes,
      iconButton('Close workspace', 'close', () => this.close()),
    );
    for (const [id, panel] of this.panels) {
      panel.wrapper.id = `workspace-panel-${id}`;
      panel.wrapper.setAttribute('role', 'tabpanel');
      panel.wrapper.setAttribute('aria-labelledby', `workspace-tab-${id}`);
      panel.wrapper.hidden = !state.open || id !== state.selected;
      panel.wrapper.inert = panel.wrapper.hidden;
    }
    this.restoring = true;
    const session = this.session,
      selected = state.selected;
    requestAnimationFrame(() => {
      if (selected !== 'files' && this.session === session && this.layout.selected === selected) {
        const scroll = this.panels
          .get(selected)
          ?.element.querySelector<HTMLElement>('.document-body,.changes-body,.workspace-content');
        if (scroll) scroll.scrollTop = state.scroll[selected] || 0;
      }
      this.restoring = false;
    });
  }
}
