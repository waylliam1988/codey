import type { Project, Session } from '../types/domain';

interface TitleEditorDeps {
  activeSession(): Session | undefined;
  activeProject(): Project | null | undefined;
  getActiveId(): string;
  closeAllMenus(): void;
  commitSessionRename(id: string, value: string): void;
  renameInputEl(
    value: string,
    onCommit: (value: string | null | undefined) => void,
    onCancel: () => void,
  ): HTMLInputElement;
}

interface TitleEdit {
  id: string;
  input: HTMLInputElement;
  composing: boolean;
  blurPending: boolean;
  pointerAway: boolean;
  refocus: boolean;
}

export function createTitleEditor(deps: TitleEditorDeps, element: HTMLElement) {
  let titleEdit: TitleEdit | null = null;
  let pointerTimer: ReturnType<typeof setTimeout> | undefined;
  const listeners = new AbortController();
  function finishTitleEdit(edit: TitleEdit, value: string | null, restoreFocus = false): void {
    if (titleEdit !== edit) return;
    titleEdit = null;
    if (value !== null) deps.commitSessionRename(edit.id, value);
    else {
      const session = deps.activeSession();
      if (session) renderTitle(session, deps.activeProject());
    }
    if (restoreFocus && deps.getActiveId() === edit.id)
      element.querySelector<HTMLElement>('.crumb-chat')?.focus({ preventScroll: true });
  }
  function startTitleRename(id: string): void {
    const s = deps.activeSession();
    if (!s || s.id !== id || titleEdit) return;
    deps.closeAllMenus();
    const edit: TitleEdit = {
      id,
      input: document.createElement('input'),
      composing: false,
      blurPending: false,
      pointerAway: false,
      refocus: false,
    };
    const input = deps.renameInputEl(
      s.title || '',
      (value: string | null | undefined) => finishTitleEdit(edit, value ?? null, edit.refocus),
      () => finishTitleEdit(edit, null, true),
    );
    input.classList.add('title-rename');
    input.setAttribute('aria-label', 'Chat title');
    edit.input = input;
    titleEdit = edit;
    const keydown = input.onkeydown;
    input.onkeydown = (e: KeyboardEvent) => {
      if (titleEdit !== edit || edit.composing || e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter' || e.key === 'Escape') {
        edit.refocus = true;
        e.stopPropagation();
      }
      keydown?.call(input, e);
    };
    input.addEventListener('compositionstart', () => {
      if (titleEdit === edit) edit.composing = true;
    });
    input.addEventListener('compositionend', () => {
      if (titleEdit !== edit) return;
      edit.composing = false;
      if (!edit.pointerAway) finishTitleBlur();
    });
    input.onblur = () => {
      if (titleEdit !== edit) return;
      edit.blurPending = true;
      if (!edit.pointerAway) finishTitleBlur();
    };
    element.querySelector<HTMLElement>('.crumb-chat')?.replaceWith(input);
    input.focus({ preventScroll: true });
    input.select();
  }
  function finishTitleBlur() {
    const edit = titleEdit;
    if (edit?.blurPending && !edit.composing) finishTitleEdit(edit, edit.input.value);
    if (titleEdit) titleEdit.pointerAway = false;
  }
  function initTitleRename() {
    const options = { capture: true, signal: listeners.signal };
    document.addEventListener(
      'pointerdown',
      (event) => {
        if (titleEdit && event.target !== titleEdit.input) titleEdit.pointerAway = true;
      },
      options,
    );
    // Commit after the click target is fixed so a sidebar render cannot swallow the click.
    document.addEventListener('click', finishTitleBlur, options);
    document.addEventListener(
      'pointerup',
      () => {
        const edit = titleEdit;
        clearTimeout(pointerTimer);
        pointerTimer = setTimeout(() => {
          if (titleEdit === edit) finishTitleBlur();
        }, 0);
      },
      options,
    );
    document.addEventListener('pointercancel', finishTitleBlur, options);
    window.addEventListener('blur', finishTitleBlur, { signal: listeners.signal });
  }
  function renderTitle(s: Session, p: Project | null | undefined) {
    if (titleEdit && (titleEdit.id !== s.id || !titleEdit.input.isConnected))
      finishTitleEdit(titleEdit, titleEdit.input.value);
    const title = element;
    if (titleEdit) {
      const project = title.querySelector('.crumb-proj');
      if (project && p) project.textContent = p.name;
      return;
    }
    const existing = title.querySelector('button.crumb-chat');
    if (existing && title.dataset.sessionId === s.id && title.dataset.projectId === (p?.id || '')) {
      existing.textContent = s.title || 'New chat';
      const project = title.querySelector('.crumb-proj');
      if (project) project.textContent = p?.name || '';
      return;
    }
    title.replaceChildren();
    title.dataset.sessionId = s.id;
    title.dataset.projectId = p?.id || '';
    if (p) {
      const project = document.createElement('span');
      project.className = 'crumb-proj';
      project.textContent = p.name;
      const separator = document.createElement('span');
      separator.className = 'crumb-sep';
      separator.textContent = '/';
      title.append(project, separator);
    }
    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'crumb-chat';
    name.textContent = s.title || 'New chat';
    name.title = 'Rename chat';
    name.setAttribute('aria-label', 'Rename chat');
    name.onclick = () => startTitleRename(s.id);
    title.append(name);
  }
  initTitleRename();
  return {
    renderTitle,
    isRenamingSession: (id: string) => titleEdit?.id === id,
    dispose() {
      listeners.abort();
      clearTimeout(pointerTimer);
      titleEdit = null;
      const button = element.querySelector<HTMLButtonElement>('.crumb-chat');
      if (button) button.onclick = null;
    },
  };
}
