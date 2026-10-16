export interface DocumentScope {
  sessionId: string;
  workspace?: string;
  parent?: string;
}

async function read(
  scope: DocumentScope,
  path: string,
  representation: 'reference' | 'image',
): Promise<Record<string, unknown>> {
  const response = await fetch('/api/documents/read', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: scope.sessionId,
      expected_workspace: scope.workspace,
      kind: 'workspace',
      path: scope.parent && !/^(?:[a-z]:[\\/]|\/)/i.test(path) ? `${scope.parent}/${path}` : path,
      representation,
    }),
  });
  const data = await response.json();
  if (!response.ok || data.ok !== true) throw new Error(data.message || data.error || 'Could not read file');
  return data;
}

function imagePreview(image: HTMLImageElement): void {
  image.tabIndex = 0;
  image.setAttribute('role', 'button');
  image.setAttribute('aria-label', 'Enlarge image: ' + (image.alt || 'Image'));
  image.onkeydown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      image.click();
    }
  };
  image.onclick = () => {
    if (!image.src) return;
    const dialog = document.createElement('dialog');
    dialog.className = 'image-preview';
    dialog.setAttribute('aria-label', image.alt || 'Image preview');
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = 'Close';
    close.onclick = () => dialog.close();
    const preview = image.cloneNode() as HTMLImageElement;
    preview.removeAttribute('loading');
    dialog.append(close, preview);
    document.body.append(dialog);
    dialog.addEventListener(
      'close',
      () => {
        dialog.remove();
        image.focus({ preventScroll: true });
      },
      { once: true },
    );
    dialog.showModal();
  };
}

async function loadImage(host: HTMLElement, image: HTMLImageElement, scope: DocumentScope): Promise<void> {
  let error: HTMLElement | null = null;
  const failed = (message: string) => {
    if (!host.contains(image) || error) return;
    error = document.createElement('div');
    error.className = 'md-image-error';
    error.setAttribute('role', 'status');
    const label = document.createElement('span');
    label.textContent = 'This image could not be displayed. ';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'link-btn';
    retry.textContent = 'Reload image';
    const detail = document.createElement('details'),
      summary = document.createElement('summary'),
      raw = document.createElement('pre');
    summary.textContent = 'Details';
    raw.textContent = message;
    detail.append(summary, raw);
    retry.onclick = () => {
      error?.remove();
      error = null;
      void loadImage(host, image, scope);
    };
    error.append(label, retry, detail);
    image.after(error);
    image.hidden = true;
  };
  image.onerror = () => failed('Image decoding or loading failed.');
  image.onload = () => {
    image.hidden = false;
    error?.remove();
    error = null;
  };
  imagePreview(image);
  const path = image.dataset.documentImage;
  if (!path) return;
  try {
    const data = await read(scope, path, 'image');
    if (!host.contains(image)) return;
    if (
      !['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(String(data.mime)) ||
      typeof data.content !== 'string'
    )
      throw new Error('Unsupported image response');
    image.src = `data:${data.mime};base64,${data.content}`;
    image.hidden = false;
  } catch (error) {
    failed(error instanceof Error ? error.message : 'Could not read image');
  }
}

export async function hydrateDocumentContent(host: HTMLElement, scope: DocumentScope): Promise<void> {
  const codes = [...host.querySelectorAll<HTMLElement>('[data-document-candidate]')].slice(0, 64);
  const jobs: Array<() => Promise<void>> = codes.map((code) => async () => {
    if (code.closest('.md-file-link')) return;
    try {
      const data = await read(scope, code.dataset.documentCandidate || '', 'reference');
      if (!host.contains(code) || typeof data.path !== 'string') return;
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'md-file-link';
      // Document-pane clicks apply their parent path themselves.
      action.dataset.documentPath = code.dataset.documentCandidate;
      if (code.dataset.documentLine) action.dataset.documentLine = code.dataset.documentLine;
      code.replaceWith(action);
      action.append(code);
    } catch {
      /* An unconfirmed reference remains ordinary code. */
    }
  });
  for (const image of [...host.querySelectorAll<HTMLImageElement>('img.md-image,img[data-document-image]')])
    jobs.push(() => loadImage(host, image, scope));
  await Promise.all(
    Array.from({ length: Math.min(4, jobs.length) }, async () => {
      let job;
      while ((job = jobs.shift())) await job();
    }),
  );
}
