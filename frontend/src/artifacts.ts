export interface ArtifactAction {
  label: string;
  action: () => unknown;
}

export function artifactRow(
  title: string,
  type: string,
  open: () => unknown,
  actions: ArtifactAction[] = [],
): HTMLElement {
  const row = document.createElement('div');
  row.className = 'artifact-row';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'artifact-open';
  button.onclick = open;
  const icon = document.createElement('span');
  icon.className = 'artifact-icon';
  icon.setAttribute('aria-hidden', 'true');
  const name = document.createElement('span');
  name.className = 'artifact-title';
  name.textContent = title;
  const meta = document.createElement('small');
  meta.textContent = type;
  name.append(meta);
  button.append(icon, name);
  row.append(button);
  const label = document.createElement('button');
  label.type = 'button';
  label.className = 'link-btn';
  label.textContent = 'Open';
  label.onclick = open;
  row.append(label);
  if (actions.length) {
    const menu = document.createElement('details');
    menu.className = 'artifact-more';
    const summary = document.createElement('summary');
    summary.textContent = '…';
    summary.setAttribute('aria-label', 'Research actions');
    const items = document.createElement('div');
    items.className = 'artifact-menu';
    for (const action of actions) {
      const item = document.createElement('button');
      item.type = 'button';
      item.textContent = action.label;
      item.onclick = () => {
        menu.open = false;
        action.action();
      };
      items.append(item);
    }
    menu.append(summary, items);
    row.append(menu);
  }
  return row;
}

export function decorateDeliverables(host: HTMLElement): void {
  for (const heading of host.querySelectorAll('p,h2,h3,h4')) {
    if (
      !/^(?:\u4ea7\u51fa\u6587\u4ef6|\u8f93\u51fa\u6587\u4ef6|\u4ea4\u4ed8\u6587\u4ef6|Outputs|Deliverables|Produced files)[:：]?$/i.test(
        heading.textContent?.trim() || '',
      )
    )
      continue;
    const list = heading.nextElementSibling;
    if (!list?.matches('ul,ol')) continue;
    for (const item of list.querySelectorAll(':scope > li')) {
      const links = item.querySelectorAll<HTMLElement>('[data-document-path]');
      if (links.length !== 1) continue;
      const link = links[0],
        path = link.dataset.documentPath || '';
      const title = path.split(/[\\/]/).pop() || path;
      const description = (item.textContent || '')
        .replace(link.textContent || '', '')
        .replace(/^\s*[—–\-:：]+\s*/, '')
        .trim();
      const format = /\.md$/i.test(path)
        ? 'Markdown'
        : /\.py$/i.test(path)
          ? 'Python'
          : /\.json$/i.test(path)
            ? 'JSON'
            : 'File';
      const row = artifactRow(title, format + (description ? ' · ' + description : ''), () => link.click());
      // Route through the same delegated document action, without nested buttons.
      const open = row.querySelector<HTMLElement>('.artifact-open')!;
      open.dataset.documentPath = path;
      if (link.dataset.documentLine) open.dataset.documentLine = link.dataset.documentLine;
      open.onclick = null;
      const action = row.querySelector<HTMLButtonElement>('.link-btn')!;
      action.onclick = null;
      action.dataset.documentPath = path;
      item.replaceChildren(row);
      item.classList.add('artifact-item');
    }
    list.classList.add('artifact-list');
  }
}
