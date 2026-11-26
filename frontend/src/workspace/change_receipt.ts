import { undoRunChanges } from '../api/changes';
import { createIconSvg } from '../render/icon_button';
import type { Message } from '../types/domain';
import { ChangesDrawer } from './changes_drawer';

type ChangeMessage = Extract<Message, { type: 'changes' }>;

export function receiptButton(label: string, action: () => void): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'link-btn';
  button.textContent = label;
  button.onclick = action;
  return button;
}

export function changeReceipt(message: ChangeMessage, sessionId: string, project: string): HTMLElement {
  const row = document.createElement('div');
  row.className = 'msg changes change-receipt';
  row.dataset.changeRun = message.runId || '';
  const summary = document.createElement('div');
  summary.className = 'change-receipt-summary';
  const count = message.count || 0;
  const stats =
    typeof message.additions === 'number' && typeof message.deletions === 'number'
      ? ` · +${message.additions} −${message.deletions}`
      : '';
  summary.append(createIconSvg('file'), `${count} file${count === 1 ? '' : 's'} changed${stats}`);
  const actions = document.createElement('div');
  actions.className = 'change-receipt-actions';
  const detail = document.createElement('div');
  detail.className = 'change-receipt-detail';
  detail.setAttribute('role', 'status');
  detail.hidden = true;
  row.append(summary, actions, detail);
  const runId = message.runId;
  if (!runId || !project) return row;
  const view = receiptButton('View diff', () => void ChangesDrawer.openRun(project, sessionId, runId));
  if (message.files?.length) {
    const files = document.createElement('div');
    files.className = 'change-receipt-files';
    for (const file of message.files) {
      const path = file.path;
      if (path) {
        const button = receiptButton(path, () => void ChangesDrawer.openRun(project, sessionId, runId, path));
        button.title = path;
        files.append(button);
      }
    }
    row.append(files);
  }
  actions.append(view);
  if (!message.undoAvailable) return row;

  const undo = receiptButton('Undo', () => void preview());
  actions.append(undo);
  function report(text: string) {
    detail.hidden = !text;
    detail.textContent = text;
  }
  async function preview() {
    undo.disabled = true;
    report('Checking files…');
    try {
      const result = await undoRunChanges(sessionId, runId!, true);
      if (!row.isConnected) return;
      if (!result.ok) {
        report([result.error, ...result.conflicts].join(' · '));
        return;
      }
      if (!result.files.length) {
        undo.remove();
        report('Files already match their state before this task.');
        return;
      }
      report(
        `Undo this task’s changes to ${result.files.length} file${result.files.length === 1 ? '' : 's'}? ${result.files.join(', ')}`,
      );
      const cancel = receiptButton('Cancel', () => {
        actions.replaceChildren(view, undo);
        report('');
        undo.focus();
      });
      const confirm = receiptButton('Undo changes', () => void commit(cancel, confirm));
      actions.replaceChildren(view, cancel, confirm);
      if (document.activeElement === undo || document.activeElement === document.body) cancel.focus();
    } catch {
      report('Could not check which files can be undone.');
    } finally {
      undo.disabled = false;
    }
  }
  async function commit(cancel: HTMLButtonElement, confirm: HTMLButtonElement) {
    cancel.disabled = confirm.disabled = true;
    report('Undoing task changes…');
    try {
      const result = await undoRunChanges(sessionId, runId!, false);
      if (!row.isConnected) return;
      actions.replaceChildren(view, ...(result.ok ? [] : [undo]));
      report(
        result.ok
          ? 'Task changes undone.'
          : [result.error, result.restored.length ? `Restored: ${result.restored.join(', ')}` : '', ...result.conflicts]
              .filter(Boolean)
              .join(' · '),
      );
    } catch {
      actions.replaceChildren(view, undo);
      report('Could not confirm undo. Check the files again before retrying.');
    }
  }
  return row;
}
