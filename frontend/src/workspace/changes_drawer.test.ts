import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ChangesDrawer } from './changes_drawer';

import type { ChangesData } from '../types/domain';

function diff(path: string, line: string): ChangesData {
  return {
    ok: true,
    mode: 'snapshot',
    files: [{ path }],
    diff: `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n---old\n+++${line}`,
  };
}

function element(id: string): HTMLElement {
  const found = document.getElementById(id);
  if (!found) throw new Error(id);
  return found;
}

beforeEach(() => {
  document.body.innerHTML = `
    <section id="changes-drawer">
      <span id="changes-scope"></span><span id="changes-subtitle"></span>
      <button id="changes-copy"></button><button id="changes-restore"></button>
      <button id="changes-refresh"></button><button id="changes-close"></button>
      <div id="changes-body"></div>
    </section>`;
});

afterEach(() => vi.unstubAllGlobals());

it('keeps historical file previews scoped to the selected run in the same project', async () => {
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
  ChangesDrawer.init({
    $: element,
    fetchChanges: fetchWorkspace,
    fetchRunChanges: fetchRun,
    fetchChangeFile: fetchWorkspaceFile,
    restoreChanges: vi.fn(),
    escapeHtml: (text) => text,
    previewHtml: vi.fn(),
  });
  const pending = ChangesDrawer.openRun('project', 'a', 'old');
  await ChangesDrawer.openRun('project', 'b', 'new');
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

it('shows each historical hunk only under its matching repository path', async () => {
  const root = diff('index.ts', 'root change');
  const nested = diff('src/index.ts', 'nested change');
  ChangesDrawer.init({
    $: element,
    fetchChanges: vi.fn(),
    fetchRunChanges: async () => ({
      ...root,
      files: [...root.files, { path: 'src\\index.ts' }],
      diff: root.diff + '\n\\ No newline at end of file\n\n' + nested.diff,
    }),
    fetchChangeFile: vi.fn(),
    restoreChanges: vi.fn(),
    escapeHtml: (text) => text,
    previewHtml: vi.fn(),
  });
  await ChangesDrawer.openRun('project', 'a', 'paths');
  const buttons = element('changes-body').querySelectorAll<HTMLButtonElement>('button');
  for (const button of buttons) button.click();
  const [rootPreview, nestedPreview] = element('changes-body').querySelectorAll('pre');
  expect(rootPreview.textContent).toContain('root change');
  expect(rootPreview.querySelector('.diff-line.meta')?.textContent).toBe('\\ No newline at end of file');
  expect(rootPreview.textContent).not.toContain('nested change');
  expect(nestedPreview.textContent).toContain('nested change');
  expect(nestedPreview.textContent).not.toContain('root change');
});

it('renders the selected run HTML and ignores output arriving after a run switch', async () => {
  const path = 'index.html';
  let deliver: (response: Response) => void = () => {
    throw new Error('Output request was not started');
  };
  const fetch = vi.fn(
    async () =>
      new Promise<Response>((resolve) => {
        deliver = resolve;
      }),
  );
  vi.stubGlobal('fetch', fetch);
  ChangesDrawer.init({
    $: element,
    fetchChanges: vi.fn(),
    fetchRunChanges: async (_sessionId, runId) => ({
      ...diff(path, runId),
      html_previews: { [path]: `<h1>${runId} snapshot</h1>` },
      verification:
        runId === 'first'
          ? {
              summary: 'Checks not recorded',
              failed_check: {
                command: 'python -m pytest',
                cwd: '.',
                exit_code: 1,
                output: { execution_id: 'execution-a', session_id: 'owner-a', run_id: 'child-a' },
              },
            }
          : { summary: 'Checks passed', checks_passed: true },
    }),
    fetchChangeFile: vi.fn(),
    restoreChanges: vi.fn(),
    escapeHtml: (text) => text,
    previewHtml: vi.fn(),
  });
  const body = element('changes-body');
  await ChangesDrawer.openRun('project', 'owner-a', 'first');
  expect(body.querySelector<HTMLButtonElement>('.change-html-preview')?.hidden).toBe(false);
  const check = body.querySelector('details');
  if (!check) throw new Error('Missing failed check');
  check.open = true;
  await vi.waitFor(() =>
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('run_id=child-a'), expect.any(Object)),
  );

  await ChangesDrawer.openRun('project', 'owner-b', 'second');
  deliver(new Response(JSON.stringify({ revision: 1, execution_state: 'settled', stdout: 'wrong run' })));
  expect(body.querySelector<HTMLButtonElement>('.change-html-preview')?.hidden).toBe(false);
  expect(body.textContent).toContain('Checks passed');
  expect(body.textContent).not.toContain('wrong run');
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining('session_id=owner-a'), expect.any(Object));
});
