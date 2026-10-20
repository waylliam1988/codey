export interface AppAction {
  id: string;
  label: string;
  run(): void;
}

/** The palette invokes the same actions as the visible controls. */
export class CommandPalette {
  readonly element = document.createElement('dialog');
  private input = document.createElement('input');
  private list = document.createElement('div');
  private visible: AppAction[] = [];
  private selected = 0;
  constructor(private actions: () => AppAction[]) {
    this.element.className = 'command-palette';
    this.element.setAttribute('aria-label', 'Commands');
    this.input.placeholder = 'Find an action…';
    this.input.setAttribute('aria-label', 'Find an action');
    this.input.setAttribute('role', 'combobox');
    this.input.setAttribute('aria-controls', 'command-options');
    this.input.setAttribute('aria-expanded', 'true');
    this.input.setAttribute('aria-autocomplete', 'list');
    this.list.id = 'command-options';
    this.list.setAttribute('role', 'listbox');
    this.list.setAttribute('aria-label', 'Actions');
    this.element.append(this.input, this.list);
    this.input.oninput = () => {
      this.selected = 0;
      this.render();
    };
    this.element.onkeydown = (event) => {
      if (event.isComposing) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        this.element.close();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        this.invoke(this.selected);
      } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) && this.visible.length) {
        event.preventDefault();
        this.selected =
          event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? this.visible.length - 1
              : (this.selected + (event.key === 'ArrowDown' ? 1 : -1) + this.visible.length) % this.visible.length;
        this.paint();
      }
    };
  }
  open(): void {
    if (this.element.open) return;
    this.input.value = '';
    this.selected = 0;
    this.render();
    this.element.showModal();
    this.input.focus();
  }
  private invoke(index: number): void {
    const action = this.visible[index];
    if (!action) return;
    this.element.close();
    action.run();
  }
  private render(): void {
    const query = this.input.value.trim().toLocaleLowerCase();
    this.visible = this.actions().filter((action) => action.label.toLocaleLowerCase().includes(query));
    this.list.replaceChildren();
    for (const [index, action] of this.visible.entries()) {
      const option = document.createElement('button');
      option.type = 'button';
      option.tabIndex = -1;
      option.id = `command-${action.id}`;
      option.setAttribute('role', 'option');
      option.textContent = action.label;
      option.onclick = () => this.invoke(index);
      this.list.append(option);
    }
    if (!this.visible.length) {
      const empty = document.createElement('div');
      empty.className = 'workspace-empty';
      empty.textContent = 'No matching actions';
      this.list.append(empty);
    }
    this.paint();
  }
  private paint(): void {
    const options = Array.from(this.list.querySelectorAll<HTMLElement>('[role="option"]'));
    options.forEach((option, index) => option.setAttribute('aria-selected', String(index === this.selected)));
    const selected = options[this.selected];
    if (selected) {
      this.input.setAttribute('aria-activedescendant', selected.id);
      selected.scrollIntoView({ block: 'nearest' });
    } else this.input.removeAttribute('aria-activedescendant');
  }
}
