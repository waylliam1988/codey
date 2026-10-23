import { mergeCommandResult } from './command_result';
import type { DocumentRef } from './document_pane';
import { commandCard } from './command_card';
import { approvalText } from './shell_approvals';
import { runtime } from './runtime';
import { hydrateDocumentContent } from './document_content';
import { artifactRow, decorateDeliverables } from './artifacts';
import type { Message, UiDeps } from './types/domain';
import { finalAnswerMarkdown } from './answer_copy';
import { createIconSvg, iconButton } from './icon_button';
import { approvalRequest } from './approval_request';
interface MessagesDeps extends UiDeps {
  openDocument(sessionId: string, ref: DocumentRef): void;
  statusRow(prefix: string, text: string, options?: Record<string, unknown>): HTMLElement;
  currentProjectPath(): string;
  openChangesDrawer(project: string, path?: string): unknown;
  attachCurrentChatToPickedProject(): unknown;
  approveCommand(id: string, approved: boolean, scope?: string): unknown;
  continueTask(sessionId: string): unknown;
  resumeTeaching(id: string, sessionId: string): unknown;
  getActiveId(): string;
  getRunningSessionId(): string | null;
}
export interface MessagesApi {
  init(deps: MessagesDeps): void;
  append(chat: HTMLElement, message: Message): void;
  updateReply(chat: HTMLElement, message: Message): boolean;
  updateApprovals(chat: HTMLElement, messages: Message[]): boolean;
  updateAnswerIdentity(chat: HTMLElement, message: Message): boolean;
}
/* Message presentation; actions are supplied by the application core. */
(function () {
  'use strict';
  let deps: MessagesDeps = {} as MessagesDeps;
  const approvalViews = new WeakMap<Element, string>();
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
  function answerActions(node: HTMLElement, message: Message): void {
    if (!message.entryId || message.finalAnswer === false) return;
    node.dataset.entryId = message.entryId;
    let actions = node.querySelector<HTMLElement>(':scope > .message-actions');
    if (!actions) {
      actions = document.createElement('div');
      actions.className = 'message-actions';
      node.append(actions);
    }
    const copy = node.querySelector('.msg-copy'),
      feedback = node.querySelector('.copy-feedback');
    if (copy) actions.prepend(copy);
    let fork = actions.querySelector<HTMLButtonElement>('.msg-fork');
    if (!fork) {
      fork = iconButton('Fork chat from here', 'branch', () => {});
      fork.classList.add('msg-fork');
      actions.append(fork);
    }
    fork.onclick = () => runtime.ChatFork?.open(message.sessionId || deps.getActiveId(), message.entryId);
    if (feedback) actions.append(feedback);
  }
  function updateAnswerIdentity(chat: HTMLElement, message: Message): boolean {
    const candidates = Array.from(chat.querySelectorAll<HTMLElement>('.msg.asst')).filter((node) =>
      message.streamId
        ? node.dataset.streamId === message.streamId && node.dataset.runId === message.runId
        : node.dataset.entryId === message.entryId || (!!message.runId && node.dataset.runId === message.runId),
    );
    if (candidates.length !== 1) return false;
    answerActions(candidates[0], message);
    return true;
  }
  function updateApprovals(chat: HTMLElement, messages: Message[]): boolean {
    const ids = new Set(
      messages
        .filter((m) => ['shell_request', 'shell_result', 'shell_approval_status'].includes(m.type))
        .map((m) => m.id),
    );
    let updated = false;
    for (const id of ids) {
      if (!id) continue;
      const rows = [
        ...new Set(
          Array.from(
            chat.querySelectorAll<HTMLElement>(`[data-approval-id="${CSS.escape(id)}"]`),
            (node) => node.closest<HTMLElement>('.msg') || node,
          ),
        ),
      ];
      if (!rows.length) continue;
      const records = messages.filter(
        (m) => m.id === id && ['shell_request', 'shell_result', 'shell_approval_status'].includes(m.type),
      );
      const signature = JSON.stringify(records);
      if (approvalViews.get(rows[0]) === signature) {
        updated = true;
        continue;
      }
      const result = records
        .filter((message) => message.type === 'shell_result' && message.runId)
        .reduce<Message | undefined>((previous, message) => mergeCommandResult(previous, message), undefined);
      if (result) {
        runtime.Process.append(chat, result, appendContent);
        for (const message of records) {
          if (message.type !== 'shell_result') runtime.Process.append(chat, message, () => {});
        }
        for (const row of rows) if (!row.closest('.process-block')) row.remove();
        for (const node of chat.querySelectorAll<HTMLElement>(`[data-approval-id="${CSS.escape(id)}"]`))
          approvalViews.set(node, signature);
        updated = true;
        continue;
      }
      const fragment = document.createElement('div');
      for (const message of records) {
        runtime.Process.append(chat, message, () => {});
        appendContent(fragment, message);
      }
      const open = rows.find((row) => row.matches('.command-card'))?.querySelector<HTMLDetailsElement>('details')?.open;
      const details = fragment.querySelector<HTMLDetailsElement>('.command-card details');
      if (details && open !== undefined) details.open = open;
      const focused = rows.some((row) => row.contains(document.activeElement));
      const first = fragment.firstElementChild;
      for (const node of Array.from(fragment.children)) approvalViews.set(node, signature);
      rows[0].replaceWith(...fragment.childNodes);
      rows.slice(1).forEach((row) => row.remove());
      if (focused && first instanceof HTMLElement) {
        first.tabIndex = -1;
        first.focus({ preventScroll: true });
      }
      updated = true;
    }
    return updated;
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
    const { messageCopyText, addMessageCopyButton, isFoldableTool, standaloneToolEl, appendOrFoldTool, escapeHtml } =
      runtime.Render;

    if (m.type === 'user') {
      const div = document.createElement('div');
      div.className = 'msg user';
      if (m.id) div.dataset.messageId = m.id;
      div.innerHTML = `<div class="msg-label">You</div><div class="body"></div>`;
      const label = div.querySelector<HTMLElement>('.msg-label');
      if (label && m.source?.name) label.textContent = m.source.name;
      if (m.source?.actor_id) div.dataset.sourceActor = m.source.actor_id;
      const body = div.querySelector<HTMLElement>('.body');
      if (body) body.textContent = String(m.text);
      addMessageCopyButton(div, messageCopyText(m));
      const actions = document.createElement('div');
      actions.className = 'message-actions';
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'msg-edit';
      edit.setAttribute('aria-label', 'Edit message');
      edit.title = 'Edit message';
      edit.append(createIconSvg('edit'));
      edit.onclick = () => beginMessageEdit(div, body, actions, m, activeId);
      const copy = div.querySelector('.msg-copy'),
        feedback = div.querySelector('.copy-feedback');
      if (copy) actions.append(copy);
      actions.append(edit);
      if (m.entryId)
        actions.append(iconButton('Fork chat from here', 'branch', () => runtime.ChatFork?.open(activeId, m.entryId)));
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
      answerActions(div, m);
      streamStatus(div, m);
      chat.appendChild(div);
    } else if (m.type === 'tool') {
      if (isFoldableTool(m)) {
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
      view.type = 'button';
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
      const row = approvalRequest(m, approveCommand);
      approvalViews.set(row, JSON.stringify([m]));
      chat.append(row);
    } else if (m.type === 'shell_approval_status' && m.approval) {
      const state = m.approval;
      // Successful continuation is an internal transition; the shell result
      // and the resumed answer already provide the user-facing evidence.
      if (state.continuation === 'started') return;
      if (!['failed', 'unavailable', 'cancelled'].includes(state.continuation) && state.execution !== 'uncertain')
        return;
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
      const row = commandCard(m, runtime.Render.copyText);
      approvalViews.set(row, JSON.stringify([m]));
      chat.appendChild(row);
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

  function beginMessageEdit(
    div: HTMLElement,
    body: HTMLElement | null,
    actions: HTMLElement,
    message: Message,
    activeId: string,
  ): void {
    if (!body || div.classList.contains('editing')) return;
    const editor = document.createElement('div');
    editor.className = 'msg-edit-editor';
    const input = document.createElement('textarea');
    input.className = 'msg-edit-input';
    input.value = body.textContent || String(message.text || '');
    input.rows = Math.min(8, Math.max(2, input.value.split(/\r?\n/).length));
    input.setAttribute('aria-label', 'Edit message');
    const controls = document.createElement('div');
    controls.className = 'msg-edit-actions';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'text-btn';
    cancel.textContent = 'Cancel';
    const send = document.createElement('button');
    send.type = 'button';
    send.className = 'text-btn msg-edit-send';
    send.textContent = 'Send';
    controls.append(cancel, send);
    editor.append(input, controls);
    div.classList.add('editing');
    body.replaceWith(editor);
    actions.hidden = true;
    const finish = () => {
      if (!editor.parentElement) return;
      editor.replaceWith(body);
      actions.hidden = false;
      div.classList.remove('editing');
    };
    cancel.onclick = finish;
    input.onkeydown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        finish();
      } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        send.click();
      }
    };
    send.onclick = async () => {
      const text = input.value.trim();
      if (!text) {
        input.focus();
        return;
      }
      input.disabled = true;
      cancel.disabled = true;
      send.disabled = true;
      send.textContent = 'Sending…';
      let sent = false;
      try {
        sent = await runtime.Composer.editMessage(message.sessionId || activeId, text);
      } catch {
        sent = false;
      }
      if (sent) return;
      input.disabled = false;
      cancel.disabled = false;
      send.disabled = false;
      send.textContent = 'Send';
      input.focus();
    };
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
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
    answerActions(node, message);
    if (message.streamState !== 'partial')
      void hydrateDocumentContent(body, {
        sessionId: message.sessionId || deps.getActiveId(),
        workspace: deps.currentProjectPath() || undefined,
      }).then(() => decorateDeliverables(body));
    return true;
  }
  runtime.Messages = { init, append, updateReply, updateApprovals, updateAnswerIdentity };
})();
