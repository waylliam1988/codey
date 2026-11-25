import { requestResponse } from '../api/transport';

interface FolderPickerBridge {
  pick_folder?(initial: string): Promise<unknown>;
}

export async function chooseProjectFolder(mode: string, initial: string): Promise<unknown> {
  const browser = window as Window & { pywebview?: { api?: FolderPickerBridge } };
  const bridge = browser.pywebview?.api;
  if (bridge?.pick_folder) {
    return bridge.pick_folder(initial);
  }

  const response = await requestResponse('/api/pick_folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, initial }),
  });
  if (!response.ok) return { error: 'Could not add project' };
  const payload: unknown = await response.json().catch(() => null);
  return payload;
}
