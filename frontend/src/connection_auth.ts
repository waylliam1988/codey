import type { ConnectionAction, ConnectionAuthentication, ModelSource } from './types/domain';

const activeStates = new Set(['starting', 'awaiting_authorization', 'exchanging']);
const labels: Partial<Record<ConnectionAction, string>> = {
  login: 'Sign in',
  reopen_login: 'Reopen sign-in page',
  cancel_login: 'Cancel sign-in',
  logout: 'Sign out',
};
interface AuthPanelDeps {
  current(): boolean;
  publish(source: ModelSource): void;
  error(message: string): void;
}

function authentication(value: unknown): ConnectionAuthentication {
  if (!value || typeof value !== 'object') throw new Error('Invalid authentication response');
  const auth = value as Partial<ConnectionAuthentication>;
  if (
    auth.kind !== 'oauth_device' ||
    typeof auth.state !== 'string' ||
    !Number.isSafeInteger(auth.revision) ||
    !Array.isArray(auth.actions) ||
    auth.actions.some(
      (action) => !['login', 'reopen_login', 'cancel_login', 'logout', 'clear_data'].includes(action),
    ) ||
    (auth.user_code != null && typeof auth.user_code !== 'string') ||
    (auth.expires_at != null && (typeof auth.expires_at !== 'number' || !Number.isFinite(auth.expires_at))) ||
    (auth.attempt_id != null && typeof auth.attempt_id !== 'string')
  )
    throw new Error('Invalid authentication response');
  return auth as ConnectionAuthentication;
}

/** One mounted settings generation owns one poll; no credential or URL persistence. */
export function mountConnectionAuthentication(parent: HTMLElement, source: ModelSource, deps: AuthPanelDeps) {
  const box = document.createElement('div');
  box.className = 'connection-auth settings-hint';
  box.setAttribute('role', 'status');
  box.tabIndex = -1;
  parent.appendChild(box);
  let snapshot = source.authentication;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let busy = false;
  let closed = false;
  function current() {
    return !closed && deps.current();
  }
  function schedule() {
    if (timer) clearTimeout(timer);
    if (current() && snapshot && activeStates.has(snapshot.state)) timer = setTimeout(() => void request(), 2000);
  }
  function update(next?: ConnectionAuthentication) {
    if (!current() || !next || (snapshot && next.revision < snapshot.revision)) return;
    snapshot = next;
    render();
    schedule();
  }
  async function request(action?: ConnectionAction) {
    if (!current() || busy || !snapshot) return;
    busy = true;
    const attempt = snapshot.attempt_id;
    render();
    try {
      const response = await fetch(
        action ? '/api/connection_action' : '/api/connection_auth?source=' + encodeURIComponent(source.id),
        action
          ? {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source: source.id, action, attempt_id: attempt }),
            }
          : { cache: 'no-store' },
      );
      const data = await response.json();
      if (!current() || (!action && attempt !== snapshot.attempt_id)) return;
      if (!response.ok) throw new Error(data.error || 'Connection operation failed');
      const next = authentication(data.authentication);
      if (next.revision < snapshot.revision) return;
      const previousState = snapshot.state;
      snapshot = next;
      const facts = data.source && data.source.id === source.id ? data.source : source;
      deps.publish({ ...facts, authentication: next });
      deps.error('');
      if (next.state === 'ready' && previousState !== 'ready' && source.discoverable) {
        const catalog = await fetch('/api/model_catalog', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: source.id, force: false }),
        });
        const discovered = await catalog.json();
        if (!current()) return;
        if (!catalog.ok)
          throw new Error(discovered.error || 'Connected, but model refresh failed. Refresh models to retry.');
        if (discovered.source?.id === source.id && Array.isArray(discovered.source.models))
          deps.publish(discovered.source);
      }
    } catch (error) {
      if (current()) deps.error(error instanceof Error ? error.message : String(error));
    } finally {
      busy = false;
      if (current()) {
        render();
        schedule();
      }
    }
  }
  function render() {
    if (!snapshot) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    const focused = box.contains(document.activeElement)
      ? (document.activeElement as HTMLElement).dataset.action
      : undefined;
    box.replaceChildren();
    const text = document.createElement('p');
    text.textContent =
      snapshot.state === 'ready'
        ? 'Connected'
        : snapshot.state === 'awaiting_authorization'
          ? 'Waiting for browser authorization' + (snapshot.user_code ? ' · ' + snapshot.user_code : '')
          : snapshot.state === 'starting' || snapshot.state === 'exchanging'
            ? 'Connecting…'
            : snapshot.state === 'reauth_required'
              ? 'Sign in again to continue'
              : snapshot.state === 'error' || snapshot.state === 'expired'
                ? 'Sign-in failed or expired. Please retry.'
                : 'Signed out';
    if (snapshot.error_code === 'browser_open_failed')
      text.textContent += ' · Could not open the browser. Reopen to retry.';
    if (snapshot.state === 'awaiting_authorization' && snapshot.expires_at)
      text.textContent += ' · Expires at ' + new Date(snapshot.expires_at * 1000).toLocaleTimeString();
    box.appendChild(text);
    const actions = document.createElement('div');
    actions.className = 'model-source-actions';
    for (const action of snapshot.actions) {
      const label = labels[action];
      // Full cleanup is a separate explicit maintenance operation, not sign-out.
      if (!label) continue;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'link-btn';
      button.textContent = label;
      button.dataset.action = action;
      button.disabled = busy;
      button.onclick = () => void request(action);
      actions.appendChild(button);
    }
    box.appendChild(actions);
    if (focused)
      (box.querySelector<HTMLButtonElement>(`[data-action="${focused}"]`) || box).focus({ preventScroll: true });
  }
  render();
  schedule();
  return {
    update,
    close() {
      closed = true;
      if (timer) clearTimeout(timer);
      box.remove();
    },
  };
}
