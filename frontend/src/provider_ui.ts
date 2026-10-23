import { runtime } from './runtime';
import type { Session } from './types/domain';
import { decodeProviderConfig, decodeProviderStatus } from './domain/protocol';
export interface ProviderModel {
  id: string;
  name?: string;
  thinking_toggle?: boolean;
  efforts?: string[];
}
export interface ProviderConnection {
  id: string;
  base_url?: string;
  default_model?: string;
  models: ProviderModel[];
}
export interface ProviderStatusItem {
  id: string;
  available?: boolean;
  freshness?: string;
  state?: string;
}
export interface ProviderUiDeps {
  $(id: string): HTMLElement;
  escapeHtml(text: unknown): string;
  currentProviderId(): string;
  setActiveProvider(id: string): void;
  PROVIDERS: string[];
  PROVIDER_LABELS: Record<string, string>;
  DEFAULT_PROVIDER: string;
  activeSession(): Session | undefined;
  persistActiveNow(): void;
}

export interface ProviderUiApi {
  init(deps: ProviderUiDeps): void;
  label(id: string): string;
  sync(providerId: string): void;
  applyConfig(data: unknown): boolean;
  adoptCatalog(): Promise<void>;
  applyStatus(providers: unknown, isSSE?: boolean, snapshotTime?: number): void;
  refreshStatus(immediate?: boolean): void;
  localReady(): boolean;
  runSelection(
    session: Session | undefined,
    provider: string,
  ): {
    model_selection?: {
      connection_id?: string;
      base_url?: string;
      model?: string;
      effort?: string | null;
      thinking?: boolean | null;
    };
  };
  applyLocalMetadata(local: unknown): void;
  applyApiModels(connections: ProviderConnection[]): void;
  openLocalConfig(): unknown;
  closeLocalConfig(): unknown;
  closeMenu(restoreFocus?: boolean): void;
}
/* Codey provider picker UI: composer model chooser, availability dots,
   provider menu, and the local provider config popover. Zero-build asset
   module; index.html injects dependencies via init() at boot. */
(function () {
  'use strict';

  let deps: ProviderUiDeps;
  interface LocalMetadata {
    connected?: boolean;
    model?: string;
    state?: string;
    base_url?: string;
    error?: string;
    message?: string;
  }
  interface ModelSelection {
    connection_id?: string;
    base_url?: string;
    model?: string;
    name?: string;
    effort?: string | null;
    thinking?: boolean | null;
    efforts: Record<string, string>;
  }
  function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null;
  }
  let PROVIDERS: string[] = [];
  let PROVIDER_LABELS: Record<string, string> = {};
  let DEFAULT_PROVIDER = '';
  let providerStatus: Record<string, boolean> = {};
  let providerState: Record<string, string> = {};
  let providerUpdatedAt: Record<string, number> = {};
  let highlightedProvider = '';

  function $<T extends HTMLElement = HTMLElement>(id: string): T {
    return deps.$(id) as T;
  }
  function escapeHtml(text: unknown): string {
    return deps.escapeHtml(text);
  }
  function currentProviderId(): string {
    return deps.currentProviderId();
  }
  function setActiveProvider(id: string): void {
    deps.setActiveProvider(id);
  }

  function init(nextDeps: ProviderUiDeps): void {
    deps = nextDeps;
    PROVIDERS = deps.PROVIDERS;
    PROVIDER_LABELS = deps.PROVIDER_LABELS;
    DEFAULT_PROVIDER = deps.DEFAULT_PROVIDER;
    providerStatus = Object.fromEntries(PROVIDERS.map((id) => [id, false]));
    providerState = Object.fromEntries(PROVIDERS.map((id) => [id, 'unknown']));
    providerUpdatedAt = Object.fromEntries(PROVIDERS.map((id) => [id, 0]));
    apiModels = Object.fromEntries(
      runtime.Models.connections().map((source: ProviderConnection) => [source.id, source]),
    );
    runtime.Models.subscribe(() => {
      apiModels = Object.fromEntries(
        runtime.Models.connections().map((source: ProviderConnection) => [
          source.id,
          { ...apiModels[source.id], ...source },
        ]),
      );
      buildProviderMenu();
      // Settings changes can invalidate the currently selected model without
      // changing the session selection. Refresh the visible projection from
      // the canonical selection so unavailable models are shown immediately.
      syncProviderUI(currentProviderId());
      runtime.Composer.updateSend();
    });
    buildProviderMenu();
    bindHandlers();
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(syncModelMenuWidth);
      observer.observe($('provider-button'));
      observer.observe($('effort-button'));
    }
    window.addEventListener('resize', syncModelMenuWidth);
    runtime.Settings.init({ $: deps.$, applyLocalMetadata });
    refreshLocalMetadata();
    refreshApiModels();
    if (catalogRefreshTimer) clearInterval(catalogRefreshTimer);
    catalogRefreshTimer = setInterval(() => void refreshApiModels(), 60 * 60 * 1000);
  }

  function buildProviderMenu() {
    const menu = deps.$('provider-menu');
    if (!menu) return;
    const warning = deps.$('provider-probe-warning');
    menu.setAttribute('role', 'dialog');
    menu.setAttribute('aria-label', 'Choose model');
    $('provider-button').setAttribute('aria-controls', 'provider-menu');
    $('provider-button').setAttribute('aria-haspopup', 'dialog');
    const focusedKey = document.activeElement instanceof HTMLElement ? document.activeElement.dataset.key : undefined;
    menu.querySelectorAll('.provider-item').forEach((btn: Element) => btn.remove());
    for (const id of PROVIDERS) {
      const connection = apiModels[id];
      const localModels = connection ? connection.models.map((item: ProviderModel) => item.id) : [''];
      if (connection && !localModels.length) localModels.push('');
      for (const model of localModels) {
        if (!runtime.Models.allows(id, model)) continue;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'provider-item';
        btn.dataset.provider = id;
        btn.dataset.model = model;
        btn.dataset.key = model ? id + ':' + model : id;
        btn.disabled = !!connection && !model;
        const dot = document.createElement('span');
        dot.className = 'dot ' + providerAvailability(id);
        dot.setAttribute('aria-hidden', 'true');
        const label = document.createElement('span');
        label.className = 'label';
        label.textContent = model ? modelTitle(model, id) : providerLabel(id);
        btn.setAttribute('aria-label', model || providerLabel(id));
        const check = document.createElement('span');
        check.className = 'check';
        check.setAttribute('aria-hidden', 'true');
        check.innerHTML =
          '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6.5 4.8 9 10 3.5"/></svg>';
        btn.append(dot, label, check);
        btn.onclick = () => {
          if ($<HTMLButtonElement>('provider-button').disabled) return;
          if (connection && model && deps.activeSession) {
            const s = deps.activeSession();
            if (s) {
              const previous = sessionSelection(s, id);
              const efforts =
                previous.connection_id === id && previous.base_url === (connection.base_url || '')
                  ? { ...previous.efforts }
                  : {};
              if (previous.model && previous.effort) efforts[previous.model] = previous.effort;
              s.modelSelection = {
                connection_id: id,
                base_url: connection.base_url || '',
                model,
                name: modelTitle(model, id),
                effort: efforts[model] || null,
                efforts,
              };
            }
          }
          setActiveProvider(id);
          if (runtime.UiState && typeof runtime.UiState.setDefaultProvider === 'function') {
            runtime.UiState.setDefaultProvider(id);
          }
          closeMenu(true);
        };
        if (warning) menu.insertBefore(btn, warning);
        else menu.appendChild(btn);
      }
    }
    $('model-menu-empty').hidden = !!modelRows().length;
    syncProviderUI(currentProviderId());
    highlightedProvider = selectedKey();
    highlightModel();
    if (focusedKey)
      modelRows()
        .find((row: HTMLButtonElement) => row.dataset.key === focusedKey)
        ?.focus();
  }

  function modelRows(): HTMLButtonElement[] {
    return Array.from($('provider-menu').querySelectorAll<HTMLButtonElement>('.provider-item'));
  }

  function highlightModel() {
    $('provider-menu')
      .querySelectorAll<HTMLButtonElement>('.provider-item')
      .forEach((row: HTMLButtonElement) =>
        row.classList.toggle('keyboard-active', row.dataset.key === highlightedProvider),
      );
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
    const left = $('provider-button').getBoundingClientRect().left;
    menu.style.setProperty('--model-menu-width', Math.min(240, Math.max(0, innerWidth - left - 16)) + 'px');
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
    (modelRows().find((row) => row.dataset.key === highlightedProvider) || modelRows()[0])?.focus();
    refreshProviderStatus(true);
    // Legacy form retained for callers without an immediate probe: refreshProviderStatus();
  }

  function applyProviderConfig(data: unknown) {
    let catalog;
    try {
      catalog = decodeProviderConfig(data);
    } catch {
      return false;
    }
    const ids = catalog.providers.map((item) => item.id);
    const labels = catalog.labels;
    if (catalog.defaultProvider && runtime.UiState && typeof runtime.UiState.setDefaultProvider === 'function') {
      runtime.UiState.setDefaultProvider(catalog.defaultProvider);
    }
    let changed = false;
    if (PROVIDERS.length !== ids.length) {
      changed = true;
    } else {
      for (let i = 0; i < ids.length; i++) {
        if (PROVIDERS[i] !== ids[i]) {
          changed = true;
          break;
        }
      }
      if (!changed) {
        for (const id of ids) {
          if (PROVIDER_LABELS[id] !== labels[id]) {
            changed = true;
            break;
          }
        }
      }
    }
    const defaultId = catalog.defaultProvider;
    if (!changed && defaultId && defaultId !== DEFAULT_PROVIDER && labels[defaultId]) changed = true;
    if (!changed) return false;
    runtime.UiState.setProviders(ids, labels, defaultId);
    DEFAULT_PROVIDER = runtime.UiState.DEFAULT_PROVIDER;
    providerStatus = Object.fromEntries(PROVIDERS.map((id) => [id, !!providerStatus[id]]));
    providerState = Object.fromEntries(PROVIDERS.map((id) => [id, providerState[id] || 'unknown']));
    providerUpdatedAt = Object.fromEntries(PROVIDERS.map((id) => [id, providerUpdatedAt[id] || 0]));
    buildProviderMenu();
    return true;
  }

  async function adoptBackendCatalog() {
    // Model preferences/catalogs are useful as soon as they arrive, but they
    // must not gate boot. Settings and the composer can render from the
    // current snapshot while this request is still probing a local endpoint.
    void runtime.Models.load().catch(() => undefined);
    // Boot-time only: adopt the backend catalog into ui_state before init.
    // Hits the cheap static catalog (no CDP/network probe); availability
    // stays on the async /api/providers refresh path. Do not touch DOM
    // here (deps is unset); menu is built later by init().
    // index.html always loads ui_state.js and dereferences UiState at
    // parse time, so the state module is guaranteed present here.
    try {
      const r = await fetch('/api/provider_catalog', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      const catalog = decodeProviderConfig(data);
      runtime.UiState.setProviders(
        catalog.providers.map((item) => item.id),
        catalog.labels,
        catalog.defaultProvider,
      );
    } catch {}
  }

  function providerLabel(id: string): string {
    return PROVIDER_LABELS[id] || id || PROVIDER_LABELS[DEFAULT_PROVIDER];
  }
  function providerAvailability(id: string): string {
    // Compatibility contract: providerStatus[id] ? 'ok' : ''.
    const state = providerState[id] || (providerStatus[id] ? 'ready' : 'unknown');
    return state === 'ready' || state === 'stale' ? 'ok' : state === 'checking' ? 'checking' : '';
  }
  function syncProviderUI(providerId: string): void {
    const id = providerId;
    const selection = sessionSelection();
    const reason = runtime.Models.reason(id, selection.model);
    $('provider-name').textContent =
      (selection.model ? modelTitle(selection.model, id) : !id || apiModels[id] ? 'Choose model' : providerLabel(id)) +
      (id && reason && (selection.model || !apiModels[id]) ? ' · ' + reason : '');
    const staleConnection =
      id === 'local' && selection.base_url && localMetadata.base_url && selection.base_url !== localMetadata.base_url;
    $('provider-button').setAttribute(
      'aria-label',
      'Choose model: ' +
        $('provider-name').textContent +
        (staleConnection ? '. Connection changed; select the model again.' : ''),
    );
    $('provider-dot').className = 'dot ' + (staleConnection ? '' : providerAvailability(id));
    document.querySelectorAll<HTMLElement>('.provider-item').forEach((btn: HTMLElement) => {
      btn.classList.toggle(
        'active',
        btn.dataset.provider === id && (!btn.dataset.model || btn.dataset.model === selection.model),
      );
      btn.setAttribute(
        'aria-pressed',
        String(btn.dataset.provider === id && (!btn.dataset.model || btn.dataset.model === selection.model)),
      );
      const dot = btn.querySelector('.dot');
      if (dot) dot.className = 'dot ' + providerAvailability(btn.dataset.provider || '');
    });
    syncThinking();
    syncModelMenuWidth();
  }
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;
  let lastRequestId = 0;

  function applyProviderStatus(rawProviders: unknown, isSSE = true, snapshotTime = Date.now()): void {
    let providers: ProviderStatusItem[];
    try {
      providers = decodeProviderStatus(rawProviders);
    } catch {
      return;
    }
    const appliedAt = Date.now();
    let apiStatusChanged = false;
    for (const item of providers) {
      if (!item || !PROVIDERS.includes(item.id)) continue;
      if (!isSSE && providerUpdatedAt[item.id] > snapshotTime) continue;
      providerStatus[item.id] = !!item.available;
      providerState[item.id] =
        item.freshness === 'stale' && item.state === 'ready'
          ? 'stale'
          : item.state || (item.available ? 'ready' : 'unavailable');
      providerUpdatedAt[item.id] = isSSE ? appliedAt : snapshotTime;
      if (runtime.Models.connections().some((source: ProviderConnection) => source.id === item.id))
        apiStatusChanged = true;
    }
    syncProviderUI(currentProviderId());
    if (apiStatusChanged && isSSE) refreshApiModels();
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
  function setProbeWarning(failed: boolean) {
    $('provider-probe-warning').hidden = !failed;
  }
  async function _doRefreshProviderStatus(): Promise<void> {
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

  function openLocalProviderConfig() {
    return runtime.Settings.open();
  }
  function closeLocalProviderConfig() {
    return runtime.Settings.close();
  }

  let localMetadata: LocalMetadata = {},
    localRequest = 0;
  function localReady() {
    return !!(localMetadata.connected && localMetadata.model);
  }
  function sessionSelection(s: Session | undefined = deps.activeSession?.(), id = currentProviderId()): ModelSelection {
    const connection = apiModels[id] || {};
    const stored = s?.modelSelection;
    const old: ModelSelection =
      stored?.connection_id === id
        ? {
            connection_id: typeof stored.connection_id === 'string' ? stored.connection_id : id,
            base_url: typeof stored.base_url === 'string' ? stored.base_url : '',
            model: typeof stored.model === 'string' ? stored.model : '',
            name: typeof stored.name === 'string' ? stored.name : undefined,
            effort: typeof stored.effort === 'string' ? stored.effort : null,
            thinking: typeof stored.thinking === 'boolean' ? stored.thinking : null,
            efforts: isRecord(stored.efforts)
              ? Object.fromEntries(
                  Object.entries(stored.efforts).filter(
                    (entry): entry is [string, string] => typeof entry[1] === 'string',
                  ),
                )
              : {},
          }
        : {
            connection_id: id,
            base_url: connection.base_url || '',
            model: '',
            efforts: {},
          };
    return {
      ...old,
      effort: old.effort || null,
      thinking: typeof old.thinking === 'boolean' ? old.thinking : null,
      efforts: old.efforts || {},
    };
  }
  function selectedKey() {
    const id = currentProviderId();
    return apiModels[id] ? id + ':' + sessionSelection().model : id;
  }
  function modelTitle(model: string, id = currentProviderId()): string {
    const record = apiModels[id]?.models?.find((item) => item.id === model);
    if (record?.name) return record.name;
    const stored = deps.activeSession?.()?.modelSelection;
    if (stored?.connection_id === id && stored.model === model && typeof stored.name === 'string' && stored.name)
      return stored.name;
    const leaf = model.split('/').pop() || model;
    const short = leaf.match(/^([A-Za-z][\w.]*?)[-_](\d+(?:\.\d+)?[Bb])(?=[-_]|$)/);
    return short ? `${short[1]} ${short[2].toUpperCase()}` : leaf;
  }
  function thinkingOptions(s: Session | undefined = deps.activeSession?.(), id = currentProviderId()): string[] {
    const selection = sessionSelection(s, id);
    const model = apiModels[id]?.models?.find((item) => item.id === selection.model);
    if (model?.efforts?.length) {
      return model.efforts.includes('off')
        ? ['off', ...model.efforts.filter((value) => value !== 'off')]
        : ['default', ...model.efforts];
    }
    return model?.thinking_toggle ? ['off', 'on'] : [];
  }
  const effortLabels: Record<string, string> = {
    default: 'Default',
    off: 'Off',
    on: 'On',
    minimal: 'Mini',
    low: 'Low',
    medium: 'Med',
    high: 'High',
    xhigh: 'XHigh',
    max: 'Max',
  };
  function selectedEffort(s: Session | undefined = deps.activeSession?.(), id = currentProviderId()): string {
    const selection = sessionSelection(s, id),
      options = thinkingOptions(s, id);
    const model = apiModels[id]?.models?.find((item) => item.id === selection.model);
    const selected = model?.efforts?.length
      ? selection.effort || 'default'
      : model?.thinking_toggle
        ? selection.thinking === true
          ? 'on'
          : 'off'
        : selection.effort || undefined;
    const normalized = selected || '';
    return options.includes(normalized) ? normalized : options[0] || '';
  }
  function closeEffortMenu(restoreFocus = false) {
    const menu = $('effort-menu');
    if (!menu) return;
    menu.classList.remove('open');
    menu.setAttribute('aria-hidden', 'true');
    $('effort-button').classList.remove('open');
    $('effort-button').setAttribute('aria-expanded', 'false');
    if (restoreFocus && !$('effort-chooser').hidden) $('effort-button').focus();
  }
  function syncThinking() {
    const button = $<HTMLButtonElement>('effort-button');
    const options = thinkingOptions(),
      value = selectedEffort();
    $('effort-chooser').hidden = !options.length;
    button.disabled = $<HTMLButtonElement>('provider-button').disabled;
    const label = effortLabels[value] || value;
    $('effort-name').textContent = label;
    const model = apiModels[currentProviderId()]?.models?.find((item) => item.id === sessionSelection().model);
    button.setAttribute('aria-label', (model?.efforts?.length ? 'Thinking effort: ' : 'Thinking: ') + label);
    const menu = $('effort-menu');
    const signature = JSON.stringify([sessionSelection().base_url, sessionSelection().model, options, value]);
    if (menu.dataset.signature === signature) return;
    const restoreFocus = menu.contains(document.activeElement);
    closeEffortMenu(restoreFocus);
    menu.dataset.signature = signature;
    menu.replaceChildren();
    options.forEach((option) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'provider-action';
      row.dataset.effort = option;
      row.textContent = effortLabels[option] || option;
      const model = apiModels[currentProviderId()]?.models?.find((item) => item.id === sessionSelection().model);
      row.setAttribute('aria-label', (model?.efforts?.length ? 'Thinking effort: ' : 'Thinking: ') + row.textContent);
      row.setAttribute('aria-pressed', String(value === option));
      row.onclick = () => {
        const s = deps.activeSession();
        if (!s || $<HTMLButtonElement>('provider-button').disabled) return;
        const selection = sessionSelection(s);
        const model = apiModels[currentProviderId()]?.models?.find((item) => item.id === selection.model);
        s.modelSelection = model?.efforts?.length
          ? {
              ...selection,
              effort: option === 'default' ? null : option,
              thinking: null,
              efforts:
                selection.model && option !== 'default'
                  ? { ...selection.efforts, [selection.model]: option }
                  : selection.efforts,
            }
          : model?.thinking_toggle
            ? { ...selection, thinking: option === 'on', effort: null }
            : {
                ...selection,
                effort: option,
                efforts: selection.model ? { ...selection.efforts, [selection.model]: option } : selection.efforts,
              };
        deps.persistActiveNow();
        syncProviderUI(currentProviderId());
        closeEffortMenu(true);
      };
      menu.appendChild(row);
    });
  }
  function applyLocalMetadata(local: unknown) {
    localRequest++;
    const next = local && typeof local === 'object' ? local : {};
    if (JSON.stringify(next) === JSON.stringify(localMetadata)) return;
    localMetadata = isRecord(next)
      ? {
          connected: typeof next.connected === 'boolean' ? next.connected : undefined,
          model: typeof next.model === 'string' ? next.model : undefined,
          state: typeof next.state === 'string' ? next.state : undefined,
          base_url: typeof next.base_url === 'string' ? next.base_url : undefined,
        }
      : {};
    refreshApiModels();
    if (typeof localMetadata.connected === 'boolean') {
      providerStatus.local = localMetadata.connected;
      providerState.local = localMetadata.state || (localMetadata.connected ? 'ready' : 'unavailable');
      providerUpdatedAt.local = Date.now();
    }
    buildProviderMenu();
  }
  async function refreshLocalMetadata() {
    if (!runtime.Models.snapshot().preferences.sources.local?.enabled) return;
    const request = ++localRequest;
    try {
      const r = await fetch('/api/local_provider', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      if (request === localRequest && data.local) applyLocalMetadata(data.local);
    } catch {}
  }
  let apiModels: Record<string, ProviderConnection> = {},
    apiModelsRequest = 0;
  let catalogRefreshTimer: ReturnType<typeof setInterval> | null = null;
  function applyApiModels(connections: ProviderConnection[]): void {
    connections.forEach((connection: ProviderConnection) => {
      if (runtime.Models.connections().some((source: ProviderConnection) => source.id === connection.id))
        runtime.Models.observeCatalog(connection);
    });
    buildProviderMenu();
  }
  async function refreshApiModels() {
    const request = ++apiModelsRequest;
    try {
      const r = await fetch('/api/api_models', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      if (request !== apiModelsRequest || !Array.isArray(data.connections)) return;
      applyApiModels(data.connections);
    } catch {}
  }
  function runSelection(s: Session | undefined, provider: string) {
    const selection = sessionSelection(s, provider);
    if (!apiModels[provider] && !selection.model) return {};
    const options = thinkingOptions(s, provider);
    const model = apiModels[provider]?.models?.find((item) => item.id === selection.model);
    if (model?.efforts?.length) {
      const effort = selectedEffort(s, provider);
      return selection.model
        ? {
            model_selection: {
              connection_id: provider,
              base_url: selection.base_url,
              model: selection.model,
              effort: effort === 'default' ? null : effort,
            },
          }
        : {};
    }
    if (model?.thinking_toggle) {
      return selection.model
        ? {
            model_selection: {
              connection_id: provider,
              base_url: selection.base_url,
              model: selection.model,
              effort: null,
              thinking: selection.thinking === true,
            },
          }
        : {};
    }
    const effort = options.length ? selectedEffort(s, provider) : selection.effort;
    return selection.model
      ? { model_selection: { connection_id: provider, base_url: selection.base_url, model: selection.model, effort } }
      : {};
  }

  function bindHandlers() {
    $('provider-button').onclick = (e) => {
      if ($<HTMLButtonElement>('provider-button').disabled) return;
      e.stopPropagation();
      if ($('provider-menu').classList.contains('open')) closeMenu(true);
      else openMenu();
    };
    $('provider-button').setAttribute('aria-expanded', 'false');
    $('provider-menu').addEventListener('keydown', (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        closeMenu(true);
        return;
      }
      const rows = modelRows();
      if (!rows.length) return;
      const focused = document.activeElement instanceof HTMLButtonElement ? rows.indexOf(document.activeElement) : -1;
      let index = focused >= 0 ? focused : rows.findIndex((row) => row.dataset.key === highlightedProvider);
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
        e.preventDefault();
        index =
          e.key === 'Home'
            ? 0
            : e.key === 'End'
              ? rows.length - 1
              : (index + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
        highlightedProvider = rows[index].dataset.key || '';
        highlightModel();
        rows[index].scrollIntoView({ block: 'nearest' });
        rows[index].focus();
      }
    });
    $('provider-menu').addEventListener('focusout', () => {
      setTimeout(() => {
        if (!$('provider-menu').contains(document.activeElement) && document.activeElement !== $('provider-button'))
          closeModelMenu();
      }, 0);
    });
    document.addEventListener('click', (e) => {
      const target = e.target instanceof Node ? e.target : null;
      if (!$('provider-menu').contains(target) && !$('provider-button').contains(target)) {
        closeModelMenu();
      }
      if (!$('effort-menu').contains(target) && !$('effort-button').contains(target)) closeEffortMenu();
    });
    $('effort-button').onclick = (e) => {
      e.stopPropagation();
      if ($<HTMLButtonElement>('provider-button').disabled || !thinkingOptions().length) return;
      if ($('effort-menu').classList.contains('open')) {
        closeEffortMenu(true);
        return;
      }
      closeMenu();
      const menu = $('effort-menu');
      menu.classList.add('open');
      menu.setAttribute('aria-hidden', 'false');
      $('effort-button').classList.add('open');
      $('effort-button').setAttribute('aria-expanded', 'true');
      (
        menu.querySelector<HTMLElement>('[aria-pressed="true"]') || (menu.firstElementChild as HTMLElement | null)
      )?.focus();
    };
    $('effort-menu').addEventListener('keydown', (e) => {
      if (e.isComposing) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        closeEffortMenu(true);
        return;
      }
      const rows = Array.from($('effort-menu').children) as HTMLElement[],
        index = document.activeElement instanceof HTMLElement ? rows.indexOf(document.activeElement) : -1;
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key) && rows.length) {
        e.preventDefault();
        const next =
          e.key === 'Home'
            ? 0
            : e.key === 'End'
              ? rows.length - 1
              : (index + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
        rows[next].scrollIntoView({ block: 'nearest' });
        rows[next].focus();
      }
    });
    $('effort-menu').addEventListener('focusout', () =>
      setTimeout(() => {
        if (!$('effort-menu').contains(document.activeElement) && document.activeElement !== $('effort-button'))
          closeEffortMenu();
      }, 0),
    );
  }

  runtime.ProviderUI = {
    init,
    label: providerLabel,
    sync: syncProviderUI,
    applyConfig: applyProviderConfig,
    adoptCatalog: adoptBackendCatalog,
    applyStatus: applyProviderStatus,
    refreshStatus: refreshProviderStatus,
    localReady,
    runSelection,
    applyLocalMetadata,
    applyApiModels,
    openLocalConfig: openLocalProviderConfig,
    closeLocalConfig: closeLocalProviderConfig,
    closeMenu,
  };
})();
