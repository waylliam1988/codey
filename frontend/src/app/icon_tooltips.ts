export function mountIconTooltips(): void {
  const tooltip = document.createElement('div');
  tooltip.className = 'control-tooltip';
  tooltip.hidden = true;
  tooltip.setAttribute('aria-hidden', 'true');
  document.body.append(tooltip);
  let active: HTMLElement | null = null;
  let timer: ReturnType<typeof setTimeout>;
  const hide = () => {
    clearTimeout(timer);
    active = null;
    tooltip.hidden = true;
  };
  const show = (event: Event) => {
    const target =
      event.target instanceof Element
        ? event.target.closest<HTMLElement>('button.icon-action[aria-label],button[data-tooltip][aria-label]')
        : null;
    if (!target || target === active || target.matches(':disabled')) return;
    hide();
    active = target;
    if (target.title) target.removeAttribute('title');
    timer = setTimeout(() => {
      if (!target.isConnected || !target.getClientRects().length) return;
      const shortcut = target.dataset.tooltip;
      tooltip.textContent = [target.getAttribute('aria-label'), shortcut].filter(Boolean).join(' · ');
      tooltip.hidden = false;
      const rect = target.getBoundingClientRect();
      tooltip.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - tooltip.clientWidth - 8))}px`;
      tooltip.style.top = `${rect.bottom + tooltip.clientHeight + 6 > innerHeight ? rect.top - tooltip.clientHeight - 6 : rect.bottom + 6}px`;
    }, 450);
  };
  document.addEventListener('pointerover', show);
  document.addEventListener('focusin', show);
  document.addEventListener('pointerout', (event) => {
    if (!active?.contains(event.relatedTarget as Node | null)) hide();
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('pointerdown', hide);
  document.addEventListener('scroll', hide, true);
}
