import { afterEach, expect, it, vi } from 'vitest';
import { hydrateDocumentContent } from './document_content';

afterEach(() => vi.unstubAllGlobals());
it('only promotes confirmed files and binds image requests to the workspace', async () => {
  const host = document.createElement('div');
  host.innerHTML =
    '<code data-document-candidate="cache.py">cache.py</code><code data-document-candidate="missing.py">missing.py</code><img data-document-image="photo.png" alt="测试图">';
  const read = vi.fn(async (_url, init) => {
    const body = JSON.parse(init.body);
    expect(body.session_id).toBe('chat');
    expect(body.expected_workspace).toBe('E:\\project');
    return {
      ok: body.path !== 'missing.py',
      json: async () =>
        body.representation === 'image'
          ? { ok: true, mime: 'image/png', content: 'AAAA' }
          : { ok: true, path: body.path },
    };
  });
  vi.stubGlobal('fetch', read);
  await hydrateDocumentContent(host, { sessionId: 'chat', workspace: 'E:\\project' });
  expect(host.querySelector('[data-document-path]')?.getAttribute('data-document-path')).toBe('cache.py');
  expect(host.querySelector('code[data-document-candidate="missing.py"]')).not.toBeNull();
  expect(host.querySelector('img')?.src).toBe('data:image/png;base64,AAAA');
});

it('isolates image read failures and retries that read only', async () => {
  const host = document.createElement('div');
  host.innerHTML = '<p>答案仍可读</p><img data-document-image="photo.png">';
  const read = vi
    .fn()
    .mockResolvedValueOnce({ ok: false, json: async () => ({ error: 'document_missing' }) })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true, mime: 'image/png', content: 'AAAA' }) });
  vi.stubGlobal('fetch', read);
  await hydrateDocumentContent(host, { sessionId: 'chat' });
  expect(host.querySelector('p')?.textContent).toBe('答案仍可读');
  host.querySelector<HTMLButtonElement>('.md-image-error button')!.click();
  await vi.waitFor(() => expect(host.querySelector('.md-image-error')).toBeNull());
  expect(read).toHaveBeenCalledTimes(2);
});

it('loads local images beyond the first twenty in bounded batches', async () => {
  const host = document.createElement('div');
  host.innerHTML = Array.from({ length: 21 }, (_, index) => `<img data-document-image="photo-${index}.png">`).join('');
  const read = vi.fn(async (_url, init) => {
    const body = JSON.parse(init.body);
    return { ok: true, json: async () => ({ ok: true, mime: 'image/png', content: body.path }) };
  });
  vi.stubGlobal('fetch', read);

  await hydrateDocumentContent(host, { sessionId: 'chat' });

  expect(read).toHaveBeenCalledTimes(21);
  expect([...host.querySelectorAll('img')].every((image) => image.src.startsWith('data:image/png;base64,'))).toBe(true);
});
