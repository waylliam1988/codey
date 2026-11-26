import type { ConversationApplication } from '../conversation/conversation_application';
import type { ProviderApplication } from '../providers/provider_application';
import type { RunControls } from '../runs/run_controls';
import type { ComposerApplication } from './composer_application';
import type { SessionApplication } from './session_application';
import { mountCharacterPond } from './character_pond';
import { reportDesktopReady } from './desktop_ready';
import { OperatorAuth } from './operator_auth';

interface BootDependencies {
  sessions: Pick<SessionApplication, 'restore'>;
  view: Pick<ConversationApplication, 'mount' | 'render' | 'renderSidebar' | 'scroll'>;
  provider: Pick<ProviderApplication, 'adoptCatalog' | 'mount' | 'refresh'>;
  runs: Pick<RunControls, 'mount' | 'connect' | 'setStatus'>;
  composer: Pick<ComposerApplication, 'mount' | 'prime' | 'restoreAndEnable'>;
}

export async function bootApplication({ sessions, view, provider, runs, composer }: BootDependencies): Promise<void> {
  const main = document.querySelector('main');
  if (main) mountCharacterPond(main);
  await OperatorAuth.authorize();
  await provider.adoptCatalog();
  provider.mount();
  runs.mount(view);
  composer.mount(view);
  view.mount();
  view.renderSidebar();
  view.render();
  composer.prime();
  provider.refresh();
  runs.setStatus('', 'ok');
  await sessions.restore();
  await composer.restoreAndEnable();
  runs.connect();
  reportDesktopReady();
}
