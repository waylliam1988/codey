import { runtime } from './runtime';
import type { ApprovalState } from './types/domain';
import type { ToolPreview } from './render';
/* Optional reasoning and trusted tool records, grouped by one user request. */
export interface ProcessSnapshot {
  runId?: string;
  touched?: boolean;
  open?: boolean;
  thinking: boolean[];
  tools: boolean[];
  details?: Record<string, boolean>;
}
export interface ProcessApi {
  append(
    chat: HTMLElement,
    message: ProcessToolRecord,
    render: (chat: HTMLElement, message: import('./types/domain').Message) => void,
  ): boolean;
  replace(chat: HTMLElement, message: ProcessToolRecord): boolean;
  capture(chat: HTMLElement): ProcessSnapshot[];
  restore(chat: HTMLElement, saved?: ProcessSnapshot[]): void;
}
export interface ProcessToolRecord {
  finalAnswer?: boolean;
  contentRole?: 'commentary' | 'final';
  preview?: ToolPreview;
  result?: string;
  elapsedMs?: number;
  streamId?: string;
  streamState?: string;
  approval?: Pick<ApprovalState, 'decision' | 'execution' | 'continuation'>;
  toolKey?: string;
  callId?: string;
  parentCallId?: string;
  entryId?: string;
  toolName?: string;
  kind?: string;
  path?: string;
  activity?: string;
  pending?: boolean;
  error?: boolean;
  status?: string;
  changed?: boolean;
  runId?: string;
  n?: string | number;
  turn?: string | number;
  reasoning?: string;
  text?: string;
  state?: string;
  type: string;
}
(function () {
  'use strict';

  type ProcessStatus =
    | 'sending'
    | 'running'
    | 'done'
    | 'failed'
    | 'error'
    | 'stopped'
    | 'max_turns'
    | 'no_progress'
    | 'approval'
    | 'question'
    | 'blocked';
  type ToolRecord = ProcessToolRecord;
  interface ProcessState {
    state: ProcessStatus;
    explicitState: boolean;
    tools: Map<string, ToolRecord>;
    reasoning: Set<string>;
    warnings: Set<string>;
    touched: boolean;
    requestObserved?: boolean;
    activity?: string;
    elapsedMs?: number;
  }
  type ProcessBlock = HTMLDivElement & { _runIds: Set<string>; _process: ProcessState };

  function find(chat: HTMLElement, id: string): ProcessBlock | undefined {
    return Array.from(chat.querySelectorAll<HTMLElement>('.process-block'))
      .map((node) => node as ProcessBlock)
      .find((node) => node._runIds.has(id));
  }
  function ensure(chat: HTMLElement, id: string): ProcessBlock {
    let block = find(chat, id);
    if (block) return block;
    const children = Array.from(chat.children);
    const answer = children.find(
      (node) => node instanceof HTMLElement && node.classList.contains('asst') && node.dataset.runId === id,
    );
    const recent = children
      .slice(0, answer ? children.indexOf(answer) + 1 : undefined)
      .filter((node): node is ProcessBlock => node instanceof HTMLElement)
      .reverse();
    const userIndex = recent.findIndex((node) => node.classList.contains('user'));
    block = recent
      .slice(0, userIndex < 0 ? recent.length : userIndex)
      .find((node) => node.classList.contains('process-block'));
    if (block) {
      block._runIds.add(id);
      return block;
    }
    block = document.createElement('div') as ProcessBlock;
    block.className = 'msg process-block';
    block.dataset.runId = id;
    block.hidden = true;
    block._runIds = new Set([id]);
    const label = document.createElement('div');
    label.className = 'msg-label';
    label.textContent = 'Codey';
    const group = document.createElement('details');
    group.className = 'process-group';
    group.open = false;
    const head = document.createElement('summary');
    head.className = 'process-summary';
    const title = document.createElement('span');
    title.className = 'process-title';
    title.setAttribute('role', 'status');
    title.setAttribute('aria-live', 'polite');
    head.append(title);
    const body = document.createElement('div');
    body.className = 'process-body';
    const alerts = document.createElement('div');
    alerts.className = 'process-alerts';
    const approvalStatus = document.createElement('div');
    approvalStatus.className = 'process-approval-status';
    approvalStatus.textContent = 'Waiting for approval';
    approvalStatus.setAttribute('role', 'status');
    approvalStatus.setAttribute('aria-live', 'polite');
    approvalStatus.hidden = true;
    group.append(head, body);
    block.append(label, approvalStatus, group, alerts);
    chat.insertBefore(block, answer || null);
    block._process = {
      state: 'running',
      explicitState: false,
      tools: new Map(),
      reasoning: new Set(),
      warnings: new Set(),
      touched: false,
    };
    head.addEventListener('click', () => {
      block._process.touched = true;
    });
    group.addEventListener('toggle', () => runtime.ConversationUI?.updateLatest());
    return block;
  }
  function countLabel(verb: string, set: Set<string>, noun: string): string {
    return set.size ? verb + ' ' + set.size + ' ' + noun + (set.size === 1 ? '' : 's') : '';
  }
  function summary(block: ProcessBlock): void {
    const state = block._process;
    const tools = Array.from(state.tools.values());
    const reads = new Set<string>(),
      edits = new Set<string>(),
      commands = new Set<string>();
    let explored = 0,
      searched = 0;
    for (const m of tools) {
      if (m.pending || m.error || m.status === 'error') continue;
      const name = runtime.Render.toolKind({ kind: m.kind, toolName: m.toolName }) || m.toolName || m.kind || '';
      if (['read', 'read_file'].includes(name) && m.path && !/^https?:/i.test(m.path)) reads.add(m.path);
      else if (
        ['edit', 'write', 'patch', 'edit_file', 'write_file', 'apply_patch', 'replace_in_file'].includes(name) &&
        m.changed &&
        m.path
      )
        edits.add(m.path);
      else if (['run', 'shell', 'run_command'].includes(name)) commands.add(m.toolKey || JSON.stringify(m));
      else if (['search', 'grep', 'web_search', 'references', 'find_references'].includes(name)) searched++;
      else if (['ls', 'list_dir'].includes(name)) explored++;
    }
    const counts = [
      countLabel('read', reads, 'file'),
      countLabel('edited', edits, 'file'),
      countLabel('ran', commands, 'command'),
      explored ? 'explored ' + explored + ' location' + (explored === 1 ? '' : 's') : '',
      searched ? 'searched ' + searched + ' time' + (searched === 1 ? '' : 's') : '',
    ].filter(Boolean);
    const pending = tools
      .slice()
      .reverse()
      .find((m) => m.pending);
    const titles = {
      sending: 'Sending…',
      running: 'Working',
      done: 'Worked',
      failed: 'Failed',
      error: 'Failed',
      stopped: 'Stopped',
      max_turns: 'Paused',
      no_progress: 'Paused',
      approval: 'Waiting for approval',
      question: 'Waiting for your answer',
      blocked: 'Paused',
    };
    let text = titles[state.state] || 'Paused';
    if (state.state === 'done' && state.elapsedMs !== undefined)
      text += ' for ' + Math.max(1, Math.round(state.elapsedMs / 1000)) + 's';
    if (state.state === 'running')
      text += state.activity
        ? ' · ' + state.activity
        : pending
          ? ' · ' +
            (['run', 'shell', 'run_command'].includes(pending.kind || '')
              ? 'Running command'
              : pending.activity || pending.kind + ' ' + pending.path)
          : ' · Waiting for reply';
    else if (counts.length) text += ' · ' + counts.join(' · ');
    const title = block.querySelector<HTMLElement>('.process-title');
    if (title)
      title.textContent =
        !state.requestObserved && !state.explicitState && !tools.length && !state.reasoning.size && state.warnings.size
          ? 'Activity'
          : text;
    const head = block.querySelector<HTMLElement>('.process-summary')!;
    const awaitingApproval = state.state === 'approval' || state.state === 'question';
    block.querySelector<HTMLElement>('.process-approval-status')!.textContent =
      state.state === 'question' ? 'Waiting for your answer' : 'Waiting for approval';
    block.querySelector<HTMLElement>('.process-approval-status')!.hidden = !awaitingApproval;
    // Sending is a transport state, not a user-expandable activity. Keep the
    // label visible while the request is admitted, but do not add a disclosure
    // arrow that suggests there is content to inspect yet.
    block.classList.toggle('process-sending', state.state === 'sending');
    block.querySelector<HTMLElement>('.process-group')!.hidden = awaitingApproval;
    const spinning = state.state === 'running' || state.state === 'sending';
    let spinner = head.querySelector<HTMLElement>('.spinner');
    if (spinning && !spinner) {
      spinner = document.createElement('span');
      spinner.className = 'spinner';
      spinner.setAttribute('aria-hidden', 'true');
      head.prepend(spinner);
    } else if (!spinning) spinner?.remove();
    block.hidden =
      !state.requestObserved && !state.explicitState && !tools.length && !state.reasoning.size && !state.warnings.size;
  }
  function thinking(block: ProcessBlock, text: unknown, step: string | number | undefined): void {
    if (typeof text !== 'string' || !text.trim()) return;
    const key = String(step || 0) + ':' + text;
    if (block._process.reasoning.has(key)) return;
    block._process.reasoning.add(key);
    let details = block.querySelector<HTMLDetailsElement>('.thinking');
    if (!details) {
      details = document.createElement('details');
      details.className = 'thinking';
      const head = document.createElement('summary');
      head.textContent = 'Thinking';
      const body = document.createElement('div');
      body.className = 'thinking-body';
      details.append(head, body);
      block.querySelector('.process-body')?.append(details);
    }
    const section = document.createElement('div');
    section.className = 'thinking-section';
    runtime.Render.renderMarkdown(section, text.trim());
    details.querySelector('.thinking-body')?.append(section);
  }

  function tool(block: ProcessBlock, m: ToolRecord): void {
    const key = m.toolKey || JSON.stringify(m);
    const previous = block._process.tools.get(key);
    if (previous && !previous.pending && m.pending) return;
    block._process.tools.set(key, m);
    const existing = m.toolKey
      ? Array.from(block.querySelectorAll<HTMLElement>('[data-tool-key]')).find(
          (el) => el.dataset.toolKey === m.toolKey,
        )
      : undefined;
    const target = block.querySelector<HTMLElement>(m.error ? '.process-alerts' : '.process-body');
    if (!target) return;
    const children = existing?.querySelector(':scope > .tool-children');
    const disclosures = existing
      ? Array.from(existing.querySelectorAll<HTMLDetailsElement>('details')).map((detail) => detail.open)
      : [];
    if (existing) existing.remove();
    if (m.pending || m.error || m.parentCallId || m.toolName === 'codemode')
      target.append(runtime.Render.standaloneToolEl(m));
    else runtime.Render.appendOrFoldTool(target, m);
    if (m.callId) {
      const node = Array.from(target.querySelectorAll<HTMLElement>('[data-tool-key]')).find(
        (element) => element.dataset.toolKey === m.toolKey,
      );
      if (node) {
        node.dataset.callId = m.callId;
        node.dataset.callScope = m.runId || '';
        if (m.entryId) node.dataset.entryId = m.entryId;
        if (children) node.append(children);
        node.querySelectorAll<HTMLDetailsElement>('details').forEach((detail, index) => {
          if (disclosures[index] !== undefined) detail.open = disclosures[index];
        });
      }
      syncToolTree(block);
    }
    summary(block);
  }

  function syncToolTree(block: ProcessBlock): void {
    const records = [...block._process.tools.values()].filter((record) => record.callId);
    const byCall = new Map(records.map((record) => [`${record.runId}:${record.callId}`, record]));
    const nodes = new Map<string, HTMLElement>();
    for (const node of block.querySelectorAll<HTMLElement>('[data-call-id]')) {
      const key = `${node.dataset.callScope}:${node.dataset.callId}`;
      if (!nodes.get(key)?.contains(node)) nodes.set(key, node);
    }
    for (const record of records) {
      const own = `${record.runId}:${record.callId}`;
      const node = nodes.get(own);
      if (!node || !record.parentCallId || record.error) continue;
      const parent = nodes.get(`${record.runId}:${record.parentCallId}`);
      let cursor: ToolRecord | undefined = record;
      const seen = new Set<string>();
      let depth = 0;
      while (cursor?.parentCallId && depth <= 6) {
        const key = `${cursor.runId}:${cursor.callId}`;
        if (seen.has(key)) break;
        seen.add(key);
        cursor = byCall.get(`${cursor.runId}:${cursor.parentCallId}`);
        depth++;
      }
      // Missing parents stay visible at the root until their event arrives.
      if (!parent || parent === node || node.contains(parent) || depth > 6 || cursor?.parentCallId) continue;
      let children = parent.querySelector<HTMLElement>(':scope > .tool-children');
      if (!children) {
        children = document.createElement('div');
        children.className = 'tool-children';
        children.setAttribute('role', 'group');
        children.setAttribute('aria-label', 'Nested tool calls');
        parent.append(children);
      }
      node.dataset.toolDepth = String(depth);
      children.append(node);
    }
  }
  function state(block: ProcessBlock, value: ProcessStatus, explicit = true): void {
    block._process.state = value;
    block._process.activity = undefined;
    if (explicit) block._process.explicitState = true;
    const group = block.querySelector<HTMLDetailsElement>('.process-group');
    if (group && value !== 'running' && !block._process.touched) group.open = false;
    summary(block);
  }
  function append(chat: HTMLElement, m: ToolRecord, render: (chat: HTMLElement, message: ToolRecord) => void): boolean {
    if (m.type === 'request_activity' && m.runId) {
      const pending = m.state === 'sending' || m.state === 'running';
      const existing = find(chat, m.runId);
      const block = pending ? ensure(chat, m.runId) : existing;
      if (block) {
        block._process.requestObserved = true;
        if (pending && !existing) block._process.explicitState = false;
        if (pending && (!existing || !block._process.explicitState)) state(block, m.state as ProcessStatus, false);
        else if (!pending && ['sending', 'running'].includes(block._process.state))
          state(block, m.state === 'paused' ? 'blocked' : (m.state as ProcessStatus), false);
        summary(block);
      }
      return true;
    }
    if (m.type === 'local_update_warning' || (m.type === 'info' && m.text === 'Local update paused')) {
      const block = ensure(chat, m.runId || 'local-updates');
      const text = m.type === 'info' ? 'Local memory update unavailable' : m.text || 'Local memory update unavailable';
      if (!block._process.warnings.has(text)) {
        block._process.warnings.add(text);
        const diagnostic = document.createElement('div');
        diagnostic.className = 'process-diagnostic';
        diagnostic.textContent = text;
        block.querySelector('.process-body')?.append(diagnostic);
      }
      summary(block);
      return true;
    }
    if (!m.runId) return false;
    if (['turn', 'thinking', 'tool', 'tool_pending', 'run_state'].includes(m.type ?? '')) {
      const block = ensure(chat, m.runId);
      const step = m.n ?? m.turn;
      if (m.type === 'turn' || m.type === 'thinking') thinking(block, m.reasoning || m.text, step);
      if (m.type === 'tool' || m.type === 'tool_pending') tool(block, m);
      else if (m.type === 'run_state' && m.state) {
        if (m.elapsedMs !== undefined) block._process.elapsedMs = m.elapsedMs;
        state(block, m.state as ProcessStatus);
      }
      summary(block);
      return true;
    }
    const block = ['shell_request', 'teach', 'user_question'].includes(m.type ?? '')
      ? ensure(chat, m.runId)
      : find(chat, m.runId);
    if (block && ['shell_request', 'teach'].includes(m.type ?? '')) state(block, 'approval');
    if (block && m.type === 'user_question') state(block, 'question');
    if (block && m.type === 'shell_approval_status' && m.approval) {
      const approval = m.approval;
      if (approval.decision === 'cancelled' || approval.continuation === 'cancelled') state(block, 'stopped');
      else if (['failed', 'unavailable'].includes(approval.continuation) || approval.execution === 'uncertain')
        state(block, 'blocked');
      else if (
        ['running', 'queued'].includes(approval.execution) ||
        ['queued', 'resuming'].includes(approval.continuation)
      ) {
        state(block, 'running');
        block._process.activity =
          approval.execution === 'running'
            ? 'Running command'
            : approval.execution === 'queued'
              ? 'Command queued'
              : 'Resuming task';
        summary(block);
      } else if (approval.continuation === 'not_required') state(block, 'done');
      else if (approval.continuation === 'started' && !['done', 'failed', 'stopped'].includes(block._process.state))
        state(block, 'running');
    }
    if (
      block &&
      m.type === 'asst' &&
      m.finalAnswer === true &&
      m.contentRole !== 'commentary' &&
      (!m.streamId || m.streamState === 'complete') &&
      ['running', 'sending', 'approval'].includes(block._process.state)
    ) {
      state(block, 'done');
    }
    if (
      block &&
      !m.streamId &&
      !block._process.explicitState &&
      ['asst', 'err', 'done', 'research_done'].includes(m.type ?? '')
    ) {
      state(block, m.type === 'err' ? 'failed' : 'done', false);
    }
    const previous = chat.lastElementChild;
    render(chat, m);
    const node = chat.lastElementChild !== previous ? chat.lastElementChild : null;
    if (node && m.runId) node.setAttribute('data-run-id', m.runId);
    if (block && !block.hidden && m.type === 'asst' && node) node.classList.add('continued-answer');
    return true;
  }
  function replace(chat: HTMLElement, m: ToolRecord): boolean {
    const block = m.runId ? find(chat, m.runId) : undefined;
    if (!block || !['tool', 'tool_pending'].includes(m.type ?? '')) return false;
    tool(block, m);
    return true;
  }

  function capture(chat: HTMLElement) {
    return Array.from(chat.querySelectorAll<HTMLElement>('.process-block')).map((block) => {
      const processBlock = block as ProcessBlock;
      const group = processBlock.querySelector<HTMLElement>('.process-group');
      return {
        runId: processBlock.dataset.runId,
        touched: processBlock._process.touched,
        open: group instanceof HTMLDetailsElement ? group.open : false,
        thinking: Array.from(processBlock.querySelectorAll<HTMLDetailsElement>('.thinking')).map((el) => el.open),
        tools: Array.from(processBlock.querySelectorAll('.tool-group')).map(
          (el) => !el.classList.contains('collapsed'),
        ),
        details: Object.fromEntries(
          Array.from(processBlock.querySelectorAll<HTMLDetailsElement>('.tool-disclosure[data-tool-key]')).map((el) => [
            el.dataset.toolKey!,
            el.open,
          ]),
        ),
      };
    });
  }
  function restore(chat: HTMLElement, saved: ProcessSnapshot[] = []): void {
    for (const item of saved) {
      if (!item.runId) continue;
      const block = find(chat, item.runId);
      if (!block) continue;
      const group = block.querySelector<HTMLDetailsElement>('.process-group');
      if (item.touched) {
        block._process.touched = true;
        if (group) group.open = item.open ?? false;
      }
      block.querySelectorAll<HTMLDetailsElement>('.thinking').forEach((el, i) => {
        if (item.thinking[i] !== undefined) el.open = item.thinking[i];
      });
      block.querySelectorAll<HTMLElement>('.tool-group').forEach((el, i) => {
        if (item.tools[i] === undefined) return;
        el.classList.toggle('collapsed', !item.tools[i]);
        el.querySelector('button')?.setAttribute('aria-expanded', String(item.tools[i]));
      });
      block.querySelectorAll<HTMLDetailsElement>('.tool-disclosure[data-tool-key]').forEach((el) => {
        const open = item.details?.[el.dataset.toolKey!];
        if (open !== undefined) el.open = open;
      });
    }
  }

  runtime.Process = { append, replace, capture, restore };
})();
