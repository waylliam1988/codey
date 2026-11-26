import { messageIcons } from '../conversation/message_icons';
export type Icon =
  | 'arrow-left'
  | 'arrow-down'
  | 'copy'
  | 'edit'
  | 'reload'
  | 'close'
  | 'expand'
  | 'dock'
  | 'workspace'
  | 'sidebar'
  | 'folder'
  | 'globe'
  | 'file'
  | 'search'
  | 'terminal'
  | 'branch'
  | 'chevron-down'
  | 'trash'
  | 'plus';
const paths: Record<Icon, string> = {
  plus: 'M12 5v14M5 12h14',
  sidebar:
    'M5 2.5h14A2.5 2.5 0 0 1 21.5 5v14a2.5 2.5 0 0 1-2.5 2.5H5A2.5 2.5 0 0 1 2.5 19V5A2.5 2.5 0 0 1 5 2.5ZM9.5 2.5v19',
  folder: 'M3 7h6l2 2h10v10H3z',
  globe: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3a14 14 0 0 0 0 18m0-18a14 14 0 0 1 0 18',
  file: 'M6 3h8l4 4v14H6zM14 3v5h4M9 12h6m-6 4h6',
  search: 'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Zm-2 5 5 5',
  terminal: 'M3 4h18v16H3zM7 9l3 3-3 3m6 0h4',
  'arrow-left': 'M19 12H5m7-7-7 7 7 7',
  'arrow-down': 'M12 5v14m-7-7 7 7 7-7',
  'chevron-down': 'm6 9 6 6 6-6',
  trash: 'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  copy: '',
  edit: '',
  reload: 'M20 7v5h-5M20 12a8 8 0 1 0-2 5M20 7l-3-3',
  close: 'm6 6 12 12M18 6 6 18',
  expand: 'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5',
  dock: 'M3 3h18v18H3ZM14 3v18',
  workspace:
    'M5 2.5h14A2.5 2.5 0 0 1 21.5 5v14a2.5 2.5 0 0 1-2.5 2.5H5A2.5 2.5 0 0 1 2.5 19V5A2.5 2.5 0 0 1 5 2.5ZM14.5 2.5v19',
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
    if (icon === 'workspace') {
      const pane = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      pane.classList.add('workspace-icon-pane');
      pane.setAttribute('stroke', 'none');
      pane.setAttribute('d', 'M14.5 2.5H19A2.5 2.5 0 0 1 21.5 5v14a2.5 2.5 0 0 1-2.5 2.5h-4.5Z');
      svg.append(pane);
    }
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
