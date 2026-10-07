/* A quiet index of user questions; native chat selection and drafts stay untouched. */
(function () {
'use strict';
let deps, area, chat, nav, preview;
let entries = [], sessionId = '', current = 0, hovered = -1, focused = -1;
let frame = 0, previewTimer = 0, previewSuppressed = false, returnFocus = null;
let bounds = null;
const STEP = 10, MIN_TURNS = 8, MAX_VISIBLE = 48;
const selected = () => hovered >= 0 ? hovered : focused;
const plain = node => (node?.textContent || '').replace(/\s+/g, ' ').trim();

function closePreview() {
  clearTimeout(previewTimer); previewTimer = 0;
  preview.hidden = true;
  entries.forEach(entry => entry.button.removeAttribute('aria-describedby'));
}
function dismiss() {
  hovered = focused = -1; previewSuppressed = false;
  closePreview(); paint();
}
function paint() {
  const active = selected();
  nav.classList.toggle('selecting', active >= 0);
  entries.forEach((entry, index) => {
    const button = entry.button, distance = Math.abs(index - active);
    if (index === current) button.setAttribute('aria-current', 'location');
    else button.removeAttribute('aria-current');
    button.classList.toggle('selected', index === active);
    button.classList.toggle('near', active >= 0 && distance === 1);
    button.tabIndex = index === (focused >= 0 ? focused : current) ? 0 : -1;
    if (active < 0) button.firstElementChild.style.removeProperty('width');
    else button.firstElementChild.style.width = (distance === 0 ? 28 : distance === 1 ? 22
      : distance === 2 ? 15 : distance === 3 ? 9 : 6) + 'px';
  });
}
function answerFor(entry) {
  for (let node = entry.anchor.nextElementSibling; node && !node.matches('.msg.user'); node = node.nextElementSibling) {
    if (node.matches('.msg.asst')) return plain(node.querySelector('.body')).slice(0, 500);
  }
  return '';
}
function showPreview() {
  const index = selected(), entry = entries[index];
  if (!entry || nav.hidden || entry.button.hidden || previewSuppressed) return;
  const button = entry.button, rect = button.getBoundingClientRect();
  preview.querySelector('.conversation-preview-label').textContent = 'Question ' + (index + 1);
  preview.querySelector('.conversation-preview-title').textContent = plain(entry.anchor.querySelector('.body'));
  const body = preview.querySelector('.conversation-preview-body');
  body.textContent = answerFor(entry); body.hidden = !body.textContent;
  preview.hidden = false;
  preview.style.left = Math.max(bounds.left + 8, Math.min(rect.right + 10, bounds.right - preview.offsetWidth - 12)) + 'px';
  preview.style.top = Math.max(bounds.top + 12, Math.min(rect.top - 25, bounds.bottom - preview.offsetHeight - 12)) + 'px';
  button.setAttribute('aria-describedby', preview.id);
}
function requestPreview() {
  closePreview();
  if (selected() >= 0 && !previewSuppressed) previewTimer = setTimeout(showPreview, 220);
}
function selectPointer(index) {
  if (hovered === index && !previewSuppressed) return;
  hovered = index; previewSuppressed = false;
  paint(); requestPreview();
}
function jump(index) {
  const entry = entries[index];
  if (!entry?.anchor.isConnected || deps.getActiveId() !== sessionId) return;
  closePreview(); previewSuppressed = true;
  const rect = area.getBoundingClientRect();
  area.scrollTop += entry.anchor.getBoundingClientRect().top - rect.top - 24;
  schedule();
}
function makeEntry(anchor) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'conversation-tick'; button.tabIndex = -1;
  const line = document.createElement('span'); line.setAttribute('aria-hidden', 'true'); button.append(line);
  button.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    if (nav.contains(document.activeElement)) { document.activeElement.blur(); focused = -1; }
  });
  button.addEventListener('click', () => jump(entries.findIndex(entry => entry.button === button)));
  return {anchor, button};
}
function reconcile() {
  const id = deps.getActiveId(), changed = id !== sessionId;
  if (changed) { sessionId = id; dismiss(); }
  const old = new Map(entries.map(entry => [entry.anchor, entry]));
  const focusIndex = entries.findIndex(entry => entry.button === document.activeElement);
  const anchors = Array.from(chat.children).filter(node => node.matches('.msg.user'));
  const same = anchors.length === entries.length && anchors.every((anchor, i) => anchor === entries[i].anchor);
  if (!same) {
    entries = anchors.map(anchor => old.get(anchor) || makeEntry(anchor));
    const buttons = new Set(entries.map(entry => entry.button));
    for (const button of Array.from(nav.children)) if (!buttons.has(button)) button.remove();
    entries.forEach((entry, index) => {
      if (nav.children[index] !== entry.button) nav.insertBefore(entry.button, nav.children[index] || null);
    });
    if (!changed && focusIndex >= 0 && entries[focusIndex] && !nav.contains(document.activeElement)) {
      entries[focusIndex].button.focus({preventScroll:true});
    }
    if (hovered >= entries.length) hovered = -1;
    if (focused >= entries.length) focused = -1;
    closePreview();
  }
  entries.forEach((entry, index) => {
    const label = 'Jump to question ' + (index + 1) + ': ' + plain(entry.anchor.querySelector('.body')).slice(0, 240);
    if (entry.button.getAttribute('aria-label') !== label) entry.button.setAttribute('aria-label', label);
  });
}
function availableBounds() {
  const rect = area.getBoundingClientRect();
  let right = rect.right;
  document.querySelectorAll('.changes-drawer.open').forEach(drawer => {
    // Drawers overlay rather than resize the chat. Count their final occupied width during animation too.
    right = Math.min(right, innerWidth - drawer.getBoundingClientRect().width);
  });
  return {left:rect.left, right, top:rect.top, bottom:rect.bottom, width:right - rect.left, height:rect.height};
}
function update() {
  frame = 0; reconcile(); bounds = availableBounds();
  const enough = bounds.width >= 920 && bounds.height >= 360 && entries.length >= MIN_TURNS
    && area.scrollHeight > area.clientHeight && !document.querySelector('dialog[open]');
  if (!enough) {
    const hadFocus = nav.contains(document.activeElement);
    nav.hidden = true; dismiss();
    if (hadFocus) (returnFocus?.isConnected && !returnFocus.closest('[inert]') ? returnFocus : document.getElementById('task')).focus({preventScroll:true});
    return;
  }
  nav.hidden = false;
  current = 0;
  for (let index = 0; index < entries.length; index++) {
    if (entries[index].anchor.getBoundingClientRect().top > bounds.top + 55) break;
    current = index;
  }
  if (area.scrollHeight - area.scrollTop - area.clientHeight < 2) current = entries.length - 1;
  const capacity = Math.min(MAX_VISIBLE, Math.floor((bounds.height - 48) / STEP));
  const center = selected() >= 0 ? selected() : current;
  const start = Math.max(0, Math.min(entries.length - capacity, center - Math.floor(capacity / 2)));
  const count = Math.min(entries.length, capacity);
  entries.forEach((entry, index) => { entry.button.hidden = index < start || index >= start + count; });
  nav.style.left = (bounds.left + 14) + 'px';
  nav.style.top = (bounds.top + (bounds.height - count * STEP) / 2) + 'px';
  paint();
  if (!preview.hidden) showPreview();
}
function schedule() { if (!frame) frame = requestAnimationFrame(update); }
function keydown(event) {
  if (event.isComposing) return;
  const index = entries.findIndex(entry => entry.button === event.target);
  if (index < 0) return;
  const keys = ['ArrowUp', 'ArrowDown', 'Home', 'End'];
  if (!keys.includes(event.key)) return;
  event.preventDefault(); event.stopPropagation();
  hovered = -1; previewSuppressed = false;
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? entries.length - 1
    : Math.max(0, Math.min(entries.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
  focused = next; update(); entries[next].button.focus({preventScroll:true}); paint(); requestPreview();
}
function init(nextDeps) {
  if (nav) return;
  deps = nextDeps; area = document.getElementById('chat-area'); chat = document.getElementById('chat');
  nav = document.createElement('nav'); nav.id = 'conversation-nav'; nav.className = 'conversation-nav'; nav.hidden = true;
  nav.setAttribute('aria-label', 'Conversation navigation');
  preview = document.createElement('div'); preview.id = 'conversation-preview'; preview.className = 'conversation-preview';
  preview.setAttribute('role', 'tooltip'); preview.hidden = true;
  preview.innerHTML = '<div class="conversation-preview-label"></div><div class="conversation-preview-title"></div><div class="conversation-preview-body"></div>';
  document.querySelector('main').append(nav, preview);
  nav.addEventListener('pointermove', event => {
    if (event.pointerType === 'touch') return;
    const button = event.target.closest('.conversation-tick');
    if (button) selectPointer(entries.findIndex(entry => entry.button === button));
  });
  nav.addEventListener('pointerleave', () => { hovered = -1; previewSuppressed = false; closePreview(); schedule(); });
  nav.addEventListener('focusin', event => {
    if (!nav.contains(event.relatedTarget)) returnFocus = event.relatedTarget;
    focused = entries.findIndex(entry => entry.button === event.target);
    previewSuppressed = false; paint(); requestPreview();
  });
  nav.addEventListener('focusout', event => {
    if (nav.contains(event.relatedTarget)) return;
    focused = -1; closePreview(); schedule();
  });
  nav.addEventListener('keydown', keydown);
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || nav.hidden || selected() < 0) return;
    event.preventDefault(); event.stopImmediatePropagation();
    const hadFocus = nav.contains(document.activeElement);
    dismiss();
    if (hadFocus) (returnFocus?.isConnected ? returnFocus : document.getElementById('task')).focus({preventScroll:true});
  }, true);
  area.addEventListener('scroll', () => { closePreview(); previewSuppressed = true; schedule(); }, {passive:true});
  window.addEventListener('resize', () => { closePreview(); schedule(); });
  window.addEventListener('blur', dismiss);
  new MutationObserver(schedule).observe(chat, {childList:true, subtree:true, characterData:true});
  new ResizeObserver(schedule).observe(area);
  new ResizeObserver(schedule).observe(chat);
  document.querySelectorAll('.changes-drawer, dialog').forEach(element => {
    new MutationObserver(() => { dismiss(); schedule(); }).observe(element, {attributes:true, attributeFilter:['class', 'open']});
  });
  schedule();
}
window.CodeyConversationNav = {init};
})();
