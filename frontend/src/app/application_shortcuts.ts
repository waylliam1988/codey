import { ConversationUI } from '../conversation/conversation_ui';
import { ProviderUI } from '../providers/provider_ui';
import { createIconSvg } from '../render/icon_button';
import { showCommandPermissions } from '../runs/command_permissions';
import type { SessionStateStore } from '../state/session_state_store';
import type { PanelId, Workspace } from '../workspace/workspace';
import { CommandPalette } from './command_palette';
import { element } from './element';
import { mountIconTooltips } from './icon_tooltips';
import type { createProjectFolderPicker } from './project_folder';

const panels: Array<[PanelId, string]> = [
  ['files', 'Files'],
  ['changes', 'Changes'],
  ['research', 'Research'],
  ['context', 'Local context'],
  ['review', 'Recorded review'],
  ['outputs', 'Command output'],
  ['history', 'Branches and workflow'],
];

export function mountApplicationShortcuts(
  sessions: SessionStateStore,
  workspace: Workspace,
  folder: ReturnType<typeof createProjectFolderPicker>,
  view: { closeMenus(): void; closeDrawers(): void },
): void {
  const palette = new CommandPalette((query) => [
    ...panels.map(([id, label]) => ({ id, label, run: () => workspace.select(id) })),
    { id: 'search-conversation', label: 'Search this conversation', run: ConversationUI.searchHistory },
    { id: 'new-chat', label: 'New chat', run: () => sessions.newSession() },
    { id: 'settings', label: 'Settings', run: () => element('btn-settings').click() },
    { id: 'sidebar', label: 'Toggle left sidebar', run: () => setSidebar(!collapsed()) },
    { id: 'workspace', label: 'Toggle right workspace', run: () => workspace.toggleButton.click() },
    {
      id: 'command-permissions',
      label: 'Manage saved permissions…',
      run: () => void showCommandPermissions('', '', true),
    },
    ...(query
      ? sessions.projects.map((project) => ({
          id: `project-${project.id}`,
          label: `Project · ${project.name}`,
          terms: project.path,
          run: () => sessions.openProject(project.id),
        }))
      : []),
    ...(query
      ? sessions.sessions.map((session) => ({
          id: `chat-${session.id}`,
          label: `Chat · ${session.title}`,
          terms: sessions.sessionProject(session)?.name,
          run: () => sessions.switchSession(session.id),
        }))
      : []),
  ]);
  document.body.append(palette.element);
  element('btn-new-chat').onclick = () => sessions.newSession();
  element('btn-add-project').onclick = folder.pickProject;
  const hide = element('toggle-sidebar');
  const show = element('show-sidebar');
  const aside = element('aside');
  const search = element('chat-search-toggle');
  const hint = document.createElement('span');
  hint.className = 'kbd sidebar-search-shortcut';
  hint.textContent = 'Ctrl+K';
  search.append(hint);
  const desktop = new URLSearchParams(location.hash.slice(1)).has('codey_startup') || 'pywebview' in window;
  for (const [button, label] of [
    [hide, 'Hide left sidebar'],
    [show, 'Show left sidebar'],
  ] as const) {
    button.replaceChildren(createIconSvg('sidebar'));
    button.setAttribute('aria-label', label);
    button.dataset.tooltip = desktop ? 'Ctrl+B' : 'Ctrl+Alt+B';
  }
  workspace.toggleButton.dataset.tooltip = desktop ? 'Ctrl+J' : 'Ctrl+Alt+J';
  const narrow = matchMedia('(max-width: 720px)');
  const collapsed = () => document.body.classList.contains('sidebar-collapsed');
  function setSidebar(hidden: boolean): void {
    document.body.classList.toggle('sidebar-collapsed', hidden);
    aside.inert = hidden;
    show.style.display = hidden ? '' : 'none';
    (hidden ? show : hide).focus({ preventScroll: true });
  }
  hide.onclick = () => setSidebar(true);
  show.onclick = () => setSidebar(false);
  element('sidebar-backdrop').onclick = hide.onclick;
  mountIconTooltips();
  document.addEventListener('keydown', (event) => {
    if (event.defaultPrevented || event.isComposing || event.keyCode === 229 || document.querySelector('dialog[open]'))
      return;
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest('.rename-input')) return;
    const key = event.key.toLowerCase();
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey) {
      if (key === 'k' && !event.altKey) palette.open();
      else if (key === 'n' && !event.altKey) sessions.newSession();
      else if (key === 'b' && event.altKey !== desktop) setSidebar(!collapsed());
      else if (key === 'j' && event.altKey !== desktop) workspace.toggleButton.click();
      else return;
      event.preventDefault();
    } else if (key === 'escape') {
      if (narrow.matches && !collapsed()) setSidebar(true);
      view.closeMenus();
      view.closeDrawers();
      ProviderUI.closeMenu(element('provider-menu').classList.contains('open'));
      ProviderUI.closeLocalConfig();
    }
  });
}
