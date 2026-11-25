/** Optional native timing bridge; browser clients use the same application. */
interface DesktopApi {
  ui_ready?(startupId: string): Promise<unknown>;
  ui_failed?(startupId: string, message: string): Promise<unknown>;
}
// Capture before OperatorAuth consumes and removes the launch fragment.
const startupId = new URLSearchParams(window.location.hash.slice(1)).get('codey_startup') || '';

function withBridge(action: (api: DesktopApi) => Promise<unknown> | undefined): void {
  const native = window as Window & { pywebview?: { api?: DesktopApi } };
  if (native.pywebview?.api) {
    void action(native.pywebview.api)?.catch(() => console.warn('Desktop startup feedback was not recorded'));
  } else window.addEventListener('pywebviewready', () => withBridge(action), { once: true });
}

export function reportDesktopReady(): void {
  window.dispatchEvent(new Event('codey:desktop-ready'));
  withBridge((api) => api.ui_ready?.(startupId));
}

export function reportDesktopFailure(message: string): void {
  withBridge((api) => api.ui_failed?.(startupId, message));
}
