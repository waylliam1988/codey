const STORAGE_KEY = 'codey:appearance:font-size:v1';
const SIZES = [
  { id: 'standard', label: 'Standard', scale: 1 },
  { id: 'large', label: 'Large', scale: 16 / 14 },
  { id: 'extra', label: 'Extra large', scale: 18 / 14 },
] as const;

function savedIndex(): number {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    const index = SIZES.findIndex((size) => size.id === saved);
    return index < 0 ? 0 : index;
  } catch {
    return 0;
  }
}

function apply(index: number, persist = false): void {
  const safeIndex = Number.isInteger(index) && index >= 0 && index < SIZES.length ? index : 0;
  const size = SIZES[safeIndex];
  document.documentElement.style.setProperty('--font-scale', String(size.scale));

  const slider = document.getElementById('font-size-slider');
  if (slider instanceof HTMLInputElement) {
    slider.value = String(safeIndex);
    slider.setAttribute('aria-valuetext', size.label);
  }
  const current = document.getElementById('font-size-current');
  if (current) current.textContent = size.label;
  document.querySelectorAll<HTMLElement>('.font-step[data-font]').forEach((dot) => {
    dot.classList.toggle('is-selected', dot.dataset.font === size.id);
  });

  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, size.id);
    } catch {
      // Keep the current selection usable when browser storage is unavailable.
    }
  }
}

apply(savedIndex());

const slider = document.getElementById('font-size-slider');
if (slider instanceof HTMLInputElement) {
  slider.addEventListener('input', () => apply(slider.valueAsNumber, true));
}

window.addEventListener('storage', (event) => {
  if (event.key === STORAGE_KEY || event.key === null) apply(savedIndex());
});

export {};
