# Changelog

[中文版本](CHANGELOG.zh-CN.md)

## 0.5.4 - Safe Tool Replay v1

- Safe Tool Replay & Resumption Recovery v1:
  - Added `codey.runtime.safe_tool_replay`: pure data validation and candidate projection module without dependencies on execution runtime or agents layer. Defines candidate data structures and strict replay argument normalization (`validate_replay_args()`, `replay_args_for_tool_call()`, `candidate_from_effect()`), requiring zero alias rewrites and zero repairs.
  - Narrow Replayable Whitelist: defined `REPLAYABLE_SAFE_TOOL_NAMES = frozenset({"read", "ls", "search", "references"})`. While `project_facts` and `project_map` remain classified as safe, they are not connected to production replay executor in 0.5.4; mutating actions (`edit`, `write`, `run`, `shell`, `knowledge_write`) and provider sends/repair rounds are strictly never replayed.
  - Runtime Effect Records Extension: `RuntimeEffectIntent` records canonical `replay_args` strictly for replayable safe tools; `RuntimeEffectSettlement` records `replay_count` (int) and `replayed_from_effect_id` (str, must match `effect_id`). Duplicate settlement idempotency checking incorporates replay metadata.
  - Recovery Summary Projection: `RecoverySummary` tracks `replayed_reads` and `replayed_searches` and renders user-facing rows (`Read action was recovered`, `Search action was recovered`) in run details. Session log compaction preserves replayed intents and settlements.
  - Agent Execution Layer Refactoring: extracted `execute_information_tool_call()` and `evaluate_tool_call_policy_for()` in `tool_execution.py` for shared execution without duplicate code paths. Extracted `tool_result_from_outcome()` and updated `record_tool_call_intent()` to persist canonical `replay_args`.
  - Seamless Resumption Loop: defined `RecoveredToolOutcome` in `codey.agents.request` and added `recovered_tool_outcomes` to `AgentRequest`. Updated `codey.agents.loop`: `_run_loop()` supports `start_turn: int = 1`; `run()` consumes recovered tool outcomes, updates session state, formats tool results for the model, and resumes conversational turn loop starting from `max(turn) + 1`.
  - Operation Gate Upgrade: upgraded `_settle_pending_effects_for_resume()` to `_recover_effects_for_resume()` in `task_run.py`, validating project paths, writer context, and policy approval before replaying safe tool calls. Wired `recovered_tool_outcomes` through `RunFrame` and consumed/cleared in `_run_one_writer_attempt()`. Unsafe, provider, repair, or invalid effects fail-closed to synthetic `interrupted` settlements.
  - Testing & Verification: added unit tests in `tests/test_safe_tool_replay.py`, smoke harness `tests/manual/safe_tool_replay_smoke.py` (`--self-test`), updated `test_tool_replay_policy.py`, `test_runtime_effect_records.py`, and `test_agent_effect_sandwich.py`. All 3,387 tests in the full pytest suite passed.

## 0.5.3 - Shared Tool Argument Repair + Protocol Friction Reduction v1


- Tool Argument Canonicalization & Protocol Friction Reduction v1:
  - Added `codey.tool_args_repair` providing pure functions for lexical path normalization, bounded positive integer parsing, and equivalent field alias rewriting for canonical runtime tools (`edit`, `read`, `ls`, `search`, `references`, `run`, `shell`).
  - Path normalization strictly enforces project-relative paths, folding `.` and safe `..` while rejecting Windows drive letters (`C:\`), UNC paths (`//share`), root paths (`/`), and parent directory traversal escaping project root (`../`). Missing optional paths default to `.`, but explicit blank/null paths fail closed. Internal path whitespace is preserved; boundary whitespace is recorded as path normalization.
  - Conflicting alias keys within the same semantic group (e.g. `old_string` + `old`, `command` + `cmd`, `query` + `pattern`, `symbol` + `name`, `path` + `cwd`) fail closed immediately with `ToolArgsRepairError`.
  - Unknown argument fields fail closed instead of being silently dropped, and unsupported runtime tools fail closed immediately.
  - Text arguments (`query`, `symbol`, `command`) strictly require non-blank string types; non-string and whitespace-only values fail closed.
  - Equivalent parameter aliases:
    - `edit`: `old` / `search` / `before` -> `old_string`; `replace` / `replacement` / `after` / `new` -> `new_string`.
    - `edit`: missing `new_string` fails closed; only an explicit empty `new_string`/alias represents deletion.
    - `edit`: `content` strictly requires a string type, avoiding silent data loss on non-string inputs.
    - `edit`: wraps single replacement object directly passed in `replacements`.
    - `edit`: parses JSON string `replacements` safely; invalid JSON fails closed.
    - `read`: coerces numeric string `offset` / `limit` to bounded positive integers; bool, float, null, and invalid values are strictly rejected.
    - `search`: `pattern` -> `query`.
    - `references`: `name` -> `symbol`.
    - `run` / `shell`: `cmd` -> `command` (never guesses command content).
    - `write` / `write_file` / `create_file` remain strictly unknown tools without hidden mutation aliases.
  - Slimmed `codey.protocols.json_codec`: `_tool_call()` delegates all parameter parsing and validation to `normalize_tool_args()`, eliminating duplicated validation branches; `read_files` and `parallel` reuse the shared normalizer, and stale private `_parse_object()` / `_text()` helpers were removed.
  - Bounded telemetry: `ToolPlan` carries `alias_rewrite_count` and `arg_repair_counts` accumulated strictly per accepted call after deduplication; `AgentLoop` forwards telemetry to `RunTrace.record_protocol_valid_turn`; `RunTrace` records and sanitizes repair counts without persisting raw paths, commands, queries, or prompt text.
  - Smoke, deterministic A/B, live-provider, and dialect-pressure harnesses: `tests/manual/tool_args_repair_smoke.py` covers dialect and fail-closed cases; `tests/manual/tool_args_repair_simulated_ab.py` runs the deterministic 0.5.2-vs-0.5.3 parser comparison; `tests/manual/tool_args_repair_live_ab.py` is the natural production-agent-loop provider probe; `tests/manual/tool_args_repair_dialect_pressure_ab.py` verifies production-loop absorption when prompts deliberately pressure provider-shaped argument variants.
  - Natural live-provider A/B on DeepSeek, MiMo, and GLM completed with no observed turn savings in the tiny clean-schema sample: baseline and candidate both finished 2/2 cases per provider with 7 total turns, zero protocol errors, zero repair prompts, and zero alias rewrites. This confirms no regression for the sampled path; deterministic dialect coverage remains the evidence for savings when aliases actually appear.
  - MiMo dialect-pressure live A/B completed 2/2 baseline and 2/2 candidate cases. Candidate reduced total turns from 9 to 8 and repair prompts from 2 to 0, with 2 numeric-string coercions recorded; the edit/run pressure case stayed canonical on MiMo and did not exercise `old`/`new` or `cmd`.
- Provider reliability:
  - GLM browser start and new-chat URLs now use the root `https://chatglm.cn/` entry instead of the `main/alltoolsdetail` deep link, which can trigger verification. No deep-link fallback was added.

## 0.5.2 - Effect Intent / Settlement + Tool Replay Policy v1

- Effect Intent / Settlement Sandwich and Replay Policy v1:
  - Added `codey.runtime.replay_policy` introducing `ReplayClass` (safe/unsafe) and `ReplayDecision`. Read-only tools (`read`, `ls`, `search`, `references`, `project_facts`, `project_map`) are classified as safe and produce retryable recovery projections; mutating actions (`edit`, `write`, `shell`, `run`, `knowledge_write`) and unknown tools are classified as unsafe; `run` command content is unconditionally classified as unsafe; provider sends and repair rounds are unsafe.
  - Added `codey.runtime.effect_records` backed by the single durable fact source `RuntimeSessionLog`, introducing `RuntimeEffectStore`, `RuntimeEffectIntent`, `RuntimeEffectSettlement`, and `RecoverySummary`. External effects follow an explicit `record intent -> execute real effect -> record settlement` sandwich.
  - Unique effect identity: introduced `new_effect_id(category, run_id)` generating globally unique ids to eliminate collisions across turns and resume attempts.
  - Strict Schema & Fail-Closed validation: `from_payload()` enforces strict type/length constraints, requires `session_id`, `lane`, `operation_id`, `turn`, `tool_index`, and canonical `ref`, rejects unknown effect payload keys, enforces string type on enum fields (`effect_category`, `replay_class`, `status`, `sent_state`) before membership tests via `_require_enum_str` to eliminate raw TypeErrors, and rejects missing semantic fields without permissive fallbacks; `record_intent()` and `record_settlement()` explicitly validate that dataclass `session_id`, `run_id`, `lane`, and `operation_id` strictly match the target run boundary; `record_settlement()` validates category consistency and rejects conflicting duplicate settlements while idempotently returning identical duplicates; `load_effects()` matches by operation and run boundaries, strictly verifies entry/payload `session_id`, `run_id`, `lane`, and `operation_id` alignment without silently dropping corrupted entries, and parses in strict chronological order (rejects duplicate intents, orphan settlements, and conflicting settlements).
  - Pre-Gated Recovery & Full Lifecycle Clean Exit: unconfirmed effect recovery is pre-gated before any work-queue claiming, Ghost auto router decisions, or provider sends; recovery failures and operation start failures cleanly exit via `_fail_early()` with standard `task_done` error events, cleared `RunRegistry` busy states, and halted external execution; `_start_run_operation()` remains a single-purpose operation-state opener, while pending-effect recovery stays in the pre-gate; `complete_or_block_work_item()` consumes `GhostWorkItem | None` directly without permissive wrapper fallbacks or unused dead arguments.
  - Per-iteration safety: agent loop resets `effect_id = ""` per tool iteration so that intent recording failures fail closed without executing tools or settling previous effect ids.
  - Full prompt digest: Provider prompt `args_digest` hashes full prompt text without truncation.
  - Tool execution ordering: adjusted order to `execute_tool_call -> record_tool_outcome -> record_tool_call_settlement`; settlement is attempted in a `finally` after tool outcome recording so event callback failures do not leave a completed effect permanently pending; `record_settlement_safely()` guarantees logging errors never mask real outcomes/exceptions; removed obsolete `begin_tool_call()` plus future-only `ReplayDecision` payload/retry flags.
  - Crash recovery projection: on session resume, unconfirmed pending effects are projected and settled with synthetic `interrupted` records; `recovery_summary` only reports settled interrupted effects and ignores in-flight pending effects and normal provider errors to prevent false UI warnings.
  - Strict context hygiene: safe replay and synthetic interrupted states are not injected into model context; prompt, tool schema, provider routing, and model transcripts remain unchanged; raw payload bodies are never persisted.

## 0.5.1 - Task Runtime Finalization + Completion Repair Durability v1

- Runtime cold-start refactor:
  - Release-gate cleanup fixed the real HTTP dispatch path for queryless GET
    JSON endpoints (`/api/ui_state` and `/api/providers`) after the app/api
    split, and refreshed the browser/MoA smoke harnesses to patch the current
    `app.services` provider owner instead of stale server re-exports.
  - Removed the production `TaskFlow` concept and deleted
    `codey/operations/task_flow.py`. Server, headless, manual harnesses, and
    tests now enter through `codey.operations.task_entry.run_task_submission()`
    with a `TaskSubmission`; there is no compatibility shim or old/new switch.
  - Split the remaining task lifecycle into names that match ownership:
    `task_entry.py` wires submissions into `TaskRuntime`, `task_run.py` owns the
    non-business run lifecycle and `TaskRunDeps`, `mode_dispatch.py` chooses the
    operation function, and review/planning/Ghost post-turn work live in their
    own operation modules.
  - Split `AgentRunner` without changing its protocol behavior: JSON protocol
    repair helpers live in `codey.agents.protocol`, base prompt/context
    rendering lives in `codey.agents.context`, callers pass a single
    `AgentRequest`, and loop progress/verification/stagnation state is explicit
    instead of spread across a broad local-variable surface.
  - Split the agent loop by real owner boundaries. `codey.agents.runner` is the
    public entry/re-export surface; `codey.agents.state` owns
    `AgentLoopSession` and mutable loop state; `codey.agents.prompt_context`
    owns provider-send prompt assembly, context epoch binding, repair context
    admission, and coding-current-context injection;
    `codey.agents.verification_driver` owns verification candidates,
    freshness, reminders, and edit/run verification accounting; and
    `codey.agents.tool_execution` owns tool policy, dispatch, and result
    accounting. `codey.agents.loop` now keeps the turn loop, parse path, visible
    `continue` / `return` control flow, state transitions, and finish.
  - Reworked `codey.operations.project_completion_flow.run_project_mode()` into
    an explicit phase script over `_ProjectRun`: project context preparation,
    writer failover, review cycle, completion enforcement, and final receipt /
    facts / terminal projection now have separate function owners without
    `nonlocal` closure state.
  - Grouped `ProjectCompletionDeps` by stable access surface:
    `AgentAccess`, `PersistenceAccess`, `VerificationAccess`, `ReviewAccess`,
    and `RuntimeAccess`. This compresses the dependency surface without adding a
    `CompletionManager` or splitting project completion by line count.
  - Split the local HTTP app boundary into `codey.app.http_plumbing`,
    `codey.app.api`, and `codey.app.services`. The Handler now validates
    origin, parses HTTP, dispatches ordinary JSON endpoints through route
    tables, and keeps SSE as the streaming transport exception; review,
    consensus/audit/advisor, provider warmup, approved shell execution, and
    shell continuation prompts live in services and take `AppContext`
    explicitly.
  - Split app runtime state out of the HTTP server shell: run lifecycle,
    approval queues, provider sessions/health/order, conversation cache/store,
    knowledge rebuild single-flight, and Ghost sleep single-flight now live in
    dedicated app modules. The server now exposes an `AppContext` with
    product-facing coordination methods instead of `server.State` forwarding
    properties or old/new runtime switches.
  - Moved operation frame/work/hooks/outcome values into `codey.operations` and
    moved the plain chat operation plus prompt/local-context tracing helpers
    out of the task runner. Chat prompt assembly, consensus handoff, provider
    session settlement, and reply emission now have an operation owner.
  - Moved project completion execution into
    `codey.operations.project_completion_flow`: writer failover, completion
    proof evaluation, bounded repair admission, receipt/facts/memory writes,
    and analysis-run projection now live in the project-completion operation.
  - Split the task package boundary from execution: `codey.task` is a
    model-only submission boundary containing `TaskSubmission` and `TaskKind`;
    `TaskContract`, `TaskState`, and the old service facade were removed.
  - Reduced the Pi-style runtime kernel to only wired production facts: typed
    operation outcomes, operation contracts, a small scheduler, and an
    append-only session log with a fail-closed reducer. Future-only lane queues,
    suspended-operation scaffolding, `TaskRuntimePort`, tool-invocation log
    entries, and the unused `OperationKind` literal were deleted before
    release.
  - Moved completion verdict ownership into `codey.completion.engine`, including
    blocked-note vocabulary and the proof + edit-integrity evaluation pass.
    project completion consumes the engine instead of rebuilding that decision
    chain inline.
  - Moved terminal `task_done` event construction and terminal turn accounting
    into `codey.runtime.terminalizer`, so stop/error/done paths share one
    terminal projection.
  - Aligned runtime submission identity: TaskRuntime, RuntimeOperationStore,
    terminal settlement, and Run Details now share one
    `task:<hash(run_id)>` operation/lane for each reserved `run_*` id. The
    previous outer `runtime:<run_id>` operation semantics were removed before
    release, so one task has one runtime operation.
  - Runtime operation tracking stays explanatory and fail-open: if strict phase
    fact validation rejects a malformed proof projection, the user-visible task
    result is preserved and the operation projection stops at its last valid
    phase.
  - TaskRuntime's single task operation now settles from the user-visible
    terminal event: `done` is completed, `stopped` is aborted, `approval` is
    suspended, and every other stop reason is failed. A task that returns
    without a terminal outcome is recorded as failed instead of completed.
  - If runtime scheduling fails before the task executor is entered, the
    reserved app run slot is released. A broken first runtime-log append can no
    longer leave the UI permanently busy.
  - Runtime log `append_many()` rows now carry batch metadata. Readers ignore an
    incomplete final batch, and the next append trims that tail before writing,
    so a crash mid-batch cannot leave a permanent open lane.
  - Runtime logs compact under the same file lock before the 4 MB guard can
    brick a long-lived session. Compaction keeps the replay-equivalent spine:
    `operation_started`, the latest `run_phase` effect, and the terminal
    `operation_settled` row when present.
  - Runtime session-log validation now keeps a process-local entries +
    projection cache. `append_many()` still fails closed through the reducer,
    but hot phase commits load their current state from cached entries when the
    file size and `mtime_ns` stamp have not changed; same-size external
    rewrites, compaction, and deletion invalidate or rebuild the cache.
  - Added package-level architecture tests for the active runtime boundaries:
    runtime cannot import operations, agents, or Ghost; agents cannot import
    operations; completion cannot import app, providers, or operations; and
    `agents.loop` cannot directly import completion, toolchain, or workspace
    context-source internals that belong to owner modules.
  - Run Details now reads runtime operation state before ledger/trace checks, so
    an interrupted run can still show its quiet `Progress` row even if the
    ledger or trace was never written or has been cleaned up. Terminal runtime
    state can also provide minimal Work/Model details without showing a stale
    Progress row.
  - RunRegistry builds `/api/state` snapshots without invoking approval
    callbacks under its internal lock.
  - `AppContext()` without an explicit state home now gets an ephemeral
    runtime-session log, operation store, and workspace revision store, so tests
    and transient callers use the same runtime path without writing to the
    user's durable state home.
  - Workspace state tracking now binds verification freshness to the project
    filesystem state with `WorkspaceState(revision, fingerprint)`. Missing
    revision files start at the initial revision, but corrupt, invalid, or
    oversized revision state fails closed instead of rolling the monotonic
    identity back. Verification observations, checkpoint green checks, and
    completion proofs require both the current revision and the current bounded
    workspace fingerprint, so out-of-band edits to unrecorded files cannot
    silently reuse stale green checks. This is intentionally separate from
    `workspace/context_epoch.py`: context epochs identify prompt-source
    provenance, while workspace state identifies the file state a verification
    observation can support.
  - Research and hybrid terminal events now keep the original task turn budget
    in the runtime terminal snapshot, even when the research engine used fewer
    turns internally.
  - Split SSE subscriber queues, replay IDs, overflow markers, and replay-window
    checks into `codey.app.event_bus`; `State` only injects active run identity
    before emitting.
  - Added a shared Ghost JSONL event-log primitive and migrated signal, router,
    sleep, inbox, continuity, work queue, affinity, and Hebbian stores onto it.
    Corrupt or oversized reads remain observable, and strict transition stores
    keep their fail-closed mutation behavior through policy-specific bad-row
    handling.
  - Added a shared browser-provider stable-completion loop and migrated
    DeepSeek and StepFun onto it. Both drivers now use
    `ProviderSendContext.record_response()` instead of mutating `ctx.last`,
    keeping stable-response accounting centralized.
  - Project completion tests now target the operation module directly for
    analysis-run projection, repair constants, verification candidate
    selection, and writer failover ranking. Production no longer preserves
    private task-runner methods only as test patch points.
- Post-review cold-start cleanup:
  - A/B harness git-state capture now reads Git output as bytes, so untracked
    CJK filenames cannot trip Windows locale decoding before a full pytest run.
  - JSON-tool parsing now ignores `<think>...</think>` only outside JSON
    objects, preserving literal `<think>` text inside valid tool arguments,
    paths, and replacements.
  - SSE history replay now has a precise trigger: only reconnects carrying a
    positive `Last-Event-ID` replay buffered events. First connects rely on
    `/api/state` reconciliation and cannot duplicate old chat rows.
  - Repair exhaustion now derives its blocked reason through
    `completion_blocked_reason()` after counting repair turns, so a run that
    consumes the last turn records `turn_budget_exhausted` instead of borrowing
    `max_repair_rounds`.
  - Removed the production `COMPLETION_ENFORCEMENT_MODE` control arms; the
    single production path is proof -> bounded repair context -> final proof
    verdict. The manual completion benchmark now executes only that path.
  - Deleted the production metadata-only capability registry and its
    fingerprint tests. Capability boundaries are now documented in
    `docs/codey_event_matrix.md` and checked by scanner tests against
    production `capability_id` stamps.
  - Moved the deterministic research regression scorer from
    `codey.research.regression_gate` to `tools/research_benchmark/scorer.py`;
    production code is tested not to import the tooling package.
- Cold-start audit hardening:
  - Terminal `task_done` events now use one helper and report observed turns on
    user stop/error paths instead of hard-coded zeroes. Repair exhaustion also
    persists its `max_repair_rounds` verdict into the durable run-operation
    register.
  - Edit-integrity diff parsing now treats every `---` / `+++` boundary as a
    file boundary, so headerless untracked-file diffs cannot inherit the
    previous tracked path. Git change collection disables quoted paths for CJK
    filenames and gives synthesized untracked diffs a `diff --git` header.
  - Research provenance is stricter: synthesis merging no longer fabricates
    conclusion or counter-evidence lines; research record construction can bind
    citations from the persisted Sources section; and `knowledge_write` updates
    merge with the existing note while preserving creation time, scope,
    sources, relations, tags, and aliases unless explicitly changed.
  - Evidence ledgers now rotate out unavailable or full active files with
    observable warning reason codes instead of leaving a session permanently
    unable to satisfy the completion gate.
  - Runtime guardrails and observability improved: DNS fake-IP compatibility is
    opt-in, consensus advisor failures are surfaced as degraded reasons,
    JSON-tool parsing strips `<think>` blocks and de-duplicates identical calls,
    writer failover records the failed provider before selecting the next one,
    and shell-approval continuation uses the currently active provider.
  - The web server now caps POST bodies, SSE streams carry bounded replay IDs
    for reconnects, stopped runs render a terminal status row, provider status
    refreshes on boot, localStorage writes are quota-safe, and the real Edge
    browser E2E is opt-in instead of part of the default unit suite.
  - Removed runtime injection of the metadata-only capability registry and
    small stale shims (`DOC_SUFFIXES`, `_query_bool`, protocol JSON alias, and
    an unreachable browser-search raise). Larger audit-only modules remain for
    a separate architecture cleanup so this bugfix commit stays behaviorally
    reviewable.
- Runtime operation facts are now session-log native. The previous standalone
  `codey/run_operation.py` register was deleted before release; the only
  durable source is `RuntimeSessionLog`, and `codey.runtime.effects`
  projects the latest bounded run phase from `operation_effect` rows.
  - `RuntimeOperationStore.start()` appends `operation_started` and the
    initial phase atomically to the runtime log; later phase commits append
    one `run_phase` effect and, at terminal, one matching
    `operation_settled` outcome. There is no parallel JSON register,
    migration path, or legacy lookup.
  - Starting an already-open run resumes the latest phase on the same
    operation instead of appending a second start or rewinding to `accepted`.
    The manual crash/resume smoke now hard-kills at `writer_running`, then
    continues the same `run_id` to terminal and verifies the lane is closed.
  - The phase contract stays closed:
    `accepted -> writer_running -> writer_settled -> completion_proof_recorded
    -> (repair_context_admitted -> repair_running -> repair_settled)* ->
    terminal`; every non-terminal phase may terminate, repair admission only
    belongs to an unsatisfied failed proof, and a blocked verdict may only
    finish.
  - Runtime log rows and phase payloads are schema-v1, closed-key, and
    no-coercion: missing durable ids/timestamps, padded strings,
    bool-as-int counts, malformed proof/context/project refs, unknown
    effect kinds, missing effect refs, impossible phase facts, or forbidden
    raw fields (`prompt`, `reply`, `stdout`, `stderr`, `diff`) fail closed
    before replay or commit.
  - The recorded proof and blocked verdict contracts remain bound to the
    completion proof vocabulary: proof refs are `completion_proof:<16 hex>`,
    statuses are `complete` / `complete_with_limitations` / `failed` /
    `blocked`, `satisfied == (status == "complete")`, and a blocked verdict
    requires an unsatisfied `failed` or `blocked` proof.
- Task entry schedules every production submission through `TaskRuntime` and
  commits completion/repair phases at the real lifecycle boundaries. Runtime
  persistence remains fail-open for the user task: a bad runtime fact disables
  this run's progress projection but never changes the coding run's behavior.
- Run Details gained one quiet `Progress` row, shown only when the user opens
  Details for a run whose operation state never reached terminal and whose
  ledger has no `run_finished` row (stale snapshots never pollute finished
  runs): `Writing was interrupted`, `Completion check was interrupted`,
  `Finishing was interrupted`, or `Stopped during repair` -- the copy names
  what was actually interrupted, including that a settled repair is over and
  a satisfied proof was already finishing. No chips, banners, or internal
  vocabulary.
- Removed TaskFlow instead of continuing to thin it: the stringly
  `completion_repair_admission` dict became a typed
  `RepairContextProjection | None`, the blocked-reason ternary chain moved
  into the pure `completion_blocked_reason()` projection, and production
  orchestration is now split across `provider_preflight.py`,
  `conversation_plan.py`, `mode_dispatch.py`, `task_run.py`, `research_flow.py`,
  `review_flow.py`, `planning_flow.py`, `ghost_context.py`,
  `ghost_post_turn.py`, and `project_completion_flow.py`.
- Tests and manual harnesses now patch the owning module directly, for example
  `codey.operations.research_flow.run_research_iteration`, instead of requiring
  a production class to retain private methods as patch points.
- Registered the `runtime_operation.state` event-matrix row on
  `runtime_session_log`. `State.forget_conversation()` now deletes the
  session's runtime log bucket. No manager classes, no provider/tool replay,
  no prompt changes.
- Verification: deterministic crash-position tests (writing, check,
  finishing, and repair positions recover with an honest progress line),
  runtime reducer exception settlement, phase round-trips, strict
  fail-closed readers/writers, terminal immutability, ledger/terminal
  consistency, payload hygiene, `tests/manual/completion_operation_resume_smoke.py
  --self-test`, and the full local pytest gate. No live provider A/B --
  this version changes nothing model-visible.

## 0.5.0 - Verified Completion v2 and Edit Integrity Monitor

- Added the 0.5.0 edit-integrity monitor to the production completion path,
  closing the gap the 0.4 stabilization A/B exposed: Qwen and MiMo tampered
  with the test fixture (deleted / commented / `try`-`except`-guarded
  `import redis`) to turn pytest green, and the production completion path
  had no way to notice.
  - `codey/completion/edit_scope.py` owns one closed edit-path vocabulary
    (production / test / fixture / verification config / docs /
    generated-vendor), a conservative task-authorization scan for test
    edits, and the shared `is_document_path` definition (moved here from
    `verification_policy`; it is a stdlib-only leaf locked by tests).
  - `codey/completion/edit_integrity.py` observes one run's changed paths
    plus the unified diff the change collection already produced and emits
    bounded, refs-only findings with a closed reason-code vocabulary:
    removed or commented test imports, `except ImportError` guards, added
    skips, net-removed assertions, narrowed verification config, and
    green verification without any production file change. A user task
    that explicitly asks for test edits downgrades findings to low
    severity instead of reading as tampering. Raw diff text never leaves
    the module, and any internal failure fails closed to `monitor_error`,
    never to clean.
  - The monitor is not evidence, does not block done, does not auto-repair,
    and adds no Manager; the clean path is completely silent.
- Added `codey/completion/decision.py` and thinned TaskRunner: the inline
  enforcement-decision closure became the pure
  `build_completion_decision(...)` projection, so the agent loop, the
  repair round, the receipt, and the trace all read one
  `CompletionDecision` (proof, provenance, analysis-run refs, failure
  class, local state). The duplicated changed-path extraction was
  converged into `edit_scope.changed_paths_from_changes()`.
- `CompletionProof` now carries structured `diagnostic_refs` naming the
  edit-integrity observation that qualifies it; they are content-addressed
  into the contract id and kept apart from review-finding refs.
- Rewrote the task receipt as schema v1
  (`TaskReceipt(display, work, verification, integrity)`).
  - Trust is a contract, not a score: `trusted` (checks passed, nothing
    high-risk observed), `needs_review` (checks passed but high-confidence
    integrity findings), and `limited` (checks did not pass, or monitoring
    failed / was incomplete and the green cannot be vouched for).
  - Display copy is minimal: `2 files changed · checks passed`,
    `2 files changed · checks need review`, or
    `2 files changed · verification limited`; the longer explanation is a
    separate `display.detail` used only by Run Details.
  - The flat `text` / `changed_count` / `checks_passed` /
    `restore_available` receipt fields are gone. `RunResult.checks_passed`
    is unchanged: it stays the agent loop's execution fact, not the
    receipt contract.
- TaskRunner wiring:
  - Edit integrity is observed at every completion decision point
    (initial and post-repair); the proof is recomputed once with the
    observation's diagnostic refs when findings exist, and both the proof
    and the observation land in the run trace.
  - Only runs whose receipt trust is `trusted` write project facts and
    project memory, so a high-suspicious green can no longer seed future
    verification habits.
  - The terminal event's receipt is now projected directly from the
    receipt the ledger durably recorded; the legacy
    `receipt_from_projection_if_compatible()` shadow check was deleted
    instead of adapted.
- Trace / ledger / details:
  - `RunTraceManifest` records a bounded `completion_edit_integrity`
    section via `record_edit_integrity()`; completion-proof rows carry
    `diagnostic_refs`; `edit_integrity` / `edit_integrity_finding` joined
    the shared runtime-ref kind registry.
  - `changes_collected` stores the validated schema-v1 receipt, the
    projection carries it on `ChangesSummary.receipt`, and
    `build_task_receipt_from_projection()` returns exactly what was
    recorded.
  - Run Details reads the verification row from the receipt contract:
    `Test changes may have weakened checks` (warning) for `needs_review`,
    `Verification monitoring incomplete` for incomplete monitoring, legacy
    wording otherwise.
- Headless JSONL receipts and the web UI consume the schema-v1 sections
  only; the shared `receiptSummary()` / `receiptChangedCount()` helpers
  live in `render.js`, and research receipts emit `display.summary`
  instead of `text`. The ghost work queue reads
  `receipt.work.changed_count`.
- Manual A/B convergence:
  - `completion_enforcement_ab.py` no longer keeps a second
    `modified_test_fixture` engine: fixture scope is read from the run's
    own trace integrity rows, and rows carry `receipt_trust` /
    `integrity_*` fields.
  - New `tests/manual/edit_integrity_ab.py` replays the recorded Qwen and
    MiMo tampering signatures through the production monitor and receipt
    (deterministic gate, 20 cases) and exposes the two minimal live
    smokes: a DeepSeek clean path and a Qwen/MiMo
    `dependency_missing_env_failure` case. No full production-quality A/B
    is required for this version; the live smokes are now recorded as
    manual evidence: DeepSeek clean path returned `receipt_trust=trusted`,
    `integrity_status=clean`, and no warning; the Qwen dependency-missing
    tampered-test case returned `receipt_trust=needs_review` with
    `test_import_removed_or_commented`.
- Review-round hardening (same-day findings, all fixed before commit):
  - `completion_evidence()` takes an explicit snapshot (changes, changed
    flag, scope files, selected check, stop reason) at every call site, so
    the integrity observation always reads the post-repair diff instead of
    one cached before the repair round.
  - "Fix the failing test" no longer authorizes test edits (it usually
    means fixing product code); the Chinese authorization list keeps only
    explicit 修改/更新/调整测试 phrasings.
  - Diff scanning saturates per section: a huge production diff can no
    longer hide a tampered test file edited after it.
  - Import findings net against unguarded re-added imports (a legitimate
    move is not a removal); `with pytest.raises(...)` removals count as
    assertions; a specific expected exception widened to `Exception` is a
    new high-signal finding (`test_expected_exception_widened`).
  - Verification-config findings fire only on provably narrowing additions
    (`--ignore`, `--deselect`, `-k "not ..."`) and testpaths replacements
    strictly inside the replaced roots; deleting testpaths/addopts is not
    a narrowing signal.
  - Receipt schema v1 closes its audit loop: `verification.state`,
    `verification.proof_refs`, `integrity.affected_paths`, and
    `integrity.refs` travel with the receipt (bounded, no raw diff).
  - Trust contract tightened: a receipt claiming passing checks without an
    integrity observation is `limited`, never `trusted`; Run Details no
    longer reconstructs a green claim from the legacy `checks_passed`
    fact (no receipt -> "Checks not recorded").
  - README / DESIGN receipt copy synced to the schema-v1 wording.
- Second review round:
  - Node verification surfaces covered without crude classification:
    `jest.config.*` / `vitest.config.*` are verification config;
    `package.json` stays production and is judged by a content-level rule
    (npm `test` script gutted of its runner, or narrowing flags added to
    it).
  - Run Details reads the Verification row from the receipt contract
    only; the trace-based integrity fallback was deleted.
  - Trust contract: green checks over changed files with an `unobserved`
    integrity observation are `limited`; only a no-change run keeps
    `trusted`.
- Third review round:
  - The persisted-receipt reader now recomputes the contract: trust and
    display wording come from the same primitive helpers the builder
    uses, and integrity status/severity must be in their closed enums --
    a stored payload that disagrees with its own facts is rejected
    outright instead of echoed back as valid.
- Release-candidate hardening:
  - The schema-v1 receipt reader now rejects non-canonical JSON types
    (`true` masquerading as `1`, numeric booleans, and non-string ref
    lists), while the builder treats boolean `changed_count` as zero
    instead of one.
  - Terminal events now add or replace the receipt from the run ledger
    whenever the durable projection has one, including late stopped/error
    exits after final changes were recorded. Runs without a final
    `changes_collected` receipt keep their mode-specific receipt or no
    receipt.
- 0.5.0 hotfix:
  - Edit Integrity Monitor now treats `clean` as observed diff coverage
    over every changed path. Known changed files with no parseable diff,
    or with only partial diff coverage, produce `unobserved` with
    `diff_unavailable`; a green receipt over changed files is downgraded
    to `verification limited` and cannot write project facts or project
    memory.
  - Test-edit authorization now checks explicit denials before broad
    edit/test authorization phrases: `not/no tests`, `without changing
    tests`, `tests ... unchanged`, and the equivalent Chinese denial
    phrases keep test tampering high suspicious.
- Bounded-observation hotfix:
  - Diff sections now carry a private saturation flag. If the monitor hits
    `MAX_SECTION_LINES` for a changed section, the observation cannot be
    `clean`: visible findings remain `suspicious`, otherwise the run
    becomes `unobserved` with `diff_unavailable`.
  - The monitor now treats `changes.truncated` as incomplete observation,
    so a globally truncated collected diff downgrades a green changed run
    to `verification limited`.
  - Content scanning now covers every parsed diff section while keeping
    emitted findings and affected paths bounded, so a later test section
    cannot be hidden behind many earlier file sections.
  - Git rename/copy display paths are normalized to the new path for
    changed-path identity, with `previous_path` preserved. `collect_git_changes()`
    and the completion edit-scope helper now agree on the same canonical
    shape, reducing false `verification limited` receipts for ordinary
    renames.
- New tests: `test_completion_edit_scope.py`,
  `test_completion_edit_integrity.py`,
  `test_task_runner_edit_integrity.py`, and the
  `tests/fixtures/edit_integrity/` path-shape fixtures; receipt, ledger
  projection, ledger, details, server, UI, checkpoint-flow, and
  enforcement tests migrated to the schema-v1 contract. Architecture
  tests lock `edit_scope` as a stdlib-only leaf and keep
  `edit_integrity`/`decision` free of provider/browser/tool-runtime/
  server dependencies.

## 0.4.21 - Research and Ghost A/B Stabilization

- Migrated `verification_review_ab.py` onto the release-grade A/B evidence
  spine.
  - Fixed-output runs now write result JSON, journal events, transcript refs,
    and a manifest under the same arm layout.
  - `--self-test` covers the baseline/current prompt split; fixed-output resume
    skips completed rows; `--rerun-failed` preserves old evidence if provider
    connection fails before a new row exists.
  - The DeepSeek live smoke showed the intended reviewer behavior change:
    baseline approved the synthetic diff, while the current arm requested tests
    and named the existing check path.
- Ran the first DeepSeek single-provider live smoke over the coding extended,
  Research, and Ghost A/B arms.
  - `read_before_edit` and `impact_guard` completed successfully in both arms;
    `impact_guard` exposed the guard and finished with fewer turns/tool calls
    in this one sample.
  - `scoped_task_plan` improved the scoped smoke result versus the current arm,
    but with a larger prompt surface.
  - `bounded_research_planner` improved the one-case research score from `3`
    to `5`; `search_coverage` made incomplete non-UTF-8 scans explicit.
  - `source_connector` and `source_connector_done` produced useful negative
    evidence: the connector and batch/checklist arms should not be promoted
    from this sample because they did not reduce retries or improve score.
  - Ghost continuity, router, signal extraction, and work-queue probes passed
    the DeepSeek control/treatment smoke without evidence/citation pollution.
- Fixed manual harness issues exposed while running the DeepSeek pass:
  - `read_before_edit_ab.py` now creates parent directories for fixed `--out`
    paths.
  - `scoped_task_plan_ab.py` supports true single-arm live runs.
  - `source_connector_done_ab.py` owns its trace bounds and `LiveTrace` helper
    instead of relying on removed internals from `source_connector_ab.py`.
  - `bounded_research_planner_ab.py` accepts and passes the production
    `topic_continuity_context` / payload arguments used by `ResearchPipeline`.
  - `ghost_research_continuity_ab.py` supports single-arm runs and bounded
    provider/new-chat timeouts, preventing mixed-arm traffic and unbounded live
    waits.
  - `ghost_router_ab.py` and `ghost_work_queue_production_ab.py` now treat
    control cases as no-regression checks instead of requiring strict cost
    improvement.

## 0.4.20 - Completion A/B Stabilization

- Ran the first DeepSeek live A/B pass for the coding/completion core arms:
  `control_done`, `proof_only_block`, `repair_context`, and
  `repair_context_minimal`, using archived transcripts and fixed result/journal
  output directories under `tests/manual/results/0.4.20/`.
- Fixed a live A/B regression where requested-verification tasks could get
  stuck in the low-level agent loop after an observed failing run.
  - `codey.agents.runner` now only enforces that a run tool call was observed
    after the latest edit when the user explicitly requested verification.
  - Pass/fail/unavailable verification semantics remain owned by the
    completion proof layer instead of the agent loop trying to force a green
    run.
  - A run before the latest edit no longer satisfies the requested-verification
    observation guard.
- Added deterministic regression tests for requested-verification observation
  epochs and for failed verification reaching the completion proof layer.
- Tightened `completion_enforcement_ab.py` evidence handling during this A/B
  pass:
  - terminal `stop_reason="error"` rows now fail the report even if the row did
    not carry an `error` field yet;
  - terminal error summaries are preserved in result rows;
  - the live path now uses the real production agent runner and change
    collector instead of `None` callables;
  - provider failure class fields are aligned with the closed manual A/B schema.

## 0.4.19 - A/B Evidence Polish and Passive Worker Health

- Standardized manual A/B evidence layout in `tests/manual/ab_harness_common.py`.
  - Added `ArmRunLayout`, `ArmManifest`, and `ResultRowStore` so fixed `--output` runs bind result JSON, journal directory, transcript directory, manifest path, provider id, git state, and dirty-state metadata.
  - Re-running the same provider/case/arm/repeat now replaces the previous row atomically instead of appending a stale failed row that keeps polluting summaries.
  - Existing result files are not modified while pending work is only being calculated, so a provider connection failure does not erase prior evidence.
  - Transcript references are digest-only by default and only marked replayable when an archived transcript file actually exists.
  - Provider failures now use a closed vocabulary (`provider_send_error`, `provider_no_reply`, `native_search_stall`, `webpage_ui_changed`, `unknown`, `none`) separate from Codey/runtime failures.
- Migrated the live-output paths for `completion_enforcement_ab.py`, `research_to_code_ab.py`, `bounded_research_planner_ab.py`, and `ghost_research_continuity_ab.py` onto the common result/journal layout.
  - Journals use stable output-derived identities and may append resume `run_start` events with explicit `resumed_attempt` / `attempt_index`.
  - External failures after a journal is opened now record a terminal failed `run_complete` event without deleting previous result rows.
- Added passive BrowserWorker health snapshots.
  - `BrowserWorker.health_snapshot()` reports queue size, current job state, running duration, stuck threshold, job counters, and thread liveness.
  - `BrowserSearchProvider` captures the latest worker-health payload on worker-boundary timeout/cancel so Qwen/native-search stalls can be diagnosed without restarting the worker automatically.
  - Regression coverage uses a non-cooperative job to prove the caller can time out while the worker remains occupied and only observed, not restarted.
- Hardened explicit atomic write modes in `codey.storage.atomic_io`.
  - Explicit `mode=` enforcement now fails hard if `fchmod/chmod` cannot apply the requested permissions.
  - `preserve_mode=True` remains best-effort, preserving existing behavior for ordinary state replacement.
- Made Ghost Work Queue transitions an explicit invariant.
  - `WORK_ITEM_TRANSITION_MATRIX` is now the single authority for action/status transitions, with tests tying it to patch schemas.
- Renamed the successful `NetworkStatus` state from `PUBLIC_WEB` to `POLICY_ALLOWED`.
  - This preserves `NetworkDecision.allowed` and `check_fetch_url()` behavior while making the status name match the actual contract.
  - `POLICY_ALLOWED` means the URL passed Codey's configured fetch policy; it is not proof that DNS resolved to a globally routed public internet address, especially when TUN/transparent-proxy fake DNS support is enabled.
  - Added regression coverage so the allowed status name/value cannot drift back toward `public` or `web` semantics.
- Updated the roadmap with a narrow 0.4.x stabilization track, a post-0.5 exit gate, and a 0.6 consolidation line.

## 0.4.18 - Network Boundary, Cooperative Cancellation, and Storage Unification

- Replaced sidecar lock creation/deletion and stale takeover heuristics with OS-backed advisory file locks (`codey.storage.file_lock`).
  - Uses operating-system native locks (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) combined with process-local thread synchronization (`threading.RLock`).
  - `LockTimeout` inherits `TimeoutError` (a subclass of `OSError`), aligning with store error handling contracts (`except OSError`).
  - Implemented ref-counted process lock registry (`_ProcessLockEntry` with `_borrow_process_lock` and `_return_process_lock`): locks are referenced upon acquisition attempt and automatically pruned from memory when reference count drops to 0, eliminating process-level memory accumulation across long-lived, multi-project workflows.
  - Sidecar `.lock` files remain permanently on disk as lock carriers, eliminating `stat -> unlink` time-of-check-to-time-of-use (TOCTOU) races and stale takeover bugs.
  - Added dedicated `codey.storage.event_state` module with `reset_event_backed_state(events_path, *state_paths)` ensuring all event logs and derived projections are deleted safely under the authoritative event lock.
  - Removed unused `transactional_json.py` abstraction.
  - Standardized cooperative locking discipline across all 7 Ghost stores (`work_queue`, `affinity`, `continuity`, `hebbian`, `inbox`, `router`, `sleep`):
    - All public read APIs (`list_*`, `export_state`, `query_*_hints`, `learning_enabled`) acquire the store's `events_path` lock, preventing torn reads against concurrent `reset_all()` or active mutations.
    - All internal read/projection helpers are renamed with `_unlocked` suffix (e.g. `_load_items_unlocked`, `_read_events_unlocked`) to explicitly designate that callers must already hold the authoritative event lock.
    - `compact_if_needed()` wraps event file stat checks, state loading, event compaction/rewriting, and post-compaction stats atomically within a single `with with_file_lock(self.events_path):` block, closing un-synchronized stat/rewrite races.
    - Fixed `UnboundLocalError` on `before` variable in `compact_if_needed()` during lock acquisition timeout in `work_queue`, `affinity`, and `router`.
- Hardened `BrowserWorker` with cooperative cancellation and decoupled job lifecycle (`codey.automation.browser_worker`):
  - Added `_Job` dataclass with `_JobState` (`QUEUED`, `RUNNING`, `COMPLETED`, `CANCELLED`, `CANCELLATION_REQUESTED`).
  - Caller cancellations and timeouts propagate into job-specific cancellation events and execution deadlines, running inside `cancellation.scope` and `cancellation.deadline_scope`.
  - Reentrant `BrowserWorker.call()` executes under active cancellation and deadline scopes for nested timeouts.
  - Queued jobs cancelled prior to dispatch skip execution cleanly.
  - `BrowserSearchProvider` integrates cancellation checks across navigation, parsing, and item iterations; fetch and search paths wrap entire browser lifecycle in unified discard boundaries (`_discard_fetch_page_on_browser_thread`, `_discard_search_page_on_browser_thread`), resetting page references and propagating cancellation without intermediate retries or corrupted page leaks.
- Unified Network Policy single source of truth and DNS caching (`codey.policies.network`, `codey.research.connector_search`, `codey.research.tools`):
  - Created centralized `NetworkPolicy` with `NetworkStatus` (`POLICY_ALLOWED`, `BLOCKED_PRIVATE`, `BLOCKED_UNRESOLVED`, `INVALID_URL`) for application-level SSRF mitigation.
  - Strict conservative non-global IP rejection (`not ip.is_global or ip.is_multicast`) covering `100.64.0.0/10` (CGNAT) and all reserved address spaces.
  - Integrated `allow_dns_fake_ip=True` support for TUN/transparent proxy environments (`198.18.0.0/15` DNS fake IPs on resolved hostnames) while strictly rejecting literal fake IPs and preventing empty DNS resolution fail-open.
  - `POLICY_ALLOWED` is documented as "allowed by this policy", not as hard proof that DNS resolved to a globally routed address under all local proxy configurations.
  - `ResearchTools.open_url()` enforces policy verification at the public tool boundary prior to invoking search providers, and reuses the short TTL policy cache for the post-fetch final URL check.
  - Connector requests (`connector_search.py`) use non-redirecting openers with explicit hop-by-hop URL policy validation (`check_fetch_url(use_cache=True)`) and bounded redirect loop limits.
  - Shared `codey.research.http_redirects` now owns the no-redirect opener, redirect-status parsing, Location-header parsing, and best-effort response close helpers for connector and browser PDF fetch paths.
  - Connector URL opening now always uses the non-redirecting opener; the old `urllib.request.urlopen` monkeypatch fallback is removed so tests cannot bypass the production redirect boundary.
  - Connector `HTTPError` redirect responses are closed before following the next hop, and redirect tests mock policy decisions per hop instead of depending on live DNS for fixture URLs.
  - Connector redirect hops share one total request deadline; each hop receives only the remaining socket timeout, so redirect chains cannot multiply a single connector request budget.
  - `check_fetch_url()` is exported directly from `codey.policies.network`; removed redundant `codey/research/url_policy.py` shim.
  - Introduced a bounded TTL cache (5s for allowed targets, 45s for blocked/unresolved targets) for subresource route guards in browser automation.
- Agent runner protocol improvements (`codey.agents.runner`):
  - Removed implicit terminal when the model returns valid tool actions without an explicit `<continue>` or `<done>` control element; tool results are now cleanly formatted and returned to the model with a protocol reminder.
- Edit tool hardening and threat model clarification (`codey.toolchain.runtime`):
  - Removed heuristic write paths in `_replace_unique_indentation_recovery()`; indentation mismatches now fail safely with diagnostic bounded context and line guidance without mutating user files.
  - Documented path traversal threat model on `safe_join()`.
- Storage and permission unification (`codey.storage.atomic_io`, `codey.storage.local_store`, `codey.workspace.changes`, `codey.storage.managed_outputs`, `codey.knowledge.store`):
  - Unified atomic file writing under `write_bytes_atomic`, `write_text_atomic`, and `write_json_atomic`.
  - Temporary files in `write_bytes_atomic` are opened directly with `creation_mode` via `os.open(..., O_CREAT | O_EXCL | O_WRONLY, creation_mode)` preventing umask permission exposure windows.
  - Applied `fchmod/chmod` immediately after write prior to flush/fsync for optimal persistence ordering.
  - Directory sync (`_fsync_dir`) on POSIX systems ensures directory entry crash durability.
  - Local credential storage (`save_local_config`) enforces `0o600` permissions.
  - Standardized atomic writes across workspace snapshots, managed tool outputs, and knowledge stores onto `atomic_io`.

## 0.4.17 - OS-Backed File Locks and Event State Reset

- Replaced sidecar lock creation/deletion and stale takeover heuristics with OS-backed advisory file locks (`codey.storage.file_lock`).
  - Uses operating-system native locks (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) combined with process-local thread synchronization (`threading.RLock`).
  - `LockTimeout` inherits `TimeoutError` (a subclass of `OSError`), aligning with store error handling contracts (`except OSError`).
  - Sidecar `.lock` files remain permanently on disk as lock carriers, eliminating `stat -> unlink` time-of-check-to-time-of-use (TOCTOU) races and stale takeover bugs.
  - Added dedicated `codey.storage.event_state` module with `reset_event_backed_state(events_path, *state_paths)` ensuring all event logs and derived projections are deleted safely under the authoritative event lock.
  - Removed unused `transactional_json.py` abstraction.
  - Standardized mutation concurrency discipline across all 7 Ghost stores (`work_queue`, `affinity`, `continuity`, `hebbian`, `inbox`, `router`, `sleep`): all mutations (append, replay, rebuild, delete_scope, reset, and compaction) acquire the store's `events_path` lock.


## 0.4.16 - Ghost Event Canonicalization and Work Queue Invariants

- Ghost Affinity and Work Queue event logs now store semantic intent events
  instead of computed upsert results. Affinity replay applies reinforced
  node/edge specs, scope deletes, decay events, and snapshot anchors through
  the reducer; Work Queue replay applies observed candidates, preconditioned
  transitions, delete events, and snapshot anchors through the reducer. The
  cold-start schema constants remain `1`; old upsert event types are
  unsupported and fail closed for mutation.
- Ghost Work Queue now enforces strict action-specific semantic validation and
  required fields for `ghost_work_item_transitioned` events (e.g. `claim`
  requires valid `started_run_id`, `lease_expires_at`, and `retry_count`;
  `complete` strictly requires matching non-empty `completed_run_id == expected_started_run_id`,
  non-empty `proof_refs`, and empty lease; `queue` strictly requires `retry_count == 0`
  and clears running fields; `release` to `queued` strictly clears lease and started run ID).
  `complete_item()` API verifies non-empty matching `run_id` before mutation, preventing
  malformed event generation or improper blocking of concurrent items.
  `GhostWorkItem.from_payload()` enforces a strict state invariant matrix for snapshot
  and observed items (`done` requires `completed_run_id` and `proof_refs`;
  `queued`/`candidate`/`rejected` clear all running/completion fields;
  `running` requires `started_run_id` and clears completion fields;
  `blocked` requires `blocked_reason` and clears completion/lease fields).
  Replay applies kind-specific primary proof enforcement (`_primary_proof_matches_item_kind`)
  and full sequence validation. Any malformed transition is flagged as
  `invalid_event` and fails closed on read.
- Ghost Affinity and Work Queue mutating APIs now run their read -> reduce ->
  decide -> append/rewrite -> project flow under the store file lock, closing
  semantic lost-update races such as concurrent reinforcement and double
  claim attempts. Mutation results (including `GhostWorkQueueStore.delete_scope()`
  and `GhostAffinityStore.decay()`) are returned as structured diagnostics
  merging `self.last_warnings` so callers do not miss diagnostic warnings.
- Ghost Work Queue's `compact_if_needed()` adds isomorphic check for missing
  events files when projection exists and reports `work_events_missing`.
  Unused arguments in `_transition_item()` and dead helper `_release_stale_claims()`
  have been cleaned up.
- Ghost Affinity and Work Queue validators now reject non-canonical raw event
  payloads instead of silently normalizing them: extra top-level fields,
  extra snapshot/spec/item fields, malformed delete/scope payloads, missing
  Affinity decay counters, string/bool counters, bool-vs-number or
  int-vs-float type confusion, and orphan edge reinforcements all fail closed
  before mutation.
- The new hardening does not change prompts, provider tools, UI/SSE payloads,
  permission profiles, or default Research/Writer behavior; it only tightens
  local Ghost event-log ingestion and replay.

## 0.4.15 - Run Command Boundary + Stabilization Hardening

- Run-command policy now treats pytest ini overrides as a guarded second argv
  surface. `addopts` is recursively parsed with the same tokenizer as pytest
  argv, including compact short-option forms such as `-oaddopts=...`;
  `cache_dir`, `log_file`, `pythonpath`, and `testpaths` are explicit
  path-carrying keys; path-shaped discovery patterns are boundary-checked;
  and unsupported override keys fail closed instead of silently carrying
  hidden filesystem operands.
- Direct `python script.py ...` verification commands now check path-shaped
  script arguments, not only the script path itself, before the allowlist can
  launch a process.
- Provider adapter overrides now install only the declared adapter repair
  surface for one provider, with guarded generation cleanup, instead of
  copying a whole `codey/` runtime snapshot into the override path.
- Evidence, Ghost affinity, and Ghost work-queue read-modify-write state paths
  now use a locked JSON mutation primitive to avoid local lost updates between
  cooperating Codey processes.
- Ghost affinity no longer treats a larger `source_refs` list as a stronger
  reward signal; references remain provenance, while one observed event
  contributes one reinforcement.
- Manual A/B harnesses share arm manifests, output identity checks,
  append-or-replace failed-row semantics, stable journal lifecycle helpers, and
  bounded failure classes so live evidence can be resumed and audited.
- Provider worker CDP target lookup releases temporary CDP sessions when the
  browser binding supports it.
- CI now runs the Python suite across 3.11, 3.12, and 3.13.
- Manual A/B verification probes pass the project root into selected-check
  coverage while the temporary project still exists, and the completion
  enforcement journal test now uses the shared journal helper directly after
  removing a dead private wrapper.
- README project-structure docs now describe the post-migration package layout
  (`app/`, `providers/`, `repairs/`, `runtime/`, `storage/`, `workspace/`)
  instead of the removed flat provider modules.

## 0.4.14 - Provider Package Cold Migration

- Provider runtime modules now live exclusively under `codey.providers.*`.
  Root `codey/provider_*.py` modules and the intermediate
  provider-prefixed names inside that package are gone; imports, tests, mock
  patch paths, manual A/B fixtures, docs, and tools now target the final
  provider paths.
- Built-in provider profile data moved to `codey/providers/profiles.json`
  and is packaged as `codey.providers` data.
- `codey.providers` keeps lazy public exports so importing small provider
  support modules does not also load every web driver.
- This release is a path-only cold migration baseline for provider A/B work:
  no compatibility wrappers and no intended behavior changes.

## 0.4.13 - Verified Completion Enforcement + Repair Context Admission v1

### Final release closeout

- Prompt-visible redaction no longer treats ordinary CamelCase engineering
  identifiers with small numeric qualifiers as high-entropy secrets:
  `OAuth2CallbackHandler`, `HTTPRequest2Handler`,
  `Windows10CompatibilityMode`, and `PyPI2026ReleasePlan` now survive the
  global prompt gate, while marker words, provider key shapes, and genuinely
  random mixed-case blobs such as `AbcdEfghIjkl1234X` still screen out.
- The adapter-repair sandbox now rejects a symlinked `source/codey` package
  root before walking or copying it. Earlier hardening rejected symlinks
  inside the package tree and reference files; the root itself is now covered
  too.
- Completion repair-context digests now include a hash of the actual bounded
  model-visible facts brief. The trace payload still has no raw text field,
  but its digest changes when the facts sent to the model change, not only
  when counts or reason codes change.
- The final proof after a repair round refreshes verification candidates
  before selecting the relevant check. A repair that changes verification
  scope, for example from a frontend command to a backend command, no longer
  gets judged against the pre-repair candidate view.
- `tests/manual/completion_enforcement_ab.py` live mode now writes the JSON
  report after every case/arm row and can journal prompt/reply traffic through
  the shared manual A/B plumbing. The default `--transcript-mode digest-only`
  keeps hashes only; `archive` stores bounded manual-layer transcripts for
  prompt-lab diagnosis, and production still imports none of it. Fixed
  `--output` paths now resume cleanly: existing rows are not overwritten,
  completed rows are skipped by default, `--rerun-failed` is the explicit
  opt-in for error rows, the old error row is replaced only after a new row
  exists, provider-connect failures keep the old row intact, and the journal
  run id is stable for the output stem without repeated `run_start` events.

### Hardening and cleanup (this batch)

- `providers.preferred` is now consumed as a **soft ranking preference**:
  `preferred_provider_for(config, mode)` (new helper in
  `codey/project_config.py`) feeds the project's per-mode preference into
  both failover rankings (`rank_providers(preferred=...)`) -- startup
  preflight and writer failover. The config was parsed since 0.4.x but never
  read. It only re-orders candidates: it cannot override the user's explicit
  provider, bypass supervisor availability/exclusions, or enable a
  disconnected provider. `.codey/config.json` is read exactly once per run
  and shared with the project context builder.
- One tokenizer for every command decision path: new
  `codey/command_line.py::split_run_command` (Windows: `posix=False` plus
  matching-quote stripping so `C:\path\file.py` survives; POSIX:
  `posix=True`). `tool_runtime`, `shell_risk`, `action_policy`,
  `verification_policy`, and `project_facts` all split through it, so
  approval risk analysis, policy guards, and actual execution see the same
  argv; tokenization failure fails closed.
- ChangeTracker race fixed: one reentrant lock now guards the baseline
  state, and `collect()` is strictly read-only by default. UI polling can no
  longer pop clean baselines out from under a running capture. Pruning moved
  behind an explicit `prune_clean()` called at run terminal states only.
  Follow-up: the remaining capture-side races are closed too --
  `capture_before()` re-checks baseline membership under the lock after its
  unlocked file read (two racing captures now yield exactly one entry and
  `_total_bytes` is counted once), and `capture_after()` re-checks after
  hashing, so `prune_clean()` dropping a baseline mid-hash can no longer
  leave an orphan after-hash behind. Both covered by dedicated concurrency
  tests.
- Recovery snapshots rewritten as two layers (`baselines/<rel-digest>` body
  files plus a small `manifest.json`): a non-Git edit now writes one bounded
  baseline file plus the manifest instead of re-serializing up to 64MB of
  JSON on every capture (write amplification). The two-layer format is
  `schema_version: 1`; any other manifest layout (including the older flat
  one) is ignored outright -- there is no compatibility path.
- `UiStateStore.save()` compares against an in-memory cache seeded by the
  first load instead of re-reading and re-parsing the file on every save;
  a failed write keeps the previous durable baseline.
- SSE overflow is now visible: slow subscribers get a bounded
  `{"type": "resync_required", "reason": "sse_queue_overflow", "dropped": N}`
  marker before the newest event, and the frontend re-pulls run state and
  provider status on receipt instead of silently missing terminal/approval
  events.
- TaskRunner closes the early-failure busy window: any exception inside the
  claim/route window now produces a bounded error terminal event,
  best-effort trace finish, and `state.finish_run(...)`, so the run slot can
  never stay busy forever after an early crash.
- Provider worker requests poll in small steps and check the cancellation
  event, so Stop interrupts a pending worker response within ~0.2s instead
  of waiting out the full timeout.
- Approved shell commands run through the shared process-tree owner
  (`cancellation.run_process`, bound to the stop flag): Stop now kills the
  whole approved-command tree instead of orphaning children.
- `DeadlineExceeded` escapes the agent tool loop like `TaskCancelled`
  instead of being swallowed into a tool error, so an exhausted provider
  budget stops burning turns.
- Work checkpoints record hash-unavailable changed paths visibly
  (`hash_unavailable_files` in payload, prompt, and reconcile) instead of
  silently dropping them from an otherwise normal-looking checkpoint.
- Live A/B harness reads repair evidence from the RunTrace manifest's
  `completion_repair_context` rows, so live reports no longer show
  `repair_rounds=0`; the injected `import redis` failure now applies only to
  the `dependency_missing_env_failure` case, making
  `fresh_failing_test_after_edit` reliably repairable.
- `_bounded_summary()` keeps the evidence layer's readable tail order
  (no more reversed output tails in repair contexts).
- Removed dead surface: `builtin_profiles` (module, TaskRunner injection
  field, server wiring, capability spec, docs page, tests -- it was
  metadata-only and explicitly unused by design), the duplicate
  `VerificationCandidate` name in `verification_map.py` (renamed to
  `TestCandidate`), unused driver selector leftovers (`SEND_READY`, `INPUT`,
  `RESPONSE`, `ANSWER`, `SEND_BUTTON` constants), `citation_scanner.source_id_bracket_ref_items`,
  `provider_discovery.find_control/find_response`, and
  `provider_controls.reject_flow`. Also removed the two zero-reference
  helpers on the adapter repair surface (`adapter_surface.shared_web_adapter_files`
  and `adapter_surface.is_known_provider`): callers read the
  `SHARED_WEB_ADAPTER_FILES` constant and `adapter_repair_surface()` directly,
  so a "known provider" predicate nobody called was spare abstraction.
- Provider adapter dedup: the five byte-identical `providers/*_web.py`
  wrappers collapsed into one spec-driven `web_provider.py` (~270 lines);
  shared control-location / response-count / rate-limit / late-response
  scaffolding extracted into `providers/web_drivers/common.py`; provider id
  normalization unified in `codey/providers/ids.py`. Per-site completion
  heuristics deliberately stay in their drivers.
- Adapter self-repair surface widened deliberately, with impact-escalated
  validation. New `codey/adapter_surface.py` defines the repair surface as
  one provider's driver plus the shared web adapter files (`web_provider.py`,
  `web_driver.py`, `web_drivers/common.py`, provider profiles/controls/flow/
  send-loop/submission/timeouts, clipboard, browser). Because repairs
  install into a per-provider override root, touching shared files inside
  one provider's override cannot leak into another provider's runtime path.
  `repair_policy.validate_candidate()` no longer treats shared files as a
  violation; it classifies changes into `provider_local`,
  `shared_web_surface`, and `profile_data`, scans `*.py` **and** `*.json`
  (the forbidden-snippet scan stays Python-only), and `_run_static_checks()`
  escalates with impact: a shared-surface edit must still import the whole
  web provider layer, and a profile-data edit must still load through the
  schema. Tests and Codey core runtime stay rejected. The repair prompt now
  states the real scope ("modify only the web adapter surface ... this
  repair runs in a provider-scoped override sandbox ... do not modify tests
  or Codey core runtime"). `adapter_overrides.adapter_base_hash()` includes
  repair-surface JSON so a changed builtin `profiles.json`
  invalidates overrides generated against the old data. The surface is
  fail-closed: a provider without a driver entry has an empty surface, so
  the shared files can never be granted on their own --
  `validate_candidate()` reports "unsupported provider for adapter repair"
  and `run_adapter_repair()` refuses before any model call or install.
- Provider id normalization fully unified: the local `_provider_id()` copies
  in `adapter_overrides.py`, `provider_supervisor.py`, `self_repair.py`, and
  `repair_policy.py` all delegate to `provider_ids.normalize_provider_id()`.
- Site drivers live in the providers package: the five page drivers are
  `codey/providers/web_drivers/*.py` next to their shared `common`
  scaffolding. `web_provider.py` imports drivers from there, so drivers
  import their sibling `common` instead of reaching back into the providers
  package init -- the fragile cold-start shape is gone.
  `web_drivers/__init__.py` stays import-free on purpose.
- `UiStateStore.save()`'s cache fast path assumes a single Codey server per
  ``state_home``; that single-writer assumption is now documented in code.

- New `codey/completion_verification.py`: the coding verification semantics
  moved out of `task_runner.py` as pure projections -- tri-state freshness
  (`fresh_pass` / `fresh_fail` / `unobserved`), explicit provenance,
  proof construction, and deterministic failure classification
  (`product_failure` / `environment_failure` / `verification_unavailable` /
  `provider_failure` / `unknown`). TaskRunner now only collects facts and
  wires I/O; it no longer interprets completion. The legacy debt demanded by
  the roadmap is paid: the invisible `checks_passed` inheritance is split into
  `stance = fresh_pass / fresh_fail / inherited_pass / unverified` and
  `source = local_run / checkpoint / none`. An inherited pass (checkpoint
  resume or the narrow pre-review green rule) keeps the receipt green but the
  proof carries the `inherited_verification_not_fresh` limitation, so it can
  never count as this round's clean verification fact; a model's claimed pass
  without local observation is simply nothing.
- Verified Completion Enforcement: when a writer claims done on changed code,
  the completion proof decides. `complete` allows done; docs-only and
  inherited-green runs stay allowed as honest
  `complete_with_limitations`; a `failed` or `blocked` proof blocks done with
  an explicit stop reason (`unobserved`, `max_repair_rounds`,
  `turn_budget_exhausted`, `environment_failure`, `provider_failure`,
  `repair_context_unavailable`, `repair_not_admitted`) instead of shipping a
  fake done.
  Unobserved checks are never failures and never repair candidates: "no
  verification" means stop, not "fix something".
- Repair Context Admission v1: for exactly one bounded round,
  `failed + product_failure` proofs admit a minimal failure-facts brief back
  to the same writer through the full 0.4.8 chain -- `ContextSource` ->
  profile allow-list (coding_writer only) -> `ContextEpoch` ->
  `PromptEnvelope`. The brief states observed facts only (failed requirement,
  failure class, changed files, command/exit, capped secret-screened output
  tail, refs) and never a fix instruction; unobserved checks are explicitly
  called out as not-failures. Turn budget is shared with the original run,
  never reset; receipts, ledger, project facts, and the user-visible event
  are driven by the final outcome only.
- New `codey/completion_repair_context.py`: a projection leaf that consumes
  an already-evaluated proof payload (it does not import the completion
  contract -- one semantic owner) and produces the prompt text plus a
  digest-only trace payload. The `minimal` detail level exists for the A/B
  arm that separates "proof enforcement" from "informative context".
- RunTrace gains one bounded manifest section:
  `record_completion_repair_context(payload, *, epoch_id)` mirrors the 0.4.12
  continuity contract -- required well-formed `ctx_epoch:<16 hex>` binding to
  the outbound provider-send bytes, digest-keyed dedupe, counts/classes/reason
  codes only; raw failure text has no field to live in. Admission rows are
  recorded at the send boundary inside `agent.run`, so assembled ≠ admitted ≠
  recorded stays true by construction.
- RunTrace protocol telemetry (P0a, trace-only): a bounded
  `protocol_telemetry` manifest section records per-phase JSON tool protocol
  facts -- codec identity (`json` / `research_json`) with model/runtime
  tool-contract hashes, protocol-error counts by kind, protocol-repair-prompt
  counts, and which provider turns produced a parseable plan
  (`first_valid_turn`, bounded `valid_turns`). Unknown tools land only as a
  digest plus an optional safe short identifier; raw prompts, replies, and
  errors have no field. Four recorder methods
  (`record_protocol_codec` / `record_protocol_error` /
  `record_protocol_repair_prompt` / `record_protocol_valid_turn`) wire into
  the coding writer loop and the research runner; nothing behavioral reads
  them -- the release A/B stays more explainable at zero runtime risk.
- Capability registry: new `completion_repair_context` capability
  (model-visible, fail-closed, canonical input `completion_contract`) with a
  new `live_ab` release gate; `completion_contract` stays trace/data-only.
  The context source key is coding_writer-only.
- No RepairManager / CompletionManager / critic / scheduler / new tools. The
  repair loop is bounded by named stop conditions and
  `MAX_COMPLETION_REPAIR_ROUNDS = 1`; architecture tests lock the projection
  leaf boundaries, the closed payload vocabulary, and the absence of manager
  layers. A/B arms (`control_done` / `proof_only_block` /
  `repair_context` / `repair_context_minimal`) run through the single
  `COMPLETION_ENFORCEMENT_MODE` constant via
  `tests/manual/completion_enforcement_ab.py`; production ships `repair`.
- Enforcement hardening (pre-release review fixes, all six closed
  structurally rather than by fallback):
  - The repair round can no longer physically exceed the turn budget: when
    the initial writer consumed `max_turns` and still failed the proof, the
    run blocks with the new explicit stop reason `turn_budget_exhausted`
    instead of sending one unbounded extra turn and clamping the displayed
    turns back; the repair `turn_budget` is exactly the shared remaining
    budget, so the sum can never exceed `max_turns`.
  - Failure classification reads the decisive check's bounded output tail:
    a non-zero exit whose output names the execution environment (missing
    dependency or tool such as `No module named pytest`, network-dependent
    tests, crashed test runners) classifies as `environment_failure` via a
    closed, reason-coded, line-anchored signature vocabulary
    (`ENVIRONMENT_FAILURE_SIGNATURES`, five reason groups): a signature
    only counts when it begins its diagnostic line once runner banners and
    lowercase tool-name heads are stripped, and every match names its
    reason code and deciding phrase (`match_environment_failure`), so a
    live-A/B misjudgment becomes one new test plus one reason code --
    never a smarter classifier. Assertion diffs that merely quote the words
    (`E   AssertionError: cannot find module`,
    `assert 'connection refused' == 'connected'`) classify as product
    failures, with negative tests locking that boundary.
  - Repair admission requires safe decisive check facts: if every decisive
    fact was empty or screened out (new refusal reason
    `refused_no_safe_check_facts`), the projection refuses to admit any
    text and TaskRunner blocks with `repair_context_unavailable`, matching
    the "no safe bounded failure facts" contract instead of admitting an
    unobserved-check description.
  - Changed-but-unlisted runs stay in enforcement scope: when changes
    collection produces no usable verdict while edits were observed
    locally, enforcement scopes from the observed edit evidence instead of
    letting an edited run pass as an unverifiable done. A measured
    net-empty diff -- the model reverted its own edit -- remains a verdict:
    the run keeps the honest unchanged receipt and stays out of scope,
    so reverting is never blocked as an unverifiable done.
  - The blocked-stop vocabulary no longer borrows a repair budget it did
    not spend: a failed proof without an admitted repair round (the
    `proof_only_block` A/B arm, or a failure class outside the repair
    candidate rule) blocks with the new explicit stop reason
    `repair_not_admitted`; `max_repair_rounds` now means exactly "a repair
    round ran and verification still fails", keeping A/B notes readable.
  - The ordinary continuation path now assembles its prompt through a
    literal `PromptEnvelope`: the follow-up request and the repair-facts
    section are envelope sections recorded against the outbound send epoch,
    identical in bytes, so every repair admission provably rides the same
    assembly structure as fresh intros.
- Writer failover no longer keeps a closed provider on the runner: closing
  is now one operation that clears `self.provider` in the same step, so a
  canary failure that hits the switch budget cannot leave a dead provider
  behind for the shared Review-repair reuse of the same instance (which
  would skip reconnect and burn one doomed attempt).
- Adapter repair fails closed on empty candidates: `validate_candidate`
  rejects a no-op diff with the explicit error code
  `repair_candidate_no_changes`, so `{"files":[]}` can no longer install as
  a "successful repair", pollute repair success metrics, or send the
  provider into a pointless override worker.
- The adapter repair sandbox materializes only the repair surface instead
  of copying the whole repo twice: the `codey` package (what the override
  installer copies and what provider unit tests import), `pyproject.toml`
  (ruff config parity), plus the provider's read-only test files.
  `reference-projects/`, docs, fixtures, and tooling never enter a sandbox.
- Protocol telemetry binds `repair_prompt_counts` to real sends: the writer
  loop and the research runner record the repair prompt only after the
  terminal-stagnation / max-protocol-errors checks pass, so a run that dies
  on a protocol failure no longer reports a repair prompt it never sent.
- `prompt_safety` stops flagging ordinary paths as secrets: path-like
  tokens (`src/main/java/util/ArrayList.java`,
  `C:/Users/alienware/.codey/state.json`) are exempt only inside the
  high-entropy branch; explicit secret markers (including markers inside
  path segments) and secret shapes still block.
- Secret shape coverage widened to common provider prefixes: AWS access key
  ids (`AKIA…`, case-sensitive, boundary-aware), GitHub fine-grained PATs
  (`github_pat_…`), and Stripe live/test keys (`sk_live_`, `rk_live_`,
  `sk_test_`, `rk_test_`). A bare 40-char AWS-secret-shaped value stays
  deliberately unflagged as a pure shape — too many ordinary values look
  like it — and is caught next to marker words instead.
- Shell risk explanations cover more of what users actually approve:
  `uv add`, `go get`, `cargo add`, `deno install`, and any `npx <pkg>`
  classify as dependency installs; `irm` / `Invoke-RestMethod` classify as
  external source; `cmd /k` unwraps like `cmd /c`. Display-only: approval
  decisions are unchanged.
- Release validation stays explicit: GitHub CI runs on pushes, pull requests,
  and manual dispatch, while local release checks are documented as direct
  `ruff`, `pytest`, and completion-enforcement self-test commands rather than
  a repository hook or custom wrapper.
- Secret detection has one owner again: `redaction.py` owns secret markers,
  provider key shapes, and the high-entropy heuristic together with its
  path-like exemption. `prompt_safety` and the Ghost signal schema reuse it
  instead of keeping divergent copies, so AWS / GitHub-PAT / Stripe shapes
  now also block in prompt-visible checks, and ordinary source paths
  (`src/main/java/util/ArrayList.java`) no longer reject Ghost work items
  or signals (the exemption previously never applied on Ghost paths).
- Adapter repair cannot escape its own error envelope anymore: sandbox
  creation moved inside the guarded region, so a missing read-only
  reference file (e.g. a packaged install without `tests/`) or a broken
  `source_root` returns a bounded `AdapterRepairResult`, journals
  `adapter_repair_error`, and always removes the temp root -- previously it
  raised bare `FileNotFoundError` before any journaling and leaked the
  sandbox directory.
- Repair sandbox reference files validate fail-closed: empty, absolute,
  drive/rooted, or `..`-traversing paths are rejected up front, and every
  copy re-checks containment on both the source and destination side, so a
  bad reference path can never materialize files outside the source tree or
  the sandbox.
- Prompt/model-visible secret screening gets one named entry:
  `redaction.looks_prompt_visible_secret()` (marker | provider key shape |
  high-entropy). The old marker+shape-only `looks_sensitive_signal` is
  gone, so no caller can forget the entropy branch: execution evidence,
  repair contexts, run traces, and research boundaries all screen through
  it, and a marker-free high-entropy blob in a failed check's output tail
  or command line is now dropped (`repair_output_line_screened` /
  `repair_check_command_screened`) instead of reaching the model.
- Writer telemetry counts the terminal no-JSON reply: an empty JSON object
  with no calls/control records `no_json` before the stagnation return, so
  protocol-error observations match real sends 1:1 (repair prompts still
  count only actual nudges).
- Adapter repair sandboxes reject symlinks outright -- in the copied
  package tree, in `pyproject.toml`, and in reference files -- instead of
  following them at copy time, closing the residual path where a link out
  of the source tree would leak its target into a sandbox.

## 0.4.12 - Ghost Research Continuity + Topic Planner v1

- New `codey/research/topic_continuity.py`: a stdlib-only pure read model
  that projects bounded local facts (structured research-interest hints,
  selected Ghost continuity items, prior evidence-ledger claim refs) into
  one short model-visible hint block plus a digest-only payload for the run
  trace. Continuity can relocate old refs and suggest what to re-check; it
  cannot create facts: no output type carries evidence references, and every
  prior-claim ref is permanently stale (`prior_claim_needs_recheck`).
  Candidates are deterministic, deduplicated, budget-bounded leads — never
  answers, never auto-executed research.
- Research admits continuity through one new context source key,
  `research_topic_continuity`, owned by the research profile only (the
  chat-side `ghost_directive` / `ghost_continuity` sources stay excluded).
  Admission runs through the shared chain — `ContextSource` -> profile
  allow-list -> `render_context_sources_with_metadata()` -> prompt envelope
  section. Intro rows are projected at the actual provider-send boundary:
  because the Research controller appends its action block after assembly,
  the assembled sections, the admitted source rows
  (`record_context_sources(..., epoch_id=...)`), and the outbound prompt are
  all bound to one content-addressed epoch computed over the exact sent
  bytes; intros that never reach a provider turn project nothing. The
  pre-send `research_request` prompt-section row was removed: it duplicated
  the model-visible `research_question` section without ever sharing its
  provider-turn epoch, and every research prompt-section row now carries the
  sent-bytes epoch. The section text says "not evidence ... re-check ... do
  not cite" and never contains Ghost / Work Queue / Concept Graph vocabulary
  in Codey-authored framing lines; follow-up material keeps using the
  separate `research_iteration_context`. Empty or gate-closed continuity
  renders to nothing, leaving the baseline intro byte-identical.
- TaskRunner wiring was thinned instead of growing: `_run_research_pipeline`
  now delegates to two helpers, `_build_research_topic_continuity` (profile
  gate -> interest hints via the new knowledge-layer `candidate_to_topic_hint`,
  bounded Ghost items, ledger claim refs -> projection; fail-open to the
  empty baseline on any error, leaving a bounded `warn` reason code in the
  run trace) and `_build_research_context` (assembles `ResearchContext`).
  No TopicManager / TopicStore / continuity runtime was added, and the
  research modules never import the Ghost runtime.
- The trace row is real: `RunTraceRecorder.record_research_topic_continuity`
  persists one bounded `research_topic_continuity` manifest section per run,
  keyed and deduplicated by content digest. Rows carry refs, counts, reason
  codes, warnings, and the digest — no raw hint text field exists, so
  prompt-lab material cannot leak into RunTrace or EvidenceLedger. Claim-ref
  inputs beyond the 16-row cap are counted before capping, so the
  `truncated` flag reports honestly. Admission is structurally closed: the
  required `epoch_id` has no default and must be a well-formed
  `ctx_epoch:<16 hex>` ref — anything empty or malformed fails closed
  without writing a row or touching the dedupe key, so an admitted row
  cannot exist outside the send-boundary binding. The projection sink
  (`RunTraceResearchSink`) exposes no continuity writer: the only path is
  the runner's gate plus `record_research_topic_continuity(...,
  epoch_id=...)` over the exact outbound bytes. The published claim stays
  honest by construction: rows prove what was bound to outbound
  provider-send attempt bytes, not that the model processed them.
- New manual harness `tests/manual/ghost_research_continuity_ab.py`:
  identical seeded state across arms with only the admission gate toggled.
  Every provider (real or stub) is wrapped in `TracingProvider`, so
  send/reply counters are attributable regardless of provider class, and
  live rows are classified as `provider_send_error`,
  `native_search_stall_suspected` (send timeout or sends-without-replies —
  a provider/native-web-search diagnostic, not planner quality), or
  `planner_quality:<stop_reason>`. Live runs journal through
  `ABJournalWriter`; `--transcript-mode digest-only|archive|off` controls
  transcript retention in the manual layer only. The harness now carries the
  selected provider id into the production `TaskRequest` and writes a terminal
  `run_complete` journal event, so live smoke provider attribution and
  manifest status match the run that actually executed.
- Verification: architecture tests lock topic_continuity as an I/O-free leaf
  and keep the whole research stack Ghost-import-free; capability registry,
  permission profiles, runner/pipeline forwarding, and TaskRunner admission
  all have deterministic tests, plus the harness pytest wrapper.
- Hardening batch (same-cycle fixes):
  - Shell-approval continuation can no longer swallow a user Stop: the
    guard is now atomic -- ``reserve_run(abort_if_stopped=True)`` re-checks
    the flag inside its lock, closing the check-then-act race where a Stop
    landing between an external peek and the reservation used to be cleared.
  - ``/api/new_chat`` and ``/api/changes/restore`` return 409 while a run is
    active for the same session/project; restore compares resolved paths and
    never blocks an idle server (the last-run project lingers in state by
    design).
  - New shared `codey/ghost/numbers.py` gives every Ghost store one finite
    unit-float contract: ``bool``, NaN, inf, and out-of-range values either
    fail closed (``coerce_unit_float``) or clamp deterministically
    (``clamp_unit_float``). schema/gate/inbox/router/hebbian/affinity/
    work_queue now share it -- NaN confidence could previously survive the
    router's range-only clamp.
  - Manual Ghost work requeue resets ``retry_count``, so items blocked at
    MAX_WORK_RETRIES become claimable again instead of being stuck forever.
  - StepFun keeps its verified newest-first DOM reading in the main path;
    the evaluate fallbacks were rewritten provider-locally (visible-only
    head-first scan) because the generic ``locate_response()`` walks
    tail-first and would read a stale reply on StepFun's newest-first DOM.
    The fallback is a two-step ladder that preserves the main path's
    ``.reason-render-ext`` filtering: a simplified string-arg JS first,
    then the pure locator scan as last resort -- for reads and for the
    response count alike, so a degraded baseline can never be inflated by
    visible reasoning copies.
  - Override workers get a dedicated stable browser profile per provider
    instead of attaching to the user's default profile from a second CDP
    port, parent-side workers drain child stderr into a bounded tail so
    startup crashes are diagnosable, the child entrypoint requires
    ``--profile`` (fail closed instead of falling back), and the self-repair
    helper gets its own isolated profile under
    ``state_home/self-repair/<provider>`` too -- with the manual live smoke
    wired for it and documenting the one-time login requirement.
  - The five web provider wrappers share one thin send/new_chat plumbing
    (`codey/providers/web_driver.py`): the outer deadline now covers
    ``response_timeout + grace + margin`` so drivers finish their own wait,
    and a firing deadline classifies as ``response_missing``, not transient,
    with full standard-capture diagnostics (url/title/stage/facts).
  - Research rigor: evidence excerpts longer than the 360-char display cap
    keep their exact matched text for proof locators, while the public
    payload boundary (`EvidenceItem.to_dict` / ``evidence_payload``) clips
    to the display form -- UI session state never stores unbounded excerpt
    text. Single-source citation inference was removed: unexplained body
    ``[n]`` refs fail compilation into repair instead of being silently
    remapped to the only source.

## 0.4.11 - Evaluation spine: regression gate + longitudinal harness + comparison benchmark

- New `codey/research/regression_gate.py` chains the Evidence Runtime
  snapshot, ResearchProofReview, Research Brief, Impact Contract,
  ReviewFinding, PlannerGap, Reproducibility Capsule, CompletionProof, and
  pipeline summaries into one end-to-end regression-tested read model. Output
  carries only bounded metrics, boolean observables, a gate verdict, reason
  codes, and bounded refs; raw prompts, replies, transcripts, and webpage
  bodies cannot enter a report by construction. It measures but never
  enforces: false completions are only counted (`false_completion_candidate`),
  blocking `done` stays with 0.4.13. Unknown expectation keys fail closed.
  Architecture tests lock the module projection-only (no I/O, providers, or
  journal) and keep it out of the research package's eager export surface.
- New frozen benchmark corpus `tests/fixtures/research_benchmark/`: six fixed
  cases (stale injection, conflicting sources, unsupported-claim injection,
  local CSV/PDF analysis, OSS ecosystem change, paper progress) split into
  development / held-out, with rubric weights plus hard gates and a
  `lock.json` recording every file's sha256. The offline validator
  `tests/manual/research_benchmark_suite.py` checks split integrity, fixture
  path containment (escapes fail), rubric weights summing to 1, vocabulary
  alignment with the regression gate, and lock hashes; `--update-lock` is the
  single explicit channel for intentional fixture changes. Raw-material keys
  (prompt/transcript/webpage) are banned inside case payloads.
- New longitudinal research harness
  `tests/manual/longitudinal_research_harness_ab.py` (deterministic by
  default, no network): multi-round runs of the same topic through the full
  production projection stack verify that old claims keep one
  content-addressed identity across rounds, stale sources are flagged before
  a revised conclusion counts, fresh evidence revises old conclusions,
  injected unsupported claims stay visible in the brief but never reach
  implementation constraints, conflicting evidence creates findings and
  planner gaps, and failed AnalysisRuns are never reported as reproduced.
- New comparison benchmark
  `tests/manual/research_comparison_benchmark_ab.py`: three deterministic
  arms (unstructured baseline report / OpenScience-style fixture / Codey
  evidence loop) scored with the frozen rubric. Wording is enforced in code:
  without a recorded real head-to-head artifact the summary may only say
  "OpenScience-style regression passed"; `--openscience-artifact` plus
  `--claim-superiority` is the only way "surpassed OpenScience" may appear,
  with the artifact digest recorded next to the claim.
- Extracted the shared manual A/B layer `tests/manual/ab_harness_common.py`:
  merges the TracingProvider wrappers, interleaved arm schedules,
  complete-matrix gates, atomic JSON writes, resume payloads with provider
  identity guards, journal directory derivation, and the fixture search
  provider that `research_to_code_ab.py` and `bounded_research_planner_ab.py`
  each maintained separately. Both existing harnesses migrated with unchanged
  behavior (all prior tests and self-tests pass); production imports of the
  manual layer are now banned by an architecture test.
- No production behavior change: no prompt, tool result, router/fallback,
  permission, UI/SSE, Research default path, or done-enforcement edits. Per
  the roadmap A/B rule, a projection/harness-only version takes no live
  provider A/B.
- Final release validation added deterministic gates plus limited Qwen live
  smoke. `research_to_code_ab.py` passed on Qwen with the projection arm
  preserving success/check behavior while removing raw excerpt, related-id,
  and trap conclusion noise from the handoff. `bounded_research_planner_ab.py`
  exposed a provider-state smoke issue: a paired Qwen run completed the
  baseline row, then the planner row failed after one send with no model reply
  while Qwen Studio was still inside its native web-search UI; a planner-only
  rerun completed and improved the fixture score. The new longitudinal and
  comparison scripts remain deterministic-only in 0.4.11, so this is recorded
  as diagnostic provider smoke, not statistical A/B or OpenScience
  head-to-head evidence.
- Review hardening (post-commit fixes):
  - The comparison benchmark's superiority wording guard upgraded from "any
    file unlocks it" to a structured schema gate: a head-to-head artifact
    must be JSON containing every roadmap-required metadata field (both
    sides' version/commit, provider/model, task inputs, run date, result
    source, scoring rubric), non-empty and bounded. Digest-only wrappers,
    unreadable JSON, non-object payloads, or missing fields fail closed; the
    CLI exits non-zero and the summary records `metadata` and `errors`.
    Validity derives from the payload itself, so hand-assembled digest
    wrappers can never unlock the wording.
  - The superiority gate then tightened from "metadata exists" to "the
    artifact's own result supports it": records must carry bounded result
    fields (`winner` in {codey, openscience, tie},
    `strictly_better_metric_count` at or above the roadmap threshold of 4,
    `regression_gates_passed: true`), and only results that actually back
    the claim unlock "surpassed OpenScience". Metadata-complete records
    where OpenScience/tie won, too few metrics improved strictly, or gates
    failed stay locked. Summaries expose `supports_superiority`, project the
    result fields into `metadata`, and `openscience_claim` now reflects the
    verdict — a failed gate run never says "passed". Text-field length caps
    plus task-input count/length caps moved into validation itself (no
    longer output-clipping only), and unreadable paths such as directories
    return `artifact_unreadable_file` instead of raising.
  - Schema validator hygiene (third review pass): `winner` is type-checked
    before set membership so unhashable JSON values (arrays/objects) yield
    `artifact_bad:winner` instead of a TypeError crash; the error list is
    truly bounded — one trailing `artifact_errors_truncated` marker, then
    recording stops; summary errors for invalid artifacts derive from the
    payload exactly like validity does (hand-assembled wrappers can no
    longer show empty errors next to "see errors"; stored loader reasons are
    used only when no payload exists, with `artifact_unverified` as last
    resort); and summaries add `codey_commit_alignment` — informational
    artifact-vs-current-HEAD display that never invalidates recorded
    results.
  - Superiority claims bind to the frozen rubric, plus remaining audit and
    environment edges (fourth review pass): an artifact's `rubric` must
    equal the current suite's frozen rubric name
    (`research_benchmark_v1`) — foreign-rubric records stay honest, valid
    evidence but cannot unlock "surpassed OpenScience"; metadata filtering
    now drops only empty strings/lists, so `winner="tie"`,
    `strictly_better_metric_count=0`, and `regression_gates_passed=False`
    survive into audit output instead of vanishing; and
    `current_codey_commit()` runs git with the repository root as cwd so the
    current commit resolves regardless of the calling process's directory.
  - Two-factor rubric binding + longitudinal fixture semantics fix (fifth
    review pass): superiority now also requires a machine-checked
    `rubric_digest`, taken from the frozen suite lock.json's
    `rubric.json` sha256 entry (one hash vocabulary, no second scheme).
    Missing or mismatched name/digest keeps the artifact a valid record but
    denies "surpassed OpenScience"; metadata projects both factors. The
    comparison matrix gate became exact — every arm exactly once — instead
    of dict-folding that let duplicated arms silently overwrite their twins.
    The longitudinal stale fixture now mirrors production's content-
    addressed claim ids: the old stable-v2 conclusion keeps its own id
    across rounds while the stable-v3 revision enters as a distinct claim
    with a new id linked by an explicit refutes relation to the superseded
    evidence, so the benchmark verifies "old claim relocatable + new claim
    revises under its own identity" rather than id-slot reuse.
  - Conflict-free handoff constraints + audit visibility (sixth review
    pass): the stale fixture goes one step further into production
    semantics — round 2 states only the current conclusion (stable-v3) and
    never restates the superseded stable-v2 as a second evidence_backed
    claim in the same record, which would hand the Writer two mutually
    exclusive verified implementation constraints. Superseded conclusions
    stay relocatable by their content-addressed ids; their evidence is
    retained as located source material and supersession is expressed via
    an explicit refutes relation. The frozen stale_claim_refresh case now
    also pins `conflicting_evidence_finding` (lock re-stamped); the
    longitudinal summary surfaces `review_ok` per round so "projection
    regression passed" is never misread as "research proof quality passed";
    and the comparison summary's `arms` became a list so duplicated arms
    stay visible after the exact-matrix gate fails.
  - The regression gate's record anchor is now validated through
    `normalize_runtime_ref(kind="research_record")`: hostile or wrong
    mappings cannot smuggle text into the refs-only payload; an invalid
    snapshot anchor falls back to a valid brief anchor, and with neither the
    report is not produced.
  - `_source_stale_facts()` no longer materializes the full source list
    before truncating; the iterable goes straight to
    `project_source_set()`, which owns its own bounded scan (redundant cap
    constant removed).
  - The shared `TracingProvider` timeout semantics now match their docstring
    as true pass-through: unconfigured and unprovided timeouts call bare
    `send(text)` / `new_chat()`, so plain scripted providers work directly;
    `close()` forwards only when the wrapped provider actually closes.

## 0.4.10 - Security and Integrity Hardening (review hardening)

- Local HTTP API is protected against DNS rebinding and cross-origin misuse:
  every request now validates the `Host` header against the loopback bind
  (plus a non-loopback bind address when explicitly serving LAN), and POST
  requests presenting a foreign `Origin` are refused with 403 before any
  handler logic runs.
- `/api/local_provider` no longer replays a stored credential against a
  different `base_url`: changing the target requires explicitly supplying
  that target's key, so a rebinding/XSS page cannot exfiltrate the saved
  key in one request. Only an existing config with the same stored
  `base_url` may reuse its key; orphaned legacy keys without a recorded
  base URL are probed with an empty key and cleared unless the user supplies
  a new one.
- `/api/stop` now expires every pending shell approval under the same lock
  and emits denied `shell_result` events; a stale Allow card can no longer
  execute a command after the user pressed stop.
- UI state persistence no longer loses research data while keeping a narrow
  state boundary: session sanitizers preserve the frontend's
  `researchRuns` shape through a whitelist capped at 32 runs and store
  `research` as the boolean UI flag it already is; message sanitizers keep
  `toolKey` / `activity` / `pending`, so restarts stop erasing research
  history and pending tool cards survive round-trips.
- Snapshot/untracked diffs render correctly again: `keepends=True` fed into
  `unified_diff(lineterm="")` plus `"\n".join()` double-spaced every content
  line for non-git projects; both diff builders now use plain `splitlines()`
  with golden assertions.
- User source files are written atomically and EOL-preserving via
  `codey/atomic_io.py` (`write_text_atomic`: unique same-directory temp
  file opened with `xb`, fsync, existing file mode copied before
  `os.replace`, CRLF/LF style retained). Wired into write/edit tool paths
  and snapshot restore, matching the "written atomically" promise in the
  tool contract without dropping executable bits on POSIX. If a replace
  fails after inheriting a read-only target mode, the temp file is chmod'd
  writable before cleanup so Windows does not leave `.target.<uuid>.tmp`
  behind.
- Digest vocabulary split to kill the同名双义: `refs.digest_ref` becomes
  `refs.content_digest` (producer: any value -> sha256 content digest);
  `research.shape.digest_ref` becomes `shape.valid_digest_ref` (validator:
  returns the value only when already a well-formed sha256 ref). All call
  sites updated; nothing imports the old names.
- Evidence ledger integrity is now verified on load over the full record
  capsule, not just the record row: each record entry carries a
  canonical-JSON `record_integrity` digest over the entry (minus that field)
  plus every referenced source/evidence/claim/assumption/relation map row.
  Any mismatch or missing field fails the whole ledger closed
  (`ledger_unavailable`) instead of serving tampered history. Records
  without their own raw `record_digest` are rejected before projection
  instead of minting the empty-string digest. Append-time shared-map id
  collisions are now rejected before any write: if a new record reuses an
  existing evidence/claim/assumption/relation id with different canonical
  content, or reuses a source id with different identity fields
  (final URL ref when known, host, content hash, content kind), the record is skipped with
  `ledger_id_collision` and the previous ledger payload is left
  byte-for-byte unchanged. Legitimate repeat captures of the same source do
  not collide merely because observation fields changed; retrieved time,
  pages read, truncation, and conservative quality hints are merged.
- Report section boundaries hardened: bare numbered headings documented in
  the README (`1. Conclusion`, `一、结论`) are recognized again, common
  Chinese section titles (`参考文献`, `风险`, `备注`, `方法`) joined the
  alias table, short lead-in colon lines (`具体如下：`) no longer cut their
  section, and unknown markdown headings route to a dropped unknown bucket.
  Writer-visible research handoff now keeps Key conclusions
  citation-map-backed: conclusion lines must cite a number present in the
  rendered Citation map through the shared citation scanner. Fake bracket
  citations such as `[99]` are demoted, later supported conclusions are not
  lost behind early uncited noise, and uncited conclusions remain visible only
  as capped `[uncited]` limitations after real counterpoints. Adjacent refs
  such as `[1][2]`, page refs such as `[1 p.4]`, and code-like text such as
  `array[0] per [1]` follow the same rules as the Research done gate.
- Research projection boundaries are now declared metadata, not comments:
  `CapabilitySpec` gained `projection_audience` / `canonical_inputs` /
  `fail_mode` / `release_gate` with validation (projection capabilities must
  declare an audience; behavior-input projections must name canonical input
  capabilities; model-visible projections must declare a release gate).
  Every triggered spec is annotated; research-owned projection count is
  capped by test; architecture tests forbid behavior-side research modules
  from reading trace/UI projections and restrict profile+source-trust
  combination to zero import sites today.
- Smaller fixes: nested evidence-profile merges flatten and cap their atomic
  "+" segments before computing merged values (no synthetic combo-looking ids
  like `finance_legal+science`, and no fifth atom leaking into the profile);
  RunTrace's brief-projection claim rows clip text before hashing and store
  digests only; `test_server.py` installs module-level guards so tests
  cannot open real provider tabs (the two receipt/memory tests patch both
  connectors); `tests/conftest.py` suppresses pytest's Windows-only
  `pytest-current` symlink cleanup `PermissionError` at atexit without
  changing temp paths and re-raises unrelated permission errors;
  `tests/manual/research_to_code_ab.py` now records `run_complete` so live
  journal manifests end as `done` or `failed`, and its gate includes the
  structural `projection_trap_not_in_key_conclusions` check;
  `tests/test_work_checkpoint_flow.py` disables post-task
  audit/consensus/advisor side effects (~137s -> ~4s); StepFun submission
  gains GLM-style double-click protection; `task_runner` restores the
  previous cancellation event on every pre-start failure path; shell approval
  continuation now waits briefly for the just-interrupted approval run to
  release the single task slot, so a fast Allow click can execute the command
  once and still resume the interrupted task;
  `context_epoch` marks clamped admissions as truncated; reopened run
  ledgers continue both byte budget and event sequence from the existing
  file; knowledge search escapes LIKE wildcards with explicit SQLite
  `ESCAPE` clauses; `Assumptions:` is a section boundary that cannot pollute
  conclusions; digest producer/validator helpers are banned from neutral
  `_digest_ref` aliases by architecture test; hebbian delete-path wraps
  projection writes symmetrically with reinforce.

## 0.4.10 - Domain Source Trust + Research Brief Projection

- Added `codey/research/domain_profiles.py`: evidence-standard profiles as
  data. An `EvidenceProfile` is a small vector of expectations
  (freshness, source quality threshold, primary-source preference,
  counterevidence requirement, analysis-for-data-claims, preferred and
  disfavored source kinds, preferred connector kinds) -- it states what kind
  of evidence makes a claim in this kind of task more credible and never
  judges whether a conclusion is true. Six atomic builtins ship:
  general / finance / legal / market / science / software_research.
  Cross-domain tasks compose at runtime via `merge_profiles`, which merges
  each ranked dimension to the stricter value and unions tuple dimensions;
  composition is capped (`MAX_MERGE_PROFILES=4`) with an explicit
  truncation warning, merged ids use "+" so they can never be mistaken for
  builtin ids, and there are no combination profiles, no inheritance, and
  no keyword-based domain inference anywhere (unknown labels fall back to
  `general` with an `unknown_profile_label` warning). The module is a pure
  stdlib leaf locked by architecture tests: no codey imports, no I/O.
- Added `codey/research/source_trust.py`: deterministic projection of what
  a source objectively is onto a low-dimensional class taxonomy
  (official / primary / peer_reviewed / preprint / dataset / filing /
  standard / repository / issue / release / news / secondary / forum /
  social / aggregator / unknown), derived only from facts the source already
  carries (host suffix, declared quality level/kind/freshness). No network,
  no page bodies, no URL pattern tables beyond stable host-suffix rules,
  and no deletion or filtering of evidence -- consumers may only turn
  projections into warnings, preferences, or threshold hints. The aggregate
  source-trust warnings previously inlined in `research/proof_quality.py`
  moved here verbatim as the single owner; proof review output stays
  byte-identical while the duplicated rule set is gone (one real complexity
  reduction for this release). `evaluate_against_profile` combines
  projections with a quality floor into bounded counts/warnings without ever
  removing rows below the floor.
- Added `codey/research/brief_projection.py`: refs-only research brief
  projection plus an explicit Research-to-Code impact contract.
  `ResearchBriefProjection` carries validated runtime refs, bounded claim
  summaries (status + text <= 260 chars), open questions, counts, and
  warnings; raw synthesis bodies, webpage content, and transcripts never
  enter it. `ResearchImpactContract` separates affected files, verified
  implementation constraints, test suggestions, risk notes, out-of-scope
  items, and decision refs, with one hard boundary enforced by tests:
  unsupported claims are demoted into risk notes and can never back an
  implementation constraint, affected-file paths are validated against
  escape patterns, and `test_suggestions` are writer context that authorize
  nothing. `render_handoff` renders the short structured handoff for future
  consumers.
- RunTrace gained two bounded sections owned by the new capability:
  `research_source_trust` (per-source class/tier/freshness rows capped at
  32) and `research_brief_projections` (record-anchored brief payloads
  capped at 8). Both fail closed on half payloads, validate refs against
  runtime ref kinds, sanitize reason codes, dedupe, append truncation
  warnings, and never store raw prompts, transcripts, or output bodies.
  The research pipeline records both projections next to findings/planner
  gaps after a run's final proof review; they stay audit-only read models
  on the trace sink and cannot influence search, planner behavior, prompts,
  provider selection, permissions, or done semantics.
- Registered the metadata-only `research_source_trust` capability
  (provides evidence_profile/source_trust/brief projections; consumes
  research_object_model + research_evidence_runtime + run_trace) and added
  the two trace sections to `KNOWN_TRACE_SECTIONS`.
- The dry-run query planner accepts an optional `evidence_profile` that may
  only prepend bounded, availability-checked connector preferences with an
  explicit `domain_profile_source_preference` reason code (score 0.92);
  unknown profile kinds yield a bounded `domain_profile_kind_unavailable`
  reason instead of guesses. Callers that pass no profile get plans
  byte-identical to 0.4.9, and the proof-ok short circuit still ignores
  preferences entirely.
- Debt reduction in `knowledge/brief.py`: the local heading-scanning parser
  (`_extract_section_lines` / `_extract_sources_section`) is gone; the
  brief now projects note bodies through `codey/report_sections.py`, a
  neutral stdlib-only leaf that also owns section parsing for report
  quality review -- one parser, and the knowledge layer no longer reaches
  upward into the eager research package (locked by an import-isolation
  test). Section boundaries are strict: every markdown heading or short
  colon-style title switches the section, and unknown titles route their
  content to a dropped unknown bucket, so legacy or custom reports can no
  longer smuggle `风险:`/`方法:` prose into the writer's conclusions. The
  unbounded raw-report excerpt ("Synthesis excerpt", up to 3600 chars of
  note body) no longer enters the Writer handoff, and related-note id noise
  was dropped from it; every remaining line comes from a named section, and
  long lines are clipped instead of silently dropped. This changes
  Writer-visible research context text, so enabling it in a release
  requires running the dedicated live A/B first (see below).
- RunTrace stays transcript-free: `research_brief_projections` rows no
  longer carry claim texts or open questions -- claim rows keep only
  claim_ref / status / evidence_count / text_digest, resolvable against the
  research record's own bounded payloads. The model-visible handoff keeps
  its short bounded texts; only the audit side becomes digest-first.
  Merged evidence-profile payloads keep the "+" composition marker instead
  of sanitizing into names that look like builtin combination profiles.
- Added `tests/manual/research_to_code_ab.py`, the roadmap's release-gate
  probe for Writer-visible handoff changes: two arms (0.4.9-style baseline
  render vs structured projection render), same fixture project, same
  synthesis note content, same Writer task. Arm order interleaves per
  repeat to cancel warm-session/order bias. The process exit code IS the
  gate verdict: it fails when the projection arm regresses on any gate
  metric (success, key-conclusion retention, trap misuse, independent
  verification pass) or any row errored -- a crash-free run with bad
  results is a failure, not a pass. The run matrix itself is part of the
  gate: every (case, repeat) pair must have exactly one baseline and one
  projection row, so unbalanced or truncated runs cannot hide a regression.
  By default every prompt/reply exchange
  is recorded into a hash-chained `ABJournalWriter` journal with full
  transcript archiving (`transcripts/<digest>.json`) for offline replay;
  `--no-live-trace` disables it. Transcripts stay manual-layer material and
  never enter RunTrace/EvidenceLedger/production evidence. A
  scripted-provider self-test keeps the whole harness runnable offline
  (`--self-test`), and its scoring/builders/gate/schedule are covered by
  unit tests without any provider traffic.
- Groundwork status: `resolve_profile`, `evaluate_against_profile`,
  `ResearchImpactContract`, and `render_handoff` are deterministic APIs
  consumed by tests and trace recording only. Production paths do not
  select or apply evidence profiles yet (no keyword/domain inference by
  design), the planner ignores profiles unless a caller passes one
  explicitly, and nothing user-visible changes until those consumers ship
  with their own gates. Capability metadata mirrors module ownership:
  `domain_evidence_profiles` / `research_source_trust` /
  `research_brief_projection` are three separate boundaries.
- Source-trust host matching hardened end to end. The domain tables (gov/mil
  suffix shapes incl. compound ccTLDs, edu/ac.uk, dataset repositories, news,
  blog, forum, social, preprint, peer-reviewed, repo, filing, standard) moved
  into one stdlib-data leaf, `codey/research/source_domains.py`, consumed by
  both the capture-time quality classifier (`ledger.classify_source_quality`)
  and the trust projection -- the two layers can no longer drift apart, so a
  lookalike URL such as `sec.gov.evil.example` gets a plain web/secondary
  stamp at capture time instead of an official one that would bypass the
  suffix table later. Defense in depth at the projection: declared `quality`
  kinds may only ever assign middle/weak classes; strong classes derive from
  the host's registered shape alone, so even a forged official/data stamp
  projects to unknown rather than tier-3 trust. Locked by lookalike tests on
  both layers plus an end-to-end classify->project test.
- Malformed hostnames fail closed everywhere. The shared hostname-shape
  predicate (`refs.is_valid_hostname`: no empty labels, no doubled dots, no
  bare single labels, RFC label characters only) now gates both the trust
  tables (`.gov` / `evil..gov` / `.edu` can never match a suffix) and the
  research URL guard: `check_fetch_url("https://.gov/x")` returns the denial
  reason "invalid URL host" on every path instead of escaping as a resolver
  UnicodeError that could abort plan preflight mid-run.
- The strong `dataset` class is host-backed and reachable again: registered
  data repositories (data.gov, data.nasa.gov, data.europa.eu, zenodo.org,
  figshare.com, kaggle.com, archive.ics.uci.edu) project to tier-3 dataset,
  while a declared data kind alone still cannot mint the class -- keeping
  the science/finance/market profile preferences meaningful without
  reopening the forgery hole.


## 0.4.9 - Research Contract Lite + Verified Completion Gate v1

- Added `codey/completion_contract.py`, the domain-neutral pure projection
  core of the Verified Completion Gate. `CompletionContract` /
  `CompletionCheck` / `CompletionProof` carry only statuses, reason codes,
  and bounded refs; status derivation is a hard gate (any failed check ->
  failed, required-but-unrun -> blocked, pass + limitations ->
  complete_with_limitations, otherwise complete) with no scoring. Coherence
  is owned by the primitive itself: a satisfied proof never carries a
  blocked_reason, junk input without valid ids fails closed to an empty
  projection, and empty checks cannot become a contract. v1 deliberately has
  no separate Requirement object -- requirements and checks are 1:1 at this
  stage, and parallel lists would just duplicate state.
- Added `codey/research/contract.py`: projects a `ResearchProofReview` plus
  its derived ReviewFindings into the shared contract/proof shapes. Open
  critical findings block a clean complete; because every critical finding
  kind is a projection of a hard proof-review failure, a passing review can
  never produce one, so queued research outcomes are item-for-item identical
  to 0.4.8 (no A/B required).
- Converged `research/completion_gate.py`: the observable contract is byte
  identical (same actions, blocked_reason strings, and proof_refs assembly),
  while internally it now consumes the contract projection; the stringly
  `_blocked_reason()` evidence semantics moved into research/contract.py as
  the single owner, and `safe_run_ref()` moved up into completion_contract.py
  as the domain-neutral run-ref sanitizer shared by research and coding
  proofs. `ResearchCompletionDecision` gains an optional `proof` field for
  trace recording.
- RunTrace gained a bounded `completion_proofs` section (proof-row cap 8;
  per-proof check cap shared with `CompletionContract` at
  `MAX_COMPLETION_CHECKS`): refs, statuses, check summaries, and reason
  codes only. finding/analysis/artifact refs validate against runtime ref
  kinds, unknown domains/statuses/check rows fail closed, proof-row
  truncation appends a warning, and raw `satisfied` mappings are ignored in
  favor of deriving coherence from `status`. The raw mapping boundary also
  enforces the contract shape: proofs with no valid check rows are dropped,
  and `complete_with_limitations` must carry at least one valid
  `limitation_refs` entry. Payloads never contain raw prompts, transcripts,
  or output bodies.
- Research queued completion now persists the generated `CompletionProof`
  into RunTrace on both complete and blocked paths, instead of only writing
  `proof_refs` back to the queue item. `complete_with_limitations` is no
  longer globally satisfied: only clean `status == "complete"` yields
  `satisfied=True`, so future enforcement cannot accidentally treat a
  limited or unobserved verification as a clean completion proof.
- Coding-side shadow completion proof: after a done project run ends, the
  proof is projected from existing local facts (changed files, selected
  verification candidate, post-edit check outcomes, executed AnalysisRun
  records) into the trace. Local verification freshness is an explicit
  tri-state -- fresh_pass / fresh_fail / unobserved -- so reads and searches
  (which are tool events too) can never masquerade as a failed verification,
  and a stale or missing run is recorded as unobserved rather than as a
  failure. Unobserved stays honest in both directions of the agent's report:
  a reported-green yields complete_with_limitations(
  verification_not_locally_observed), while a falsy reported value only
  blocks -- `RunResult.checks_passed` starts as `False` and is reset by
  edits, so an absent local observation can never be promoted to "verified
  bad"; failure is reserved for locally observed covering checks that
  actually failed. The agent's own reported checks are captured before the
  receipt's local override so the proof never mistakes the override for a
  claim; docs-only changes yield complete_with_limitations(docs_only_change);
  no matching verification command yields
  blocked(no_matching_verification_command). A model claiming "tests pass"
  is never local proof. done/receipt/prompt/SSE are unchanged.
- Completion proofs cite provenance, not just verdicts: analysis_run_refs
  attach the actual executed runs behind the decisive checks -- only the
  commands that cover the selected candidate and determined the state
  (fresh-pass cites its passing run, fresh-fail cites its failing run,
  unobserved cites nothing). Matching is cwd-aware through the same
  project-relative path digest the AnalysisRun projection uses, so the same
  command under two packages of a monorepo cites its own execution, never a
  sibling's; redacted commands keep digest-only provenance in the
  analysis_runs section.
- The shared bounded-ref vocabulary moved out of the research namespace into
  two domain-neutral stdlib leaves: `codey/refs.py` (clip / identifier /
  bounded_refs / digests / stable_ref) and `codey/redaction.py` (secret
  marker/shape/code predicates); `research/identity.py` keeps only the
  research-specific URL/project/path helpers and imports the primitives from
  `codey/refs`. No compatibility shims: every importer was updated. An
  architecture test locks both new leaves as stdlib-only, so coding, research,
  and future experiment projections share one dialect without cross-domain
  imports.
- Contract ids are content-addressed over every payload field: finding,
  analysis-run, artifact, and external refs are hashed alongside checks and
  limitations, so two contracts that differ in any carried reference can
  never share a contract_id (proofs derive their id from it and RunTrace
  dedupes by proof id).
- Real debt reduction in `task_runner`: the `select_verification_candidate` +
  `check_covers_selected_candidate` evaluation now happens in exactly one
  place shared by the receipt decision and the shadow proof instead of being
  computed twice. The roadmap now tracks the remaining receipt verification
  provenance debt explicitly: before completion proof enforcement, the legacy
  `checks_passed` inheritance path should be split into explicit provenance
  fields instead of preserved as cold-start compatibility.
- Capability registry adds metadata-only `completion_contract`
  (model_visible=False, trace_sections=("completion_proofs",)); architecture
  tests lock both new modules as projection-only (no runtime imports, no I/O
  tokens).

## 0.4.8 - Safe Context Epoch + Capability Boundary v1

- Added `codey/context_epoch.py`: a pure stdlib-leaf projection over model
  visible context facts — `ContextEpoch` / `ContextAdmission` /
  `ContextSnapshot` read models, content-addressed `ctx_epoch:<16hex>` epoch
  ids derived from the outbound prompt bytes, stable `context_source_ref()`
  normalization, and one shared admission projection
  (`admission_from_rendered_source()`) consumed by both
  `snapshot_from_rendered_sources()` and RunTrace's context-source rows, so
  production and tests share a single ref/digest vocabulary. The module
  performs no I/O and imports nothing from codey; an architecture test locks
  it as a projection-only leaf. Empty or unusable source keys fail closed:
  they produce no ref and the source is skipped instead of emitting an
  incomplete `context_source:` entry.
- Provenance closure: every model-visible row of a coding run is bound to
  the content-addressed epoch of the prompt that actually leaves.
  `agent.project_intro()` renders the final prompt first and stamps its
  sections, admitted context-source rows (via the new
  `record_context_sources(..., epoch_id=...)` binding), and the outbound
  prompt recorded through `record_provider_send_prompt()` with the same
  epoch id. Follow-up tool-result turns prepare `coding_current_context`
  rows without an epoch and bind them at send time; when a conversation
  rollover replaces the prompt with a fresh intro, the stale prepared rows
  are discarded instead of being attributed to a prompt that never leaves.
  Real-run tests lock the contract for both the intro turn and a follow-up
  tool-result turn. The existing internal rollover summary prompt is also
  recorded as a digest-only `conversation_handoff_summary_prompt`
  provider-send row with `capability_id="conversation_handoff"`, so it is no
  longer a hidden model-visible send. Chat-mode sends carry
  `capability_id="chat_runner"` with their own payload regression, and
  `coding_request_context` source refs are built through the shared
  `context_source_ref()` helper so production keeps one ref vocabulary.
  Epoch ids identify turn *content*,
  not numbered provider calls: identical re-sends share the id by design
  and stay deduplicated in the trace, while any byte difference yields a
  new epoch.
- Extended the shared ContextSource contract: `ContextSource` /
  `RenderedContextSource` now carry optional `capability_id` and
  `admission_reason` metadata (default empty). Rendering order, clipping,
  failure policy, and rendered text are unchanged byte-for-byte; agent.py's
  nine run-start sources are stamped via one small `intro_source()` factory
  instead of repeating every field nine times.
- Prompt envelope sections carry the same three optional fields
  (`epoch_id` / `admission_reason` / `capability_id`) through render and the
  fail-open trace sink. Metadata keywords are appended to trace calls only
  when present, so legacy trace sinks keep receiving the exact same keyword
  contract as before.
- Added one shared `record_provider_send_prompt()` projection and deleted the
  nine hand-written copies of the same provider-send block across
  `agent.py` (3), `server.py` (2), `task_runner.py` (1),
  `research/runner.py` (1), and `consensus.py` (delegating
  `_trace_model_prompt`). Every outbound prompt section is now stamped at a
  single place with the provider_send freshness, a content-addressed epoch
  id (an explicit `epoch_id=` override lets callers share an already-computed
  epoch), and the fixed `provider_turn_boundary` admission reason. Prompt
  text, send order, and provider behavior are unchanged; the same helper now
  wraps the two existing conversation handoff summary sends in `agent.py` and
  `task_runner.py`. Parity stays locked by the existing byte-for-byte agent
  prompt test plus the real-run metadata tests (these caught a double-wrapped
  trace sink during development).
- Run Trace: `PromptSectionTrace` gained optional `epoch_id`,
  `admission_reason`, and `capability_id` fields, serialized only when set —
  without them the manifest payload shape is unchanged. The prompt-section
  dedup key now includes the epoch id: unchanged repeats collapse exactly as
  before, and any content change produces a new epoch and a new row.
  `record_context_sources()` projects rows through the shared admission
  projection and binds them to a supplied epoch; per-source admission
  reasons win over the caller's fallback argument.
- Capability Registry v1 completed its roadmap field set: specs now declare
  `trace_sections`, `context_sources`, `evidence_producer`, and
  `enabled_by_default`, validated against new `KNOWN_TRACE_SECTIONS` /
  `KNOWN_CONTEXT_SOURCES` allowlists at construction time. Registered the
  0.4.7 modules (`research_evidence_runtime`,
  `research_review_finding`) plus this version's boundaries (`context_epoch`,
  `conversation_handoff`, `chat_runner`, `consensus_advisors`) and filled
  factual ownership for existing specs: agent_runner owns eight coding context sources,
  local_context owns ghost_directive/ghost_continuity, policy_guard writes
  policy_decisions, and the object-model/ledger/proof-quality/query-planner/
  finding specs name the dedicated trace sections their projections produce.
  Chat-mode outbound prompts carry `capability_id="chat_runner"` and
  rollover summary prompts carry `capability_id="conversation_handoff"`
  instead of anonymous provenance. A new architecture test locks every
  `capability_id` literal stamped anywhere in production code to a registered
  capability.
- Scope notes: no prompt wording change, no context ordering or budget
  change, no router/fallback/permission change, no planner or finding
  behavior change, no plugin loader, no skill system, no config UI, no new
  model-visible capability. Metadata and trace projections only, so per the
  roadmap A/B rule this version needs no live A/B; that becomes mandatory
  the moment findings or gaps start influencing prompts, planner behavior,
  or report contracts.

## 0.4.7 - Evidence Runtime + ReviewFinding Core v1

- Added `codey/research/evidence_runtime.py`: one deterministic validator for
  every research runtime ref (`source/evidence/claim/assumption/relation/
  research_record/research_proof/research_plan/analysis_run/artifact/
  artifact_version/review_finding/planner_gap:<16hex>` plus bounded `run:` ids)
  and `snapshot_from_research_record()`, which projects a typed or mapping
  ResearchRecord together with its proof review, analysis runs, and artifact
  versions into a bounded `EvidenceRuntimeSnapshot` read model (validated refs,
  digests, allow-listed answer status, counts; no raw text). Typed and mapping
  proof-review inputs both preserve the proof review's `question_digest` instead
  of manufacturing a new digest.
  This replaces the per-module copies of the same ref regexes: artifact
  lineage's `is_valid_derived_ref()` now delegates to the shared validator with
  an explicit narrow kind allowlist (`source/evidence/analysis_run/run`), with
  accept/reject behavior preserved exactly.
- Added located proof diagnostics: `_review_relations()` now also emits
  `ProofDiagnostic(reason_code, claim_ref, evidence_ref, source_ref,
  relation_ref)` alongside the unchanged hard-failure reason codes, and
  `ResearchProofReview` carries them in a new `diagnostics` field with a
  `diagnostics_payload()` accessor that revalidates refs through Evidence
  Runtime before emitting them. Diagnostics are deliberately NOT serialized by
  `to_payload()` / `to_trace_payload()` and do not affect `proof_ref`, so
  existing payload/trace shapes stay byte-identical.
- Added `codey/research/review_finding.py` (pure projection, no runtime
  imports): stable `ReviewFindingRecord`
  (`finding_id/kind/severity/status/target refs/reason_codes/addressed_by/
  confirmed_by`), `PlannerGap`, and `ReviewFindingEvent`; v1 carries no
  free-form `message` field.
  - `findings_from_proof_review(review, snapshot)` projects diagnostics plus
    record-level warnings into located findings; when a snapshot is supplied,
    refs outside the record graph are dropped instead of being invented.
    Kinds: `unsupported_claim` / `citation_mismatch` / `stale_source` /
    `overreach` / `missing_counterevidence` (+ enum-only
    `failed_analysis_support` producer via `failed_analysis_findings`,
    `contradictory_sources`, `source_conflict`, `qualified_support` reserved
    until real producers exist).
  - `planner_gaps_from_findings()` maps actionable findings to gap kinds
    (`followup_search` / `locator_verification` / `counterevidence_search` /
    `refresh_query` / `rerun_analysis`) as deterministic read models that plan
    nothing by themselves.
  - `apply_finding_events()` implements the append-only lifecycle:
    `open -> addressed -> confirmed/rejected`. `confirmed` requires
    `verified_by` from a fixed allowlist (`deterministic_check`,
    `analysis_run`, `opened_source_evidence`, `reviewer_pass`); model
    self-reports fail closed.
  - The existing `codey.reviews.core.ReviewFinding` parser object is intentionally
    not migrated; integrating code review findings waits for a real consumer.
- ResearchPipeline now projects findings once, after the final proof review:
  final review -> EvidenceRuntimeSnapshot -> ReviewFindingRecords ->
  PlannerGaps -> trace sink only. The planner does not consume gaps; follow-up
  search behavior is unchanged. Projection failures fail open without touching
  task completion.
- Run Trace gained two bounded sections: `research_review_findings` (cap 16)
  and `research_planner_gaps` (cap 16), storing validated refs, fixed-allowlist
  `kind`/`gap_kind` plus `severity`/`status`, and bounded reason codes only —
  no raw claim text, webpage body, stdout/stderr, provider transcript, or
  free-form messages. Recorder entries are deduplicated by id, invalid shapes
  or taxonomy values are dropped or normalized at the recorder boundary,
  overflow keeps the newest entries and appends truncation warnings. Without
  findings the manifest shape is unchanged apart from two empty lists.
- Architecture tests now lock Evidence Runtime and ReviewFinding as
  projection-only modules: no browser/provider/tool_runtime/task_runner/server/
  managed_outputs/events/ghost/codey.reviews.core/journal imports and no I/O tokens;
  the A/B journal boundary tests already cover all research modules including
  the new ones.
- Scope notes: no model critic, no prompt changes, no tool result changes, no
  UI, no report contract change, no graph database, no new model-visible
  capability. Deterministic projection only, so per the roadmap no live A/B is
  required; A/B becomes mandatory when findings start influencing prompts,
  planner behavior, or the report contract.

## 0.4.6 - A/B Observation Journal + Transcript Replay Cache v1

- Added a shared A/B observation journal for manual harnesses
  (`tests/manual/ab_journal.py`): single-writer append-only JSONL events with
  flush/fsync and a verifiable sha256 hash chain, tail recovery after
  interrupted runs, identity fail-closed manifests (one experiment/run/provider
  per directory), and `completed_case_keys()` resume support.
- Journal identity is enforced from the events themselves, not only from the
  manifest: `verify_event_chain()` reports mixed experiment/run/provider within
  one chain, and a writer refuses to open when existing events carry a
  different identity even if the manifest is missing, corrupt, or replaced.
- Reader verification surfaces unparseable-line counts (`mid_file`/`tail`) so
  garbage lines can no longer hide behind otherwise-clean chain verification.
- Strict JSON durability: non-finite floats are dropped during fact
  sanitization and event lines serialize with `allow_nan=False`, so
  `events.jsonl` never contains NaN/Infinity tokens. Mid-file unparseable lines
  are no longer silently cleaned by writer auto-recovery — appending refuses
  until an explicit `ABJournalReader.recover_tail()`.
- Provider observation facts cross the boundary through per-event typed
  schemas: unknown fact names such as `page_text`, `response_text`, and
  `cookies` are dropped before value sanitization. URL/HTML/cookie-ish values,
  secret-shaped values, opaque objects, and generic nested maps are redacted or
  dropped; nested `provider_failure` keeps only `kind`/`stage`
  (as `provider_failure_kind` / `provider_failure_stage`), so raw provider
  error messages and page titles cannot re-enter the journal.
- Harness run ids follow the final provider-specific output name
  (`output.stem` after all-mode renaming) instead of wall-clock time, so
  resuming any result file — including per-provider files renamed in all-mode —
  continues the same journal identity instead of colliding with the previous
  manifest.
- Added `TranscriptReplayCache`: prompt/reply pairs are digest-only by default;
  explicit archive mode stores content-addressed bounded transcripts under
  `transcripts/<digest>.json` for manual replay/scoring only, with explicit
  `delete_transcript()` and `prune_transcripts()` retention helpers.
- Migrated `bounded_research_planner_ab.py` and `source_connector_ab.py` onto
  the shared journal and deleted their duplicated LiveTrace implementations;
  trace output is now a `<stem>.trace/` directory (`manifest.json`,
  `events.jsonl`, optional `transcripts/`). Result JSON shapes are unchanged,
  so historical results remain readable. Connector case-start calls were fixed
  to the new signature and both self-tests replay the full per-case event
  sequence as a regression lock; both harnesses also execute as package modules
  (`python -m tests.manual.<harness>`). `deep_research_core_ab.py` migration
  is deferred.
- Locked the layer boundary in architecture tests: production layers
  (run_trace/research/task_runner/server) cannot import the journal, the
  journal cannot depend on production orchestration, and transcripts cannot
  reach EvidenceLedger/ObjectModel.

## 0.4.5 - AnalysisRun + Reproducibility Capsule v1

- Added `AnalysisRun` projections for audited local command runs (`codey/research/analysis_run.py`):
  each project `run` tool execution is now projected into a bounded, deterministic record with
  command digest, sanitized display command, cwd ref, exit code, started/finished timestamps,
  duration, capture quality, and an allow-listed environment summary digest.
  No raw stdout/stderr, no script/dependency fingerprints, and no runtime imports: the projection
  consumes normalized metadata mappings only.
- Added minimal Artifact lineage (`codey/research/artifact_lineage.py`):
  Managed Output handles now project into stable content-addressed `artifact:<16hex>` /
  `artifact_version:<16hex>` refs with sha256, bounded size, pinned `text/plain` mime,
  origin run id, and producing analysis run. Derived refs validate against the
  Source/Evidence/AnalysisRun/Run prefix allow-list; malformed digests fail open to no lineage entry.
- Added Reproducibility Capsule aggregation (`codey/research/reproducibility.py`):
  one bounded per-run snapshot of analysis runs, captured artifact versions, environment digest,
  and an honest reproduction status (`no_analysis_runs` / `output_captured` / `output_not_captured`
  / `failed`) that never claims more than v1 can verify. Capsule snapshots replace by id instead of
  accumulating stale states.
- Extended Run Trace with three bounded audit sections:
  `analysis_runs` (cap 8), `artifact_refs` (cap 16), and `reproducibility_capsules` (cap 8) with
  generated-ref validation, deduplication, truncation warnings, and no raw output storage.
- `run_command_raw()` now records audit-only timing (`started_at` / `finished_at` / `duration_ms`);
  timed-out commands carry timing too because the process did launch. The fields flow through
  `ToolOutcome.audit` only. The model-visible `model_text`, UI/SSE payload shape, and managed-output
  footer are byte-identical, including the timeout `ERROR:` prefix, locked by characterization tests.
- Hardened AnalysisRun projection after review:
  - `tool_id` now records the actual UI/runtime tool instance id (`turn:index`) and
    `tool_name` separately records `run`, so trace entries line up with UI payloads without
    overloading one field.
  - `command_display` is redacted (kept empty with a `command_display_redacted` warning) when the
    command matches secret-looking signals, matching ProjectFacts' refusal to persist such commands;
    the digest stays authoritative. `RunTrace.record_analysis_run()` repeats the same display
    redaction at its recorder boundary for direct callers.
  - Only real executions become AnalysisRun records: outcomes without execution timing (policy
    denial, invalid cwd, command not found) stay out of the trace, while timeouts are recorded as
    honest failures.
  - `duration_ms=0` no longer reports `timing_unavailable`.
- Managed Output audit payloads now carry `stored_truncated`, so artifact lineage knows when the
  locally stored output itself was secondarily truncated (`normalized_managed_output()` passes it
  through).
- Derived lineage refs are shape-checked instead of prefix-checked:
  `source/evidence/analysis_run:<16hex>` plus `run:<bounded-id>`; URLs and free text fail closed,
  enforced in both the projection module and `run_trace.record_artifact_refs()`. The projection now
  accepts `derived_from` only as a list/tuple, and the recorder requires both valid `artifact_id` and
  `version_id` before storing artifact lineage.
- Replaced the lexicographic tuple ranking in candidate selection with an explicit
  `ResearchCandidateScore` dataclass whose field order documents the priority order
  (proof-complete dominance, stop quality, question alignment before coverage, verification
  booleans, fewer missing gaps). Unsupported-claim regression remains a hard constraint checked
  before any score comparison.
- Consolidated TaskRunner's duplicated project tool-event branches:
  project facts recording, checkpoint edit/run tracking, and AnalysisRun projection now share one
  `_handle_project_tool_event()` seam with unchanged branch conditions, and projection failures
  fail open without touching task completion.
- Architecture tests now forbid `codey.storage.managed_outputs` imports from research/review/ghost modules
  and keep the new projection modules pure (no events/tool_runtime/task_runner/server dependencies).
- v1 scope note: Research reports do not yet cite `analysis_run:<id>`; the internal support
  relation is recorded first. Making the citation model-visible would change the report contract
  and requires a small live A/B in a later version.

## 0.4.4 - Bounded Research Planner v1

- Implemented memory Staging isolation (`StagedKnowledgeStore` / `StagedKnowledgeChanges`):
  knowledge writes and note links during follow-up are buffered in-memory with full read-through capability and compensating rollback;
  rejected candidates incur 0 writes to disk store or changes, guaranteeing zero pollution for store, `sources_read`, and `created_ids`;
  `link()` validates endpoint note existence; changes are committed only upon candidate selection.
- Hardened staged note-link semantics and rollback:
  staged links now resolve note titles through the same narrow store resolver used by normal `KnowledgeStore.link()`,
  staged commit snapshots and restores the SQLite link edges touching staged/link endpoint notes on failure,
  `replace_links_touching()` now filters restore rows to links actually touching the requested note ids,
  and staging-only change tracking is a pure no-op facade rather than a half-used state holder.
- Formalized `KnowledgeChanges.snapshot()` / `restore_snapshot()` as the rollback boundary used by staged commits,
  avoiding private-field restoration while preserving the full in-memory change tracking state.
- Tightened evidence-only `knowledge_write` to the minimal argument surface:
  `type`, `title`, `body`, `sources`, and `evidence` are accepted; `sources` must be a non-empty list of URLs,
  `evidence` must be a non-empty list of evidence objects, each item must use explicit `source_url`, and ordinary
  write side channels such as `tags`, `relations`, `aliases`, `status`, or custom ids are rejected in follow-up mode.
- Fixed deterministic merge project metadata preservation:
  merged records rebuild `project_ref` from the active `ResearchTools.project`, matching modern Research records'
  `basename/digest` project identity without a legacy path shim.
- Added Staging Commit Exception Guard and Compensating Rollback in ResearchPipeline:
  safely protects `commit_staged` against disk full or IO exceptions by automatically unlinking newly written note files on disk
  (including cleaning up moved folder paths while restoring original files with byte-exact precision without timestamp drift, unified with `content_hash_bytes`),
  removing newly created notes from the SQLite index (preventing ghost index entries), and restoring `KnowledgeChanges` snapshots,
  cleanly preserving the initial successful result with `planner_stop_reason="followup_commit_error"`.
- Improved pipeline and trace observability (`task_runner.py` / `pipeline.py` / `run_trace.py`):
  surfaced and persisted `fresh_source_count`, `new_evidence_count`, and `final_evidence_count` (total evidence count in final delivered report) as well as fully observable `attempted_fresh_source_count`
  and `attempted_new_evidence_count` regardless of candidate selection, providing complete traffic and cost persistence transparency.

- Strengthened Evidence Follow-up and executor boundaries (`evidence_followup.py` / `plan_executor.py`):
  - fixed schema divergence by strictly requiring explicit `type='fact'` and rejecting missing or invalid types in controller validation;
  - strictly enforced single tool call execution: replies with multiple tool calls are rejected as `invalid_tool_calls_count` rather than silently ignored;
  - enforced strict evidence provenance integrity requiring `evidence[].source_url` to be declared within the note's `sources` list;
  - added pre-check and duplicate redirect prevention for `canonical_final` URLs in `PlanExecutor`, avoiding redundant fetches and budget exhaustion;
  - replaced private helper cross-module import with public `source_from_opened` (exported in `__all__`) and removed dead code in `done_finalizer`.

- Enhanced deterministic graph patch merger (`codey/research/record_merge.py`):
  enforces strict evidence-backed citation verification across all sections (conclusion, evidence, counter), filtering uncited or dangling citations (e.g. `[99]`),
  idempotently merges new evidence and sources based on `(canonical_url, excerpt_hash)`, re-indexes citations with `done_finalizer`,
  reuses the shared report-quality citation parser instead of a merge-local Markdown regex, and fully synchronizes `queries`, `search_results` with full `query/opened/final_url` shape, `notes_created`, `notes_updated`, `links_created`, `counterpoints`, and stably sorted `source_urls`.
- Thoroughly removed dead `max_wall_time` branches and unused timer arguments from `PlanExecutor`.
- `PlanExecutor` now stops before issuing another search once the total fresh-source budget is already exhausted, and deterministic merge no longer increments `ResearchRunResult.turns` for non-model report assembly.
- Moved Research lifecycle orchestration into `codey/research/pipeline.py`.
  Initial `ResearchRunner`, proof review, `QueryPlanner`, bounded
  `PlanExecutor`, evidence-only follow-up, deterministic `merge_evidence_patch`, final proof review, and Evidence Ledger
  persistence now have one owner. `TaskRunner` keeps the outer
  provider/session/trace/mode lifecycle.
- Implemented true Evidence-Only Follow-up mode (`codey/research/evidence_followup.py`):
  follow-up is strictly bounded to a single model turn, program-level allowlist permits ONLY `knowledge_write`,
  forbids `done/web_search/open_url/knowledge_link`, and enforces URL whitelisting against `fresh_source_urls` while rejecting internal `s1/s2` labels.
- Implemented deterministic record patch applier (`codey/research/record_merge.py`):
  discards unsupported new claims, merges new sources and evidence idempotently based on `(canonical_url, excerpt_hash)`,
  re-indexes citations with `done_finalizer`, and deterministically generates the final ResearchRecord and report.
- `PlanExecutor` enforces fresh-material semantics: collects baseline URLs (read, opened, cited evidence),
  skips duplicate URLs, and returns `stop_reason="no_new_material"` when no fresh URL is opened.
- Added `ResearchIterationRun` as the explicit single-iteration boundary.
  Runtime `ResearchTools` now travels only across that boundary and is no
  longer hidden on `ResearchRunResult.runtime_tools`.
- Removed the `_run_research_task` and `close_search` legacy seams. Tests and
  manual harnesses patch `_run_research_iteration`, the current cold-start
  seam, instead of preserving unused compatibility for test scripts.
- Kept one bounded follow-up round with sequential search/open/fetch, the
  existing tool contract, URL guard, UI/SSE payload, and single final-record
  write behavior unchanged.
- Added architecture boundary coverage ensuring ResearchPipeline does not
  depend on TaskRunner or Server and final Research results do not carry
  runtime tool objects.
- Pipeline follow-up observability now flows through `task_done` and Run Trace:
  `followup_applied`, `followup_rounds`, and `planner_stop_reason` are visible
  above `ResearchRunResult`. Follow-up execution failures keep the successful
  initial result instead of letting the enhancement path fail the whole
  research task.
- Follow-up eligibility now includes actionable `max_turns` / `no_progress`
  initial runs when proof review still exposes a planner-addressable gap.
- Added `tests/manual/bounded_research_planner_ab.py`, a live bounded-planner
  A/B harness with atomic send/reply trace writes. Its planner arm disables the
  wall-clock limiter, treats time as diagnostic cost, and records paired
  baseline/planner deltas for coverage, unsupported-claim rate, material gain,
  provider traffic, and elapsed time.
- Tightened `followup_usefulness`: failed rows are not evaluated as pairs, and
  `useful=true` requires a completed pair, a follow-up round, final-record
  material gain, quality-side improvement, and no quality regression. Pipeline
  diagnostics now distinguish missing proof review from no actionable gap.
- Added pre-integration fresh-material and hidden-material probes. The experimental
  executor skips already-opened URLs, separates `execution_material_gain` from
  final-record `material_gain`, and verifies that patch-only merge is safer
  than asking the model to rewrite the whole follow-up report.
- Added a pre-integration evidence-only follow-up probe to validate the bounded
  planner shape before production merge. Planner follow-up was limited to one
  `knowledge_write` turn and deterministic patching, which became the
  production evidence follow-up plus `record_merge` design.
- Fixed Qwen Studio homepage first-submit readiness. Qwen can expose
  `textarea.message-input-textarea` and `button.send-button` before its
  homepage submit handler is hydrated; immediate submit can clear the composer
  without creating a chat. `new_chat()` now waits out that homepage false-ready
  state with the same timeout budget. Live Qwen submit probe and
  `new_chat(timeout=60)` both pass after the fix.
- Live hidden-material paired A/B on 2026-08-20 now shows useful planner uplift
  on MiMo, DeepSeek, and Qwen: each moved `widget_noop` from score `5` to `6`,
  added one evidence-backed source, improved coverage by `+0.111`, and reduced
  unsupported-claim rate. GLM improved raw score from `1` to `6` but regressed
  unsupported-claim rate, so it is not counted useful by the conservative
  gate. StepFun stayed `1 -> 1` because the initial run stopped at protocol
  before follow-up could execute.
- The pre-integration evidence-only patch-merge A/B showed useful planner uplift
  on all five tested web providers. DeepSeek,
  MiMo, and Qwen moved `widget_noop` from score `5` to `6`; StepFun and GLM
  moved from `1` to `6`. Every planner row added one fresh source/evidence pair
  with no unsupported-claim-rate regression. StepFun avoided the long
  `done.answer` JSON failure because the follow-up model never writes the final
  report.
- The bounded planner manual A/B harness now exercises the production
  `run_evidence_followup()` and production deterministic merge path directly;
  the only remaining A/B-specific execution patch is the fixture material-phase
  executor used to expose hidden source B. The old harness-only follow-up
  controller and patch-only merge path were removed.
- Replayed the five successful evidence-only3 follow-up replies against the
  current production `run_evidence_followup()`: DeepSeek, MiMo, Qwen, StepFun,
  and GLM all accepted the strict explicit `{"tool":"knowledge_write","args":{...}}`
  schema and wrote exactly one new evidence item.
- Post-production bounded planner A/B now has paired DeepSeek, Qwen, and
  StepFun rows on `widget_noop`. DeepSeek and Qwen both reached
  `ab_followup_mode=production_evidence_followup`.
  DeepSeek moved score `5 -> 6`, added one fresh source/evidence pair, improved
  coverage `0.556 -> 0.667`, and stayed `useful=true` with one extra provider
  send. Qwen also moved score `5 -> 6` and added one fresh source/evidence pair,
  but unsupported-claim rate regressed `0.333 -> 0.750`, so the conservative
  usefulness gate keeps that production-path row `useful=false`. StepFun fetched
  the hidden fresh source but stayed protocol/not-answered, selected no
  candidate, and remained score `1 -> 1` / `useful=false`.
- Added `tests/manual/bounded_research_merge_projection.py`, an offline-only
  diagnostic that applies saved bounded-planner A/B JSON and trace files to a
  narrow evidence-backed merge projection. The projection kept the five
  evidence-only3 rows useful and converted Qwen plus the earlier StepFun
  production rows. One StepFun live rerun under provider-rate-limit pressure is
  treated as an invalid gate sample; a later clean paired StepFun rerun reached
  fresh evidence extraction and stayed raw `1/false` only because the candidate
  was not selected. The projection converted that row to `6/true`, validating
  the production `record_merge.py` narrow rebuild.
- Strengthened `record_merge.py` for narrow evidence-backed candidates: report
  quality review now receives search-result URLs from `search_results_payload`
  instead of a nonexistent ledger helper, protocol/not_answered initial reports
  are rebuilt from staged ledger evidence, and source-quality / coverage
  sections are regenerated deterministically instead of inherited from the old
  model-written report.
- Recorded a post-fix Qwen production paired A/B on `widget_noop`: the narrow
  rebuild kept score `5 -> 6`, changed usefulness to `true`, added one fresh
  source/evidence pair, and improved unsupported-claim rate from `0.333` to
  `0.250` instead of regressing to the older `0.750` row.
- Production hardening & code hygiene cleanup for bounded evidence-only follow-up & deterministic merge:
  1. Completely removed `max_wall_time` production gate and stop branch; bounded semantics are enforced solely via queries/sources/rounds/cancellation, treating wall time strictly as a diagnostic metric.
  2. Strengthened `evidence_followup.py`: updated prompt note type to `fact`, strictly enforced explicit non-empty evidence lists with explicit `source_url`, tightened URL whitelisting against internal `s1/s2` IDs, invalidated entire round upon forbidden tool calls (`invalid_tool_called`), and restricted execution to exactly 1 valid `knowledge_write`.
  3. Introduced staged ledger isolation (`ledger.clone()`) during follow-up iterations: rejected candidate follow-up artifacts cause zero pollution to the primary knowledge store and ledger.
  4. Strengthened `record_merge.py`: aligned idempotent deduplication key on `(canonical_url, excerpt_digest)`, supported non-standard/protocol-stop initial summary recovery and formatting, synchronized all `ResearchRunResult` metadata fields (`source_urls`, `opened_sources`, `sources_read`, `evidence_items`, `citation_map`, `coverage`), and generated a deterministic `synthesis:merge:{digest}` synthesis ID.
  5. Fixed `PlanExecutor` field reference to `tools.ledger.evidence_items`, removed dead `_followup_context` helper, and expanded `ResearchPipelineResult` trace observability metrics (`fresh_source_count`, `new_evidence_count`, `final_evidence_count`).



## 0.4.3 - Source Connector Boundary + Query Planner Dry Run v1

- Added `codey/research/source_connectors.py`, a pure source connector boundary
  with `SourceConnectorSpec`, `SourceConnectorRegistry`, `SourceHit`,
  `FetchedSource`, and `SourceConnectorResult`. The built-in registry ships
  fixture/local coverage for `local_file`, `csv_tsv`, `json_file`, `arxiv`, and
  `pubmed`; `openalex` is explicitly deferred and `rss` is optional, so neither
  counts as a shipped connector.
- Added recorded connector fixtures under `tests/fixtures/research_connectors/`
  for local text, CSV, TSV, JSON, arXiv Atom, and PubMed XML. Fixture parsing
  produces stable `source_ref`, `connector_source`, and `source_hit` refs.
  Local file reads are confined to explicit allowed roots, CSV/TSV parsing uses
  Python's `csv` module, and URL-backed recorded hits still pass the Research
  URL guard before fixture fetch.
- Added `codey/research/query_planner.py`, a deterministic ResearchPlan
  dry-run. It consumes proof-review gaps plus connector registry metadata and
  returns bounded query candidates, source preferences, max bounds, reason
  codes, warnings, and a stable `research_plan:<16 hex>` ref. Medical and
  life-science questions prefer PubMed; paper/preprint/ML-style questions
  prefer arXiv; local table/file/JSON questions prefer local connectors.
- Run Trace now records bounded `research_plans` summaries after proof review:
  plan ref, question digest, proof ref, query count, source preference ids,
  max bounds, warnings, and reason codes. It does not store query text, raw
  prompts, source bodies, fetched pages, raw URLs, or raw absolute paths.
- The Capability Registry and Event / Capability Matrix now declare
  `research_source_connectors`, `research_connector_search`, and
  `research_query_planner`. Architecture tests keep connector/planner modules
  away from provider adapters, browser code, tool runtime, server/TaskRunner
  runtime layers, Ghost runtime, subprocess, and plugin loaders.
- Enabled the 0.4.3b connector-aware search/fetch path for PubMed and arXiv
  through the existing Research runtime tools. The controller now exposes
  distinct model-visible open actions, `open_result`, `reopen_source`, and
  `open_hit`, instead of overloaded `open_url(result_id/source_id/hit_id)`
  shapes. Those actions compile to the runtime open/fetch path; connector hits
  are appended as ordinary search results and only become citable after the
  connector boundary fetches them into the opened-source ledger.
- Hardened the connector boundary before enabling it: PubMed recorded fetches
  only accept `pubmed.ncbi.nlm.nih.gov`, arXiv recorded fetches only accept
  `arxiv.org`, arXiv fixture URLs are canonicalized to `https://arxiv.org/...`,
  fixture parsers and recorded fetches reject malformed PubMed/arXiv IDs,
  `SourceHit` audit metadata refs filter secret-looking values, `SourceHit`
  and `FetchedSource` scalar audit fields are allow-listed, connector catalog
  ids/kinds reject secret-looking or non-canonical codes, catalog hints plus
  connector result warning/error codes filter secret-looking values, malformed
  fixture limits fall back to bounded defaults, and CSV/TSV truncation now reads
  one row past the display limit before marking a file truncated.
- Hardened `RunTrace.record_research_plan()` and planner trace payloads so
  trace sinks accept only connector-id-shaped source preferences, ignore
  non-collection list fields instead of iterating strings or raising on `None`,
  and filter secret-looking reason or warning codes. Adjacent evidence-ledger
  and proof-review trace sinks now use the same trace-safe reason/warning
  rules without dropping safe audit codes such as `token_budget_exceeded` or
  `authorization_required`. Proof-ok/no-gap reviews now produce a no-op
  `proof_ok_no_required_followup` plan with no query candidates. Run Trace now
  also separates the model-visible controller action contract hash from the
  compiled runtime tool contract hash, keeps proof-ok no-op warnings empty, and
  records bounded connector fallback error summaries without raw request data.
- Browser-backed Research search now explicitly reuses a dedicated Research
  browser profile/port for ordinary Research runs, with custom CDP port
  families scoped away from the default model-provider port pool.
  `BrowserSearchProvider()` itself keeps an isolated default for direct
  construction, and browser attach/port waits remain bounded at 20 seconds for
  faster failure feedback. Cancellation now bypasses isolated CDP launch and
  search-page navigation retries, and the manual connector A/B harness uses the
  same non-isolated Research browser reuse path as production Research.
- Connector-aware live search now builds PubMed/arXiv API queries from the same
  safe term boundary used by the dry-run planner. That boundary masks
  high-confidence secret marker/value windows such as `api key ...`,
  `password ...`,
  `api key is ...`, `password is equal to ...`, `password is set to ...`,
  `api key called ...`, `api key named ...`, `client secret known as ...`,
  `password is configured as ...`, over-padded or punctuation-separated
  connector phrases such as `password is configured as known as called ...` and
  `password - is - configured - as - known - as - called - ...`, Chinese
  windows such as `密码 是 ...` and `密钥等于 ...`, `private key is ...`,
  `client_secret=...`, `access_token ...`, `passphrase ...`, value-shaped
  contextual markers such as `token abcdef`, `cookie abcdef`, `jwt abcdef`,
  and `Authorization: Bearer ...` across planner previews, connector digests,
  and live PubMed/arXiv requests. Multi-word values after
  explicit secret markers such as `api key one two three ...` and
  `password correct horse battery staple ...` are masked until a bounded
  domain-term boundary. Cleaned domain terms such as `clinical` or `cancer` can
  still drive connectors, while URL/path spans are removed and connector lookup
  is skipped when no safe terms remain. Live connector routing and request
  assembly reuse one `SafeConnectorQuery` instead of recomputing from raw text.
  Browser search starts before connector lookup, connector
  requests use a short bounded budget, ordinary browser results keep
  query-string-distinct URLs, and direct PubMed/arXiv URL fetches fall back to
  browser fetch when connector lookup fails. Connector result digests also use
  the shared safe query,
  live connector transport metadata uses a neutral tool name and User-Agent
  without the product name, and the Research JSON codec no longer accepts
  legacy tool or argument aliases such as `open`/`fetch`, `queries`, or
  `done.summary`; the fallback contract no longer carries an alias layer and
  requires exactly one plain JSON object with only top-level `tool` plus
  `args`, rather than `name`, top-level argument fields, extra top-level fields,
  extra JSON objects, arrays, fenced blocks, or prose wrappers.
  Providers that still emit legacy names may take one repair turn, but provider
  quirks stay in provider adapters or repair prompts instead of becoming shared
  parser compatibility.
- Shared domain routing now drives both the dry-run planner and live connector
  search, including genetic/genomic and common RAG/NLP/retrieval/benchmark
  terms. The registry's availability, shipped, and capability flags are
  authoritative for live search/fetch; remaining connector budget is never
  rounded above the actual deadline, safe scientific slash terms such as
  `JAK/STAT` remain searchable, and path-like slash tokens such as
  `docs/ADR2026/research`, `Docs/ADR/Plan`, and `ProjectX/ConfigV2` are dropped
  before source API requests. Secret redaction predicates now split independent
  marker words from key-shaped values, so `secreted` and `secretion` stay valid
  Research terms. Shared Research shape helpers now cover connector IDs,
  generated refs, digest refs, and bounded connector limits.
- Added `codey/research/done_finalizer.py`, a narrow deterministic citation
  compiler that runs before the Research report-quality gate. It compiles only
  reliable source-id/contextual refs and parsed numeric source rows, renders the
  final source table from opened sources with saved evidence excerpts, removes
  opened-only sources, and leaves missing support to the quality gate instead
  of adding citations. Numeric and source-id refs use separate bindings so
  mixed `[s1]` and numeric source rows are not rebound through stale tables;
  duplicate old source numbers that point to different URLs are rejected, while
  unambiguous single-source drift can still be normalized. Source-id leakage
  checks now cover pre-heading prose plus report body text, scan the `Sources`
  section line by line, allow real source titles such as `Analysis of [S1]`,
  and reject separate notes or contextual leaks such as `source_id=s9`. Reports
  with no citable source are re-rendered into the required sections before
  quality review, and Run Trace records only a bounded compilation summary.
- Added a shared `codey/citation_scanner.py` helper so the done compiler,
  report-quality gate, and Writer handoff use the same citation and source-id
  scan rules instead of drifting apart. The report-quality gate is now split into
  small review helpers for missing sections, source-id leaks, no-citable
  reports, provenance, source-table validation, body citation checks, and
  source-quality warnings.
- Qwen now waits for an interactive, non-generating composer before filling it,
  confirms that the controlled input keeps the complete message and enables
  send, and refuses to click if hydration has already cleared the draft. This
  removes the fixed pre-send composer settle window while preserving the
  one-shot submission boundary. Browser PDF requests also use a neutral
  User-Agent.
- Live connector smoke/A-B was run one provider at a time with atomic per-row
  result files. DeepSeek showed a clear PubMed source-targeting improvement,
  MiMo and StepFun connector arms reached PubMed target hosts, Qwen improved on
  arXiv after the provenance fix, and DeepSeek/Qwen/MiMo/StepFun/GLM all reached
  arXiv target hosts in at least one recorded arm. Several runs still stopped at
  `max_turns` or protocol repair, and GLM PubMed was left inconclusive after
  repeated attempts hit provider rate limits, so this is source-selection smoke
  evidence rather than a proof-quality win claim.

## 0.4.2 - Research Proof Quality Gate + Planner Signals v0

- Added `codey/research/proof_quality.py`, a deterministic proof reviewer for
  `ResearchRecord` plus the durable Evidence Ledger read model. It checks
  answer coverage, citation presence, opened-source evidence, locator/source
  consistency, support relations, assumptions, counter/limitation handling, and
  source-trust warnings without model calls or raw source reads.
- Added `codey/research/completion_gate.py`, a narrow Research queue completion
  boundary. Queued `research` and `open_question` work items now complete only
  when the proof review passes and emits a generated
  `research_proof:<16 hex>` ref. Ordinary manual Research still finishes and
  records proof metadata without being blocked by this queue gate.
- `ghost/work_queue.py` remains Research-runtime-free. It only validates that
  research/open-question completion includes a generated-looking
  `research_proof:<16 hex>` primary proof; TaskRunner calls the Research
  completion gate for the actual proof decision.
- Run Trace now stores a bounded `research_proof_reviews` summary: proof ref,
  queued-question digest, booleans, answer coverage score, counts, reason
  codes, and record id/digest when a record exists. Missing-record gate blocks
  still leave an auditable proof review without storing the queued question
  text. Passing proof reviews must include valid record id/digest, and duplicate
  proof summaries are de-duplicated by proof/question/reason identity. The trace
  does not store planner signal text, raw prompts, raw model responses, raw
  URLs, raw paths, source text, or fetched pages.
- Queued Research proof review is recomputed against the queued item title, so
  strict-continuation wrapper text cannot dilute answer coverage. The gate also
  ignores stale precomputed reviews by recomputing against the current
  `ResearchRecord` and durable ledger state.
- `research_proof:<16 hex>` refs now bind the question digest as well as the
  record id/digest and proof result, so later audit/planner code can distinguish
  which queued question a proof reviewed without storing the question text.
- Proof semantics are fail-closed: a required conclusion/key-evidence claim
  only counts as supported when it is `evidence_backed`, has its own
  `evidence_refs`, and a `supports` relation targets one of those refs.
  Counterevidence or limitations handling is now required for `ok=True`.
- The Capability Registry and Event / Capability Matrix now declare
  `research_proof_quality` as a deterministic completion gate and planner
  signal producer. Architecture tests keep proof modules away from provider
  adapters, browser code, tool runtime, server/TaskRunner runtime layers,
  Ghost runtime, and plugin loaders.
- This release does not change Research prompts, tool schemas, model-visible
  tool results, Router behavior, provider fallback ordering, permissions, UI,
  task receipts, or SSE payload shape. Small Research/Ghost queue A/B passed on
  DeepSeek, Qwen, MiMo, StepFun, and GLM, one provider at a time against Edge
  CDP 9222; this was not a broad provider/prompt A/B.

## 0.4.1 - Evidence Ledger v2

- Added `codey/research/identity.py`, a shared bounded identity helper for
  Research projections. URL refs now reuse one redaction/digest path across
  Research object and ledger code, including malformed/no-host URL inputs and
  query keys/values. Project and path refs continue to store basename plus
  digest rather than raw absolute paths.
- Added `codey/research/evidence_ledger.py`, a durable local read model that
  appends completed `ResearchRecord` objects into a bounded
  `research/evidence_ledgers/<session>/<project>.json` ledger. It records
  source/evidence/claim/assumption/relation ids, locator refs, counts, schema
  version, and content-addressed refs for later proof checks.
- Evidence ledger writes are fail-open: missing/invalid records, bad ledger
  JSON, oversized ledger files, and write failures do not break the Research
  run. Run Trace records only a bounded evidence-ledger write summary.
- Ledger trimming now preserves graph closure. When source/evidence/claim/
  assumption/relation maps reach their caps, Codey keeps the newest complete
  records and prunes older records instead of retaining records with dangling
  refs. Loaded ledgers are graph-validated before becoming available. Closure
  includes nested claim evidence/assumption refs, assumption claim refs,
  evidence source refs, evidence locator source refs, and relation endpoints.
  Load-time allow-list validation rejects unknown raw fields, orphan map
  entries, map key / entry id mismatches, non-canonical scalar values, and
  locator source ids that disagree with their evidence source ids. Loaded
  record counts must match retained refs, and source `content_hash` values are
  kept only when they are canonical hashes; fake `sha256:` content-hash values
  are cleared rather than rehashed.
- `EvidenceLedgerStore.append_record()` accepts typed `ResearchRecord` objects
  only; mapping fallbacks are rejected before nested refs can persist raw URLs,
  paths, or source-body-like fields. Digest refs only preserve real
  `sha256:<64 hex>` strings; pseudo-digest strings are rehashed.
- If a malformed typed record is pruned for ledger closure, `append_record()`
  now returns `skipped=True` with `record_pruned_for_ledger_closure` instead of
  reporting a successful write. Candidate writes are isolated until the new
  record survives trimming and the candidate payload passes full canonical
  validation, so a malformed replacement cannot delete an existing good record
  or poison the next load. When no new payload is written, the result preserves
  previously loaded ledger counts. Typed records whose `to_jsonable()` fails,
  including malformed nested objects, return `invalid_record` without raising
  from the store.
- `TaskRunner`, server state, and the headless JSONL runner now carry the
  optional `EvidenceLedgerStore`. The user-facing Research payload, task
  receipt shape, UI, and SSE events remain unchanged.
- Added the `research_evidence_ledger` capability and the
  `research.evidence_ledger` Event / Capability Matrix row. Architecture tests
  keep the identity and ledger modules away from provider adapters, browser
  code, tool runtime, TaskRunner/server orchestration, Ghost runtime, and
  plugin loaders.
- This release does not change Research prompts, tool schemas,
  model-visible tool output, Router behavior, provider fallback ordering,
  permissions, UI, task receipts, or SSE payload shape. No production live
  provider A/B is required for this persistence-only release.

## 0.4.0 - Evidence Kernel / Research Object Model v1

- Added `codey/research/object_model.py`, a deterministic projection that turns
  each Research run's ledger and final report review into a bounded
  `ResearchRecord` with question, source, evidence, claim, assumption, and
  relation objects.
- Research results now carry `research_record` internally, while TaskRunner
  keeps the UI/SSE `research` payload shape unchanged. Run Trace stores only a
  bounded record summary: id, answer status, counts, unsupported-claim count,
  and digest.
- Claim evidence binding is conservative in v1. A citation to a source does
  not attach every evidence snippet from that source to every claim; Codey only
  creates `supports` relations when the final claim matches the evidence claim
  or exact bounded excerpt and the evidence stance is appropriate for the
  report section. Claim `status` is limited to `evidence_backed`,
  `unsupported`, or `assumption`; support/refutation/limits are expressed by
  relation kind. Contradicting evidence and non-empty unknown stance values
  cannot support conclusion or key-evidence claims.
- Search results, Ghost/local memory, and unopened sources are not evidence.
  Evidence must come from sources Codey opened during the Research run.
- Research object URL refs redact userinfo and secret query-key variants such
  as `client_secret`, `refresh_token`, `x-api-key`, `jwt`, and token/api-key
  suffixes before digesting. The query component is redacted before URL
  digesting, including query keys, malformed/no-host URL inputs, and malformed
  userinfo heads. Local paths are stored as basename plus digest refs, not raw
  absolute paths.
- Added the `research_object_model` capability and the
  `research.object_model` row in the Event / Capability Matrix. Architecture
  tests keep the object model away from server, TaskRunner orchestration,
  provider adapters, browser code, tool runtime, Ghost runtime, plugin loaders,
  and file writes.
- This release does not change Research prompts, tool schemas,
  model-visible tool output, Router behavior, provider fallback ordering,
  permissions, UI, task receipts, or SSE payload shape.

## 0.3.20 - Run Details v1

- Added `codey/run_details.py`, a bounded read-only projection that turns
  RunLedger and RunTrace metadata into short user-facing run explanations. It
  reports work type, model, context used, local actions, safety decisions,
  model fallback, and verification without returning raw prompts, raw tool
  output, source bodies, webpage bodies, or provider error dumps.
- Added `GET /api/run_details?session_id=...&run_id=...`, a quiet read-only
  endpoint that returns `available=false` when a run has no local details
  rather than raising or writing state.
- Run Details reads trace manifests through the bounded local JSON reader with
  `MAX_TRACE_BYTES` and validates the Run Trace schema version and kind before
  using trace metadata.
- Added `codey/web/assets/run_details.js` and a small inline `Details` action
  on terminal task rows. Details are lazy-loaded, expanded in place under the
  existing receipt/status row, cached only in memory, and never persisted into
  chat state.
- Added the `run_details` capability and the `run_details.summary` row in the
  Event / Capability Matrix. Architecture tests keep the projection away from
  runtime dispatch, provider adapters, TaskRunner orchestration, browser code,
  plugin loaders, raw trace viewers, and new SSE event shapes.
- Updated the UI design baseline to define Run Details as an inline receipt
  expansion: monochrome, no drawer, no background card, no rounded container,
  no colored warning styling, no topbar entry, and no internal terms such as
  RunTrace, PromptEnvelope, Policy Pipeline, Router, Ghost, or Provider.
- This release does not change prompt text, tool schema, model-visible tool
  output, Router behavior, provider fallback ordering, permissions,
  Research/Writer/Review semantics, task receipts, or SSE payload shape.

## 0.3.19 - Built-in Profiles v1

- Added `codey/builtin_profiles.py`, a read-only catalog of Codey's built-in
  default-profile boundaries. It declares fixed internal tendencies for
  `default`, `research_heavy`, `review_strict`, `local_only`, and `beginner`.
- Added `docs/codey_builtin_profiles.md` and `tests/test_builtin_profiles.py`
  to lock stable profile ids, JSON export, fingerprint, capability references,
  permission defaults, provider scopes, fallback posture, local-context default
  enums, UI detail levels, and quiet user-facing copy. The `local_only`
  profile intentionally omits a Research permission default.
- `server.State` now owns the built-in profile registry, and `TaskRunner`
  carries it as metadata only. Profiles do not participate in Router decisions,
  provider fallback, permission selection, prompt assembly, tool dispatch,
  UI, SSE, receipts, or project configuration.
- Added a `builtin_profiles` capability declaration. The Capability Registry
  remains metadata only, and architecture tests reject plugin-loader or runtime
  host shapes in the built-in profile module.
- This release does not add a profile picker, configuration platform, plugin
  system, dynamic imports, prompt patches, provider-choice overrides,
  mode-choice overrides, permission relaxation, or UI changes.

## 0.3.18 - Event / Capability Matrix v1

- Added `docs/codey_event_matrix.md`, a tested architecture matrix for event
  producers, consumers, durable state, model visibility, UI visibility,
  policy requirements, trace requirements, privacy boundaries, and linked
  capabilities.
- Added `tests/test_event_matrix.py` to reject duplicate event ids, unknown
  capabilities or durable states, missing Prompt Envelope / Run Trace coverage
  for model-visible rows, missing policy declarations, and raw-payload privacy
  boundary regressions.
- The matrix explicitly declares the Review recent-log projection rendered from
  `RunEvent` history as model-visible and covered by Prompt Envelope / Run
  Trace, while keeping the UI/SSE `run_event.*` rows scoped to their UI and
  ledger projections.
- Moved the existing Web/SSE `RunEvent` projection into
  `codey.runtime.events.run_event_ui_payload()` and the research display-name mapping
  into `codey.runtime.events.display_tool()`. `TaskRunner` now calls this shared
  projection instead of owning a local duplicate.
- Kept `run_event_payload()` and RunLedger projection separate because they
  serve different consumers. This release does not add an event bus, runtime
  dispatcher, plugin system, Run Details UI, Router behavior, provider fallback
  behavior, prompt behavior, permission behavior, or UI/SSE payload shape
  changes.

## 0.3.17 - Action Policy Pipeline v1

- Added `codey/action_policy.py`, a monotonic local action policy pipeline for
  `allow` / `ask_user` / `deny` decisions. It covers local file actions,
  run-command checks, shell approval, Research URL checks, provider fallback
  audit decisions, Local context action boundaries, and managed-output artifact
  limits.
- The existing run-command allowlist now has one source of truth in the action
  policy module. `tool_runtime` still owns execution and result projection, but
  its sink-level policy check now requires an explicit permission profile and
  records denial as `ToolOutcome.audit["policy_decision"]`.
- Research URL checks keep the existing `check_fetch_url()` API and user-facing
  denial text while reusing the shared action policy URL guard; malformed URL
  ports are denied as policy reasons instead of escaping as parser exceptions.
- Managed-output artifact writes now pass through size/count policy guards.
  They require a writer verification profile; oversized artifacts are not
  retained as handles, while the bounded model result text remains unchanged.
- Unknown action kinds are denied by the policy pipeline instead of falling
  through to default allow.
- The action policy module keeps a narrow `__all__` surface; low-level
  run-command helper functions are internal implementation details.
- Run Trace manifests now include bounded `policy_decisions` entries with
  kind, decision, guard id, reason code, phase, subject ref, and display digest.
  They do not save raw commands, URLs, stdout/stderr, source bodies, or prompt
  text, and mapping fallbacks must use digest-shaped refs.
- Provider fallback policy decisions are traced without changing provider
  selection, fallback ordering, Router behavior, prompt text, tool schema,
  UI/SSE payload shape, or task receipts.
- `policy_guard` capability metadata now declares the
  `action_policy_boundary`, while the Capability Registry remains read-only and
  does not dispatch runtime behavior.

## 0.3.16 - Tool Contract v2

- `ToolOutcome` and `ToolResult` now use `model_text` as the only
  model-visible tool-result text field. The legacy `output` field and
  top-level managed-output metadata fields were removed instead of kept as a
  compatibility layer.
- Tool results now carry separate `presentation`, `audit`, and `canonical`
  projections so UI/SSE/receipts, RunLedger/local audit, and small internal
  facts do not have to parse the model-visible text.
- `presentation`, `audit`, and `canonical` are sanitized into bounded
  JSON-safe mappings at the `ToolOutcome` / `ToolResult` boundary. Unsupported
  values become short marker strings and add projection warnings instead of
  breaking later audit/export serialization.
- Managed-output audit metadata is schema-normalized when consumed: only
  `out_[A-Za-z0-9_.-]{1,80}` handles are accepted, invalid byte counts become
  `0`, only 64-character lowercase hex `sha256` values are retained, and
  malformed audit values cannot crash UI/SSE event rendering.
- Managed output handles now live under `audit["managed_output"]`; the model
  still receives the same bounded footer that says full output was retained
  locally for audit/export, not as a new tool.
- Coding and Research codecs render only `model_text`. Tests pin that
  `presentation`, ordinary `audit`, and `canonical` sentinels do not leak into
  prompts.
- Run events, TaskRunner SSE payloads, and RunLedger projection now consume
  `presentation`/`audit` helpers instead of reading old top-level output
  fields.
- This release does not add a new tool system, plugin system, runtime
  dispatcher, Router behavior, provider fallback behavior, permission behavior,
  UI surface, or tool-schema prompt.

## 0.3.15 - Internal Capability Registry v1

- Added `codey/capabilities.py`, a read-only registry of Codey's built-in
  capability boundaries.
- The registry declares each internal capability's id, provided boundaries,
  consumed boundaries, model-visible status, policy requirement, UI surface,
  durable state, permission profiles, owner module, and whether it can load
  third-party code or override user choices.
- The first built-in map covers provider factory, provider capability hints,
  agent runner, tool runtime, Research runner, Review runner, Local context,
  changes presenter, RunLedger, Run Trace, Prompt Envelope, and policy guard.
- `server.State` now owns the built-in registry, and `TaskRunner` carries it as
  metadata only. It does not participate in provider selection, Router
  decisions, permission profile selection, prompt assembly, tool dispatch, UI,
  SSE, receipts, or fallback behavior.
- Added capability and architecture tests that reject unknown dependencies,
  permission profiles, UI surfaces, durable states, third-party flags,
  user-choice override flags, and any capability-registry plugin-loader shape.
- No live provider A/B is required because this release is metadata and
  architecture constraints only.

## 0.3.14 - Prompt Envelope v1

- Added `codey/prompt_envelope.py`, a small internal prompt envelope and
  fail-open trace sink for model-visible sections.
- Coding, chat, Research, review, consensus, and project-audit model boundaries
  now record prompt section metadata through the shared sink immediately before
  the actual provider send, instead of hand-written `trace_call` helpers.
- Run Trace prompt-section payloads now include bounded `purpose`,
  `model_visible`, and source-ref fallback metadata while still storing only
  digests, character counts, budgets, truncation flags, and refs.
- Research intro assembly now uses a prompt envelope with byte-equivalent
  rendering. Coding keeps its existing prompt shape, including the single
  newline boundary before `User task`.
- Provider-send prompt sections still flush before the actual model call.
  TaskRunner secondary snippets use non-boundary `secondary_input_prepared`
  metadata; non-boundary metadata keeps checkpoint batching.
- Trace-disabled local-context and secondary-input helpers now return early
  instead of scanning sections.
- Chat consensus runs no longer record an unsent `chat_outbound_prompt`, and
  project-audit advisor prompt refs include the advisor id so duplicate advisor
  prompts remain auditable.
- `PromptEnvelope` stays independent of provider control code; cancellation from
  control teaching is propagated by exception name. Run Trace prompt-section
  dedup now includes `purpose`.
- `PromptEnvelope` v1 keeps a minimal API surface: sections are supplied through
  construction and rendered, without an unused mutable builder convenience.
- No UI, SSE, Router, provider fallback, permission, Writer, Review, or
  Research behavior changed. No live provider A/B is required when prompt
  parity tests pass.

## 0.3.13 - Run Trace Manifest v1

- Added `codey/run_trace.py`, a bounded run audit sidecar stored under
  `run_traces/<session>/<run>.json`. It is keyed by `run_id` and complements
  RunLedger without becoming a second execution fact stream.
- Run traces now record mode/provider/permission profile, structured Router
  outcome, prompt-section digests and character counts, model-visible tool
  contract hash, Local context item refs, Research note/source refs, provider
  failures, and provider fallback switches.
- Hybrid runs keep phase-scoped Research and Writer profile/contract entries, and
  secondary model calls for consensus, project audit, and review are traced by
  digest-only inputs.
- Research source refs store hostnames without URL userinfo or ports, and review
  trace reuses the same precomputed impact map that reaches the reviewer prompt.
- High-frequency trace metadata is checkpoint-batched while terminal milestones
  still flush immediately.
- Provider-send and secondary-model prompt digests flush at the model boundary.
- Prompt tracing is digest-only. The sidecar does not store raw prompts, chat
  transcripts, source code bodies, webpage bodies, Research note bodies,
  evidence excerpts, provider raw errors, or full diffs.
- Added context-source metadata rendering and stable coding/Research tool
  contract hash helpers while preserving the exact prompt text sent to models.
- Forgetting a conversation now removes that session's run trace sidecars.
- No live provider A/B is required because 0.3.13 does not change prompts,
  Router decisions, Research/Writer behavior, provider fallback policy, tool
  permissions, UI, SSE events, or task receipts.

## 0.3.12 - Research Notes v2

- Upgraded the Research drawer `Notes` tab from plain note id/excerpt text into
  readable note cards grouped as `Selected note`, `Synthesis`, `Created notes`,
  and `Updated notes`. Empty note sections are skipped; an empty run shows one
  quiet `No notes recorded` state.
- Notes now render a bounded Markdown preview through Codey's existing safe
  renderer, with headings, paragraphs, lists, bold, inline code, code fences,
  and blockquotes. Raw HTML remains escaped, and note bodies are never inserted
  directly as trusted HTML.
- Added quiet source chips below each note. Chips are derived from saved local
  provenance (`note.sources`, `citationMap`, `openedSources`, and `sourceUrls`)
  and only open `http:` / `https:` URLs with `noopener,noreferrer`.
- Long note bodies are clipped at a bounded preview length with a local
  `Show more` / `Show less` toggle. Expanding a note only changes the drawer DOM
  and does not write state.
- Removed the Notes-tab source URL section; sources stay available through the
  `Sources` tab and per-note source chips.
- No live provider A/B is required because 0.3.12 does not change the Research
  prompt, runner, provider behavior, Router, Writer path, or permission model.

## 0.3.11 - Local Context Control Surface v1

- Added `codey/ghost/control_surface.py`, a bounded presenter/action dispatcher
  for the web UI. `GET /api/ghost/summary`, `POST /api/ghost/action`, and
  `GET /api/ghost/export` expose local audit controls without returning full
  chat transcripts, Research bodies, webpage/source snippets, source code,
  prompts, raw provider replies, or raw provider errors in the summary.
- Added a quiet topbar `... -> Local context` audit drawer. It reuses the
  existing Changes/Research right drawer language, is mutually exclusive with
  those drawers, and does not add a persistent sidebar entry, badge, toast, or
  task receipt prompt.
- The drawer is a single grouped view: `Recent focus`, `Pending review`,
  `Active preferences`, `Follow-ups`, and `Health`. User-visible copy does not
  expose internal terms such as Ghost, Memory, Affinity, Hebbian, or Directive.
- Empty Local context renders one quiet empty state instead of multiple empty
  groups, and Settings now has a clear divider from audit content.
- Research Notes no longer reuse diff/code block styling; note text now uses a
  plain Research note text style.
- The composer context row now shows only `Choose folder · Research`; the active
  provider/model remains visible only in the bottom provider picker.
- Supported v1 actions are accept/reject candidate, queue/reject work item,
  enable/disable updates, delete current chat/project data, reset all, and
  export. v1 does not add demote, prompt/provider/router/tool-permission
  controls, or direct free-form memory editing.
- The drawer binds to the loaded session/project scope and closes when the user
  switches chat/project. Backend actions also verify that the target candidate
  or work item belongs to the requested scope before mutating local state.
- Local context loading binds the requested scope before the summary request
  starts, so stale loading/error callbacks cannot leave or update an old drawer.
- Removed the obsolete `ctx-provider` composer-context compatibility path after
  provider/model selection moved fully to the bottom provider picker.
- Fixed Affinity replay idempotency for Hebbian evidence refs by materializing
  generated refs before bounding them, preventing unstable generator-object
  strings from being stored as local association evidence.
- Added `tests/test_ghost_control_surface.py` and expanded server/UI/architecture
  coverage. No live provider A/B is required because 0.3.11 does not change the
  model-visible prompt, Router, Research/Writer paths, provider fallback, or
  permission boundaries.

## 0.3.10 - Affinity Index v1

- Added `codey/ghost/affinity.py`, a bounded local association ledger backed by
  `affinity_events.jsonl` and rebuildable `affinity.json`. Events are the source
  of truth; mutating sync is blocked when the event log is unreadable or over
  the byte cap.
- Affinity nodes/edges can be synced from existing bounded local facts:
  accepted Hebbian memory, Work Queue rows, Research Interest candidates, Router
  audit metadata, provider failure kinds, and task outcome summaries. It does
  not store full chat text, Research bodies, webpage/source snippets, source
  code, prompts, raw provider replies, or raw provider error messages.
- Affinity is not truth, permission, routing authority, or automation. Research
  claims still require evidence/citations; explicit provider/mode/project
  choices still win; shell/tool/file permissions are unchanged.
- Low-risk consumption is enabled only as bounded ordering: Ghost Directive
  reorders already-renderable typed memory nodes, Work Queue strict `continue`
  claim order can get a small affinity boost, and Research Interest priority can
  be boosted without treating concepts as evidence.
- Ghost Directive's model-visible header now keeps the neutral `Local Context`
  label but avoids internal memory system terms.
- Hint consumption fails closed when the event log is unreadable, oversized, or
  missing while a projection exists. Bounded ref hashes preserve replay
  idempotency after display refs are capped, and reinforcement weight is based
  only on newly observed refs.
- `ghost export`, `ghost reset --yes`, `ghost delete-scope`, server
  `forget_conversation()`, and Cognitive Sleep maintenance now cover Affinity.
  `ghost disable` prevents automatic sync and hint consumption, but export,
  reset, and delete-scope still work.
- Added `tests/test_ghost_affinity.py`, `tests/test_task_runner_affinity.py`,
  architecture boundary coverage, `tests/manual/ghost_affinity_ab.py`, and
  `tests/manual/ghost_affinity_quality_ab.py` for same-metric ordering uplift
  checks.

## 0.3.9 - Research Interest Queue v1

- Added `codey/knowledge/research_interest.py`, a bounded research-interest
  candidate builder. It turns Research note structured `open_questions` and
  structured Concept Graph missing links into candidates for the existing Ghost
  Work Queue.
- Research synthesis / decision notes now have a typed `open_questions`
  frontmatter field cached in the rebuildable SQLite index. Research Interest
  harvesting reads that field only; it does not parse Markdown section headings.
- Concept missing links are now available as structured data instead of being
  parsed from UI excerpt text. The UI still renders text; queue harvesting uses
  `MissingConceptLink` fields such as related concepts, shared neighbors, and
  support note refs.
- 0.3.9 does not create a second Research queue. Candidates map into existing
  `GhostWorkItem` rows: strong supported concept gaps or structured Research
  note questions can become queued Research items; weak concept gaps remain
  candidate open questions.
- TaskRunner post-turn Work Queue sync now harvests deterministic research
  candidates from the local knowledge store. It does not change Router,
  Research prompts, Directive/Continuity prompt text, UI, permissions, or
  provider behavior.
- Research-interest items still require `research:*` proof to be marked done.
  Concept refs explain why a question is worth checking; they do not prove the
  answer.
- Added `tests/test_research_interest_queue.py` and
  `tests/manual/ghost_research_interest_queue_production_ab.py`.

## 0.3.8 - Ghost Work Queue v1

- Added `codey/ghost/work_queue.py`, a bounded local work-item state machine
  inspired by Symphony's claim/running/done/blocked flow. It stores
  `work_events.jsonl` as the audit source of truth and `work_items.json` as a
  rebuildable projection.
- Work items can be synced from existing bounded facts: continuity open
  questions, structured Research note `open_questions`, interrupted work
  checkpoints, run ledger failure projections, and review follow-ups. It does
  not read full chat transcripts, source files, webpage bodies, raw Research
  bodies, or prompts.
- Automatic consumption is deliberately narrow. Only `intent=auto` plus a
  strict continuation request such as `continue`, `next item`, `继续`, or
  `下一个` can claim one queued item. Non-continuation requests keep going
  through the normal Router/baseline path.
- Claimed items map to existing execution modes: research/open-question items
  run Research, coding/project follow-ups run Project Writer, and review items
  run review-only. The queue cannot grant permissions, approve shell commands,
  choose tool arguments, or execute by itself.
- Completion requires local proof refs from the task event, run ledger,
  receipt, diff, Research report, or review result. Missing proof blocks the
  item instead of marking it done.
- Existing Ghost controls now cover work queue state. `ghost export` includes
  work items/events, `ghost reset --yes` removes them, and
  `ghost delete-scope` filters matching queue rows. CLI also has thin
  inspection/control commands: `ghost work-list`, `ghost work-queue`, and
  `ghost work-reject`.
- Cognitive Sleep now includes work queue projection/event health and compacts
  work queue events only when limits require it. Sleep still does not execute
  work, call providers, change prompts, emit UI events, or generate new tasks.
- Added `tests/test_ghost_work_queue.py`,
  `tests/test_task_runner_work_queue.py`, `tests/test_ghost_work_queue_ab.py`,
  and `tests/manual/ghost_work_queue_production_ab.py`.

## 0.3.7 - Ghost Router v1

- Added `codey/ghost/router.py`, a bounded automatic routing layer for
  `intent=auto`. Before `task_start`, Codey can ask a fresh provider tab to
  choose one execution mode: `chat`, `planning_readonly`, `research`, `project`,
  `hybrid`, or `review`.
- The router is not a permission system. Manual intents still win, shell/tool
  approval is unchanged, and the router cannot choose tool arguments, grant
  capabilities, or let Research/Writer cross permission boundaries.
- Local safety rails reject malformed routing replies with multiple, prose-
  wrapped, or array-wrapped JSON objects, and block project-reading/writing
  modes when the user explicitly asks for chat without project file access.
- Router decisions are consumed by the production `TaskRunner`. `task_start`,
  run ledger mode, provider ranking, and mode dispatch now reflect the final
  routed mode.
- Added review-only mode for explicit diff review. It collects the current diff,
  calls the reviewer, does not start Writer, does not repair, does not edit
  files, and does not connect the main chat provider.
- Added bounded router audit files:
  `state_home/ghost/router_events.jsonl` and
  `state_home/ghost/router_state.json`. Audit stores only route metadata,
  hashes, mode choices, confidence, local reason codes, and bounded diagnostics;
  it does not store full user tasks, raw router prompts, raw replies, or model
  reason text.
- Fail-open boundaries were tightened: cancellation stops the task, provider
  parse/timeout failures fall back to the existing baseline route, and event
  audit failure prevents a route from changing behavior. Projection/compaction
  failure after a successful event append only adds warnings. Any path that
  rewrites router events uses `router_events.jsonl` as the source of truth; if
  events are missing, the projection is bootstrapped before new audit is added.
- CLI/headless support: `python -m codey agent --json --auto` opts headless
  runs into auto routing. Existing `ghost export`, `ghost reset --yes`, and
  `ghost delete-scope` now cover router audit files.
- Added `tests/test_ghost_router.py`, `tests/test_task_runner_router.py`,
  router A/B fixtures, and both router-only and production-spine manual A/B
  harnesses.

## 0.3.6 - Cognitive Sleep v1

- Added `codey/ghost/sleep.py`, a short-lived local Ghost maintenance cycle
  that runs after successful tasks. It checks projection/event health, applies
  Hebbian decay only when due, refreshes continuity from existing bounded local
  sources, compacts Ghost event logs when limits require it, and writes a
  bounded sleep report.
- Cognitive Sleep is not a background agent and does not call providers, browse
  the web, run shell commands, generate new memory candidates, create
  prompt-visible free text, or change the `Local Context` format. It is
  invisible in the UI and emits no SSE event.
- Added `state_home/ghost/sleep_state.json` and
  `state_home/ghost/sleep_events.jsonl`. Reports store only cycle metadata,
  step names, counts, warnings, timings, cancellation state, and run/session/
  project references; they do not store user tasks, assistant replies, prompts,
  Research bodies, webpage text, source snippets, or source code.
- Sleep is single-flight and cancellable between steps. New user work keeps the
  main task slot first; sleep fails open and never blocks task completion.
- Existing Ghost privacy controls now cover sleep files: `ghost export` includes
  sleep state/events, `ghost reset --yes` removes them, and `ghost delete-scope`
  filters matching session/project/user report references.
- Hebbian decay now supports a minimum maintenance interval and skips projection
  and audit writes when no weight/status change is due, avoiding per-turn event
  noise.
- Added `tests/test_ghost_sleep.py` plus server, CLI, UI, architecture, and
  Hebbian coverage. No live web A/B is required for this release because it
  does not change model-visible prompts, provider adapters, or UI behavior.

## 0.3.5 - Ghost Continuity v1

- Added `codey/ghost/continuity.py`, a bounded local continuity projection built
  from existing audited facts rather than full chat transcripts. It can project
  recent focus, open questions, active projects, fresh corrections, recently
  reinforced preferences, and long-term goals.
- Continuity state is stored in `state_home/ghost/continuity.json`, with
  `state_home/ghost/continuity_events.jsonl` as a small audit/rebuild log. It
  supports export, reset, delete-scope, and explicit rebuild controls.
- Runtime continuity reads are projection-only. Prompt rendering does not
  rebuild missing projections, quarantine corrupt files, append events, call a
  provider, or scan project source.
- Model-visible text uses neutral `Local Context` wording: bounded local
  continuity is not new user input, not Research evidence, cannot grant tools,
  cannot bypass approval, and cannot override the current request or project
  instructions. Internal Ghost naming, sensitive text, dangerous instruction
  hierarchy language, raw model replies, raw Research bodies, webpage text, and
  source snippets are not rendered.
- Normal Chat and `planning_readonly` can read continuity context. Consensus
  sends it only to the owner prompt. Project Writer, Reviewer, Research, and
  protocol repair prompts still receive no Ghost context.
- Task completion now runs a best-effort local continuity sync after the
  learning loop. It does not call providers; Chat contributes only a short
  user-focus excerpt, while planning can also contribute bounded run ledger
  projection facts. The new context is eventual-consistent and is intended to
  be relied on after the post-turn `ghost_continuity_done` event rather than at
  the exact instant `task_done` is emitted. `ghost disable` prevents automatic
  continuity sync while keeping local preview/export/delete/reset controls
  available.
- Research synthesis / decision notes contribute only titles and bounded
  `Open questions` section lines. Raw note bodies, evidence sections, source
  snippets, and webpage text are not rendered into model-visible continuity.
- Extended Ghost CLI with `python -m codey ghost continuity` and
  `python -m codey ghost rebuild-continuity --yes`; `export`, `reset`, and
  `delete-scope` now cover continuity files too.
- Added `tests/test_ghost_continuity.py` and
  `tests/manual/ghost_continuity_ab.py`. The manual probe uses a fixed
  temporary `continuity.json` seed so live A/B checks context behavior without
  involving the learning extractor.

## 0.3.4 - Ghost Learning Loop v1

- Added `codey/ghost/typed_fields.py` as the shared typed memory-field
  allowlist used by the signal extractor contract, deterministic gate, and
  directive renderer. Model-visible memory text remains generated from known
  slot/value templates; unknown or protected fields stay non-renderable.
- Added `codey/ghost/learning_loop.py`, a post-turn best-effort learning flow
  that runs `GhostSignalExtractor`, writes the raw signal audit first, ingests
  into the inbox/gate, then syncs accepted candidates into Hebbian state.
  Provider/browser access is injected from outside the Ghost package, so
  `codey/ghost` still does not import provider, browser, Research, or tool
  runtime modules.
- Normal Chat now triggers the learning loop after `task_done` is emitted.
  The extractor uses a fresh provider tab through the server-injected factory,
  so it does not type the extractor JSON contract into the user's current chat.
  `planning_readonly` has code coverage but is not enabled for automatic
  learning by default.
- Auto-accept is stricter: high-confidence `style_preference` signals are
  accepted only when they include a grounded, known typed field that can render
  safely in the next directive. Unknown style fields remain candidates, and
  `correction` / `action_tendency` are not automatically reinforced.
- `ghost disable` now prevents post-turn extractor calls while list/export,
  directive preview, reset, and delete-scope controls continue to work.
- Added `tests/test_ghost_learning_loop.py` and
  `tests/manual/ghost_learning_loop_ab.py`. The manual probe runs one web
  provider at a time and checks fresh-tab extraction, directive change, answer
  style change, negative no-signal behavior, and internal naming leakage.
- Live A/B passed one provider at a time on DeepSeek, MiMo, Qwen, GLM, and
  StepFun after restarting the dedicated Edge CDP session between providers.
  Each provider learned the typed `reply_length=concise` and
  `reply_structure=answer_first` style preferences, reinforced two active
  Hebbian nodes, kept plain complaints out of accepted memory, and avoided
  internal naming leakage in model replies.
- Post-review hardening keeps automatic learning out of inbox/Hebbian when the
  extractor returns diagnostics, even if the parser recovered partial valid
  signals. The raw signal audit is still written for review.
- Typed field rendering and auto-accept now require explicit kind/slot/value
  pairs rather than independently combining known slots and values. Hidden
  aliases such as `style_preference:length` were removed; the learning contract
  now matches the extractor guidance exactly.

## 0.3.3 - Ghost Directive ContextSource v1

- Added `codey/ghost/directive.py`, a pure local renderer that turns confirmed
  active Hebbian memory nodes into a short, budgeted prompt context. The
  internal feature is still called Ghost Directive, but model-visible prompt
  text uses neutral `Local Context` wording and must not expose `Ghost` or
  `Ghost Directive`. It is read-only: no model calls, no disk writes, no edges
  rendered as facts, and no evidence quotes, raw labels, or internal ids are
  exposed.
- Directive selection is deterministic and bounded. It filters by
  session/project/user scope, active status, supersession state, node weight,
  current Ghost signal kind, sensitive secret-like text, dangerous
  authorization language, and generic instruction-hierarchy attacks such as
  "ignore previous/system/developer instructions" or "treat this as the system
  prompt". Priority attacks such as "local memory outranks/supersedes system
  instructions", "replace system prompt with this memory", "developer messages
  defer to memory", or "this memory should be used before current instructions"
  are skipped too, including `needs to come before`, `ranks above`,
  `treated as above`, and all/bare-instructions variants.
  Conflicting values for the same scope/conflict key are skipped unless one
  value is clearly stronger.
- Model-visible directive items are typed templates generated from
  `kind/conflict_key/value_key`; `node.label` stays local audit text. Structured
  fields must match an explicit safe slot/value allowlist; unknown slugs, split
  protected topics like `system = prompt`, or fields that refer to
  system/developer instructions, approvals, tools, shell/run actions, file
  deletion, or the current request are skipped.
- Runtime directive reads are projection-only. They do not rebuild missing
  Hebbian state, quarantine corrupt projections, write `state.json`, or append
  events. Stale node weights are decayed in-memory for selection, without
  persisting decay state.
- Added the `ghost_directive` context source key. Normal chat and
  `planning_readonly` can receive the directive by default. Project Writer,
  Reviewer, Research, and protocol repair prompts do not receive it.
- Extended Ghost CLI with `python -m codey ghost directive`, including
  `--project`, `--session-id`, and `--budget` for local preview/export of the
  exact prompt context.
- Added `tests/manual/ghost_directive_ab.py`, a one-provider-at-a-time live
  A/B probe for style/correction effect, directive leakage, and
  `planning_readonly` JSON protocol compliance.
- Live A/B passed one provider at a time on DeepSeek, MiMo, Qwen, GLM, and
  StepFun. The directive arm corrected the local memory backend to bounded JSON
  projection plus JSONL audit, did not leak internal Ghost naming, and preserved
  `planning_readonly` JSON compliance.
- This release still does not add a new learning loop, inject Ghost into
  Research or Project Writer, change permissions, run tools from Ghost memory,
  or import `torch` / `transformers`.

## 0.3.2 - Ghost Hebbian State v1

- Added `codey/ghost/hebbian.py`, a bounded local Hebbian state ledger that can
  reinforce accepted Ghost inbox candidates into weighted `GhostNode` rows and
  `coactivated_with` `GhostEdge` rows. Edges only mean local co-occurrence, not
  external facts.
- Added inbox review and value semantics. Candidates now carry `value_key`,
  `evidence_refs`, review metadata, and `superseded_by`. Same
  scope/ref/conflict/value rows merge evidence; same scope/ref/conflict with
  different values stay as competing candidates instead of silently overwriting
  each other.
- Accepted candidates are no longer downgraded by later lower-confidence
  candidate/rejected ingest. A manual `accept` can supersede older accepted
  values for the same scope and conflict key, and ordinary ingest cannot revive
  a superseded value.
- Hebbian state is stored as `state_home/ghost/state.json`, with
  `state_home/ghost/hebbian_events.jsonl` as the separate audit/replay log.
  Projection and event files are bounded, bad projections are quarantined, bad
  event lines are skipped with warnings, and event replay can rebuild state.
- Reinforcement is deterministic and local: bounded weights, evidence-ref
  dedupe, continuous and idempotent half-life decay, edge fanout caps,
  project/session/user scope isolation, reset, export, and delete-scope
  support. Write failures fail open and do not affect chat, coding, or Research
  execution.
- Extended Ghost CLI controls:
  `python -m codey ghost accept/reject/state/rebuild-state`, and updated
  `export`, `reset`, and `delete-scope` to include Hebbian state and events.
  `accept` now backfills same-run coactivation edges when sibling nodes already
  exist; `reject` removes the corresponding active Hebbian node and connected
  edges from the active Hebbian log. `sync_from_inbox()` reconciles rejected and
  superseded inbox rows instead of only reinforcing accepted rows.
- Coactivation edge evidence is pair/run scoped, so the same candidate pair in
  the same run can backfill an edge once regardless of traversal order.
- Hebbian node kinds remain aligned with the five current Ghost signal kinds;
  future affinity/boundary node kinds are not accepted until their extractor and
  gate paths exist.
- `server.State` now creates `ghost_hebbian` only when `state_home` is present.
  Bare `State()` still disables Ghost writes.
- This release still does not generate Ghost Directives, inject prompt context,
  wire Ghost into TaskRunner, run automatic daily learning, alter
  chat/coding/Research behavior, add UI, or import `torch` / `transformers`.

## 0.3.1 - Ghost Memory Inbox v1

- Added `codey/ghost/inbox.py` and `codey/ghost/gate.py`. Ghost signals from
  0.3.0 can now be projected into auditable local memory inbox candidates, with
  a deterministic local gate deciding `accepted`, `candidate`, or `rejected`.
  In this release `accepted` only means "eligible for future 0.3.2 Hebbian
  reinforcement"; it does not affect model behavior.
- Ghost state is now split across `signals.jsonl`, `events.jsonl`,
  `inbox.json`, and `settings.json`. `events.jsonl` is the append-only source
  of truth, while `inbox.json` is a rebuildable projection. Bad projections or
  future schemas are quarantined; bad event lines are skipped with warnings.
- `events.jsonl` is compacted by both event count and byte size. If an
  oversized event log cannot be read after a projection is lost, Codey keeps an
  `events_too_large` warning instead of silently writing an empty projection.
- 0.3.1 deliberately does not write `state.json`; that file is reserved for the
  0.3.2 Hebbian state. Candidate types stay aligned with the five 0.3.0 signal
  kinds, with no early `boundary_candidate`.
- The gate is conservative: high-confidence `style_preference` signals may be
  auto-accepted, while `correction`, `research_interest`, `long_term_goal`, and
  `action_tendency` remain candidates by default. The gate does not use
  hard-coded Chinese/English phrase lists to auto-accept corrections or
  classify preference semantics.
- `conflict_key` uses structured `metadata.conflict_key` /
  `conflict_key_hint` when present, otherwise a stable text fingerprint. It
  does not rely on local language marker lists such as `tone` or
  `reply_structure`. Duplicate candidates with the same scope and conflict key
  are merged and counted with `reinforcement_count`.
- Added local controls:
  `python -m codey ghost list/export/reset/delete-scope/enable/disable`.
  `export` includes both the inbox projection and raw `signals.jsonl` audit.
  `reset` / `delete-scope` clean both raw signal audit and the inbox/events
  active store instead of leaving deleted text behind as tombstones. `reset`
  and `delete-scope` require `--yes`.
- `disable` blocks future ingest only; list/export/delete continue to work.
  `enable` / `disable` return false when the audit event cannot be written
  instead of silently reporting success.
- `server.State` now creates `ghost_inbox` only when `state_home` is present.
  Bare `State()` still disables Ghost writes for embedded and test paths.
- This release still does not generate Ghost Directives, update Hebbian
  weights, inject prompt context, wire Ghost into the default TaskRunner
  learning loop, alter chat/coding/Research behavior, or import `torch` /
  `transformers`.

## 0.3.0 - Ghost Signal Extractor v1

- Added `codey/ghost/`, a provider-neutral Ghost signal extraction layer for
  explicit learning signals. It recognizes candidate `style_preference`,
  `correction`, `research_interest`, `long_term_goal`, and `action_tendency`
  signals.
- Added `GhostSignalCodec`, a narrow JSON contract that asks an external
  provider to return bounded signal candidates. `evidence_quote` must be
  grounded in the current user message; invented quotes, unknown kinds, invalid
  scopes, bad confidence values, malformed JSON, and multiple JSON objects are
  rejected into diagnostics. Candidate signals that look like passwords, API
  keys, bearer tokens, private keys, or high-entropy secrets are rejected before
  they can be written to the local signal log.
- Added `GhostSignalExtractor`, a fail-open provider wrapper intended for
  manual/shadow use. Provider errors produce no signals and do not affect chat,
  coding, or Research execution.
- Added `GhostSignalStore`, an append-only candidate event log under
  `state_home/ghost/signals.jsonl`. It stores bounded candidate summaries,
  quotes, diagnostics, and metadata, not full transcripts or accepted long-term
  memory. Bare `State()` disables the store.
- Added `tests/manual/ghost_signal_extractor_ab.py`, a one-provider-at-a-time
  live probe with self-test coverage for explicit preferences, corrections,
  research interests, action tendencies, and no-signal controls. Connection
  failures are written as bounded failure rows instead of masking the original
  provider/CDP error with a probe exception.
- CDP robustness hardening from the live A/B: the Ghost manual probe always
  releases non-isolated Playwright automation even when keeping the provider
  tab open, and failed non-isolated browser launches now terminate their child
  process. Codey does not silently switch away from an attached provider port
  after Playwright attach failure; the correct recovery is to restart that CDP
  browser session.
- This release does not inject a Ghost directive into prompts, write accepted
  memory, update Hebbian weights, alter TaskRunner behavior, change Research or
  coding tool protocols, add UI, or import `torch` / `transformers`. The
  package root stays lightweight; provider/browser code is loaded only by the
  explicit extractor path, not by schema/store imports.

## 0.2.33 - Project-local Config v1

- Added `codey/project_config.py`, a strict parser for explicit
  `.codey/config.json` files. Project config is a bounded fact/preference
  source, not an authorization system.
- Project config can declare verification command candidates, scan ignored
  path prefixes, a `project_map_chars` budget hint, and future provider
  preferences. Provider preferences are parsed and validated only; they do not
  affect provider selection in this release.
- Configured verification commands feed the existing verification candidate
  pipeline with a stable source priority below previously successful checks and
  above manifest discovery. They still must pass the normal executable,
  cwd-in-project, and `tool_runtime` run allowlist checks.
- Configured `scan.ignored_paths` use project-root-relative prefix semantics.
  They are applied to Project Map listing, symbol overview, focused subtree,
  and verification discovery scans without weakening the existing secret,
  hidden, symlink, and default excluded-path filters.
- Project config warnings are rendered as a short ContextSource block for
  Writer/read-only planning prompts, so models can see when a config was
  partly ignored. Repair prompts remain short and do not include config
  context.
- `context.budget_hints.project_map_chars` can only reduce the Project Map
  render budget, with a lower bound; project config cannot expand prompt
  budgets.
- Live web-provider smoke hardening: StepFun now waits for composer text to
  survive late page hydration before submitting and reports missing send
  controls as send-button failures, the manual submit probe no longer leaves
  reused Playwright CDP sessions open, and `tools/live_smoke.py --provider all`
  targets web providers only instead of including `local`.
- Review hardening: oversized `.codey/config.json` files are rejected from
  file metadata before reading the body, project config validates provider
  hints against the lightweight static capability table instead of importing
  web adapters, config warning omission counts are reachable, and StepFun no
  longer keeps an unreachable Enter-submission fallback after the stable
  composer gate.
- This release does not add a workflow DSL, project-local permission matrix,
  shell auto-approval, automatic config writing, Research headless config, UI
  changes, or any relaxation of runtime safety guards.

## 0.2.32 - Headless JSONL Runner v1

- Added `codey/headless_runner.py`, a thin machine-readable runner backed by
  the production `TaskRunner` rather than a second agent loop.
- `python -m codey agent --json` now runs through the headless TaskRunner path.
  Plain `python -m codey agent` remains on the existing direct CLI path for this
  release.
- Headless JSONL emits bounded event projections for task start, status/info,
  turns, tool start/finish, shell rejection, and task completion. It does not
  dump UI-only state, full command logs, or full model replies.
- Project coding headless runs reuse Run Ledger, Managed Outputs, provider
  fallback ordering, change tracking, and receipt generation from the same
  orchestration spine as the UI. The first headless version uses a no-op review
  callback rather than silently opening a reviewer model.
- Added `--readonly` for JSONL mode. It maps to the internal
  `planning_readonly` profile, exposes read/search/reference tools only, does
  not collect diffs, does not create Work Checkpoints, and does not write
  ProjectFacts.
- Headless shell approval is default-deny: a `shell_request` is projected as
  `shell_rejected` with reason `headless_default_deny`, and the command is not
  approved or waited on.
- TaskRunner now has an explicit internal `planning_readonly` task kind. It is
  projected as `planning` in terminal events instead of being treated as a
  normal Project Writer run.
- This release does not add a background agent, Research headless automation,
  shell auto-approval, CI deployment actions, UI changes, or a new provider
  selection product surface.

## 0.2.31 - Internal Permission Profiles v1

- Added `codey/permission_profiles.py`, a small internal registry for runtime
  phase boundaries: `chat`, `research`, `coding_writer`, `reviewer`, and
  `planning_readonly`.
- Coding tool definitions can now be filtered by profile. `JsonToolCodec()`
  still defaults to the full Project Writer contract, while
  `JsonToolCodec(permission_profile="planning_readonly")` omits `edit`, `run`,
  and `shell`.
- Coding protocol errors now distinguish globally unknown tools from tools that
  exist but are not allowed in the current profile. `write_file` remains
  `unknown_tool`; `edit` in `planning_readonly` is `disallowed_tool`.
- `parallel` now checks both `parallel_safe` and the active profile, so a
  read-only profile cannot smuggle disallowed tools through a batch wrapper.
- Empty coding tool definition sets render no contract, and non-coding profiles
  fail fast if accidentally used to construct a coding codec. Tests also lock
  `coding_writer` to all current coding tool definitions.
- `agent.run()` accepts `permission_profile` for default codec creation and
  ContextSource filtering, while still respecting any explicit codec supplied
  by tests or manual probes.
- The private consensus/project-audit codec now uses `planning_readonly`,
  matching its existing read-only execution boundary.
- Project Writer calls are explicitly bound to `coding_writer`. Research and
  Reviewer profiles are declared and tested, but their stable runtimes are not
  rewritten in this release.
- This release does not add a user-visible mode switch, project-local permission
  config, a new safety system, headless execution, or any relaxation of
  `tool_runtime`, shell approval, safe-path, Research, or run allowlist guards.

## 0.2.30 - Managed Output Handles v1

- Added `codey/managed_outputs.py`, a run-scoped local store for command output
  that was too large for the model-facing `run` result.
- `run_command()` now has an internal raw/projection split. The default public
  behavior is unchanged; production Project Writer runs can save the raw
  stdout/stderr before dependency-stack pruning and prompt clipping.
- Managed output handles are written only when the projected `run` result is
  actually truncated. Short command output is not stored.
- Managed output metadata distinguishes the production `tool_id`,
  `original_bytes`, `stored_bytes`, `sha256` of the stored text, and
  `stored_truncated`. Single outputs and per-run handle counts are capped,
  paths are constrained under Codey's state directory, write failures fail open,
  and oversized stored outputs keep head and tail text with an omission marker.
- `ToolOutcome` and `ToolResult` now carry optional handle metadata. The JSON
  tool codec renders a short footer telling the model that the handle is for
  local audit/export, not a tool; full output is not injected into prompts.
- Run Ledger `tool_finished` events now record handle id, original/stored byte
  counts, and stored-output hash without saving full command output.
- `State()` enables managed outputs only when `state_home` is provided, matching
  Run Ledger behavior and avoiding writes to real `~/.codey` in bare tests.
- This release does not add UI, `/api/output`, `read_output`, full-text search,
  RAG, Research webpage storage, or automatic handle reads by the model.

## 0.2.29 - Provider Capability Registry v1

- Added `codey/provider_capabilities.py`, a static internal registry of
  provider capability hints: JSON reliability, coding/research/review fit,
  context budget hints, native-tool interference risk, canary hint, bounded
  failure families, and notes.
- `rank_providers()` is a pure deterministic ordering helper. It preserves
  input order as the tie-breaker, keeps an explicit preferred provider first,
  treats `avoid` as "rank later" rather than "disable", and returns default
  capabilities for unknown providers. Generic hybrid ranking uses the stricter
  of Research and Coding fit.
- `TaskRunner` now uses mode-aware capability ordering only when it must choose
  a replacement provider: selected provider unavailable, connect failure,
  canary failure, and Writer failover. Hybrid startup fallback is ranked as
  Research because the first phase is Research; hybrid Writer failover is ranked
  as Project. User-selected providers are not preempted by capability.
- `reviewer_candidates()` now runs candidate ids through the same static
  ordering helper for review mode while still filtering writer/local/unavailable
  providers and without exposing capability terminology to the UI.
- `ProviderSupervisor` remains the runtime health/cooldown/canary owner.
  Static capabilities are not persisted in `provider-health.json`, and runtime
  failures do not mutate them.
- Provider capability tests pin `failure_families` to the real
  `ProviderFailure` kind vocabulary. `context_budget_hint` remains a static
  hint and is not consumed by production policy in this release.
- This release does not add a provider ranking UI, model self-routing, Research
  mid-run failover, live A/B, runtime capability learning, or canary consumption
  from capability fields.

## 0.2.28 - ContextSource v1

- Added `codey/context_source.py`, a small prompt assembly layer that renders
  named context sources with per-source character budgets, freshness metadata,
  inclusion reasons, headings, and explicit failure policies.
- `agent.py` now wraps the existing project prompt blocks as `ContextSource`
  instances: project instructions, verified project facts, Research Brief,
  Project Map, Work Checkpoint, and initial listing. `ProjectTaskContextBuilder`
  still owns the business loading of facts, knowledge, maps, checkpoints, and
  verification candidates.
- `Coding current local context` is also rendered through `ContextSource`, but
  it is still appended only after local tool results. It is not added to the
  initial project prompt or protocol repair prompts.
- Optional context source failures fail open, but `TaskCancelled` and
  `DeadlineExceeded` are re-raised so user Stop and provider deadlines cannot
  be swallowed during prompt assembly.
- Work Checkpoint context budget is now derived from `work_checkpoint.py`
  producer limits, so a bounded checkpoint does not lose its changed-file list
  to source-level clipping.
- Source metadata is not rendered into model prompts. This release keeps the
  prompt content goal equivalent while making context blocks named, bounded,
  testable, and easier to audit.
- This release does not add live A/B, UI, provider routing, vector memory,
  automatic Research vault injection, checkpoint/restore migration, or new
  model capabilities.

## 0.2.27 - ToolDefinition v1

- Added `codey/tool_definition.py` as the single internal metadata source for
  coding tools. It covers the existing public JSON tool names only:
  `list_dir`, `read_file`, `read_files`, `grep`, `find_references`, `parallel`,
  `edit`, `run`, `shell`, and `done`.
- `JsonToolCodec` now consumes tool definitions for the rendered tool contract,
  aliases, parallel-safety checks, result tool names, and batch limits. The codec
  no longer owns or re-exports the tool definition table.
- `agent.py` now derives supported runtime tool names, information follow-up
  tool names, repair examples, and tool activity rows from the definition layer.
  The dispatch loop, schema validation, read-before-edit guard, shell approval,
  and run allowlist remain unchanged.
- `edit` declares the `file_changed` ledger fact and `run` declares
  `command_verified`; tests assert those declarations match Run Ledger v1
  output. `write` and `write_file` remain unknown tools and still repair toward
  `edit(content=...)`.
- Shell tool-start activity now uses the clearer text `Requesting shell approval
  for ...`; tests lock this intentional visible wording change.
- This is a small internal refactor, not a plugin system. No Research tools,
  UI controls, permissions UI, runtime safety gates, checkpoint, restore, or
  provider behavior changed.

## 0.2.26 - Ledger Projections v1

- Added `codey/run_ledger_projection.py`, a pure read model over Run Ledger
  JSONL records. It projects run lifecycle, provider selection/switch/failure,
  model reply counts, tool counts/errors, observed file edits, verified
  commands, final change summaries, and completion/truncation state.
- `changes_collected` now stores `checks_passed` as a top-level bounded fact.
  Receipt projection uses only `changed_count`, `mode`, and `checks_passed`;
  it does not read the nested legacy `receipt` dictionary back out of the
  ledger.
- `TaskRunner` now shadow-consumes the projection after `run_finished` is
  appended. Codey adopts the projected receipt only when the ledger is complete,
  not truncated, has final changes, and the projected `changed_count`,
  `restore_available`, and `checks_passed` exactly match the legacy receipt.
  Otherwise it falls back to the existing receipt path.
- This release does not change UI, checkpoint, restore, `ExecutionEvidence`,
  Research ledgers, API export, or headless behavior. Projection failures remain
  fail-open.
- Added focused projection tests plus TaskRunner coverage that verifies a
  complete ledger projection is read before the terminal event is published.

## 0.2.25 - Run Ledger v1

- Added a bounded `Run Ledger`: project coding runs now write an append-only
  JSONL fact stream under Codey's local state directory. The first slice records
  events such as `run_started`, `provider_selected`, `model_reply`,
  `tool_started`, `tool_finished`, `file_changed`, `command_verified`, final
  `changes_collected`, and `run_finished`.
- This is an observe-only layer. It does not change the `agent.py` loop, the web
  model JSON protocol, UI/SSE events, `ExecutionEvidence`, `WorkCheckpoint`,
  receipts, or restore. `TaskRunner` only projects already-observed local facts
  into the ledger.
- The ledger does not store full model replies, source files, shell output,
  browser DOM, or webpage text. `model_reply` stores reply length and a bounded
  note; tool results store a short first line.
- The ledger byte budget is derived from semantic constants:
  `MAX_LEDGER_EVENTS * LEDGER_BYTES_PER_EVENT_BUDGET`, currently about
  512 KiB. When the budget is exceeded, Codey writes one `ledger_truncated`
  event and stops appending. Ledger write failures fail open and do not break
  the active task.
- Terminal error paths now write a bounded `provider_failure` event before
  `run_finished`. Bare `State()` instances without a durable `state_home`
  disable run ledgers, so tests and embedded callers do not write project-run
  ledgers into the real user `~/.codey` directory.
- Added focused tests for path confinement, bounded model replies, edit/run fact
  projection, byte-budget truncation, append failure fail-open behavior, and
  TaskRunner project-run integration.

## 0.2.24 - Coding Current Context

- Coding now appends a bounded `Coding current local context` block after local
  tool results. It tells the web model which files were read this run, which
  existing files are eligible for exact edits, which changed files still need
  verification or are already covered, and the selected verification command
  when the current changes are not yet verified.
- The context is intentionally advisory. It is not an allowed-tools gate, not a
  hard state machine, and it does not change coding's existing multiple
  top-level-JSON compatibility behavior. Protocol repair prompts stay short and
  do not include the context block.
- Verification candidates are refreshed once after edits before the next tool
  prompt is sent, so the suggested check can appear before the model tries to
  finish. "Verified" is shown only when a successful run covers the currently
  selected candidate after the latest edit; once fresh, the context stops
  showing runnable verification JSON to avoid redundant check loops.
- Qwen submission handling now waits for both retained composer text and an
  enabled send button. This fixes a live A/B failure mode where Codey typed
  into Qwen before late page hydration finished, then the page cleared the
  composer and the send failed.
- Production-like live A/B on already-open provider tabs supported the change:
  DeepSeek, MiMo, and Qwen all kept success at `2/2`, while the context arm
  removed the generic default-verification reminder turns (`DeepSeek -2`,
  `MiMo -1`, `Qwen -2`) and finished in fewer turns. Prompt text increased by
  roughly 2K characters across two cases per provider.

## 0.2.23 - Coding Protocol Typed Repairs

- Coding JSON protocol errors now carry typed `protocol_error_kind` values for
  `no_json`, `unknown_tool`, `invalid_args`, `direct_answer`,
  `native_tool_denial`, and `nested_tool_in_done`, reusing the
  `ToolPlan.protocol_error_kind` field already proven in Research.
- The coding run loop now sends specific repair prompts instead of the same
  generic JSON reminder for every failure. Examples: `write_file` is corrected
  toward `edit(content=...)`; mixed edit modes explain that only one edit mode
  is allowed; `read_file offset=0` explains 1-based offsets; prose answers are
  redirected into `done.summary`; website-native "tool unavailable" replies are
  redirected back to local-runner JSON; and tool JSON nested inside
  `done.summary` is corrected to a direct tool call.
- Manual live A/B evidence now measures the production repair renderer
  directly. After tightening the prompt to generate previous-intent examples
  from the invalid JSON itself, DeepSeek improved from `clean_repair=5/6` to
  `6/6`, Qwen from `4/6` to `6/6` (with one transient baseline send rerun), and
  MiMo from `5/6` to `6/6` on six deliberately invalid coding replies. Earlier
  prototype wording showed the same direction but was treated as over-strong
  because it embedded ideal repaired shapes.
- Scope intentionally stayed narrow: coding still accepts accidental multiple
  top-level JSON tool objects for compatibility, and this release does not add
  a coding allowed-tools gate, verification candidate IDs, or concept-context
  prompt injection.
- Manual-only Research probe archival: `concept_context_ab.py` records the
  negative/neutral Concept Context injection result and remains outside
  production Research prompts.

## 0.2.22 - Concept Graph Seed

- Concept layer over the knowledge vault: notes can now declare typed
  concept relations (`relations: [{src, dst, kind}]` with kinds
  affects/uses/causes/part_of/enables/relates) in `knowledge_write`. Relations
  are normalized by the new `knowledge/concept_schema.py` (lowercasing, URL /
  machine-tag / year noise filtering, self-loop and duplicate removal, 8 per
  note), stored in note front-matter (Markdown stays authoritative), and
  cached in a rebuildable `concept_edges` SQLite table with per-note
  provenance. Relation endpoints are auto-merged into the note's tags.
- Concept Graph read model: new `knowledge/concepts.py` builds a virtual
  concept graph -- concepts never become Markdown notes. Declared relations
  become edges (support count in the label), recent synthesis notes attach to
  their concept tags via faint `tagged` edges, and the current session's
  concepts are focus-highlighted. Co-tags never create edges, and missing-link
  candidates (two concepts sharing a declared neighbor but no declared edge)
  appear only as text on the concept node, capped at 6 and marked
  "unproven; not facts". The Evidence Graph (`knowledge/graph.py`) is
  untouched.
- New `GET /api/research/concept_graph` diagnostic endpoint plus a unified
  Research drawer `Graph` tab. The user-facing graph is now composed from
  concepts, the current synthesis/report, related notes, and source URLs with
  depth 1/2/3, while the concept endpoint stays available for deterministic
  tests and diagnostics.
- Synthesis notes now aggregate the run's top concept tags from active notes
  instead of only machine tags, so reports connect to the concept layer
  without reviving contradicted/stale note tags.
- Contract discipline: `knowledge_write.relations` must be a list of objects
  (a single object is normalized to a one-item list; non-object items are
  typed `invalid_args`); lenient cleaning happens in the tool with explicit
  warnings in the tool result. The research prompt tells models to declare
  only relations the cited sources actually state.
- Review hardening: concept-node details list declared relations as grouped
  Outgoing/Incoming summaries with supporting note titles (the canvas stays
  undirected); open questions replace raw missing-link wording and remain
  marked "unproven; not facts"; only `status='active'` notes feed the concept
  layer; node/edge limits are hard -- synthesis attachments only spend leftover
  budget, direct bad limit args sanitize, Concept Graph node selection keeps
  declared relation endpoints in pairs before filling leftover space with
  tag-only concepts, Concept Graph edge selection prioritizes relations
  supported by notes from the current session instead of guessing from shared
  concept names, `concept_edge_rows` / `tag_concept_rows` read requested-session
  rows before global backfill so older runs survive vault growth, and the
  unified Graph preserves the current evidence spine before spending remaining
  budget on global concepts; the default Research
  controller prompt and its `knowledge_write` JSON shape now teach tags +
  relations too; an empty Graph shows the builder's guidance text instead of a
  generic "No graph yet".
- Live-provider hardening: MiMo code-block overlays are now read once by
  ignoring hidden duplicate layers, so one visible JSON tool call is not
  misclassified as `too_many_tools`; controller repairs now treat a
  currently-forbidden tool such as premature `knowledge_write` as
  `disallowed_tool` before teaching its argument schema; Research drawer tabs
  stay compact by exposing one unified `Graph` tab instead of separate
  evidence/concept graph tabs.
- Source-node display polish: source URL nodes now use titles recovered
  from synthesis source ledgers when available, and graph node details render
  short Markdown excerpts with the existing zero-build renderer.
- Not in this release by design: no concept context injected into research
  prompts, no co-tag inference, no edge-click UI, no alias/embedding merging.

## 0.2.21 - UI Asset Modularization

- Zero-build asset modules: the web UI is no longer one giant `index.html`.
  CSS variables live in `assets/tokens.css`, all other styles in
  `assets/app.css`, and reusable UI logic in plain-script IIFE modules:
  `render.js` (pure markdown/tool-line helpers), `research_drawer.js`,
  `changes_drawer.js`, and `provider_ui.js`, alongside the existing
  `research_graph.js`. No npm, no bundler, no ESM -- scripts still load
  synchronously in a fixed order.
- Safe asset serving: the server replaced its hand-written asset dict with a
  path resolver that only serves `/assets/*.js` and `/assets/*.css` from
  inside the assets directory; traversal attempts, directories, and unknown
  extensions return 404. `index.html` is served with `__CODEY_VERSION__`
  substituted, and every asset reference carries `?v=<version>` for cache
  busting.
- Thin core, thin wrappers: `index.html` keeps only the HTML skeleton,
  state/storage, session ops, SSE ingestion/reconciliation, composer send
  chain, and boot wiring. Extracted modules receive their dependencies via
  `init(deps)` at boot; existing call sites go through thin wrapper
  functions, so DOM structure, visuals, `/api/*`, SSE reconciliation, and
  provider behavior are unchanged.
- Architecture ratchet: new `tests/test_ui_architecture.py` enforces zero
  inline `<style>` lines, an inline `<script>` budget that may only go down
  (currently 1950 lines, actual 1915), one `window.Codey*` namespace per
  asset module, versioned references to existing files only, and the fixed
  script load order.

## 0.2.20 - Research Controller v1

- Production Research controller: Research now uses a thin ledger read-model to
  expose only currently reasonable tools each turn, instead of showing every
  Research tool shape all the time.
- Stable IDs: search results, opened sources, and source_search locators receive
  run-global `result_id`, `source_id`, and `hit_id` handles. The controller
  rewrites those IDs into the normal `url` / `pages` / `offset` arguments before
  the existing 0.2.18 tool contract validates the call.
- Source-write convenience: `knowledge_write` can use `sources:["s1"]` and
  `evidence.source_url:"s1"` in controller mode; Codey rewrites those source IDs
  to opened final URLs before saving.
- Non-linear gate: this is not a hard state machine. `knowledge_search`,
  `knowledge_read`, and `web_search` remain available so models can go back for
  local memory, counter-evidence, or better sources. `open_url`,
  `source_search`, `knowledge_write`, `knowledge_link`, and `done` become
  available only when the ledger makes them meaningful.
- Done discipline: `done` is normally allowed after saved evidence exists, with
  a narrow near-turn-limit escape for insufficient/no-citable-evidence reports.
  The deterministic report quality gate remains unchanged.
- Boundary discipline: no Deep Research Core prompt in production, no provider
  routing, no new UI mode, and no relaxed provenance/evidence rules.

## 0.2.19 - Research Browser Isolation and Thin-Gate Probe

- Research browser isolation: browser-backed `web_search` and HTML `open_url`
  now use a separate Research Edge profile and CDP port by default, instead of
  sharing the provider chat browser. Search/result/article tabs no longer live
  in the same 9222 browser as DeepSeek, MiMo, StepFun, Qwen, or GLM chat tabs.
- Isolated CDP hygiene: isolated browser sessions now choose free ports without
  consulting stale active/saved provider ports, so they cannot accidentally
  reattach to the shared provider browser.
- Page-fetch robustness: HTML fetch now retries short `Page.content()` races
  while the page is still navigating or replacing content, avoiding transient
  `open_url` errors from dynamic news pages.
- Research UI observability: the event bridge, UI state store, and turn dividers
  now keep protocol notes such as `(done)` and typed protocol notes such as
  `(direct_answer)`, so quality-gate, private evidence-review, or protocol
  repair turns no longer appear as blank turns. The runner also emits the
  quality-review message before asking the model to revise a failed `done`.
- Manual A/B thin gate: `tests/manual/deep_research_core_ab.py` gained a
  manual-only `thin_gate` arm with state-aware allowed tools, stable
  `result_id` / `source_id` rewrites, and atomic `send_start` trace events.
  A live MiMo `long-official-doc/thin_gate` probe completed in 8 turns with
  `done=True`, `quality_score=11`, zero protocol repairs, and four ID rewrites.
- Boundary discipline: no production Research controller yet, no automatic
  provider routing, no Deep Research Core prompt in the main chain, and no UI
  mode change. The thin-gate work remains evidence for a narrow 0.2.20
  allowed-tools/stable-ID controller.

## 0.2.18 - Research Tool Contract and Typed Repairs

- Research tool contract: all Research JSON tools now pass through typed local
  argument validation before execution, including `knowledge_search`,
  `knowledge_read`, `knowledge_write`, `knowledge_link`, `web_search`,
  `open_url`, `source_search`, and `done`.
- Typed protocol repairs: Research now classifies protocol failures as
  `no_json`, `unknown_tool`, `too_many_tools`, `invalid_args`,
  `direct_answer`, or `native_search_leak`, then sends a specific repair prompt
  with one copyable JSON shape.
- Safer argument handling: optional defaults are applied only when fields are
  missing; malformed numbers such as `offset="abc"` are rejected instead of
  silently defaulting, aliases such as `queries` and `summary` are normalized,
  and unknown extra args do not pass through to tools.
- Final report discipline: `knowledge_write type="synthesis"` is rejected with
  a repair that points to `done`; Codey saves the synthesis after the final
  report passes quality review.
- Research browser isolation: browser-backed web search and page fetch now run
  on a dedicated Research browser worker instead of reentering the provider
  browser worker, fixing Playwright sync-API failures seen during live Research
  runs.
- MiMo submit stability: MiMo now waits briefly for the response footer/copy
  action to settle before the next send, matching the StepFun-style pacing fix
  for pages that finish text before their composer/action area is ready.
- Live provider check: Qwen stayed JSON-format clean but still hit the 10-turn
  cap before `done`; MiMo first showed a typed `too_many_tools` repair, then
  passed a continuous long-message submit probe and completed
  `long-official-doc/source_search` with `done=True` in 9 turns after the footer
  wait and final-report clarification.

## 0.2.17 - Source Search Production and Research Tool Boundary

- Production Research tool: `source_search` is now part of the default Research
  JSON protocol. It searches only within sources already opened by `open_url`
  and returns locators, offsets, PDF pages, and short previews.
- Research tool boundary: the Research prompt now explicitly forbids using a
  chat website's built-in web search, browsing, plugins, or outside knowledge.
  Web and knowledge access must go through Codey's local JSON tools.
- Single-action discipline: Research now tells providers to choose exactly one
  tool per turn, and the Research JSON parser rejects multiple tool calls in one
  reply so MiMo-style tool floods go through the normal protocol repair path.
- Evidence boundary: `source_search` does not write evidence, does not update
  PDF `pages_read`, and does not relax report quality. HTML uses soft locator
  discipline; PDF page-specific citations still require `open_url pages="N"`.
- PDF bounded scan: after a PDF URL has been opened once, `source_search` may
  re-fetch that same opened URL and scan a bounded first-page range for locator
  hits without recording those pages as read evidence.
- Manual A/B hygiene: baseline runs can instantiate
  `JsonToolCodec(include_source_search=False)` so production source_search does
  not contaminate the no-source-search arm; the manual harness now reuses the
  production source-search matching logic and has a manual-only
  `--single-tool-boundary` probe switch for provider diagnosis.
- Live MiMo follow-up: without the single-tool boundary, MiMo repeatedly emitted
  multiple search calls. With the boundary enabled on a fresh tab, MiMo completed
  the `long-official-doc/source_search` fixture in 10 turns with
  `quality_score=11`, `done=True`, zero protocol repairs, opened the target
  offset, saved exact evidence, and passed report quality.
- Boundary discipline: no `deep_core` production prompt, no UI change, no role
  router, no vector index, and no HTML range hard gate.

## 0.2.15 - Source Search Research Hygiene

- Qwen submit stability: the Qwen adapter now verifies that the controlled
  composer keeps the full message across a short settle window, and refills a
  few times if late page hydration clears the draft before submit.
- Research protocol tolerance: `web_search`, `knowledge_search`, and the manual
  A/B `source_search` probe accept a single query from either `query` or the
  common model mistake `queries`.
- Research report quality: numbered source lines in the URL-first form
  `1. https://final-url - Title` now pass the same provenance checks as
  title-first source lines.
- Manual A/B harness hygiene: fresh-tab and keep-open diagnostics now have safe
  defaults so older scripted calls keep working.
- A/B evidence: DeepSeek, StepFun, Qwen, and local Gemma4-12B probes now all
  support the same conclusion: deterministic `source_search` inside
  already-opened sources improves long-document/PDF evidence recall. The heavier
  `deep_core` plan/coverage prompt remains manual A/B only.
- Boundary discipline: no default Research prompt broadening, no role router,
  no UI change, and no new production source-search tool yet.

## 0.2.14 - StepFun Submit Stability

- StepFun send stability: updated the profiled send control for the current
  `custom-icon-send-outline` button and added a response-footer idle wait based
  on StepFun's reload action. Codey now waits for the answer surface to finish
  rendering before the next send, which avoids swallowing follow-up prompts when
  StepFun is still settling.
- Submission certainty: StepFun no longer treats a textarea newline/change as
  proof that a message was submitted. If a click cannot be confirmed by an empty
  composer or new response activity, the adapter fails fast with the existing
  `SubmissionUncertain` boundary instead of waiting for a fake timeout.
- Manual probes: added low-send provider submit probes and fresh-tab/error
  diagnostics for the Deep Research A/B harness so live web-provider issues can
  be checked one arm at a time without burning a full research run.
- Boundary discipline: no provider role router, no UI change, no prompt
  broadening, and no change to the provider-independent coding/review/research
  core.

## 0.2.13 - Provider Fit Update

- Provider set: added StepFun as an additional supported web provider while
  keeping Xiaomi MiMo available. The UI, provider registry, browser warmup,
  provider profiles, repair policy, and worker port offsets now cover both
  `mimo` and `stepfun`.
- StepFun adapter: added a dedicated `codey/stepfun.py` driver and
  `StepFunWebProvider` wrapper for `https://chat.stepfun.com/chats/`. The
  adapter uses StepFun's ordinary textarea composer, reads the newest markdown
  answer while ignoring reasoning blocks, and keeps send/read logic isolated
  from the agent core.
- Provider selection rationale: live probes showed StepFun can follow the local
  JSON-tool protocol one action at a time in the Research fixture. It passed a
  small edit-and-test coding smoke after the existing protocol nudge recovered
  initial non-JSON tool-call markup, but a fresh project creation smoke still
  failed on Python syntax repair, so it is not promoted as the strongest
  default project writer. MiMo remains useful for coding/editing, but is not
  recommended for strict JSON-tool Research after live probe failures.
  MiniMax was not selected because its Agent page ignored the local JSON-tool
  protocol on the first probe and used its own web/agent behavior instead.
- Boundary discipline: no role router, no new UI mode, no prompt broadening for
  other providers, and no change to the provider-independent agent/tool/review
  core.

## 0.2.12 - Research A/B and Provider Parsing Hygiene

- Manual Deep Research A/B: the live probe now defaults to a low-send `cheap`
  profile, tells web models to use only the local JSON tools against fixture
  sources, and writes an atomic `.trace.json` file after each provider reply so
  protocol and quality-gate failures can be inspected without rerunning a model.
- Research diagnostics: A/B rows now include send/reply counts, done attempts,
  protocol/quality repair prompt counts, opened sources, evidence items, raw
  reply previews, and the last `done` quality review.
- Provider parsing: DeepSeek can now return stable malformed JSON-tool-shaped
  replies promptly so the Research protocol repair loop can fix them instead of
  waiting until timeout.
- Research quality hygiene: Chinese-adjacent citations such as `结论[1]` are
  accepted, and URL provenance parsing treats Chinese brackets, backticks, and
  quotes as URL boundaries.
- Runtime tolerance: CDP attach timeout is longer to handle loaded browser
  sessions while leaving the warmup timeout unchanged.

## 0.2.11 - Provider Readiness Self-Repair

- Provider diagnostics: added a narrow `readiness_stale` failure kind so
  adapters can report cases where safe DOM facts suggest the page is usable but
  a readiness signal has gone stale.
- Safe failure facts: `ProviderFailure` can now carry a small sanitized facts
  packet for self-repair. Facts use an explicit readiness allowlist
  (`composer_visible`, `send_visible`, `model_selector_text_present`,
  `response_count`, `question_count`, `waited_for`) and ordinary failures still
  omit empty facts from their payloads.
- Self-repair routing: `readiness_stale` is treated as a structural provider
  failure for circuit/self-repair purposes, while keeping the existing rule
  that repairs are queued only after the provider circuit opens.
- Adapter repair prompts: self-repair worker now forwards failure kind, stage,
  and sanitized facts into the adapter repair prompt so the repair model can
  distinguish stale readiness checks from missing controls.
- Boundary discipline: no UI changes, no user-project access, no webpage body
  capture, no new repair framework, and no change to the fresh-chat marker
  canary before enabling a provisional adapter override.

## 0.2.10 - Research Quality and Provider JSON Hygiene

- Web provider JSON hygiene: moved shared JSON-tool reply detection/repair into
  `codey/json_tool_reply.py` and reused it from DeepSeek and Qwen. Qwen no
  longer depends on the stale `/api/v2/models/` bootstrap signal, can finish
  stable JSON tool replies promptly, and prefers DOM JSON when copied text is
  stale or incomplete.
- Research quality gate: final reports now accept common source formats such as
  `[1] https://final-url` and numbered `Available at:` lines while still
  requiring cited source URLs to be opened and evidence-backed. The provenance
  check is stricter for conclusion/evidence/source sections, but lets
  counterpoint and coverage sections mention unopened search-result domains as
  limitations.
- No-source research reports: when a Research run searched but found no citable
  opened source, Codey can now accept a clearly labelled no-citable-source
  report instead of forcing invented citations.
- Research repair UX: quality-gate followups now explain the exact hard
  requirements instead of sending a generic continue prompt, and the Research
  system prompt uses the neutral "local research agent" wording.
- Plain chat display: chat replies now carry `run_id`, `task_done` includes the
  chat answer as its summary, and the frontend de-duplicates `reply` /
  `task_done` answer events so normal chat answers can be restored and shown
  reliably.
- Manual Research A/B: added `tests/manual/deep_research_core_ab.py` as a live
  provider harness for source-search / plan / coverage experiments. The probe
  does not change default production Research behavior.

## 0.2.9 - Runtime Responsiveness Hygiene

- SSE reliability: when a subscriber queue is full, Codey now drops the oldest
  queued event and keeps the newest event instead of silently losing the newest
  state update.
- Research restore responsiveness: restoring Research note changes now schedules
  a coalesced background knowledge index rebuild, so `/api/research/restore`
  can return without waiting for a full vault scan.
- Review responsiveness: rejected Review repair reuses the project map already
  refreshed for the same review cycle instead of refreshing it twice.
- Boundary discipline: no API payload changes, frontend changes, incremental
  indexer, workspace watcher, or background task framework.

## 0.2.8 - Research TaskRunner Hygiene

- TaskRunner hygiene: split `TaskRunner.run()` mode execution into private
  chat, research, hybrid, and project helpers while keeping run reservation,
  provider lifecycle, cancellation, and terminal `finish_run()` handling in the
  main orchestration method.
- Boundary discipline: added only small private frame/work/hook data carriers
  inside `task_runner.py`; there is no new router, strategy system, task mode,
  or module split.
- Compatibility: kept task events, payloads, provider failover behavior,
  review flow, project memory writes, and Research handoff behavior unchanged.

## 0.2.7 - Research Server Hygiene

- Research API hygiene: moved the `/api/research/graph`,
  `/api/research/note`, and `/api/research/restore` response assembly into
  small `server.py` helpers that return `(status, payload)`.
- Run submit hygiene: moved `/api/run` validation and submit response assembly
  into a matching helper, keeping `_submit_task()` and task execution behavior
  unchanged.
- Boundary discipline: kept helpers inside `server.py`; there is no router,
  `server/research.py` module, schema change, or payload/status-code change.

## 0.2.6 - Frontend Research Graph Split

- Frontend hygiene: moved the Research drawer Graph implementation out of the
  monolithic `index.html` script and into `codey/web/assets/research_graph.js`.
  The main HTML now keeps only the drawer wrapper and callbacks for depth,
  note handoff, and source opening.
- Static asset boundary: added a narrow whitelist route for
  `/assets/research_graph.js`; Codey still does not expose `codey/web` as a
  general static directory.
- Compatibility: kept the existing Graph UI, canvas layout, CSS, callbacks,
  and global browser shape. There is no ES module migration, bundler, CSS
  split, or frontend framework change.
- Cache hygiene: the HTML loads the graph script as
  `/assets/research_graph.js?v=0.2.6` so webview/browser sessions pick up the
  split module during the release.

## 0.2.5 - Research Graph

- Research drawer Graph: added an Obsidian-like local graph tab that visualizes
  the current Research run as note/source nodes connected by
  `derives` / `supports` / `contradicts` / `implements` / `verifies` links.
  The graph uses a lightweight canvas force layout with hover highlighting,
  click details, source URL opening, Depth 1/2, and Reset.
- Graph read model: added `codey/knowledge/graph.py` plus bounded index queries
  for notes, incoming/outgoing links, and note sources. Markdown notes remain
  the source of truth; SQLite remains a rebuildable cache; the graph is loaded
  on demand through `/api/research/graph`.
- Provenance display: URL sources become virtual `source_url` nodes with
  virtual `cites` edges for display only. `cites` is intentionally not a
  persistent note link kind.
- Counterpoint hygiene: virtual counterpoint nodes are created only from the
  current run's counterpoint payload when there is no real `contradicts` link.
  The graph does not parse synthesis Markdown sections.
- UI language: the drawer graph keeps Codey's monochrome design, using grey
  nodes and lines by default. `--ok-dot` appears only as an interaction accent
  on the hovered node and its connected edges.

## 0.2.4 - Research PDF Intake

- PDF source intake: `open_url` can now read text PDFs directly. There is no
  `open_pdf` tool, PDF mode, or extra UI button; PDF is handled as another
  Research source type.
- Bounded extraction: Codey reads a bounded page range by default, streams PDF
  downloads with a hard byte cap, caps extracted text, and returns neutral
  `SKIPPED` results for scanned, oversized, empty, or extraction-failing PDFs.
  PDF redirects are followed manually and checked against URL policy before
  each next request, so a public PDF URL cannot silently redirect into local or
  private network targets.
- Page-aware evidence: the Evidence Ledger records `content_kind`, MIME type,
  page count, pages read, truncation state, and page locators for snippets.
  `knowledge_write` can accept `evidence.page`, infer pages from snippets, and
  replace bad excerpts with exact text from the opened PDF page.
- Report quality gate: final reports can cite PDF evidence as `[1 p.4]` or
  `[1 pp.4-5]`. Page citations pass only when the cited PDF page was read and
  has snippet-backed evidence.
- UI and handoff: the Research drawer shows PDF page locators in `Evidence`
  and PDF/page/truncation metadata in `Sources`. Synthesis notes and Project
  Briefs carry the same page-aware evidence without injecting the full vault.
- Dependency: added `pypdf>=6.0,<7` for pure-Python PDF text extraction.

## 0.2.3 - Research Provenance Polish

- Research provenance: explicit URL citations remain exact, but source-quality
  text can now name a parent site domain for an opened child host. For example,
  opening `docs.python.org` allows a quality note to say `python.org`, while
  opening `python.org` still does not allow a report to claim
  `docs.python.org`.
- Research quality gate: URL spans are excluded from bare-domain scanning, so
  paths such as `pathlib.html` are not misread as unopened source domains.
- Project memory: added an integration regression for the verified
  implementation memory path, covering implementation notes, verification
  notes, and `implements` / `verifies` links back to the research synthesis.
- Hygiene: removed a stale `EvidencePack` import from the task runner.

## 0.2.2 - Research Report Quality

- Evidence Ledger: each Research run now records search queries, ranked search
  results, opened requested/final URLs, retrieval timestamps, source-quality
  hints, and short evidence snippets.
- Report quality gate: final Research reports must include `Conclusion`, `Key
  evidence`, `Counter-evidence / limitations`, `Source quality`, `Search
  coverage`, and `Sources`. Numbered citations must map to sources Codey
  actually opened as final URLs in the run, and each cited source must have at
  least one saved evidence snippet copied from the opened page text.
- Evidence discipline: evidence snippets attached to notes must appear in the
  opened page text. Search results are still not evidence until `open_url`
  reads them, and low-quality reports are sent back to the researcher for
  revision instead of being saved as synthesis.
- Research UX recovery: unreadable sources such as PDFs are now shown as
  neutral `SKIPPED` tool results so the researcher can continue with readable
  HTML sources. If a model supplies a paraphrased evidence excerpt for an
  opened page, Codey replaces it with an exact opened-page snippet and keeps
  the quality gate strict.
- Report parsing: the quality gate accepts common numbered headings such as
  `1. Conclusion` / `一、结论` and source rows written as Markdown links, while
  keeping citation provenance strict.
- Advisors: Research MoA advisors now receive a richer read-only EvidencePack
  with citations, evidence items, coverage, notes, and source URLs; advisors
  still cannot browse or write the vault.
- UI and handoff: the Research drawer now has `Evidence`, `Sources`, and
  `Notes` tabs, with search coverage shown inside `Evidence` as supporting
  audit detail. Project handoff carries a bounded Research Brief with citation
  map, evidence items, counterpoints, and source-quality risks instead of the
  full vault.

## 0.2.1 - Research Polish and UI Follow-through

- Local provider: removed the explicit saved-key clearing checkbox. Leaving the
  API key field blank keeps the saved key; entering a new key replaces it on
  `Connect`.
- Research evidence flow: citing a search-result URL before opening it now
  returns `NEEDS_OPEN` instead of a red error. `NEEDS_OPEN` is a neutral
  `needs_action` tool status, not a saved note and not a changed tool result.
  Both generic run events and the Web/SSE production event path carry the same
  status.
- UI: active `Research` now only brightens the text; it does not add a border,
  background, or font-weight change. Assistant replies render expanded by
  default, with `Collapse` available for long answers.
- Markdown: assistant report rendering now supports `#` through `######`
  headings and basic nested lists while staying dependency-free and
  monochrome.

## 0.2.0 - Research, Knowledge, and Local Models

This is a major workflow release. Codey is no longer only a local coding loop:
it can explicitly research a question, save grounded local notes, carry a
bounded synthesis into a project, and remember verified implementation facts.

- Research: the composer context now includes `Research`. When enabled for a
  message, Codey can search the web, open pages, enforce URL policy, write
  source/fact/synthesis notes, and reject final citations that were not opened
  in the run.
- Knowledge: research notes are stored in a local Markdown vault with a
  rebuildable SQLite FTS index, per-run restore, note links, and a bounded
  Research Brief for project handoff. Project source code is not copied into
  the vault.
- Project handoff: after research, choosing a folder injects only the bounded
  Research Brief into the Writer prompt. Follow-up Research and Project Hybrid
  runs receive bounded prior chat context so prompts like "continue researching
  that plan" keep working.
- Local provider: `Local` connects to OpenAI-compatible endpoints such as LM
  Studio, Ollama, or llama.cpp. The compact config popover supports base URL,
  model id, optional API key preservation, and replacing the saved key by
  entering a new one.
- Safety and reliability: web provider sends stay on the browser-worker thread;
  only thread-safe Local sends use cancellable background send. Browser-worker
  calls are reentrant, preventing Research search self-deadlocks. Hidden-browser
  runtime code was removed from Codey's runtime path.
- UI: Research is a lightweight composer token beside `Choose folder` and the
  model name, not a separate app or another button beside the model selector.
  The Research drawer shows notes, source URLs, synthesis, and restore state.

## 0.1.63 - Single Provider Self-Review

- Review: when no different provider is available for final diff review, Codey
  now opens a temporary fresh tab for the Writer's same provider and runs a
  clearly labelled self-review pass.
- Repair loop: self-review findings reuse the existing Reviewer-to-Writer
  repair path, but Writer follow-up wording no longer claims that a second
  model reviewed the diff.
- Safety: true second-model review is still preferred. Self-review does not
  clear the Writer provider session, closes the temporary reviewer tab in a
  `finally` block, and failures continue to fall back to the existing
  single-model result.

## 0.1.62 - Review Impact Map

- Review: final diff review now receives a short, bounded Review Impact Map
  after the ChangeSet summary. It lists obvious changed symbols plus local
  caller/test reference hints so reviewers can inspect likely blast radius.
- Reliability: changed-symbol extraction is centralized in `changed_symbols.py`
  and is reused by Verification Map. Rename cases use the old symbol name for
  reference scans while preserving the new changed-file path from ChangeSet.
- Safety: the map is review-only, source-body-free, best-effort, and explicitly
  labelled as not coverage proof. Writer behavior, UI, tools, provider logic,
  and `/api/changes` remain unchanged.

## 0.1.61 - ChangeSet Anchored Review

- Review: final diff review now receives a structured ChangeSet summary before
  the raw diff, including changed files and parsed hunk ranges.
- Reliability: reviewer findings may include optional `hunk_index`,
  `new_line`, or `old_line` anchors. Codey validates those anchors against the
  actual changed hunks before passing them back to the Writer; path-only
  findings remain valid.
- Compatibility: `/api/changes`, the UI diff drawer, receipts, restore, and the
  underlying `changes.py` dict output are unchanged. Git rename labels such as
  `old.py -> new.py` are normalized only inside the ChangeSet interpretation
  layer so review hunks attach to the new path.

## 0.1.60 - CLI Agent JSONL

- CLI: `python -m codey agent --json ...` now emits one JSON object per stdout
  line, including a session header, agent start/end records, turn events,
  status/info events, and bounded tool start/result records.
- Integration: JSONL output is intended for scripts, CI wrappers, benchmarks,
  and external launchers that need stable progress and final-result fields
  without parsing human-readable stderr logs.
- Safety: normal CLI mode is unchanged. JSONL tool records include only compact
  result summaries, bounded text fields, and command/status metadata; provider,
  server, UI, agent, and tool execution behavior are unchanged.

## 0.1.59 - Package Manager Setup Hints

- Reliability: setup context now uses the same Node package-manager detection
  as trusted verification discovery: `packageManager` first, then local
  lockfiles, then parent lockfiles, then `npm`.
- UX: setup hints now recommend concrete install commands such as `pnpm install`,
  `yarn install`, or `npm ci or npm install` instead of generic package-install
  wording.
- Consistency: shell approval follow-up now renders trusted check candidates
  through the shared verification candidate formatter, keeping cwd-scoped
  command lines aligned with Project Map and Verification Map.

## 0.1.58 - Scoped Successful Change Checks

- Reliability: successful-change facts now preserve the working directory for
  local checks, so scoped validations render as `backend/: npm test` instead of
  losing the path context.
- Compatibility: existing project fact files with legacy string-only
  `successful_changes[].checks` continue to load with `cwd="."`; new writes use
  structured `{command, cwd}` check records.
- Safety: non-check commands, sensitive commands, and unsafe working-directory
  values remain filtered before they can become durable project facts.

## 0.1.57 - Policy-Sourced Verification Candidates

- Reliability: Project Map and Review Verification Map now receive candidate
  check commands from the same trusted `verification_policy` discovery path,
  instead of letting Project Map infer commands from manifests on its own.
- Review: Verification Map now labels only the uniquely selected, change-relevant
  command as `Recommended local check candidates`; broader commands remain
  available under the weaker broader-candidate label when no unique choice
  exists.
- Cleanup: direct `render_project_map()` calls no longer guess candidate
  commands. Manual probes that need production context now use
  `ProjectTaskContextBuilder`, keeping evaluation scripts aligned with the
  real Writer path.

## 0.1.56 - Composer Folder Label Cleanup

- UX: no-project chats now keep the composer context label as `Choose folder`
  even when the composer contains a draft. The draft-send behavior remains
  explicit through the same folder click, with the longer wording kept out of
  the visible composer chrome.
- Safety: no behavior change to project access. The user still has to choose a
  folder explicitly, and pressing Enter in a no-project chat remains a normal
  chat send.

## 0.1.55 - Draft-to-Project Send

- UX: a plain New Chat can now be attached to a project folder in place from
  the composer context. If the composer has a draft, the same explicit folder
  click keeps that draft and sends it in the same session after the folder is
  chosen.
- Continuity: chat-to-project transitions now preserve the prior conversation
  handoff and visible recent chat facts for the Writer, instead of starting the
  project task without the discussion that led to it.
- Safety: there is no natural-language intent detector and no automatic project
  access. The user must explicitly choose the folder context; pressing Enter in
  a no-project chat remains a normal chat send.

## 0.1.54 - Trusted Verification Discovery

- Reliability: trusted post-edit verification discovery now recognizes more
  safe project checks that were already permitted by the local `run` tool,
  including package-manager scripts selected from `packageManager`/lockfiles,
  `pytest` config, `tests/` unittest discovery, `ruff`/`mypy` config, and simple
  safe Makefile targets.
- Selection: completion-time verification candidates now use a small command
  priority so discovering `test`, `typecheck`, `lint`, `build`, and Makefile
  targets does not make common projects ambiguous. More specific ecosystem
  checks beat Makefile fallbacks.
- Safety: no UI changes, no automatic installs, no shell permission expansion,
  and no automatic execution behavior were added.

## 0.1.53 - CDP Browser Warmup

- UX: launching the UI now schedules a best-effort browser warmup that prepares
  the Codey-controlled CDP browser and opens DeepSeek, Qwen, MiMo, and GLM tabs
  when no provider tab is already visible.
- Safety: warmup does not check login state, send test messages, change UI, or
  bypass provider supervisor health filtering. Existing provider tabs are reused
  and no duplicate provider pages are opened.
- Reliability: warmup runs on the shared browser worker with short timeouts,
  avoids reusing unrelated external CDP browsers, keeps slow provider pages when
  they reached the target URL, and closes failed blank warmup tabs.

## 0.1.52 - Provider Send Loop Consolidation

- Maintainability: added shared provider send-loop helpers for response-watch
  lifetime, response stability state, completion-flow checks, flow response
  reads, and standard timeout recovery.
- Scope: migrated GLM, Qwen, DeepSeek, and MiMo to the shared helpers while
  keeping provider-specific submission, completion, retry, and response-reading
  behavior inside each web driver.
- Safety: no UI changes, no selector changes, no provider base class, and no
  broad `run_send_flow` callback framework were introduced.

## 0.1.51 - Shell Approval Follow-up

- Follow-up: approved shell results now include short internal hints about
  failed commands, truncated output, PATH refreshes, dev-server ambiguity,
  publish confirmation, and trusted local checks when relevant.
- Safety: follow-up hints do not execute commands, retry installs, or change the
  UI. Writer still has to request any next tool or shell approval explicitly.

## 0.1.50 - Setup-Aware Shell Approval

- UX: shell approval cards now use a neutral `Approval required` label and show
  concise risk notes for dependency installs, system installs, external source
  retrieval, publishing, dev servers, and generic shell commands.
- Context: after the user approves setup-like shell commands, Codey sends the
  Writer a bounded read-only `Setup Context` with local tool availability,
  project manifests, lockfiles, and scoped setup notes. The context is not a new
  model tool and is not injected into normal prompts.
- Safety: setup context never installs, clones, writes files, or networks on its
  own. It reuses sensitive-path filtering, avoids absolute tool paths, reports
  listing limits, and keeps shell execution behind the existing approval flow.

## 0.1.49 - Tool Start Visibility

- UX: Agent tools now emit a lightweight `tool_started` event before local
  execution begins. The web UI shows a quiet pending tool line such as
  `read app.py -> Reading app.py`, then replaces it with the final tool result.
- Design: production tool execution remains serial and observable. The pending
  line reuses the existing monochrome `.tool-line` style; there is no spinner,
  progress system, parallel runner, or ToolSpec registry.
- Safety: `tool_started` events are UI/CLI visibility only and do not count as
  execution evidence, reviewer recent-log facts, or progress toward task
  completion.

## 0.1.48 - Tool Function Injection and Parallel Probe

- Improvement: Agent runtime now supports explicit `AgentToolFns` injection, so
  tests and manual probes can replace tool functions without monkeypatching
  `codey.agents.runner` globals.
- UX decision: Codey keeps production `read`, `ls`, and `search` execution
  serial by default. A deterministic probe showed local wall-clock speedups from
  concurrent read-only batches, but serial tool events preserve step-by-step
  observability, which better matches Codey's quiet local developer-tool feel.
- Safety: bounded file scanning/search paths now check cooperative cancellation
  during long loops.
- Tests/probes: manual A/B probes now use explicit tool-function injection
  instead of monkeypatching `codey.agents.runner` globals. The read-only parallel probe
  remains as a script-local experiment and documents why production Codey does
  not enable read-only concurrency by default.

## 0.1.47 - Search Omission Coverage

- Bugfix: `grep` / `search` now reports non-UTF-8 and unreadable files instead
  of silently treating omitted files as a complete no-match result. The fix is
  intentionally local to the Writer search tool: existing oversized, read-budget,
  and bounded-scan messages are unchanged, and hidden advisor search is not
  migrated.

## 0.1.46 - Coverage-Aware References

`find_references` now reports when its bounded lexical scan skipped files that
could still contain references. The low-level reference scanner records compact
`ScanReport` facts for oversized files, unreadable files, and non-UTF-8 files
without exposing file contents. The Writer-facing tool renders those facts as a
short `Scan coverage` note and marks the tool result as truncated so the JSON
tool protocol also warns models not to treat omitted content as clean.

This is intentionally a narrow production slice. Hidden project-audit advisors
still receive the old low-level reference output, and Project Map, Verification
Map, persistent indexes, cache layers, and ScanPolicy profiles remain unchanged.
The manual scan-coverage A/B probe now reconstructs the old low-level baseline
and compares it with the production Writer coverage renderer.

Live A/B across DeepSeek, MiMo, Qwen, and GLM confirmed the intended behavior.
GLM's old baseline still made a false complete-scan claim and a confident
unused conclusion; the coverage arm made the skipped oversized file explicit
and produced a safe incomplete-scan answer.

## 0.1.45 - Provider Adapter Self-Repair

Codey can now attempt a bounded background repair when a web provider adapter
itself breaks and control-level or Flow-level recovery is not enough. Structural
provider failures can enqueue a deduplicated self-repair job without blocking
new user tasks. Failed repairs stay queued behind a cooldown instead of being
dropped.

Repair runs in a separate Python subprocess. A healthy helper model receives
only the broken provider id, bounded failure context, allowed adapter source
files, and read-only provider tests. It may modify only the target provider's
adapter files inside a temporary sandbox; core files, test files, registry,
tool runtime, server, supervisor, recovery, and safety modules remain outside
the v1 self-modification boundary.

Candidates must pass the repair policy, `py_compile`, Ruff, the corresponding
provider unit test module, and a neutral marker canary before they become a
local provisional override. Overrides load only through a child Provider worker
process, not the main Codey process. The child uses a fresh background tab in
the durable logged-in Codey browser profile, so it does not need cookie copying
and does not steal the user's current chat tab. The parent can close that
temporary tab by CDP target id before killing a stuck worker.

Adapter overrides are versioned under the local state directory, record the
built-in Codey base hash, promote from provisional to active only after natural
successes, and roll back after repeated structural failures. Helper models are
tried in sequence when a candidate is invalid, fails policy, fails checks, or
fails canary.

Live smoke verified the final shared-profile fresh-tab path across DeepSeek,
Qwen, MiMo, and GLM for both the repair helper and candidate worker canary.
Qwen exposed the key design fix: isolated profiles could look logged in but
fail to submit; fresh background tabs in the main Codey browser profile submit
normally.

## 0.1.44 - Focused Project Map

Project Map now adds a bounded `Focused subtree` section for task-aware deep
repository navigation. It scans source files under fixed file, directory,
single-file-size, total-byte, and output-character budgets, then shows only the
highest-scoring module with relative paths, source/test labels, and symbol
signatures. It does not show source bodies, create an index, persist data, add
UI, or call an extra planner model.

The Focused subtree is emitted only when a task is present and only when the
normal Symbol overview is likely insufficient for a larger or budget-limited
repository. When it appears, it replaces the ordinary Symbol overview so the
Project Map stays focused on the deep task-relevant module instead of spending
tokens on low-relevance early files.

Qwen readiness is also stricter and faster: Codey now waits for the chat input,
bootstrap signal, and two consecutive identical non-empty model selector reads
before sending. A cleared input alone no longer counts as a successful
submission; Qwen submit confirmation now requires a stop indicator or response
count increase.

Manual probes recorded that two pre-scope approaches did not earn production
adoption. The retained production path is the lighter layered map: on a deep
synthetic monorepo across DeepSeek, MiMo, Qwen, and GLM, Focused subtree
improved first-file selection from 0/16 to 16/16 while reducing prompt
characters from 53,424 to 33,564 in the post-merge live verification.

Internally, project task context preparation was extracted from `TaskRunner`
into `codey/project_task_context.py`. The builder owns verified facts, Project
Map rendering, checkpoint resume/start, checkpoint prompt construction, and
initial verification candidates. `TaskRunner` still owns Writer execution,
Review, receipts, conversation state, provider failover, and the explicit
evidence seed/invalidation calls.

The diff-review lifecycle is also isolated in `codey/review_coordinator.py`.
The coordinator handles diff retry before review, reviewability checks,
review-unavailable fallback, reviewer follow-up creation, repair dirty-state
tracking, and the narrow green-check inheritance rule after review repair.
Reviewer connection, Writer failover, receipts, ProjectFacts, and conversation
state remain in `TaskRunner`.

## 0.1.43 - Quiet UI Persistence and Sidebar Polish

UI state persistence is now debounced on the hot SSE path. Streaming turn,
tool, and info events coalesce their full localStorage/server saves, while
discrete user actions and terminal task events still flush immediately. This
reduces hidden serialization, network POST, and atomic-write churn during long
tasks without changing the persisted state shape.

Native browser `prompt()` and `confirm()` calls have been removed from the
sidebar. Chat and project rename now use inline inputs, and destructive menu
actions use a quiet two-step confirmation inside the existing monochrome menu.

Consecutive read-only tool rows now fold at render time into compact groups
such as `read · 5 files`. A single read/search/list/reference row remains
visible; only consecutive rows of the same safe kind are grouped. Edit, run,
shell, and error rows stay expanded.

Internally, Writer provider failover was extracted from `TaskRunner` into a
small tested state machine. The refactor does not change provider protocol or
user-facing behavior, but keeps provider takeover, shared turn budget, canary,
checkpoint refresh, and Stop priority easier to prove.

## 0.1.42 - Broader Checks and Quiet Markdown

Controlled `run` now accepts more common verification commands without opening
the unsafe shell path: `ruff check`, `ruff format --check`, `mypy`,
`python -m mypy`, `python -m ruff`, safe `make` targets, `bun test` or
allowed `bun run` scripts, and safe Deno test/lint/check/fmt forms. Mutating or
installing forms such as `ruff --fix`, `ruff format` without `--check`,
`mypy --install-types`, `make deploy`, `bun install`, and `deno run` remain
blocked.

Full verification suites now receive a 300-second timeout while quick commands
keep the existing 90-second budget. Timeout feedback explicitly states that a
timeout is not a test failure and asks the Writer to rerun a smaller subset
instead of guessing a code fix. Literal grep output also suggests narrowing the
query or path when it reaches the match limit.

Assistant replies in the local UI now render a small, monochrome Markdown
subset: code blocks, inline code, bold, headings, and simple lists. Code blocks
get a quiet copy button. There is still no syntax highlighting, no new color
palette, and no additional mode or panel.

## 0.1.41 - Smart Pagination Hint

Paged `read_file` results now include the exact next JSON tool call when more
content remains. Codey keeps the existing complete-line paging behavior and the
older `next offset` text, but adds a concrete `read_file` call with the same
path, next offset, and effective limit so the Writer does not have to infer how
to continue reading a large file.

The hint is generated with JSON escaping, appears only when another page exists,
and does not change read budgets, file contents, tool protocol, or truncation
semantics.

## 0.1.40 - Bounded Stacktrace Pruning

Controlled `run` output now folds obvious dependency stack frames before the
existing middle clipping budget is applied. Python dependency frames from
`site-packages`, `dist-packages`, `.venv`, and `venv` are folded together with
their immediate dependency source line. Node frames are folded only when they
are explicit `at ...` stack entries with a `:line:column` location inside
`node_modules` or `.pnpm`.

Project source frames, assertion messages, exception summaries, test names, and
ordinary logs are preserved. If no dependency stack frame is found, output is
returned byte-for-byte unchanged. The feature changes no tool protocol, exit
code, `ok`, `changed`, or `truncated` semantics.

## 0.1.39 - MiMo Typing Flow and Neutral Web Markers

MiMo completion recovery now reuses its explicit `data-is-typing` transition.
Flow observations distinguish true, false, and unavailable states, so a missing
attribute or DOM read error is never treated as completion. Recovery still
requires a previously observed typing state, an explicit transition to false,
non-empty output, and stable text; built-in completion remains the first path.

Short-answer, long-code, and deep-thinking live probes all observed the required
transition without post-completion growth. A forced-Flow run saved a provisional
rule on the first send and promoted it to active on the next natural send.
Browser-visible verification markers, temporary DOM attributes, page globals,
and clipboard sentinels are now product-neutral. Local-only configuration names
remain unchanged.

## 0.1.38 - Bounded Provider Flow Recovery

Provider recovery bundles can now carry one bounded web-chat state rule in
addition to verified controls. A Flow Recipe is built only from a fixed set of
boolean observations, contains no selectors, JavaScript, URLs, arbitrary
actions, page text, or project data, and shares the existing provisional,
promotion, failure-counting, and rollback lifecycle.

Completion recovery requires stable non-empty output plus a real transition
from generation evidence to terminal evidence; stable text alone is never
enough. Qwen is the first completion pilot through its visible-to-hidden stop
transition. MiMo and GLM deliberately keep their built-in completion behavior
when no equally reliable terminal evidence exists. Four-provider Edge/CDP
control fault injection passed, and a stricter Qwen live run completed with its
built-in completion check disabled, then reused and promoted the recovered
Flow on the next send.

## 0.1.37 - Python Syntax Regression Hint

Successful replacement edits to Python files now receive one bounded syntax
regression hint when the original file parsed successfully but the final edited
content does not. The edit remains written and successful: Codey does not
rollback, run commands, or treat the hint as a passed check. Existing-invalid
files, valid edits, non-Python files, and files above a 128K-character parsing
budget receive no hint.

DeepSeek, Qwen, MiMo, and GLM live A/B fault injection all avoided one failed
test run while preserving correct final code and independent test success.
Three providers also reduced turns or total tool calls; valid-edit controls
produced zero hints across all four providers.

## 0.1.36 - Provider Revival and Writer Takeover

Provider recovery now works as one bounded transaction across the message box,
send button, and answer read. When local discovery is uncertain, Codey can ask
up to three healthy sibling models to select among sanitized structural
candidates, verify the choice through one real send, save a provisional control
bundle atomically, promote it after the next natural success, and roll it back
after explicit repeated control failures.

A passive provider-health circuit now distinguishes structural failures,
transient errors, rate limits, uncertain submissions, and authentication or
challenge states. A Writer that fails with a typed provider-page error can hand
the unfinished task to a healthy sibling in a strict fresh chat using bounded
checkpoint facts. Switches and turn budgets remain bounded; Stop, ordinary
tool failures, protocol failures, and uncertain submissions never trigger an
unsafe resend. Four-provider Edge/CDP fault injection verified recovery and
persisted reuse for DeepSeek, Qwen, MiMo, and GLM.

## 0.1.35 - Default Post-edit Verification

Code changes now receive one bounded verification reminder when Codey can prove
there is a unique, runnable check for the changed files. Candidates come only
from previously successful checks or explicit pytest, npm, Cargo, and Go
project configuration. Existing green checks after the latest edit are reused;
documentation-only changes, ambiguous commands, missing executables, and
cross-ecosystem matches do not enable the gate. Codey never installs or runs a
command automatically, and a failed default check is not repeated forever.
Manifest candidates are refreshed at completion so edits to project scripts
cannot leave the gate using stale commands.

## 0.1.34 - Bounded Edit Failure Context

Failed exact replacements now return bounded current-file evidence when a
unique lexical anchor can be proven. Non-unique replacements report up to three
exact start lines. The write decision remains fully exact: Codey never applies
a closest match, never returns partial long lines as copyable code, and never
uses an in-memory partial batch as disk evidence. Normal successful edits have
no added prompt cost or output.

## 0.1.33 - Read-before-edit Guard

Added a run-scoped guard that rejects replacement edits to existing files until
the Writer has successfully read that file in the current agent run. Full
`content` writes are limited to new-file creation; existing files must use exact
replacements. Files created or changed during the run become known for follow-up
replacement edits. This keeps Symbol overview as navigation help without
letting it become a substitute for inspecting real file contents. DeepSeek and
GLM also auto-click their visible rate-limit retry buttons after a short
cooldown. The initial project prompt now omits absolute temporary paths and the
empty instructions section; repository instructions are included only when an
`AGENTS.md` or `CLAUDE.md` file exists.

## 0.1.32 - Bounded Symbol Overview

Added a task-aware Symbol overview inside the existing Project Map so the
Writer starts with better file and symbol navigation hints before its first
read. It remains bounded and local-only: no new UI, public tool, cache, index,
embedding, LSP, or source body injection. Qwen also gained a narrow recovery
for redirect aborts and one stalled-response retry.

## 0.1.31 - Structured Execution Evidence

Added a bounded in-memory execution ledger so Verification Map, Review, receipts, and successful project facts use the same read, search, edit, truncation, and post-edit check evidence.

## 0.1.30 - Simplified Navigation Tooling

Removed the withdrawn `outline_file` tool after live evaluation showed that Project Map, literal `grep`, `find_references`, and offset `read_file` formed the more reliable navigation path.

## 0.1.29 - Verification Map

Added a hidden, bounded map of test candidates and checks observed after the latest edit for the Reviewer. It is evidence for verification decisions, not impact or coverage proof.

## 0.1.28 - Durable Execution Checkpoint

Added session-scoped recovery facts for unfinished project work: changed-file hashes, fresh successful checks, the last edit or run action, and the stop reason.

## 0.1.27 - Find References and Bounded Scans

Added bounded lexical reference hints and a shared streaming scanner for references, grep, and hidden audits, with explicit incomplete-result reporting.

## 0.1.26 - Outline File Experiment

Introduced `outline_file` as a bounded navigation experiment. Natural-use evaluation later showed weak adoption, and the tool was fully removed in 0.1.30.

## 0.1.25 - Hidden Project Map

Added a bounded, read-only project structure map for Writer, hidden advisors, and Reviewer without indexing source, adding RAG, or exposing a new UI.

## 0.1.24 - Hidden Change Briefs

Added a private, bounded ChangeBrief shared by Writer and Reviewer, plus verified successful-change facts derived from real edits and checks.

## 0.1.23 - Browser Launch Robustness

Added Edge-first browser discovery with Chrome fallback, clearer WebView startup failure handling, and explicit truncation markers for tool and review results.

## 0.1.22 - Durable Conversation Handoff

Added a bounded visible-conversation excerpt to factual handoff when a browser-model context is no longer trusted.

## 0.1.21 - Durable Chat State

Persisted bounded sidebar and chat state, added quiet copy controls, and reconciled Send/Stop state across restarts.

## 0.1.20 - Quiet Chat Controls

Refined the compact chat controls and interaction states without adding a new workflow or mode.

## 0.1.19 - MiMo Answer Completion

Separated MiMo send-button detection from answer-completion detection and used the answer DOM to avoid premature completion.

## 0.1.18 - Provider Reliability

Tightened MiMo, Qwen, and GLM browser-state handling, local JSON protocol validation, and review-repair check freshness.

## 0.1.17 - Hidden MoA Layer

Added hidden owner-first multi-model advice for normal chat and new projects, plus bounded read-only advisor audits for existing projects.

## 0.1.16 - Plain Chat and Project Discussion

Kept New Chat project-free while allowing one project conversation to move naturally from discussion to reading and editing.

## 0.1.15 - GLM Provider

Added GLM as the fourth supported web model and consolidated provider registration and smoke selection.

## 0.1.14 - Protocol Efficiency and Safety

Unified the local tool contract, bounded safe parallel reads, paged large files, and made multi-replacement edits atomic.

## 0.1.13 - Runtime Ownership Cleanup

Unified Git and snapshot change handling, centralized runtime storage, and made provider-session ownership explicit.

## 0.1.12 - Resilient Run Reconciliation

Added bounded backend run snapshots and ordered UI reconciliation across refreshes and short connection interruptions.

## 0.1.11 - Responsive Stop

Made Stop interrupt provider waits, recovery, review, and controlled commands, and preserved both ends of long command output.

## 0.1.10 - ProfileDoctor Recovery

Added a bounded, sanitized second recovery step that can ask an already-open model to choose among structural browser-control candidates.

## 0.1.9 - Bounded Provider Recovery

Added versioned provider profiles and conservative, verified rediscovery for changed message boxes, send buttons, and answers.

## 0.1.8 - Durable Local Continuity

Persisted a small set of proven project commands, bounded factual chat snapshots, and non-Git recovery baselines.

## 0.1.7 - Structured Runtime

Introduced structured tool outcomes and events, separated task orchestration from HTTP transport, and removed UI parsing of prose logs.

## 0.1.6 - Hidden Context Handoff

Added bounded factual summarization and fresh-chat continuation near the shared context budget.

## 0.1.5 - Control Teaching Cleanup

Refined recovery and cleanup around user-taught browser controls while keeping teaching as a quiet last resort.

## 0.1.4 - Task Receipts

Added compact task receipts showing changed files, check status, and restore availability.

## 0.1.3 - Durable CDP Browser Reuse

Reused an existing Edge CDP browser and model tabs across Codey UI restarts before launching a new browser.

## 0.1.2 - Provider Status and Composer Shortcuts

Improved provider status feedback and keyboard-oriented message composer controls.

## 0.1.1 - Stability Smoke

Added release-level stability smoke coverage for the initial local browser-model workflow.

## 0.1.0 - Initial Bilingual Release

Published the first bilingual Codey release: a local-first bridge from supported web AI chats to controlled file editing, checks, diffs, and restore.
