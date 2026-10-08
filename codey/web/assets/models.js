/* Source preferences are authoritative; discovery supplies names, never choices. */
(function () {
'use strict';
let state = {preferences:{revision:0, sources:{}}, sources:[], in_use:[]};
const listeners = new Set();
function snapshot() { return structuredClone(state); }
function applySettings(data) {
  if (!data?.preferences || !Array.isArray(data.sources)) throw new Error('Model settings unavailable');
  if (data.preferences.revision < state.preferences.revision) return;
  state = structuredClone(data);
  listeners.forEach(listener => listener());
}
async function load() {
  const response = await fetch('/api/model_settings', {cache:'no-store'});
  if (!response.ok) throw new Error('Could not load models');
  applySettings(await response.json());
  return snapshot();
}
function sourceFor(provider) {
  return state.sources.find(source => source.id === provider ||
    (source.id === 'websites' && source.models.some(model => model.id === provider)));
}
function reason(provider, model = '') {
  const source = sourceFor(provider);
  if (!source) return 'Unavailable';
  const choice = state.preferences.sources[source.id];
  return choice?.enabled && choice.models.includes(source.id === 'websites' ? provider : model) ? '' : 'Disabled';
}
function allows(provider, model = '') { return !reason(provider, model); }
function connections() { return state.sources.filter(source => source.id !== 'websites'); }
function hasAny() {
  return state.sources.some(source => state.preferences.sources[source.id]?.enabled &&
    source.models?.some(model => state.preferences.sources[source.id].models.includes(model.id)));
}
function observeCatalog(source) {
  const index = state.sources.findIndex(item => item.id === source.id);
  if (index < 0) return;
  state.sources[index] = {...state.sources[index], ...source};
  listeners.forEach(listener => listener());
}
window.CodeyModels = {load, snapshot, applySettings, allows, reason, connections, hasAny, observeCatalog,
  subscribe:listener => listeners.add(listener)};
})();
