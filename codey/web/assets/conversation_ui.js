/* Conversation continuity, quiet status, search, and keyboard navigation. */
(function () {
'use strict';
let deps = null;
let shownId = '';
const views = new Map();
let menuTrigger = null;

function $(id) { return document.getElementById(id); }
function query() { return $('chat-search').value.trim().toLocaleLowerCase(); }
function matchesSession(s) {
  const p = deps.sessionProject(s);
  return [s.title, p && p.name].filter(Boolean).join(' ').toLocaleLowerCase().includes(query());
}
function matchesProject(p) { return !query() || deps.getSessions().some(s => s.projectId === p.id && matchesSession(s)); }
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
  if (changing) captureView();
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

function init(nextDeps) {
  deps = nextDeps;
  const narrow = matchMedia('(max-width: 720px)');
  const collapseOnNarrow = () => {
    if (narrow.matches) { document.body.classList.add('sidebar-collapsed'); $('aside').inert = true; $('show-sidebar').style.display = ''; }
  };
  collapseOnNarrow(); narrow.addEventListener('change', collapseOnNarrow);
  const search = $('chat-search'), searchToggle = $('chat-search-toggle');
  const setSearchOpen = open => {
    search.hidden = !open;
    searchToggle.hidden = open;
    searchToggle.setAttribute('aria-expanded', String(open));
  };
  searchToggle.onclick = () => { setSearchOpen(true); search.focus(); };
  search.addEventListener('blur', () => { if (!query()) setSearchOpen(false); });
  $('chat-search').addEventListener('input', () => {
    deps.renderSidebar();
    $('search-empty').hidden = !query() || deps.getSessions().some(matchesSession);
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
}

window.CodeyConversationUI = { init, renderChat, scrollChat, captureView, isFollowing, updateNotice,
  matchesSession, matchesProject, isSearching: () => !!query(), forget, openFile, menuOpened, menusClosed };
})();
