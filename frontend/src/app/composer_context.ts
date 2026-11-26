import { Composer } from '../conversation/composer';
import { createIconSvg, type Icon } from '../render/icon_button';
import type { SessionStateStore } from '../state/session_state_store';
import type { RunProjection } from '../runs/run_projection';

export class ComposerContext {
  constructor(
    private readonly sessions: SessionStateStore,
    private readonly runs: RunProjection,
    private readonly picker: { busy(): boolean },
  ) {}

  update = (): void => {
    const context = document.getElementById('composer-context');
    if (!context) return;
    const project = this.sessions.activeProject();
    const label = project?.name || 'Choose folder';
    const disabled = this.runs.isSessionRunning(this.sessions.activeId) || Composer.isSending(this.sessions.activeId);
    const folder = this.button(context, 'ctx-folder', 'folder');
    folder.setAttribute('aria-label', label);
    const text = folder.querySelector('span');
    if (text && text.textContent !== label) text.textContent = label;
    folder.disabled = disabled || !!project || this.picker.busy();
    folder.title = project?.path || '';
    const research = this.button(context, 'ctx-research', 'globe', 'Research');
    const active = !!this.sessions.activeSession()?.research;
    research.disabled = disabled;
    research.classList.toggle('active', active);
    research.setAttribute('aria-pressed', String(active));
  };

  intent = (sessionId: string): string => {
    const session = this.sessions.sessions.find((item) => item.id === sessionId);
    if (!session?.research) return 'auto';
    return this.sessions.sessionProject(session) ? 'hybrid' : 'research';
  };

  updateProjectAction = (): void => {
    const button = document.getElementById('research-use-project');
    if (!(button instanceof HTMLButtonElement)) return;
    const session = this.sessions.activeSession();
    const busy = this.runs.isSessionRunning(this.sessions.activeId);
    const project = this.sessions.sessionProject(session);
    button.disabled = busy || !session || !!project;
    button.title = busy ? 'Busy' : !session ? 'No chat' : project ? 'Already in a project' : '';
  };

  private button(context: HTMLElement, id: string, icon: Icon, label = ''): HTMLButtonElement {
    const existing = context.querySelector<HTMLButtonElement>(`#${id}`);
    if (existing) return existing;
    const button = document.createElement('button');
    button.type = 'button';
    button.id = id;
    button.className = `ctx-token ${id}`;
    button.setAttribute('aria-label', label);
    const text = document.createElement('span');
    text.textContent = label;
    button.append(createIconSvg(icon), text);
    context.append(button);
    return button;
  }
}
