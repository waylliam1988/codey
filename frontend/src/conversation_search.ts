import { HistoryRepository } from './history_repository';
import { iconButton } from './icon_button';

interface SearchDeps {
  sessionId(): string;
  jump(entryId: string): Promise<void>;
  restore(): Promise<void>;
}
export class ConversationSearch {
  readonly element = document.createElement('section');
  private query = document.createElement('input');
  private results = document.createElement('div');
  private status = document.createElement('div');
  private session = '';
  private generation = 0;
  private returnFocus: HTMLElement | null = null;
  private selected = -1;
  private hits: Array<Record<string, unknown>> = [];
  constructor(private deps: SearchDeps) {
    this.element.className = 'conversation-find';
    this.element.hidden = true;
    this.element.setAttribute('aria-label', 'Search this conversation');
    this.query.type = 'search';
    this.query.placeholder = 'Search this conversation';
    this.query.maxLength = 200;
    this.query.setAttribute('aria-label', 'Search conversation history');
    const form = document.createElement('form');
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.textContent = 'Search';
    form.onsubmit = (event) => {
      event.preventDefault();
      void this.search();
    };
    const previous = document.createElement('button');
    previous.type = 'button';
    previous.textContent = 'Previous';
    previous.onclick = () => {
      void this.move(-1);
    };
    const next = document.createElement('button');
    next.type = 'button';
    next.textContent = 'Next';
    next.onclick = () => {
      void this.move(1);
    };
    form.append(
      this.query,
      submit,
      previous,
      next,
      iconButton('Close conversation search', 'close', () => {
        void this.close();
      }),
    );
    this.status.setAttribute('role', 'status');
    this.results.className = 'conversation-find-results';
    this.element.append(form, this.status, this.results);
    this.element.onkeydown = (event) => {
      if (event.isComposing) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        void this.close();
      } else if (event.key === 'F3') {
        event.preventDefault();
        void this.move(event.shiftKey ? -1 : 1);
      }
    };
  }
  open(): void {
    this.session = this.deps.sessionId();
    this.returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this.element.hidden = false;
    this.query.focus();
  }
  hide(): void {
    this.generation++;
    this.element.hidden = true;
  }
  async close(): Promise<void> {
    const same = this.session === this.deps.sessionId();
    this.hide();
    if (same) {
      try {
        await this.deps.restore();
      } catch {
        /* A changed path can invalidate the previous reading anchor. */
      }
      (this.returnFocus?.isConnected ? this.returnFocus : document.getElementById('task'))?.focus({
        preventScroll: true,
      });
    }
  }
  private async search(): Promise<void> {
    const query = this.query.value.trim();
    if (!query) return;
    const generation = ++this.generation;
    this.status.textContent = 'Searching…';
    this.results.replaceChildren();
    try {
      const hits = await new HistoryRepository().search(this.session, query);
      if (generation !== this.generation || this.session !== this.deps.sessionId()) return;
      this.hits = hits;
      this.selected = -1;
      this.status.textContent = hits.length ? `${hits.length}${hits.length === 50 ? '+' : ''} matches` : 'No matches';
      hits.forEach((hit, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.searchHit = String(index);
        const excerpt = String(hit.excerpt || ''),
          at = excerpt.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
        if (at < 0) button.textContent = excerpt;
        else {
          const mark = document.createElement('mark');
          mark.textContent = excerpt.slice(at, at + query.length);
          button.append(
            document.createTextNode(excerpt.slice(0, at)),
            mark,
            document.createTextNode(excerpt.slice(at + query.length)),
          );
        }
        button.onclick = () => {
          this.selected = index;
          void this.jump();
        };
        this.results.append(button);
      });
    } catch {
      if (generation === this.generation) this.status.textContent = 'Search unavailable. Try again.';
    }
  }
  private async move(direction: number): Promise<void> {
    if (!this.hits.length) return;
    this.selected = (this.selected + direction + this.hits.length) % this.hits.length;
    await this.jump();
  }
  private async jump(): Promise<void> {
    const hit = this.hits[this.selected];
    if (!hit || this.session !== this.deps.sessionId()) return;
    const generation = ++this.generation;
    this.status.textContent = 'Opening message…';
    try {
      await this.deps.jump(String(hit.entry_id || hit.message_id));
      if (generation === this.generation) {
        this.status.textContent = `${this.selected + 1} / ${this.hits.length}`;
        this.results
          .querySelectorAll('button')
          .forEach((button, index) => button.setAttribute('aria-current', String(index === this.selected)));
      }
    } catch {
      if (generation === this.generation)
        this.status.textContent = 'Message unavailable. Search again to refresh results.';
    }
  }
}
