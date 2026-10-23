import { mountCharacterPond } from './character_pond';
import './desktop_splash.css';

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
    pywebview?: { api?: { retry(): Promise<unknown> } };
    codeyStartup?: (data: { error: string }) => void;
  };
  retry.onclick = () => {
    retry.disabled = true;
    void native.pywebview?.api?.retry();
  };
  native.codeyStartup = (data) => {
    error.textContent = data.error;
    error.hidden = !data.error;
    retry.hidden = !data.error;
    retry.disabled = false;
  };
  window.addEventListener(
    'codey:desktop-ready',
    () => {
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
