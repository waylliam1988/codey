import { QuestionDraftStore, type QuestionDraft } from './question_draft_store';

export interface UserQuestion {
  id: string;
  session_id: string;
  run_id: string;
  revision: number;
  state: string;
  question: string;
  options: string[];
  descriptions?: string[];
  allow_free_text?: boolean;
  answer?: string;
}
interface QuestionDeps {
  observe(question: UserQuestion): void;
  changed(): void;
  instanceId?: string;
}
function questionRecord(value: unknown): value is UserQuestion {
  if (!value || typeof value !== 'object') return false;
  const q = value as UserQuestion;
  return (
    typeof q.id === 'string' &&
    typeof q.session_id === 'string' &&
    typeof q.run_id === 'string' &&
    Number.isSafeInteger(q.revision) &&
    typeof q.question === 'string' &&
    Array.isArray(q.options) &&
    q.options.length >= 2 &&
    q.options.length <= 6 &&
    q.options.every((option) => typeof option === 'string')
  );
}
export class UserQuestions {
  private items = new Map<string, UserQuestion>();
  private phases = new Map<string, 'sending' | 'unconfirmed' | 'checking'>();
  private drafts = new Map<string, { selected: string; custom: boolean; text: string }>();
  private readonly draftStore: QuestionDraftStore;
  private draftTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private notices = new Map<string, string>();
  private noticeTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private editing = new Set<string>();
  private conflicts = new Map<string, QuestionDraft>();
  private rendered = new WeakMap<HTMLElement, string>();
  constructor(private deps: QuestionDeps) {
    this.draftStore = new QuestionDraftStore({ instanceId: deps.instanceId || 'default' });
    window.addEventListener('pagehide', () => this.flushDrafts());
    window.addEventListener('storage', (event) => {
      if (!event.key?.startsWith('codey:question-draft:') || !event.newValue) return;
      const identity = [...this.items.values()].find((item) => event.key?.endsWith(`:${item.session_id}:${item.id}`));
      if (!identity) return;
      let incoming: QuestionDraft | null = null;
      try {
        const value = JSON.parse(event.newValue) as QuestionDraft;
        if (value && value.schema_version === 1) incoming = value;
      } catch {
        return;
      }
      if (this.editing.has(identity.id)) {
        if (incoming) this.conflicts.set(identity.id, incoming);
        this.setNotice(identity.id, 'Another tab has a newer draft');
        this.syncViews(document.body);
        return;
      }
      this.drafts.delete(identity.id);
      this.setNotice(identity.id, 'Draft updated in another tab');
      this.syncViews(document.body);
    });
  }
  pending(session: string): boolean {
    return [...this.items.values()].some((q) => q.session_id === session && q.state === 'pending');
  }
  focus(session: string): boolean {
    const item = [...this.items.values()].find((q) => q.session_id === session && q.state === 'pending');
    const node =
      item &&
      [...document.querySelectorAll<HTMLElement>('.question-card')].find((el) => el.dataset.questionId === item.id);
    if (!node) return false;
    node.scrollIntoView({ block: 'center', behavior: 'smooth' });
    node.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true });
    return true;
  }
  apply(value: unknown, confirmed = false): boolean {
    if (!questionRecord(value)) return false;
    const old = this.items.get(value.id);
    if (old && (old.revision > value.revision || (!confirmed && old.revision === value.revision))) return false;
    this.items.set(value.id, value);
    if (confirmed || value.state !== 'pending') this.phases.delete(value.id);
    this.syncViews(document.body);
    this.deps.observe(value);
    this.deps.changed();
    return true;
  }
  card(identity: string): HTMLElement {
    const card = document.createElement('div');
    card.className = 'question-card';
    card.dataset.questionId = identity;
    this.render(card);
    return card;
  }
  syncViews(host: HTMLElement): void {
    host.querySelectorAll<HTMLElement>('.question-card').forEach((card) => this.render(card));
  }
  async refresh(): Promise<void> {
    try {
      const response = await fetch('/api/questions'),
        data = await response.json();
      if (response.ok && Array.isArray(data.questions)) data.questions.forEach((q: unknown) => this.apply(q));
    } catch {
      /* Saved cards remain available for an explicit status check. */
    }
  }
  async check(identity: string): Promise<void> {
    if (this.phases.get(identity) === 'checking') return;
    this.phases.set(identity, 'checking');
    this.syncViews(document.body);
    try {
      const response = await fetch('/api/questions?id=' + encodeURIComponent(identity)),
        data = await response.json();
      if (!response.ok || !questionRecord(data.question)) throw new Error('Question unavailable');
      this.apply(data.question, true);
    } catch {
      this.phases.set(identity, 'unconfirmed');
    } finally {
      this.syncViews(document.body);
    }
  }
  private async submit(question: UserQuestion, answer: string, cancel = false): Promise<void> {
    if (this.phases.has(question.id)) return;
    this.phases.set(question.id, 'sending');
    this.flushDraft(question.id);
    this.syncViews(document.body);
    try {
      const response = await fetch('/api/questions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: question.id,
          revision: question.revision,
          answer,
          ...(cancel ? { action: 'cancel' } : {}),
        }),
      });
      const data = await response.json();
      if (!questionRecord(data.question)) throw new Error(data.error || 'Answer was not confirmed');
      this.apply(data.question, true);
      if (data.question.state !== 'answer_accepted' && data.question.state !== 'pending') {
        this.draftStore.clear(question.session_id, question.id);
        this.drafts.delete(question.id);
      }
    } catch {
      this.phases.set(question.id, 'unconfirmed');
      this.syncViews(document.body);
    }
  }
  private render(card: HTMLElement): void {
    const identity = card.dataset.questionId!,
      q = this.items.get(identity),
      phase = this.phases.get(identity);
    const signature = String(q?.revision) + ':' + String(phase) + ':' + (this.notices.get(identity) || '');
    if (this.rendered.get(card) === signature) return;
    this.rendered.set(card, signature);
    card.replaceChildren();
    card.dataset.state = q?.state || 'unavailable';
    const status = document.createElement('div');
    status.className = 'question-state';
    status.setAttribute('role', 'status');
    const check = () => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'link-btn';
      button.dataset.checkQuestion = '';
      button.textContent = phase === 'checking' ? 'Checking…' : 'Check status';
      button.disabled = phase === 'checking';
      button.onclick = () => this.check(identity);
      return button;
    };
    if (!q) {
      status.textContent = 'Question unavailable';
      card.append(status, check());
      return;
    }
    if (q.state !== 'pending') {
      status.textContent =
        q.state === 'answered'
          ? 'Answered · ' + q.answer
          : q.state === 'cancelled'
            ? 'Question dismissed'
            : 'Answer accepted · waiting to queue';
      card.append(status);
      if (q.state === 'answer_accepted') card.append(check());
      return;
    }
    const hadDraft = this.drafts.has(identity);
    const restored = this.draftStore.load(q.session_id, q.id, {
      revision: q.revision,
      options: q.options,
      allow_free_text: q.allow_free_text,
    });
    const draft = this.drafts.get(identity) || {
      selected: restored?.selected || q.options[0],
      custom: restored?.custom || false,
      text: restored?.text || '',
    };
    this.drafts.set(identity, draft);
    if (!hadDraft && restored) this.setNotice(identity, 'Draft restored');
    const form = document.createElement('form'),
      title = document.createElement('div'),
      fields = document.createElement('fieldset');
    title.className = 'question-title';
    title.textContent = q.question;
    fields.disabled = !!phase;
    const conflict = this.conflicts.get(identity);
    if (conflict) {
      const conflictBox = document.createElement('div');
      conflictBox.className = 'question-conflict';
      conflictBox.textContent = 'Another tab has a newer draft.';
      const keep = document.createElement('button');
      keep.type = 'button';
      keep.className = 'text-btn';
      keep.textContent = 'Keep current';
      keep.onclick = () => {
        this.conflicts.delete(identity);
        this.setNotice(identity, 'Kept current draft');
        this.syncViews(document.body);
      };
      const use = document.createElement('button');
      use.type = 'button';
      use.className = 'text-btn';
      use.textContent = 'Use other draft';
      use.onclick = () => {
        this.drafts.set(identity, {
          selected: conflict.selected || q.options[0],
          custom: conflict.custom,
          text: conflict.text,
        });
        this.conflicts.delete(identity);
        this.setNotice(identity, 'Draft restored');
        this.syncViews(document.body);
      };
      conflictBox.append(keep, use);
      card.append(conflictBox);
    }
    const customText = document.createElement('textarea');
    customText.className = 'question-answer';
    customText.maxLength = 8000;
    customText.setAttribute('aria-label', 'Your answer');
    customText.value = draft.text;
    customText.hidden = !draft.custom;
    customText.oninput = () => {
      draft.text = customText.value;
      this.markDraft(q, draft);
    };
    const choice = (label: string, description: string, custom = false) => {
      const row = document.createElement('label');
      row.className = 'question-choice';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'answer-' + identity;
      radio.value = label;
      radio.checked = custom ? draft.custom : !draft.custom && draft.selected === label;
      radio.onchange = () => {
        draft.custom = custom;
        draft.selected = label;
        customText.hidden = !custom;
        this.markDraft(q, draft);
        if (custom) customText.focus();
      };
      const text = document.createElement('span');
      text.textContent = label;
      if (description) {
        const detail = document.createElement('small');
        detail.textContent = description;
        text.append(detail);
      }
      row.append(radio, text);
      fields.append(row);
    };
    q.options.forEach((option, index) => choice(option, q.descriptions?.[index] || ''));
    if (q.allow_free_text !== false) {
      choice('Write an answer', '', true);
      fields.append(customText);
    }
    const actions = document.createElement('div');
    actions.className = 'question-actions';
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'text-btn';
    clear.textContent = 'Clear draft';
    clear.disabled = !!phase;
    clear.onclick = () => {
      this.draftStore.clear(q.session_id, q.id);
      this.drafts.delete(identity);
      this.setNotice(identity, this.draftStore.lastError() || 'Draft cleared');
      this.syncViews(document.body);
    };
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'text-btn';
    cancel.textContent = 'Dismiss';
    cancel.disabled = !!phase;
    cancel.onclick = () => this.submit(q, '', true);
    const confirm = document.createElement('button');
    confirm.type = 'submit';
    confirm.className = 'text-btn question-confirm';
    confirm.textContent = phase === 'sending' ? 'Submitting…' : 'Confirm';
    confirm.disabled = !!phase;
    actions.append(clear, cancel, confirm);
    if (phase === 'unconfirmed' || phase === 'checking') actions.append(check());
    const notice = this.notices.get(identity);
    if (notice) {
      const saved = document.createElement('div');
      saved.className = 'question-draft-notice';
      saved.setAttribute('role', 'status');
      saved.textContent = notice;
      form.append(saved);
    }
    const error = document.createElement('div');
    error.className = 'question-error';
    error.setAttribute('role', 'alert');
    if (phase === 'unconfirmed') status.textContent = 'Answer could not be confirmed';
    let composing = false;
    form.addEventListener('compositionstart', () => {
      composing = true;
    });
    form.addEventListener('compositionend', () => {
      composing = false;
    });
    customText.addEventListener('focus', () => this.editing.add(identity));
    customText.addEventListener('blur', () => this.editing.delete(identity));
    fields.querySelectorAll<HTMLInputElement>('input').forEach((input) => {
      input.addEventListener('focus', () => this.editing.add(identity));
      input.addEventListener('blur', () => this.editing.delete(identity));
    });
    form.onsubmit = (event) => {
      event.preventDefault();
      if (composing || this.phases.has(identity)) return;
      const answer = draft.custom ? draft.text.trim() : draft.selected;
      if (!answer) {
        error.textContent = 'Enter an answer.';
        return;
      }
      void this.submit(q, answer);
    };
    form.append(title, fields, status, error, actions);
    card.append(form);
  }

  private markDraft(question: UserQuestion, draft: { selected: string; custom: boolean; text: string }): void {
    this.drafts.set(question.id, draft);
    const old = this.draftTimers.get(question.id);
    if (old) clearTimeout(old);
    this.draftTimers.set(
      question.id,
      setTimeout(() => {
        this.draftTimers.delete(question.id);
        this.flushDraft(question.id);
      }, 300),
    );
  }

  private flushDraft(identity: string): void {
    const question = this.items.get(identity);
    const draft = this.drafts.get(identity);
    if (!question || !draft || question.state !== 'pending') return;
    const old = this.draftTimers.get(identity);
    if (old) clearTimeout(old);
    this.draftTimers.delete(identity);
    this.draftStore.save(question.session_id, question.id, {
      question_revision: question.revision,
      selected: draft.selected || null,
      custom: draft.custom,
      text: draft.text.slice(0, 8000),
      edit_revision: Date.now(),
      writer_id: this.draftStore.writer(),
      saved_at: this.draftStore.timestamp(),
    });
    this.setNotice(identity, this.draftStore.lastError() || 'Draft saved');
    this.syncViews(document.body);
  }

  private setNotice(identity: string, text: string): void {
    this.notices.set(identity, text);
    const old = this.noticeTimers.get(identity);
    if (old) clearTimeout(old);
    this.noticeTimers.set(
      identity,
      setTimeout(() => {
        this.notices.delete(identity);
        this.noticeTimers.delete(identity);
        this.syncViews(document.body);
      }, 1800),
    );
  }

  private flushDrafts(): void {
    for (const identity of this.drafts.keys()) this.flushDraft(identity);
    this.draftStore.flush();
  }
}
