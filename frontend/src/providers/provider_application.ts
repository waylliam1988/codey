import { element } from '../app/element';
import { Composer } from '../conversation/composer';
import { Render } from '../render/render';
import type { SessionStateStore } from '../state/session_state_store';
import { UiState } from '../state/ui_state';
import { ProviderUI } from './provider_ui';

export class ProviderApplication {
  constructor(private readonly sessions: SessionStateStore) {}

  adoptCatalog(): Promise<void> {
    return ProviderUI.adoptCatalog();
  }

  mount(): void {
    ProviderUI.init({
      $: element,
      escapeHtml: Render.escapeHtml,
      PROVIDERS: UiState.PROVIDERS,
      PROVIDER_LABELS: UiState.PROVIDER_LABELS,
      DEFAULT_PROVIDER: UiState.DEFAULT_PROVIDER,
      updateSend: Composer.updateSend,
      setActiveProvider: Composer.setActiveProvider,
      activeSession: this.sessions.activeSession.bind(this.sessions),
      currentProviderId: this.sessions.currentProviderId.bind(this.sessions),
      persistActiveNow: UiState.persistNow,
    });
  }

  refresh(): void {
    ProviderUI.sync(this.sessions.currentProviderId());
    ProviderUI.refreshStatus(true);
  }
}
