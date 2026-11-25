import { afterEach, expect, it, vi } from 'vitest';
import { createProjectFolderPicker } from './project_folder';

afterEach(() => {
  delete window.pywebview;
});

it('rechecks the original conversation after the folder dialog closes', async () => {
  let available = true;
  let select: (value: unknown) => void = () => {};
  window.pywebview = {
    api: {
      pick_folder: () =>
        new Promise((resolve) => {
          select = resolve;
        }),
    },
  };
  const ensureProject = vi.fn(() => ({ id: 'project' }));
  const attachSession = vi.fn();
  const picker = createProjectFolderPicker({
    onBusy: vi.fn(),
    onError: vi.fn(),
    initialPath: () => '',
    activeSessionId: () => 'conversation',
    canAttach: () => available,
    hasRunningTask: () => false,
    ensureProject,
    attachSession,
    newSession: vi.fn(),
  });

  const pending = picker.pickProject();
  expect(picker.busy()).toBe(true);
  available = false;
  select({ ok: true, path: '/project', name: 'Project' });
  await pending;

  expect(picker.busy()).toBe(false);
  expect(ensureProject).not.toHaveBeenCalled();
  expect(attachSession).not.toHaveBeenCalled();
});
