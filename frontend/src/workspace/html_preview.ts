import type { ChangeFile, ChangesData } from '../types/domain';

export function isHtmlPath(path: string): boolean {
  return /\.html?$/i.test(path);
}

export function isPreviewableHtml(file: ChangeFile): boolean {
  return (
    typeof file?.path === 'string' &&
    isHtmlPath(file.path) &&
    !(typeof file.status === 'string' && file.status.trim().startsWith('D')) &&
    (!file.preview_status || file.preview_status === 'text')
  );
}

export function finishedHtmlPath(
  changes?: Pick<ChangesData, 'files' | 'changed_count' | 'truncated'>,
): string | undefined {
  if (!Array.isArray(changes?.files) || changes.truncated || changes.changed_count !== changes.files.length)
    return undefined;
  const files = changes.files.filter(isPreviewableHtml);
  return files.length === 1 ? files[0].path : undefined;
}

const policy = [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  'img-src data: blob:',
  'font-src data:',
  'media-src data: blob:',
  "base-uri 'none'",
  "form-action 'none'",
].join('; ');

// The opaque frame has no browser storage. Provide storage for this page instance only,
// so standalone games that register their controls after reading a score can start.
const storage = `
(() => {
  const values = new Map();
  Object.defineProperty(window, 'localStorage', {
    value: {
      get length() { return values.size; },
      getItem(key) { return values.get(String(key)) ?? null; },
      setItem(key, value) { values.set(String(key), String(value)); },
      removeItem(key) { values.delete(String(key)); },
      clear() { values.clear(); },
      key(index) { return Array.from(values.keys())[index >>> 0] ?? null; },
    },
  });
})();
`;

function staticPreviewMarkup(content: string): string {
  // A sandboxed frame can navigate itself. Remove navigation sources while the markup is inert.
  const template = document.createElement('template');
  template.innerHTML = content;
  for (const node of template.content.querySelectorAll(
    'meta, base, link, script, iframe, frame, frameset, object, embed, portal, fencedframe, template, animate, set, discard',
  )) {
    node.remove();
  }
  for (const link of template.content.querySelectorAll('a, area')) {
    link.removeAttribute('href');
    link.removeAttributeNS('http://www.w3.org/1999/xlink', 'href');
  }
  return template.innerHTML;
}

export function htmlPreview(
  title: string,
  content: string,
  mode: 'interactive' | 'static' = 'interactive',
): HTMLIFrameElement {
  const frame = document.createElement('iframe');
  frame.className = 'html-preview-frame';
  frame.title = title;
  frame.tabIndex = 0;
  const interactive = mode === 'interactive';
  frame.setAttribute('sandbox', interactive ? 'allow-scripts' : '');
  frame.referrerPolicy = 'no-referrer';
  const doctype = content.match(/^\s*<!doctype\s+html\b[^>]*>/i)?.[0] ?? '';
  // The trusted prelude must be parsed before any markup or script from the file.
  const body = content.slice(doctype.length);
  frame.srcdoc = `${doctype || '<!doctype html>'}<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}">${interactive ? `<script>${storage}</script>${body}` : staticPreviewMarkup(body)}`;
  return frame;
}
