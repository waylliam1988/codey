/* Message presentation; actions are supplied by the application core. */
(function () {
'use strict';
let deps = null;
function init(nextDeps) { deps = nextDeps; }
function append(chat, m) {
  if (!window.CodeyProcess.append(chat, m, appendContent)) appendContent(chat, m);
}
function appendContent(chat, m) {
  const {statusRow, currentProjectPath, openChangesDrawer, attachCurrentChatToPickedProject,
    approveCommand, continueTask, resumeTeaching} = deps;
  const activeId = deps.getActiveId(), runningSessionId = deps.getRunningSessionId();
  const {messageCopyText, addMessageCopyButton, FOLDABLE_TOOL_KINDS, standaloneToolEl,
    appendOrFoldTool, escapeHtml} = window.CodeyRender;

  if (m.type === 'user') {
    const div = document.createElement('div');
    div.className = 'msg user';
    if (m.id) div.dataset.messageId = m.id;
    div.innerHTML = `<div class="msg-label">You</div><div class="body"></div>`;
    div.querySelector('.body').textContent = m.text;
    addMessageCopyButton(div, messageCopyText(m));
    chat.appendChild(div);
  } else if (m.type === 'request_status') {
    window.CodeyRequests.appendStatus(chat, m);
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
    const toggle = document.createElement('button');
    toggle.className = 'toggle';
    const ctl = window.CodeyRender.renderAssistantBody(body, m.text || '', toggle);
    if (ctl.long) {
      toggle.textContent = 'Collapse';
      toggle.onclick = () => { const on = ctl.isExpanded(); if (on) ctl.collapse(); else ctl.expand(); toggle.textContent = on ? 'Expand' : 'Collapse'; toggle.setAttribute('aria-expanded', String(!on)); };
      div.appendChild(toggle);
    }
    addMessageCopyButton(div, messageCopyText(m));
    chat.appendChild(div);
  } else if (m.type === 'tool') {
    if (FOLDABLE_TOOL_KINDS.has(m.kind) && !m.error) {
      appendOrFoldTool(chat, m);
    } else {
      chat.appendChild(standaloneToolEl(m));
    }
  } else if (m.type === 'tool_pending') {
    chat.appendChild(standaloneToolEl(m));
  } else if (m.type === 'research_done') {
    const actions = window.CodeyRunDetails.actionsForMessage(m, [
      { label: 'Open', onclick: () => window.CodeyResearchDrawer.open(m.sessionId || activeId) },
      { label: 'Use in Project', onclick: () => attachCurrentChatToPickedProject() },
    ]);
    chat.appendChild(statusRow('Research', m.text || 'Research saved', {
      kind: 'done',
      actions,
      copyText: m.text || '',
    }));
  } else if (m.type === 'done') {
    const action = m.project && m.changed ? {
      label: 'View diff',
      onclick: () => openChangesDrawer(m.project),
    } : null;
    const text = m.text || 'Task complete';
    chat.appendChild(statusRow('Done', text, {
      kind: 'done',
      actions: window.CodeyRunDetails.actionsForMessage(m, [action]),
      copyText: text,
    }));
  } else if (m.type === 'review') {
    const text = m.text || 'Complete';
    chat.appendChild(statusRow('Review', text, {
      kind: 'review',
      copyText: text,
    }));
  } else if (m.type === 'changes') {
    const div = document.createElement('div');
    div.className = 'msg changes';
    div.dataset.project = m.project || currentProjectPath();
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
    view.onclick = () => openChangesDrawer(m.project || currentProjectPath());
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
    card.dataset.approvalId = m.id; card.tabIndex = -1;
    const title = document.createElement('div');
    title.className = 'sc-title';
    title.textContent = 'Approval required';
    const meta = document.createElement('div');
    meta.className = 'sc-meta';
    meta.textContent = m.cwd ? `Runs in ${m.cwd}` : 'Runs in .';
    const riskTitle = m.riskTitle || m.riskLabel || '';
    const riskDetail = m.riskDetail || '';
    const note = document.createElement('div');
    note.className = 'sc-note';
    note.textContent = riskTitle || riskDetail
      ? `${riskTitle || 'Shell command'}${riskDetail ? ` · ${riskDetail}` : ''}`
      : '';
    const cmd = document.createElement('div');
    cmd.className = 'shell-command';
    cmd.textContent = '$ ' + (m.command || '');
    const commandHash = m.commandSha256 || '';
    const commandIntegrity = document.createElement('div');
    commandIntegrity.className = 'sc-note';
    commandIntegrity.textContent = m.commandTruncated && commandHash
      ? `Command preview truncated · full sha256 ${commandHash}`
      : '';
    const error = document.createElement('div');
    error.className = 'sc-error';
    error.hidden = true;
    const actions = document.createElement('div');
    actions.className = 'shell-actions';
    const deny = document.createElement('button');
    deny.className = 'text-btn';
    deny.textContent = 'Deny';
    const approve = document.createElement('button');
    approve.className = 'text-btn primary';
    approve.textContent = 'Allow';
    approve.onclick = () => approveCommand(m.id, true, approve, deny, error);
    deny.onclick = () => approveCommand(m.id, false, approve, deny, error);
    actions.append(deny, approve);
    card.append(title, meta);
    if (note.textContent) card.appendChild(note);
    if (commandIntegrity.textContent) card.appendChild(commandIntegrity);
    card.append(cmd, error, actions);
    div.appendChild(card);
    chat.appendChild(div);
  } else if (m.type === 'shell_result') {
    const div = document.createElement('div');
    div.className = 'msg shell';
    const out = document.createElement('div');
    out.className = 'shell-output';
    const exit = m.exitCode === null || m.exitCode === undefined ? '' : `exit ${m.exitCode}\n`;
    const verb = window.CodeyRender.shellStatusVerb(m.shellStatus, m.approved);
    out.textContent = `${verb}\n$ ${m.command || ''}\n${exit}${m.output || ''}`;
    div.appendChild(out);
    chat.appendChild(div);
  } else if (m.type === 'limit') {
    chat.appendChild(statusRow('Limit', m.text || 'Turn limit reached', {
      actions: window.CodeyRunDetails.actionsForMessage(m, [
        { label: 'Continue', disabled: !!runningSessionId, onclick: () => continueTask(m.sessionId || activeId) },
      ]),
    }));
  } else if (m.type === 'pause') {
    chat.appendChild(statusRow('Paused', m.text || 'No progress for several turns', {
      actions: window.CodeyRunDetails.actionsForMessage(m, [
        { label: 'Continue', disabled: !!runningSessionId, onclick: () => continueTask(m.sessionId || activeId) },
      ]),
    }));
  } else if (m.type === 'teach') {
    chat.appendChild(statusRow('Paused', m.text || 'Click the control in the model page', {
      action: { label: 'Resume', disabled: false, onclick: () => resumeTeaching(m.id, m.sessionId || activeId) },
    }));
  } else if (m.type === 'err') {
    chat.appendChild(statusRow('Error', m.text || '', {
      err: true,
      actions: window.CodeyRunDetails.actionsForMessage(m, window.CodeyComposer.recoveryActions(m)),
    }));
  } else if (m.type === 'info') {
    const div = document.createElement('div');
    div.className = 'info-line';
    div.textContent = m.text;
    chat.appendChild(div);
  }
}

window.CodeyMessages = {init, append};
})();
