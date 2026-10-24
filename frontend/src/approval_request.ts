import { runtime } from './runtime';
import { createIconSvg } from './icon_button';
import { showCommandPermissions } from './command_permissions';
import type { Message } from './types/domain';

export function approvalRequest(
  message: Message,
  approve: (id: string, allowed: boolean, scope?: string) => unknown,
): HTMLElement {
  const row = document.createElement('div');
  row.className = 'msg shell';
  const card = document.createElement('div');
  card.className = 'shell-card approval-request';
  card.dataset.approvalId = message.id;
  card.dataset.status = 'pending';
  card.tabIndex = -1;
  const title = document.createElement('div');
  title.className = 'sc-title';
  title.textContent = 'Run this command?';
  const body = document.createElement('div');
  body.className = 'sc-command-body';
  const cmd = document.createElement('pre');
  cmd.className = 'shell-command';
  cmd.textContent = message.command || '';
  body.append(cmd);
  if (message.cwd) {
    const cwd = document.createElement('div');
    cwd.className = 'sc-meta';
    cwd.textContent = `Working directory · ${message.cwd}`;
    body.append(cwd);
  }
  if (message.commandTruncated && message.commandSha256) {
    const integrity = document.createElement('div');
    integrity.className = 'sc-note';
    integrity.textContent = `Command preview truncated · full sha256 ${message.commandSha256}`;
    body.append(integrity);
  }
  const error = document.createElement('div');
  error.className = 'sc-error';
  error.setAttribute('role', 'status');
  error.textContent = message.approvalError || '';
  error.hidden = !error.textContent;
  const actions = document.createElement('div');
  actions.className = 'shell-actions';
  function button(label: string, action: () => unknown) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'text-btn';
    node.textContent = label;
    node.disabled = !!message.approvalSubmission;
    node.onclick = action;
    return node;
  }
  if (message.approvalSubmission && message.approvalSubmission !== 'sending') {
    error.hidden = false;
    error.textContent =
      message.approvalSubmission === 'checking' ? 'Confirming approval status…' : 'Decision could not be confirmed';
    if (message.approvalSubmission === 'unconfirmed') {
      const check = button('Check status', () => runtime.ShellApprovals.refresh(message.id || ''));
      check.disabled = false;
      actions.append(check);
    }
  }
  const deny = button('Deny', () => approve(message.id || '', false));
  const split = document.createElement('div');
  split.className = 'approval-split';
  const allow = button(message.approvalSubmission === 'sending' ? 'Submitting…' : 'Allow once', () =>
    approve(message.id || '', true),
  );
  allow.classList.add('approval-allow');
  if (message.approvalSubmission === 'sending') {
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    spinner.setAttribute('aria-hidden', 'true');
    allow.prepend(spinner);
    allow.setAttribute('aria-busy', 'true');
  }
  const options = document.createElement('details');
  options.className = 'approval-options';
  const toggle = document.createElement('summary');
  toggle.setAttribute('aria-label', 'More approval options');
  toggle.append(createIconSvg('chevron-down'));
  const menu = document.createElement('div');
  menu.className = 'approval-menu';
  for (const [scope, label, note] of [
    [
      'session',
      'Allow exact command in this chat',
      'Same command and working directory. Other commands still need approval.',
    ],
    ...(message.projectRuleAvailable
      ? [['project', 'Allow exact command in this project', 'Same command and working directory across this project.']]
      : []),
  ]) {
    const choice = button(label, () => {
      options.open = false;
      return approve(message.id || '', true, scope);
    });
    choice.setAttribute('aria-label', label);
    const description = document.createElement('small');
    description.textContent = note;
    choice.append(description);
    menu.append(choice);
  }
  const manage = button('Manage saved permissions…', () => {
    options.open = false;
    void showCommandPermissions(message.sessionId || '', message.project || '', false, toggle);
  });
  manage.classList.add('approval-manage-permissions');
  manage.disabled ||= !message.sessionId;
  menu.append(manage);
  options.append(toggle, menu);
  let outside: AbortController | undefined;
  options.addEventListener('toggle', () => {
    outside?.abort();
    if (!options.open) return;
    options.classList.remove('opens-up');
    const area = card.closest('#chat-area')?.getBoundingClientRect();
    const bounds = menu.getBoundingClientRect();
    options.classList.toggle('opens-up', bounds.bottom > (area?.bottom || window.innerHeight));
    outside = new AbortController();
    document.addEventListener(
      'pointerdown',
      (event) => {
        if (!options.contains(event.target as Node)) options.open = false;
      },
      { once: true, signal: outside.signal },
    );
  });
  options.onkeydown = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      options.open = false;
      toggle.focus();
    }
  };
  options.addEventListener('focusout', (event) => {
    if (!options.contains(event.relatedTarget as Node | null)) options.open = false;
  });
  if (message.approvalSubmission) options.hidden = true;
  split.append(allow, options);
  actions.append(deny, split);
  card.append(title, body, error, actions);
  row.append(card);
  return row;
}
