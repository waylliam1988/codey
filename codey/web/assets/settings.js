/* One quiet connection settings dialog. No build step. */
(function () {
'use strict';
let deps, returnFocus, generation = 0, connected = false, loading = false;
const $ = id => deps.$(id);
const selects = [];

function closeSelects() { selects.forEach(select => select.close()); }
function makeSelect(id) {
  const input = $(id), wrap = document.createElement('div'); wrap.className = 'settings-select-wrap';
  input.before(wrap); wrap.appendChild(input); input.hidden = true; input.tabIndex = -1;
  const button = document.createElement('button'); button.type = 'button'; button.id = id + '-button';
  button.className = 'settings-select'; button.setAttribute('aria-haspopup', 'listbox');
  const label = input.parentElement.parentElement;
  const labelText = label.firstChild.textContent.trim(); label.htmlFor = button.id;
  button.setAttribute('aria-controls', id + '-menu'); button.setAttribute('aria-expanded', 'false');
  const menu = document.createElement('div'); menu.id = id + '-menu'; menu.className = 'settings-select-menu';
  menu.setAttribute('role', 'listbox'); menu.setAttribute('aria-label', labelText); menu.hidden = true;
  wrap.append(button, menu);
  function sync() {
    button.textContent = input.selectedOptions[0]?.textContent || '';
    button.setAttribute('aria-label', labelText + ': ' + button.textContent);
  }
  function close() { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); }
  function open() {
    if (button.disabled) return;
    closeSelects(); menu.replaceChildren();
    Array.from(input.options).forEach(option => {
      const row = document.createElement('button'); row.type = 'button'; row.className = 'settings-select-option';
      row.textContent = option.textContent; row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(option.value === input.value));
      row.onclick = () => {
        input.value = option.value; sync(); close(); button.focus();
        input.dispatchEvent(new Event('change', {bubbles:true}));
      }; menu.appendChild(row);
    });
    menu.hidden = false; button.setAttribute('aria-expanded', 'true');
    const rect = button.getBoundingClientRect(), dialog = $('local-config-pop').getBoundingClientRect();
    menu.classList.toggle('above', rect.bottom + menu.offsetHeight + 6 > Math.min(dialog.bottom, innerHeight - 16));
    (menu.querySelector('[aria-selected="true"]') || menu.firstElementChild)?.focus();
  }
  button.onclick = () => menu.hidden ? open() : close();
  wrap.addEventListener('keydown', e => {
    if (e.isComposing) return;
    if (['ArrowDown','ArrowUp','Home','End'].includes(e.key)) {
      e.preventDefault();
      if (menu.hidden) { open(); return; }
      const rows = Array.from(menu.children), current = rows.indexOf(document.activeElement);
      const next = e.key === 'Home' ? 0 : e.key === 'End' ? rows.length - 1 : (current + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
      rows[next]?.focus();
    } else if (e.key === 'Tab') close();
  });
  wrap.addEventListener('focusout', () => setTimeout(() => { if (!wrap.contains(document.activeElement)) close(); }, 0));
  document.addEventListener('click', e => { if (!wrap.contains(e.target)) close(); });
  selects.push({sync, close, menu, button}); sync();
}

function init(next) {
  deps = next;
  window.CodeyModelSettings.init({close});
  $('local-connection-editor').ontoggle = () => { if ($('local-connection-editor').open) loadConnection(); };
  $('btn-settings').onclick = () => open();
  $('settings-dismiss').onclick = close;
  $('local-config-close').onclick = close;
  $('local-config-retry').onclick = loadConnection;
  $('local-config-form').onsubmit = e => { e.preventDefault(); save(); };
  $('local-config-pop').addEventListener('cancel', e => { e.preventDefault(); close(); });
  $('local-config-pop').addEventListener('click', e => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (e.target === e.currentTarget && (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom)) close();
  });
  $('local-context-preset').onchange = () => {
    const custom = $('local-context-preset').value === 'custom';
    $('local-custom-context').hidden = !custom;
    if (custom) $('local-context-window').focus();
    else $('local-context-window').value = $('local-context-preset').value;
  };
  makeSelect('local-context-preset'); makeSelect('local-native-tools-mode'); makeSelect('local-api-protocol');
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === ',') {
      e.preventDefault(); e.stopImmediatePropagation();
      $('local-config-pop').open ? close() : open();
    } else if ($('local-config-pop').open && e.key === 'Escape') {
      e.preventDefault(); e.stopImmediatePropagation();
      const openSelect = selects.find(select => !select.menu.hidden);
      if (openSelect) { openSelect.close(); openSelect.button.focus(); } else close();
    } else if ($('local-config-pop').open && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
      e.preventDefault(); e.stopImmediatePropagation();
    }
  }, true);
  document.querySelector('#top-menu [data-act="settings"]').addEventListener('click', () => {
    $('top-menu').classList.remove('open'); open($('topbar-more'));
  });
}

async function open(trigger = document.activeElement) {
  const dialog = $('local-config-pop');
  if (dialog.open) return;
  returnFocus = trigger;
  $('local-advanced').open = false;
  $('local-custom-context').hidden = true;
  $('local-config-error').textContent = '';
  $('local-api-key').value = '';
  $('local-config-retry').hidden = true;
  dialog.showModal(); $('settings-dismiss').focus();
  $('local-connection-editor').open = false;
  await window.CodeyModelSettings.open();
}

async function loadConnection() {
  const dialog = $('local-config-pop');
  if (!dialog.open || loading) return;
  const restoreRetryFocus = document.activeElement === $('local-config-retry');
  loading = true;
  const request = ++generation;
  dialog.setAttribute('aria-busy', 'true');
  $('local-config-summary').textContent = 'Loading connection…';
  $('local-config-summary').classList.remove('load-error');
  Array.from($('local-config-form').elements).forEach(el => { el.disabled = true; });
  $('local-config-close').disabled = false;
  try {
    const r = await fetch('/api/local_provider', {cache:'no-store'});
    if (!r.ok) throw new Error('connection unavailable');
    const data = await r.json();
    if (request !== generation || !dialog.open) return;
    const local = data.local || {};
    deps.applyLocalMetadata(local);
    connected = !!local.connected;
    $('local-config-summary').textContent = connected ? 'Connected. Connection defaults apply to local chats.' : 'Open Ollama, KoboldCpp, or LM Studio, then connect.';
    $('local-base-url').value = local.base_url || '';
    $('local-model-name').value = local.model || '';
    $('local-display-name').value = local.saved_display_name || '';
    $('local-key-status').textContent = local.has_api_key ? 'Saved' : 'Optional';
    $('local-api-key').placeholder = local.has_api_key ? 'Leave blank to keep saved key' : 'Optional';
    $('local-model-options').replaceChildren(...(local.models || []).map(model => {
      const option = document.createElement('option'); option.value = model; return option;
    }));
    $('local-config-candidates').replaceChildren(...(local.candidates || []).map(url => {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = url;
      button.onclick = () => { $('local-base-url').value = url; }; return button;
    }));
    const tokens = String(local.context?.context_window_tokens || 32768);
    $('local-context-window').value = tokens;
    $('local-context-preset').value = ['32768','131072','262144'].includes(tokens) ? tokens : 'custom';
    $('local-custom-context').hidden = $('local-context-preset').value !== 'custom';
    $('local-native-tools-mode').value = local.native_tools_mode || 'auto';
    $('local-api-protocol').value = local.api_protocol || 'openai-completions';
    selects.forEach(select => select.sync());
    if (local.context_error) $('local-config-error').textContent = local.context_error;
    else if (local.error) $('local-config-error').textContent = local.error;
    Array.from($('local-config-form').elements).forEach(el => { el.disabled = false; });
    $('local-config-retry').hidden = true;
    $('local-config-save').textContent = connected ? 'Save connection' : 'Connect';
  } catch {
    if (request !== generation || !dialog.open) return;
    $('local-config-summary').textContent = 'Could not load connection';
    $('local-config-summary').classList.add('load-error');
    $('local-config-retry').hidden = false;
  } finally {
    if (request === generation) {
      loading = false;
      dialog.setAttribute('aria-busy', 'false');
      $('local-config-retry').disabled = false;
      if (restoreRetryFocus && [document.body, dialog].includes(document.activeElement)) {
        ($('local-config-retry').hidden ? $('local-base-url') : $('local-config-retry')).focus();
      }
    }
  }
}

function close() {
  const dialog = $('local-config-pop');
  if (!dialog.open) return;
  generation++; loading = false; window.CodeyModelSettings.close(); dialog.close();
  closeSelects();
  if (returnFocus?.isConnected && !returnFocus.closest('[inert]')) returnFocus.focus();
}

async function save() {
  if ($('local-config-save').disabled) return;
  const base_url = $('local-base-url').value.trim(), model = $('local-model-name').value.trim();
  if (!base_url) return;
  const payload = {base_url, model, display_name:$('local-display-name').value.trim(),
    api_protocol:$('local-api-protocol').value,
    native_tools_mode:$('local-native-tools-mode').value, context_window_tokens:$('local-context-window').value.trim()};
  const key = $('local-api-key').value.trim(); if (key) payload.api_key = key;
  $('local-config-error').textContent = '';
  Array.from($('local-config-form').elements).forEach(el => { el.disabled = true; });
  $('local-config-save').textContent = 'Connecting…';
  const request = generation;
  try {
    const r = await fetch('/api/local_provider', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const data = await r.json().catch(() => ({}));
    if (request !== generation) return;
    if (!r.ok || !data.ok) { $('local-config-error').textContent = data.error || 'Could not connect.'; return; }
    deps.applyLocalMetadata(data.local || {});
    $('local-api-key').value = ''; connected = !!data.local?.connected;
    $('local-config-summary').textContent = 'Connection saved. Refresh models to update the list.';
  } catch {
    if (request === generation) $('local-config-error').textContent = 'Could not reach the server.';
  } finally {
    if (request === generation) {
      Array.from($('local-config-form').elements).forEach(el => { el.disabled = false; });
      $('local-config-save').textContent = connected ? 'Save connection' : 'Connect';
    }
  }
}

window.CodeySettings = {init, open, close, save};
})();
