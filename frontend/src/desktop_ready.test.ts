import { afterEach, expect, it, vi } from 'vitest';
import { reportDesktopFailure, reportDesktopReady } from './desktop_ready';

afterEach(() => {
  delete (window as Window & { pywebview?: unknown }).pywebview;
});

it('reports readiness to the native window without delaying browser boot', async () => {
  const ui_ready = vi.fn().mockResolvedValue(undefined);
  Object.assign(window, { pywebview: { api: { ui_ready } } });
  reportDesktopReady();
  expect(ui_ready).toHaveBeenCalledTimes(1);
});

it('waits for the native bridge if boot finishes before bridge injection', () => {
  reportDesktopReady();
  const ui_ready = vi.fn().mockResolvedValue(undefined);
  Object.assign(window, { pywebview: { api: { ui_ready } } });
  window.dispatchEvent(new Event('pywebviewready'));
  window.dispatchEvent(new Event('pywebviewready'));
  expect(ui_ready).toHaveBeenCalledTimes(1);
});

it('reports boot failure so the same native window can show Retry', () => {
  const ui_failed = vi.fn().mockResolvedValue(undefined);
  Object.assign(window, { pywebview: { api: { ui_failed } } });
  reportDesktopFailure('Could not load the interface');
  expect(ui_failed).toHaveBeenCalledWith('', 'Could not load the interface');
});

it('retains only its startup attempt ID after authorization consumes the launch fragment', async () => {
  vi.resetModules();
  window.history.replaceState(null, '', '/#codey_bootstrap=private-token&codey_startup=attempt-one');
  try {
    const bridge = await import('./desktop_ready');
    window.history.replaceState(null, '', '/');
    const ui_ready = vi.fn().mockResolvedValue(undefined);
    Object.assign(window, { pywebview: { api: { ui_ready } } });
    bridge.reportDesktopReady();
    expect(ui_ready).toHaveBeenCalledWith('attempt-one');
    expect(JSON.stringify(ui_ready.mock.calls)).not.toContain('private-token');
  } finally {
    window.history.replaceState(null, '', '/');
  }
});
