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
  private rendered = new WeakMap<HTMLElement, string>();
  constructor(private deps: QuestionDeps) {}
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
    } catch {
      this.phases.set(question.id, 'unconfirmed');
      this.syncViews(document.body);
    }
  }
  private render(card: HTMLElement): void {
    const identity = card.dataset.questionId!,
      q = this.items.get(identity),
      phase = this.phases.get(identity);
    const signature = String(q?.revision) + ':' + String(phase);
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
    const draft = this.drafts.get(identity) || { selected: q.options[0], custom: false, text: '' };
    this.drafts.set(identity, draft);
    const form = document.createElement('form'),
      title = document.createElement('div'),
      fields = document.createElement('fieldset');
    title.className = 'question-title';
    title.textContent = q.question;
    fields.disabled = !!phase;
    const customText = document.createElement('textarea');
    customText.className = 'question-answer';
    customText.maxLength = 8000;
    customText.setAttribute('aria-label', 'Your answer');
    customText.value = draft.text;
    customText.hidden = !draft.custom;
    customText.oninput = () => {
      draft.text = customText.value;
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
    actions.append(cancel, confirm);
    if (phase === 'unconfirmed' || phase === 'checking') actions.append(check());
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
}
