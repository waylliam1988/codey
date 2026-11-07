import type { Project, Session } from './types/domain';

export type RenameCommit = (value: string | undefined) => void;

export interface SidebarDeps {
  read(): {
    sessions: readonly Session[];
    projects: readonly Project[];
    activeId: string;
  };
  switchSession(id: string): void;
  openProject(id: string): void;
  newSession(projectId: string): void;
  setProjectExpanded(id: string, expanded: boolean): void;
  renameSession(id: string, title: string): void;
  renameProject(id: string, name: string): void;
  openSessionMenu(anchor: HTMLElement, id: string): void;
  openProjectMenu(anchor: HTMLElement, id: string): void;
  sessionProject(session: Session): Project | null | undefined;
  renameInputEl(value: string, commit: RenameCommit, cancel: () => void): HTMLInputElement;
  search: {
    update(): void;
    isSearching(): boolean;
    matchesSession(session: Session): boolean;
    matchesProject(project: Project): boolean;
    appendGroupLabel(parent: HTMLElement, label: string, hasMatches: boolean): void;
  };
}

export interface Sidebar {
  render(): void;
  beginSessionRename(id: string): void;
  beginProjectRename(id: string): void;
  cancelRename(): void;
  isEditingSession(id: string): boolean;
}

const PROJECT_CHAT_PREVIEW = 5;

export function createSidebar(deps: SidebarDeps): Sidebar {
  let editingSessionId = '';
  let editingProjectId = '';
  const expandedProjectChats = new Set<string>();

  return {
    render,
    beginSessionRename,
    beginProjectRename,
    cancelRename,
    isEditingSession,
  };

  function render(): void {
    const state = deps.read();
    const list = document.getElementById('session-list');
    if (!(list instanceof HTMLElement)) return;
    const previousArrows = new Map(
      Array.from(list.querySelectorAll<HTMLElement>('[data-project-toggle]')).map((button) => [
        button.dataset.projectToggle,
        button.getAttribute('aria-expanded') === 'true',
      ]),
    );
    list.innerHTML = '';
    deps.search.update();

    deps.search.appendGroupLabel(
      list,
      'PROJECTS',
      state.projects.some((project) => deps.search.matchesProject(project)),
    );

    if (!state.projects.length && !deps.search.isSearching()) {
      const empty = document.createElement('div');
      empty.className = 'project-empty';
      empty.textContent = 'No projects';
      list.appendChild(empty);
    }

    for (const project of state.projects) {
      if (!deps.search.matchesProject(project)) continue;
      const node = document.createElement('div');
      node.className = 'project-node';

      const row = document.createElement('div');
      row.className = 'project-row';
      const activeSession = state.sessions.find((session) => session.id === state.activeId);
      if (activeSession?.projectId === project.id) row.classList.add('has-active');
      row.title = project.path;

      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'project-toggle';
      toggle.dataset.projectToggle = project.id;
      const expanded = project.expanded || deps.search.isSearching();
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.setAttribute('aria-label', `${expanded ? 'Collapse' : 'Expand'} ${project.name}`);
      const chevron = document.createElement('span');
      chevron.className = 'disclosure-chevron' + (expanded ? '' : ' is-collapsed');
      chevron.setAttribute('aria-hidden', 'true');
      toggle.append(chevron);
      toggle.onclick = (event) => {
        event.stopPropagation();
        deps.setProjectExpanded(project.id, !project.expanded);
        render();
        list
          .querySelector<HTMLButtonElement>(`[data-project-toggle="${CSS.escape(project.id)}"]`)
          ?.focus({ preventScroll: true });
      };
      row.appendChild(toggle);

      if (project.id === editingProjectId) {
        row.appendChild(
          deps.renameInputEl(project.name || '', (value) => commitProjectRename(project.id, value), cancelRename),
        );
      } else {
        const main = document.createElement('button');
        main.type = 'button';
        main.className = 'project-main';
        main.textContent = project.name;
        row.appendChild(main);
        row.onclick = () => deps.openProject(project.id);
      }

      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'project-more';
      more.setAttribute('aria-label', 'More');
      more.textContent = '⋯';
      more.onclick = (event) => {
        event.stopPropagation();
        deps.openProjectMenu(event.currentTarget as HTMLElement, project.id);
      };
      row.appendChild(more);

      const create = document.createElement('button');
      create.className = 'project-new';
      create.type = 'button';
      create.setAttribute('aria-label', `New chat in ${project.name}`);
      create.innerHTML = '<span class="new-chat-icon" aria-hidden="true"></span>';
      create.onclick = (event) => {
        event.stopPropagation();
        deps.setProjectExpanded(project.id, true);
        deps.newSession(project.id);
      };
      row.appendChild(create);
      node.appendChild(row);

      if (project.expanded || deps.search.isSearching()) {
        const children = document.createElement('div');
        children.className = 'project-children';
        const projectSessions = state.sessions.filter(
          (session) => session.projectId === project.id && deps.search.matchesSession(session),
        );
        if (!projectSessions.length) {
          const empty = document.createElement('div');
          empty.className = 'project-empty';
          empty.textContent = 'No chats';
          children.appendChild(empty);
        } else {
          const searching = deps.search.isSearching();
          const chatsExpanded = expandedProjectChats.has(project.id);
          const preview = projectSessions.slice(0, PROJECT_CHAT_PREVIEW);
          const active = projectSessions.find((session) => session.id === state.activeId);
          if (active && !preview.includes(active)) preview[preview.length - 1] = active;
          for (const session of searching || chatsExpanded ? projectSessions : preview)
            children.appendChild(sessionNode(session, state.activeId));
          if (!searching && projectSessions.length > PROJECT_CHAT_PREVIEW) {
            const reveal = document.createElement('button');
            reveal.type = 'button';
            reveal.className = 'project-reveal';
            reveal.textContent = chatsExpanded ? 'Show less' : 'Show more';
            reveal.setAttribute('aria-expanded', String(chatsExpanded));
            reveal.onclick = () => {
              if (chatsExpanded) expandedProjectChats.delete(project.id);
              else expandedProjectChats.add(project.id);
              render();
              list.querySelector<HTMLButtonElement>(`[data-project-reveal="${CSS.escape(project.id)}"]`)?.focus();
            };
            reveal.dataset.projectReveal = project.id;
            children.appendChild(reveal);
          }
        }
        node.appendChild(children);
      }

      list.appendChild(node);
      const previous = previousArrows.get(project.id);
      if (previous !== undefined && previous !== expanded) {
        chevron.classList.toggle('is-collapsed', !previous);
        chevron.getBoundingClientRect();
        chevron.classList.toggle('is-collapsed', !expanded);
      }
    }

    const loose = state.sessions.filter(
      (session) => !deps.sessionProject(session) && deps.search.matchesSession(session),
    );
    deps.search.appendGroupLabel(list, 'CHATS', loose.length > 0);
    if (!loose.length && !deps.search.isSearching()) {
      const empty = document.createElement('div');
      empty.className = 'project-empty';
      empty.style.marginLeft = '10px';
      empty.textContent = 'No chats';
      list.appendChild(empty);
    }
    for (const session of loose) list.appendChild(sessionNode(session, state.activeId));
  }

  function sessionNode(session: Session, activeId: string): HTMLElement {
    const item = document.createElement('div');
    item.className = 'session-item' + (session.id === activeId ? ' active' : '');
    if (session.id === editingSessionId) {
      item.appendChild(
        deps.renameInputEl(session.title || '', (value) => commitSessionRename(session.id, value), cancelRename),
      );
      return item;
    }
    item.onclick = (event) => {
      if (!(event.target instanceof Element) || !event.target.closest('button')) deps.switchSession(session.id);
    };
    const title = document.createElement('button');
    title.type = 'button';
    title.className = 'session-title';
    title.textContent = session.title || 'Untitled';
    title.onclick = () => deps.switchSession(session.id);
    item.appendChild(title);
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'session-more';
    more.textContent = '⋯';
    more.setAttribute('aria-label', 'More');
    more.onclick = (event) => {
      event.stopPropagation();
      deps.openSessionMenu(event.currentTarget as HTMLElement, session.id);
    };
    item.appendChild(more);
    return item;
  }

  function focusRenameInput(): void {
    const input = document.querySelector<HTMLInputElement>('.rename-input');
    input?.focus();
    input?.select();
  }

  function beginSessionRename(id: string): void {
    const { sessions, projects } = deps.read();
    const session = sessions.find((item) => item.id === id);
    if (!session) return;
    editingProjectId = '';
    editingSessionId = id;
    if (session.projectId) {
      const project = projects.find((item) => item.id === session.projectId);
      if (project && !project.expanded) deps.setProjectExpanded(project.id, true);
    }
    render();
    focusRenameInput();
  }

  function beginProjectRename(id: string): void {
    if (!deps.read().projects.some((project) => project.id === id)) return;
    editingSessionId = '';
    editingProjectId = id;
    render();
    focusRenameInput();
  }

  function cancelRename(): void {
    editingSessionId = '';
    editingProjectId = '';
    render();
  }

  function isEditingSession(id: string): boolean {
    return editingSessionId === id;
  }

  function commitSessionRename(id: string, value: string | undefined): void {
    editingSessionId = '';
    const title = (value || '').trim();
    if (title) deps.renameSession(id, title);
    else render();
  }

  function commitProjectRename(id: string, value: string | undefined): void {
    editingProjectId = '';
    const name = (value || '').trim();
    if (name) deps.renameProject(id, name);
    else render();
  }
}
