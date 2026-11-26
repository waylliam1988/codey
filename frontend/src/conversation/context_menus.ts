interface MenuDependencies {
  projectMenu: HTMLElement;
  sessionMenu: HTMLElement;
  deleteProject(id: string): Promise<void>;
  deleteSession(id: string): Promise<void>;
  newSession(projectId: string): void;
  renameProject(id: string): void;
  renameSession(id: string): void;
  menuOpened(menu: HTMLElement, anchor: HTMLElement): void;
  menusClosed(): void;
}

export function createContextMenus(deps: MenuDependencies) {
  let armedDangerTimer: ReturnType<typeof setTimeout> | null = null;
  const eventElement = (event: Event): Element | null => (event.target instanceof Element ? event.target : null);
  function disarmDangerButtons() {
    if (armedDangerTimer !== null) {
      clearTimeout(armedDangerTimer);
      armedDangerTimer = null;
    }
    document.querySelectorAll<HTMLButtonElement>('.ctx-menu button.arming').forEach((b) => {
      const button = b;
      button.classList.remove('arming');
      if (button.dataset.label) button.textContent = button.dataset.label;
    });
  }
  function armDanger(btn: HTMLButtonElement, run: () => void) {
    if (btn.classList.contains('arming')) {
      disarmDangerButtons();
      closeAllMenus();
      run();
      return;
    }
    disarmDangerButtons();
    btn.dataset.label = btn.textContent;
    btn.textContent = btn.dataset.confirm || 'Confirm';
    btn.classList.add('arming');
    armedDangerTimer = setTimeout(disarmDangerButtons, 3000);
  }

  function openMenuAt(menu: HTMLElement, anchor: HTMLElement) {
    const rect = anchor.getBoundingClientRect();
    menu.classList.add('open');
    const mr = menu.getBoundingClientRect();
    let left = rect.right - mr.width;
    if (left < 8) left = rect.left;
    let top = rect.bottom + 4;
    if (top + mr.height > window.innerHeight - 8) top = rect.top - mr.height - 4;
    menu.style.left = Math.max(8, left) + 'px';
    menu.style.top = Math.max(8, top) + 'px';
    deps.menuOpened(menu, anchor);
  }
  function closeAllMenus() {
    disarmDangerButtons();
    document.querySelectorAll('.ctx-menu.open').forEach((m) => m.classList.remove('open'));
    deps.menusClosed();
  }
  function openProjectMenu(anchor: HTMLElement, projectId: string) {
    closeAllMenus();
    const menu = deps.projectMenu;
    menu.dataset.projectId = projectId;
    openMenuAt(menu, anchor);
  }
  function openSessionMenu(anchor: HTMLElement, sessionId: string) {
    closeAllMenus();
    const menu = deps.sessionMenu;
    menu.dataset.sessionId = sessionId;
    openMenuAt(menu, anchor);
  }
  deps.projectMenu.addEventListener('click', (e) => {
    const btn = eventElement(e)?.closest<HTMLButtonElement>('button[data-act]');
    if (!btn) return;
    const id = deps.projectMenu.dataset.projectId;
    const act = btn.dataset.act;
    if (act === 'remove') {
      if (id) armDanger(btn, () => deps.deleteProject(id));
      return;
    }
    closeAllMenus();
    if (!id) return;
    if (act === 'newchat') deps.newSession(id);
    else if (act === 'rename') deps.renameProject(id);
  });
  deps.sessionMenu.addEventListener('click', (e) => {
    const btn = eventElement(e)?.closest<HTMLButtonElement>('button[data-act]');
    if (!btn) return;
    const id = deps.sessionMenu.dataset.sessionId;
    const act = btn.dataset.act;
    if (act === 'delete') {
      if (id) armDanger(btn, () => deps.deleteSession(id));
      return;
    }
    closeAllMenus();
    if (!id) return;
    if (act === 'rename') deps.renameSession(id);
  });
  document.addEventListener('click', (e) => {
    if (eventElement(e)?.closest('.ctx-menu')) return;
    if (eventElement(e)?.closest('.project-more, .session-more')) return;
    closeAllMenus();
  });

  return { openProjectMenu, openSessionMenu, closeAllMenus };
}
