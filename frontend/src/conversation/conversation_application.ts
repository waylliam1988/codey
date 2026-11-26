import { fetchChangeFile, fetchChanges, fetchRunChanges, restoreChanges } from '../api/changes';
import { element } from '../app/element';
import type { ComposerContext } from '../app/composer_context';
import type { createProjectFolderPicker } from '../app/project_folder';
import { ProviderUI } from '../providers/provider_ui';
import { Render } from '../render/render';
import { Process } from '../runs/process';
import type { RunControls } from '../runs/run_controls';
import { RunDetails } from '../runs/run_details';
import type { RunPresentation } from '../runs/run_presentation';
import type { SessionStateStore } from '../state/session_state_store';
import { ChangesDrawer } from '../workspace/changes_drawer';
import { LocalContextDrawer } from '../workspace/local_context_drawer';
import type { DocumentPane, DocumentRef } from '../workspace/document_pane';
import type { Workspace } from '../workspace/workspace';
import { ResearchDrawer } from '../research/research_drawer';
import type { createResearchPanel } from '../research/research_panel';
import type { ChatFork } from './chat_fork';
import { Composer } from './composer';
import { ConversationUI } from './conversation_ui';
import { Messages } from './messages';
import { renameInputEl } from './rename_input';
import type { Sidebar } from './sidebar';
import { statusRow } from './status_row';

interface ViewDependencies {
  sessions: SessionStateStore;
  sidebar: Sidebar;
  workspace: Workspace;
  documents: DocumentPane;
  context: ComposerContext;
  runs: RunControls;
  activity: RunPresentation;
  folder: ReturnType<typeof createProjectFolderPicker>;
  research: ReturnType<typeof createResearchPanel>;
  fork: ChatFork;
  closeMenus(): void;
  openDocument(sessionId: string, ref: DocumentRef): void;
  previewHtml(sessionId: string, path: string, project: string): void;
}

export class ConversationApplication {
  constructor(private readonly deps: ViewDependencies) {}

  mount(): void {
    const d = this.deps;
    const { sessions, workspace, runs, fork, activity } = d;
    const { coordinator, questions, approvals } = runs;
    const { projection } = coordinator;
    const getActiveId = () => sessions.activeId;
    const activeSession = sessions.activeSession.bind(sessions);
    const currentProjectPath = sessions.currentProjectPath.bind(sessions);
    d.research.init();
    ChangesDrawer.init({
      workspace,
      $: element,
      fetchChanges,
      fetchRunChanges,
      fetchChangeFile,
      restoreChanges,
      escapeHtml: Render.escapeHtml,
      viewed: activity.viewed,
      previewHtml: (project, path) => d.previewHtml(sessions.activeId, path, project),
    });
    LocalContextDrawer.init({ $: element, getActiveId, currentProjectPath, copyText: Render.copyText });
    RunDetails.init({ workspace, getActiveId });
    Messages.init({
      chatFork: fork,
      userQuestions: questions,
      approvals,
      composer: Composer,
      historyRevision: ConversationUI.historyRevision,
      isSessionRunning: projection.isSessionRunning.bind(projection),
      openDocument: d.openDocument,
      statusRow,
      currentProjectPath,
      attachCurrentChatToPickedProject: d.folder.attachCurrentChatToPickedProject,
      approveCommand: runs.approve,
      continueTask: Composer.continueTask,
      resumeTeaching: runs.resumeTeaching,
      getActiveId,
      getRunningSessionId: runs.runningSession,
      getSessions: () => sessions.sessions,
    });
    Process.setLayoutListener(ConversationUI.updateLatest);
    ConversationUI.init({
      syncComposer: Composer.syncSession,
      chatFork: fork,
      userQuestions: questions,
      projectTasks: (tasks, sessionId) =>
        coordinator.projectTaskStates(tasks, new Map(projection.entries()), sessionId),
      activeSession,
      activeProject: sessions.activeProject.bind(sessions),
      getSessions: () => sessions.sessions,
      getProjects: () => sessions.projects,
      getActiveId,
      getRunningSessionId: runs.runningSession,
      sessionProject: sessions.sessionProject.bind(sessions),
      currentProjectPath,
      currentProviderId: sessions.currentProviderId.bind(sessions),
      syncProviderUI: ProviderUI.sync,
      updateComposerContext: d.context.update,
      syncResearchUseProjectButton: d.context.updateProjectAction,
      appendMessageNode: Messages.append,
      switchSession: sessions.switchSession.bind(sessions),
      renderSidebar: this.renderSidebar,
      closeAllMenus: d.closeMenus,
      closeOtherDrawers: this.closeDrawers,
      renameInputEl,
      commitSessionRename: sessions.renameSession.bind(sessions),
      activityChanged: this.updateActivity,
    });
    element('changes-close').onclick = ChangesDrawer.close;
    element('research-close').onclick = ResearchDrawer.close;
    element('research-restore').onclick = d.research.restoreResearchRun;
    element('research-use-project').onclick = d.folder.attachCurrentChatToPickedProject;
    activity.mount();
  }

  renderSidebar = (): void => {
    this.deps.sidebar.render();
    this.updateActivity();
  };

  render = (): void => {
    const { workspace, documents, sessions } = this.deps;
    workspace.setSession(sessions.activeId);
    documents.setSession(sessions.activeId);
    ConversationUI.renderChat();
  };

  scroll = (capture = true): void => ConversationUI.scrollChat(capture);

  scopeChanged = (): void => {
    LocalContextDrawer.handleScopeChanged();
    this.deps.workspace.suspend();
    ChangesDrawer.close();
    ResearchDrawer.close();
  };

  closeDrawers = (): void => {
    this.deps.workspace.close();
    this.deps.documents.close();
    ChangesDrawer.close();
    ResearchDrawer.close();
    LocalContextDrawer.close();
  };

  updateActivity = (): void => {
    const { activity, sessions, workspace } = this.deps;
    activity.update();
    const session = sessions.activeSession();
    const change = session?.messages
      .slice()
      .reverse()
      .find((message) => message.type === 'changes');
    workspace.count('changes', change?.type === 'changes' ? change.count || 0 : 0);
    workspace.count('research', session?.researchRuns?.length || 0);
  };
}
