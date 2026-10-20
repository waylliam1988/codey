import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workspace } from './workspace';

beforeEach(() => {
  localStorage.clear();
  document.body.replaceChildren();
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
});
afterEach(() => vi.unstubAllGlobals());
function create() {
  const workspace = new Workspace();
  const files = document.createElement('div'),
    changes = document.createElement('div');
  const showFiles = vi.fn(),
    showChanges = vi.fn();
  workspace.register('files', 'Files', files, showFiles);
  workspace.register('changes', 'Changes', changes, showChanges);
  document.body.append(workspace.element);
  workspace.setSession('one');
  return { workspace, files, changes, showFiles, showChanges };
}

it('keeps late child rendering inside its inactive tab and restores the selected tab per conversation', () => {
  const { workspace, files, changes, showChanges } = create();
  workspace.select('files');
  workspace.select('changes', { path: 'one.py' });
  files.hidden = false;
  expect(files.parentElement!.hidden).toBe(true);
  expect(changes.parentElement!.hidden).toBe(false);
  workspace.setSession('two');
  expect(workspace.element.hidden).toBe(true);
  workspace.select('files');
  workspace.setSession('one');
  expect(showChanges).toHaveBeenLastCalledWith({ path: 'one.py' });
  expect(changes.parentElement!.hidden).toBe(false);
});

it('restores only layout metadata after reload and persists keyboard resizing', () => {
  const first = create();
  first.workspace.select('changes', { path: 'a.py' });
  first.workspace.element
    .querySelector('[role="separator"]')!
    .dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
  first.workspace.element.remove();
  const second = create();
  expect(second.showChanges).toHaveBeenCalledWith({ path: 'a.py' });
  expect(document.body.style.getPropertyValue('--workspace-width')).toContain('px');
  expect(second.workspace.element.querySelector('[aria-selected="true"]')!.textContent).toBe('Changes');
  second.workspace.close();
  expect(document.body.classList.contains('workspace-open')).toBe(false);
});

it('moves between view tabs with keyboard arrows and restores focus on close', () => {
  const { workspace } = create();
  const trigger = document.createElement('button');
  document.body.append(trigger);
  trigger.focus();
  workspace.select('files');
  workspace.select('changes');
  const selected = workspace.element.querySelector<HTMLElement>('[aria-selected="true"]')!;
  selected.focus();
  selected.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  expect(document.activeElement?.textContent).toBe('Files');
  workspace.close();
  expect(document.activeElement).toBe(trigger);
});

it('reports unavailable layout storage while keeping the current view usable', () => {
  const { workspace } = create();
  const save = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('full');
  });
  workspace.select('files');
  expect(workspace.element.hidden).toBe(false);
  expect(workspace.element.textContent).toContain('could not be saved');
  save.mockRestore();
});

it('leaves file reading positions to the document tabs when reopening or changing workspace mode', () => {
  const { workspace, files } = create();
  const body = document.createElement('div');
  body.className = 'document-body';
  body.scrollTop = 240;
  files.append(body);
  workspace.select('files');
  expect(body.scrollTop).toBe(240);
  workspace.close();
  workspace.select('files');
  expect(body.scrollTop).toBe(240);
  workspace.element.querySelector<HTMLButtonElement>('[aria-label="Expand workspace"]')!.click();
  expect(body.scrollTop).toBe(240);
});
