import { runtime } from './runtime';
import { loadNavigationIndex, type NavigationItem } from './conversation_nav_index';
import type { Message } from './types/domain';
export interface ConversationNavDeps {
  getActiveId(): string;
  getMessages(): Message[];
  reveal(entryId: string, message?: Message): Promise<void>;
}
/* A quiet index of user questions; native chat selection and drafts stay untouched. */
(function () {
  'use strict';
  interface NavDeps extends ConversationNavDeps {}
  interface Entry {
    id: string;
    text: string;
    message?: Message;
    anchor: HTMLElement | null;
    button: HTMLButtonElement;
  }
  interface Bounds {
    left: number;
    right: number;
    top: number;
    bottom: number;
    width: number;
    height: number;
  }
  let deps: NavDeps | null = null;
  let area!: HTMLElement;
  let chat!: HTMLElement;
  let nav!: HTMLElement;
  let preview!: HTMLDivElement;
  let entries: Entry[] = [],
    sessionId = '',
    current = 0,
    hovered = -1,
    focused = -1;
  let frame = 0,
    previewTimer: number | undefined,
    previewSuppressed = false;
  let returnFocus: Element | null = null;
  let index: NavigationItem[] = [];
  let indexRequest: AbortController | undefined;
  let jumpGeneration = 0;
  let dragging: { pointer: number; index: number; moved: boolean; target: HTMLButtonElement } | null = null;
  let suppressClick = false;
  const localIds = new WeakMap<Message, string>();
  let localSequence = 0;
  const previews = new Map<string, string>();
  let bounds: Bounds = { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 };
  const STEP = 10,
    MIN_TURNS = 4;
  const selected = () => (hovered >= 0 ? hovered : focused);
  const plain = (node: Element | null): string => (node?.textContent || '').replace(/\s+/g, ' ').trim();

  function closePreview() {
    if (previewTimer !== undefined) window.clearTimeout(previewTimer);
    previewTimer = undefined;
    preview.hidden = true;
    entries.forEach((entry) => entry.button.removeAttribute('aria-describedby'));
  }
  function dismiss() {
    hovered = focused = -1;
    previewSuppressed = false;
    closePreview();
    paint();
  }
  function paint() {
    const active = selected();
    nav.classList.toggle('selecting', active >= 0);
    entries.forEach((entry, index) => {
      const button = entry.button,
        distance = Math.abs(index - active);
      if (index === current) button.setAttribute('aria-current', 'location');
      else button.removeAttribute('aria-current');
      button.classList.toggle('selected', index === active);
      button.classList.toggle('near', active >= 0 && distance === 1);
      button.tabIndex = index === (focused >= 0 ? focused : current) ? 0 : -1;
      const marker = button.firstElementChild;
      if (!(marker instanceof HTMLElement)) return;
      if (active < 0) marker.style.removeProperty('width');
      else
        marker.style.width =
          (distance === 0 ? 28 : distance === 1 ? 22 : distance === 2 ? 15 : distance === 3 ? 9 : 6) + 'px';
    });
  }
  function answerFor(entry: Entry): string {
    if (!entry.anchor) {
      if (previews.has(entry.id)) return previews.get(entry.id) || '';
      const messages = deps?.getMessages() || [];
      const start = entry.message ? messages.indexOf(entry.message) : -1;
      if (start >= 0)
        for (const message of messages.slice(start + 1)) {
          if (message.type === 'user') break;
          if (message.type === 'asst') return String(message.text || '').slice(0, 500);
        }
      return '';
    }
    for (
      let node = entry.anchor.nextElementSibling;
      node && !node.matches('.msg.user');
      node = node.nextElementSibling
    ) {
      if (node.matches('.msg.asst')) return plain(node.querySelector('.body')).slice(0, 500);
    }
    return '';
  }
  function showPreview(): void {
    const index = selected(),
      entry = entries[index];
    if (!entry || nav.hidden || entry.button.hidden || previewSuppressed) return;
    const button = entry.button,
      rect = button.getBoundingClientRect();
    const title = preview.querySelector<HTMLElement>('.conversation-preview-title');
    const body = preview.querySelector<HTMLElement>('.conversation-preview-body');
    if (!title || !body) return;
    title.textContent = entry.text;
    body.textContent = answerFor(entry);
    body.hidden = !body.textContent;
    preview.hidden = false;
    preview.style.left =
      Math.max(bounds.left + 8, Math.min(rect.right + 10, bounds.right - preview.offsetWidth - 12)) + 'px';
    preview.style.top =
      Math.max(bounds.top + 12, Math.min(rect.top - 25, bounds.bottom - preview.offsetHeight - 12)) + 'px';
    button.setAttribute('aria-describedby', preview.id);
    if (!entry.anchor && !entry.message && !previews.has(entry.id)) void loadPreview(entry);
  }
  async function loadPreview(entry: Entry): Promise<void> {
    const activeSession = sessionId;
    previews.set(entry.id, 'Loading preview…');
    const body = preview.querySelector<HTMLElement>('.conversation-preview-body');
    if (body) {
      body.hidden = false;
      body.textContent = 'Loading preview…';
    }
    try {
      const response = await fetch(
        `/api/conversation_tree?${new URLSearchParams({ session_id: activeSession, entry_id: entry.id })}`,
        { signal: AbortSignal.timeout(15000) },
      );
      if (!response.ok) throw new Error('Preview unavailable');
      const page = await response.json();
      const at = page.items.findIndex((item: { id: string }) => item.id === entry.id);
      let answer = '';
      for (const item of page.items.slice(at + 1)) {
        if (item.type === 'user') break;
        if (item.type === 'asst') answer = String(item.text || '').slice(0, 500);
      }
      if (activeSession !== sessionId) return;
      previews.set(entry.id, answer);
    } catch {
      if (activeSession !== sessionId) return;
      previews.set(entry.id, 'Preview unavailable');
    }
    if (entries[selected()]?.id === entry.id && !preview.hidden) showPreview();
  }
  function requestPreview() {
    closePreview();
    if (selected() >= 0 && !previewSuppressed) previewTimer = window.setTimeout(showPreview, 220);
  }
  function selectPointer(index: number): void {
    if (hovered === index && !previewSuppressed) return;
    hovered = index;
    previewSuppressed = false;
    paint();
    requestPreview();
  }
  async function jump(index: number, scrub = false): Promise<void> {
    const entry = entries[index];
    if (!entry || !deps || deps.getActiveId() !== sessionId) return;
    // Dragging only visits already mounted turns, avoiding a request per tick.
    if (scrub && !entry.anchor?.isConnected) return;
    const generation = ++jumpGeneration,
      identity = sessionId;
    closePreview();
    previewSuppressed = true;
    runtime.ConversationUI.stopFollowing();
    if (!entry.anchor?.isConnected) {
      entry.button.setAttribute('aria-busy', 'true');
      try {
        await deps.reveal(entry.id, entry.message);
      } catch {
        if (identity === sessionId && generation === jumpGeneration) {
          hovered = entries.findIndex((item) => item.id === entry.id);
          previewSuppressed = false;
          previews.set(entry.id, 'Could not load message. Click to retry.');
          showPreview();
        }
        return;
      } finally {
        entry.button.removeAttribute('aria-busy');
      }
      if (identity !== sessionId || generation !== jumpGeneration) return;
      reconcile();
    }
    const anchor = entries.find((item) => item.id === entry.id)?.anchor;
    if (!anchor?.isConnected) return;
    const rect = area.getBoundingClientRect();
    const top = area.scrollTop + anchor.getBoundingClientRect().top - rect.top - 24;
    area.scrollTo({
      top,
      behavior: scrub || matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
    });
    runtime.ConversationUI.captureView();
    schedule();
  }
  function makeEntry(id: string, text: string): Entry {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'conversation-tick';
    button.tabIndex = -1;
    const line = document.createElement('span');
    line.setAttribute('aria-hidden', 'true');
    button.append(line);
    button.addEventListener('pointerdown', (event: PointerEvent) => {
      if (event.button !== 0) return;
      event.preventDefault();
      if (document.activeElement instanceof HTMLElement && nav.contains(document.activeElement)) {
        document.activeElement.blur();
        focused = -1;
      }
    });
    button.addEventListener('click', () => {
      if (suppressClick) {
        suppressClick = false;
        return;
      }
      void jump(entries.findIndex((entry) => entry.button === button));
    });
    return { id, text, anchor: null, button };
  }
  function messageId(message: Message): string {
    if (message.entryId) return message.entryId;
    let id = localIds.get(message);
    if (!id) {
      id = `local-${++localSequence}`;
      localIds.set(message, id);
    }
    return id;
  }
  function refresh(reset = false): void {
    if (deps && deps.getActiveId() !== sessionId) {
      schedule();
      return;
    }
    if (reset) {
      index = [];
      previews.clear();
      jumpGeneration++;
      dismiss();
      schedule();
    }
    indexRequest?.abort();
    const request = new AbortController(),
      identity = sessionId;
    indexRequest = request;
    void loadNavigationIndex(identity, request.signal)
      .then((items) => {
        if (request.signal.aborted || identity !== sessionId) return;
        index = items;
        schedule();
      })
      .catch(() => {
        /* Keep the mounted/local index usable; the next commit retries. */
      });
  }
  function reconcile(): void {
    if (!deps) return;
    const id = deps.getActiveId(),
      changed = id !== sessionId;
    if (changed) {
      sessionId = id;
      index = [];
      previews.clear();
      jumpGeneration++;
      refresh();
      dismiss();
    }
    const old = new Map(entries.map((entry) => [entry.id, entry]));
    const focusIndex = entries.findIndex((entry) => entry.button === document.activeElement);
    const focusId = entries[focusIndex]?.id;
    const hoveredId = entries[hovered]?.id;
    const focusedId = entries[focused]?.id;
    const anchors = Array.from(chat.children).filter(
      (node): node is HTMLElement => node instanceof HTMLElement && node.matches('.msg.user'),
    );
    const messages = deps.getMessages().filter((message) => message.type === 'user');
    const messageMap = new Map(messages.map((message) => [messageId(message), message]));
    const mounted = new Map<string, HTMLElement>();
    messages.slice(-anchors.length || messages.length).forEach((message, i) => {
      if (anchors[i]) mounted.set(messageId(message), anchors[i]);
    });
    for (const anchor of anchors) if (anchor.dataset.entryId) mounted.set(anchor.dataset.entryId, anchor);
    const rows = new Map(index.map((item) => [item.id, item]));
    for (const message of messages)
      rows.set(messageId(message), { id: messageId(message), text: String(message.text || '') });
    const next = Array.from(rows.values());
    const same = next.length === entries.length && next.every((item, i) => item.id === entries[i].id);
    if (!same) {
      entries = next.map((item) => old.get(item.id) || makeEntry(item.id, item.text));
      const buttons = new Set(entries.map((entry) => entry.button));
      for (const button of Array.from(nav.children)) {
        if (button instanceof HTMLButtonElement && !buttons.has(button)) button.remove();
      }
      entries.forEach((entry, index) => {
        if (nav.children[index] !== entry.button) nav.insertBefore(entry.button, nav.children[index] || null);
      });
      const focusedEntry = entries.find((entry) => entry.id === focusId);
      if (!changed && focusedEntry && !nav.contains(document.activeElement)) {
        focusedEntry.button.focus({ preventScroll: true });
      }
      hovered = entries.findIndex((entry) => entry.id === hoveredId);
      focused = entries.findIndex((entry) => entry.id === focusedId);
      closePreview();
    }
    entries.forEach((entry, index) => {
      entry.anchor = mounted.get(entry.id) || null;
      entry.message = messageMap.get(entry.id);
      entry.text = rows.get(entry.id)?.text || '';
      const label = 'Jump to question ' + (index + 1) + ': ' + entry.text.slice(0, 240);
      if (entry.button.getAttribute('aria-label') !== label) entry.button.setAttribute('aria-label', label);
    });
  }
  function availableBounds(): Bounds {
    const rect = area.getBoundingClientRect();
    let right = rect.right;
    document.querySelectorAll<HTMLElement>('.changes-drawer.open:not(.workspace .changes-drawer)').forEach((drawer) => {
      // Drawers overlay rather than resize the chat. Count their final occupied width during animation too.
      right = Math.min(right, innerWidth - drawer.getBoundingClientRect().width);
    });
    return {
      left: rect.left,
      right,
      top: rect.top,
      bottom: rect.bottom,
      width: right - rect.left,
      height: rect.height,
    };
  }
  function update(): void {
    frame = 0;
    reconcile();
    bounds = availableBounds();
    const content = chat.getBoundingClientRect();
    const leftMargin = Math.min(content.left - bounds.left, (bounds.width - content.width) / 2);
    const enough =
      leftMargin >= 48 &&
      bounds.height >= 100 &&
      entries.length >= MIN_TURNS &&
      !document.querySelector('dialog[open]');
    if (!enough) {
      const hadFocus = nav.contains(document.activeElement);
      nav.hidden = true;
      dismiss();
      if (hadFocus) {
        const focusTarget =
          returnFocus?.isConnected && !returnFocus.closest('[inert]') ? returnFocus : document.getElementById('task');
        if (focusTarget instanceof HTMLElement) focusTarget.focus({ preventScroll: true });
      }
      return;
    }
    nav.hidden = false;
    current = Math.max(
      0,
      entries.findIndex((entry) => entry.anchor),
    );
    for (let index = 0; index < entries.length; index++) {
      const anchor = entries[index].anchor;
      if (!anchor) continue;
      if (anchor.getBoundingClientRect().top > bounds.top + 55) break;
      current = index;
    }
    if (area.scrollHeight - area.scrollTop - area.clientHeight < 2) {
      for (let index = entries.length - 1; index >= 0; index--) {
        if (entries[index].anchor) {
          current = index;
          break;
        }
      }
    }
    const height = Math.min(bounds.height * 0.7, 640, entries.length * STEP);
    nav.style.maxHeight = height + 'px';
    nav.style.left = bounds.left + 14 + 'px';
    nav.style.top = bounds.top + (bounds.height - height) / 2 + 'px';
    if (!dragging && hovered < 0 && focused < 0) keepVisible(entries[current]?.button);
    paint();
    if (!preview.hidden) showPreview();
  }
  function keepVisible(button?: HTMLButtonElement): void {
    if (!button) return;
    const top = button.offsetTop;
    if (top < nav.scrollTop) nav.scrollTop = top;
    else if (top + STEP > nav.scrollTop + nav.clientHeight) nav.scrollTop = top + STEP - nav.clientHeight;
  }
  function schedule(): void {
    if (!frame) frame = requestAnimationFrame(update);
  }
  function keydown(event: KeyboardEvent): void {
    if (event.isComposing) return;
    const index = entries.findIndex((entry) => entry.button === event.target);
    if (index < 0) return;
    const keys = ['ArrowUp', 'ArrowDown', 'Home', 'End'];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    hovered = -1;
    previewSuppressed = false;
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? entries.length - 1
          : Math.max(0, Math.min(entries.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
    focused = next;
    update();
    entries[next].button.focus({ preventScroll: true });
    keepVisible(entries[next].button);
    paint();
    requestPreview();
  }
  function init(nextDeps: NavDeps): void {
    if (nav) return;
    deps = nextDeps;
    const areaElement = document.getElementById('chat-area');
    const chatElement = document.getElementById('chat');
    const main = document.querySelector('main');
    if (!areaElement || !chatElement || !main) throw new Error('conversation navigation requires chat DOM');
    area = areaElement;
    chat = chatElement;
    nav = document.createElement('nav');
    nav.id = 'conversation-nav';
    nav.className = 'conversation-nav';
    nav.hidden = true;
    nav.setAttribute('aria-label', 'Conversation navigation');
    preview = document.createElement('div');
    preview.id = 'conversation-preview';
    preview.className = 'conversation-preview';
    preview.setAttribute('role', 'tooltip');
    preview.hidden = true;
    preview.innerHTML = '<div class="conversation-preview-title"></div><div class="conversation-preview-body"></div>';
    main.append(nav, preview);
    nav.addEventListener(
      'pointerdown',
      (event: PointerEvent) => {
        if (event.button !== 0 || event.pointerType === 'touch') return;
        const button = event.target instanceof Element ? event.target.closest('.conversation-tick') : null;
        const index = entries.findIndex((entry) => entry.button === button);
        if (index < 0) return;
        dragging = { pointer: event.pointerId, index, moved: false, target: entries[index].button };
        dragging.target.setPointerCapture(event.pointerId);
        runtime.ConversationUI.stopFollowing();
      },
      true,
    );
    const endDrag = (event: PointerEvent, cancel = false) => {
      if (dragging?.pointer !== event.pointerId) return;
      const state = dragging;
      dragging = null;
      if (state.target.hasPointerCapture(event.pointerId)) state.target.releasePointerCapture(event.pointerId);
      if (state.moved) {
        suppressClick = true;
        if (!cancel) void jump(state.index);
        window.setTimeout(() => {
          suppressClick = false;
        }, 0);
      }
    };
    nav.addEventListener('pointerup', (event) => endDrag(event));
    nav.addEventListener('pointercancel', (event) => endDrag(event, true));
    nav.addEventListener('lostpointercapture', (event) => endDrag(event, true));
    nav.addEventListener('pointermove', (event: PointerEvent) => {
      if (event.pointerType === 'touch') return;
      if (dragging?.pointer === event.pointerId) {
        const rect = nav.getBoundingClientRect();
        if (event.clientY < rect.top + STEP) nav.scrollTop -= STEP * 2;
        else if (event.clientY > rect.bottom - STEP) nav.scrollTop += STEP * 2;
        const index = Math.max(
          0,
          Math.min(entries.length - 1, Math.floor((event.clientY - rect.top + nav.scrollTop) / STEP)),
        );
        if (index !== dragging.index) {
          dragging.index = index;
          dragging.moved = true;
          selectPointer(index);
          void jump(index, true);
        }
        return;
      }
      const button = event.target instanceof Element ? event.target.closest('.conversation-tick') : null;
      if (button) selectPointer(entries.findIndex((entry) => entry.button === button));
    });
    nav.addEventListener('pointerleave', () => {
      hovered = -1;
      previewSuppressed = false;
      closePreview();
      schedule();
    });
    nav.addEventListener('focusin', (event: FocusEvent) => {
      if (!(event.relatedTarget instanceof Node) || !nav.contains(event.relatedTarget)) {
        returnFocus = event.relatedTarget instanceof Element ? event.relatedTarget : null;
      }
      focused = entries.findIndex((entry) => entry.button === event.target);
      previewSuppressed = false;
      paint();
      requestPreview();
    });
    nav.addEventListener('focusout', (event: FocusEvent) => {
      if (event.relatedTarget instanceof Node && nav.contains(event.relatedTarget)) return;
      focused = -1;
      closePreview();
      schedule();
    });
    nav.addEventListener('keydown', keydown);
    document.addEventListener(
      'keydown',
      (event: KeyboardEvent) => {
        if (
          !event.isComposing &&
          event.altKey &&
          !event.ctrlKey &&
          !event.metaKey &&
          !event.shiftKey &&
          !nav.hidden &&
          (event.key === 'ArrowUp' || event.key === 'ArrowDown') &&
          !(event.target instanceof Element && event.target.closest('.document-pane,[role="dialog"]'))
        ) {
          event.preventDefault();
          const anchor = entries[current]?.anchor;
          const previous = anchor && anchor.getBoundingClientRect().top < bounds.top - 24 ? current : current - 1;
          void jump(Math.max(0, Math.min(entries.length - 1, event.key === 'ArrowDown' ? current + 1 : previous)));
          return;
        }
        if (event.key !== 'Escape' || nav.hidden || selected() < 0) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        const hadFocus = nav.contains(document.activeElement);
        dismiss();
        if (hadFocus) {
          const focusTarget = returnFocus?.isConnected ? returnFocus : document.getElementById('task');
          if (focusTarget instanceof HTMLElement) focusTarget.focus({ preventScroll: true });
        }
      },
      true,
    );
    area.addEventListener(
      'scroll',
      () => {
        closePreview();
        previewSuppressed = true;
        schedule();
      },
      { passive: true },
    );
    window.addEventListener('resize', () => {
      closePreview();
      schedule();
    });
    window.addEventListener('blur', dismiss);
    new MutationObserver(schedule).observe(chat, { childList: true, subtree: true, characterData: true });
    new ResizeObserver(schedule).observe(area);
    new ResizeObserver(schedule).observe(chat);
    document.querySelectorAll<Element>('.changes-drawer, dialog').forEach((element) => {
      new MutationObserver(() => {
        dismiss();
        schedule();
      }).observe(element, { attributes: true, attributeFilter: ['class', 'open'] });
    });
    schedule();
  }
  runtime.ConversationNav = { init, refresh };
})();
