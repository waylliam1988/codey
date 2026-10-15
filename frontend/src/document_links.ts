export interface DocumentLink {
  path: string;
  line?: number;
}

export function parseDocumentLink(value: string): DocumentLink | null {
  let path = value.trim().replace(/\\/g, '/');
  if (!path || path.includes('\0') || path.startsWith('//')) return null;
  if (/^[a-z][\w+.-]*:/i.test(path) && !/^[a-z]:\//i.test(path)) return null;
  const suffix = /(?::(\d+)(?::\d+)?|#L(\d+))$/.exec(path);
  const line = suffix ? Number(suffix[1] || suffix[2]) : undefined;
  if (suffix) path = path.slice(0, suffix.index);
  // Inline code is a file action only when it names a plausible file, never an arbitrary command.
  if (!/\.[a-z0-9][\w-]*$/i.test(path) || /[\r\n*?<>|]/.test(path)) return null;
  return { path, ...(line && Number.isSafeInteger(line) ? { line } : {}) };
}
