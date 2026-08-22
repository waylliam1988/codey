# Changelog

[中文版本](CHANGELOG.zh-CN.md)

This file records Codey's release history. The newest release appears first.

## 0.4.8 - Safe Context Epoch + Capability Boundary v1

- Added `codey/context_epoch.py`: a pure stdlib-leaf projection over model
  visible context facts — `ContextEpoch` / `ContextAdmission` /
  `ContextSnapshot` read models, content-addressed `ctx_epoch:<16hex>` epoch
  ids derived from the outbound prompt bytes, stable `context_source_ref()`
  normalization, and `snapshot_from_rendered_sources()` which projects
  rendered sources into bounded admission records (digests, chars, budgets,
  refs only). The module performs no I/O and imports nothing from codey;
  an architecture test locks it as a projection-only leaf.
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
  id, and the fixed `provider_turn_boundary` admission reason. Prompt text,
  send order, and provider behavior are unchanged; parity stays locked by
  the existing byte-for-byte agent prompt test plus a new real-run test that
  also asserts the epoch metadata actually lands on the recorded section
  (this caught a double-wrapped trace sink during development).
- Run Trace: `PromptSectionTrace` gained optional `epoch_id`,
  `admission_reason`, and `capability_id` fields, serialized only when set —
  without them the manifest payload shape is unchanged. The prompt-section
  dedup key now includes the epoch id, so identical content re-admitted at a
  later boundary is still recorded while unchanged repeats stay deduplicated.
  `record_context_sources()` passes capability/admission metadata through.
- Capability Registry v1 completed its roadmap field set: specs now declare
  `trace_sections`, `context_sources`, `evidence_producer`, and
  `enabled_by_default`, validated against new `KNOWN_TRACE_SECTIONS` /
  `KNOWN_CONTEXT_SOURCES` allowlists at construction time. Registered the
  0.4.7 modules (`research_evidence_runtime`,
  `research_review_finding`) plus this version's boundaries (`context_epoch`,
  `consensus_advisors`) and filled factual ownership for existing specs:
  agent_runner owns eight coding context sources, local_context owns
  ghost_directive/ghost_continuity, policy_guard writes policy_decisions,
  and the object-model/ledger/proof-quality/query-planner/finding specs name
  the dedicated trace sections their projections produce. A new architecture
  test locks every `capability_id` literal stamped anywhere in production
  code to a registered capability.
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
  - The existing `codey.review.ReviewFinding` parser object is intentionally
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
  managed_outputs/events/ghost/codey.review/journal imports and no I/O tokens;
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
- Architecture tests now forbid `codey.managed_outputs` imports from research/review/ghost modules
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
- Added a shared `codey/research/citation_scanner.py` helper so the done
  compiler and report-quality gate use the same citation and source-id scan
  rules instead of drifting apart. The report-quality gate is now split into
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
  `codey.events.run_event_ui_payload()` and the research display-name mapping
  into `codey.events.display_tool()`. `TaskRunner` now calls this shared
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
  `codey.agent` globals.
- UX decision: Codey keeps production `read`, `ls`, and `search` execution
  serial by default. A deterministic probe showed local wall-clock speedups from
  concurrent read-only batches, but serial tool events preserve step-by-step
  observability, which better matches Codey's quiet local developer-tool feel.
- Safety: bounded file scanning/search paths now check cooperative cancellation
  during long loops.
- Tests/probes: manual A/B probes now use explicit tool-function injection
  instead of monkeypatching `codey.agent` globals. The read-only parallel probe
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
