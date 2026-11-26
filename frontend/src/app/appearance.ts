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

  document.querySelectorAll<HTMLInputElement>('input[name="font-size"]').forEach((input) => {
    input.checked = input.value === size.id;
  });

  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, size.id);
    } catch {
      // Keep the current selection usable when browser storage is unavailable.
    }
  }
}

export function mountAppearance(): void {
  apply(savedIndex());

  document.querySelectorAll<HTMLInputElement>('input[name="font-size"]').forEach((input, index) => {
    input.addEventListener('change', () => apply(index, true));
  });

  window.addEventListener('storage', (event) => {
    if (event.key === STORAGE_KEY || event.key === null) apply(savedIndex());
  });
}
