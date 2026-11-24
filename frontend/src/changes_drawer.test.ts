import { expect, it, vi } from 'vitest';
import './changes_drawer';
import { runtime } from './runtime';
import type { ChangesData } from './types/domain';

function diff(path: string, line: string): ChangesData {
  return {
    ok: true,
    mode: 'snapshot',
    files: [{ path }],
    diff: `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n---old\n+++${line}`,
  };
}

it('keeps historical file previews scoped to the selected run in the same project', async () => {
  document.body.innerHTML = `
    <section id="changes-drawer">
      <span id="changes-scope"></span><span id="changes-subtitle"></span>
      <button id="changes-copy"></button><button id="changes-restore"></button>
      <button id="changes-refresh"></button><button id="changes-close"></button>
      <div id="changes-body"></div>
    </section>`;
  const element = (id: string): HTMLElement => {
    const found = document.getElementById(id);
    if (!found) throw new Error(id);
    return found;
  };
  let resolveOld: (data: ChangesData) => void = () => {
    throw new Error('Old request not opened');
  };
  const old = new Promise<ChangesData>((resolve) => {
    resolveOld = resolve;
  });
  const fetchWorkspace = vi.fn(async () => diff('workspace.txt', 'working tree'));
  const fetchWorkspaceFile = vi.fn();
  const fetchRun = vi.fn(async (sessionId: string, runId: string) =>
    sessionId === 'a' && runId === 'old' ? old : diff('b/shared b/file.txt', 'second run'),
  );
  runtime.UiState = { pathName: (project: string) => project, setDrawerOpen: vi.fn() };
  window.ChangesDrawer.init({
    $: element,
    fetchChanges: fetchWorkspace,
    fetchRunChanges: fetchRun,
    fetchChangeFile: fetchWorkspaceFile,
    restoreChanges: vi.fn(),
    escapeHtml: (text) => text,
    previewHtml: vi.fn(),
  });
  const pending = window.ChangesDrawer.openRun('project', 'a', 'old');
  await window.ChangesDrawer.openRun('project', 'b', 'new');
  resolveOld(diff('b/shared b/file.txt', 'first run'));
  await pending;
  element('changes-body').querySelector('button')?.click();
  expect(element('changes-body').textContent).toContain('second run');
  expect(element('changes-body').textContent).not.toContain('first run');
  expect(element('changes-restore').hidden).toBe(true);
  expect(fetchWorkspace).not.toHaveBeenCalled();
  expect(fetchWorkspaceFile).not.toHaveBeenCalled();
  expect(fetchRun.mock.calls).toEqual([
    ['a', 'old'],
    ['b', 'new'],
  ]);
});
