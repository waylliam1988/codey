import { runtime } from './runtime';
import type { AttachmentRef } from './types/domain';

const STORAGE = 'codey:attachment-drafts:v1';
const MAX_FILE = 2 * 1024 * 1024;
const MAX_TOTAL = 4 * 1024 * 1024;
export const IMAGE_UNSUPPORTED =
  'This model does not support image input. Remove the image or choose a supported model.';
type Item = {
  key: string;
  ref?: AttachmentRef;
  file?: File;
  url?: string;
  busy?: boolean;
  error?: string;
  controller?: AbortController;
};

/** Files belong to a chat draft until an immutable request/outbox takes ownership. */
export class ComposerAttachments {
  readonly element = document.createElement('div');
  private chats = new Map<string, Item[]>();
  private session = '';
  private list = document.createElement('div');
  private error = document.createElement('div');
  private notice = '';
  private capabilityKey = '';
  private capability: boolean | undefined;
  private capabilityError = '';
  private storageError = '';

  constructor(private changed: () => void) {
    this.element.className = 'composer-attachments';
    this.element.hidden = true;
    this.list.className = 'attachment-list';
    this.error.className = 'attachment-error';
    this.error.role = 'alert';
    this.error.hidden = true;
    this.element.append(this.list, this.error);
    try {
      const entries = JSON.parse(localStorage.getItem(STORAGE) || '[]');
      if (!Array.isArray(entries)) throw new Error('Invalid attachment drafts');
      for (const [id, refs] of entries) {
        if (typeof id !== 'string' || !Array.isArray(refs) || refs.length > 4 || refs.some((ref) => !validRef(ref)))
          throw new Error('Invalid attachment drafts');
        this.chats.set(
          id,
          refs.map((ref: AttachmentRef) => ({ key: ref.id, ref })),
        );
      }
    } catch {
      this.storageError = 'Attachment drafts could not be restored. Reload before sending attachments.';
    }
  }

  bind(box: HTMLElement, task: HTMLTextAreaElement): void {
    box.prepend(this.element);
    box.addEventListener('dragover', (event) => {
      if ([...(event.dataTransfer?.types || [])].includes('Files')) {
        event.preventDefault();
        box.classList.add('attachment-dragging');
      }
    });
    box.addEventListener('dragleave', (event) => {
      if (!box.contains(event.relatedTarget as Node)) box.classList.remove('attachment-dragging');
    });
    box.addEventListener('drop', (event) => {
      box.classList.remove('attachment-dragging');
      if (!event.dataTransfer?.files.length) return;
      event.preventDefault();
      void this.add([...event.dataTransfer.files]);
    });
    task.addEventListener('paste', (event) => {
      const files = [...(event.clipboardData?.files || [])];
      if (files.length) {
        event.preventDefault();
        void this.add(files);
      }
    });
  }

  setSession(id: string): void {
    if (this.session === id) return;
    this.session = id;
    this.notice = '';
    this.capabilityKey = '';
    this.capability = undefined;
    this.capabilityError = '';
    this.render();
    for (const item of this.items())
      if (item.ref?.media_type.startsWith('image/') && !item.url) void this.preview(id, item);
  }
  private items(id = this.session): Item[] {
    return this.chats.get(id) || [];
  }
  has(): boolean {
    return this.items().length > 0;
  }
  refs(id = this.session): AttachmentRef[] {
    return this.items(id).flatMap((item) => (item.ref ? [item.ref] : []));
  }
  blocked(): boolean {
    return (
      this.has() &&
      (!!this.storageError ||
        this.items().some((item) => item.busy || item.error || !item.ref) ||
        (this.items().some((item) => (item.ref?.media_type || item.file?.type || '').startsWith('image/')) &&
          this.capability !== true))
    );
  }

  checkCapability(provider: string, selection: unknown, runId: string): void {
    if (!this.items().some((item) => (item.ref?.media_type || item.file?.type || '').startsWith('image/'))) return;
    const key = JSON.stringify([this.session, provider, selection, runId]);
    if (key === this.capabilityKey) return;
    this.capabilityKey = key;
    this.capability = undefined;
    this.capabilityError = '';
    void (async () => {
      try {
        if (!(await runtime.UiState.ensureSaved()))
          throw new Error('Could not save this chat. Retry attaching the image.');
        const response = await fetch('/api/attachments/capabilities', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: JSON.parse(key)[0], provider, model_selection: selection }),
          signal: AbortSignal.timeout(10000),
        });
        const data = await response.json();
        if (!response.ok || typeof data.images !== 'boolean')
          throw new Error(data.error || 'Could not check image support.');
        if (key !== this.capabilityKey) return;
        this.capability = data.images;
      } catch (error) {
        if (key !== this.capabilityKey) return;
        this.capabilityError = String((error as Error).message);
      }
      this.render();
      this.changed();
    })();
  }

  claim(id: string, refs: AttachmentRef[]): void {
    const ids = new Set(refs.map((ref) => ref.id));
    const removed = this.items(id).filter((item) => item.ref && ids.has(item.ref.id));
    this.chats.set(
      id,
      this.items(id).filter((item) => !item.ref || !ids.has(item.ref.id)),
    );
    for (const item of removed) this.release(item);
    this.persist();
    if (id === this.session) this.render();
    this.changed();
  }
  restore(id: string, refs: AttachmentRef[]): void {
    const existing = new Set(this.refs(id).map((ref) => ref.id));
    this.chats.set(id, [
      ...this.items(id),
      ...refs.filter((ref) => !existing.has(ref.id)).map((ref) => ({ key: ref.id, ref })),
    ]);
    this.persist();
    if (id === this.session) {
      this.render();
      for (const item of this.items(id))
        if (item.ref?.media_type.startsWith('image/') && !item.url) void this.preview(id, item);
    }
    this.changed();
  }

  async add(files: File[]): Promise<void> {
    const id = this.session;
    if (!id || (document.getElementById('task') as HTMLTextAreaElement)?.disabled) return;
    this.notice = '';
    for (const file of files) {
      const current = this.items(id);
      if (!file.size || file.size > MAX_FILE) {
        this.notice = 'File must contain between 1 byte and 2 MiB.';
        continue;
      }
      if (
        current.length >= 4 ||
        current.reduce((sum, item) => sum + (item.ref?.size || item.file?.size || 0), 0) + file.size > MAX_TOTAL
      ) {
        this.notice = 'Choose up to four files, with a total size of 4 MiB or less.';
        continue;
      }
      const item: Item = {
        key: crypto.randomUUID(),
        file,
        url: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
        busy: true,
      };
      this.chats.set(id, [...current, item]);
      this.render();
      this.changed();
      void this.upload(id, item);
    }
    this.render();
    this.changed();
  }

  private async upload(id: string, item: Item): Promise<void> {
    item.busy = true;
    item.error = '';
    item.controller = new AbortController();
    this.render();
    this.changed();
    try {
      if (!(await runtime.UiState.ensureSaved())) throw new Error('Could not save this chat. Retry the upload.');
      const encoded = await fileData(item.file!);
      if (!this.items(id).includes(item)) return;
      const response = await fetch('/api/attachments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: id, name: item.file!.name, data: encoded }),
        signal: AbortSignal.any([item.controller.signal, AbortSignal.timeout(30000)]),
      });
      const data = await response.json();
      if (!response.ok || !validRef(data.attachment))
        throw new Error(data.error || 'Could not upload this file. Retry.');
      if (!this.items(id).includes(item)) return;
      if (this.items(id).some((other) => other !== item && other.ref?.id === data.attachment.id)) {
        this.chats.set(
          id,
          this.items(id).filter((other) => other !== item),
        );
        this.release(item);
      } else {
        item.ref = data.attachment;
        item.file = undefined;
        if (!item.ref!.media_type.startsWith('image/') && item.url) {
          this.release(item);
          item.url = undefined;
        } else if (item.ref!.media_type.startsWith('image/') && !item.url) {
          void this.preview(id, item);
        }
      }
      this.persist();
    } catch (error) {
      if (this.items(id).includes(item)) item.error = String((error as Error).message);
    } finally {
      item.busy = false;
      if (id === this.session) this.render();
      this.changed();
    }
  }

  private async preview(id: string, item: Item): Promise<void> {
    try {
      const data = await readAttachment(id, item.ref!.id);
      if (this.items(id).includes(item) && typeof data.data_url === 'string') {
        item.url = data.data_url;
        if (id === this.session) this.render();
      }
    } catch {
      /* A missing preview does not imply the stored attachment can be sent. Admission validates it. */
    }
  }
  private release(item: Item): void {
    item.controller?.abort();
    if (item.url?.startsWith('blob:')) URL.revokeObjectURL(item.url);
  }
  private persist(): void {
    try {
      localStorage.setItem(
        STORAGE,
        JSON.stringify(
          [...this.chats].map(([id, items]) => [id, items.flatMap((item) => (item.ref ? [item.ref] : []))]),
        ),
      );
      this.storageError = '';
    } catch {
      this.storageError = 'Attachment drafts could not be saved. Free browser storage before sending.';
    }
  }
  private render(): void {
    this.list.replaceChildren();
    for (const item of this.items()) {
      const chip = document.createElement('div');
      chip.className = 'attachment-chip';
      const name = item.ref?.name || item.file?.name || 'File';
      if (item.url) {
        const image = document.createElement('img');
        image.alt = name;
        image.src = item.url;
        chip.append(image);
      }
      const label = document.createElement('span');
      label.textContent = name;
      label.title = name;
      chip.append(label);
      if (item.busy) {
        const spinner = document.createElement('span');
        spinner.className = 'spinner';
        spinner.setAttribute('aria-label', 'Uploading ' + name);
        chip.append(spinner);
      }
      if (item.error) {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.textContent = 'Retry';
        retry.onclick = () => {
          if (item.file) void this.upload(this.session, item);
          else {
            this.capabilityKey = '';
            this.changed();
          }
        };
        chip.append(retry);
      }
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.setAttribute('aria-label', 'Remove ' + name);
      remove.onclick = () => {
        this.chats.set(
          this.session,
          this.items().filter((other) => other !== item),
        );
        this.release(item);
        this.notice = '';
        this.capabilityKey = '';
        this.persist();
        this.render();
        this.changed();
      };
      chip.append(remove);
      this.list.append(chip);
    }
    const images = this.items().some((item) => (item.ref?.media_type || item.file?.type || '').startsWith('image/'));
    const error =
      this.storageError ||
      this.notice ||
      this.items().find((item) => item.error)?.error ||
      (images ? this.capabilityError || (this.capability === false ? IMAGE_UNSUPPORTED : '') : '');
    this.error.textContent = error || '';
    if (images && this.capabilityError) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'link-btn';
      retry.textContent = 'Retry';
      retry.onclick = () => {
        this.capabilityKey = '';
        this.capabilityError = '';
        this.render();
        this.changed();
      };
      this.error.append(retry);
    }
    this.error.hidden = !error;
    this.list.hidden = !this.has();
    this.element.hidden = !this.has() && !error;
  }
}

export function validRef(value: unknown): value is AttachmentRef {
  const item = value as AttachmentRef;
  return (
    !!item &&
    typeof item.id === 'string' &&
    /^att_[a-f0-9]{64}$/.test(item.id) &&
    typeof item.name === 'string' &&
    typeof item.media_type === 'string' &&
    Number.isSafeInteger(item.size) &&
    item.size > 0 &&
    item.size <= MAX_FILE &&
    typeof item.sha256 === 'string'
  );
}
function fileData(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1]);
    reader.onerror = () => reject(new Error('This file could not be read. Choose it again.'));
    reader.readAsDataURL(file);
  });
}
export async function readAttachment(session: string, id: string): Promise<{ data_url?: string }> {
  const response = await fetch('/api/attachments/read', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: session, id }),
    signal: AbortSignal.timeout(10000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Attachment unavailable');
  return data;
}

export function appendAttachmentRefs(host: HTMLElement, refs: AttachmentRef[] | undefined, session: string): void {
  if (!refs?.length) return;
  const list = document.createElement('div');
  list.className = 'attachment-list message-attachments';
  for (const ref of refs) {
    if (!validRef(ref)) continue;
    const chip = document.createElement('div');
    chip.className = 'attachment-chip';
    const name = document.createElement('span');
    name.textContent = ref.name;
    chip.append(name);
    if (ref.media_type.startsWith('image/'))
      void readAttachment(session, ref.id)
        .then((data) => {
          if (data.data_url && chip.isConnected) {
            const img = document.createElement('img');
            img.alt = ref.name;
            img.src = data.data_url;
            chip.prepend(img);
          }
        })
        .catch(() => {
          chip.title = 'Image preview unavailable';
        });
    list.append(chip);
  }
  host.append(list);
}
