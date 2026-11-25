import { expect, it, vi } from 'vitest';
import { createSelectionMenu } from './selection_menu';

it('keeps a newer selection while an earlier copy settles and restores its focus on Escape', async () => {
  document.body.innerHTML = `
    <textarea id="first">First message</textarea>
    <textarea id="second">Second message</textarea>
    <input type="password" value="private">
    <div id="selection-menu">
      <button id="selection-copy">Copy</button>
      <button id="selection-quote">Quote</button>
      <span id="selection-copy-status"></span>
    </div>
  `;
  const first = document.querySelector<HTMLTextAreaElement>('#first');
  const second = document.querySelector<HTMLTextAreaElement>('#second');
  const password = document.querySelector<HTMLInputElement>('input[type="password"]');
  const menu = document.getElementById('selection-menu');
  const copy = document.querySelector<HTMLButtonElement>('#selection-copy');
  if (!first || !second || !password || !menu || !copy) throw new Error('Missing selection fixture');

  let settle!: (copied: boolean) => void;
  const clipboard = vi.fn(
    (text: string) =>
      new Promise<boolean>((resolve) => {
        expect(text).toBe('First');
        settle = resolve;
      }),
  );
  const controller = createSelectionMenu({
    activeSession: () => undefined,
    getActiveId: () => 'session',
    closeAllMenus: () => {},
    closeProviderMenu: () => {},
    stopFollowing: () => {},
    quoteInReply: () => false,
    openSelection: () => {},
    copyText: clipboard,
  });
  const open = (element: HTMLElement) =>
    element.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));

  try {
    expect(open(password)).toBe(true);
    expect(menu.classList.contains('open')).toBe(false);
    first.focus();
    first.setSelectionRange(0, 5);
    expect(open(first)).toBe(false);
    copy.click();
    expect(clipboard).toHaveBeenCalledExactlyOnceWith('First');
    expect(copy.disabled).toBe(true);

    second.focus();
    second.setSelectionRange(0, 6);
    expect(open(second)).toBe(false);
    settle(true);
    await Promise.resolve();
    expect(copy.textContent).toBe('Copy');
    expect(copy.disabled).toBe(false);
    expect(menu.classList.contains('open')).toBe(true);

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }));
    expect(menu.classList.contains('open')).toBe(false);
    expect(document.activeElement).toBe(second);
    expect(second.selectionEnd).toBe(6);
  } finally {
    controller.closeSelectionMenu();
    document.body.replaceChildren();
  }
});
