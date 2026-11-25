import { messageSelectionOffsets, type MessageSelection } from '../workspace/local_context_drawer';
import type { Session } from '../types/domain';

interface SelectionMenuDeps {
  activeSession(): Session | undefined;
  getActiveId(): string;
  closeAllMenus(): void;
  closeProviderMenu(): void;
  stopFollowing(): void;
  quoteInReply(sessionId: string, text: string): boolean;
  openSelection(selection: MessageSelection): void;
  copyText(text: string): Promise<boolean>;
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
  memory?: MessageSelection;
  sessionId?: string;
  scrollPositions?: ScrollPosition[];
}
export function createSelectionMenu(deps: SelectionMenuDeps) {
  let selectedCopy: SelectionSnapshot | null = null;
  let copyGeneration = 0;
  const $ = (id: string) => document.getElementById(id)!;
  const button = $('selection-copy');
  if (!(button instanceof HTMLButtonElement)) throw new Error('Missing selection copy button');
  const copyButton: HTMLButtonElement = button;
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
    const parent = (node: Node): Element | null => (node instanceof Element ? node : node.parentElement);
    const answer = parent(range.startContainer)?.closest('#chat .msg.asst .body');
    const quoteBody = answer?.contains(range.endContainer) ? answer : null;
    const userBody = parent(range.startContainer)?.closest('#chat .msg.user .body');
    const entryId = userBody?.closest<HTMLElement>('.msg.user[data-entry-id]')?.dataset.entryId;
    const session = deps.activeSession();
    const raw = session?.messages.find((message) => message.type === 'user' && message.entryId === entryId)?.text;
    const text = selection.toString();
    const offsets =
      userBody && typeof raw === 'string' && entryId ? messageSelectionOffsets(raw, userBody, range, text) : null;
    return {
      range,
      text,
      focus: document.activeElement,
      quoteBody,
      memory: session && entryId && offsets ? { sessionId: session.id, entryId, quote: text, ...offsets } : undefined,
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
    deps.closeProviderMenu();
    if (!snapshot) return false;
    if (snapshot.range && $('chat').contains(snapshot.range.startContainer)) deps.stopFollowing();
    snapshot.scrollPositions = [];
    for (let node: Element | null = target; node instanceof HTMLElement; node = node.parentElement)
      snapshot.scrollPositions.push({ node, top: node.scrollTop, left: node.scrollLeft });
    const menu = $('selection-menu'),
      copy = copyButton;
    (target?.closest('dialog[open]') || document.body).appendChild(menu);
    selectedCopy = snapshot;
    copyGeneration++;
    $('selection-quote').hidden = !snapshot.quoteBody;
    const remember = $('selection-memory');
    if (remember) remember.hidden = !snapshot.memory;
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
      copy = copyButton;
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
      if (deps.quoteInReply(snapshot.sessionId, snapshot.text)) closeSelectionMenu();
      else {
        $('selection-copy-status').textContent = 'Could not insert quote';
        $('selection-copy-status').hidden = false;
      }
    };
    const remember = $('selection-memory');
    if (remember)
      remember.onclick = () => {
        const snapshot = selectedCopy;
        if (
          !snapshot?.memory ||
          snapshot.memory.sessionId !== deps.getActiveId() ||
          !snapshot.range?.startContainer.isConnected ||
          !snapshot.range.endContainer.isConnected
        ) {
          closeSelectionMenu();
          return;
        }
        deps.openSelection(snapshot.memory);
        closeSelectionMenu();
      };
    copy.onclick = async () => {
      const snapshot = selectedCopy,
        generation = copyGeneration;
      if (!snapshot || copy.disabled) return;
      copy.disabled = true;
      let ok = false;
      try {
        ok = await deps.copyText(snapshot.text);
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
  initSelectionMenu();
  return { closeSelectionMenu };
}
