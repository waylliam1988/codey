import { afterEach, expect, it, vi } from 'vitest';
import type { Session } from '../types/domain';
import { createTitleEditor } from './title_editor';

const cleanup: Array<() => void> = [];

afterEach(() => {
  for (const dispose of cleanup.splice(0)) dispose();
  document.body.replaceChildren();
});

function fixture() {
  let session: Session = {
    id: 'first',
    title: 'First chat',
    draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
    messages: [],
  };
  const element = document.createElement('div');
  const next = document.createElement('button');
  document.body.append(element, next);
  const commit = vi.fn((id: string, title: string) => {
    if (id === session.id) {
      session = { ...session, title };
      editor.renderTitle(session, null);
    }
  });
  const editor = createTitleEditor(
    {
      activeSession: () => session,
      activeProject: () => null,
      getActiveId: () => session.id,
      closeAllMenus: () => {},
      commitSessionRename: commit,
      renameInputEl(value, onCommit, onCancel) {
        const input = document.createElement('input');
        input.value = value;
        input.onkeydown = (event) => {
          if (event.key === 'Enter') onCommit(input.value);
          if (event.key === 'Escape') onCancel();
        };
        return input;
      },
    },
    element,
  );
  cleanup.push(editor.dispose);
  editor.renderTitle(session, null);
  element.querySelector('button')?.click();
  const input = element.querySelector('input');
  if (!input) throw new Error('Title editor did not mount');
  return {
    input,
    element,
    next,
    commit,
    editor,
    switchSession() {
      session = { ...session, id: 'second', title: 'Second chat' };
      editor.renderTitle(session, null);
    },
  };
}

it('ignores IME confirmation and commits the completed title after blur', () => {
  const { input, commit, element } = fixture();
  input.dispatchEvent(new CompositionEvent('compositionstart'));
  input.value = '新標題';
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true }));
  input.blur();
  expect(commit).not.toHaveBeenCalled();
  input.dispatchEvent(new CompositionEvent('compositionend'));
  expect(commit).toHaveBeenCalledExactlyOnceWith('first', '新標題');
  expect(element.textContent).toBe('新標題');
});

it('keeps the original title edit scoped to its chat while a sidebar click switches chats', () => {
  const { input, next, commit, element, switchSession } = fixture();
  input.value = 'Renamed';
  next.dispatchEvent(new Event('pointerdown', { bubbles: true }));
  input.blur();
  expect(commit).not.toHaveBeenCalled();
  next.onclick = switchSession;
  next.click();
  input.dispatchEvent(new CompositionEvent('compositionend'));
  expect(commit).toHaveBeenCalledExactlyOnceWith('first', 'Renamed');
  expect(element.textContent).toBe('Second chat');
});

it('restores the title button and its focus after Escape', () => {
  const { input, element, commit } = fixture();
  input.value = 'Discard this';
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  expect(commit).not.toHaveBeenCalled();
  expect(element.textContent).toBe('First chat');
  expect(document.activeElement).toBe(element.querySelector('.crumb-chat'));
});
