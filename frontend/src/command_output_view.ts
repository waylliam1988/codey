import { iconButton } from './icon_button';

export interface OutputReading {
  top: number;
  left: number;
  following: boolean;
}
interface OutputView {
  write(text: string): void;
  capture(): OutputReading;
  restore(state: OutputReading): void;
}
const views = new WeakMap<HTMLElement, OutputView>();

/** A bounded viewport over retained output; scrolling never discards text. */
export function commandOutputView(pre: HTMLElement, section: HTMLElement, details: HTMLDetailsElement): OutputView {
  let following = true;
  let positioned = false;
  let lastTop = 0;
  let lastLeft = 0;
  let restore: OutputReading | undefined;
  const latest = iconButton('Back to latest output', 'arrow-down', () => {
    following = true;
    restore = undefined;
    pre.scrollTop = pre.scrollHeight;
    lastTop = pre.scrollTop;
    lastLeft = pre.scrollLeft;
    paint();
  });
  latest.classList.add('command-latest');
  latest.hidden = true;
  section.append(latest);
  function atBottom(): boolean {
    return pre.scrollHeight - pre.scrollTop - pre.clientHeight <= 3;
  }
  function paint(): void {
    latest.hidden = pre.clientHeight === 0 || atBottom();
    pre.style.setProperty('--output-fade-top', pre.scrollTop > 2 ? '14px' : '0px');
    pre.style.setProperty('--output-fade-bottom', latest.hidden ? '0px' : '14px');
  }
  function layout(): void {
    if (!pre.clientHeight) return;
    if (restore) {
      following = restore.following;
      pre.scrollLeft = restore.left;
      pre.scrollTop = following ? pre.scrollHeight : restore.top;
      restore = undefined;
      positioned = true;
    } else if (!positioned || following) {
      pre.scrollTop = pre.scrollHeight;
      positioned = true;
    }
    lastTop = pre.scrollTop;
    lastLeft = pre.scrollLeft;
    paint();
  }
  pre.addEventListener(
    'wheel',
    (event) => {
      if (event.deltaY < 0) following = false;
    },
    { passive: true },
  );
  pre.addEventListener('keydown', (event) => {
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) following = false;
  });
  let touchY = 0;
  pre.addEventListener(
    'touchstart',
    (event) => {
      touchY = event.touches[0]?.clientY || 0;
    },
    { passive: true },
  );
  pre.addEventListener(
    'touchmove',
    (event) => {
      const next = event.touches[0]?.clientY || touchY;
      if (next > touchY) following = false;
      touchY = next;
    },
    { passive: true },
  );
  pre.addEventListener('pointerup', () => {
    const selection = document.getSelection();
    if (selection && !selection.isCollapsed && pre.contains(selection.anchorNode)) following = false;
  });
  pre.addEventListener(
    'scroll',
    () => {
      if (!pre.clientHeight) return;
      if (pre.scrollTop < lastTop) following = false;
      if (atBottom()) following = true;
      lastTop = pre.scrollTop;
      lastLeft = pre.scrollLeft;
      paint();
    },
    { passive: true },
  );
  details.addEventListener('toggle', () => {
    if (details.open) layout();
  });
  if (typeof ResizeObserver !== 'undefined') {
    let attached = false;
    const observer = new ResizeObserver(() => {
      if (pre.isConnected) {
        attached = true;
        layout();
      } else if (attached) observer.disconnect();
    });
    observer.observe(pre);
  }
  const view: OutputView = {
    write(text) {
      const top = pre.clientHeight ? pre.scrollTop : lastTop,
        left = pre.clientHeight ? pre.scrollLeft : lastLeft;
      const previous = pre.textContent || '';
      if (pre.firstChild instanceof Text && pre.childNodes.length === 1 && text.startsWith(previous))
        pre.firstChild.appendData(text.slice(previous.length));
      else if (text !== previous) pre.textContent = text;
      if (!pre.clientHeight) {
        restore ||= { top, left, following };
        paint();
        return;
      }
      pre.scrollTop = following ? pre.scrollHeight : top;
      pre.scrollLeft = left;
      lastTop = pre.scrollTop;
      lastLeft = pre.scrollLeft;
      paint();
    },
    capture: () =>
      restore || {
        top: pre.clientHeight ? pre.scrollTop : lastTop,
        left: pre.clientHeight ? pre.scrollLeft : lastLeft,
        following,
      },
    restore(state) {
      restore = state;
      following = state.following;
      layout();
    },
  };
  views.set(pre, view);
  return view;
}

export function captureCommandOutput(card?: Element): OutputReading | undefined {
  const pre = card?.querySelector<HTMLElement>('.shell-stdout');
  return pre ? views.get(pre)?.capture() : undefined;
}
export function restoreCommandOutput(card: Element, state?: OutputReading): void {
  const pre = card.querySelector<HTMLElement>('.shell-stdout');
  if (pre && state) views.get(pre)?.restore(state);
}
