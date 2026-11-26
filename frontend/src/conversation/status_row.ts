import { Render } from '../render/render';

interface StatusAction {
  label: string;
  disabled?: boolean;
  onclick: () => void;
}
interface StatusRowOptions {
  kind?: string;
  err?: boolean;
  actions?: StatusAction[];
  action?: StatusAction;
  copyText?: string;
}

export function statusRow(prefix: string, text: string, opts: StatusRowOptions = {}) {
  const div = document.createElement('div');
  div.className = 'msg ' + (opts.kind || '');
  const row = document.createElement('div');
  row.className = 'status-row' + (opts.err ? ' err' : '');
  const pre = document.createElement('span');
  pre.className = 'sr-prefix';
  pre.textContent = prefix;
  const txt = document.createElement('span');
  txt.className = 'sr-text';
  let body = text || '';
  if (opts.err) body = body.replace(/^ERROR:\s*/i, '');
  txt.textContent = body;
  row.append(pre, txt);
  const actions = Array.isArray(opts.actions) ? opts.actions.slice() : [];
  if (opts.action) actions.push(opts.action);
  for (const action of actions) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'link-btn';
    btn.textContent = action.label;
    btn.disabled = !!action.disabled;
    btn.onclick = action.onclick;
    row.appendChild(btn);
  }
  div.appendChild(row);
  if (opts.copyText) Render.addMessageCopyButton(div, opts.copyText);
  return div;
}
