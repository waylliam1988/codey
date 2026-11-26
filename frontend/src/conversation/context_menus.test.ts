import { afterEach, expect, it, vi } from 'vitest';
import { createContextMenus } from './context_menus';

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  document.body.replaceChildren();
});

it('expires delete confirmation, closes outside, and commits only the confirmed selection', () => {
  vi.useFakeTimers();
  const projectMenu = document.createElement('div');
  projectMenu.className = 'ctx-menu';
  projectMenu.innerHTML = '<button data-act="remove">Remove project</button>';
  const sessionMenu = document.createElement('div');
  sessionMenu.className = 'ctx-menu';
  sessionMenu.innerHTML = '<button data-act="delete">Delete chat</button>';
  const anchor = document.createElement('button');
  document.body.append(projectMenu, sessionMenu, anchor);
  const deleteProject = vi.fn(async (_id: string) => {});
  const deleteSession = vi.fn(async (_id: string) => {});
  const menuOpened = vi.fn();
  const menusClosed = vi.fn();
  const menus = createContextMenus({
    projectMenu,
    sessionMenu,
    deleteProject,
    deleteSession,
    newSession: vi.fn(),
    renameProject: vi.fn(),
    renameSession: vi.fn(),
    menuOpened,
    menusClosed,
  });
  const remove = projectMenu.querySelector('button');
  const destroy = sessionMenu.querySelector('button');
  if (!remove || !destroy) throw new Error('Missing menu actions');

  menus.openProjectMenu(anchor, 'project-one');
  expect(menuOpened).toHaveBeenLastCalledWith(projectMenu, anchor);
  remove.click();
  expect(deleteProject).not.toHaveBeenCalled();
  expect(remove.textContent).toBe('Confirm');
  vi.advanceTimersByTime(3000);
  expect(remove.textContent).toBe('Remove project');
  remove.click();
  document.body.click();
  expect(remove.classList.contains('arming')).toBe(false);
  expect(projectMenu.classList.contains('open')).toBe(false);

  menus.openSessionMenu(anchor, 'chat-one');
  destroy.click();
  destroy.click();
  expect(deleteSession).toHaveBeenCalledExactlyOnceWith('chat-one');
  expect(deleteProject).not.toHaveBeenCalled();
  expect(sessionMenu.classList.contains('open')).toBe(false);
  expect(menusClosed).toHaveBeenCalled();
});
