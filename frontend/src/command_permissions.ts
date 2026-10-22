export async function showCommandPermissions(sessionId: string, project: string, allScopes = false): Promise<void> {
  const dialog = document.createElement('dialog');
  dialog.className = 'shell-card command-permissions-dialog';
  dialog.setAttribute('aria-label', 'Saved command permissions');
  dialog.style.maxWidth = 'min(720px, 90vw)';
  const title = document.createElement('h3');
  title.textContent = 'Saved command permissions';
  const rows = document.createElement('div');
  const close = document.createElement('button');
  close.className = 'text-btn';
  close.textContent = 'Close';
  close.onclick = () => dialog.close();
  dialog.addEventListener('close', () => dialog.remove());
  dialog.append(title, rows, close);
  document.body.append(dialog);
  dialog.showModal();
  async function load() {
    rows.textContent = 'Loading…';
    try {
      const query = new URLSearchParams({ session_id: sessionId, project });
      if (allScopes) query.set('all', '1');
      const response = await fetch(`/api/command_permissions?${query}`);
      if (!response.ok) throw new Error('Could not load saved permissions');
      const data = (await response.json()) as { rules: Array<Record<string, string>> };
      rows.replaceChildren();
      if (!data.rules.length) rows.textContent = 'No saved permissions for this chat or project.';
      for (const rule of data.rules) {
        const row = document.createElement('section');
        const scope = document.createElement('p');
        scope.textContent = `${rule.scope === 'project' ? 'This project' : allScopes ? `Chat ${rule.session_id}` : 'This chat'} · Exact command · ${rule.cwd}`;
        const command = document.createElement('pre');
        command.textContent = rule.command;
        const revoke = document.createElement('button');
        revoke.className = 'text-btn';
        revoke.textContent = 'Revoke';
        revoke.onclick = async () => {
          revoke.disabled = true;
          try {
            const result = await fetch('/api/command_permissions/revoke', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ id: rule.id }),
            });
            if (!result.ok) throw new Error();
            await load();
          } catch {
            revoke.disabled = false;
            revoke.textContent = 'Could not revoke · Try again';
          }
        };
        row.append(scope, command, revoke);
        rows.append(row);
      }
    } catch (error) {
      rows.textContent = error instanceof Error ? error.message : 'Could not load saved permissions';
    }
  }
  await load();
}

export function permissionsButton(sessionId: string, project: string): HTMLButtonElement {
  const button = document.createElement('button');
  button.className = 'text-btn';
  button.textContent = 'Saved permissions';
  button.onclick = () => void showCommandPermissions(sessionId, project);
  button.disabled = !sessionId;
  return button;
}
