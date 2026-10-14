/* Optional reasoning and trusted tool records, grouped by one user request. */
export interface ProcessSnapshot {
  runId?: string;
  touched?: boolean;
  open?: boolean;
  thinking: boolean[];
  tools: boolean[];
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
  toolKey?: string;
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
    | 'running'
    | 'done'
    | 'failed'
    | 'error'
    | 'stopped'
    | 'max_turns'
    | 'no_progress'
    | 'approval'
    | 'blocked';
  interface ToolRecord {
    toolKey?: string;
    toolName?: string;
    kind?: string;
    path?: string;
    activity?: string;
    pending?: boolean;
    error?: boolean;
    status?: string;
    changed?: boolean;
    runId?: string;
    type: string;
    n?: string | number;
    turn?: string | number;
    reasoning?: string;
    text?: string;
    state?: string;
  }
  interface ProcessState {
    state: ProcessStatus;
    explicitState: boolean;
    tools: Map<string, ToolRecord>;
    steps: Set<string | number>;
    reasoning: Set<string>;
    touched: boolean;
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
    const recent = Array.from(chat.children)
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
    group.open = true;
    const head = document.createElement('summary');
    head.className = 'process-summary';
    const title = document.createElement('span');
    title.className = 'process-title';
    head.append(title);
    const body = document.createElement('div');
    body.className = 'process-body';
    const steps = document.createElement('div');
    steps.className = 'process-steps';
    const alerts = document.createElement('div');
    alerts.className = 'process-alerts';
    group.append(head, body, steps);
    block.append(label, group, alerts);
    chat.append(block);
    block._process = {
      state: 'running',
      explicitState: false,
      tools: new Map(),
      steps: new Set(),
      reasoning: new Set(),
      touched: false,
    };
    head.addEventListener('click', () => {
      block._process.touched = true;
    });
    group.addEventListener('toggle', () => window.ConversationUI?.updateLatest());
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
      const name = m.toolName || m.kind || '';
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
      running: 'Working',
      done: 'Worked',
      failed: 'Failed',
      error: 'Failed',
      stopped: 'Stopped',
      max_turns: 'Paused',
      no_progress: 'Paused',
      approval: 'Waiting for approval',
      blocked: 'Paused',
    };
    let text = titles[state.state] || 'Paused';
    if (state.state === 'running')
      text += pending ? ' · ' + (pending.activity || pending.kind + ' ' + pending.path) : ' · Waiting for reply';
    else if (counts.length) text += ' · ' + counts.join(' · ');
    const title = block.querySelector<HTMLElement>('.process-title');
    const steps = block.querySelector<HTMLElement>('.process-steps');
    if (title) title.textContent = text;
    if (steps) steps.textContent = state.steps.size ? 'Steps ' + Array.from(state.steps).join(' · ') : '';
    block.hidden = !tools.length && !state.reasoning.size;
  }
  function thinking(block: ProcessBlock, text: unknown, step: string | number | undefined): void {
    if (typeof text !== 'string' || !text.trim()) return;
    const key = String(step || 0) + ':' + text;
    if (block._process.reasoning.has(key)) return;
    block._process.reasoning.add(key);
    const details = document.createElement('details');
    details.className = 'thinking';
    const head = document.createElement('summary');
    head.textContent = 'Thinking';
    if (step) head.title = 'Step ' + step;
    const body = document.createElement('div');
    body.className = 'thinking-body';
    window.Render.renderMarkdown(body, text.trim());
    window.Render.addMessageCopyButton(body, text.trim());
    details.append(head, body);
    const processBody = block.querySelector<HTMLElement>('.process-body');
    if (processBody) processBody.append(details);
  }
  function tool(block: ProcessBlock, m: ToolRecord): void {
    const key = m.toolKey || JSON.stringify(m);
    block._process.tools.set(key, m);
    const existing =
      m.toolKey &&
      Array.from(block.querySelectorAll<HTMLElement>('[data-tool-key]')).find((el) => el.dataset.toolKey === m.toolKey);
    const target = block.querySelector<HTMLElement>(m.error ? '.process-alerts' : '.process-body');
    if (!target) return;
    if (existing && existing.parentElement === target && existing !== target.lastElementChild) {
      existing.replaceWith(window.Render.standaloneToolEl(m));
      summary(block);
      return;
    }
    if (existing) existing.remove();
    if (m.pending || m.error) target.append(window.Render.standaloneToolEl(m));
    else window.Render.appendOrFoldTool(target, m);
    summary(block);
  }
  function state(block: ProcessBlock, value: ProcessStatus, explicit = true): void {
    block._process.state = value;
    if (explicit) block._process.explicitState = true;
    const group = block.querySelector<HTMLDetailsElement>('.process-group');
    if (group && value !== 'running' && !block._process.touched) group.open = false;
    if (group && value === 'running' && !block._process.touched) group.open = true;
    summary(block);
  }
  function append(chat: HTMLElement, m: ToolRecord, render: (chat: HTMLElement, message: ToolRecord) => void): boolean {
    if (!m.runId) return false;
    if (['turn', 'thinking', 'tool', 'tool_pending', 'run_state'].includes(m.type ?? '')) {
      const block = ensure(chat, m.runId);
      const step = m.n ?? m.turn;
      if (step !== undefined) block._process.steps.add(step);
      if (m.type === 'turn' || m.type === 'thinking') thinking(block, m.reasoning || m.text, step);
      if (m.type === 'tool' || m.type === 'tool_pending') tool(block, m);
      else if (m.type === 'run_state' && m.state) state(block, m.state as ProcessStatus);
      summary(block);
      return true;
    }
    const block = find(chat, m.runId);
    if (block && ['shell_request', 'teach'].includes(m.type ?? '')) state(block, 'approval');
    if (block && !block._process.explicitState && ['asst', 'err', 'done', 'research_done'].includes(m.type ?? '')) {
      state(block, m.type === 'err' ? 'failed' : 'done', false);
    }
    render(chat, m);
    const node = chat.lastElementChild;
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
    }
  }

  window.Process = { append, replace, capture, restore };
})();
