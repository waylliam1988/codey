import type { DocumentRef } from './document_pane';
import { commandCard } from './command_card';
import { approvalText } from './shell_approvals';
import { runtime } from './runtime';
import type { Message, UiDeps } from './types/domain';
interface MessagesDeps extends UiDeps {
  openDocument(sessionId: string, ref: DocumentRef): void;
  statusRow(prefix: string, text: string, options?: Record<string, unknown>): HTMLElement;
  currentProjectPath(): string;
  openChangesDrawer(project: string, path?: string): unknown;
  attachCurrentChatToPickedProject(): unknown;
  approveCommand(id: string, approved: boolean): unknown;
  continueTask(sessionId: string): unknown;
  resumeTeaching(id: string, sessionId: string): unknown;
  getActiveId(): string;
  getRunningSessionId(): string | null;
}
export interface MessagesApi {
  init(deps: MessagesDeps): void;
  append(chat: HTMLElement, message: Message): void;
}
/* Message presentation; actions are supplied by the application core. */
(function () {
  'use strict';
  let deps: MessagesDeps = {} as MessagesDeps;
  function init(nextDeps: MessagesDeps): void {
    deps = nextDeps;
  }
  function append(chat: HTMLElement, m: Message): void {
    if (!runtime.Process.append(chat, m, appendContent)) appendContent(chat, m);
  }
  function appendContent(chat: HTMLElement, m: Message): void {
    const {
      statusRow,
      currentProjectPath,
      openChangesDrawer,
      attachCurrentChatToPickedProject,
      approveCommand,
      continueTask,
      resumeTeaching,
    } = deps;
    const activeId = deps.getActiveId?.() ?? '',
      runningSessionId = deps.getRunningSessionId?.() ?? null;
    const {
      messageCopyText,
      addMessageCopyButton,
      FOLDABLE_TOOL_KINDS,
      standaloneToolEl,
      appendOrFoldTool,
      escapeHtml,
    } = runtime.Render;

    if (m.type === 'user') {
      const div = document.createElement('div');
      div.className = 'msg user';
      if (m.id) div.dataset.messageId = m.id;
      div.innerHTML = `<div class="msg-label">You</div><div class="body"></div>`;
      const body = div.querySelector<HTMLElement>('.body');
      if (body) body.textContent = String(m.text);
      addMessageCopyButton(div, messageCopyText(m));
      chat.appendChild(div);
    } else if (m.type === 'request_status') {
      runtime.Requests.appendStatus(chat, m);
    } else if (m.type === 'turn') {
      const div = document.createElement('div');
      div.className = 'turn-divider';
      const label = document.createElement('span');
      const note = (m.note || '').toString().trim();
      label.textContent = `Turn ${m.n}${note ? ` ${note}` : ''}`;
      const line = document.createElement('span');
      line.className = 'line';
      div.appendChild(label);
      div.appendChild(line);
      chat.appendChild(div);
    } else if (m.type === 'asst') {
      const div = document.createElement('div');
      div.className = 'msg asst';
      const label = document.createElement('div');
      label.className = 'msg-label';
      label.textContent = 'Codey';
      div.appendChild(label);
      const body = document.createElement('div');
      body.className = 'body md';
      div.appendChild(body);
      runtime.Render.renderMarkdownChunked(body, m.text || '');
      addMessageCopyButton(div, messageCopyText(m));
      chat.appendChild(div);
    } else if (m.type === 'tool') {
      if (FOLDABLE_TOOL_KINDS.has(m.kind || '') && !m.error) {
        appendOrFoldTool(chat, m);
      } else {
        chat.appendChild(standaloneToolEl(m));
      }
    } else if (m.type === 'tool_pending') {
      chat.appendChild(standaloneToolEl(m));
    } else if (m.type === 'research_done') {
      const report = runtime.ResearchRuns.current(m.sessionId || activeId)?.synthesisId;
      const actions = runtime.RunDetails.actionsForMessage(m, [
        ...(report
          ? [
              {
                label: 'Open report',
                onclick: () => deps.openDocument(m.sessionId || activeId, { kind: 'research', note_id: report }),
              },
            ]
          : []),
        { label: 'Open', onclick: () => runtime.ResearchDrawer.open(m.sessionId || activeId) },
        { label: 'Use in Project', onclick: () => attachCurrentChatToPickedProject() },
      ]);
      chat.appendChild(
        statusRow('Research', m.text || 'Research saved', {
          kind: 'done',
          actions,
          copyText: m.text || '',
        }),
      );
    } else if (m.type === 'done') {
      const action =
        m.project && m.changed
          ? {
              label: 'View diff',
              onclick: () => openChangesDrawer(String(m.project || '')),
            }
          : null;
      const text = m.text || 'Task complete';
      chat.appendChild(
        statusRow('Done', text, {
          kind: 'done',
          actions: runtime.RunDetails.actionsForMessage(m, action ? [action] : []),
          copyText: text,
        }),
      );
    } else if (m.type === 'review') {
      const text = m.text || 'Complete';
      chat.appendChild(
        statusRow('Review', text, {
          kind: 'review',
          copyText: text,
        }),
      );
    } else if (m.type === 'changes') {
      const div = document.createElement('div');
      div.className = 'msg changes';
      div.dataset.project = typeof m.project === 'string' ? m.project : currentProjectPath();
      const head = document.createElement('div');
      head.className = 'changes-head-row';
      const label = document.createElement('span');
      label.className = 'msg-label';
      label.textContent = 'Changes';
      const count = document.createElement('span');
      count.className = 'changes-count';
      const n = m.count || 0;
      count.textContent = `${n} file${n === 1 ? '' : 's'}`;
      const view = document.createElement('button');
      view.className = 'link-btn';
      view.textContent = 'View diff';
      view.onclick = () => openChangesDrawer(typeof m.project === 'string' ? m.project : currentProjectPath());
      head.append(label, count, view);
      div.appendChild(head);
      const files = Array.isArray(m.files) ? m.files : [];
      if (files.length) {
        const list = document.createElement('div');
        list.className = 'changes-files';
        for (const f of files) {
          const row = document.createElement('div');
          row.className = 'changes-file';
          row.innerHTML = `<span class="cf-status">${escapeHtml(f.status || 'M')}</span><button class="cf-path link-btn">${escapeHtml(f.path || '')}</button><span class="cf-stat">+${f.additions || 0} -${f.deletions || 0}</span>`;
          list.appendChild(row);
        }
        div.appendChild(list);
      }
      chat.appendChild(div);
    } else if (m.type === 'shell_request') {
      const div = document.createElement('div');
      div.className = 'msg shell';
      const card = document.createElement('div');
      card.className = 'shell-card';
      card.dataset.approvalId = m.id;
      card.tabIndex = -1;
      const title = document.createElement('div');
      title.className = 'sc-title';
      title.textContent = 'Run this command?';
      const meta = document.createElement('div');
      meta.className = 'sc-meta';
      meta.textContent = m.cwd || '.';
      const commandBody = document.createElement('div');
      commandBody.className = 'sc-command-body';
      const commandLabel = document.createElement('div');
      commandLabel.className = 'sc-command-label';
      commandLabel.textContent = 'Shell';
      const cmd = document.createElement('pre');
      cmd.className = 'shell-command';
      cmd.textContent = m.command || '';
      const commandHash = m.commandSha256 || '';
      const commandIntegrity = document.createElement('div');
      commandIntegrity.className = 'sc-note';
      commandIntegrity.textContent =
        m.commandTruncated && commandHash ? `Command preview truncated · full sha256 ${commandHash}` : '';
      const error = document.createElement('div');
      error.className = 'sc-error';
      error.hidden = true;
      const actions = document.createElement('div');
      actions.className = 'shell-actions';
      const deny = document.createElement('button');
      deny.className = 'text-btn';
      deny.textContent = 'Deny';
      const approve = document.createElement('button');
      approve.className = 'text-btn approval-allow';
      const submitting = m.approvalSubmission === 'sending';
      approve.textContent = submitting ? 'Submitting…' : 'Allow once';
      if (submitting) {
        const spinner = document.createElement('span');
        spinner.className = 'spinner';
        spinner.setAttribute('aria-hidden', 'true');
        approve.prepend(spinner);
        approve.setAttribute('aria-busy', 'true');
      }
      approve.disabled = deny.disabled = !!m.approvalSubmission;
      if (m.approvalSubmission && !submitting) {
        error.hidden = false;
        error.textContent =
          m.approvalSubmission === 'checking' ? 'Confirming approval status…' : 'Decision could not be confirmed';
        if (m.approvalSubmission === 'unconfirmed') {
          const check = document.createElement('button');
          check.className = 'text-btn';
          check.textContent = 'Check status';
          check.onclick = () => runtime.ShellApprovals.refresh(m.id || '');
          actions.append(check);
        }
      }
      approve.onclick = () => approveCommand(m.id || '', true);
      deny.onclick = () => approveCommand(m.id || '', false);
      actions.append(deny, approve);
      commandBody.append(commandLabel, cmd, meta);
      if (commandIntegrity.textContent) commandBody.append(commandIntegrity);
      card.append(title, commandBody, error, actions);
      div.appendChild(card);
      chat.appendChild(div);
    } else if (m.type === 'shell_approval_status' && m.approval) {
      const state = m.approval;
      const actions =
        state.continuation === 'failed' && state.execution !== 'uncertain'
          ? [
              {
                label: 'Retry continuation',
                onclick: () => runtime.ShellApprovals.retry(state.id),
              },
            ]
          : [];
      const row = statusRow('Command', approvalText(state), { actions });
      row.dataset.approvalId = state.id;
      row.tabIndex = -1;
      chat.appendChild(row);
    } else if (m.type === 'shell_result') {
      chat.appendChild(commandCard(m, runtime.Render.copyText));
    } else if (m.type === 'limit') {
      chat.appendChild(
        statusRow('Limit', m.text || 'Turn limit reached', {
          actions: runtime.RunDetails.actionsForMessage(m, [
            { label: 'Continue', disabled: !!runningSessionId, onclick: () => continueTask(m.sessionId || activeId) },
          ]),
        }),
      );
    } else if (m.type === 'pause') {
      chat.appendChild(
        statusRow('Paused', m.text || 'No progress for several turns', {
          actions: runtime.RunDetails.actionsForMessage(m, [
            { label: 'Continue', disabled: !!runningSessionId, onclick: () => continueTask(m.sessionId || activeId) },
          ]),
        }),
      );
    } else if (m.type === 'teach') {
      chat.appendChild(
        statusRow('Paused', m.text || 'Click the control in the model page', {
          action: {
            label: 'Resume',
            disabled: false,
            onclick: () => resumeTeaching(m.id || '', m.sessionId || activeId),
          },
        }),
      );
    } else if (m.type === 'err') {
      chat.appendChild(
        statusRow('Error', m.text || '', {
          err: true,
          actions: runtime.RunDetails.actionsForMessage(m, runtime.Composer.recoveryActions(m)),
        }),
      );
    } else if (m.type === 'info') {
      const div = document.createElement('div');
      div.className = 'info-line';
      div.textContent = m.text || '';
      chat.appendChild(div);
    }
  }

  runtime.Messages = { init, append };
})();
