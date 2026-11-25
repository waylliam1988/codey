export function mountSidebarResize(
  handle: HTMLElement,
  sidebar: HTMLElement,
  closeAllMenus: () => void,
  onResize: () => void,
): void {
  const root = document.documentElement;
  const tokens = getComputedStyle(root),
    token = (name: string): number => parseFloat(tokens.getPropertyValue(name));
  const initial = token('--sidebar-width'),
    min = token('--sidebar-min-width'),
    max = token('--sidebar-max-width');
  const contentMin = token('--sidebar-content-min-width'),
    narrow = () => innerWidth <= 720;
  let preferred = initial;
  let drag: { id: number; x: number; width: number; preferred: number } | null = null;
  try {
    const rawSaved = localStorage.getItem('codey:sidebar-width');
    const saved: unknown = rawSaved ? JSON.parse(rawSaved) : null;
    if (typeof saved === 'number' && Number.isFinite(saved)) preferred = Math.max(min, Math.min(max, saved));
  } catch {}
  const limit = () => Math.max(min, Math.min(max, innerWidth - contentMin));
  const clamp = (value: number): number => Math.max(min, Math.min(limit(), value));
  function apply(snap = false) {
    if (snap) sidebar.style.transition = 'none';
    const size = narrow() ? Math.min(initial, innerWidth - 32) : clamp(preferred);
    root.style.setProperty('--sidebar-width', size + 'px');
    handle.setAttribute('aria-valuemin', String(min));
    handle.setAttribute('aria-valuemax', String(limit()));
    handle.setAttribute('aria-valuenow', String(size));
    if (snap) {
      sidebar.getBoundingClientRect();
      sidebar.style.removeProperty('transition');
    }
  }
  function save() {
    try {
      localStorage.setItem('codey:sidebar-width', JSON.stringify(preferred));
    } catch {}
  }
  function finish(cancel = false) {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (cancel) preferred = previous.preferred;
    document.body.classList.remove('sidebar-resizing');
    if (handle.hasPointerCapture(previous.id)) handle.releasePointerCapture(previous.id);
    apply();
    if (!cancel) save();
  }
  handle.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || narrow()) return;
    e.preventDefault();
    closeAllMenus();
    drag = { id: e.pointerId, x: e.clientX, width: sidebar.getBoundingClientRect().width, preferred };
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add('sidebar-resizing');
  });
  handle.addEventListener('pointermove', (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    preferred = clamp(drag.width + e.clientX - drag.x);
    apply();
  });
  handle.addEventListener('pointerup', (e) => {
    if (drag?.id === e.pointerId) finish();
  });
  handle.addEventListener('pointercancel', () => finish(true));
  handle.addEventListener('lostpointercapture', () => finish(true));
  handle.addEventListener('dblclick', () => {
    if (!narrow()) {
      preferred = initial;
      apply();
      save();
    }
  });
  handle.addEventListener('keydown', (e) => {
    if (narrow() || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    e.preventDefault();
    preferred =
      e.key === 'Home'
        ? min
        : e.key === 'End'
          ? limit()
          : clamp(sidebar.getBoundingClientRect().width + (e.key === 'ArrowLeft' ? -10 : 10));
    apply();
    save();
  });
  document.addEventListener(
    'keydown',
    (e) => {
      if (drag && e.key === 'Escape') {
        e.preventDefault();
        e.stopImmediatePropagation();
        finish(true);
      }
    },
    true,
  );
  window.addEventListener('resize', () => {
    if (narrow()) finish(true);
    apply(true);
    onResize();
  });
  window.addEventListener('blur', () => finish(true));
  new MutationObserver(() => {
    if (document.body.classList.contains('sidebar-collapsed')) finish(true);
  }).observe(document.body, { attributes: true, attributeFilter: ['class'] });
  apply(true);
}
