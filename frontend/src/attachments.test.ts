import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ComposerAttachments, IMAGE_UNSUPPORTED } from './attachments';
import { runtime } from './runtime';

const ref = {
  id: 'att_' + 'a'.repeat(64),
  name: 'notes.txt',
  media_type: 'text/plain',
  size: 4,
  sha256: 'b'.repeat(64),
};
const tick = () => new Promise((resolve) => setTimeout(resolve, 30));
afterEach(() => vi.unstubAllGlobals());
function setup() {
  const change = vi.fn();
  const files = new ComposerAttachments(change);
  files.bind(document.querySelector('.composer-box')!, document.querySelector('#task')!);
  files.setSession('alpha');
  return files;
}
beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  document.body.innerHTML = '<div class="composer-box"><textarea id="task"></textarea></div>';
  runtime.UiState = { ensureSaved: vi.fn(async () => true) };
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ ok: true, attachment: ref }))),
  );
  vi.stubGlobal('URL', { createObjectURL: () => 'blob:preview', revokeObjectURL: vi.fn() });
});

it('keeps successful attachment UI quiet and persists only opaque references', async () => {
  const files = setup();
  await files.add([new File(['text'], 'notes.txt', { type: 'text/plain' })]);
  await tick();
  expect(files.refs()).toEqual([ref]);
  expect(files.blocked()).toBe(false);
  expect(document.querySelector('.attachment-error')?.hasAttribute('hidden')).toBe(true);
  expect(document.body.textContent).not.toContain('Attachment pending');
  expect(document.body.textContent).not.toContain('Current task');
  expect(localStorage.getItem('codey:attachment-drafts:v1')).not.toContain('data:');
});

it('keeps late upload completion in its original chat', async () => {
  let respond!: (response: Response) => void;
  vi.stubGlobal(
    'fetch',
    vi.fn(
      () =>
        new Promise((resolve) => {
          respond = resolve;
        }),
    ),
  );
  const files = setup();
  await files.add([new File(['text'], 'notes.txt')]);
  await tick();
  files.setSession('beta');
  respond(new Response(JSON.stringify({ attachment: ref })));
  await tick();
  expect(files.has()).toBe(false);
  files.setSession('alpha');
  expect(files.refs()).toEqual([ref]);
});

it('preserves a failed upload until retry or removal', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ error: 'Upload failed' }), { status: 503 })),
  );
  const files = setup();
  await files.add([new File(['text'], 'notes.txt')]);
  await tick();
  expect(files.blocked()).toBe(true);
  expect(document.querySelector('.attachment-error')?.textContent).toBe('Upload failed');
  expect(document.body.textContent).toContain('Retry');
  (document.querySelector('[aria-label="Remove notes.txt"]') as HTMLButtonElement).click();
  expect(files.has()).toBe(false);
  expect(document.querySelector('.attachment-error')?.hasAttribute('hidden')).toBe(true);
});

it('blocks an unsupported image with only the requested error text', async () => {
  localStorage.setItem(
    'codey:attachment-drafts:v1',
    JSON.stringify([['alpha', [{ ...ref, media_type: 'image/png', name: 'screen.png' }]]]),
  );
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async (url) => new Response(JSON.stringify(url === '/api/attachments/capabilities' ? { images: false } : {})),
    ),
  );
  const files = setup();
  files.checkCapability('deepseek', undefined, '');
  await tick();
  expect(files.blocked()).toBe(true);
  expect(document.querySelector('.attachment-error')?.textContent).toBe(IMAGE_UNSUPPORTED);
});

it('claims only the submitted references and can restore a rejected admission', async () => {
  const files = setup();
  await files.add([new File(['text'], 'notes.txt')]);
  await tick();
  files.claim('alpha', [ref]);
  expect(files.has()).toBe(false);
  files.restore('alpha', [ref]);
  expect(files.refs()).toEqual([ref]);
});

it('ignores stale capability replies after model changes', async () => {
  localStorage.setItem(
    'codey:attachment-drafts:v1',
    JSON.stringify([['alpha', [{ ...ref, media_type: 'image/png' }]]]),
  );
  const replies: Array<(response: Response) => void> = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((url) =>
      url === '/api/attachments/capabilities'
        ? new Promise((resolve) => replies.push(resolve))
        : Promise.resolve(new Response('{}')),
    ),
  );
  const files = setup();
  files.checkCapability('first', undefined, '');
  await tick();
  files.checkCapability('second', undefined, '');
  await tick();
  replies[1](new Response('{"images":true}'));
  await tick();
  replies[0](new Response('{"images":false}'));
  await tick();
  expect(files.blocked()).toBe(false);
  expect(document.querySelector('.attachment-error')?.hasAttribute('hidden')).toBe(true);
});
