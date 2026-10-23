import './character_pond.css';

interface Ripple {
  x: number;
  y: number;
  born: number;
  gain: number;
  speed: number;
}

/** Decorative only: never intercept input or persist animation state. */
export function mountCharacterPond(main: HTMLElement): () => void {
  const canvas = document.createElement('canvas');
  canvas.className = 'character-pond';
  canvas.setAttribute('aria-hidden', 'true');
  main.prepend(canvas);
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    canvas.remove();
    return () => {};
  }
  const context = ctx;
  const chars = '0123456789.:;+=-';
  let cells: { x: number; y: number; character: string; alpha: number }[] = [];
  let ripples: Ripple[] = [];
  let width = 1;
  let height = 1;
  let ratio = 1;
  let ink = '';
  let green = '';
  let frame = 0;
  let lastFrame = 0;
  let clock = 0;
  let nextAmbient = 0.25;
  let active = true;
  let inView = true;
  let showing = false;
  let disposed = false;
  let fadeTimer = 0;
  let wasEmpty: boolean | undefined;
  let pointer: { x: number; y: number; time: number } | undefined;
  const empty = () => main.classList.contains('empty-conversation');
  const hash = (a: number, b: number) => {
    const value = Math.sin(a * 127.1 + b * 311.7) * 43758.5453;
    return value - Math.floor(value);
  };
  function pause() {
    cancelAnimationFrame(frame);
    frame = 0;
    pointer = undefined;
  }
  function clear() {
    context.clearRect(0, 0, width, height);
  }
  function ripple(x: number, y: number, gain: number, speed: number) {
    ripples.push({ x, y, born: clock, gain, speed });
    if (ripples.length > 18) ripples.splice(0, ripples.length - 18);
  }
  function draw(time: number) {
    frame = 0;
    if (disposed || document.hidden || !active || !inView || !showing) return;
    if (time - lastFrame < 32) {
      frame = requestAnimationFrame(draw);
      return;
    }
    clock += Math.min(65, time - lastFrame || 33) / 1000;
    lastFrame = time;
    if (empty() && clock >= nextAmbient) {
      ripple(
        width * (0.1 + Math.random() * 0.8),
        height * (0.08 + Math.random() * 0.82),
        0.8 + Math.random() * 0.25,
        61,
      );
      nextAmbient = clock + 2.8 * (0.7 + Math.random() * 0.7);
    }
    ripples = ripples.filter((wave) => clock - wave.born < 7.5);
    const waves = ripples.map((wave) => {
      const age = clock - wave.born;
      return {
        ...wave,
        radius: age * wave.speed,
        spread: 29 + age * 3,
        decay: (wave.gain * Math.exp(-age / 3.6) * Math.min(1, age / 0.2)) / Math.sqrt(1 + age * 0.26),
      };
    });
    clear();
    context.font = '11px Consolas, "SFMono-Regular", monospace';
    context.textBaseline = 'middle';
    for (const cell of cells) {
      let elevation = 0;
      let offsetX = 0;
      let offsetY = 0;
      for (const wave of waves) {
        const dx = cell.x - wave.x,
          dy = cell.y - wave.y;
        const distance = Math.hypot(dx, dy),
          front = distance - wave.radius;
        if (Math.abs(front) > wave.spread * 2.5) continue;
        const displacement =
          Math.cos(front * 0.18) * Math.exp((-front * front) / (2 * wave.spread * wave.spread)) * wave.decay;
        elevation += displacement;
        offsetX += (dx / Math.max(1, distance)) * displacement * 2.1;
        offsetY += (dy / Math.max(1, distance)) * displacement * 1.8;
      }
      const x = cell.x + Math.max(-3, Math.min(3, offsetX));
      const y = cell.y + Math.max(-3, Math.min(3, offsetY));
      context.fillStyle = ink;
      context.globalAlpha = Math.max(0.012, cell.alpha + Math.min(0, elevation) * 0.035);
      context.fillText(cell.character, x, y);
      if (elevation > 0.001) {
        context.fillStyle = green;
        context.globalAlpha = Math.min(0.82, elevation * 0.84);
        context.fillText(cell.character, x, y);
      }
    }
    context.globalAlpha = 1;
    frame = requestAnimationFrame(draw);
  }
  function wake() {
    if (!disposed && !frame && showing && active && inView && !document.hidden) {
      lastFrame = performance.now() - 34;
      frame = requestAnimationFrame(draw);
    }
  }
  function resize() {
    if (!showing) return;
    const box = canvas.getBoundingClientRect();
    width = Math.max(1, box.width);
    height = Math.max(1, box.height);
    ratio = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    // Read actual rendered token colors, including aliases such as --ok-dot.
    const style = getComputedStyle(canvas);
    ink = style.color;
    green = style.getPropertyValue('--pond-green').trim();
    cells = [];
    for (let y = 12, row = 0; y < height; y += 22, row++) {
      for (let x = 9, col = 0; x < width; x += 12, col++) {
        const seed = hash(col, row);
        if (seed < 0.035) continue;
        cells.push({
          x,
          y,
          character: chars[Math.floor(seed * chars.length) % chars.length],
          alpha: 0.025 + hash(row + 2, col + 8) * 0.022,
        });
      }
    }
    wake();
  }
  function sync() {
    if (wasEmpty === empty()) return;
    wasEmpty = empty();
    window.clearTimeout(fadeTimer);
    if (empty()) {
      if (!showing) {
        showing = true;
        ripples = [];
        clock = 0;
        nextAmbient = 0.25;
        resize();
      }
      wake();
    } else if (showing) {
      fadeTimer = window.setTimeout(() => {
        showing = false;
        pause();
        clear();
        cells = [];
        ripples = [];
      }, 520);
    }
  }
  function move(event: PointerEvent) {
    if (document.hidden || !empty()) return;
    active = true;
    const box = canvas.getBoundingClientRect();
    const x = event.clientX - box.left,
      y = event.clientY - box.top;
    if (x < 0 || y < 0 || x > width || y > height) return;
    const now = performance.now() / 1000;
    if (!pointer) {
      ripple(x, y, 0.65, 83);
      pointer = { x, y, time: now };
    } else {
      const distance = Math.hypot(x - pointer.x, y - pointer.y);
      if (distance > 6 && now - pointer.time > 0.045) {
        const count = Math.min(3, Math.max(1, Math.floor(distance / 24)));
        for (let i = 1; i <= count; i++)
          ripple(
            pointer.x + ((x - pointer.x) * i) / count,
            pointer.y + ((y - pointer.y) * i) / count,
            0.4 + Math.min(0.3, distance / 200),
            83,
          );
        pointer = { x, y, time: now };
      }
    }
    wake();
  }
  const leave = () => {
    pointer = undefined;
  };
  const visibility = () => {
    if (document.hidden) pause();
    else wake();
  };
  const blur = () => {
    active = false;
    pause();
  };
  const focus = () => {
    active = true;
    wake();
  };
  const classes = new MutationObserver(sync);
  classes.observe(main, { attributes: true, attributeFilter: ['class'] });
  const size = new ResizeObserver(resize);
  size.observe(canvas);
  const intersection = new IntersectionObserver((entries) => {
    inView = entries[0]?.isIntersecting !== false;
    if (inView) wake();
    else pause();
  });
  intersection.observe(main);
  main.addEventListener('pointermove', move, { passive: true });
  main.addEventListener('pointerleave', leave);
  document.addEventListener('visibilitychange', visibility);
  window.addEventListener('blur', blur);
  window.addEventListener('focus', focus);
  sync();
  return () => {
    disposed = true;
    pause();
    window.clearTimeout(fadeTimer);
    classes.disconnect();
    size.disconnect();
    intersection.disconnect();
    main.removeEventListener('pointermove', move);
    main.removeEventListener('pointerleave', leave);
    document.removeEventListener('visibilitychange', visibility);
    window.removeEventListener('blur', blur);
    window.removeEventListener('focus', focus);
    canvas.remove();
  };
}
