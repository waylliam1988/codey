import type { ConversationApplication } from '../conversation/conversation_application';
import { ProviderUI } from '../providers/provider_ui';
import type { SessionStateStore } from '../state/session_state_store';
import { UiState } from '../state/ui_state';
import type { Workspace } from '../workspace/workspace';
import type { ComposerContext } from './composer_context';

export class SessionApplication {
  constructor(
    private readonly state: SessionStateStore,
    private readonly workspace: Workspace,
    private readonly view: ConversationApplication,
    private readonly context: ComposerContext,
  ) {}

  mount(): void {
    const state = this.state;
    const cached = UiState.cached();
    UiState.init({
      workspace: this.workspace,
      getSessions: () => state.sessions,
      setSessions: (sessions) => {
        state.sessions = sessions;
      },
      getProjects: () => state.projects,
      setProjects: (projects) => {
        state.projects = projects;
      },
      getActiveId: () => state.activeId,
      setActiveId: (id) => {
        state.activeId = id;
      },
      getUpdatedAt: () => state.updatedAt,
      setUpdatedAt: (updated) => {
        state.updatedAt = updated;
      },
      getRevision: () => state.revision,
      setRevision: (revision) => {
        state.revision = revision;
      },
      activeSession: state.activeSession.bind(state),
      currentProviderId: state.currentProviderId.bind(state),
      renderSidebar: this.view.renderSidebar,
      renderChat: this.view.render,
      updateComposerContext: this.context.update,
      syncProviderUI: ProviderUI.sync,
    });
    UiState.apply(cached);
    UiState.cache();
  }

  restore() {
    return UiState.restoreFromServer();
  }
}
