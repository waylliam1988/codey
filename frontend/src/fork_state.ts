/** Three-way merge only UI metadata changed while a fork request was in flight. */
export function mergeForkState(
  server: Record<string, unknown>,
  baseline: Record<string, unknown>,
  current: Record<string, unknown>,
): Record<string, unknown> {
  const merged = { ...server };
  for (const kind of ['sessions', 'projects']) {
    const before = new Map(((baseline[kind] || []) as Array<Record<string, unknown>>).map((item) => [item.id, item]));
    const now = new Map(((current[kind] || []) as Array<Record<string, unknown>>).map((item) => [item.id, item]));
    const result = new Map(
      ((server[kind] || []) as Array<Record<string, unknown>>).map((item) => [item.id, { ...item }]),
    );
    for (const [id, previous] of before) {
      const local = now.get(id);
      if (!local) {
        result.delete(id);
        continue;
      }
      const next = result.get(id);
      if (!next) continue;
      for (const key of new Set([...Object.keys(previous), ...Object.keys(local)])) {
        if (key === 'messages') continue;
        if (JSON.stringify(local[key]) !== JSON.stringify(previous[key])) {
          if (key in local) next[key] = local[key];
          else delete next[key];
        }
      }
    }
    for (const [id, local] of now) if (!before.has(id)) result.set(id, { ...local });
    merged[kind] = [...result.values()];
  }
  merged.active_id = current.active_id;
  return merged;
}
