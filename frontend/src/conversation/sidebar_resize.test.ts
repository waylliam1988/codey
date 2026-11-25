import { afterEach, expect, it, vi } from 'vitest';
import { mountSidebarResize } from './sidebar_resize';

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  document.body.replaceChildren();
  document.documentElement.removeAttribute('style');
});

it('clamps keyboard and pointer changes and preserves the saved width across narrow layout', () => {
  vi.stubGlobal('innerWidth', 1000);
  const root = document.documentElement;
  root.style.setProperty('--sidebar-width', '260px');
  root.style.setProperty('--sidebar-min-width', '180px');
  root.style.setProperty('--sidebar-max-width', '500px');
  root.style.setProperty('--sidebar-content-min-width', '600px');
  const sidebar = document.createElement('aside');
  const handle = document.createElement('button');
  sidebar.append(handle);
  document.body.append(sidebar);
  let captured = false;
  Object.assign(handle, {
    setPointerCapture: () => {
      captured = true;
    },
    releasePointerCapture: () => {
      captured = false;
    },
    hasPointerCapture: () => captured,
  });
  vi.spyOn(sidebar, 'getBoundingClientRect').mockImplementation(
    () => new DOMRect(0, 0, parseFloat(root.style.getPropertyValue('--sidebar-width')), 100),
  );

  const closeMenus = vi.fn();
  const resized = vi.fn();
  mountSidebarResize(handle, sidebar, closeMenus, resized);
  const width = () => handle.getAttribute('aria-valuenow');
  const key = (value: string) => handle.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true }));
  const pointer = (type: string, clientX: number) => {
    const event = new MouseEvent(type, { clientX, bubbles: true });
    Object.defineProperty(event, 'pointerId', { value: 1 });
    handle.dispatchEvent(event);
  };

  key('End');
  expect(width()).toBe('400');
  expect(localStorage.getItem('codey:sidebar-width')).toBe('400');
  pointer('pointerdown', 400);
  pointer('pointermove', 140);
  expect(width()).toBe('180');
  key('Escape');
  expect(captured).toBe(false);
  expect(closeMenus).toHaveBeenCalledOnce();
  expect(width()).toBe('400');
  expect(localStorage.getItem('codey:sidebar-width')).toBe('400');

  vi.stubGlobal('innerWidth', 700);
  window.dispatchEvent(new Event('resize'));
  expect(width()).toBe('260');
  key('Home');
  expect(width()).toBe('260');
  vi.stubGlobal('innerWidth', 1000);
  window.dispatchEvent(new Event('resize'));
  expect(width()).toBe('400');
  expect(resized).toHaveBeenCalledTimes(2);
});
