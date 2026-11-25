export function nestedOwnsScroll(target: EventTarget | null, outer: HTMLElement, delta: number): boolean {
  for (let node = target instanceof Element ? target : null; node && node !== outer; node = node.parentElement) {
    if (!(node instanceof HTMLElement) || !node.matches('[data-scrollable], .shell-output, .md-code, .md-table-wrap'))
      continue;
    const maximum = node.scrollHeight - node.clientHeight;
    if (maximum > 1 && (delta < 0 ? node.scrollTop > 0 : node.scrollTop < maximum - 1)) return true;
  }
  return false;
}
