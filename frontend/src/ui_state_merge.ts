type Row = Record<string, unknown>;
export interface UiSnapshot extends Row {
  sessions: Row[];
  projects: Row[];
}
const equal = (a: unknown, b: unknown): boolean => {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  const first = a as Row,
    second = b as Row;
  const keys = Object.keys(first);
  return (
    keys.length === Object.keys(second).length && keys.every((key) => key in second && equal(first[key], second[key]))
  );
};

export const sameUiContent = (first: UiSnapshot, second: UiSnapshot): boolean =>
  equal(first.sessions, second.sessions) &&
  equal(first.projects, second.projects) &&
  first.active_id === second.active_id;

function fields(base: Row, local: Row, remote: Row): Row {
  const result = { ...remote };
  for (const key of new Set([...Object.keys(base), ...Object.keys(local)])) {
    if (equal(base[key], local[key])) continue;
    if (!(key in local)) delete result[key];
    else result[key] = local[key];
  }
  return result;
}

/** Three-way UI metadata merge. A remote deletion cannot be undone by a stale edit. */
export function mergeUiState(base: UiSnapshot, local: UiSnapshot, remote: UiSnapshot): UiSnapshot {
  const result = fields(base, local, remote) as UiSnapshot;
  for (const key of ['sessions', 'projects'] as const) {
    const before = new Map(base[key].map((row) => [row.id, row]));
    const pending = new Map(local[key].map((row) => [row.id, row]));
    const current = new Map(remote[key].map((row) => [row.id, row]));
    const deleted = remote.deleted_ids as Record<string, unknown> | undefined;
    const removed = new Set(Array.isArray(deleted?.[key]) ? (deleted[key] as unknown[]) : []);
    const rows: Row[] = [];
    for (const row of local[key]) {
      if (removed.has(row.id)) continue;
      const old = before.get(row.id),
        server = current.get(row.id);
      if (!old) rows.push(server ? fields({}, row, server) : row);
      else if (server) rows.push(fields(old, row, server));
    }
    for (const row of remote[key]) if (!before.has(row.id) && !pending.has(row.id)) rows.push(row);
    result[key] = rows;
  }
  if (!result.sessions.some((row) => row.id === result.active_id)) result.active_id = result.sessions[0]?.id || '';
  result.revision = remote.revision;
  result.deleted_ids = remote.deleted_ids;
  return result;
}
