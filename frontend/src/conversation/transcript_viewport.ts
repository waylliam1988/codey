import { nestedOwnsScroll } from '../render/scroll_owner';

interface ReadingAnchor {
  index: number;
  offset: number;
}
export interface TranscriptView {
  scroll: number;
  following: boolean;
  anchor: ReadingAnchor | null;
  height: number;
}
interface TranscriptViewportDeps {
  sessionId(): string;
  currentView(): TranscriptView | undefined;
  capture(): void;
  updateLatest(): void;
  onResize(): void;
  loadEarlier(): void;
  onReading(): void;
}

export function createTranscriptViewport(area: HTMLElement, chat: HTMLElement, deps: TranscriptViewportDeps) {
  let following = true;
  let scrollOffset = 0;
  let scrollIntent: 'history' | 'latest' | null = null;
  let scrollbarHeld = false;
  let userScrollPending = false;
  let pendingJump: { sessionId: string; scroll: number; anchor: ReadingAnchor } | null = null;

  function isAtLatest() {
    return area.scrollHeight - area.scrollTop - area.clientHeight <= 2;
  }
  function setScroll(top: number) {
    area.scrollTop = top;
    scrollOffset = area.scrollTop;
  }
  function readingAnchor() {
    if (pendingJump?.sessionId === deps.sessionId()) return pendingJump.anchor;
    const top = area.getBoundingClientRect().top;
    const index = Array.from(chat.children).findIndex((node) => node.getBoundingClientRect().bottom > top);
    return index < 0 ? null : { index, offset: chat.children[index].getBoundingClientRect().top - top };
  }
  function restoreReading(view: Pick<TranscriptView, 'scroll' | 'anchor'> | undefined) {
    const node = view?.anchor && chat.children[view.anchor.index];
    if (node && view?.anchor)
      setScroll(
        area.scrollTop + node.getBoundingClientRect().top - area.getBoundingClientRect().top - view.anchor.offset,
      );
    else if (view) setScroll(view.scroll);
  }
  function interruptFollowing() {
    pendingJump = null;
    following = false;
    scrollIntent = 'history';
    userScrollPending = true;
    deps.onReading();
    deps.capture();
    deps.updateLatest();
  }
  function scrollToMessage(anchor: HTMLElement, smooth: boolean): void {
    const index = Array.from(chat.children).indexOf(anchor);
    const top = area.scrollTop + anchor.getBoundingClientRect().top - area.getBoundingClientRect().top - 24;
    pendingJump = { sessionId: deps.sessionId(), scroll: top, anchor: { index, offset: 24 } };
    area.scrollTo({ top, behavior: smooth ? 'smooth' : 'instant' });
    deps.capture();
  }

  function syncLayout(): void {
    if (following) setScroll(area.scrollHeight);
    else if (!userScrollPending && !scrollbarHeld) restoreReading(deps.currentView());
  }

  function capture(): TranscriptView {
    return {
      scroll: pendingJump?.sessionId === deps.sessionId() ? pendingJump.scroll : area.scrollTop,
      following,
      anchor: readingAnchor(),
      height: area.scrollHeight,
    };
  }

  function readEarlier(): void {
    following = false;
    scrollIntent = 'history';
  }

  area.addEventListener(
    'wheel',
    (e) => {
      if (e.shiftKey || Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
      if (nestedOwnsScroll(e.target, area, e.deltaY)) return;
      if (e.deltaY < 0) {
        interruptFollowing();
        if (area.scrollTop < 100) deps.loadEarlier();
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
    if (['ArrowUp', 'PageUp', 'Home'].includes(e.key) || (e.key === ' ' && e.shiftKey)) interruptFollowing();
    else if (['ArrowDown', 'PageDown', 'End', ' '].includes(e.key)) {
      scrollIntent = 'latest';
      userScrollPending = true;
    }
    if (scrollIntent === 'history' && area.scrollTop < 100) deps.loadEarlier();
  });
  area.addEventListener('pointerdown', (e) => {
    const rect = area.getBoundingClientRect();
    if (e.target === area && e.clientX >= rect.right - (area.offsetWidth - area.clientWidth || 12)) {
      scrollbarHeld = true;
      interruptFollowing();
    }
  });
  document.addEventListener('pointerup', () => {
    if (scrollbarHeld && isAtLatest()) {
      following = true;
      scrollIntent = null;
      deps.capture();
      deps.updateLatest();
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
    if (selection && !selection.isCollapsed && area.contains(selection.anchorNode)) interruptFollowing();
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
      if (touchY !== undefined && y !== undefined && y > touchY) interruptFollowing();
      else if (touchY !== undefined && y !== undefined && y < touchY) {
        scrollIntent = 'latest';
        userScrollPending = true;
      }
      touchY = y;
    },
    { passive: true },
  );
  area.addEventListener('scroll', () => {
    const view = deps.currentView();
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
    deps.capture();
    deps.updateLatest();
    if (!following && scrollIntent === 'history' && area.scrollTop < 100) deps.loadEarlier();
  });
  area.addEventListener('scrollend', () => {
    pendingJump = null;
    deps.capture();
  });
  new ResizeObserver(() => {
    deps.onResize();
  }).observe(chat);
  return {
    following: () => following,
    setFollowing: (value: boolean) => {
      following = value;
    },
    clearIntent: () => {
      scrollIntent = null;
    },
    clearJump: () => {
      pendingJump = null;
    },
    endUserScroll: () => {
      userScrollPending = false;
    },
    isAtLatest,
    capture,
    syncLayout,
    readEarlier,
    readingAnchor,
    restoreReading,
    setScroll,
    scrollToMessage,
    interruptFollowing,
  };
}
