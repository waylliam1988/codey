import type { UiStateApi } from './ui_state';

export async function forgetSessionOnServer(
  id: string,
  preserveUi: boolean,
  acceptState: UiStateApi['accept'],
  reportError: (message: string) => void,
): Promise<boolean> {
  try {
    const response = await fetch('/api/new_chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: id, preserve_ui: preserveUi }),
    });
    const result = await response.json();
    if (!response.ok || result.ok !== true || result.unpurged_stores?.length) {
      reportError(result.error || 'Could not delete chat. Please retry.');
      return false;
    }
    acceptState(result.state);
    return true;
  } catch {
    reportError('Could not delete chat. Please retry.');
    return false;
  }
}
