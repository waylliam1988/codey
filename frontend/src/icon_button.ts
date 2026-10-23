import { messageIcons } from './message_icons';
export type Icon =
  | 'copy'
  | 'edit'
  | 'reload'
  | 'close'
  | 'expand'
  | 'dock'
  | 'workspace'
  | 'branch'
  | 'chevron-down'
  | 'trash';
const paths: Record<Icon, string> = {
  'chevron-down': 'm6 9 6 6 6-6',
  trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  copy: '',
  edit: '',
  reload: 'M20 7v5h-5M20 12a8 8 0 1 0-2 5M20 7l-3-3',
  close: 'm6 6 12 12M18 6 6 18',
  expand: 'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5',
  dock: 'M3 3h18v18H3ZM14 3v18',
  workspace: 'M3 3h18v18H3ZM14 3v18M14 9h7',
  branch: 'M6 3v12a3 3 0 0 0 3 3h9M6 9a9 9 0 0 0 9-9',
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
  if (messageIcons[icon]) {
    svg.classList.add('filled-icon');
    svg.setAttribute('viewBox', messageIcons[icon].viewBox);
    svg.setAttribute('fill', 'currentColor');
    svg.setAttribute('stroke', 'none');
    svg.innerHTML = messageIcons[icon].body;
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
