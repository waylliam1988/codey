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
  bindHandlers();
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
function applyProviderStatus(providers) {
  if (!Array.isArray(providers)) return;
  for (const item of providers) {
    if (item && PROVIDERS.includes(item.id)) providerStatus[item.id] = !!item.available;
  }
  syncProviderUI(currentProviderId());
}
async function refreshProviderStatus() {
  try {
    const r = await fetch('/api/providers');
    if (!r.ok) return;
    const data = await r.json();
    applyProviderStatus(data.providers);
  } catch {}
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
    if (local.base_url) $('local-base-url').value = local.base_url;
    if (local.model) $('local-model-name').value = local.model;
    const cands = Array.isArray(local.candidates) ? local.candidates : [];
    $('local-config-candidates').innerHTML = cands.length
      ? cands.map(url => `<button type="button" data-url="${escapeHtml(url)}">${escapeHtml(url)}</button>`).join('')
      : '';
    document.querySelectorAll('#local-config-candidates button').forEach((btn) => {
      btn.onclick = () => { $('local-base-url').value = btn.dataset.url || ''; };
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
  if (!base_url) return;
  $('local-config-error').textContent = '';
  $('local-config-save').disabled = true;
  $('local-config-save').textContent = 'Connecting';
  try {
    const payload = { base_url, model };
    if (api_key) payload.api_key = api_key;
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
    !e.target.closest('.provider-item') &&
    e.target.id !== 'ctx-provider'
  ) {
    closeLocalProviderConfig();
  }
});
document.querySelectorAll('.provider-item').forEach((btn) => {
  btn.onclick = () => {
    setActiveProvider(btn.dataset.provider);
    $('provider-menu').classList.remove('open');
    $('provider-button').classList.remove('open');
  };
});
$('local-config-close').onclick = closeLocalProviderConfig;
$('local-config-save').onclick = saveLocalProviderConfig;
}

window.CodeyProviderUI = {
  init,
  label: providerLabel,
  sync: syncProviderUI,
  applyStatus: applyProviderStatus,
  refreshStatus: refreshProviderStatus,
  openLocalConfig: openLocalProviderConfig,
  closeLocalConfig: closeLocalProviderConfig,
};
})();
