import type { DocumentRef } from './document_pane';
import { commandCard } from './command_card';
import { approvalText } from './shell_approvals';
import { runtime } from './runtime';
import { hydrateDocumentContent } from './document_content';
import { artifactRow, decorateDeliverables } from './artifacts';
import type { Message, UiDeps } from './types/domain';
import { finalAnswerMarkdown } from './answer_copy';
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
  updateReply(chat: HTMLElement, message: Message): boolean;
}
/* Message presentation; actions are supplied by the application core. */
(function () {
  'use strict';
  let deps: MessagesDeps = {} as MessagesDeps;
  function init(nextDeps: MessagesDeps): void {
    deps = nextDeps;
  }
  function streamStatus(node: HTMLElement, message: Message): void {
    let note = node.querySelector<HTMLElement>('.reply-stream-note');
    if (message.streamState !== 'interrupted') {
      note?.remove();
      return;
    }
    if (!note) {
      note = document.createElement('div');
      note.className = 'reply-stream-note';
      note.setAttribute('role', 'status');
      node.append(note);
    }
    note.textContent = 'Response interrupted · partial text retained';
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
      const actions = document.createElement('div');
      actions.className = 'message-actions';
      const recall = document.createElement('button');
      recall.type = 'button';
      recall.className = 'msg-recall';
      recall.setAttribute('aria-label', 'Return message to draft');
      recall.title = 'Return message to draft';
      recall.onclick = () => runtime.Composer.recallMessage(m.sessionId || activeId, String(m.text || ''));
      const copy = div.querySelector('.msg-copy'),
        feedback = div.querySelector('.copy-feedback');
      if (copy) actions.append(copy);
      actions.append(recall);
      if (feedback) actions.append(feedback);
      div.append(actions);
      chat.appendChild(div);
    } else if (m.type === 'request_status') {
      runtime.Requests.appendStatus(chat, m);
    } else if (m.type === 'user_question') {
      const div = document.createElement('div');
      div.className = 'msg user-question';
      div.append(runtime.UserQuestions.card(m.id || ''));
      chat.append(div);
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
      if (m.runId) div.dataset.runId = m.runId;
      if (m.streamId) div.dataset.streamId = m.streamId;
      const label = document.createElement('div');
      label.className = 'msg-label';
      label.textContent = 'Codey';
      div.appendChild(label);
      const body = document.createElement('div');
      body.className = 'body md';
      div.appendChild(body);
      runtime.Render.updateMarkdown(body, m.text || '');
      {
        void hydrateDocumentContent(body, {
          sessionId: m.sessionId || activeId,
          workspace: currentProjectPath() || undefined,
        }).then(() => decorateDeliverables(body));
      }
      if (!m.streamId || m.finalAnswer) {
        const sessionId = m.sessionId || activeId;
        const final = m.contentRole === 'final' && !!m.turnId && !!m.answerId;
        addMessageCopyButton(
          div,
          final
            ? () => {
                const session = deps.getSessions?.().find((item) => item.id === sessionId);
                return session ? finalAnswerMarkdown(session.messages, m.turnId || '') || m.text || '' : m.text || '';
              }
            : () => messageCopyText(m),
          final ? 'final' : 'message',
        );
      }
      streamStatus(div, m);
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
      const sessionId = m.sessionId || activeId;
      const report = runtime.ResearchRuns.forMessage(sessionId, m.runId || '')?.synthesisId;
      if (report) {
        chat.append(
          artifactRow(
            'Research report.md',
            'Research · Markdown',
            () => deps.openDocument(sessionId, { kind: 'research', note_id: report }),
            [
              { label: 'View research evidence', action: () => runtime.ResearchDrawer.open(sessionId, m.runId || '') },
              { label: 'Use in Project', action: () => attachCurrentChatToPickedProject() },
            ],
          ),
        );
        return;
      }
      const actions = runtime.RunDetails.actionsForMessage(m, [
        {
          label: 'View research evidence',
          onclick: () => runtime.ResearchDrawer.open(m.sessionId || activeId, m.runId || ''),
        },
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

  function updateReply(chat: HTMLElement, message: Message): boolean {
    if (message.type !== 'asst' || !message.streamId) return false;
    const node = [...chat.querySelectorAll<HTMLElement>('.msg.asst')].find(
      (node) => node.dataset.streamId === message.streamId && node.dataset.runId === message.runId,
    );
    const body = node?.querySelector<HTMLElement>('.body');
    if (!node || !body) return false;
    runtime.Render.updateMarkdown(body, message.text || '');
    streamStatus(node, message);
    if (message.finalAnswer && !node.querySelector('.msg-copy')) {
      const sessionId = message.sessionId || deps.getActiveId();
      const final = message.contentRole === 'final' && !!message.turnId && !!message.answerId;
      runtime.Render.addMessageCopyButton(
        node,
        final
          ? () => {
              const session = deps.getSessions?.().find((item) => item.id === sessionId);
              return session
                ? finalAnswerMarkdown(session.messages, message.turnId || '') || message.text || ''
                : message.text || '';
            }
          : () => message.text || '',
        final ? 'final' : 'message',
      );
    }
    if (message.streamState !== 'partial')
      void hydrateDocumentContent(body, {
        sessionId: message.sessionId || deps.getActiveId(),
        workspace: deps.currentProjectPath() || undefined,
      }).then(() => decorateDeliverables(body));
    return true;
  }
  runtime.Messages = { init, append, updateReply };
})();
