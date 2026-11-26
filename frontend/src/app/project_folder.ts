import { requestResponse } from '../api/transport';
import { isRecord } from '../domain/display_values';

type FolderMode = 'open' | 'new';
type FolderSelection =
  | { kind: 'selected'; path: string; name: string }
  | { kind: 'cancelled' }
  | { kind: 'error'; message: string };

declare global {
  interface Window {
    pywebview?: { api?: { pick_folder?(mode: FolderMode, initial: string): Promise<unknown> } };
  }
}

const endpoint = '/api/pick_folder';
const requestTimeoutMs = 10_000;

interface ProjectFolderDeps {
  onBusy(busy: boolean): void;
  onError(message: string): void;
  initialPath(): string;
  activeSessionId(): string;
  canAttach(sessionId: string): boolean;
  hasRunningTask(): boolean;
  ensureProject(path: string, name: string): { id: string };
  attachSession(projectId: string, sessionId: string): void;
  newSession(projectId: string): void;
}

function decodeSelection(value: unknown): FolderSelection {
  if (isRecord(value)) {
    if (value.ok === true && typeof value.path === 'string' && value.path && typeof value.name === 'string') {
      return { kind: 'selected', path: value.path, name: value.name };
    }
    if (value.cancelled === true) return { kind: 'cancelled' };
    if (typeof value.error === 'string') return { kind: 'error', message: value.error };
  }
  return { kind: 'error', message: 'Could not select a project folder' };
}

async function chooseProjectFolder(mode: FolderMode, initial: string): Promise<FolderSelection> {
  const bridge = window.pywebview?.api;
  if (bridge?.pick_folder) return decodeSelection(await bridge.pick_folder(mode, initial));

  let response = await requestResponse(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, initial }),
    signal: AbortSignal.timeout(requestTimeoutMs),
  });
  const first: unknown = await response.json();
  if (response.status !== 202) return decodeSelection(first);
  if (!isRecord(first) || typeof first.id !== 'string' || !first.id) return decodeSelection(null);
  const query = new URLSearchParams({ id: first.id });
  do {
    await new Promise<void>((resolve) => setTimeout(resolve, 250));
    response = await requestResponse(endpoint + '?' + query, {
      cache: 'no-store',
      signal: AbortSignal.timeout(requestTimeoutMs),
    });
  } while (response.status === 202);
  return decodeSelection(await response.json());
}

export function createProjectFolderPicker(deps: ProjectFolderDeps) {
  let busy = false;

  async function pick(sessionId: string | null, requireIdle: boolean): Promise<void> {
    if (busy) return;
    busy = true;
    deps.onBusy(true);
    try {
      const selected = await chooseProjectFolder(sessionId === null ? 'open' : 'new', deps.initialPath());
      if (selected.kind === 'error') deps.onError(selected.message);
      if (selected.kind !== 'selected') return;
      if (sessionId !== null && (!deps.canAttach(sessionId) || (requireIdle && deps.hasRunningTask()))) return;
      const project = deps.ensureProject(selected.path, selected.name);
      if (sessionId === null) deps.newSession(project.id);
      else deps.attachSession(project.id, sessionId);
    } catch {
      deps.onError('Could not select a project folder');
    } finally {
      busy = false;
      deps.onBusy(false);
    }
  }

  function pickProject(): Promise<void> {
    const sessionId = deps.activeSessionId();
    return pick(deps.canAttach(sessionId) ? sessionId : null, false);
  }

  async function attachCurrentChatToPickedProject(): Promise<void> {
    const sessionId = deps.activeSessionId();
    if (deps.hasRunningTask() || !deps.canAttach(sessionId)) return;
    await pick(sessionId, true);
  }

  return { busy: () => busy, pickProject, attachCurrentChatToPickedProject };
}
