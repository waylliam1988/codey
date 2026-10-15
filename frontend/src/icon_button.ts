type Icon = 'copy' | 'reload';
const paths: Record<Icon, string> = {
  copy: 'M9 5V3h12v12h-2M3 7h14v14H3z',
  reload: 'M20 7v5h-5M20 12a8 8 0 1 0-2 5M20 7l-3-3',
};

export function iconButton(label: string, icon: Icon, action: () => void): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'icon-action';
  button.title = label;
  button.setAttribute('aria-label', label);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.5');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', paths[icon]);
  svg.append(path);
  button.append(svg);
  button.onclick = action;
  return button;
}
