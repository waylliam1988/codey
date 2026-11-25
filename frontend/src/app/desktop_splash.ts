import { mountCharacterPond } from './character_pond';
import './desktop_splash.css';
import { reportDesktopFailure } from './desktop_ready';

// Loaded before application boot. Ordinary browser visits have no splash.
if (
  document.body.hasAttribute('data-desktop-startup') ||
  new URLSearchParams(location.hash.slice(1)).has('codey_startup')
) {
  const surface = document.createElement('div');
  surface.id = 'desktop-startup';
  surface.className = 'empty-conversation';
  surface.setAttribute('aria-label', 'Codey');
  const error = document.createElement('p');
  error.id = 'startup-error';
  error.setAttribute('role', 'alert');
  error.hidden = true;
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.id = 'startup-retry';
  retry.textContent = 'Retry';
  retry.hidden = true;
  const panel = document.createElement('section');
  panel.append(error, retry);
  surface.append(panel);
  document.body.append(surface);
  const dispose = mountCharacterPond(surface);
  const native = window as Window & {
    pywebview?: { api?: { retry(): Promise<boolean> } };
    codeyStartup?: (data: { error: string }) => void;
  };
  retry.onclick = () => {
    retry.disabled = true;
    void native.pywebview?.api
      ?.retry()
      .then((accepted) => {
        if (accepted) native.codeyStartup?.({ error: '' });
        else retry.disabled = false;
      })
      .catch(() => {
        retry.disabled = false;
      });
  };
  native.codeyStartup = (data) => {
    error.textContent = data.error;
    error.hidden = !data.error;
    retry.hidden = !data.error;
    retry.disabled = false;
  };
  // This entry executes before the main module graph. Its failure handling
  // must not depend on that graph successfully importing or calling boot().
  const failed = (event: Event) => {
    if (
      !(event.target instanceof HTMLScriptElement) &&
      !(event.target instanceof HTMLLinkElement && event.target.rel === 'modulepreload')
    )
      return;
    window.removeEventListener('error', failed, true);
    const message = 'Could not load the interface. Retry to reload it.';
    native.codeyStartup?.({ error: message });
    reportDesktopFailure(message);
  };
  window.addEventListener('error', failed, true);
  window.addEventListener(
    'codey:desktop-ready',
    () => {
      window.removeEventListener('error', failed, true);
      surface.classList.add('revealing');
      window.setTimeout(() => {
        dispose();
        surface.remove();
        delete native.codeyStartup;
      }, 350);
    },
    { once: true },
  );
}
