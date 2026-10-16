export interface QuestionDraft {
  schema_version: 1;
  question_revision: number;
  selected: string | null;
  custom: boolean;
  text: string;
  edit_revision: number;
  writer_id: string;
  saved_at: number;
}

export interface QuestionShape {
  revision: number;
  options: string[];
  allow_free_text?: boolean;
}

interface StoreOptions {
  instanceId: string;
  storage?: Storage;
  writerId?: string;
  now?: () => number;
}

const PREFIX = 'codey:question-draft:';
const MAX_TEXT = 8000;

export class QuestionDraftStore {
  private readonly storage: Storage;
  private readonly writerId: string;
  private readonly now: () => number;
  private readonly instanceId: string;
  private readonly memory = new Map<string, QuestionDraft>();
  private error = '';

  constructor(options: StoreOptions) {
    this.instanceId = options.instanceId.trim();
    this.storage = options.storage || window.localStorage;
    this.writerId = options.writerId || crypto.randomUUID();
    this.now = options.now || Date.now;
  }

  private key(sessionId: string, questionId: string): string {
    return `${PREFIX}${this.instanceId}:${sessionId}:${questionId}`;
  }

  private valid(value: unknown): value is QuestionDraft {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const row = value as Partial<QuestionDraft>;
    return (
      row.schema_version === 1 &&
      Number.isSafeInteger(row.question_revision) &&
      typeof row.custom === 'boolean' &&
      (row.selected === null || typeof row.selected === 'string') &&
      typeof row.text === 'string' &&
      row.text.length <= MAX_TEXT &&
      Number.isSafeInteger(row.edit_revision) &&
      typeof row.writer_id === 'string' &&
      Number.isFinite(row.saved_at)
    );
  }

  save(sessionId: string, questionId: string, draft: Omit<QuestionDraft, 'schema_version'>): QuestionDraft {
    const value: QuestionDraft = { schema_version: 1, ...draft, text: draft.text.slice(0, MAX_TEXT) };
    const key = this.key(sessionId, questionId);
    this.memory.set(key, value);
    try {
      this.storage.setItem(key, JSON.stringify(value));
      this.error = '';
    } catch {
      this.error = 'Could not save draft. Refresh may lose it.';
    }
    return value;
  }

  load(sessionId: string, questionId: string, question: QuestionShape): QuestionDraft | null {
    const key = this.key(sessionId, questionId);
    let raw: unknown = this.memory.get(key) || null;
    try {
      const stored = this.storage.getItem(key);
      if (stored) raw = JSON.parse(stored);
    } catch {
      this.error = 'Could not read draft. Refresh may lose it.';
    }
    if (!this.valid(raw)) return null;
    if (raw.question_revision !== question.revision || raw.text.length > MAX_TEXT) return null;
    if (raw.custom) {
      if (question.allow_free_text === false) return null;
    } else if (raw.selected === null || !question.options.includes(raw.selected)) return null;
    return raw;
  }

  clear(sessionId: string, questionId: string): void {
    const key = this.key(sessionId, questionId);
    this.memory.delete(key);
    try {
      this.storage.removeItem(key);
      this.error = '';
    } catch {
      this.error = 'Could not clear draft.';
    }
  }

  flush(): void {
    for (const [key, value] of this.memory) {
      try {
        this.storage.setItem(key, JSON.stringify(value));
      } catch {
        this.error = 'Could not save draft. Refresh may lose it.';
      }
    }
  }

  lastError(): string {
    return this.error;
  }

  writer(): string {
    return this.writerId;
  }
  timestamp(): number {
    return this.now();
  }
}
