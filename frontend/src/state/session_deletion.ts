import { isRecord } from '../domain/display_values';
import { readJsonObject, requestResponse } from '../api/transport';
import type { UiStateApi } from './ui_state';

export async function forgetSessionOnServer(
  id: string,
  preserveUi: boolean,
  acceptState: UiStateApi['accept'],
  reportError: (message: string) => void,
): Promise<boolean> {
  try {
    const response = await requestResponse('/api/new_chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: id, preserve_ui: preserveUi }),
    });
    const result = await readJsonObject(response);
    if (
      !response.ok ||
      result.ok !== true ||
      (Array.isArray(result.unpurged_stores) && result.unpurged_stores.length > 0)
    ) {
      reportError(typeof result.error === 'string' ? result.error : 'Could not delete chat. Please retry.');
      return false;
    }
    if (!isRecord(result.state)) throw new Error('Missing session state');
    acceptState(result.state);
    return true;
  } catch {
    reportError('Could not delete chat. Please retry.');
    return false;
  }
}
