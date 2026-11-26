import { isFailureInfo, isRecord, isToolPreview } from './display_values';
import { isInputRecord } from '../runs/input_outbox';
import { isQuestionRecord } from '../runs/user_questions';
import publicEventFields from '../../../codey/app/public_events.json';
import { decodeApprovalState } from '../runs/shell_approvals';
export type ProviderCatalog = {
  providers: Array<{ id: string; label: string }>;
  labels: Record<string, string>;
  defaultProvider: string;
};

export type ProviderStatus = Array<{ id: string; available?: boolean; freshness?: string; state?: string }>;

import type {
  ChangeFile,
  ChangeFilePreview,
  ChangesData,
  ChangeVerification,
  ResearchPayload,
  RunState,
} from '../types/domain';
type PublicEventKind = keyof typeof publicEventFields;
type ServerPayload = {
  question?: unknown;
  input?: unknown;
  preview?: import('../render/render').ToolPreview;
  elapsed_ms?: number;
  started_at_ms?: number;
  phase?: string;
  stream_id?: string;
  sequence?: number;
  state?: 'partial' | 'complete' | 'interrupted';
  output_bytes?: number;
  output_stored_bytes?: number;
  detail?: string;
  detail_truncated?: boolean;
  final_delivery?: string;
  task_revision?: number;
  task_status?: string;
  commit_seq?: number;
  path_generation?: number;
  revision?: number;
  database_id?: string;
  after_commit_seq?: number;
  entry_ids?: string[];
  edited_entry_id?: string;
  replaced_run_ids?: string[];
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
  call_id?: string;
  parent_call_id?: string;
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
  failure?: import('../runs/failure_presentation').FailureInfo;
  providers?: unknown;
  changes?: Pick<ChangesData, 'changed_count' | 'mode' | 'files'>;
  research?: ResearchPayload;
  receipt?: unknown;
  research_restore_runs?: unknown;
  run_status?: string;
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
  session_rule_available?: boolean;
  project_rule_available?: boolean;
};

type FieldsFor<Kind extends PublicEventKind> = Extract<
  keyof (typeof publicEventFields)[Kind]['fields'],
  keyof ServerPayload
>;
type RequiredFor<Kind extends PublicEventKind> = (typeof publicEventFields)[Kind] extends { required: infer Fields }
  ? Extract<keyof Fields, FieldsFor<Kind>>
  : never;
type ScopedPayload<Kind extends PublicEventKind> = Pick<ServerPayload, FieldsFor<Kind>> &
  Required<Pick<ServerPayload, RequiredFor<Kind>>> &
  Partial<Record<Exclude<keyof ServerPayload, FieldsFor<Kind>>, never>>;
export type ServerEvent = {
  [Kind in PublicEventKind]: { type: Kind } & ScopedPayload<Kind>;
}[PublicEventKind];

export class UnknownServerEvent extends Error {
  constructor(kind: string) {
    super(`Unknown server event ${kind}`);
  }
}

function publicEventKind(kind: string): kind is PublicEventKind {
  return Object.hasOwn(publicEventFields, kind);
}

export function sequence(value: unknown, minimum = 0): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum;
}

export function nonempty(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

export { isRecord } from './display_values';

export function decodeProviderCatalog(value: unknown): ProviderCatalog {
  if (!isRecord(value) || !Array.isArray(value.providers)) {
    throw new Error('provider catalog is invalid');
  }
  const providers = value.providers.filter(
    (item: unknown): item is { id: string; label: string } =>
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

export function decodeProviderStatus(value: unknown): ProviderStatus {
  if (!Array.isArray(value)) throw new Error('provider status is invalid');
  return value.map((item: unknown) => {
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
  const files = value.files.map((item: unknown): ChangeFile => {
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
      ...(validPreviewStatus ? { preview_status: previewStatus } : {}),
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
    ...(typeof value.undo_available === 'boolean' ? { undo_available: value.undo_available } : {}),
    ...(typeof value.truncated === 'boolean' ? { truncated: value.truncated } : {}),
    ...(isRecord(value.html_previews)
      ? {
          html_previews: Object.fromEntries(
            Object.entries(value.html_previews).filter(
              (entry): entry is [string, string] => typeof entry[1] === 'string',
            ),
          ),
        }
      : {}),
    ...(isRecord(value.verification) ? { verification: decodeChangeVerification(value.verification) } : {}),
    ...(Array.isArray(value.conflicts) && value.conflicts.every((item: unknown) => typeof item === 'string')
      ? { conflicts: value.conflicts }
      : {}),
  };
}

function decodeChangeVerification(value: Record<string, unknown>): ChangeVerification {
  const failure = value.failed_check;
  const result: ChangeVerification = {};
  if (typeof value.summary === 'string') result.summary = value.summary;
  if (typeof value.tone === 'string') result.tone = value.tone;
  if (typeof value.checks_passed === 'boolean') result.checks_passed = value.checks_passed;
  if (typeof value.trust === 'string') result.trust = value.trust;
  if (typeof value.integrity === 'string') result.integrity = value.integrity;
  if (
    isRecord(failure) &&
    typeof failure.command === 'string' &&
    typeof failure.cwd === 'string' &&
    typeof failure.exit_code === 'number'
  ) {
    result.failed_check = { command: failure.command, cwd: failure.cwd, exit_code: failure.exit_code };
    const output = failure.output;
    if (
      isRecord(output) &&
      typeof output.session_id === 'string' &&
      typeof output.run_id === 'string' &&
      typeof output.execution_id === 'string'
    ) {
      result.failed_check.output = {
        execution_id: output.execution_id,
        session_id: output.session_id,
        run_id: output.run_id,
      };
    }
  }
  return result;
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
    if (!Array.isArray(item) || item.some((entry: unknown) => typeof entry !== 'string'))
      throw new Error(`research ${key} is invalid`);
    return item;
  };
  const records = (key: string): Array<Record<string, unknown>> | undefined => {
    const item = value[key];
    if (item === undefined) return undefined;
    if (!Array.isArray(item) || item.some((entry: unknown) => !isRecord(entry)))
      throw new Error(`research ${key} is invalid`);
    return item;
  };
  let coverage: ResearchPayload['coverage'];
  if (value.coverage !== undefined) {
    if (!isRecord(value.coverage)) throw new Error('research coverage is invalid');
    const { queries, skipped_results } = value.coverage;
    if (queries !== undefined && (!Array.isArray(queries) || !queries.every(isString)))
      throw new Error('research coverage queries are invalid');
    if (skipped_results !== undefined && (!Array.isArray(skipped_results) || !skipped_results.every(isRecord)))
      throw new Error('research skipped results are invalid');
    coverage = { queries, skipped_results };
  }
  return {
    restoreable: typeof value.restoreable === 'boolean' ? value.restoreable : undefined,
    synthesis_id: typeof value.synthesis_id === 'string' ? value.synthesis_id : undefined,
    sources_read: typeof value.sources_read === 'number' ? value.sources_read : undefined,
    notes_created: strings('notes_created'),
    notes_updated: strings('notes_updated'),
    source_urls: strings('source_urls'),
    queries: strings('queries'),
    counterpoints: strings('counterpoints'),
    quality_warnings: strings('quality_warnings'),
    search_results: records('search_results'),
    opened_sources: records('opened_sources'),
    citation_map: records('citation_map'),
    evidence_items: records('evidence_items'),
    coverage,
  };
}

function isString(value: unknown): value is string {
  return typeof value === 'string';
}

function isTaskStatus(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    ['pending', 'running', 'waiting', 'completing', 'completed', 'failed', 'cancelled'].includes(value)
  );
}

export function decodeTaskStates(value: unknown): NonNullable<RunState['task_states']> {
  if (!Array.isArray(value)) throw new Error('task states are invalid');
  return value.map((row: unknown) => {
    if (!isRecord(row) || !nonempty(row.run_id) || !nonempty(row.session_id)) throw new Error('task state is invalid');
    if (!sequence(row.task_revision)) throw new Error('task revision is invalid');
    if (!isTaskStatus(row.task_status)) throw new Error('task status is invalid');
    return {
      run_id: row.run_id,
      session_id: row.session_id,
      task_revision: row.task_revision,
      task_status: row.task_status,
      ...(typeof row.stop_reason === 'string' ? { stop_reason: row.stop_reason } : {}),
      ...(sequence(row.task_change_count) ? { task_change_count: row.task_change_count } : {}),
    };
  });
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
  if (value.task_states !== undefined) state.task_states = decodeTaskStates(value.task_states);
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
      if (!Array.isArray(item) || item.some((entry: unknown) => typeof entry !== 'string'))
        throw new Error('run state research_restore_runs is invalid');
      state.research_restore_runs = item;
    }
  }
  if (value.active_runs !== undefined) {
    if (!Array.isArray(value.active_runs)) throw new Error('run state active_runs is invalid');
    state.active_runs = value.active_runs.map((entry: unknown) => {
      if (!isRecord(entry) || typeof entry.run_id !== 'string' || typeof entry.session_id !== 'string')
        throw new Error('run state active run is invalid');
      return {
        run_id: entry.run_id,
        session_id: entry.session_id,
        ...(typeof entry.run_status === 'string' ? { run_status: entry.run_status } : {}),
        ...(typeof entry.project === 'string' || entry.project === null ? { project: entry.project } : {}),
        ...(typeof entry.provider === 'string' ? { provider: entry.provider } : {}),
        ...(typeof entry.started_at_ms === 'number' ? { started_at_ms: entry.started_at_ms } : {}),
      };
    });
  }
  for (const key of ['last_shell_result', 'last_terminal_event', 'pending_event'] as const) {
    const item = value[key];
    if (item === undefined || item === null) continue;
    const event = decodeServerEvent(item);
    const allowed =
      key === 'last_shell_result'
        ? ['shell_result']
        : key === 'last_terminal_event'
          ? ['task_done']
          : ['shell_request', 'teach_request', 'user_question'];
    if (!allowed.includes(event.type)) throw new Error(`Invalid run state event ${key}`);
    state[key] = event;
  }
  if (value.shell_approvals !== undefined) {
    if (!Array.isArray(value.shell_approvals)) throw new Error('Invalid approval list');
    state.shell_approvals = value.shell_approvals.map(decodeApprovalState);
  }
  return state;
}

function decodeChangesSummary(value: unknown): NonNullable<ServerPayload['changes']> {
  if (
    !isRecord(value) ||
    !sequence(value.changed_count) ||
    typeof value.mode !== 'string' ||
    !Array.isArray(value.files)
  )
    throw new Error('invalid event changes summary');
  const files = value.files.map((file: unknown) => {
    if (!isRecord(file) || typeof file.path !== 'string' || typeof file.status !== 'string')
      throw new Error('invalid event change file');
    return { path: file.path, status: file.status };
  });
  return { changed_count: value.changed_count, mode: value.mode, files };
}

export function decodeServerEvent(raw: unknown): ServerEvent {
  if (!isRecord(raw) || typeof raw.type !== 'string' || !raw.type) throw new Error('server event is invalid');
  if (!publicEventKind(raw.type)) throw new UnknownServerEvent(raw.type);
  const spec = publicEventFields[raw.type];
  const value: Record<string, unknown> = {};
  for (const field of Object.keys(spec.fields)) {
    if (Object.hasOwn(raw, field)) value[field] = raw[field];
  }
  if ('required' in spec && Object.keys(spec.required).some((field) => value[field] === undefined))
    throw new Error(`server event ${raw.type} is missing required fields`);
  if (!validateServerFields(value, raw.type)) throw new Error('invalid public event');
  return value;
}

function validateServerFields(
  value: Record<string, unknown>,
  type: PublicEventKind,
): value is Record<string, unknown> & ServerEvent {
  if (value.type !== type) return false;
  if (value.task_revision !== undefined && !sequence(value.task_revision)) throw new Error('task revision is invalid');
  if (value.task_status !== undefined && !isTaskStatus(value.task_status)) throw new Error('task status is invalid');
  if (
    type === 'durable_commit' &&
    value.entry_ids !== undefined &&
    (!Array.isArray(value.entry_ids) || !value.entry_ids.every(nonempty))
  )
    throw new Error('durable commit entries are invalid');
  if (
    value.replaced_run_ids !== undefined &&
    (!Array.isArray(value.replaced_run_ids) ||
      !value.replaced_run_ids.every((id: unknown) => typeof id === 'string' && id.length > 0))
  )
    throw new Error('replaced run identities are invalid');
  if (value.path_generation !== undefined && !sequence(value.path_generation))
    throw new Error('history path generation is invalid');
  if (value.revision !== undefined && !sequence(value.revision)) throw new Error('history revision is invalid');
  const stringFields = [
    'edited_entry_id',
    'session_id',
    'run_id',
    'task_id',
    'execution_id',
    'status',
    'text',
    'tool_id',
    'call_id',
    'parent_call_id',
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
    'reasoning',
    'note',
    'activity',
    'command',
    'output',
    'result',
    'phase',
    'detail',
    'project',
    'cwd',
    'command_sha256',
    'risk_label',
    'risk_title',
    'risk_detail',
    'final_delivery',
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
    'detail_truncated',
    'session_rule_available',
    'project_rule_available',
  ];
  for (const field of booleanFields) {
    if (field in value && value[field] !== undefined && typeof value[field] !== 'boolean')
      throw new Error(`server event field ${field} is invalid`);
  }
  for (const field of [
    'turn',
    'turns',
    'max_turns',
    'command_chars',
    'retry_after',
    'elapsed_ms',
    'started_at_ms',
    'output_bytes',
    'output_stored_bytes',
  ]) {
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
      ['action', 'kind', 'provider', 'stage'].some((field) => {
        const failure = value.provider_failure;
        return isRecord(failure) && failure[field] !== undefined && typeof failure[field] !== 'string';
      }))
  ) {
    throw new Error('server event provider_failure is invalid');
  }
  if (value.failure !== undefined && !isFailureInfo(value.failure)) throw new Error('server event failure is invalid');
  if (value.preview !== undefined && !isToolPreview(value.preview)) throw new Error('server event preview is invalid');
  if (value.providers !== undefined) value.providers = decodeProviderStatus(value.providers);
  if (value.cursor !== undefined && typeof value.cursor !== 'number' && typeof value.cursor !== 'string')
    throw new Error('invalid resync cursor');
  if (value.research !== undefined) value.research = decodeResearchPayload(value.research);
  if (value.changes !== undefined) value.changes = decodeChangesSummary(value.changes);
  if (type === 'hello') {
    if (!nonempty(value.database_id) || !sequence(value.after_commit_seq))
      throw new Error('durable cursor handshake is invalid');
  }
  if (type === 'durable_commit') {
    if (!nonempty(value.session_id) || !sequence(value.commit_seq, 1)) throw new Error('durable commit is invalid');
  }
  if (type === 'reply_stream') {
    const { session_id, run_id, stream_id, state, text } = value;
    if (
      !nonempty(session_id) ||
      !nonempty(run_id) ||
      !nonempty(stream_id) ||
      !sequence(value.sequence, 1) ||
      typeof text !== 'string' ||
      (state !== 'partial' && state !== 'complete' && state !== 'interrupted') ||
      (value.done !== undefined && typeof value.done !== 'boolean') ||
      (value.delta !== undefined && typeof value.delta !== 'string')
    )
      throw new Error('reply stream is invalid');
  }
  if (
    type === 'conversation_input' &&
    (!isInputRecord(value.input) || value.session_id !== value.input.request.session_id)
  )
    throw new Error('conversation input is invalid');
  if (
    (type === 'user_question' || type === 'user_question_state') &&
    (!isQuestionRecord(value.question) || value.session_id !== value.question.session_id)
  )
    throw new Error('user question is invalid');
  if (type === 'shell_approval_status') decodeApprovalState(value);
  return true;
}
