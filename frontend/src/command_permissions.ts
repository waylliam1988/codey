import { createIconSvg } from './icon_button';

/** Managing stored rules never submits a decision for the current command. */
export async function showCommandPermissions(
  sessionId: string,
  project: string,
  allScopes = false,
  returnTo?: HTMLElement,
): Promise<void> {
  const origin = returnTo || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const pending = !!returnTo?.closest('.approval-request');
  const request = new AbortController();
  const dialog = document.createElement('dialog');
  dialog.className = 'settings-dialog command-permissions-dialog';
  dialog.setAttribute('aria-label', 'Saved permissions');
  const header = document.createElement('div');
  header.className = 'settings-head command-permissions-head';
  const title = document.createElement('h2');
  title.textContent = 'Saved permissions';
  const back = document.createElement('button');
  back.type = 'button';
  back.className = 'btn-icon';
  back.setAttribute('aria-label', pending ? 'Back to approval' : 'Back to chat');
  back.append(createIconSvg('arrow-left'));
  back.onclick = () => dialog.close();
  header.append(back, title);
  const body = document.createElement('div');
  body.className = 'settings-body';
  const hint = document.createElement('p');
  hint.className = 'settings-hint';
  hint.textContent = allScopes
    ? 'Saved exact-command rules. Other commands still need approval.'
    : 'Commands allowed in this chat or project. Other commands still need approval.';
  const rows = document.createElement('div');
  rows.setAttribute('aria-live', 'polite');
  const footer = document.createElement('div');
  footer.className = 'command-permissions-actions';
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'text-btn';
  close.textContent = 'Back';
  close.onclick = () => dialog.close();
  footer.append(close);
  body.append(hint, rows, footer);
  dialog.addEventListener(
    'close',
    () => {
      request.abort();
      dialog.remove();
      const target = origin?.isConnected ? origin : document.getElementById('task');
      target?.focus({ preventScroll: true });
    },
    { once: true },
  );
  dialog.append(header, body);
  document.body.append(dialog);
  dialog.showModal();
  async function load(): Promise<void> {
    rows.textContent = 'Loading…';
    try {
      const query = new URLSearchParams({ session_id: sessionId, project });
      if (allScopes) query.set('all', '1');
      const response = await fetch(`/api/command_permissions?${query}`, {
        signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]),
      });
      if (!response.ok) throw new Error('Could not load saved permissions');
      const data = (await response.json()) as { rules: Array<Record<string, string>> };
      if (request.signal.aborted) return;
      rows.replaceChildren();
      if (!data.rules.length) {
        const empty = document.createElement('p');
        empty.className = 'command-permissions-empty';
        empty.textContent = 'No saved permissions';
        rows.append(empty);
      }
      for (const rule of data.rules) {
        const row = document.createElement('section');
        row.className = 'command-permission-rule';
        const content = document.createElement('div');
        const scope = document.createElement('p');
        scope.className = 'settings-hint';
        scope.textContent = `${rule.scope === 'project' ? 'This project' : allScopes ? `Chat ${rule.session_id}` : 'This chat'} · ${rule.cwd}`;
        const command = document.createElement('pre');
        command.textContent = rule.command;
        const revoke = document.createElement('button');
        revoke.type = 'button';
        revoke.className = 'btn-icon';
        revoke.setAttribute('aria-label', `Remove permission for ${rule.command}`);
        revoke.append(createIconSvg('trash'));
        revoke.onclick = async () => {
          revoke.disabled = true;
          try {
            const result = await fetch('/api/command_permissions/revoke', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ id: rule.id }),
              signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]),
            });
            if (!result.ok) throw new Error();
            if (!request.signal.aborted) await load();
          } catch {
            if (request.signal.aborted) return;
            revoke.disabled = false;
            revoke.replaceChildren(document.createTextNode('Try again'));
            revoke.className = 'text-btn';
            revoke.setAttribute('aria-label', `Could not remove permission for ${rule.command}. Try again`);
          }
        };
        content.append(command, scope);
        row.append(content, revoke);
        rows.append(row);
      }
    } catch {
      if (request.signal.aborted) return;
      rows.textContent = 'Could not load saved permissions. ';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'link-btn';
      retry.textContent = 'Retry';
      retry.onclick = () => {
        void load();
      };
      rows.append(retry);
    }
  }
  await load();
}
