/* Conversation continuity, quiet status, search, and keyboard navigation. */
(function () {
'use strict';
let deps = null;
let shownId = '';
const views = new Map();
let menuTrigger = null;
let selectedCopy = null, copyGeneration = 0;

function $(id) { return document.getElementById(id); }
function query() { return $('chat-search').value.trim().toLocaleLowerCase(); }
function matchesSession(s) {
  const p = deps.sessionProject(s);
  return [s.title, p && p.name].filter(Boolean).join(' ').toLocaleLowerCase().includes(query());
}
function matchesProject(p) {
  return !query() || String(p.name || '').toLocaleLowerCase().includes(query())
    || deps.getSessions().some(s => s.projectId === p.id && matchesSession(s));
}
function appendGroupLabel(list, text, hasMatches) {
  const label = document.createElement('div');
  label.className = 'group-label'; label.textContent = text;
  label.hidden = !!query() && !hasMatches;
  list.appendChild(label);
}
function updateSearch() {
  if (!deps) return;
  $('chat-search-clear').hidden = $('chat-search').hidden || !$('chat-search').value;
  const empty = !!query() && !deps.getProjects().some(matchesProject) && !deps.getSessions().some(matchesSession);
  $('search-empty').hidden = !empty;
  $('session-list').hidden = empty;
}
function isFollowing() { const a = $('chat-area'); return a.scrollHeight - a.scrollTop - a.clientHeight < 120; }

function captureView() {
  if (!shownId) return;
  const session = deps.getSessions().find(s => s.id === shownId);
  if (!session) { views.delete(shownId); return; }
  views.delete(shownId);
  views.set(shownId, { messages: session.messages.slice(), nodes: Array.from($('chat').childNodes),
    disclosures: window.CodeyProcess.capture($('chat')), scroll: $('chat-area').scrollTop, following: isFollowing() });
  const cached = Array.from(views.values()).filter(view => view.nodes);
  for (const old of cached.slice(0, Math.max(0, cached.length - 6))) { old.nodes = null; old.messages = null; }
}
function unchanged(view, session) {
  return view && view.nodes && view.messages && view.messages.length === session.messages.length
    && view.messages.every((m, i) => m === session.messages[i] || JSON.stringify(m) === JSON.stringify(session.messages[i]));
}
function renderChat(forceBottom = false) {
  if (!deps) return;
  const s = deps.activeSession();
  if (!s) return;
  window.CodeyComposer.syncSession();
  const p = deps.activeProject();
  const title = $('sess-title');
  title.replaceChildren();
  if (p) {
    const project = document.createElement('span'); project.className = 'crumb-proj'; project.textContent = p.name;
    const separator = document.createElement('span'); separator.className = 'crumb-sep'; separator.textContent = '/';
    title.append(project, separator);
  }
  const name = document.createElement('span'); name.className = 'crumb-chat'; name.textContent = s.title || 'New chat'; title.append(name);
  deps.syncProviderUI(deps.currentProviderId());
  deps.updateComposerContext();
  deps.syncResearchUseProjectButton();
  const changing = shownId !== s.id;
  if (changing) { closeSelectionMenu(); captureView(); }
  const saved = views.get(s.id);
  const scroll = changing && saved ? saved.scroll : $('chat-area').scrollTop;
  const follow = changing ? (!saved || saved.following) : isFollowing();
  const chat = $('chat');
  if (unchanged(saved, s)) {
    if (changing) chat.replaceChildren(...saved.nodes);
  } else {
    const disclosures = changing ? saved?.disclosures : window.CodeyProcess.capture(chat);
    chat.replaceChildren();
    if (!s.messages.length) {
      const welcome = document.createElement('div'); welcome.className = 'welcome';
      welcome.innerHTML = '<h1>Codey</h1><p>Send a message to start.</p>'; chat.append(welcome);
    } else for (const m of s.messages) deps.appendMessageNode(chat, m);
    window.CodeyProcess.restore(chat, disclosures);
  }
  shownId = s.id;
  $('chat-area').scrollTop = scroll;
  if (forceBottom || follow) $('chat-area').scrollTop = $('chat-area').scrollHeight;
  captureView();
  updateLatest();
  updateNotice();
}

function scrollChat(force = false) {
  if (force || isFollowing()) $('chat-area').scrollTop = $('chat-area').scrollHeight;
  captureView();
  updateLatest();
  updateNotice();
}
function updateLatest() { $('back-to-latest').hidden = isFollowing(); }

function updateNotice() {
  if (!deps) return;
  const sessions = deps.getSessions();
  const runningId = deps.getRunningSessionId();
  const owner = sessions.find(s => s.id === runningId);
  const status = $('status');
  if (owner && status.classList.contains('run')) {
    const text = status.querySelector('.status-text');
    if (text) text.textContent = owner.id === deps.getActiveId() ? 'Running' : 'Running in ' + owner.title;
    let open = status.querySelector('button');
    if (owner.id !== deps.getActiveId()) {
      if (!open) { open = document.createElement('button'); open.className = 'link-btn'; open.textContent = 'Open'; status.append(open); }
      open.onclick = () => deps.switchSession(owner.id);
    } else if (open) open.remove();
  }
  const pendingSession = sessions.find(s => s.messages.some(m => m.type === 'shell_request'));
  const notice = $('composer-notice');
  const pending = pendingSession && pendingSession.messages.find(m => m.type === 'shell_request');
  const key = pending ? pendingSession.id + ':' + pending.id : '';
  if (notice.dataset.pending === key) return;
  notice.dataset.pending = key;
  notice.replaceChildren();
  notice.hidden = !pending;
  if (!pending) return;
  const text = document.createElement('span'); text.textContent = 'Approval required';
  const review = document.createElement('button'); review.className = 'link-btn'; review.textContent = 'Review command';
  review.onclick = () => {
    if (deps.getActiveId() !== pendingSession.id) deps.switchSession(pendingSession.id);
    const card = Array.from($('chat').querySelectorAll('[data-approval-id]')).find(el => el.dataset.approvalId === String(pending.id));
    if (card) { card.scrollIntoView({block:'center'}); card.focus({preventScroll:true}); }
  };
  notice.append(text, review);
}

function openFile(path, project = '') {
  const target = project || deps.currentProjectPath();
  if (target && path) return window.CodeyChangesDrawer.open(target, path);
}
function forget(id) { views.delete(id); window.CodeyComposer.forgetDraft(id); }

function menuOpened(menu, anchor) {
  menuTrigger = anchor;
  menu.setAttribute('role', 'menu');
  menu.querySelectorAll('button').forEach(button => button.setAttribute('role', 'menuitem'));
  anchor.setAttribute('aria-expanded', 'true');
  menu.querySelector('button:not(:disabled)')?.focus();
}
function menusClosed(restore = false) {
  closeSelectionMenu(restore);
  if (menuTrigger) {
    menuTrigger.setAttribute('aria-expanded', 'false');
    if (restore && menuTrigger.isConnected) {
      const row = menuTrigger.closest('.session-item, .project-row');
      row?.querySelector('.session-title, .project-main')?.focus();
      menuTrigger.focus();
    }
    menuTrigger = null;
  }
}

function initSidebarResize() {
  const handle = $('sidebar-resize'), root = document.documentElement;
  const tokens = getComputedStyle(root), token = name => parseFloat(tokens.getPropertyValue(name));
  const initial = token('--sidebar-width'), min = token('--sidebar-min-width'), max = token('--sidebar-max-width');
  const contentMin = token('--sidebar-content-min-width'), narrow = () => innerWidth <= 720;
  let preferred = initial, drag = null;
  try {
    const saved = JSON.parse(localStorage.getItem('codey:sidebar-width'));
    if (typeof saved === 'number' && Number.isFinite(saved)) preferred = Math.max(min, Math.min(max, saved));
  } catch {}
  const limit = () => Math.max(min, Math.min(max, innerWidth - contentMin));
  const clamp = value => Math.max(min, Math.min(limit(), value));
  function apply(snap = false) {
    if (snap) $('aside').style.transition = 'none';
    const size = narrow() ? Math.min(initial, innerWidth - 32) : clamp(preferred);
    root.style.setProperty('--sidebar-width', size + 'px');
    handle.setAttribute('aria-valuemin', String(min)); handle.setAttribute('aria-valuemax', String(limit()));
    handle.setAttribute('aria-valuenow', String(size));
    if (snap) { $('aside').getBoundingClientRect(); $('aside').style.removeProperty('transition'); }
  }
  function save() { try { localStorage.setItem('codey:sidebar-width', JSON.stringify(preferred)); } catch {} }
  function finish(cancel = false) {
    if (!drag) return;
    const previous = drag; drag = null;
    if (cancel) preferred = previous.preferred;
    document.body.classList.remove('sidebar-resizing');
    if (handle.hasPointerCapture(previous.id)) handle.releasePointerCapture(previous.id);
    apply(); if (!cancel) save();
  }
  handle.addEventListener('pointerdown', e => {
    if (e.button !== 0 || narrow()) return;
    e.preventDefault(); deps.closeAllMenus();
    drag = {id:e.pointerId, x:e.clientX, width:$('aside').getBoundingClientRect().width, preferred};
    handle.setPointerCapture(e.pointerId); document.body.classList.add('sidebar-resizing');
  });
  handle.addEventListener('pointermove', e => {
    if (!drag || drag.id !== e.pointerId) return;
    preferred = clamp(drag.width + e.clientX - drag.x); apply();
  });
  handle.addEventListener('pointerup', e => { if (drag?.id === e.pointerId) finish(); });
  handle.addEventListener('pointercancel', () => finish(true));
  handle.addEventListener('lostpointercapture', () => finish(true));
  handle.addEventListener('dblclick', () => { if (!narrow()) { preferred = initial; apply(); save(); } });
  handle.addEventListener('keydown', e => {
    if (narrow() || !['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) return;
    e.preventDefault();
    preferred = e.key === 'Home' ? min : e.key === 'End' ? limit()
      : clamp($('aside').getBoundingClientRect().width + (e.key === 'ArrowLeft' ? -10 : 10));
    apply(); save();
  });
  document.addEventListener('keydown', e => {
    if (drag && e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); finish(true); }
  }, true);
  window.addEventListener('resize', () => { if (narrow()) finish(true); apply(true); closeSelectionMenu(); });
  window.addEventListener('blur', () => finish(true));
  new MutationObserver(() => { if (document.body.classList.contains('sidebar-collapsed')) finish(true); })
    .observe(document.body, {attributes:true, attributeFilter:['class']});
  apply(true);
}

function selectionFor(target) {
  const input = target.closest('input,textarea');
  if (input) {
    if (input.type === 'password' || input.selectionStart === null || input.selectionStart === input.selectionEnd) return null;
    return {input, start:input.selectionStart, end:input.selectionEnd,
      text:input.value.slice(input.selectionStart,input.selectionEnd), focus:input};
  }
  const area = target.closest('#chat-area,.changes-drawer'), selection = getSelection();
  if (!area || !selection?.rangeCount || selection.isCollapsed || !selection.toString()
    || !area.contains(selection.anchorNode) || !area.contains(selection.focusNode)) return null;
  const range = selection.getRangeAt(0).cloneRange();
  if (!range.intersectsNode(target)) return null;
  return {range, text:selection.toString(), focus:document.activeElement};
}
function restoreSelection(snapshot, focus = false) {
  if (!snapshot) return;
  if (focus && snapshot.focus?.isConnected && !snapshot.focus.closest('[inert]')) snapshot.focus.focus({preventScroll:true});
  if (snapshot.input?.isConnected) snapshot.input.setSelectionRange(snapshot.start,snapshot.end);
  else if (snapshot.range?.startContainer.isConnected && snapshot.range?.endContainer.isConnected) {
    const selection = getSelection(); selection.removeAllRanges(); selection.addRange(snapshot.range);
  }
}
function closeSelectionMenu(restore = false) {
  const snapshot = selectedCopy; selectedCopy = null; copyGeneration++;
  $('selection-menu')?.classList.remove('open');
  if (restore) restoreSelection(snapshot, true);
}
function placeSelectionMenu(x, y) {
  const menu = $('selection-menu'), rect = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(x, innerWidth - rect.width - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(y, innerHeight - rect.height - 8)) + 'px';
}
function showSelectionMenu(target, x, y, keyboard = false) {
  const snapshot = selectionFor(target);
  deps.closeAllMenus(); window.CodeyProviderUI.closeMenu();
  if (!snapshot) return false;
  const menu = $('selection-menu'), copy = $('selection-copy');
  (target.closest('dialog[open]') || document.body).appendChild(menu);
  selectedCopy = snapshot; copyGeneration++;
  copy.textContent = 'Copy'; copy.disabled = false;
  $('selection-copy-status').hidden = true; $('selection-copy-status').textContent = '';
  menu.classList.toggle('keyboard-open', keyboard);
  menu.classList.add('open'); placeSelectionMenu(x,y);
  copy.focus({preventScroll:true});
  return true;
}
function initSelectionMenu() {
  const menu = $('selection-menu'), copy = $('selection-copy');
  document.addEventListener('contextmenu', e => {
    if (menu.contains(e.target)) { e.preventDefault(); return; }
    if (showSelectionMenu(e.target,e.clientX,e.clientY)) { e.preventDefault(); e.stopPropagation(); }
  });
  menu.addEventListener('pointerdown', e => e.preventDefault());
  copy.onclick = async () => {
    const snapshot = selectedCopy, generation = copyGeneration;
    if (!snapshot || copy.disabled) return;
    copy.disabled = true;
    let ok = false;
    try { ok = await window.CodeyRender.copyText(snapshot.text); } catch {}
    if (generation !== copyGeneration) return;
    restoreSelection(snapshot);
    if (ok) {
      copy.textContent = 'Copied';
      setTimeout(() => { if (generation === copyGeneration) closeSelectionMenu(true); }, 800);
    } else {
      copy.disabled = false;
      $('selection-copy-status').textContent = 'Could not copy'; $('selection-copy-status').hidden = false;
      const rect = menu.getBoundingClientRect(); placeSelectionMenu(rect.left,rect.top); copy.focus({preventScroll:true});
    }
  };
  window.addEventListener('keydown', e => {
    if (menu.classList.contains('open') && ['ArrowDown','ArrowUp','Home','End'].includes(e.key)) menu.classList.add('keyboard-open');
    if (menu.classList.contains('open') && ['Escape','Tab'].includes(e.key)) {
      closeSelectionMenu(true);
      if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); }
    } else if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
      const target = document.activeElement, snapshot = selectionFor(target);
      if (!snapshot) return;
      const rect = snapshot.range ? snapshot.range.getBoundingClientRect() : target.getBoundingClientRect();
      e.preventDefault(); e.stopImmediatePropagation(); showSelectionMenu(target,rect.left,rect.bottom,true);
    }
  }, true);
  document.addEventListener('scroll', () => closeSelectionMenu(), true);
  window.addEventListener('blur', () => closeSelectionMenu());
}

function init(nextDeps) {
  deps = nextDeps;
  const narrow = matchMedia('(max-width: 720px)');
  const collapseOnNarrow = () => {
    if (narrow.matches) { document.body.classList.add('sidebar-collapsed'); $('aside').inert = true; $('show-sidebar').style.display = ''; }
  };
  collapseOnNarrow(); narrow.addEventListener('change', collapseOnNarrow);
  const search = $('chat-search'), searchToggle = $('chat-search-toggle'), searchClear = $('chat-search-clear');
  const setSearchOpen = open => {
    search.hidden = !open;
    searchToggle.hidden = open;
    searchToggle.setAttribute('aria-expanded', String(open));
    searchClear.hidden = !open || !search.value;
  };
  searchToggle.onclick = () => { setSearchOpen(true); search.focus(); };
  searchClear.onclick = () => {
    search.value = ''; search.dispatchEvent(new Event('input'));
    search.focus();
  };
  search.addEventListener('blur', () => { if (!query()) setSearchOpen(false); });
  $('chat-search').addEventListener('input', () => {
    deps.renderSidebar();
  });
  $('chat-search').addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation();
      search.value = ''; search.dispatchEvent(new Event('input'));
      setSearchOpen(false); searchToggle.focus();
    }
  });
  $('chat-area').addEventListener('scroll', () => { captureView(); updateLatest(); });
  $('back-to-latest').onclick = () => scrollChat(true);
  document.addEventListener('keydown', e => {
    if (e.isComposing || e.keyCode === 229) return;
    const menu = e.target.closest('.ctx-menu.open');
    if (menu && ['ArrowDown','ArrowUp','Home','End','Escape'].includes(e.key)) {
      e.preventDefault(); e.stopPropagation();
      if (e.key === 'Escape') { menusClosed(true); deps.closeAllMenus(); return; }
      const rows = Array.from(menu.querySelectorAll('button:not(:disabled)'));
      const index = rows.indexOf(document.activeElement);
      const next = e.key === 'Home' ? 0 : e.key === 'End' ? rows.length - 1 : (index + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
      rows[next]?.focus();
      return;
    }
    const drawer = document.querySelector('.changes-drawer.open');
    if (drawer && e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation(); deps.closeOtherDrawers();
    } else if (drawer && e.key === 'Tab') {
      const rows = Array.from(drawer.querySelectorAll('button:not(:disabled), a[href], input, [tabindex="0"]')).filter(el => el.getClientRects().length);
      const first = rows[0], last = rows[rows.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    }
  }, true);
  $('chat').addEventListener('click', e => {
    const path = e.target.closest('.cf-path');
    if (path) openFile(path.textContent, path.closest('[data-project]')?.dataset.project);
  });
  document.querySelectorAll('.changes-drawer').forEach(el => { el.inert = !el.classList.contains('open'); });
  initSidebarResize(); initSelectionMenu();
}

window.CodeyConversationUI = { init, renderChat, scrollChat, captureView, isFollowing, updateNotice,
  matchesSession, matchesProject, appendGroupLabel, updateSearch, isSearching: () => !!query(), forget, openFile, menuOpened, menusClosed };
})();
