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
import type { ToolDisplay as importToolDisplay } from '../render';
export interface ModelChoice {
  enabled: boolean;
  models: string[];
}
export interface ModelUse {
  provider: string;
  model?: string;
}
export interface ModelSource {
  id: string;
  models: Array<{ id: string; name?: string }>;
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
export type AttemptState = 'sending' | 'running' | 'done' | 'paused' | 'failed';
export interface RequestAttempt {
  runId: string;
  state: AttemptState;
  error?: string;
  sendFailure?: string;
  runOwner?: string;
}
export interface Message extends importToolDisplay {
  source?: { actor_id: string; name: string; kind: string };
  entryId?: string;
  elapsedMs?: number;
  queuedInputId?: string;
  streamId?: string;
  streamSequence?: number;
  streamState?: 'partial' | 'complete' | 'interrupted';
  finalAnswer?: boolean;
  turnId?: string;
  contentRole?: 'commentary' | 'final';
  answerId?: string;
  answerPartIndex?: number;
  failure?: {
    schema_version: 1;
    code: string;
    category: string;
    scope: 'tool' | 'run' | 'request' | 'render';
    phase: string;
    effect: 'not_started' | 'applied' | 'unknown' | 'not_applicable';
    session_id?: string;
    run_id?: string;
    call_id?: string;
    execution_id?: string;
    source_id?: string;
    incident_id: string;
    resource?: { kind: string; path?: string; id?: string };
    diagnostic?: string;
    retry_after?: number;
  };
  type: string;
  approval?: ApprovalState;
  approvalSubmission?: 'sending' | 'checking' | 'unconfirmed';
  approvalError?: string;
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
  truncated?: boolean;
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
  isDirtySinceBoot?: () => boolean;
  setDirtySinceBoot?: (value: boolean) => void;
  bootHadMeaningfulUiState?: () => boolean;
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
  setStatus(text: string, kind: string): void;
  applyRunState(data: RunState): void;
  handleServerEvent(data: import('../domain/protocol').ServerEvent): void;
  acceptRun(runId: string | null, sessionId: string | null): void;
  setProviderBusy(value: boolean): void;
  updateSend(): void;
  updateComposerContext(): void;
  refreshProviderStatus(immediate: boolean): void;
}
export interface RunState {
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
  }>;
}
