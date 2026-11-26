export type RenameCommit = (value: string | undefined) => void;

export function renameInputEl(value: string, commit: RenameCommit, cancel: () => void) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'rename-input';
  input.value = value || '';
  input.maxLength = 80;
  input.setAttribute('aria-label', 'Rename');
  let settled = false;
  const finish = (fn: RenameCommit | (() => void), arg?: string) => {
    if (settled) return;
    settled = true;
    fn(arg);
  };
  input.onmousedown = (e) => e.stopPropagation();
  input.onclick = (e) => e.stopPropagation();
  input.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      finish(commit, input.value);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      finish(cancel, undefined);
    }
  };
  input.onblur = () => finish(commit, input.value);
  return input;
}
