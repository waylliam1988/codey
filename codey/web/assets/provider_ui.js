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
}

function buildProviderMenu() {
  const menu = deps.$('provider-menu');
  if (!menu) return;
  const warning = deps.$('provider-probe-warning');
  menu.querySelectorAll('.provider-item').forEach((btn) => btn.remove());
  for (const id of PROVIDERS) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'provider-item';
    btn.dataset.provider = id;
    const dot = document.createElement('span');
    dot.className = 'dot ' + providerAvailability(id);
    dot.setAttribute('aria-hidden', 'true');
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = providerLabel(id);
    const check = document.createElement('span');
    check.className = 'check';
    check.setAttribute('aria-hidden', 'true');
    check.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6.5 4.8 9 10 3.5"/></svg>';
    btn.append(dot, label, check);
    btn.onclick = () => {
      setActiveProvider(id);
      menu.classList.remove('open');
      deps.$('provider-button').classList.remove('open');
    };
    if (warning) menu.insertBefore(btn, warning);
    else menu.appendChild(btn);
  }
  syncProviderUI(currentProviderId());
}

function applyRecommended(data) {
  if (data && data.recommended && window.CodeyUiState && typeof window.CodeyUiState.setRecommended === 'function') {
    window.CodeyUiState.setRecommended(data.recommended);
  }
}

function applyProviderConfig(data) {
  if (!data || !Array.isArray(data.providers)) return false;
  const ids = data.providers.map((item) => item && item.id).filter(Boolean);
  if (!ids.length) return false;
  const labels = {};
  for (const item of data.providers) {
    if (item && item.id) labels[item.id] = item.label || item.id;
  }
  let changed = false;
  for (const id of ids) {
    if (!PROVIDERS.includes(id)) { changed = true; break; }
    if (PROVIDER_LABELS[id] !== labels[id]) { changed = true; break; }
  }
  if (data.default && data.default !== DEFAULT_PROVIDER && labels[data.default]) changed = true;
  applyRecommended(data);
  if (!changed) return false;
  if (window.CodeyUiState && typeof window.CodeyUiState.setProviders === 'function') {
    window.CodeyUiState.setProviders(ids, labels, data.default);
    applyRecommended(data);
  } else {
    PROVIDER_LABELS = labels;
    PROVIDERS = ids;
    if (data.default && labels[data.default]) DEFAULT_PROVIDER = data.default;
  }
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
  try {
    const r = await fetch('/api/provider_catalog', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    if (!data || !Array.isArray(data.providers) || !data.providers.length) return;
    const ids = data.providers.map((item) => item && item.id).filter(Boolean);
    const labels = {};
    for (const item of data.providers) {
      if (item && item.id) labels[item.id] = item.label || item.id;
    }
    if (ids.length && window.CodeyUiState && typeof window.CodeyUiState.setProviders === 'function') {
      window.CodeyUiState.setProviders(ids, labels, data.default);
    }
  } catch {}
}

function providerLabel(id) {
  return PROVIDER_LABELS[id] || PROVIDER_LABELS[DEFAULT_PROVIDER];
}
function providerAvailability(id) { return providerStatus[id] ? 'ok' : ''; }
function syncProviderUI(providerId) {
  const id = PROVIDERS.includes(providerId) ? providerId : DEFAULT_PROVIDER;
  $('provider-name').textContent = providerLabel(id);
  $('provider-dot').className = 'dot ' + providerAvailability(id);
  document.querySelectorAll('.provider-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.provider === id);
    const dot = btn.querySelector('.dot');
    if (dot) dot.className = 'dot ' + providerAvailability(btn.dataset.provider);
  });
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

async function openLocalProviderConfig() {
  const pop = $('local-config-pop');
  pop.classList.add('open');
  pop.setAttribute('aria-hidden', 'false');
  $('local-config-error').textContent = '';
  $('local-api-key').value = '';
  try {
    const r = await fetch('/api/local_provider');
    if (!r.ok) return;
    const data = await r.json();
    const local = data.local || {};
    $('local-config-summary').textContent = local.connected && local.base_url
      ? `Connected to ${local.base_url}`
      : 'Open Ollama, KoboldCPP, or LM Studio, then connect.';
    if (local.base_url) $('local-base-url').value = local.base_url;
    if (local.model) $('local-model-name').value = local.model;
    const models = Array.isArray(local.models) ? local.models : [];
    $('local-model-options').innerHTML = models
      .map(m => `<option value="${escapeHtml(m)}"></option>`).join('');
    const cands = Array.isArray(local.candidates) ? local.candidates : [];
    $('local-config-candidates').innerHTML = cands.length
      ? cands.map(url => `<button type="button" data-url="${escapeHtml(url)}">${escapeHtml(url)}</button>`).join('')
      : '';
    document.querySelectorAll('#local-config-candidates button').forEach((btn) => {
      btn.onclick = () => { $('local-base-url').value = btn.dataset.url || ''; };
    });
    const windowTokens = local.context && local.context.context_window_tokens;
    if (windowTokens) $('local-context-window').value = String(windowTokens);
    $('local-native-tools-mode').value = local.native_tools_mode || 'auto';
    if (local.context_error) $('local-config-error').textContent = String(local.context_error);
    else if (local.error) $('local-config-error').textContent = String(local.error);
    document.querySelectorAll('#local-context-presets button').forEach((btn) => {
      btn.onclick = () => { $('local-context-window').value = btn.dataset.contextWindow || ''; };
    });
  } catch {}
}

function closeLocalProviderConfig() {
  $('local-config-pop').classList.remove('open');
  $('local-config-pop').setAttribute('aria-hidden', 'true');
}

async function saveLocalProviderConfig() {
  const base_url = $('local-base-url').value.trim();
  const model = $('local-model-name').value.trim();
  const api_key = $('local-api-key').value.trim();
  const contextWindow = $('local-context-window').value.trim();
  const nativeToolsMode = $('local-native-tools-mode').value;
  if (!base_url) return;
  $('local-config-error').textContent = '';
  $('local-config-save').disabled = true;
  $('local-config-save').textContent = 'Connecting';
  try {
    // Reserve/keep derive server-side from the window preset; the page
    // never computes budgets itself.
    const payload = { base_url, model, native_tools_mode: nativeToolsMode };
    if (api_key) payload.api_key = api_key;
    if (contextWindow) payload.context_window_tokens = contextWindow;
    const r = await fetch('/api/local_provider', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.ok) {
      $('local-config-error').textContent = data.error || 'could not connect';
      return;
    }
    providerStatus.local = true;
    providerUpdatedAt.local = Date.now();
    syncProviderUI(currentProviderId());
    closeLocalProviderConfig();
    refreshProviderStatus();
  } catch {
    $('local-config-error').textContent = 'could not reach the server';
  } finally {
    $('local-config-save').disabled = false;
    $('local-config-save').textContent = 'Connect';
  }
}

function bindHandlers() {
$('provider-button').onclick = (e) => {
  if ($('provider-button').disabled) return;
  e.stopPropagation();
  const menu = $('provider-menu');
  menu.classList.toggle('open');
  $('provider-button').classList.toggle('open', menu.classList.contains('open'));
  if (menu.classList.contains('open')) refreshProviderStatus();
};
document.addEventListener('click', (e) => {
  if (!$('provider-menu').contains(e.target) && !$('provider-button').contains(e.target)) {
    $('provider-menu').classList.remove('open');
    $('provider-button').classList.remove('open');
  }
  if (
    $('local-config-pop').classList.contains('open') &&
    !$('local-config-pop').contains(e.target) &&
    !e.target.closest('.provider-item')
  ) {
    closeLocalProviderConfig();
  }
});
$('local-config-close').onclick = closeLocalProviderConfig;
$('local-config-save').onclick = saveLocalProviderConfig;
}

window.CodeyProviderUI = {
  init,
  label: providerLabel,
  sync: syncProviderUI,
  applyConfig: applyProviderConfig,
  adoptCatalog: adoptBackendCatalog,
  applyStatus: applyProviderStatus,
  refreshStatus: refreshProviderStatus,
  openLocalConfig: openLocalProviderConfig,
  closeLocalConfig: closeLocalProviderConfig,
};
})();
