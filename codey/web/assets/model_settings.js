/* One source editor for every connector, including removable integrations. */
(function () {
'use strict';
let deps, draft, generation = 0, saving = false;
const $ = id => document.getElementById(id);
function init(next) {
  deps = next;
  $('model-settings-save').onclick = save;
  $('model-settings-cancel').onclick = deps.close;
  $('model-settings-retry').onclick = open;
}
function error(text) { $('model-settings-error').textContent = text; }
function used(source, model) {
  return draft.in_use.some(item => (source.id === 'websites' ?
    source.models.some(entry => entry.id === item.provider) && (!model || item.provider === model) :
    item.provider === source.id && (!model || !item.model || item.model === model)));
}
function button(label, action) {
  const node = document.createElement('button'); node.type = 'button'; node.className = 'link-btn';
  node.textContent = label; node.onclick = action; return node;
}
function checkbox(label, checked, action, className = '') {
  const input = document.createElement('input'); input.type = 'checkbox'; input.checked = checked;
  input.className = className; input.setAttribute('aria-label', label); input.onchange = () => action(input.checked);
  return input;
}
function renderSource(source) {
  const choice = draft.preferences.sources[source.id];
  const section = document.createElement('section'); section.className = 'model-source'; section.dataset.sourceId = source.id;
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  const name = document.createElement('span'); name.textContent = source.label;
  const count = document.createElement('span'); count.className = 'model-source-count';
  const toggle = checkbox('Enable ' + source.label, choice.enabled, checked => { choice.enabled = checked; sync(); }, 'model-source-toggle');
  if (used(source)) toggle.title = 'In use';
  summary.append(name, count); details.appendChild(summary); section.append(details, toggle);
  const body = document.createElement('div'); body.className = 'model-source-body'; details.appendChild(body);
  const actions = document.createElement('div'); actions.className = 'model-source-actions'; body.appendChild(actions);
  actions.append(button('Select all', () => { choice.models = [...new Set([...choice.models, ...(source.models || []).map(model => model.id)])]; choice.enabled = !!choice.models.length; sync(); }),
    button('Clear', () => { choice.models = choice.models.filter(model => used(source, model)); sync(); }));
  let query = '';
  if ((source.models || []).length > 8) {
    const search = document.createElement('input'); search.type = 'search'; search.placeholder = 'Find models';
    search.setAttribute('aria-label', 'Find ' + source.label + ' models'); body.appendChild(search);
    search.oninput = () => { query = search.value.trim().toLowerCase(); sync(); };
  }
  const list = document.createElement('div'); list.className = 'model-source-models'; body.appendChild(list);
  const rows = (source.models || []).map(model => {
    const label = document.createElement('label'); label.className = 'model-choice';
    const input = checkbox('Use ' + (model.name || model.id), choice.models.includes(model.id), checked => {
      choice.models = checked ? [...new Set([...choice.models, model.id])] : choice.models.filter(id => id !== model.id);
      if (checked) choice.enabled = true;
      sync();
    }); input.disabled = used(source, model.id);
    const text = document.createElement('span'); text.textContent = model.name || model.id; label.append(input, text); list.appendChild(label);
    return {label, input, model};
  });
  if (!rows.length) { const empty = document.createElement('p'); empty.className = 'settings-hint'; empty.textContent = 'No models loaded'; list.appendChild(empty); }
  if (source.discoverable) {
    const refresh = button('Refresh models', async () => {
      const request = generation; refresh.disabled = true; refresh.textContent = 'Refreshing…'; error('');
      try {
        const response = await fetch('/api/model_catalog', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source:source.id})});
        const data = await response.json();
        if (request !== generation) return;
        if (!response.ok) throw new Error(data.error || 'Could not refresh models');
        window.CodeyModels.observeCatalog(data.source);
        draft.sources = draft.sources.map(item => item.id === source.id ? {...item, ...data.source} : item);
        render(source.id);
      } catch (exception) { if (request === generation) error(exception.message); }
      finally { refresh.disabled = false; refresh.textContent = 'Refresh models'; }
    }); actions.appendChild(refresh);
  }
  if (source.connection_editor) body.appendChild($('local-connection-editor'));
  function sync() {
    if (!choice.models.length) choice.enabled = false;
    toggle.checked = choice.enabled;
    toggle.disabled = used(source) || !choice.models.length;
    count.textContent = choice.models.length + ' selected';
    section.classList.toggle('source-disabled', !choice.enabled);
    rows.forEach(({label, input, model}) => {
      input.checked = choice.models.includes(model.id);
      label.hidden = !!query && !(model.name || model.id).toLowerCase().includes(query) && !model.id.toLowerCase().includes(query);
    });
  }
  sync(); return section;
}
function render(expanded) {
  const list = $('model-source-list');
  const opened = expanded ? [expanded] : Array.from(list.querySelectorAll('details[open]')).map(node => node.closest('.model-source').dataset.sourceId);
  // Preserve the connection form and its values when a catalog refresh redraws sources.
  $('model-settings-body').appendChild($('local-connection-editor'));
  $('local-connection-editor').hidden = !draft.sources.some(source => source.connection_editor);
  list.replaceChildren(...draft.sources.map(renderSource));
  opened.forEach(id => { const section = Array.from(list.children).find(node => node.dataset.sourceId === id); if (section) section.querySelector('details').open = true; });
}
async function open() {
  const request = ++generation; saving = false;
  error(''); $('model-settings-save').disabled = true; $('model-settings-retry').hidden = true;
  $('model-settings-loading').hidden = false; $('model-source-list').hidden = true;
  try {
    draft = await window.CodeyModels.load();
    if (request !== generation) return;
    render(); $('model-source-list').hidden = false; $('model-settings-save').disabled = false;
  } catch (exception) {
    if (request === generation) { error(exception.message); $('model-settings-retry').hidden = false; }
  } finally { if (request === generation) $('model-settings-loading').hidden = true; }
}
function close() { generation++; }
async function save() {
  if (saving || !draft) return;
  saving = true; const request = generation; error('');
  $('model-settings-save').disabled = true;
  $('model-source-list').inert = true;
  try {
    const response = await fetch('/api/model_settings', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({base_revision:draft.preferences.revision, sources:draft.preferences.sources})});
    const data = await response.json();
    if (request !== generation) return;
    if (!response.ok) throw new Error(data.error || 'Could not save models');
    window.CodeyModels.applySettings(data); deps.close();
  } catch (exception) { if (request === generation) error(exception.message); }
  finally { saving = false; $('model-settings-save').disabled = false; $('model-source-list').inert = false; }
}
window.CodeyModelSettings = {init, open, close};
})();
