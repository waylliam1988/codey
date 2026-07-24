(function () {
  'use strict';

  let runtime = null;
  let loadSeq = 0;

function dispose() {
  loadSeq += 1;
  if (runtime && typeof runtime.stop === 'function') {
    runtime.stop();
  }
  runtime = null;
}

function render(panel, options) {
  dispose();
  options = options || {};
  panel.classList.add('research-graph-panel');
  const toolbar = document.createElement('div');
  toolbar.className = 'research-graph-toolbar';
  const depthLabel = document.createElement('span');
  depthLabel.textContent = 'Depth';
  toolbar.appendChild(depthLabel);
  for (const depth of [1, 2]) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'research-depth' + ((options.depth || 1) === depth ? ' active' : '');
    btn.textContent = String(depth);
    btn.onclick = () => {
      if (typeof options.onDepthChange === 'function') options.onDepthChange(depth);
    };
    toolbar.appendChild(btn);
  }
  const spacer = document.createElement('div');
  spacer.className = 'research-graph-spacer';
  toolbar.appendChild(spacer);
  const reset = document.createElement('button');
  reset.type = 'button';
  reset.className = 'drawer-btn';
  reset.textContent = 'Reset';
  reset.onclick = () => {
    if (runtime && typeof runtime.reset === 'function') {
      runtime.reset();
    }
  };
  toolbar.appendChild(reset);
  panel.appendChild(toolbar);

  const stage = document.createElement('div');
  stage.className = 'research-graph-stage';
  const canvas = document.createElement('canvas');
  const status = document.createElement('div');
  status.className = 'research-graph-status';
  setStatus(status, 'Loading graph...');
  stage.append(canvas, status);
  panel.appendChild(stage);

  const detail = document.createElement('div');
  detail.className = 'research-graph-detail';
  panel.appendChild(detail);
  setDetail(detail, null, options);
  loadGraph(options, canvas, status, detail);
}

async function loadGraph(options, canvas, status, detail) {
  const seq = ++loadSeq;
  try {
    const params = graphParams(options);
    const r = await fetch('/api/research/graph?' + params.toString());
    let data = {};
    try { data = await r.json(); } catch {}
    if (seq !== loadSeq || !canvas.isConnected) return;
    if (!r.ok || !data.ok || !data.graph) {
      setStatus(status, data.error || 'Graph unavailable', true);
      return;
    }
    const graph = data.graph;
    if (!Array.isArray(graph.nodes) || !graph.nodes.length) {
      setStatus(status, 'No graph yet');
      return;
    }
    if (seq !== loadSeq || !canvas.isConnected) return;
    const nextRuntime = draw(canvas, graph, detail, options);
    if (seq !== loadSeq || !canvas.isConnected) {
      if (nextRuntime && typeof nextRuntime.stop === 'function') nextRuntime.stop();
      return;
    }
    runtime = nextRuntime;
    status.classList.remove('error');
    status.hidden = true;
  } catch {
    if (seq === loadSeq && canvas.isConnected) {
      setStatus(status, 'Graph unavailable', true);
    }
  }
}

function setStatus(status, text, isError = false) {
  status.hidden = false;
  status.classList.toggle('error', !!isError);
  status.textContent = text;
}

function graphParams(options) {
  const params = new URLSearchParams();
  params.set('session_id', options.sessionId || options.activeId || '');
  params.set('depth', String(options.depth || 1));
  params.set('limit', '96');
  const focusIds = Array.isArray(options.focusIds) ? options.focusIds : [];
  for (const id of focusIds) params.append('focus', id);
  for (const item of (options.counterpoints || []).slice(0, 8)) {
    if (item) params.append('counterpoint', item);
  }
  return params;
}

function setDetail(detail, node, options) {
  detail.innerHTML = '';
  if (!node) {
    const meta = document.createElement('div');
    meta.className = 'research-graph-detail-meta';
    meta.textContent = 'Select a node';
    detail.appendChild(meta);
    return;
  }
  const title = document.createElement('div');
  title.className = 'research-graph-detail-title';
  title.textContent = node.label || node.id || '';
  const meta = document.createElement('div');
  meta.className = 'research-graph-detail-meta';
  meta.textContent = nodeMeta(node);
  detail.append(title, meta);
  if (node.excerpt || node.url || node.path) {
    const body = document.createElement('div');
    body.className = 'research-graph-detail-body';
    body.textContent = node.excerpt || node.url || node.path || '';
    detail.appendChild(body);
  }
  if (node.url || (node.id && !node.virtual)) {
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'drawer-btn research-copy';
    action.textContent = node.url ? 'Open source' : 'Open in Notes';
    action.onclick = () => openNode(node, options);
    detail.appendChild(action);
  }
}

function nodeMeta(node) {
  return [
    node.kind || node.note_type || 'note',
    node.status || '',
    node.path || '',
    node.url || '',
  ].filter(Boolean).join(' · ');
}

function openNode(node, options) {
  if (node.url) {
    if (typeof options.onOpenSource === 'function') options.onOpenSource(node.url);
    return;
  }
  if (!node.id || node.virtual) return;
  if (typeof options.onOpenNote === 'function') options.onOpenNote(node.id);
}

function draw(canvas, graph, detail, options) {
  const css = getComputedStyle(document.documentElement);
  const colors = {
    bg: css.getPropertyValue('--bg').trim() || '#181818',
    faint: css.getPropertyValue('--faint').trim() || '#4a4a4a',
    muted: css.getPropertyValue('--muted').trim() || '#6b6b6b',
    text: css.getPropertyValue('--text').trim() || '#e6e6e6',
    textDim: css.getPropertyValue('--text-dim').trim() || '#a0a0a0',
    ok: css.getPropertyValue('--ok-dot').trim() || '#4ec9b0',
  };
  const nodes = (graph.nodes || []).map((node, index) => {
    const seed = hashUnit(node.id || String(index));
    const angle = seed * Math.PI * 2;
    const radius = 56 + hashUnit((node.id || '') + ':r') * 120;
    return {
      ...node,
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
      fixed: false,
    };
  });
  const byId = new Map(nodes.map(node => [node.id, node]));
  const edges = (graph.edges || [])
    .map(edge => ({ ...edge, source: byId.get(edge.src), target: byId.get(edge.dst) }))
    .filter(edge => edge.source && edge.target);
  const neighbors = graphNeighbors(edges);
  const ctx = canvas.getContext('2d');
  const state = { scale: 1, tx: 0, ty: 0, width: 1, height: 1, hover: null, selected: null };
  let frame = 0;
  let running = true;
  let draggingNode = null;
  let panning = false;
  let start = null;
  let moved = false;

  const resize = () => {
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    state.width = Math.max(1, rect.width);
    state.height = Math.max(1, rect.height);
    canvas.width = Math.round(state.width * ratio);
    canvas.height = Math.round(state.height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (!state.tx && !state.ty) resetView();
    draw();
  };
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);

  function resetView() {
    const bounds = graphBounds(nodes);
    const sx = state.width / Math.max(220, bounds.w + 120);
    const sy = state.height / Math.max(220, bounds.h + 120);
    state.scale = Math.max(0.45, Math.min(1.35, Math.min(sx, sy)));
    state.tx = state.width / 2 - (bounds.cx * state.scale);
    state.ty = state.height / 2 - (bounds.cy * state.scale);
    draw();
  }

  function animate() {
    if (!running) return;
    stepResearchGraph(nodes, edges);
    draw();
    frame = requestAnimationFrame(animate);
  }

  function draw() {
    ctx.clearRect(0, 0, state.width, state.height);
    ctx.fillStyle = colors.bg;
    ctx.fillRect(0, 0, state.width, state.height);
    ctx.save();
    ctx.translate(state.tx, state.ty);
    ctx.scale(state.scale, state.scale);
    for (const edge of edges) drawGraphEdge(ctx, edge, state, neighbors, colors);
    for (const node of nodes) drawGraphNode(ctx, node, state, neighbors, colors);
    for (const node of nodes) drawGraphLabel(ctx, node, state, neighbors, colors);
    ctx.restore();
  }

  function pointerPoint(event) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    };
  }

  function worldPoint(event) {
    const point = pointerPoint(event);
    return {
      x: (point.x - state.tx) / state.scale,
      y: (point.y - state.ty) / state.scale,
      sx: point.x,
      sy: point.y,
    };
  }

  function nodeAt(event) {
    const point = worldPoint(event);
    for (let i = nodes.length - 1; i >= 0; i--) {
      const node = nodes[i];
      const r = graphNodeRadius(node) + 4 / state.scale;
      if (Math.hypot(node.x - point.x, node.y - point.y) <= r) return node;
    }
    return null;
  }

  function onPointerDown(event) {
    const point = worldPoint(event);
    const hit = nodeAt(event);
    start = { ...point, tx: state.tx, ty: state.ty };
    moved = false;
    if (hit) {
      draggingNode = hit;
      hit.fixed = true;
      canvas.classList.add('dragging');
    } else {
      panning = true;
      canvas.classList.add('dragging');
    }
    canvas.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event) {
    const point = worldPoint(event);
    if (start && Math.hypot(point.sx - start.sx, point.sy - start.sy) > 4) moved = true;
    if (draggingNode) {
      draggingNode.x = point.x;
      draggingNode.y = point.y;
      draggingNode.vx = 0;
      draggingNode.vy = 0;
      draw();
      return;
    }
    if (panning && start) {
      state.tx = start.tx + (point.sx - start.sx);
      state.ty = start.ty + (point.sy - start.sy);
      draw();
      return;
    }
    const hit = nodeAt(event);
    if (hit !== state.hover) {
      state.hover = hit;
      draw();
    }
  }

  function onPointerUp(event) {
    const hit = nodeAt(event);
    if (draggingNode) draggingNode.fixed = false;
    draggingNode = null;
    panning = false;
    canvas.classList.remove('dragging');
    try { canvas.releasePointerCapture(event.pointerId); } catch {}
    if (!moved) {
      state.selected = hit;
      setDetail(detail, hit, options);
    }
    start = null;
    draw();
  }

  function onWheel(event) {
    event.preventDefault();
    const point = pointerPoint(event);
    const before = {
      x: (point.x - state.tx) / state.scale,
      y: (point.y - state.ty) / state.scale,
    };
    const factor = event.deltaY > 0 ? 0.9 : 1.1;
    state.scale = Math.max(0.35, Math.min(2.8, state.scale * factor));
    state.tx = point.x - before.x * state.scale;
    state.ty = point.y - before.y * state.scale;
    draw();
  }

  function onDblClick(event) {
    const hit = nodeAt(event);
    if (hit) openNode(hit, options);
  }

  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerup', onPointerUp);
  canvas.addEventListener('pointercancel', onPointerUp);
  canvas.addEventListener('wheel', onWheel, { passive: false });
  canvas.addEventListener('dblclick', onDblClick);
  resize();
  animate();

  return {
    reset: resetView,
    stop() {
      running = false;
      cancelAnimationFrame(frame);
      observer.disconnect();
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('pointercancel', onPointerUp);
      canvas.removeEventListener('wheel', onWheel);
      canvas.removeEventListener('dblclick', onDblClick);
    },
  };
}

function graphNeighbors(edges) {
  const out = new Map();
  for (const edge of edges) {
    if (!out.has(edge.src)) out.set(edge.src, new Set());
    if (!out.has(edge.dst)) out.set(edge.dst, new Set());
    out.get(edge.src).add(edge.dst);
    out.get(edge.dst).add(edge.src);
  }
  return out;
}

function stepResearchGraph(nodes, edges) {
  const repel = 2600;
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let dist = Math.max(18, Math.hypot(dx, dy));
      const force = repel / (dist * dist);
      dx /= dist;
      dy /= dist;
      if (!a.fixed) {
        a.vx += dx * force;
        a.vy += dy * force;
      }
      if (!b.fixed) {
        b.vx -= dx * force;
        b.vy -= dy * force;
      }
    }
  }
  for (const edge of edges) {
    const a = edge.source;
    const b = edge.target;
    const desired = edge.kind === 'cites' ? 92 : 118;
    let dx = b.x - a.x;
    let dy = b.y - a.y;
    const dist = Math.max(1, Math.hypot(dx, dy));
    const force = (dist - desired) * 0.012;
    dx /= dist;
    dy /= dist;
    if (!a.fixed) {
      a.vx += dx * force;
      a.vy += dy * force;
    }
    if (!b.fixed) {
      b.vx -= dx * force;
      b.vy -= dy * force;
    }
  }
  for (const node of nodes) {
    if (node.fixed) continue;
    node.vx += -node.x * 0.0025;
    node.vy += -node.y * 0.0025;
    node.vx *= 0.86;
    node.vy *= 0.86;
    node.x += Math.max(-6, Math.min(6, node.vx));
    node.y += Math.max(-6, Math.min(6, node.vy));
  }
}

function drawGraphEdge(ctx, edge, state, neighbors, colors) {
  const hoverActive = state.hover && (edge.src === state.hover.id || edge.dst === state.hover.id);
  const selectedActive = !hoverActive && state.selected && (edge.src === state.selected.id || edge.dst === state.selected.id);
  const active = hoverActive || selectedActive;
  ctx.save();
  ctx.globalAlpha = hoverActive ? 0.95 : (selectedActive ? 0.78 : (edge.kind === 'cites' ? 0.2 : 0.32));
  ctx.strokeStyle = hoverActive ? colors.ok : (selectedActive ? colors.text : colors.faint);
  ctx.lineWidth = active ? 1.35 : (edge.kind === 'cites' ? 0.7 : 1);
  if (edge.kind === 'contradicts') ctx.setLineDash([5, 5]);
  ctx.beginPath();
  ctx.moveTo(edge.source.x, edge.source.y);
  ctx.lineTo(edge.target.x, edge.target.y);
  ctx.stroke();
  ctx.restore();
}

function drawGraphNode(ctx, node, state, neighbors, colors) {
  const hot = state.hover || state.selected;
  const connected = hot && (hot.id === node.id || (neighbors.get(hot.id) || new Set()).has(node.id));
  const hovered = state.hover && state.hover.id === node.id;
  const faded = hot && !connected;
  const r = graphNodeRadius(node);
  ctx.save();
  ctx.globalAlpha = faded ? 0.28 : 1;
  ctx.fillStyle = hovered ? colors.ok : (node.kind === 'source_url' ? colors.faint : colors.textDim);
  if (!hovered && node.kind === 'counterpoint') ctx.fillStyle = colors.muted;
  ctx.beginPath();
  ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
  ctx.fill();
  if (connected) {
    ctx.strokeStyle = hovered ? colors.ok : colors.text;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.arc(node.x, node.y, r + 3, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.restore();
}

function drawGraphLabel(ctx, node, state, neighbors, colors) {
  const hot = state.hover || state.selected;
  const connected = hot && (hot.id === node.id || (neighbors.get(hot.id) || new Set()).has(node.id));
  const shouldShow = node.focus || connected || Number(node.weight || 0) >= 2.6;
  if (!shouldShow) return;
  const label = String(node.label || node.id || '').slice(0, 42);
  if (!label) return;
  ctx.save();
  ctx.fillStyle = connected || node.focus ? colors.text : colors.textDim;
  const fontSize = Math.max(11, 12 / state.scale);
  ctx.font = `${fontSize}px ${getComputedStyle(document.body).fontFamily}`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.globalAlpha = connected || node.focus ? 0.95 : 0.7;
  const metrics = ctx.measureText(label);
  const pad = 8 / state.scale;
  const minX = (0 - state.tx) / state.scale + metrics.width / 2 + pad;
  const maxX = (state.width - state.tx) / state.scale - metrics.width / 2 - pad;
  const minY = (0 - state.ty) / state.scale + pad;
  const maxY = (state.height - state.ty) / state.scale - fontSize - pad;
  const labelX = minX <= maxX ? Math.max(minX, Math.min(maxX, node.x)) : node.x;
  const rawY = node.y + graphNodeRadius(node) + 5;
  const labelY = minY <= maxY ? Math.max(minY, Math.min(maxY, rawY)) : rawY;
  ctx.fillText(label, labelX, labelY);
  ctx.restore();
}

function graphNodeRadius(node) {
  const base = node.kind === 'source_url' ? 3 : 4.2;
  return Math.max(2.8, Math.min(11, base + Math.sqrt(Number(node.weight || 1)) * 1.6));
}

function graphBounds(nodes) {
  if (!nodes.length) return { cx: 0, cy: 0, w: 1, h: 1 };
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of nodes) {
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x);
    maxY = Math.max(maxY, node.y);
  }
  return {
    cx: (minX + maxX) / 2,
    cy: (minY + maxY) / 2,
    w: maxX - minX,
    h: maxY - minY,
  };
}

function hashUnit(value) {
  let hash = 2166136261;
  const text = String(value || '');
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return ((hash >>> 0) % 10000) / 10000;
}
window.CodeyResearchGraph = { render, dispose };
})();
