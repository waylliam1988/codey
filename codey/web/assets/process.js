/* Optional reasoning and trusted tool records, grouped by one user request. */
(function () {
'use strict';

function find(chat, id) {
  return Array.from(chat.querySelectorAll('.process-block')).find(node => node._runIds.has(id));
}
function ensure(chat, id) {
  let block = find(chat, id);
  if (block) return block;
  const recent = Array.from(chat.children).reverse();
  const userIndex = recent.findIndex(node => node.classList.contains('user'));
  block = recent.slice(0, userIndex < 0 ? recent.length : userIndex).find(node => node.classList.contains('process-block'));
  if (block) { block._runIds.add(id); return block; }
  block = document.createElement('div');
  block.className = 'msg process-block'; block.dataset.runId = id; block.hidden = true;
  block._runIds = new Set([id]);
  const label = document.createElement('div'); label.className = 'msg-label'; label.textContent = 'Codey';
  const group = document.createElement('details'); group.className = 'process-group'; group.open = true;
  const head = document.createElement('summary'); head.className = 'process-summary';
  const title = document.createElement('span'); title.className = 'process-title'; head.append(title);
  const body = document.createElement('div'); body.className = 'process-body';
  const steps = document.createElement('div'); steps.className = 'process-steps';
  const alerts = document.createElement('div'); alerts.className = 'process-alerts';
  group.append(head, body, steps); block.append(label, group, alerts); chat.append(block);
  block._process = {state:'running', explicitState:false, tools:new Map(), steps:new Set(), reasoning:new Set(), touched:false};
  head.addEventListener('click', () => { block._process.touched = true; });
  group.addEventListener('toggle', () => window.CodeyConversationUI?.updateLatest());
  return block;
}
function countLabel(verb, set, noun) {
  return set.size ? verb + ' ' + set.size + ' ' + noun + (set.size === 1 ? '' : 's') : '';
}
function summary(block) {
  const state = block._process;
  const tools = Array.from(state.tools.values());
  const reads = new Set(), edits = new Set(), commands = new Set();
  let explored = 0, searched = 0;
  for (const m of tools) {
    if (m.pending || m.error || m.status === 'error') continue;
    const name = m.toolName || m.kind;
    if (['read', 'read_file'].includes(name) && m.path && !/^https?:/i.test(m.path)) reads.add(m.path);
    else if (['edit', 'write', 'patch', 'edit_file', 'write_file', 'apply_patch', 'replace_in_file'].includes(name)
      && m.changed && m.path) edits.add(m.path);
    else if (['run', 'shell', 'run_command'].includes(name)) commands.add(m.toolKey || m);
    else if (['search', 'grep', 'web_search', 'references', 'find_references'].includes(name)) searched++;
    else if (['ls', 'list_dir'].includes(name)) explored++;
  }
  const counts = [countLabel('read', reads, 'file'), countLabel('edited', edits, 'file'),
    countLabel('ran', commands, 'command'), explored ? 'explored ' + explored + ' location' + (explored === 1 ? '' : 's') : '',
    searched ? 'searched ' + searched + ' time' + (searched === 1 ? '' : 's') : ''].filter(Boolean);
  const pending = tools.slice().reverse().find(m => m.pending);
  const titles = {running:'Working', done:'Worked', failed:'Failed', error:'Failed', stopped:'Stopped',
    max_turns:'Paused', no_progress:'Paused', approval:'Waiting for approval', blocked:'Paused'};
  let text = titles[state.state] || 'Paused';
  if (state.state === 'running') text += pending ? ' · ' + (pending.activity || pending.kind + ' ' + pending.path) : ' · Waiting for reply';
  else if (counts.length) text += ' · ' + counts.join(' · ');
  block.querySelector('.process-title').textContent = text;
  block.querySelector('.process-steps').textContent = state.steps.size
    ? 'Steps ' + Array.from(state.steps).join(' · ') : '';
  block.hidden = !tools.length && !state.reasoning.size;
}
function thinking(block, text, step) {
  if (typeof text !== 'string' || !text.trim()) return;
  const key = String(step || 0) + ':' + text;
  if (block._process.reasoning.has(key)) return;
  block._process.reasoning.add(key);
  const details = document.createElement('details'); details.className = 'thinking';
  const head = document.createElement('summary'); head.textContent = 'Thinking';
  if (step) head.title = 'Step ' + step;
  const body = document.createElement('div'); body.className = 'thinking-body';
  window.CodeyRender.renderMarkdown(body, text.trim());
  window.CodeyRender.addMessageCopyButton(body, text.trim());
  details.append(head, body); block.querySelector('.process-body').append(details);
}
function tool(block, m) {
  const key = m.toolKey || m;
  block._process.tools.set(key, m);
  const existing = m.toolKey && Array.from(block.querySelectorAll('[data-tool-key]')).find(el => el.dataset.toolKey === m.toolKey);
  const target = block.querySelector(m.error ? '.process-alerts' : '.process-body');
  if (existing && existing.parentElement === target && existing !== target.lastElementChild) {
    existing.replaceWith(window.CodeyRender.standaloneToolEl(m)); summary(block); return;
  }
  if (existing) existing.remove();
  if (m.pending || m.error) target.append(window.CodeyRender.standaloneToolEl(m));
  else window.CodeyRender.appendOrFoldTool(target, m);
  summary(block);
}
function state(block, value, explicit = true) {
  block._process.state = value;
  if (explicit) block._process.explicitState = true;
  const group = block.querySelector('.process-group');
  if (value !== 'running' && !block._process.touched) group.open = false;
  if (value === 'running' && !block._process.touched) group.open = true;
  summary(block);
}
function append(chat, m, render) {
  if (!m.runId) return false;
  if (['turn', 'thinking', 'tool', 'tool_pending', 'run_state'].includes(m.type)) {
    const block = ensure(chat, m.runId);
    if (m.n || m.turn) block._process.steps.add(m.n || m.turn);
    if (m.type === 'turn' || m.type === 'thinking') thinking(block, m.reasoning || m.text, m.n || m.turn);
    if (m.type === 'tool' || m.type === 'tool_pending') tool(block, m);
    else if (m.type === 'run_state') state(block, m.state);
    summary(block);
    return true;
  }
  const block = find(chat, m.runId);
  if (block && ['shell_request', 'teach'].includes(m.type)) state(block, 'approval');
  if (block && !block._process.explicitState && ['asst', 'err', 'done', 'research_done'].includes(m.type)) {
    state(block, m.type === 'err' ? 'failed' : 'done', false);
  }
  render(chat, m);
  const node = chat.lastElementChild;
  if (node && m.runId) node.dataset.runId = m.runId;
  if (block && !block.hidden && m.type === 'asst') node.classList.add('continued-answer');
  return true;
}
function replace(chat, m) {
  const block = m.runId && find(chat, m.runId);
  if (!block || !['tool', 'tool_pending'].includes(m.type)) return false;
  tool(block, m); return true;
}

function capture(chat) {
  return Array.from(chat.querySelectorAll('.process-block')).map(block => ({
    runId:block.dataset.runId, touched:block._process.touched,
    open:block.querySelector('.process-group').open,
    thinking:Array.from(block.querySelectorAll('.thinking')).map(el => el.open),
    tools:Array.from(block.querySelectorAll('.tool-group')).map(el => !el.classList.contains('collapsed')),
  }));
}
function restore(chat, saved = []) {
  for (const item of saved) {
    const block = find(chat, item.runId);
    if (!block) continue;
    if (item.touched) { block._process.touched = true; block.querySelector('.process-group').open = item.open; }
    block.querySelectorAll('.thinking').forEach((el, i) => { if (item.thinking[i] !== undefined) el.open = item.thinking[i]; });
    block.querySelectorAll('.tool-group').forEach((el, i) => {
      if (item.tools[i] === undefined) return;
      el.classList.toggle('collapsed', !item.tools[i]);
      el.querySelector('button').setAttribute('aria-expanded', String(item.tools[i]));
    });
  }
}

window.CodeyProcess = {append, replace, capture, restore};
})();
