export type Icon = 'copy' | 'reload' | 'close';
const paths: Record<Icon, string> = {
  copy: '',
  reload: 'M20 7v5h-5M20 12a8 8 0 1 0-2 5M20 7l-3-3',
  close: 'm6 6 12 12M18 6 6 18',
};

export function createIconSvg(icon: Icon, className = ''): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.5');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  if (className) svg.classList.add(className);
  if (icon === 'copy') {
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', '8');
    rect.setAttribute('y', '8');
    rect.setAttribute('width', '11');
    rect.setAttribute('height', '11');
    rect.setAttribute('rx', '2');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M5 15V7a2 2 0 0 1 2-2h8');
    svg.append(rect, path);
  } else {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', paths[icon]);
    svg.append(path);
  }
  return svg;
}

export function iconButton(label: string, icon: Icon, action: () => void): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'icon-action';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.append(createIconSvg(icon));
  button.onclick = action;
  return button;
}
