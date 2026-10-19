import { decodeApprovalState } from '../shell_approvals';
export type ProviderCatalog = {
  providers: Array<{ id: string; label: string }>;
  labels: Record<string, string>;
  defaultProvider: string;
};

export type ProviderConfig = ProviderCatalog & { recommendedProvider?: string };
export type ProviderStatus = Array<{ id: string; available?: boolean; freshness?: string; state?: string }>;

import type { ChangeFile, ChangeFilePreview, ChangesData, ResearchPayload, RunState } from '../types/domain';
export type ServerEvent = {
  type: string;
  commit_seq?: number;
  database_id?: string;
  after_commit_seq?: number;
  entry_ids?: string[];
  event_id?: number | string;
  cursor?: number | string;
  run_id?: string;
  task_id?: string;
  execution_id?: string;
  session_id?: string;
  status?: string;
  turn?: number;
  turns?: number;
  max_turns?: number;
  text?: string;
  reasoning?: string;
  tool_id?: string;
  kind?: string;
  tool_name?: string;
  path?: string;
  activity?: string;
  command?: string;
  result?: string;
  output?: string;
  truncated?: boolean;
  id?: string;
  summary?: string;
  stop_reason?: string;
  provider?: string;
  mode?: string;
  ok?: boolean;
  changed?: boolean;
  approved?: boolean;
  busy?: boolean;
  continuation_requested?: boolean;
  continued?: boolean;
  stopped?: boolean;
  command_chars?: number;
  command_truncated?: boolean;
  exit_code?: number | null;
  retry_after?: number;
  provider_failure?: { action?: string; kind?: string; provider?: string; stage?: string };
  failure?: {
    schema_version: 1;
    code: string;
    category: string;
    scope: string;
    phase: string;
    effect: string;
    incident_id: string;
    session_id?: string;
    run_id?: string;
    call_id?: string;
    execution_id?: string;
    resource?: { kind: string; path?: string; id?: string };
    diagnostic?: string;
    retry_after?: number;
  };
  providers?: unknown;
  changes?: ChangesData;
  research?: ResearchPayload;
  receipt?: unknown;
  research_restore_runs?: unknown;
  run_status?: string;
  pending_event?: ServerEvent;
  last_shell_result?: ServerEvent;
  last_terminal_event?: ServerEvent;
  error_ref?: string;
  error_type?: string;
  stage?: string;
  command_sha256?: string;
  cwd?: string;
  project?: string;
  note?: string;
  risk_label?: string;
  risk_title?: string;
  risk_detail?: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function decodeProviderCatalog(value: unknown): ProviderCatalog {
  if (!isRecord(value) || !Array.isArray(value.providers)) {
    throw new Error('provider catalog is invalid');
  }
  const providers = value.providers.filter(
    (item): item is { id: string; label: string } =>
      isRecord(item) &&
      typeof item.id === 'string' &&
      item.id.length > 0 &&
      typeof item.label === 'string' &&
      item.label.length > 0,
  );
  if (providers.length === 0) throw new Error('provider catalog has no providers');
  const labels: Record<string, string> = {};
  for (const provider of providers) {
    labels[provider.id] = provider.label;
  }
  const defaultId = typeof value.default === 'string' ? value.default : '';
  const defaultProvider = providers.some((provider) => provider.id === defaultId) ? defaultId : providers[0].id;
  return { providers, labels, defaultProvider };
}

export function decodeProviderConfig(value: unknown): ProviderConfig {
  const catalog = decodeProviderCatalog(value);
  const recommendedProvider =
    isRecord(value) && typeof value.recommended === 'string' && value.recommended.length > 0
      ? value.recommended
      : undefined;
  return { ...catalog, ...(recommendedProvider ? { recommendedProvider } : {}) };
}

export function decodeProviderStatus(value: unknown): ProviderStatus {
  if (!Array.isArray(value)) throw new Error('provider status is invalid');
  return value.map((item) => {
    if (!isRecord(item)) throw new Error('provider status item is invalid');
    if (typeof item.id !== 'string' || item.id.length === 0) throw new Error('provider status id is invalid');
    if (item.available !== undefined && typeof item.available !== 'boolean')
      throw new Error('provider status availability is invalid');
    if (item.freshness !== undefined && typeof item.freshness !== 'string')
      throw new Error('provider status freshness is invalid');
    if (item.state !== undefined && typeof item.state !== 'string') throw new Error('provider status state is invalid');
    return {
      id: item.id,
      ...(typeof item.available === 'boolean' ? { available: item.available } : {}),
      ...(typeof item.freshness === 'string' ? { freshness: item.freshness } : {}),
      ...(typeof item.state === 'string' ? { state: item.state } : {}),
    };
  });
}

export function decodeChangesData(value: unknown): ChangesData {
  if (
    !isRecord(value) ||
    typeof value.ok !== 'boolean' ||
    typeof value.diff !== 'string' ||
    !Array.isArray(value.files)
  )
    throw new Error('changes response is invalid');
  const files = value.files.map((item) => {
    if (!isRecord(item) || typeof item.path !== 'string') throw new Error('changes file is invalid');
    const previewStatus = item.preview_status;
    const validPreviewStatus =
      previewStatus === 'text' ||
      previewStatus === 'binary' ||
      previewStatus === 'too_large' ||
      previewStatus === 'read_error' ||
      previewStatus === 'symlink' ||
      previewStatus === 'invalid' ||
      previewStatus === 'missing';
    return {
      path: item.path,
      ...(typeof item.status === 'string' ? { status: item.status } : {}),
      ...(typeof item.additions === 'number' ? { additions: item.additions } : {}),
      ...(typeof item.deletions === 'number' ? { deletions: item.deletions } : {}),
      ...(validPreviewStatus ? { preview_status: previewStatus as ChangeFile['preview_status'] } : {}),
    };
  });
  return {
    ok: value.ok,
    diff: value.diff,
    files,
    ...(typeof value.project === 'string' ? { project: value.project } : {}),
    ...(typeof value.mode === 'string' ? { mode: value.mode } : {}),
    ...(typeof value.error === 'string' ? { error: value.error } : {}),
    ...(typeof value.changed_count === 'number' ? { changed_count: value.changed_count } : {}),
    ...(typeof value.truncated === 'boolean' ? { truncated: value.truncated } : {}),
    ...(Array.isArray(value.conflicts) && value.conflicts.every((item) => typeof item === 'string')
      ? { conflicts: value.conflicts }
      : {}),
  };
}

export function decodeChangeFilePreview(value: unknown): ChangeFilePreview {
  if (!isRecord(value) || typeof value.ok !== 'boolean' || typeof value.path !== 'string')
    throw new Error('change file response is invalid');
  const allowed = new Set(['text', 'binary', 'too_large', 'read_error', 'symlink', 'invalid', 'missing']);
  if (typeof value.status !== 'string' || !allowed.has(value.status)) throw new Error('change file status is invalid');
  return {
    ok: value.ok,
    path: value.path,
    status: value.status as ChangeFilePreview['status'],
    ...(typeof value.diff === 'string' ? { diff: value.diff } : {}),
    ...(typeof value.size === 'number' ? { size: value.size } : {}),
    ...(typeof value.truncated === 'boolean' ? { truncated: value.truncated } : {}),
    ...(typeof value.error === 'string' ? { error: value.error } : {}),
  };
}

export function decodeResearchPayload(value: unknown): ResearchPayload {
  if (!isRecord(value)) throw new Error('research payload is invalid');
  const strings = (key: string): string[] | undefined => {
    const item = value[key];
    if (item === undefined) return undefined;
    if (!Array.isArray(item) || item.some((entry) => typeof entry !== 'string'))
      throw new Error(`research ${key} is invalid`);
    return item;
  };
  const records = (key: string): Array<Record<string, unknown>> | undefined => {
    const item = value[key];
    if (item === undefined) return undefined;
    if (!Array.isArray(item) || item.some((entry) => !isRecord(entry))) throw new Error(`research ${key} is invalid`);
    return item;
  };
  const result: ResearchPayload = {};
  if (typeof value.restoreable === 'boolean') result.restoreable = value.restoreable;
  if (typeof value.synthesis_id === 'string') result.synthesis_id = value.synthesis_id;
  if (typeof value.sources_read === 'number') result.sources_read = value.sources_read;
  for (const key of ['notes_created', 'notes_updated', 'source_urls', 'queries', 'counterpoints', 'quality_warnings']) {
    const item = strings(key);
    if (item) result[key as keyof ResearchPayload] = item as never;
  }
  for (const key of ['search_results', 'opened_sources', 'citation_map', 'evidence_items']) {
    const item = records(key);
    if (item) result[key as keyof ResearchPayload] = item as never;
  }
  if (value.coverage !== undefined && isRecord(value.coverage))
    result.coverage = value.coverage as ResearchPayload['coverage'];
  return result;
}

export function decodeRunState(value: unknown): RunState {
  if (!isRecord(value)) throw new Error('run state is invalid');
  if (typeof value.busy !== 'boolean') throw new Error('run state busy is required');
  if (
    value.busy &&
    (typeof value.run_id !== 'string' ||
      value.run_id.length === 0 ||
      typeof value.session_id !== 'string' ||
      value.session_id.length === 0)
  ) {
    throw new Error('busy run state requires run and session IDs');
  }
  const state: RunState = {};
  for (const key of ['busy', 'run_id', 'session_id', 'run_status', 'research_restore_runs'] as const) {
    const item = value[key];
    if (item === undefined || item === null) continue;
    if (key === 'busy') {
      if (typeof item !== 'boolean') throw new Error('run state busy is invalid');
      state.busy = item;
    } else if (key === 'run_id' || key === 'session_id' || key === 'run_status') {
      if (typeof item !== 'string') throw new Error(`run state ${key} is invalid`);
      state[key] = item;
    } else {
      if (!Array.isArray(item) || item.some((entry) => typeof entry !== 'string'))
        throw new Error('run state research_restore_runs is invalid');
      state.research_restore_runs = item;
    }
  }
  if (value.active_runs !== undefined) {
    if (!Array.isArray(value.active_runs)) throw new Error('run state active_runs is invalid');
    state.active_runs = value.active_runs.map((entry) => {
      if (!isRecord(entry) || typeof entry.run_id !== 'string' || typeof entry.session_id !== 'string')
        throw new Error('run state active run is invalid');
      return {
        run_id: entry.run_id,
        session_id: entry.session_id,
        ...(typeof entry.run_status === 'string' ? { run_status: entry.run_status } : {}),
        ...(typeof entry.project === 'string' || entry.project === null ? { project: entry.project } : {}),
        ...(typeof entry.provider === 'string' ? { provider: entry.provider } : {}),
      };
    });
  }
  for (const key of ['last_shell_result', 'last_terminal_event', 'pending_event'] as const) {
    const item = value[key];
    if (item !== undefined && item !== null) state[key] = decodeServerEvent(item);
  }
  if (value.shell_approvals !== undefined) {
    if (!Array.isArray(value.shell_approvals)) throw new Error('Invalid approval list');
    state.shell_approvals = value.shell_approvals.map(decodeApprovalState);
  }
  return state;
}

export function decodeServerEvent(value: unknown): ServerEvent {
  if (!isRecord(value) || typeof value.type !== 'string' || value.type.length === 0) {
    throw new Error('server event is invalid');
  }
  if (
    value.type === 'hello' &&
    (typeof value.database_id !== 'string' ||
      !value.database_id ||
      !Number.isSafeInteger(value.after_commit_seq) ||
      (value.after_commit_seq as number) < 0)
  )
    throw new Error('durable cursor handshake is invalid');
  if (
    value.type === 'durable_commit' &&
    (typeof value.session_id !== 'string' ||
      !value.session_id ||
      !Number.isSafeInteger(value.commit_seq) ||
      (value.commit_seq as number) <= 0 ||
      (value.entry_ids !== undefined &&
        (!Array.isArray(value.entry_ids) || !value.entry_ids.every((id) => typeof id === 'string' && id.length > 0))))
  )
    throw new Error('durable commit is invalid');
  const stringFields = [
    'session_id',
    'run_id',
    'task_id',
    'execution_id',
    'status',
    'text',
    'tool_id',
    'kind',
    'tool_name',
    'path',
    'id',
    'summary',
    'stop_reason',
    'stage',
    'error_type',
    'error_ref',
    'provider',
    'mode',
  ];
  for (const field of stringFields) {
    if (field in value && value[field] !== undefined && typeof value[field] !== 'string')
      throw new Error(`server event field ${field} is invalid`);
  }
  const booleanFields = [
    'ok',
    'changed',
    'approved',
    'command_truncated',
    'truncated',
    'busy',
    'continuation_requested',
    'continued',
    'stopped',
  ];
  for (const field of booleanFields) {
    if (field in value && value[field] !== undefined && typeof value[field] !== 'boolean')
      throw new Error(`server event field ${field} is invalid`);
  }
  for (const field of ['turn', 'turns', 'max_turns', 'command_chars', 'retry_after']) {
    if (field in value && value[field] !== undefined && typeof value[field] !== 'number')
      throw new Error(`server event field ${field} is invalid`);
  }
  if (
    'exit_code' in value &&
    value.exit_code !== undefined &&
    value.exit_code !== null &&
    typeof value.exit_code !== 'number'
  ) {
    throw new Error('server event field exit_code is invalid');
  }
  if (
    value.provider_failure !== undefined &&
    (!isRecord(value.provider_failure) ||
      (value.provider_failure.action !== undefined && typeof value.provider_failure.action !== 'string'))
  ) {
    throw new Error('server event provider_failure is invalid');
  }
  if (value.failure !== undefined) {
    if (
      !isRecord(value.failure) ||
      value.failure.schema_version !== 1 ||
      typeof value.failure.code !== 'string' ||
      typeof value.failure.category !== 'string' ||
      typeof value.failure.scope !== 'string' ||
      typeof value.failure.phase !== 'string' ||
      typeof value.failure.effect !== 'string' ||
      typeof value.failure.incident_id !== 'string'
    )
      throw new Error('server event failure is invalid');
    if (
      value.failure.resource !== undefined &&
      (!isRecord(value.failure.resource) || typeof value.failure.resource.kind !== 'string')
    )
      throw new Error('server event failure resource is invalid');
  }
  if (
    value.providers !== undefined &&
    (!Array.isArray(value.providers) ||
      value.providers.some(
        (item) =>
          !isRecord(item) ||
          typeof item.id !== 'string' ||
          (item.available !== undefined && typeof item.available !== 'boolean'),
      ))
  ) {
    throw new Error('server event providers are invalid');
  }
  const research = value.research === undefined ? undefined : decodeResearchPayload(value.research);
  return {
    ...value,
    ...(research === undefined ? {} : { research }),
  } as ServerEvent;
}
