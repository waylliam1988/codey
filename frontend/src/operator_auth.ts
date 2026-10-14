import { runtime } from './runtime';
// Browser-compatible source module; the bundler supplies the module wrapper.
async function authorize() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const token = fragment.get('codey_bootstrap');
  if (token) {
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  const response = await fetch(
    '/api/operator_session',
    token
      ? {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token }),
          cache: 'no-store',
          credentials: 'same-origin',
        }
      : { cache: 'no-store', credentials: 'same-origin' },
  );
  if (!response.ok) {
    throw new Error('Open the current Codey launch link to authorize this browser.');
  }
}

runtime.OperatorAuth = { authorize };
