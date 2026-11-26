export const APPROVAL_DECISIONS = ['pending', 'allow', 'deny', 'cancelled'] as const;

export const APPROVAL_EXECUTIONS = [
  'not_started',
  'not_required',
  'queued',
  'running',
  'settled',
  'uncertain',
] as const;

export const APPROVAL_CONTINUATIONS = [
  'waiting_for_result',
  'queued',
  'not_required',
  'resuming',
  'started',
  'failed',
  'unavailable',
  'cancelled',
] as const;

export type ApprovalDecision = (typeof APPROVAL_DECISIONS)[number];
export type ApprovalExecution = (typeof APPROVAL_EXECUTIONS)[number];
export type ApprovalContinuation = (typeof APPROVAL_CONTINUATIONS)[number];

export interface ApprovalState {
  type: 'shell_approval_status';
  id: string;
  session_id: string;
  run_id?: string;
  source_run_id?: string;
  continuation_run_id?: string;
  execution_id?: string;
  decision: ApprovalDecision;
  execution: ApprovalExecution;
  continuation: ApprovalContinuation;
  revision: number;
}
import type { ToolDisplay as importToolDisplay } from '../render/render';
export interface ModelChoice {
  enabled: boolean;
  models: string[];
}
export interface ModelUse {
  provider: string;
  model?: string;
}
export interface ProviderModel {
  id: string;
  name?: string;
  protocol?: string;
  efforts?: string[];
  thinking_toggle?: boolean;
  thinking_default_allowed?: boolean;
  availability?: 'available' | 'unavailable' | 'capability_unknown';
  unavailable_reason?: string;
  trial?: boolean;
}
export type ConnectionAction = 'login' | 'reopen_login' | 'cancel_login' | 'logout' | 'clear_data';
export interface ConnectionAuthentication {
  kind: 'oauth_device';
  state: string;
  revision: number;
  attempt_id?: string | null;
  expires_at?: number | null;
  user_code?: string | null;
  error_code?: string | null;
  actions: ConnectionAction[];
}
export interface ModelSource {
  id: string;
  label?: string;
  discoverable?: boolean;
  connection_editor?: boolean;
  models: ProviderModel[];
  authentication?: ConnectionAuthentication;
  state?: string;
  unavailable_models?: Array<{ id: string; name?: string; status?: string }>;
  stale?: boolean;
  error?: string;
  updated_at?: number;
  checked_at?: number;
}
export interface ModelState {
  preferences: { revision: number; sources: Record<string, ModelChoice> };
  sources: ModelSource[];
  in_use: ModelUse[];
}
export interface Draft {
  text: string;
  start: number;
  end: number;
  direction: 'none' | 'forward' | 'backward';
  revision: number;
}
export interface AttachmentRef {
  id: string;
  name: string;
  media_type: string;
  size: number;
  sha256: string;
}
export type AttemptState = 'sending' | 'running' | 'waiting' | 'done' | 'paused' | 'failed';
export interface RequestAttempt {
  edit?: boolean;
  runId: string;
  state: AttemptState;
  error?: string;
  sendFailure?: string;
  runOwner?: string;
}
interface MessageFields extends importToolDisplay {
  supersededRunIds?: string[];
  attachments?: AttachmentRef[];
  source?: { actor_id: string; name: string; kind: string };
  entryId?: string;
  elapsedMs?: number;
  undoAvailable?: boolean;
  additions?: number;
  deletions?: number;
  queuedInputId?: string;
  streamId?: string;
  streamSequence?: number;
  streamState?: 'partial' | 'complete' | 'interrupted';
  finalAnswer?: boolean;
  turnId?: string;
  contentRole?: 'commentary' | 'final';
  answerId?: string;
  answerPartIndex?: number;
  approval?: ApprovalState;
  approvalSubmission?: 'sending' | 'checking' | 'unconfirmed';
  approvalError?: string;
  permissionScope?: 'session' | 'project';
  permissionSaveScope?: 'session' | 'project';
  permissionSaveError?: string;
  sessionRuleAvailable?: boolean;
  projectRuleAvailable?: boolean;
  id?: string;
  text?: string;
  sessionId?: string;
  runId?: string;
  executionId?: string;
  eventKey?: string;
  toolKey?: string;
  kind?: string;
  path?: string;
  command?: string;
  result?: string;
  activity?: string;
  error?: boolean;
  pending?: boolean;
  n?: string | number;
  turn?: string | number;
  note?: string;
  status?: string;
  state?: string;
  exitCode?: number | null;
  shellStatus?: string;
  approved?: boolean;
  output?: string;
  outputTruncated?: boolean;
  requestId?: string;
  sendFailure?: string;
  runOwner?: string;
  attempts?: RequestAttempt[];
  payload?: unknown;
  reasoning?: string;
  thinking?: boolean;
  changed?: boolean;
  project?: string;
  count?: number;
  files?: Array<{ path?: string; status?: string; additions?: number; deletions?: number }>;
  commandSha256?: string;
  commandChars?: number;
  commandTruncated?: boolean;
  cwd?: string;
  riskLabel?: string;
  riskTitle?: string;
  riskDetail?: string;
  toolName?: string;
  confidence?: number;
  source_url?: string;
  requested_url?: string;
  final_url?: string;
  title?: string;
  url?: string;
  note_id?: string;
  excerpt?: string;
}
type MessageIdentity = 'id' | 'entryId' | 'sessionId' | 'runId' | 'eventKey' | 'text' | 'turnId';
type MessageVariant<Kind extends string, Keys extends keyof MessageFields = never> = Kind extends string
  ? { type: Kind } & Pick<MessageFields, MessageIdentity | Keys> &
      Partial<Record<Exclude<keyof MessageFields, MessageIdentity | Keys>, never>>
  : never;

type ToolFields = keyof importToolDisplay;
type CommandFields =
  | ToolFields
  | 'executionId'
  | 'status'
  | 'state'
  | 'exitCode'
  | 'shellStatus'
  | 'approved'
  | 'output'
  | 'project'
  | 'commandSha256'
  | 'commandChars'
  | 'commandTruncated'
  | 'cwd'
  | 'approval'
  | 'approvalSubmission'
  | 'approvalError'
  | 'permissionScope'
  | 'permissionSaveScope'
  | 'permissionSaveError'
  | 'sessionRuleAvailable'
  | 'projectRuleAvailable'
  | 'riskLabel'
  | 'riskTitle'
  | 'riskDetail';

export type Message =
  | MessageVariant<'user', 'attachments' | 'source' | 'queuedInputId' | 'attempts' | 'runOwner' | 'sendFailure'>
  | MessageVariant<
      'asst',
      'streamId' | 'streamSequence' | 'streamState' | 'finalAnswer' | 'contentRole' | 'answerId' | 'answerPartIndex'
    >
  | MessageVariant<
      'tool' | 'tool_pending',
      | ToolFields
      | 'changed'
      | 'status'
      | 'payload'
      | 'confidence'
      | 'source_url'
      | 'requested_url'
      | 'final_url'
      | 'title'
      | 'url'
      | 'note_id'
      | 'excerpt'
    >
  | MessageVariant<'shell_request' | 'shell_result' | 'shell_approval_status', CommandFields>
  | MessageVariant<'turn' | 'thinking', 'n' | 'turn' | 'reasoning' | 'thinking' | 'note'>
  | MessageVariant<
      'request_status' | 'request_activity',
      'requestId' | 'state' | 'sendFailure' | 'runOwner' | 'supersededRunIds'
    >
  | MessageVariant<'changes', 'count' | 'files' | 'project' | 'changed' | 'undoAvailable' | 'additions' | 'deletions'>
  | MessageVariant<
      'done' | 'research_done' | 'err' | 'pause' | 'limit' | 'review',
      'payload' | 'elapsedMs' | 'failure' | 'status' | 'changed' | 'project' | 'sendFailure' | 'runOwner'
    >
  | MessageVariant<
      'info' | 'teach' | 'user_question' | 'run_state' | 'local_update_warning',
      'state' | 'payload' | 'failure' | 'status' | 'note' | 'elapsedMs'
    >;

export interface ResearchRun {
  runId: string;
  restoreable: boolean;
  synthesisId?: string;
  notesCreated: string[];
  notesUpdated: string[];
  sourceUrls: string[];
  sourcesRead?: number;
  queries: string[];
  searchResults: Array<Record<string, unknown>>;
  openedSources: Array<Record<string, unknown>>;
  coverage: { queries?: string[]; skipped_results?: Array<Record<string, unknown>> };
  citationMap: Array<Record<string, unknown>>;
  evidenceItems: Array<Record<string, unknown>>;
  counterpoints: string[];
  qualityWarnings: string[];
  receipt?: string;
  createdAt?: number;
}
export interface ResearchPayload {
  restoreable?: boolean;
  synthesis_id?: string;
  notes_created?: string[];
  notes_updated?: string[];
  source_urls?: string[];
  sources_read?: number;
  queries?: string[];
  search_results?: Array<Record<string, unknown>>;
  opened_sources?: Array<Record<string, unknown>>;
  coverage?: { queries?: string[]; skipped_results?: Array<Record<string, unknown>> };
  citation_map?: Array<Record<string, unknown>>;
  evidence_items?: Array<Record<string, unknown>>;
  counterpoints?: string[];
  quality_warnings?: string[];
}
export interface ChangeFile {
  path: string;
  status?: string;
  additions?: number;
  deletions?: number;
  preview_status?: ChangePreviewStatus;
}
export type ChangePreviewStatus = 'text' | 'binary' | 'too_large' | 'read_error' | 'symlink' | 'invalid' | 'missing';
export interface ChangeFilePreview {
  ok: boolean;
  path: string;
  status: ChangePreviewStatus;
  diff?: string;
  size?: number;
  truncated?: boolean;
  error?: string;
}
export interface ChangesData {
  ok: boolean;
  project?: string;
  files: ChangeFile[];
  diff: string;
  mode?: string;
  error?: string;
  conflicts?: string[];
  changed_count?: number;
  undo_available?: boolean;
  truncated?: boolean;
  html_previews?: Record<string, string>;
  verification?: ChangeVerification;
}
export interface ChangeVerification {
  summary?: string;
  tone?: string;
  checks_passed?: boolean;
  trust?: string;
  integrity?: string;
  failed_check?: {
    command: string;
    cwd: string;
    exit_code: number;
    output?: import('../runs/execution_output').ExecutionOutputScope;
  };
}
export interface RunData {
  ok: boolean;
  available?: boolean;
  details?: { title?: string; rows?: Array<{ label?: unknown; value?: unknown; tone?: string }>; warnings?: unknown[] };
  error?: string;
  message?: string;
  run_id?: string;
  session_id?: string;
  status?: string;
}
export interface RestoreChangesResult {
  ok?: boolean;
  run?: ResearchRun;
  error?: string;
  message?: string;
  restored?: unknown[];
  conflicts?: string[];
}
export interface ResearchRestoreResult {
  ok: boolean;
  ownerSessionId: string;
  run: ResearchRun;
  message: string;
  restored: unknown[];
}
export interface Session {
  id: string;
  title: string;
  draft: Draft;
  messages: Message[];
  projectId?: string | null;
  provider?: string;
  research?: boolean;
  modelSelection?: {
    connection_id?: string;
    base_url?: string;
    model?: string;
    name?: string;
    effort?: string | null;
    thinking?: boolean | null;
    efforts?: Record<string, string>;
    reasoning_reselect_required?: boolean;
  };
  _toolKeys?: Set<string>;
  _eventKeys?: Set<string>;
  _terminalRunSet?: Set<string>;
  terminalRuns?: string[];
  titleRenamed?: boolean;
  createdAt?: number;
  researchRuns?: ResearchRun[];
}
export interface Project {
  id: string;
  name: string;
  path: string;
  expanded?: boolean;
  createdAt?: number;
}
export interface UiDeps {
  getSessions?: () => Session[];
  setSessions?: (value: Session[]) => void;
  getProjects?: () => Project[];
  setProjects?: (value: Project[]) => void;
  getActiveId?: () => string;
  setActiveId?: (value: string) => void;
  getUpdatedAt?: () => number;
  setUpdatedAt?: (value: number) => void;
  getRevision?: () => number;
  setRevision?: (value: number) => void;
  renderSidebar?: () => void;
  renderChat?: () => void;
  updateComposerContext?: () => void;
  syncProviderUI?: (provider: string) => void;
  currentProviderId?: () => string;
  receiptSummary?: (receipt: unknown) => string;
  persistActive?: () => void;
  persistActiveNow?: () => void;
  activeSession?: () => Session | undefined;
}
export interface SseDeps {
  forRun(sessionId: string, runId: string): import('../runs/requests').RequestMatch | null;
  setStatus(text: string, kind: string): void;
  applyRunState(data: RunState): void;
  handleServerEvent(data: import('../domain/protocol').ServerEvent, cursor: string): void | Promise<void>;
  acceptRun(runId: string | null, sessionId: string | null): void;
  setProviderBusy(value: boolean): void;
  updateSend(): void;
  updateComposerContext(): void;
  refreshProviderStatus(immediate: boolean): void;
}
export interface RunState {
  task_states?: Array<{
    run_id: string;
    session_id: string;
    task_revision: number;
    task_status: string;
    stop_reason?: string;
    task_change_count?: number;
  }>;
  response_ack_run_id?: string;
  shell_approvals?: ApprovalState[];
  busy?: boolean;
  run_id?: string;
  session_id?: string;
  run_status?: string;
  last_shell_result?: import('../domain/protocol').ServerEvent;
  last_terminal_event?: import('../domain/protocol').ServerEvent;
  pending_event?: import('../domain/protocol').ServerEvent;
  research_restore_runs?: string[];
  active_runs?: Array<{
    run_id: string;
    session_id: string;
    run_status?: string;
    project?: string | null;
    provider?: string;
    started_at_ms?: number;
  }>;
}
