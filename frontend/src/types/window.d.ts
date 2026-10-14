export {};

declare global {
  interface SseApi {
    init(deps: import('./domain').SseDeps): void;
    connect(): void;
    reconcileRunState(): Promise<void>;
    displayRunStatus(status: string): void;
    acceptRunResponse(response: Response, sessionId: string): Promise<void>;
    eventKey(data: Record<string, unknown>, fallback?: string): string;
  }

  interface Window {
    Render: import('../render').RenderApi;
    UiState: import('../ui_state').UiStateApi;
    ProviderUI: import('../provider_ui').ProviderUiApi;
    Composer: import('../composer').ComposerApi;
    ConversationUI: import('../conversation_ui').ConversationUiApi;
    Messages: import('../messages').MessagesApi;
    Sse: SseApi;
    Requests: import('../requests').RequestsApi;
    Process: import('../process').ProcessApi;
    ChangesDrawer: {
      init(deps: import('../changes_drawer').ChangesDrawerDeps): void;
      open(project: string, path?: string): Promise<void>;
      close(): void;
    };
    LocalContextDrawer: {
      init(deps: import('../local_context_drawer').LocalContextDeps): void;
      open(): void;
      load(): Promise<void>;
      close(): void;
      render(data: unknown): void;
      handleScopeChanged(): void;
    };
    ResearchDrawer: {
      init(deps: import('../research_drawer').ResearchDrawerDeps): void;
      open(sessionId: string): void;
      close(): void;
      render(sessionId: string): void;
      dispose(): void;
      coreNoteIdsForRun(run: import('../research_drawer').ResearchRunView | null | undefined): string[];
      noteIdsForRun(run: import('../research_drawer').ResearchRunView): string[];
      invalidateNoteCache(ids: string[]): void;
    };
    ResearchRuns: {
      init(deps: import('../types/domain').UiDeps): void;
      normalize(
        runId: string,
        research: import('../types/domain').ResearchPayload,
        receipt: unknown,
      ): import('../types/domain').ResearchRun;
      record(
        sessionId: string,
        runId: string,
        research: import('../types/domain').ResearchPayload,
        receipt: unknown,
      ): import('../types/domain').ResearchRun | null;
      current(sessionId?: string): import('../types/domain').ResearchRun | null;
      restore(ownerSessionId?: string): Promise<import('../types/domain').ResearchRestoreResult | null>;
      markRestoreAvailability(runIds: unknown): boolean;
    };
    RunDetails: {
      init(deps: import('../run_details').RunDetailsDeps): void;
      actionForMessage(message: import('./domain').Message): import('../run_details').RunDetailsAction | null;
      actionsForMessage(
        message: import('./domain').Message,
        actions?: Array<import('../run_details').RunDetailsAction | null>,
      ): import('../run_details').RunDetailsAction[];
    };
    OperatorAuth: { authorize(): Promise<void> };
    Settings: {
      init(deps: import('../settings').SettingsModuleDeps): void;
      open(): Promise<void>;
      close(): void;
      save(): Promise<void>;
    };
    ModelSettings: {
      init(deps: import('../model_settings').ModelSettingsDeps): void;
      open(): Promise<void>;
      close(): void;
    };
    ResearchGraph: {
      render(panel: HTMLElement, options: import('../research_graph').GraphOptions): void;
      dispose(): void;
    };
    ConversationNav: { init(deps: import('../conversation_nav').ConversationNavDeps): void };
    Models: {
      load(): Promise<import('./domain').ModelState>;
      snapshot(): import('./domain').ModelState;
      applySettings(data: import('./domain').ModelState): void;
      allows(provider: string, model?: string): boolean;
      reason(provider: string, model?: string): string;
      connections(): import('./domain').ModelSource[];
      hasAny(): boolean;
      observeCatalog(source: import('./domain').ModelSource): void;
      subscribe(listener: () => void): void;
    };
  }
}
