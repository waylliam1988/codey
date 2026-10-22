import { runtime } from './runtime';
import type { ModelSource, ModelState } from './types/domain';
/* Source preferences are authoritative; discovery supplies names, never choices. */
(function () {
  'use strict';
  let state: ModelState = { preferences: { revision: 0, sources: {} }, sources: [], in_use: [] };
  const listeners = new Set<() => void>();
  let loading: Promise<ModelState> | null = null;
  function snapshot(): ModelState {
    return structuredClone(state);
  }
  function catalogTime(source: ModelSource): number {
    return source.checked_at ?? source.updated_at ?? 0;
  }
  function applySettings(data: ModelState): void {
    if (!data?.preferences || !Array.isArray(data.sources)) throw new Error('Model settings unavailable');
    if (data.preferences.revision < state.preferences.revision) return;
    const next = structuredClone(data);
    next.sources = next.sources.map((source) => {
      const current = state.sources.find((item) => item.id === source.id);
      return current && catalogTime(current) > catalogTime(source) ? structuredClone(current) : source;
    });
    state = next;
    listeners.forEach((listener) => listener());
  }
  async function load(): Promise<ModelState> {
    if (loading) return loading;
    loading = (async () => {
      const response = await fetch('/api/model_settings', { cache: 'no-store' });
      if (!response.ok) throw new Error('Could not load models');
      applySettings(await response.json());
      return snapshot();
    })();
    try {
      return await loading;
    } finally {
      loading = null;
    }
  }
  function sourceFor(provider: string): ModelSource | undefined {
    return state.sources.find(
      (source) =>
        source.id === provider || (source.id === 'websites' && source.models.some((model) => model.id === provider)),
    );
  }
  function reason(provider: string, model = ''): string {
    const source = sourceFor(provider);
    if (!source) return 'Unavailable';
    if (source.id !== 'websites' && !source.models.some((entry) => entry.id === model)) return 'Unavailable';
    const choice = state.preferences.sources[source.id];
    return choice?.enabled && choice.models.includes(source.id === 'websites' ? provider : model) ? '' : 'Disabled';
  }
  function allows(provider: string, model = ''): boolean {
    return !reason(provider, model);
  }
  function connections(): ModelSource[] {
    return state.sources.filter((source) => source.id !== 'websites');
  }
  function hasAny(): boolean {
    return state.sources.some(
      (source) =>
        state.preferences.sources[source.id]?.enabled &&
        source.models?.some((model) => state.preferences.sources[source.id].models.includes(model.id)),
    );
  }
  function observeCatalog(source: ModelSource): void {
    const index = state.sources.findIndex((item) => item.id === source.id);
    if (index < 0) return;
    if (catalogTime(source) < catalogTime(state.sources[index])) return;
    state.sources[index] = { ...state.sources[index], ...source };
    listeners.forEach((listener) => listener());
  }
  runtime.Models = {
    load,
    snapshot,
    applySettings,
    allows,
    reason,
    connections,
    hasAny,
    observeCatalog,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
})();
