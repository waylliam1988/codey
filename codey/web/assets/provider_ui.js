/* Codey provider picker UI: composer model chooser, availability dots,
   provider menu, and the local provider config popover. Zero-build asset
   module; index.html injects dependencies via init() at boot. */
(function () {
'use strict';

let deps = null;
let PROVIDERS = [];
let PROVIDER_LABELS = {};
let DEFAULT_PROVIDER = '';
let providerStatus = {};
let providerUpdatedAt = {};
let highlightedProvider = '';

function $(id) { return deps.$(id); }
function escapeHtml(text) { return deps.escapeHtml(text); }
function currentProviderId() { return deps.currentProviderId(); }
function setActiveProvider(id) { deps.setActiveProvider(id); }

function init(nextDeps) {
  deps = nextDeps;
  PROVIDERS = deps.PROVIDERS;
  PROVIDER_LABELS = deps.PROVIDER_LABELS;
  DEFAULT_PROVIDER = deps.DEFAULT_PROVIDER;
  providerStatus = Object.fromEntries(PROVIDERS.map(id => [id, false]));
  providerUpdatedAt = Object.fromEntries(PROVIDERS.map(id => [id, 0]));
  buildProviderMenu();
  bindHandlers();
  if (typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver(syncModelMenuWidth);
    observer.observe($('provider-button'));
    observer.observe($('effort-button'));
  }
  window.CodeySettings.init({$:deps.$, applyLocalMetadata});
  refreshLocalMetadata();
}

function buildProviderMenu() {
  const menu = deps.$('provider-menu');
  if (!menu) return;
  const warning = deps.$('provider-probe-warning');
  menu.setAttribute('role', 'dialog');
  menu.setAttribute('aria-label', 'Choose model');
  $('provider-button').setAttribute('aria-controls', 'provider-menu');
  $('provider-button').setAttribute('aria-haspopup', 'dialog');
  const focusedKey = document.activeElement?.dataset?.key;
  menu.querySelectorAll('.provider-item').forEach((btn) => btn.remove());
  for (const id of PROVIDERS) {
    const localModels = id === 'local' && localReady() ? [...new Set([localMetadata.model, ...(localMetadata.models || [])])] : [''];
    for (const model of localModels) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'provider-item';
    btn.dataset.provider = id; btn.dataset.model = model; btn.dataset.key = model ? 'local:' + model : id;
    const dot = document.createElement('span');
    dot.className = 'dot ' + providerAvailability(id);
    dot.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = model ? modelTitle(model) : providerLabel(id);
    btn.setAttribute('aria-label', model || providerLabel(id));
    const check = document.createElement('span');
    check.className = 'check';
    check.setAttribute('aria-hidden', 'true');
    check.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6.5 4.8 9 10 3.5"/></svg>';
    btn.append(dot, label, check);
    btn.onclick = () => {
      if ($('provider-button').disabled) return;
      if (id === 'local' && model && deps.activeSession) {
        const s = deps.activeSession(); if (s) {
          const previous = sessionSelection(s);
          const efforts = previous.base_url === localMetadata.base_url ? {...previous.efforts} : {};
          if (previous.model && previous.effort) efforts[previous.model] = previous.effort;
          s.localSelection = {base_url:localMetadata.base_url, model, effort:efforts[model] || null, efforts};
        }
      }
      setActiveProvider(id);
      closeMenu(true);
    };
    if (warning) menu.insertBefore(btn, warning);
    else menu.appendChild(btn);
  }
  }
  syncProviderUI(currentProviderId());
  highlightedProvider = selectedKey();
  highlightModel();
  if (focusedKey) modelRows().find(row => row.dataset.key === focusedKey)?.focus();
}

function modelRows() { return Array.from($('provider-menu').querySelectorAll('.provider-item')); }

function highlightModel() {
  $('provider-menu').querySelectorAll('.provider-item').forEach(row => row.classList.toggle('keyboard-active', row.dataset.key === highlightedProvider));
}

function closeModelMenu(restoreFocus = false) {
  $('provider-menu').classList.remove('open');
  $('provider-menu').setAttribute('aria-hidden', 'true');
  $('provider-button').classList.remove('open');
  $('provider-button').setAttribute('aria-expanded', 'false');
  if (restoreFocus) $('provider-button').focus();
}

function closeMenu(restoreFocus = false) {
  closeEffortMenu();
  closeModelMenu(restoreFocus);
}

function syncModelMenuWidth() {
  const menu = $('provider-menu');
  if (!menu.classList.contains('open')) return;
  const arrow = $('effort-chooser').hidden ? null : $('effort-button').querySelector('.chev');
  const bounds = arrow?.getBoundingClientRect?.();
  if (!bounds) { menu.style.removeProperty('--model-menu-width'); return; }
  const left = $('provider-button').getBoundingClientRect().left;
  menu.style.setProperty('--model-menu-width', Math.max(0, bounds.right - left) + 'px');
}

function openMenu() {
  closeEffortMenu();
  const menu = $('provider-menu');
  menu.classList.add('open');
  menu.setAttribute('aria-hidden', 'false');
  $('provider-button').classList.add('open');
  $('provider-button').setAttribute('aria-expanded', 'true');
  syncModelMenuWidth();
  highlightedProvider = selectedKey();
  highlightModel();
  (modelRows().find(row => row.dataset.key === highlightedProvider) || modelRows()[0])?.focus();
  refreshProviderStatus();
  refreshLocalMetadata();
}

function applyRecommended(data) {
  if (data && data.recommended && window.CodeyUiState && typeof window.CodeyUiState.setRecommended === 'function') {
    window.CodeyUiState.setRecommended(data.recommended);
  }
}

function extractCatalog(data) {
  if (!data || !Array.isArray(data.providers)) return null;
  const ids = data.providers.map((item) => item && item.id).filter(Boolean);
  if (!ids.length) return null;
  const labels = {};
  for (const item of data.providers) {
    if (item && item.id) labels[item.id] = item.label || item.id;
  }
  return { ids, labels };
}

function applyProviderConfig(data) {
  const catalog = extractCatalog(data);
  if (!catalog) return false;
  const { ids, labels } = catalog;
  let changed = false;
  if (PROVIDERS.length !== ids.length) {
    changed = true;
  } else {
    for (let i = 0; i < ids.length; i++) {
      if (PROVIDERS[i] !== ids[i]) { changed = true; break; }
    }
    if (!changed) {
      for (const id of ids) {
        if (PROVIDER_LABELS[id] !== labels[id]) { changed = true; break; }
      }
    }
  }
  if (!changed && data.default && data.default !== DEFAULT_PROVIDER && labels[data.default]) changed = true;
  applyRecommended(data);
  if (!changed) return false;
  window.CodeyUiState.setProviders(ids, labels, data.default);
  DEFAULT_PROVIDER = window.CodeyUiState.DEFAULT_PROVIDER;
  applyRecommended(data);
  providerStatus = Object.fromEntries(PROVIDERS.map(id => [id, !!providerStatus[id]]));
  providerUpdatedAt = Object.fromEntries(PROVIDERS.map(id => [id, providerUpdatedAt[id] || 0]));
  buildProviderMenu();
  return true;
}

async function adoptBackendCatalog() {
  // Boot-time only: adopt the backend catalog into ui_state before init.
  // Hits the cheap static catalog (no CDP/network probe); availability
  // stays on the async /api/providers refresh path. Do not touch DOM
  // here (deps is unset); menu is built later by init().
  // index.html always loads ui_state.js and dereferences CodeyUiState at
  // parse time, so the state module is guaranteed present here.
  try {
    const r = await fetch('/api/provider_catalog', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    const catalog = extractCatalog(data);
    if (!catalog) return;
    window.CodeyUiState.setProviders(catalog.ids, catalog.labels, data.default);
  } catch {}
}

function providerLabel(id) {
  return PROVIDER_LABELS[id] || PROVIDER_LABELS[DEFAULT_PROVIDER];
}
function providerAvailability(id) { return providerStatus[id] ? 'ok' : ''; }
function syncProviderUI(providerId) {
  const id = PROVIDERS.includes(providerId) ? providerId : DEFAULT_PROVIDER;
  const selection = sessionSelection();
  $('provider-name').textContent = id === 'local' && selection.model ? modelTitle(selection.model) : providerLabel(id);
  const staleConnection = id === 'local' && selection.base_url && localMetadata.base_url && selection.base_url !== localMetadata.base_url;
  $('provider-button').setAttribute('aria-label', 'Choose model: ' + $('provider-name').textContent +
    (staleConnection ? '. Connection changed; select the model again.' : ''));
  $('provider-dot').className = 'dot ' + (staleConnection ? '' : providerAvailability(id));
  document.querySelectorAll('.provider-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.provider === id && (!btn.dataset.model || btn.dataset.model === selection.model));
    btn.setAttribute('aria-pressed', String(btn.dataset.provider === id && (!btn.dataset.model || btn.dataset.model === selection.model)));
    const dot = btn.querySelector('.dot');
    if (dot) dot.className = 'dot ' + providerAvailability(btn.dataset.provider);
  });
  syncThinking();
  syncModelMenuWidth();
}
let refreshTimer = null;
let lastRequestId = 0;

function applyProviderStatus(providers, isSSE = true, snapshotTime = Date.now()) {
  if (!Array.isArray(providers)) return;
  const appliedAt = Date.now();
  for (const item of providers) {
    if (!item || !PROVIDERS.includes(item.id)) continue;
    if (!isSSE && providerUpdatedAt[item.id] > snapshotTime) continue;
    providerStatus[item.id] = !!item.available;
    providerUpdatedAt[item.id] = isSSE ? appliedAt : snapshotTime;
  }
  syncProviderUI(currentProviderId());
}
function refreshProviderStatus(immediate = false) {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
  if (!immediate) {
    refreshTimer = setTimeout(() => _doRefreshProviderStatus(), 500);
    return;
  }
  _doRefreshProviderStatus();
}
function setProbeWarning(failed) {
  $('provider-probe-warning').hidden = !failed;
}
async function _doRefreshProviderStatus() {
  const reqId = ++lastRequestId;
  const fetchTime = Date.now();
  try {
    const r = await fetch('/api/providers');
    if (!r.ok) {
      if (reqId === lastRequestId) setProbeWarning(true);
      return;
    }
    const data = await r.json();
    if (reqId !== lastRequestId) return;
    applyProviderConfig(data);
    applyProviderStatus(data.providers, false, fetchTime);
    setProbeWarning(!!data.probe_error);
  } catch {
    if (reqId === lastRequestId) setProbeWarning(true);
  }
}

function openLocalProviderConfig() { return window.CodeySettings.open(); }
function closeLocalProviderConfig() { return window.CodeySettings.close(); }

let localMetadata = {}, localRequest = 0;
function localReady() { return !!(localMetadata.connected && localMetadata.model); }
function sessionSelection(s = deps.activeSession?.()) {
const old = s?.localSelection || {base_url:localMetadata.base_url || '', model:localMetadata.model || ''};
  return {...old, effort:old.effort || (old.thinking === false ? 'off' : old.thinking === true ? 'high' : null), efforts:old.efforts || {}};
}
function selectedKey() {
  const id = currentProviderId();
  return id === 'local' ? 'local:' + sessionSelection().model : id;
}
function modelTitle(model) {
  if (model === localMetadata.model && localMetadata.display_name) return localMetadata.display_name;
  // A restored chat can retain its model while offline discovery returns none.
  const leaf = model.split('/').pop();
  const short = leaf.match(/^([A-Za-z][\w.]*?)[-_](\d+(?:\.\d+)?[Bb])(?=[-_]|$)/);
  return short ? `${short[1]} ${short[2].toUpperCase()}` : leaf;
}
function thinkingOptions() {
  const selection = sessionSelection();
  return currentProviderId() === 'local' && selection.base_url === localMetadata.base_url && selection.model === localMetadata.model
    ? (localMetadata.thinking_options || []) : [];
}
const effortLabels = {off:'Off', minimal:'Mini', low:'Low', medium:'Med', high:'High', max:'Max'};
function selectedEffort() {
  const options = thinkingOptions(), selected = sessionSelection().effort;
  return options.includes(selected) ? selected : options.includes('high') ? 'high' : options.find(value => value !== 'off') || 'off';
}
function closeEffortMenu(restoreFocus = false) {
  const menu = $('effort-menu'); if (!menu) return;
  menu.classList.remove('open'); menu.setAttribute('aria-hidden', 'true');
  $('effort-button').classList.remove('open'); $('effort-button').setAttribute('aria-expanded', 'false');
  if (restoreFocus && !$('effort-chooser').hidden) $('effort-button').focus();
}
function syncThinking() {
  const button = $('effort-button'); if (!button) return;
  const options = thinkingOptions(), value = selectedEffort();
  $('effort-chooser').hidden = !options.length;
  button.disabled = $('provider-button').disabled;
  const label = effortLabels[value] || value;
  $('effort-name').textContent = label;
  button.setAttribute('aria-label', 'Thinking effort: ' + label);
  const menu = $('effort-menu');
  const signature = JSON.stringify([sessionSelection().base_url, sessionSelection().model, options, value]);
  if (menu.dataset.signature === signature) return;
  const restoreFocus = menu.contains(document.activeElement);
  closeEffortMenu(restoreFocus); menu.dataset.signature = signature; menu.replaceChildren();
  options.forEach(option => {
    const row = document.createElement('button'); row.type = 'button'; row.className = 'provider-action';
    row.dataset.effort = option; row.textContent = effortLabels[option] || option;
    row.setAttribute('aria-label', 'Thinking effort: ' + row.textContent);
    row.setAttribute('aria-pressed', String(value === option));
    row.onclick = () => {
      const s = deps.activeSession(); if (!s || $('provider-button').disabled) return;
      const selection = sessionSelection(s);
      s.localSelection = {base_url:selection.base_url, model:selection.model, effort:option,
        efforts:{...selection.efforts, [selection.model]:option}};
      deps.persistActiveNow(); syncProviderUI('local'); closeEffortMenu(true);
    };
    menu.appendChild(row);
  });
}
function applyLocalMetadata(local) {
  localRequest++;
  const next = local && typeof local === 'object' ? local : {};
  if (JSON.stringify(next) === JSON.stringify(localMetadata)) return;
  localMetadata = next;
  if (typeof localMetadata.connected === 'boolean') {
    providerStatus.local = localMetadata.connected;
    providerUpdatedAt.local = Date.now();
  }
  buildProviderMenu();
}
async function refreshLocalMetadata() {
  const request = ++localRequest;
  try {
    const r = await fetch('/api/local_provider', {cache:'no-store'});
    if (!r.ok) return;
    const data = await r.json();
    if (request === localRequest && data.local) applyLocalMetadata(data.local);
  } catch {}
}
function runSelection(s, provider) {
  if (provider !== 'local') return {};
  const selection = sessionSelection(s);
const options = thinkingOptions();
  const effort = options.length ? selectedEffort() : selection.effort;
  return selection.base_url && selection.model ? {local_selection:{base_url:selection.base_url, model:selection.model, effort}} : {};
}

function bindHandlers() {
$('provider-button').onclick = (e) => {
  if ($('provider-button').disabled) return;
  e.stopPropagation();
  if ($('provider-menu').classList.contains('open')) closeMenu(true);
  else openMenu();
};
$('provider-button').setAttribute('aria-expanded', 'false');
$('provider-menu').addEventListener('keydown', e => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(true); return; }
  const rows = modelRows();
  if (!rows.length) return;
  const focused = rows.indexOf(document.activeElement);
  let index = focused >= 0 ? focused : rows.findIndex(row => row.dataset.key === highlightedProvider);
  if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
    e.preventDefault();
    index = e.key === 'Home' ? 0 : e.key === 'End' ? rows.length - 1 : (index + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
    highlightedProvider = rows[index].dataset.key;
    highlightModel();
    rows[index].scrollIntoView({ block: 'nearest' });
    rows[index].focus();
  }
});
$('provider-menu').addEventListener('focusout', () => {
  setTimeout(() => { if (!$('provider-menu').contains(document.activeElement) && document.activeElement !== $('provider-button')) closeModelMenu(); }, 0);
});
document.addEventListener('click', e => {
  if (!$('provider-menu').contains(e.target) && !$('provider-button').contains(e.target)) {
    closeModelMenu();
  }
  if (!$('effort-menu').contains(e.target) && !$('effort-button').contains(e.target)) closeEffortMenu();
});
$('effort-button').onclick = e => {
  e.stopPropagation();
  if ($('provider-button').disabled || !thinkingOptions().length) return;
  if ($('effort-menu').classList.contains('open')) { closeEffortMenu(true); return; }
  closeMenu();
  const menu = $('effort-menu'); menu.classList.add('open'); menu.setAttribute('aria-hidden','false');
  $('effort-button').classList.add('open'); $('effort-button').setAttribute('aria-expanded','true');
  (menu.querySelector('[aria-pressed="true"]') || menu.firstElementChild)?.focus();
  refreshLocalMetadata();
};
$('effort-menu').addEventListener('keydown', e => {
  if (e.isComposing) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeEffortMenu(true); return; }
  const rows = Array.from($('effort-menu').children), index = rows.indexOf(document.activeElement);
  if (['ArrowDown','ArrowUp','Home','End'].includes(e.key) && rows.length) {
    e.preventDefault();
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? rows.length-1 : (index + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
    rows[next].scrollIntoView({block:'nearest'}); rows[next].focus();
  }
});
$('effort-menu').addEventListener('focusout', () => setTimeout(() => {
  if (!$('effort-menu').contains(document.activeElement) && document.activeElement !== $('effort-button')) closeEffortMenu();
}, 0));

}

window.CodeyProviderUI = {
  init,
  label: providerLabel,
  sync: syncProviderUI,
  applyConfig: applyProviderConfig,
  adoptCatalog: adoptBackendCatalog,
  applyStatus: applyProviderStatus,
  refreshStatus: refreshProviderStatus,
  localReady, runSelection, applyLocalMetadata,
  openLocalConfig: openLocalProviderConfig,
  closeLocalConfig: closeLocalProviderConfig,
  closeMenu,
};
})();
