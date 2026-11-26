import { iconButton } from '../render/icon_button';
import { DocumentTabsEvent } from './document_pane';

export type PanelId = 'files' | 'changes' | 'research' | 'context' | 'review' | 'outputs' | 'history';
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
const ids: PanelId[] = ['files', 'changes', 'research', 'context', 'review', 'outputs', 'history'];
function isPanelId(value: unknown): value is PanelId {
  return typeof value === 'string' && ids.some((id) => id === value);
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
const initial = (): Layout => ({
  open: false,
  selected: 'files',
  tabs: [],
  width: 448,
  full: false,
  targets: {},
  scroll: {},
});

/** Only view metadata is stored here. Documents and run facts are read from their scoped APIs. */
export class Workspace {
  readonly element = document.createElement('aside');
  readonly toggleButton = iconButton('Show right sidebar', 'workspace', () => {
    if (!this.element.hidden) this.close();
    else if (this.layout.tabs.length || !this.defaultTabs.length) this.select(this.layout.selected);
    else {
      this.returnFocus = this.toggleButton;
      this.layout.open = true;
      this.render();
      this.header.querySelector<HTMLElement>('[aria-label="Open in workspace"]')?.focus();
      this.save();
    }
  });
  private panels = new Map<PanelId, Panel>();
  private documentTabs: HTMLElement | null = null;
  private menu = document.createElement('div');
  private layouts = new Map<string, Layout>();
  private session = '';
  private suspended = false;
  private restoring = false;
  private returnFocus: HTMLElement | null = null;
  private header = document.createElement('div');
  private body = document.createElement('div');
  private notice = document.createElement('div');
  private empty = document.createElement('p');
  private counts = new Map<PanelId, number>();
  private drag: { id: number; width: number } | null = null;
  private handle = document.createElement('div');

  constructor(private readonly defaultTabs: PanelId[] = []) {
    this.element.id = 'right-workspace';
    this.element.className = 'workspace';
    this.element.hidden = true;
    this.element.setAttribute('aria-label', 'Workspace');
    this.empty.className = 'workspace-empty';
    this.empty.textContent = 'Open a view using the plus button.';
    this.body.append(this.empty);
    this.toggleButton.id = 'workspace-toggle';
    this.toggleButton.disabled = true;
    this.toggleButton.setAttribute('aria-controls', this.element.id);
    this.toggleButton.setAttribute('aria-expanded', 'false');
    this.header.className = 'workspace-header';
    this.header.addEventListener(
      'keydown',
      (event) => {
        if (!(event.target instanceof Element) || event.target.getAttribute('role') !== 'tab') return;
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        event.stopPropagation();
        const tabs = Array.from(this.header.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
        const current = tabs.findIndex((tab) => tab === event.target);
        const index =
          event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? tabs.length - 1
              : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        tabs[index]?.click();
        this.header.querySelector<HTMLElement>('[aria-selected="true"]')?.focus();
      },
      { capture: true },
    );
    this.body.className = 'workspace-panels';
    this.notice.className = 'workspace-notice';
    this.notice.setAttribute('role', 'status');
    this.handle.className = 'workspace-resize';
    this.handle.tabIndex = 0;
    this.handle.setAttribute('role', 'separator');
    this.handle.setAttribute('aria-label', 'Resize workspace');
    this.handle.setAttribute('aria-orientation', 'vertical');
    this.menu.className = 'workspace-menu';
    this.menu.hidden = true;
    this.element.append(this.handle, this.header, this.menu, this.notice, this.body);
    document.addEventListener('pointerdown', (event) => {
      if (event.target instanceof Node && !this.header.contains(event.target) && !this.menu.contains(event.target))
        this.hideMenu();
    });
    try {
      const records: unknown = JSON.parse(localStorage.getItem(KEY) || '[]');
      if (!Array.isArray(records)) throw new Error('Invalid layouts');
      const entries: unknown[] = records;
      for (const entry of entries.slice(-40)) {
        if (!Array.isArray(entry)) continue;
        const pair: unknown[] = entry;
        const [session, layout] = pair;
        if (
          typeof session !== 'string' ||
          !isRecord(layout) ||
          !isPanelId(layout.selected) ||
          !Array.isArray(layout.tabs)
        )
          continue;
        const clean = initial();
        clean.open = layout.open === true;
        clean.full = layout.full === true;
        clean.selected = layout.selected;
        const tabs: unknown[] = layout.tabs;
        clean.tabs = [...new Set(tabs.filter(isPanelId))];
        if (typeof layout.width === 'number' && Number.isFinite(layout.width))
          clean.width = Math.max(320, Math.min(900, layout.width));
        for (const id of ids) {
          const target = isRecord(layout.targets) ? layout.targets[id] : undefined;
          if (isRecord(target)) {
            const values: Target = {};
            for (const [key, value] of Object.entries(target)) {
              if (typeof value === 'string' && value.length <= 4096) values[key] = value;
            }
            clean.targets[id] = values;
          }
          const scroll = isRecord(layout.scroll) ? layout.scroll[id] : undefined;
          if (typeof scroll === 'number' && Number.isFinite(scroll)) clean.scroll[id] = Math.max(0, scroll);
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
      if (!this.menu.hidden) {
        this.hideMenu();
        this.header.querySelector<HTMLElement>('[aria-label="Open in workspace"]')?.focus();
      } else if (this.drag) this.finishResize(true);
      else if (this.layout.full) {
        this.layout.full = false;
        this.render();
        this.save();
      } else this.close();
    });
    window.addEventListener('blur', () => this.finishResize(true));
    window.addEventListener('resize', () => this.paintSize());
    window.addEventListener('pagehide', () => {
      this.captureScroll();
      this.save();
    });
  }

  private get layout(): Layout {
    if (!this.layouts.has(this.session)) this.layouts.set(this.session, { ...initial(), tabs: [...this.defaultTabs] });
    return this.layouts.get(this.session)!;
  }
  register(id: PanelId, label: string, element: HTMLElement, show: Panel['show']): void {
    const wrapper = document.createElement('section');
    wrapper.dataset.workspacePanel = id;
    wrapper.hidden = true;
    wrapper.append(element);
    this.panels.set(id, { label, element, wrapper, show });
    this.body.append(wrapper);
    if (id === 'files')
      element.addEventListener('document-tabs', (event) => {
        if (!(event instanceof DocumentTabsEvent)) return;
        const restoreFocus = this.header.contains(document.activeElement);
        this.documentTabs = event.strip;
        event.strip.removeAttribute('role');
        for (const tab of event.strip.querySelectorAll<HTMLElement>('[role="tab"]')) {
          tab.dataset.documentSelected = tab.getAttribute('aria-selected') || 'false';
        }
        event.strip.addEventListener(
          'click',
          (click) => {
            if (click.target instanceof Element && click.target.closest('[role="tab"]')) this.reveal('files');
          },
          { capture: true },
        );
        this.render();
        if (restoreFocus)
          this.header.querySelector<HTMLElement>('[aria-selected="true"]')?.focus({ preventScroll: true });
      });
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
    if (this.layout.open && this.layout.tabs.includes(this.layout.selected)) this.select(this.layout.selected);
  }

  count(id: PanelId, value: number): void {
    this.counts.set(id, value);
    const label = this.header.querySelector<HTMLElement>(`#workspace-tab-${id} .workspace-tab-count`);
    if (label) label.textContent = value ? String(value) : '';
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
    if (!this.suspended && this.layout.selected === id) this.closeTab(id);
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
  private hideMenu(): void {
    this.menu.hidden = true;
    this.header.querySelector('[aria-label="Open in workspace"]')?.setAttribute('aria-expanded', 'false');
  }
  private closeTab(id: PanelId): void {
    this.captureScroll();
    this.layout.tabs = this.layout.tabs.filter((tab) => tab !== id);
    if (this.layout.selected === id) {
      const next = this.layout.tabs.at(-1);
      if (next) this.select(next);
      else this.render();
    } else this.render();
    this.save();
    if (!this.element.hidden)
      (
        this.header.querySelector<HTMLElement>('[aria-selected="true"]') ??
        this.header.querySelector<HTMLElement>('[aria-label="Open in workspace"]')
      )?.focus();
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
    const toggleLabel = state.open ? 'Hide right sidebar' : 'Show right sidebar';
    this.toggleButton.disabled = !this.session;
    this.toggleButton.title = toggleLabel;
    this.toggleButton.setAttribute('aria-label', toggleLabel);
    this.toggleButton.setAttribute('aria-expanded', String(state.open));
    document.body.classList.toggle('workspace-open', state.open);
    this.element.classList.toggle('workspace-full', state.full);
    this.paintSize();
    this.header.replaceChildren();
    this.menu.replaceChildren();
    this.hideMenu();
    const tabs = document.createElement('div');
    tabs.className = 'workspace-tabs';
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Workspace views');
    const available = [...new Set(state.tabs)].filter((id) => this.panels.has(id));
    for (const id of available) {
      if (id === 'files' && this.documentTabs?.childElementCount) {
        this.documentTabs.classList.add('workspace-document-tabs');
        for (const tab of this.documentTabs.querySelectorAll<HTMLElement>('[role="tab"]')) {
          tab.setAttribute(
            'aria-selected',
            String(state.selected === 'files' && tab.dataset.documentSelected === 'true'),
          );
          tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1;
        }
        tabs.append(this.documentTabs);
        continue;
      }
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.textContent = this.panels.get(id)!.label;
      const count = document.createElement('span');
      count.className = 'workspace-tab-count';
      count.textContent = String(this.counts.get(id) || '');
      tab.append(count);
      tab.id = `workspace-tab-${id}`;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(id === state.selected));
      tab.setAttribute('aria-controls', `workspace-panel-${id}`);
      tab.tabIndex = id === state.selected ? 0 : -1;
      tab.onclick = () => {
        this.select(id);
        this.header.querySelector<HTMLElement>(`#workspace-tab-${id}`)?.focus({ preventScroll: true });
      };
      const group = document.createElement('div');
      group.className = 'workspace-tab';
      group.append(
        tab,
        iconButton(`Close ${this.panels.get(id)!.label}`, 'close', () => this.closeTab(id)),
      );
      tabs.append(group);
    }
    const modes = iconButton(state.full ? 'Dock workspace' : 'Expand workspace', state.full ? 'dock' : 'expand', () => {
      state.full = !state.full;
      this.render();
      this.save();
      this.header.querySelector<HTMLElement>('[title$="workspace"]')?.focus();
    });
    const open = iconButton('Open in workspace', 'plus', () => {
      this.menu.hidden = !this.menu.hidden;
      open.setAttribute('aria-expanded', String(!this.menu.hidden));
      if (!this.menu.hidden) this.menu.querySelector<HTMLElement>('button')?.focus();
    });
    open.setAttribute('aria-expanded', 'false');
    for (const [id, panel] of this.panels) {
      const item = document.createElement('button');
      item.type = 'button';
      item.textContent = panel.label;
      item.onclick = () => {
        this.select(id);
        this.header.querySelector<HTMLElement>('[aria-selected="true"]')?.focus();
      };
      this.menu.append(item);
    }
    this.header.append(
      this.backButton(),
      tabs,
      open,
      modes,
      iconButton('Close workspace', 'close', () => this.close()),
    );
    this.empty.hidden = !state.open || available.length > 0;
    for (const [id, panel] of this.panels) {
      panel.wrapper.id = `workspace-panel-${id}`;
      panel.wrapper.setAttribute('role', 'tabpanel');
      panel.wrapper.setAttribute('aria-label', panel.label);
      panel.wrapper.hidden = !state.open || id !== state.selected || !available.includes(id);
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

  private backButton(): HTMLButtonElement {
    const back = iconButton('Back to conversation', 'arrow-left', () => this.close());
    back.classList.add('workspace-back');
    return back;
  }
}
