import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createSidebar, type SidebarDeps } from './sidebar';
import type { Project, Session } from './types/domain';

function session(id: string, projectId?: string): Session {
  return {
    id,
    title: id,
    draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
    messages: [],
    projectId,
  };
}

function makeDeps(sessions: Session[], projects: Project[], activeId: string): SidebarDeps {
  const state = { sessions, projects, activeId };
  return {
    read: () => state,
    switchSession: vi.fn(),
    openProject: vi.fn(),
    newSession: vi.fn(),
    setProjectExpanded: (id, expanded) => {
      const project = projects.find((item) => item.id === id);
      if (project) project.expanded = expanded;
    },
    renameSession: vi.fn(),
    renameProject: vi.fn(),
    openSessionMenu: vi.fn(),
    openProjectMenu: vi.fn(),
    sessionProject: (item) => projects.find((project) => project.id === item.projectId),
    renameInputEl: (value, commit, cancel) => {
      const input = document.createElement('input');
      input.className = 'rename-input';
      input.value = value;
      input.onkeydown = (event) => {
        if (event.key === 'Enter') commit(input.value);
        if (event.key === 'Escape') cancel();
      };
      return input;
    },
    search: {
      update: vi.fn(),
      isSearching: () => false,
      matchesSession: () => true,
      matchesProject: () => true,
      appendGroupLabel: (parent, label) => {
        const element = document.createElement('div');
        element.textContent = label;
        parent.appendChild(element);
      },
    },
  };
}

describe('Sidebar', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="session-list"></div>';
  });

  it('keeps the active session in a project preview', () => {
    const project: Project = { id: 'p1', name: 'Project', path: '/project', expanded: true };
    const sessions = Array.from({ length: 6 }, (_, index) => session(`s${index + 1}`, 'p1'));
    const sidebar = createSidebar(makeDeps(sessions, [project], 's6'));

    sidebar.render();

    expect(document.querySelectorAll('.session-item')).toHaveLength(5);
    expect(document.querySelector('.session-item.active')?.textContent).toContain('s6');
  });

  it('clears editing state before delegating a non-empty rename', () => {
    const sessions = [session('s1')];
    const deps = makeDeps(sessions, [], 's1');
    const sidebar = createSidebar(deps);

    sidebar.beginSessionRename('s1');
    const input = document.querySelector<HTMLInputElement>('.rename-input');
    expect(input).not.toBeNull();
    if (!input) return;
    input.value = 'Renamed';
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));

    expect(deps.renameSession).toHaveBeenCalledWith('s1', 'Renamed');
    sidebar.render();
    expect(document.querySelector('.rename-input')).toBeNull();
  });
});
